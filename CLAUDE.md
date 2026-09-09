# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EV Smart Charge optimizes electric vehicle charging based on electricity spot prices. It reads the car and commands charging through **Skoda's official public API**, and fetches electricity prices in 15-minute slots from an external API (Swedish price zones, SEK/kWh).

It is a single long-running process doing two things: a scheduler thread deciding every 4 minutes whether to charge, and a Flask web UI for settings, charging history, and what smart charging has saved. State lives in Postgres on the NUC.

**Home Assistant is not used.** The manual override was its last remaining job, and that moved into the app's own `settings` table so the toggle sits beside the other preferences in the UI.

## The constraint that shapes everything

**Skoda's public API allows 20 requests per hour per API key, shared across reads *and* commands, with no burst — and it is poll-only, with no webhooks.** Check any new request against that budget before adding it. The scheduler ticks every 4 minutes (15 polls/hour), leaving 5 for start/stop.

This is also why **no web request may ever call the Skoda API.** Every screen reads Postgres only; a page that polled the car would let an open browser tab spend the scheduler's budget and stop the car charging.

The cadence is not about price resolution — prices move in 15-minute slots, so polling faster than that buys nothing there. It is about **detecting the cable being plugged in**: the car draws power the moment it is connected and this script is the only thing that can veto it.

But the polling interval is only half of that delay, and it is the smaller half. **The car reports to Skoda's cloud on its own schedule, and we read a snapshot.** Every response carries `carCapturedTimestamp` — the time the *car* last reported, not the time of the request. Measured on 2026-09-05:

- **At rest, that snapshot goes stale for tens of minutes.** A parked, unplugged car sat at a 37-minute-old snapshot across repeated polls.
- **A state change pushes promptly.** Plugging in moved the snapshot from 37 minutes old to *0.2 minutes* old, and it then refreshed every ~2-3 minutes while charging.

So worst-case detection lag is the car's push (~3-4 minutes, measured) *plus* the poll interval (up to 4 minutes): call it **~8 minutes, or ~1.5 kWh at 11 kW**. Polling faster cannot fix the first term, which is why 4 minutes costs little despite sounding slow.

This is not a regression from Home Assistant. HA's Skoda integration reads the same upstream snapshot — checked side by side, `sensor.skoda_enyaq_charging_state` was showing the identical 43-minute-old `connect_cable`. The previous every-minute cron was polling a value that only changes every ~30-40 minutes; its claimed ~0.18 kWh exposure was never real.

`carCapturedTimestamp` is stored on every run for a second reason: it dates a plug-in far more accurately than our poll time, and the savings baseline is priced from the moment the cable went in.

## How It Works

| File | Role |
| --- | --- |
| `app.py` | Entry point. Scheduler thread + waitress serving Flask. Owns `/livez`. |
| `main.py` | The decision: pure slot arithmetic, the Skoda client, `run_once`. |
| `db.py` | Postgres. Connections are opened per unit of work, not held. |
| `savings.py` | Sessions and savings, derived from the runs log. Pure functions. |
| `web.py` | Flask routes, form validation, inline-SVG charts. |
| `schema.sql` | Four tables, applied idempotently at every boot. |

Each scheduler tick (`main.run_once`):
1. Reads the car once: `GET /api/v1/vehicles/{vin}?include=charging`
2. Aborts if the cable is not connected, or the battery is already at target
3. Reads the override from `settings` — after the car, so aborts above cost nothing
4. Compares 15-minute slots remaining until departure vs slots needed to charge
5. If time is tight, charges immediately; otherwise charges only in the cheapest slots

**Every tick writes exactly one `runs` row, including the failures.** That is what the `finally` in `run_once` is for. Under the CronJob a failed run was visible only as a failed Job in kubectl; now it reaches the history screen, and the savings maths needs to know a tick happened but told us nothing.

There is deliberately **no `sessions` table**. A plug-in-to-unplug session is fully derivable from the ordered runs log, so `savings.reconstruct_sessions` derives it at read time. One append-only source of truth, and the reconstruction can be corrected and re-run over all of history.

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
- `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`

**The `settings` table** — user preferences, edited at `/settings`, seeded from the column defaults in `schema.sql` on first boot:
- `departure_hour`, `charger_speed_kw`, `battery_capacity_kwh`
- `charge_limit_percent` — fallback only; the car's own `targetStateOfChargeInPercent` wins when reported
- `smart_charging_enabled` — the manual override that used to be a Home Assistant toggle

Do not reintroduce the settings-table values as environment variables. Two sources of truth for the departure hour means one of them silently disagreeing with what the UI shows.

## Testing

Four test files, none of which need a network or a database:
- `test_main.py` — decision arithmetic, quota handling, what each tick records
- `test_savings.py` — session reconstruction and the savings maths
- `test_web.py` — routes rendered through Flask's test client, and form validation
- `test_app.py` — the scheduler loop survives a failing tick, and `/livez`

A break here has no loud symptom — the car simply does not charge overnight, or a plausible-looking number appears on a dashboard nobody can check by eye. The tests are the only safety net.

Two conventions worth keeping: keep the decision and savings logic as **pure functions taking explicit arguments**, and stub the database at the `db` module rather than faking psycopg. The web tests **render the real templates**, because a Jinja typo is the likeliest breakage and is invisible to anything that stops short of rendering.

`schema.sql` is not covered by any test. It gets its first execution when the pod boots.

## Deployment

CI (`.github/workflows/homelab-build-push.yml`) runs the tests, then builds an arm64 image tagged with the commit SHA and pushes it to the private homelab registry; `main` also promotes it to `:latest`.

Deployment lives in the `homebrain` repo (`home/k8s/apps/ev-smart-charge/`) on the k3s cluster: a Deployment, a Service, and a Traefik Ingress on `ev-smart-charge.home.dkms.se`. Apply with `make app-ev-smart-charge`; pick up a newly promoted image with `kubectl -n apps rollout restart deploy/ev-smart-charge`. There is still no deploy job in CI.

The container runs Python 3.14-alpine with `TZ=Europe/Stockholm` — the app uses naive local datetimes and compares them to price slots in Swedish local time, and stores them in `timestamp without time zone` columns for the same reason.

**`replicas: 1` and `strategy: Recreate` are load-bearing, not modest sizing.** Two pods would each poll on their own timer against the shared 20-requests-an-hour budget and could issue contradictory start/stop commands inside one price slot. The CronJob got single-flight behaviour free from `concurrencyPolicy: Forbid`; a Deployment has to be told.

The two probes answer different questions on purpose. `/healthz` never touches Postgres — readiness means "this pod serves", so a database blip does not pull the pod out of Traefik and take the settings screen down with it. `/livez` fails when no tick has completed for three intervals, which restores what `activeDeadlineSeconds` used to do: a wedged scheduler thread is otherwise invisible, because the pod goes on serving pages perfectly while the car never charges.
