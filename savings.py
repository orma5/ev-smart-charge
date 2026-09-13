"""
What the cheap-slot charging actually saved, reconstructed from the runs log.

Everything here is a pure function over rows already read from the database, so
it is testable without a database and - more usefully - it can be corrected and
re-run over all of history. That is the whole reason `sessions` is not a table:
a session is derived here, at read time, from the one append-only log.

Two honest limits are built into these numbers, and the UI says so:

1. Energy is metered where the charger reported it and inferred where it did
   not. Zaptec's own kWh counter is preferred; failing that, a session's
   energy is its state-of-charge delta times the battery capacity, where the
   1% quantisation is about +/-0.82 kWh on ~40 kWh, or ~2%. Between two
   consecutive polls that quantisation is larger than the energy that flowed,
   which is why per-slot deltas are never used - see attribute_energy.

   The two disagree by more than measurement error, and metered is the higher
   of the two: it includes the AC-to-DC and thermal losses that never reach
   the battery, typically 8-12%. That is the more correct figure for a cost
   comparison, because it is what crosses the meter you are billed on.

2. The plug-in moment is known to within a few minutes. It is taken from the
   car's own carCapturedTimestamp rather than from when we polled, which
   removes most of that error, but not all of it.

Every run is read through the charger first and the car second, because the
charger's answer is current and specific to home while the car's is a snapshot
that can be half an hour old. Rows written before Zaptec existed have no
charger reading at all, so both signals stay supported and the reconstruction
falls back per row rather than per session.
"""
from datetime import timedelta

import zaptec
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

def connected(run):
    """
    Whether one run saw a car plugged in, or None when it could not tell.

    The charger wins when it answered. It is both fresher and narrower - it
    reports the car on *our* charger, where the car reports a cable in
    anywhere - and after the gate went in, a tick that finds nothing on the
    charger returns before reading the car at all, so charging_state is null
    on exactly the rows the charger has already settled.
    """
    mode = run.get("zaptec_mode")
    if mode is not None:
        return zaptec.car_connected(mode)

    state = run["charging_state"]
    return None if state is None else cable_connected(state)


def charging(run):
    """Whether one run saw energy actually flowing."""
    mode = run.get("zaptec_mode")
    if mode is not None:
        return mode == zaptec.CHARGING

    return run["charging_state"] == "CHARGING"


def reconstruct_sessions(runs):
    """
    Split the runs log into plug-in-to-unplug sessions, oldest first.

    `runs` must be ordered by time. A run that could tell neither way - no
    charger reading and no charging state, so a tick that failed before it
    learned anything - neither opens nor closes a session; it is carried along
    inside whichever one is open. Treating those as "unplugged" would split one
    night's charging into several sessions every time the cluster's DNS
    hiccuped.
    """
    sessions = []
    current = None

    for run in runs:
        plugged_in = connected(run)

        if plugged_in is None:
            if current is not None:
                current["runs"].append(run)
            continue

        if plugged_in:
            if current is None:
                current = {"runs": []}
                sessions.append(current)
            current["runs"].append(run)
        else:
            current = None

    return [_describe(session["runs"]) for session in sessions]


def _describe(runs):
    """
    Summarise one session's runs into the fields the maths needs.

    A session can now contain no successful car reading at all: the charger
    says a car is on it while every Skoda request in the window failed. That
    was impossible before, because a session could only be opened by a reading
    that had already succeeded. Such a session has no percentages, and its
    energy has to come from the meter or not at all.
    """
    read = [run for run in runs if run["battery_percent"] is not None]
    first, last = (read[0], read[-1]) if read else (runs[0], runs[-1])

    return {
        # The car's own timestamp, not ours: it is when the car reported being
        # plugged in, which is up to ~8 minutes before we heard about it. The
        # baseline is priced from this moment, so the difference is money.
        "started_at": first["car_captured_at"] or first["at"],
        "ended_at": last["car_captured_at"] or last["at"],
        "start_percent": first["battery_percent"],
        "end_percent": last["battery_percent"],
        "target_percent": last["target_percent"],
        "charging_slots": [run["slot_start"] for run in runs if charging(run)],
        "metered_kwh": _metered_kwh(runs),
        "error_count": sum(1 for run in runs if run["error"]),
    }


def _metered_kwh(runs):
    """
    The session's energy according to the charger, or None if it never said.

    Zaptec's counter is cumulative within *its* idea of a session, which is not
    guaranteed to be ours: smart charging stops and restarts the car several
    times a night, and if the charger treats a restart as a new session the
    counter goes back to zero mid-way through ours. So this sums increments and
    treats any decrease as a reset, which is correct whether or not that
    happens - rather than taking the maximum, which would silently drop
    everything before a reset.
    """
    readings = [
        run["session_energy_kwh"] for run in runs
        if run.get("session_energy_kwh") is not None
    ]
    if not readings:
        return None

    total = readings[0]
    for previous, current in zip(readings, readings[1:]):
        total += current - previous if current >= previous else current
    return total


def session_energy_kwh(session, battery_capacity_kwh):
    """
    Energy delivered across the whole session.

    The charger's meter is preferred over the state-of-charge delta wherever it
    reported one: it is a measurement rather than an inference, and it counts
    the conversion and thermal losses that the battery never sees but the
    electricity bill does.

    Where it is absent - every session before Zaptec, and any where the charger
    could not be reached - the delta is used as it always was. Negative deltas
    are floored at zero rather than reported: the car can spend charge on
    preconditioning while plugged in, and a negative "delivered" figure would
    make no sense in a savings total.
    """
    metered = session.get("metered_kwh")
    if metered is not None:
        return max(0.0, metered)

    if session["start_percent"] is None or session["end_percent"] is None:
        return 0.0

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
