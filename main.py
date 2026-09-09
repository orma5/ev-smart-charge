import math
import os
import sys
import time
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

import db

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

# Cluster DNS resolution of the Skoda hostname fails intermittently (EAI_AGAIN,
# a few runs an hour), and one bad lookup used to lose the whole run. Two
# attempts fit inside the Job's 60s activeDeadlineSeconds alongside everything
# else the run does.
CONNECT_ATTEMPTS = 2
CONNECT_RETRY_DELAY = 2

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES

# A day is treated as fully published, and so never re-fetched, at this many
# slots. A normal day has 96; the spring DST day has 92 and the autumn one 100,
# so the threshold is the short day rather than 96, and a genuinely partial
# response simply gets fetched again on the next tick.
DAY_IS_COMPLETE = 92

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


def load_config():
    """
    Read and validate configuration, reporting every problem at once by name.

    What is left here is deployment facts only - where the services are and how
    to authenticate to them. Everything a person would want to change (the
    departure hour, the car's specs, the manual override) now lives in the
    settings table, where the web UI can edit it; see schema.sql.
    """
    spec = {
        "PRICE_ZONE": str,
        "PRICE_BASE_URL": str,
        "SKODA_API_BASE": str,
        "SKODA_API_KEY": str,
        "SKODA_VIN": str,
        "DATABASE_HOST": str,
        "DATABASE_PORT": int,
        "DATABASE_NAME": str,
        "DATABASE_USER": str,
        "DATABASE_PASSWORD": str,
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

    Returns (state, battery_percent, target_percent, captured_at).
    target_percent is None when the car does not report one, in which case the
    configured limit is used. captured_at is None when the car does not report
    one, which is tolerated rather than fatal - it is only used to date history
    more precisely, and no charging decision depends on it.
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

    captured = charging.get("carCapturedTimestamp") or status.get("carCapturedTimestamp")

    return state, int(percent), settings.get("targetStateOfChargeInPercent"), _local(captured)


def _local(timestamp):
    """
    Parse an API timestamp into a naive local datetime, or None.

    The API sends UTC with an offset; everything else in this application is
    naive Europe/Stockholm, because that is the clock the price slots are
    published against. astimezone() with no argument converts to whatever TZ
    the container is set to, which the deployment pins to Europe/Stockholm.
    """
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


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
        # Only a ConnectionError is retried: it means the request never reached
        # Skoda, so it cost no quota and re-sending cannot duplicate a command.
        # Anything else - a read timeout above all - may already have been
        # applied, and retrying it would spend a second request from the 20.
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                response = requests.request(
                    method, url, headers=self.headers, timeout=SKODA_TIMEOUT, **kwargs
                )
                break
            except requests.ConnectionError as exc:
                if attempt == CONNECT_ATTEMPTS:
                    raise SkodaError(
                        f"could not reach the Skoda API ({method} {path}): {exc}"
                    ) from exc
                print(f"Could not connect to the Skoda API, retrying in {CONNECT_RETRY_DELAY}s")
                time.sleep(CONNECT_RETRY_DELAY)
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


# --- Prices -----------------------------------------------------------------

def _fetch_day(config, date):
    """Fetch one whole day of 15-minute price slots. Returns [] if unpublished."""
    url = f"{config['PRICE_BASE_URL']}/{date:%Y}/{date:%m-%d}_{config['PRICE_ZONE']}.json"

    try:
        response = requests.get(url, timeout=SKODA_TIMEOUT)
    except requests.RequestException as exc:
        print(f"Could not fetch prices for {date:%Y-%m-%d}: {exc}")
        return []

    if response.status_code != 200:
        print(f"No prices published for {date:%Y-%m-%d} (status {response.status_code})")
        return []

    return [
        {
            "time_start": datetime.fromisoformat(price["time_start"]).replace(tzinfo=None),
            "price": price["SEK_per_kWh"],
        }
        for price in response.json()
    ]


def ensure_prices(conn, config, date):
    """
    Make sure a day's slots are in the database, fetching them if not.

    The whole day is stored, not just the slots still ahead of us: the savings
    baseline is priced from the moment the cable went in, which by then is in
    the past. Filtering to what the decision needs happens at read time.
    """
    if db.count_prices_for_day(conn, config["PRICE_ZONE"], date.date()) >= DAY_IS_COMPLETE:
        return

    print(f"Fetch electricity prices for {date:%Y-%m-%d}")
    slots = _fetch_day(config, date)
    if slots:
        db.store_prices(conn, config["PRICE_ZONE"], slots)


def prices_for_decision(conn, config, now):
    """
    The price slots the decision may choose between: today's, plus tomorrow's
    once they are published in the afternoon.

    The lower bound is the top of the current hour, which is what this has
    always used. Note it lets up to three already-elapsed slots of the current
    hour compete for the cheapest ranks, which can crowd out the slot in
    progress - pre-existing behaviour, left alone here on purpose so that
    moving prices into Postgres changes storage and nothing else.
    """
    ensure_prices(conn, config, now)
    if now.hour > 13:
        ensure_prices(conn, config, now + timedelta(days=1))

    since = now.replace(minute=0, second=0, microsecond=0)
    return db.load_prices(conn, config["PRICE_ZONE"], since)


# --- One scheduled tick -----------------------------------------------------

def run_once(conn, config):
    """
    Decide, from spot prices, whether the EV should be charging right now.

    Exactly one `runs` row is written per call, on every path including the
    failures, which is what the `finally` below is for. A tick that could not
    reach Skoda is a fact both the history screen and the savings maths need;
    under the CronJob it was visible only as a failed Job in kubectl.
    """
    print("** EV Smart charge run started **")
    now = datetime.now()
    skoda = Skoda(config)

    run = {
        "at": now,
        "slot_start": slot_start(now),
        "car_captured_at": None,
        "charging_state": None,
        "battery_percent": None,
        "target_percent": None,
        "quota_remaining": None,
        "decision": "error",
        "error": None,
    }

    try:
        _decide(conn, config, now, skoda, run)
    except SkodaError as exc:
        run["error"] = str(exc)
        raise
    finally:
        run["quota_remaining"] = skoda.remaining
        db.record_run(conn, run)
        print(f"** EV Smart charge run ended: {run['decision']} **")


def _decide(conn, config, now, skoda, run):
    """The decision itself, filling `run` in as it learns things."""
    settings = db.load_settings(conn)

    state, battery_percent, target_percent, captured_at = read_charging(skoda.vehicle())
    print(f"Quota remaining this hour: {skoda.remaining}")

    run.update(
        charging_state=state,
        battery_percent=battery_percent,
        target_percent=target_percent,
        car_captured_at=captured_at,
    )

    if not cable_connected(state):
        print(f"Charger not connected (state: '{state}'), aborted")
        run["decision"] = "cable-disconnected"
        return

    # The car's own target wins when it reports one - it is the value set in the
    # MyŠkoda app, so there is one source of truth rather than two that drift.
    limit_percent = (
        target_percent if target_percent is not None else settings["charge_limit_percent"]
    )
    print(f"Charging state: {state}, battery {battery_percent}%, target {limit_percent}%")

    if battery_percent >= limit_percent:
        print("Battery fully charged, aborting")
        run["decision"] = "battery-full"
        return

    # Read after the car, not before. The override only decides whether to
    # command the charger, so every abort above reaches the same outcome
    # without it. That ordering was forced by Home Assistant returning 502s
    # while it rebooted; it costs nothing to keep now that the toggle is a
    # column in the same database this row is about to be written to.
    if not settings["smart_charging_enabled"]:
        print("Smart charging disabled, aborting")
        run["decision"] = "disabled"
        return

    print(f"Current date is: {now}")
    print(f"Next departure hour is set to: {settings['departure_hour']}")

    slots_available = slots_to_departure(now, settings["departure_hour"])
    print(f"Number of 15-min slots to next departure is {slots_available}")

    slots_to_charge = slots_needed_to_charge(
        battery_percent,
        limit_percent,
        settings["battery_capacity_kwh"],
        settings["charger_speed_kw"],
    )
    print(f"Number of 15-min slots needed to charge is: {slots_to_charge}")

    charging_now = state == "CHARGING"

    if slots_available <= slots_to_charge:
        print("Too few slots available to smart charge, charging now")
        run["decision"] = "charging-now-no-time"
        if not charging_now:
            skoda.set_charging("ON")
        return

    prices = prices_for_decision(conn, config, now)
    departure = next_departure(now, settings["departure_hour"])

    if not prices:
        # No price data is not a reason to interrupt a charge in progress.
        print("No price data available, leaving charging state unchanged")
        run["decision"] = "no-prices"
        return

    if should_charge_now(prices, now, departure, slots_to_charge):
        print("Current slot is cheap, start or continue charging")
        run["decision"] = "charging-cheap-slot"
        if not charging_now:
            skoda.set_charging("ON")
    else:
        schedule = cheapest_slots(prices, departure, slots_to_charge)
        print(f"Current slot is not cheap, stop charging. Charging schedule is: {schedule} ")
        run["decision"] = "waiting-for-cheaper"
        if charging_now:
            skoda.set_charging("OFF")


# --- Entry point ------------------------------------------------------------

def main():
    """One tick, for local development. The deployed process schedules these."""
    config = load_config()
    with db.connect(config) as conn:
        db.init_schema(conn)
        run_once(conn, config)


if __name__ == "__main__":
    try:
        main()
    except SkodaError as exc:
        print(f"Aborting: {exc}")
        sys.exit(1)
