import math
import os
import sys
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Load environment variables from .env file (local dev only; in the cluster
# these come from the CronJob's env and the ev-smart-charge Secret).
load_dotenv()

# Every call gets a timeout. Without one a single hung request hung the whole
# run forever - and because cron triggered this with `docker start`, which is a
# no-op on an already-running container, that silently stopped smart charging
# until someone noticed the car was not full.
#
# The totals matter: this runs every minute, so a run must finish inside a
# minute or it starts blocking its own successor. Worst case here is three HA
# reads plus a switch call plus two price fetches - 40s, comfortably inside the
# CronJob's 60s activeDeadlineSeconds.
HA_TIMEOUT = 5      # Home Assistant is on the LAN.
PRICE_TIMEOUT = 10  # The price API is on the public internet.

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES

# Charger states that mean the cable is physically connected.
CONNECTED_STATES = {"ready_for_charging", "conserving", "charging"}


class HomeAssistantError(Exception):
    """Home Assistant could not be read, so the car's real state is unknown."""


def load_config():
    """
    Read and validate configuration.

    Reports *every* missing or malformed variable at once and names it. The
    previous version did `int(os.getenv('DEPARTURE_HOUR'))` at import time, so a
    missing variable surfaced as a bare TypeError with no clue which one it was.
    """
    spec = {
        "PRICE_ZONE": str,
        "PRICE_BASE_URL": str,
        "DEPARTURE_HOUR": int,
        "EV_CHARGER_SPEED_KW": float,
        "EV_BATTERY_CAPACITY_KWH": float,
        "EV_CHARGE_LIMIT_PERCENT": int,
        "HA_BASE_URL": str,
        "HA_TOKEN": str,
        "HA_EV_BATTERY_ENTITY": str,
        "HA_EV_CHARGE_SWITCH": str,
        "HA_EV_CHARGER_STATE": str,
        "HA_EV_SMART_CHARGING_BOOLEAN": str,
    }

    config = {}
    problems = []

    for name, cast in spec.items():
        raw = os.getenv(name)
        if raw is None or raw == "":
            problems.append(f"{name} is not set")
            continue
        try:
            config[name] = cast(raw)
        except ValueError:
            problems.append(f"{name}={raw!r} is not a valid {cast.__name__}")

    if problems:
        raise SystemExit("Configuration error:\n  " + "\n  ".join(problems))

    return config


# --- Pure logic -------------------------------------------------------------
# No network and no globals, so this is the part worth testing.

def slot_start(moment):
    """Round a datetime down to the start of its 15-minute price slot."""
    return moment.replace(
        minute=(moment.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def next_departure(now, departure_hour):
    """
    The next datetime at which the car must be ready.

    A departure hour that has already passed today means tomorrow. The old code
    decided this twice, once with `>` and once with `>=`; at exactly the
    departure hour the two disagreed and the slot count went negative.
    """
    departure = now.replace(hour=departure_hour, minute=0, second=0, microsecond=0)
    if departure <= now:
        departure += timedelta(days=1)
    return departure


def slots_to_departure(now, departure_hour):
    """
    How many 15-minute slots are usable before departure.

    Counted from the start of the current slot, so the slot in progress counts
    as available - matching the original behaviour.
    """
    remaining = next_departure(now, departure_hour) - slot_start(now)
    return int(remaining.total_seconds() // (SLOT_MINUTES * 60))


def slots_needed_to_charge(battery_percent, limit_percent, capacity_kwh, speed_kw):
    """How many 15-minute slots of charging it takes to reach the limit."""
    if battery_percent >= limit_percent:
        return 0

    kwh_to_charge = capacity_kwh / 100.0 * (limit_percent - battery_percent)
    hours_to_charge = kwh_to_charge / speed_kw
    return math.ceil(hours_to_charge * SLOTS_PER_HOUR)


def cheapest_slots(prices, departure, count):
    """The `count` cheapest price slots that fall before departure."""
    before_departure = [p for p in prices if p["time_start"] < departure]
    return sorted(before_departure, key=lambda p: p["price"])[:count]


def should_charge_now(prices, now, departure, slots_needed):
    """True if the slot in progress is one of the cheapest we still need."""
    current = slot_start(now)
    return any(p["time_start"] == current for p in cheapest_slots(prices, departure, slots_needed))


# --- Home Assistant ---------------------------------------------------------

def _ha_headers(token):
    return {"Authorization": f"Bearer {token}", "content-type": "application/json"}


def ha_state(config, entity):
    """
    Read one entity's state.

    Raises rather than returning "" on failure. The old code returned an empty
    string, which `int()` then blew up on - and in the one path that swallowed
    it, a Home Assistant blip made the script decide it needed 0 slots and
    switch charging *off* on a car that should have been charging.
    """
    url = f"{config['HA_BASE_URL']}/states/{entity}"

    try:
        response = requests.get(url, headers=_ha_headers(config["HA_TOKEN"]), timeout=HA_TIMEOUT)
    except requests.RequestException as exc:
        raise HomeAssistantError(f"could not reach Home Assistant for {entity}: {exc}") from exc

    if response.status_code != 200:
        raise HomeAssistantError(f"Home Assistant returned {response.status_code} for {entity}")

    state = response.json().get("state")
    # HA reports these literally when an integration is down or a device is
    # asleep - they are not numbers and must not be treated as one.
    if state in (None, "", "unknown", "unavailable"):
        raise HomeAssistantError(f"{entity} state is '{state}'")

    return state


def ha_battery_percent(config):
    state = ha_state(config, config["HA_EV_BATTERY_ENTITY"])
    try:
        return int(float(state))
    except ValueError as exc:
        raise HomeAssistantError(f"battery state {state!r} is not a number") from exc


def toggle_charging(config, to_state):
    """Turn the charger switch on or off."""
    if to_state not in ("ON", "OFF"):
        # Previously this branch was an `else` attached to the "OFF" test, so it
        # fired on every successful "ON" - 196 bogus errors in the NUC's logs.
        print(f"Received unknown command for toggle charging: {to_state}")
        return

    action = "turn_on" if to_state == "ON" else "turn_off"
    verb = "started" if to_state == "ON" else "stopped"
    url = f"{config['HA_BASE_URL']}/services/switch/{action}"

    try:
        response = requests.post(
            url=url,
            headers=_ha_headers(config["HA_TOKEN"]),
            json={"entity_id": config["HA_EV_CHARGE_SWITCH"]},
            timeout=HA_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HomeAssistantError(f"could not reach Home Assistant to {action}: {exc}") from exc

    if response.status_code == 200:
        print(f"EV {verb} charging")
    else:
        print(f"Failed to {verb.replace('ed', '')} charging, response status: {response.status_code}")


# --- Prices -----------------------------------------------------------------

def _fetch_day(config, date, since=None):
    """Fetch one day of 15-minute price slots. Returns [] if not published."""
    url = f"{config['PRICE_BASE_URL']}/{date:%Y}/{date:%m-%d}_{config['PRICE_ZONE']}.json"

    try:
        response = requests.get(url, timeout=PRICE_TIMEOUT)
    except requests.RequestException as exc:
        print(f"Could not fetch prices for {date:%Y-%m-%d}: {exc}")
        return []

    if response.status_code != 200:
        print(f"No prices published for {date:%Y-%m-%d} (status {response.status_code})")
        return []

    slots = []
    for price in response.json():
        starts = datetime.fromisoformat(price["time_start"]).replace(tzinfo=None)
        if since is None or starts >= since:
            slots.append({"time_start": starts, "price": price["SEK_per_kWh"]})
    return slots


def fetch_electricity_prices_from_date(config, date):
    """
    Today's remaining slots, plus tomorrow's once they are published.

    Nord Pool publishes the next day in the early afternoon, so asking before
    then just 404s.
    """
    print("Fetch electricity prices for today")
    prices = _fetch_day(config, date, since=date.replace(minute=0, second=0, microsecond=0))

    if date.hour > 13:
        print("Fetch electricity prices for tomorrow")
        prices += _fetch_day(config, date + timedelta(days=1))

    return prices


# --- Entry point ------------------------------------------------------------

def main():
    """
    Decide, from spot prices, whether the EV should be charging right now.

    1. Bail out unless smart charging is on, the cable is connected, and the
       battery is below the limit.
    2. Work out how many 15-minute slots remain before departure, and how many
       are needed to reach the charge limit.
    3. If time is tight, charge now. Otherwise charge only during the cheapest
       slots between now and departure.
    """
    print("** EV Smart charge run started **")
    config = load_config()
    now = datetime.now()

    if not ha_state(config, config["HA_EV_SMART_CHARGING_BOOLEAN"]) == "on":
        print("Smart charging disabled, aborting")
        return

    charging_state = ha_state(config, config["HA_EV_CHARGER_STATE"])
    if charging_state not in CONNECTED_STATES:
        print(f"Charger not connected (state: '{charging_state}'), aborted")
        return

    battery_percent = ha_battery_percent(config)
    if battery_percent >= config["EV_CHARGE_LIMIT_PERCENT"]:
        print("Battery fully charged, aborting")
        return

    print(f"Current date is: {now}")
    print(f"Next departure hour is set to: {config['DEPARTURE_HOUR']}")

    slots_available = slots_to_departure(now, config["DEPARTURE_HOUR"])
    print(f"Number of 15-min slots to next departure is {slots_available}")

    slots_to_charge = slots_needed_to_charge(
        battery_percent,
        config["EV_CHARGE_LIMIT_PERCENT"],
        config["EV_BATTERY_CAPACITY_KWH"],
        config["EV_CHARGER_SPEED_KW"],
    )
    print(f"Number of 15-min slots needed to charge is: {slots_to_charge}")

    if slots_available <= slots_to_charge:
        print("Too few slots available to smart charge, charging now")
        if charging_state != "charging":
            toggle_charging(config, "ON")
        print("** EV Smart charge run ended **")
        return

    prices = fetch_electricity_prices_from_date(config, now)
    departure = next_departure(now, config["DEPARTURE_HOUR"])

    if not prices:
        # No price data is not a reason to interrupt a charge in progress.
        print("No price data available, leaving charging state unchanged")
        print("** EV Smart charge run ended **")
        return

    if should_charge_now(prices, now, departure, slots_to_charge):
        print("Current slot is cheap, start or continue charging")
        if charging_state != "charging":
            toggle_charging(config, "ON")
    else:
        schedule = cheapest_slots(prices, departure, slots_to_charge)
        print(f"Current slot is not cheap, stop charging. Charging schedule is: {schedule} ")
        if charging_state == "charging":
            toggle_charging(config, "OFF")

    print("** EV Smart charge run ended **")


if __name__ == "__main__":
    try:
        main()
    except HomeAssistantError as exc:
        # Exit non-zero so a failed run is visible as a failed Job rather than
        # looking like a successful decision to do nothing.
        print(f"Aborting: {exc}")
        sys.exit(1)
