# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EV Smart Charge is a Python script that optimizes electric vehicle charging based on electricity spot prices. It reads the car and commands charging through **Skoda's official public API**, and fetches electricity prices in 15-minute slots from an external API (Swedish price zones, SEK/kWh).

Home Assistant is used for exactly one thing: the manual override toggle (`input_boolean.smart_charging`). That is a user preference rather than a property of the car, so the Skoda API has no equivalent.

## The constraint that shapes everything

**Skoda's public API allows 20 requests per hour per API key, shared across reads *and* commands, with no burst — and it is poll-only, with no webhooks.** Check any new request against that budget before adding it. The CronJob runs every 4 minutes (15 polls/hour), leaving 5 for start/stop.

The cadence is not about price resolution — prices move in 15-minute slots, so polling faster than that buys nothing there. It is about **detecting the cable being plugged in**: the car draws power the moment it is connected and this script is the only thing that can veto it, so the polling interval *is* the window of expensive charging.

## How It Works

`main.py` is the entire application — a single-file script run on a schedule. Each run:
1. Checks the manual override in Home Assistant (local, free)
2. Reads the car once: `GET /api/v1/vehicles/{vin}?include=charging`
3. Aborts if the cable is not connected, or the battery is already at target
4. Compares 15-minute slots remaining until departure vs slots needed to charge
5. If time is tight, charges immediately; otherwise charges only in the cheapest slots

Charging commands (`POST .../charging/start|stop`) return **202 Accepted** — applied asynchronously, so the response is not confirmation. Nothing waits for it: confirming would cost another request from the same quota, and the next run reads the real state anyway.

Charging state is one of `CONNECT_CABLE`, `CHARGING`, `CONSERVING`, `READY_FOR_CHARGING`, `DISCHARGING`, `CHARGING_INTERRUPTED`. Connectedness is tested by *excluding* `CONNECT_CABLE` rather than by listing connected states, because the spec warns new values may be added and clients must tolerate them.

## Commands

```bash
# Run tests (no network needed)
pytest -q

# Run locally (requires .env file with config)
python main.py

# Install dependencies
pip install -r dev-requirements.txt   # includes requirements.txt

# Build Docker image
docker build -t ev-smart-charge .
```

## Configuration

All configuration is via environment variables (loaded from `.env` in local dev):
- `PRICE_ZONE`, `PRICE_BASE_URL` — electricity price API settings
- `DEPARTURE_HOUR` — target departure time (integer hour)
- `EV_CHARGER_SPEED_KW`, `EV_BATTERY_CAPACITY_KWH` — EV/charger specs
- `EV_CHARGE_LIMIT_PERCENT` — fallback only; the car's own `targetStateOfChargeInPercent` wins when reported
- `SKODA_API_BASE`, `SKODA_API_KEY`, `SKODA_VIN` — Skoda public API. The key is generated in the MyŠkoda app and sent as `X-API-Key`. There is no vehicle-list endpoint, so the VIN is configuration.
- `HA_BASE_URL`, `HA_TOKEN`, `HA_EV_SMART_CHARGING_BOOLEAN` — override toggle only

## Testing

`test_main.py` covers the decision arithmetic and the quota handling, with no network. This workload has no HTTP surface, so a break has no symptom other than a car that did not charge overnight — the tests are the only safety net. Keep the decision logic as pure functions taking explicit arguments so it stays that way.

## Deployment

CI (`.github/workflows/homelab-build-push.yml`) runs the tests, then builds a multi-arch (amd64 + arm64) image tagged with the commit SHA and pushes it to the private homelab registry; `main` also promotes it to `:latest`.

Deployment lives in the `homebrain` repo as a CronJob (`home/k8s/apps/ev-smart-charge/`) on the k3s cluster. There is no deploy job in CI: a CronJob has nothing to roll out, and the next run pulls `:latest` by itself. Container runs Python 3.13-alpine with `TZ=Europe/Stockholm` — the script uses naive local datetimes and compares them to price slots in Swedish local time.
