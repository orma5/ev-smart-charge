import math
import os
import sys
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Load environment variables from .env file (local dev only; in the cluster
# these come from the CronJob's env and the ev-smart-charge Secret).
load_dotenv()

# --- Rate limiting ----------------------------------------------------------
# Skoda's public API allows 20 requests per hour per API key, shared across
# reads AND commands, with no burst. That single number drives this whole
# design. The CronJob runs every 4 minutes (15 polls/hour), which leaves 5 for
# start/stop commands.
#
# Four minutes sounds slow for catching the cable going in, but the poll interval
# is the smaller half of that delay. We read a *snapshot*: carCapturedTimestamp
# in every response is when the car last reported to Skoda's cloud, not when we
# asked. Measured 2026-09-05: a parked car sat at a 37-minute-old snapshot, and
# plugging in pushed a fresh one within ~3-4 minutes, after which it refreshed
# every ~2-3 minutes while charging.
#
# So detection lag is the car's push plus our interval - about 8 minutes worst
# case, ~1.5 kWh at 11 kW. Polling faster cannot shrink the first term. Home
# Assistant was no better: its Skoda integration reads the same upstream
# snapshot, and was observed showing the identical 43-minute-old state.

SKODA_TIMEOUT = 10  # Public internet.
HA_TIMEOUT = 5      # Home Assistant is on the LAN.

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES

# The one charging state that means the cable is NOT in the car. Tested by
# exclusion rather than by listing the connected states, because the spec warns
# that new values may be added and clients must tolerate ones they do not know.
#
# Observed 2026-09-05: stopping a charge puts the car in READY_FOR_CHARGING, not
# CHARGING_INTERRUPTED as was assumed here - so the old explicit list would in
# fact have coped with that particular case. CHARGING_INTERRUPTED and
# DISCHARGING are still unobserved; exclusion is what keeps them, and anything
# added later, from reading as "cable unplugged" and never resuming.
CABLE_DISCONNECTED = "CONNECT_CABLE"


class SkodaError(Exception):
    """The car's state could not be read or a command could not be sent."""


class RateLimited(SkodaError):
    """The hourly request quota is exhausted."""


class HomeAssistantError(Exception):
    """Home Assistant could not be read, so the override state is unknown."""


def load_config():
    """
    Read and validate configuration, reporting every problem at once by name.
    """
    spec = {
        "PRICE_ZONE": str,
        "PRICE_BASE_URL": str,
        "DEPARTURE_HOUR": int,
        "EV_CHARGER_SPEED_KW": float,
        "EV_BATTERY_CAPACITY_KWH": float,
        "EV_CHARGE_LIMIT_PERCENT": int,
        "SKODA_API_BASE": str,
        "SKODA_API_KEY": str,
        "SKODA_VIN": str,
        # Home Assistant is still consulted for exactly one thing: the manual
        # override. It is a preference, not a property of the car, so the Skoda
        # API has no equivalent. This costs nothing against the Skoda quota.
        "HA_BASE_URL": str,
        "HA_TOKEN": str,
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
    """The next datetime at which the car must be ready."""
    departure = now.replace(hour=departure_hour, minute=0, second=0, microsecond=0)
    if departure <= now:
        departure += timedelta(days=1)
    return departure


def slots_to_departure(now, departure_hour):
    """
    How many 15-minute slots are usable before departure, counted from the start
    of the current slot so the slot in progress counts as available.
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


def cable_connected(charging_state):
    """True unless the car is asking for the cable to be plugged in."""
    return charging_state != CABLE_DISCONNECTED


def read_charging(payload):
    """
    Pull the fields we care about out of a vehicle response.

    The car is wrapped in a top-level "vehicle" key, with any errors beside it
    rather than inside it. Reading straight through to `charging` cost a live
    smoke test to find: every mocked fixture had been built to the unwrapped
    shape, so the whole suite passed against a payload the API never sends.

    Returns (state, battery_percent, target_percent). target_percent is None when
    the car does not report one, in which case the configured limit is used.
    """
    payload = payload or {}
    vehicle = payload.get("vehicle") or {}
    charging = vehicle.get("charging") or {}
    status = charging.get("status") or {}
    settings = charging.get("settings") or {}
    battery = status.get("battery") or {}

    state = status.get("state")
    if not state:
        raise SkodaError(f"no charging state in response (errors: {payload.get('errors')})")

    percent = battery.get("stateOfChargeInPercent")
    if percent is None:
        raise SkodaError("no battery state of charge in response")

    return state, int(percent), settings.get("targetStateOfChargeInPercent")


# --- Skoda public API -------------------------------------------------------

class Skoda:
    """
    Minimal client for the Skoda public API, tracking the hourly quota.

    Every response carries RateLimit-Remaining. We keep the most recent value so
    that a decision to send a command can check whether there is budget left,
    rather than discovering it as a 429 at the worst possible moment.
    """

    def __init__(self, config):
        self.base = config["SKODA_API_BASE"].rstrip("/")
        self.vin = config["SKODA_VIN"]
        self.headers = {"X-API-Key": config["SKODA_API_KEY"], "accept": "application/json"}
        self.remaining = None

    def _record_quota(self, response):
        raw = response.headers.get("RateLimit-Remaining")
        if raw is not None:
            try:
                self.remaining = int(raw)
            except ValueError:
                pass

    def _request(self, method, path, **kwargs):
        url = f"{self.base}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=SKODA_TIMEOUT, **kwargs
            )
        except requests.RequestException as exc:
            raise SkodaError(f"could not reach the Skoda API ({method} {path}): {exc}") from exc

        self._record_quota(response)

        if response.status_code == 429:
            reset = response.headers.get("RateLimit-Reset", "unknown")
            raise RateLimited(f"hourly quota exhausted; resets in {reset}s")

        # 202 Accepted is the success case for the command endpoints.
        if response.status_code not in (200, 202):
            raise SkodaError(f"{method} {path} returned {response.status_code}: {response.text[:200]}")

        return response

    def vehicle(self):
        """Read the car. `include=charging` keeps the payload to what we use."""
        response = self._request(
            "GET", f"/api/v1/vehicles/{self.vin}", params={"include": "charging"}
        )
        return response.json()

    def set_charging(self, to_state):
        """
        Start or stop charging.

        The endpoint returns 202 Accepted: the car acts asynchronously, so this
        returning does not mean charging has actually begun. Nothing here waits
        for confirmation - checking would cost another request from the same
        quota, and the next scheduled run reads the real state anyway.
        """
        if to_state not in ("ON", "OFF"):
            print(f"Received unknown command for toggle charging: {to_state}")
            return

        action = "start" if to_state == "ON" else "stop"

        if self.remaining is not None and self.remaining < 1:
            raise RateLimited(f"no quota left to {action} charging")

        self._request("POST", f"/api/v1/vehicles/{self.vin}/charging/{action}")
        print(f"EV charging {action} requested (202 accepted, applied asynchronously)")


# --- Home Assistant (override only) -----------------------------------------

def smart_charging_enabled(config):
    """
    Read the manual override toggle.

    Raises rather than assuming: if we cannot tell whether the user has disabled
    smart charging, the safe move is to touch nothing.
    """
    url = f"{config['HA_BASE_URL']}/states/{config['HA_EV_SMART_CHARGING_BOOLEAN']}"
    headers = {"Authorization": f"Bearer {config['HA_TOKEN']}", "content-type": "application/json"}

    try:
        response = requests.get(url, headers=headers, timeout=HA_TIMEOUT)
    except requests.RequestException as exc:
        raise HomeAssistantError(f"could not reach Home Assistant: {exc}") from exc

    if response.status_code != 200:
        raise HomeAssistantError(f"Home Assistant returned {response.status_code}")

    state = response.json().get("state")
    if state in (None, "", "unknown", "unavailable"):
        raise HomeAssistantError(f"override state is '{state}'")

    return state == "on"


# --- Prices -----------------------------------------------------------------

def _fetch_day(config, date, since=None):
    """Fetch one day of 15-minute price slots. Returns [] if not published."""
    url = f"{config['PRICE_BASE_URL']}/{date:%Y}/{date:%m-%d}_{config['PRICE_ZONE']}.json"

    try:
        response = requests.get(url, timeout=SKODA_TIMEOUT)
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
    """Today's remaining slots, plus tomorrow's once they are published."""
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

    The car is read from and commanded through Skoda's public API. Home
    Assistant is consulted for one thing only: the manual override toggle.
    """
    print("** EV Smart charge run started **")
    config = load_config()
    now = datetime.now()

    if not smart_charging_enabled(config):
        print("Smart charging disabled, aborting")
        return

    skoda = Skoda(config)
    state, battery_percent, target_percent = read_charging(skoda.vehicle())
    print(f"Quota remaining this hour: {skoda.remaining}")

    if not cable_connected(state):
        print(f"Charger not connected (state: '{state}'), aborted")
        return

    # The car's own target wins when it reports one - it is the value set in the
    # MyŠkoda app, so there is one source of truth rather than two that drift.
    limit_percent = target_percent if target_percent is not None else config["EV_CHARGE_LIMIT_PERCENT"]
    print(f"Charging state: {state}, battery {battery_percent}%, target {limit_percent}%")

    if battery_percent >= limit_percent:
        print("Battery fully charged, aborting")
        return

    print(f"Current date is: {now}")
    print(f"Next departure hour is set to: {config['DEPARTURE_HOUR']}")

    slots_available = slots_to_departure(now, config["DEPARTURE_HOUR"])
    print(f"Number of 15-min slots to next departure is {slots_available}")

    slots_to_charge = slots_needed_to_charge(
        battery_percent,
        limit_percent,
        config["EV_BATTERY_CAPACITY_KWH"],
        config["EV_CHARGER_SPEED_KW"],
    )
    print(f"Number of 15-min slots needed to charge is: {slots_to_charge}")

    charging_now = state == "CHARGING"

    if slots_available <= slots_to_charge:
        print("Too few slots available to smart charge, charging now")
        if not charging_now:
            skoda.set_charging("ON")
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
        if not charging_now:
            skoda.set_charging("ON")
    else:
        schedule = cheapest_slots(prices, departure, slots_to_charge)
        print(f"Current slot is not cheap, stop charging. Charging schedule is: {schedule} ")
        if charging_now:
            skoda.set_charging("OFF")

    print("** EV Smart charge run ended **")


if __name__ == "__main__":
    try:
        main()
    except (SkodaError, HomeAssistantError) as exc:
        # Exit non-zero so a failed run is a failed Job rather than looking like
        # a successful decision to do nothing.
        print(f"Aborting: {exc}")
        sys.exit(1)
