"""
What the cheap-slot charging actually saved, reconstructed from the runs log.

Everything here is a pure function over rows already read from the database, so
it is testable without a database and - more usefully - it can be corrected and
re-run over all of history. That is the whole reason `sessions` is not a table:
a session is derived here, at read time, from the one append-only log.

Two honest limits are built into these numbers, and the UI says so:

1. Energy is inferred, not metered. There is no meter in the loop, so a
   session's energy is its state-of-charge delta times the battery capacity.
   Over a whole session the 1% quantisation is about +/-0.82 kWh on ~40 kWh,
   or ~2%. Between two consecutive polls it is larger than the energy that
   flowed, which is why per-slot deltas are not used - see attribute_energy.

2. The plug-in moment is known to within a few minutes. It is taken from the
   car's own carCapturedTimestamp rather than from when we polled, which
   removes most of that error, but not all of it.
"""
from datetime import timedelta

from main import SLOT_MINUTES, cable_connected, slot_start

SLOT_HOURS = SLOT_MINUTES / 60.0

# Below this, a session is treated as having delivered nothing. One percent of
# a large battery is under a kWh, and a session that never charged can still
# show a 1% drift from the car waking up and reporting again.
NEGLIGIBLE_KWH = 0.5


def by_slot(prices):
    """Turn the price rows the database returns into a slot -> price lookup."""
    return {price["time_start"]: price["price"] for price in prices}


# --- Sessions ---------------------------------------------------------------

def reconstruct_sessions(runs):
    """
    Split the runs log into plug-in-to-unplug sessions, oldest first.

    `runs` must be ordered by time. A run with no charging_state is a tick that
    failed before it could read the car; those neither open nor close a
    session, they are just carried along inside whichever one is open. Treating
    them as "unplugged" would split one night's charging into several sessions
    every time the cluster's DNS hiccuped.
    """
    sessions = []
    current = None

    for run in runs:
        state = run["charging_state"]

        if state is None:
            if current is not None:
                current["runs"].append(run)
            continue

        if cable_connected(state):
            if current is None:
                current = {"runs": []}
                sessions.append(current)
            current["runs"].append(run)
        else:
            current = None

    return [_describe(session["runs"]) for session in sessions]


def _describe(runs):
    """Summarise one session's runs into the fields the maths needs."""
    read = [run for run in runs if run["battery_percent"] is not None]
    first, last = read[0], read[-1]

    return {
        # The car's own timestamp, not ours: it is when the car reported being
        # plugged in, which is up to ~8 minutes before we heard about it. The
        # baseline is priced from this moment, so the difference is money.
        "started_at": first["car_captured_at"] or first["at"],
        "ended_at": last["car_captured_at"] or last["at"],
        "start_percent": first["battery_percent"],
        "end_percent": last["battery_percent"],
        "target_percent": last["target_percent"],
        "charging_slots": [
            run["slot_start"] for run in runs if run["charging_state"] == "CHARGING"
        ],
        "error_count": sum(1 for run in runs if run["error"]),
    }


def session_energy_kwh(session, battery_capacity_kwh):
    """
    Energy delivered across the whole session, from the state-of-charge delta.

    Negative deltas are floored at zero rather than reported: the car can spend
    charge on preconditioning while plugged in, and a negative "delivered"
    figure would make no sense in a savings total.
    """
    delta = session["end_percent"] - session["start_percent"]
    return max(0.0, delta / 100.0 * battery_capacity_kwh)


# --- Attribution ------------------------------------------------------------

def attribute_energy(session, battery_capacity_kwh):
    """
    Spread the session's energy across the slots it was charging in.

    Each slot's share is proportional to the number of polls that saw CHARGING
    in it, which with an even poll interval is proportional to time spent
    charging. A missing poll simply lowers that slot's weight rather than
    breaking the sum.

    This is the "hybrid" of the two obvious approaches, and it exists because
    neither works alone. Raw per-slot state-of-charge deltas are mostly
    quantisation noise: 1% of an 82 kWh battery is 0.82 kWh, while four minutes
    at 11 kW is 0.73 kWh, so consecutive polls jump between 0 and a whole
    percent regardless of what actually flowed. Modelling it as charger speed
    times time instead ignores the taper near the target and reads high. So the
    accurate number - the session total - is taken from the delta, and only its
    *distribution* is modelled.
    """
    energy = session_energy_kwh(session, battery_capacity_kwh)
    slots = session["charging_slots"]

    if energy < NEGLIGIBLE_KWH or not slots:
        return {}

    share = energy / len(slots)
    distribution = {}
    for slot in slots:
        distribution[slot] = distribution.get(slot, 0.0) + share
    return distribution


def baseline_energy(session, battery_capacity_kwh, charger_speed_kw):
    """
    Where that same energy would have gone if charging had simply started when
    the cable went in and run flat out until it was done.

    The first slot is pro-rated: plugging in at 18:07 gets eight minutes of the
    18:00 slot, not fifteen.
    """
    remaining = session_energy_kwh(session, battery_capacity_kwh)

    if remaining < NEGLIGIBLE_KWH or charger_speed_kw <= 0:
        return {}

    distribution = {}
    cursor = session["started_at"]

    while remaining > 1e-9:
        slot = slot_start(cursor)
        minutes_left = ((slot + timedelta(minutes=SLOT_MINUTES)) - cursor).total_seconds() / 60.0
        take = min(remaining, charger_speed_kw * (minutes_left / 60.0))

        distribution[slot] = distribution.get(slot, 0.0) + take
        remaining -= take
        cursor = slot + timedelta(minutes=SLOT_MINUTES)

    return distribution


def cost(distribution, prices):
    """
    What a distribution of kWh across slots cost, or None if it cannot be
    priced.

    None rather than a partial sum on purpose: a missing slot price would
    silently understate whichever side of the comparison it fell on, and an
    understated baseline flatters the savings figure. Better to show the
    session as unpriced than to show a wrong number confidently.
    """
    total = 0.0
    for slot, kwh in distribution.items():
        if slot not in prices:
            return None
        total += kwh * prices[slot]
    return total


# --- The number the UI shows ------------------------------------------------

def summarise(session, settings, prices):
    """
    One session's energy, what it cost, and what charging on plug-in would have.

    `savings` is None when either side could not be priced; the caller shows
    the session without a figure rather than guessing at one.
    """
    capacity = settings["battery_capacity_kwh"]

    actual = cost(attribute_energy(session, capacity), prices)
    baseline = cost(baseline_energy(session, capacity, settings["charger_speed_kw"]), prices)

    return {
        **session,
        "energy_kwh": session_energy_kwh(session, capacity),
        "actual_cost": actual,
        "baseline_cost": baseline,
        "savings": None if actual is None or baseline is None else baseline - actual,
    }
