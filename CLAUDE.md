# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EV Smart Charge is a Python script that optimizes electric vehicle charging based on electricity spot prices. It integrates with Home Assistant to read EV battery state and control a charger switch, and fetches hourly electricity prices from an external API (Swedish price zones, SEK/kWh).

## How It Works

`main.py` is the entire application — a single-file script run on a schedule (via container/cron). Each run:
1. Checks if smart charging is enabled (Home Assistant boolean)
2. Checks if charger cable is connected and battery isn't already full
3. Compares hours remaining until departure vs hours needed to charge
4. If time is tight, charges immediately; otherwise picks the cheapest hours

## Commands

```bash
# Run locally (requires .env file with config)
python main.py

# Install dependencies
pip install -r requirements.txt

# Build Docker image
docker build -t ev-smart-charge .
```

## Configuration

All configuration is via environment variables (loaded from `.env` in local dev):
- `PRICE_ZONE`, `PRICE_BASE_URL` — electricity price API settings
- `DEPARTURE_HOUR` — target departure time (integer hour)
- `EV_CHARGER_SPEED_KW`, `EV_BATTERY_CAPACITY_KWH`, `EV_CHARGE_LIMIT_PERCENT` — EV/charger specs
- `HA_BASE_URL`, `HA_TOKEN` — Home Assistant connection
- `HA_EV_BATTERY_ENTITY`, `HA_EV_CHARGE_SWITCH`, `HA_EV_CHARGER_STATE`, `HA_EV_SMART_CHARGING_BOOLEAN` — Home Assistant entity IDs

## Deployment

Docker image built and pushed to a private homelab registry on every push via GitHub Actions (`.github/workflows/homelab-build-push.yml`). Container runs Python 3.13-alpine with `TZ=Europe/Stockholm`.
