# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EV Smart Charge optimizes electric vehicle charging based on electricity spot prices. It reads the car and commands charging through **Skoda's official public API**, reads the home charger through **Zaptec's cloud API**, and fetches electricity prices in 15-minute slots from an external API (Swedish price zones, SEK/kWh).

It is a single long-running process doing two things: a scheduler thread deciding every 4 minutes whether to charge, and a Flask web UI for settings, charging history, and what smart charging has saved. State lives in Postgres on the NUC.

**Home Assistant is not used.** The manual override was its last remaining job, and that moved into the app's own `settings` table so the toggle sits beside the other preferences in the UI.

## The constraint that shapes everything

**Skoda's public API allows 20 requests per hour per API key, shared across reads *and* commands, with no burst — and it is poll-only, with no webhooks.** Check any new request against that budget before adding it. The scheduler ticks every 4 minutes (15 polls/hour), leaving 5 for start/stop.

This is also why **no web request may ever call the Skoda API.** Every screen reads Postgres only; a page that polled the car would let an open browser tab spend the scheduler's budget and stop the car charging. The same rule holds for Zaptec despite its far looser limit — not to save requests, but because a screen that reads live from one API and Postgres for the rest shows two different moments side by side and calls them both "now".

The cadence is not about price resolution — prices move in 15-minute slots, so polling faster than that buys nothing there. It is about **detecting the cable being plugged in**: the car draws power the moment it is connected and this script is the only thing that can veto it.

But the polling interval is only half of that delay, and it is the smaller half. **The car reports to Skoda's cloud on its own schedule, and we read a snapshot.** Every response carries `carCapturedTimestamp` — the time the *car* last reported, not the time of the request. Measured on 2026-09-05:

- **At rest, that snapshot goes stale for tens of minutes.** A parked, unplugged car sat at a 37-minute-old snapshot across repeated polls.
- **A state change pushes promptly.** Plugging in moved the snapshot from 37 minutes old to *0.2 minutes* old, and it then refreshed every ~2-3 minutes while charging.

So worst-case detection lag is the car's push (~3-4 minutes, measured) *plus* the poll interval (up to 4 minutes): call it **~8 minutes, or ~1.5 kWh at 11 kW**. Polling faster cannot fix the first term, which is why 4 minutes costs little despite sounding slow.

This is not a regression from Home Assistant. HA's Skoda integration reads the same upstream snapshot — checked side by side, `sensor.skoda_enyaq_charging_state` was showing the identical 43-minute-old `connect_cable`. The previous every-minute cron was polling a value that only changes every ~30-40 minutes; its claimed ~0.18 kWh exposure was never real.

`carCapturedTimestamp` is stored on every run for a second reason: it dates a plug-in far more accurately than our poll time, and the savings baseline is priced from the moment the cable went in.

## Two APIs, and which owns what

The Zaptec charger answers a question Skoda answers badly, so since 2026-09-13 both are read. The split is not arbitrary and should not be blurred:

| | Skoda | Zaptec |
| --- | --- | --- |
| Budget | 20 requests/**hour**, reads and commands share it | 10 requests/**second** per account |
| Owns | battery %, target %, start/stop | is a car plugged in, and is it plugged in **here**, metered kWh |
| Freshness | a snapshot the car pushed on its own schedule | current; the charger is mains-powered and always online |

Neither API can do this alone. Zaptec has no idea what the state of charge is, and the whole decision is "how many slots to get from 34% to 80%" — so Skoda is not optional. Skoda's own location signals are all unavailable: measured 2026-09-13, `isVehicleInSavedLocation` was `false` while the car sat on the home charger (no saved locations exist), `chargingProfiles.profiles` was empty, and `parkingPosition` returned `PARKING_POSITION_DISABLED`. All three would need changes in the MyŠkoda app; the charger needs none.

**The charger is read first, before the car.** That ordering is the point: a tick that finds nothing on the home charger returns before spending a Skoda request, and that is most ticks. It also halves detection lag, because the charger's half of it is zero.

**Both reads fail open.** An unreachable Zaptec says nothing about where the car is, so the tick carries on exactly as it did before Zaptec existed. Refusing to charge on a cloud outage would lose a night's cheap electricity to a failure with no loud symptom.

**Start/stop deliberately stays on Skoda** even though Zaptec commands `501`/`502` would cost no quota. Cutting power at the charger is mechanically more reliable, but some cars treat a charger-side pause as a fault and will not resume on their own — and the cost of being wrong is a car that silently did not charge. Worth revisiting, but only with a test behind it.

Beware the key casing. The live Zaptec API returns `StateId`/`ValueAsString`; its own published schema says `stateId`/`valueAsString`. Building to the schema produced a client that found no observations at all. `_by_state_id` reads either, and `test_zaptec.py` covers both.

## How It Works

| File | Role |
| --- | --- |
| `app.py` | Entry point. Scheduler thread + waitress serving Flask. Owns `/livez`. |
| `main.py` | The decision: pure slot arithmetic, the Skoda client, `run_once`. |
| `zaptec.py` | The home charger: is a car on it, and how much has it delivered. |
| `db.py` | Postgres. Connections are opened per unit of work, not held. |
| `savings.py` | Sessions and savings, derived from the runs log. Pure functions. |
| `web.py` | Flask routes, form validation, inline-SVG charts. |
| `schema.sql` | Four tables, applied idempotently at every boot. |

Each scheduler tick (`main.run_once`):
1. Reads the charger once: `GET /api/chargers/{id}/state`, for observations `710` (operating mode) and `553` (session kWh)
2. Aborts as `not-at-home` if the charger reports `Disconnected` — **before** any Skoda request, so the common case is free
3. Reads the car once: `GET /api/v1/vehicles/{vin}?include=charging`
4. Aborts if the cable is not connected, or the battery is already at target
5. Reads the override from `settings` — after the car, so aborts above cost nothing
6. Compares 15-minute slots remaining until departure vs slots needed to charge
7. If time is tight, charges immediately; otherwise charges only in the cheapest slots

Connectedness is tested by *excluding* `Disconnected` (mode 1) rather than by listing the connected modes, for the same reason `CABLE_DISCONNECTED` is: mode 0 is the charger admitting it does not know, and a value we do not recognise must never be able to stop a charge.

**Every tick writes exactly one `runs` row, including the failures.** That is what the `finally` in `run_once` is for. Under the CronJob a failed run was visible only as a failed Job in kubectl; now it reaches the history screen, and the savings maths needs to know a tick happened but told us nothing.

There is deliberately **no `sessions` table**. A plug-in-to-unplug session is fully derivable from the ordered runs log, so `savings.reconstruct_sessions` derives it at read time. One append-only source of truth, and the reconstruction can be corrected and re-run over all of history.

Session boundaries come from `zaptec_mode` where a row has one and fall back to `charging_state` where it does not — **per row, not per session**, because every row written before 2026-09-13 has no charger reading and a log spanning the changeover must still reconstruct as one session. Two consequences follow. A session can now contain no successful car reading at all (charger sees a car, every Skoda request fails), which was impossible when only a successful reading could open one, so `_describe` must not assume it has percentages. And `session_energy_kwh` prefers the meter over the state-of-charge delta, which makes metered sessions read ~8-12% higher than inferred ones — the meter counts conversion and thermal losses that never reach the battery. That is the more correct figure for a cost comparison, because it is what you are billed for, but it means the savings number stepped up on the day this shipped and that is not a bug.

Zaptec's session counter is cumulative within *its* idea of a session, which is not guaranteed to be ours: smart charging stops and restarts the car several times a night. `_metered_kwh` therefore sums increments and treats any decrease as a counter reset, rather than taking the maximum, which would silently discard everything before a reset.

Charging commands (`POST .../charging/start|stop`) return **202 Accepted** — applied asynchronously, so the response is not confirmation. Nothing waits for it: confirming would cost another request from the same quota, and the next run reads the real state anyway.

Charging state is one of `CONNECT_CABLE`, `CHARGING`, `CONSERVING`, `READY_FOR_CHARGING`, `DISCHARGING`, `CHARGING_INTERRUPTED`. Connectedness is tested by *excluding* `CONNECT_CABLE` rather than by listing connected states, because the spec warns new values may be added and clients must tolerate them.

## Commands

```bash
# Run tests (no network, no database)
uv run pytest -q

# Run the whole thing locally (requires .env and a reachable Postgres)
uv run python app.py            # scheduler + UI on http://localhost:8000

# One decision, no web server - handy for checking a change against the real car
uv run python main.py

# Install dependencies
uv sync --group dev

# Build Docker image
docker build -t ev-smart-charge .
```

## Configuration

Configuration is split by who owns it, and the split matters: anything a person might want to change is in the database so the UI can edit it, and nothing lives in both places.

**Environment** — deployment facts only (loaded from `.env` in local dev):
- `PRICE_ZONE`, `PRICE_BASE_URL` — electricity price API settings
- `SKODA_API_BASE`, `SKODA_API_KEY`, `SKODA_VIN` — Skoda public API. The key is generated in the MyŠkoda app and sent as `X-API-Key`. There is no vehicle-list endpoint, so the VIN is configuration.
- `ZAPTEC_API_BASE`, `ZAPTEC_USERNAME`, `ZAPTEC_PASSWORD`, `ZAPTEC_CHARGER_ID` — Zaptec cloud API. Auth is an OAuth2 password grant against `/oauth/token`, which is the only flow Zaptec offers a script; the token lasts 24 hours and is cached on the client. Unlike Skoda there *is* a list endpoint, but the charger is pinned anyway: adding a second charger to the account must not be able to silently change which one the decision is about.
- `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`

**The `settings` table** — user preferences, edited at `/settings`, seeded from the column defaults in `schema.sql` on first boot:
- `departure_hour`, `charger_speed_kw`, `battery_capacity_kwh`
- `charge_limit_percent` — fallback only; the car's own `targetStateOfChargeInPercent` wins when reported

Two notes on the pair above, because both look more removable than they are now that Zaptec meters the energy.

`battery_capacity_kwh` **is** now a fallback in `savings` — the meter has replaced it for any session the charger recorded — but it is still load-bearing in the decision, where it turns a state-of-charge delta into the kWh that sets how many slots are needed. Zaptec cannot supply it: the charger has no idea how big the battery is, and neither does Skoda's API.

`charger_speed_kw` is likewise irreplaceable at planning time, because the car is usually `READY_FOR_CHARGING` when the decision runs and there is no power being delivered to measure.

The redundancy that *does* exist is between the two of them. `slots_needed_to_charge` reduces to `(capacity / speed) × ΔSoC/100`, so only their **ratio** ever reaches a decision — 82 kWh at 11 kW behaves identically to 41 kWh at 5.5 kW. They are two fields carrying one degree of freedom, and that one number, "how long a full charge takes", is now directly measurable from Skoda's state of charge against Zaptec's charging time. Collapsing them is a real option and deliberately not taken: it rewrites the core decision arithmetic, a derived value needs a history that can be empty or unrepresentative, and charge rate tapers near the target, so an average depends on which part of the range the session covered. Getting it wrong is silent — the car simply is not ready at 07:00.
- `smart_charging_enabled` — the manual override that used to be a Home Assistant toggle

Do not reintroduce the settings-table values as environment variables. Two sources of truth for the departure hour means one of them silently disagreeing with what the UI shows.

## Testing

Five test files, none of which need a network or a database:
- `test_main.py` — decision arithmetic, quota handling, what each tick records
- `test_zaptec.py` — parsing the charger state, token reuse, the connectedness rule
- `test_savings.py` — session reconstruction and the savings maths
- `test_web.py` — routes rendered through Flask's test client, and form validation
- `test_app.py` — the scheduler loop survives a failing tick, and `/livez`

A break here has no loud symptom — the car simply does not charge overnight, or a plausible-looking number appears on a dashboard nobody can check by eye. The tests are the only safety net.

Two conventions worth keeping: keep the decision and savings logic as **pure functions taking explicit arguments**, and stub the database at the `db` module rather than faking psycopg. The web tests **render the real templates**, because a Jinja typo is the likeliest breakage and is invisible to anything that stops short of rendering.

A third, learned twice now: **build fixtures to the shape the API actually sends, not to its documentation.** Skoda's top-level `vehicle` wrapper cost a live smoke test to find because every mock had been built unwrapped. Zaptec's `StateId` casing would have cost another — the whole suite passed against a payload the API never sends. When adding a fixture for a remote API, probe it once and copy what comes back.

`schema.sql` is not covered by any test. It gets its first execution when the pod boots.

## Deployment

CI (`.github/workflows/homelab-build-push.yml`) runs the tests, then builds an arm64 image tagged with the commit SHA and pushes it to the private homelab registry; `main` also promotes it to `:latest`.

Deployment lives in the `homebrain` repo (`home/k8s/apps/ev-smart-charge/`) on the k3s cluster: a Deployment, a Service, and a Traefik Ingress on `ev-smart-charge.home.dkms.se`. Apply with `make app-ev-smart-charge`; pick up a newly promoted image with `kubectl -n apps rollout restart deploy/ev-smart-charge`. There is still no deploy job in CI.

`load_config` validates every variable at boot and raises `SystemExit` naming all the missing ones, so **new configuration must reach the Secret before the image that needs it reaches the cluster**, or the pod crash-loops. The four `ZAPTEC_*` values are the current example.

The container runs Python 3.14-alpine with `TZ=Europe/Stockholm` — the app uses naive local datetimes and compares them to price slots in Swedish local time, and stores them in `timestamp without time zone` columns for the same reason.

**`replicas: 1` and `strategy: Recreate` are load-bearing, not modest sizing.** Two pods would each poll on their own timer against the shared 20-requests-an-hour budget and could issue contradictory start/stop commands inside one price slot. The CronJob got single-flight behaviour free from `concurrencyPolicy: Forbid`; a Deployment has to be told.

The two probes answer different questions on purpose. `/healthz` never touches Postgres — readiness means "this pod serves", so a database blip does not pull the pod out of Traefik and take the settings screen down with it. `/livez` fails when no tick has completed for three intervals, which restores what `activeDeadlineSeconds` used to do: a wedged scheduler thread is otherwise invisible, because the pod goes on serving pages perfectly while the car never charges.
