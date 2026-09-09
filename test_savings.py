"""
Tests for the savings maths. No database, no network.

These matter more than they look. A wrong number here is not a crash, it is a
plausible-looking figure on a dashboard that nobody can check by eye - the
inputs are spread over a night and the arithmetic is the only thing that says
whether charging at 02:00 was worth it.
"""
from datetime import datetime

import pytest

import savings

# 82 kWh at 11 kW, matching the car this was written for.
SETTINGS = {"battery_capacity_kwh": 82.0, "charger_speed_kw": 11.0}


def at(hour, minute, day=30):
    return datetime(2026, 8, day, hour, minute)


def run(hour, minute, state, percent, day=30, captured=None, error=None, target=80):
    """One row of the runs log, in the shape the database returns it."""
    return {
        "at": at(hour, minute, day),
        "car_captured_at": captured,
        "charging_state": state,
        "battery_percent": percent,
        "target_percent": target,
        "slot_start": at(hour, minute - minute % 15, day),
        "decision": "recorded-by-a-test",
        "error": error,
    }


# One night: plugged in at 18:00 at 70%, charged in three cheap small-hours
# slots, sitting at 80% by 02:45. 10% of 82 kWh is 8.2 kWh.
A_NIGHT = [
    run(17, 56, "CONNECT_CABLE", 70),
    run(18, 0, "READY_FOR_CHARGING", 70),
    run(2, 0, "CHARGING", 70, day=31),
    run(2, 15, "CHARGING", 74, day=31),
    run(2, 30, "CHARGING", 77, day=31),
    run(2, 45, "READY_FOR_CHARGING", 80, day=31),
    run(7, 0, "CONNECT_CABLE", 80, day=31),
]

# Plugging in is expensive, the small hours are cheap.
PRICES = {at(hour, minute): 2.0 for hour in (18, 19) for minute in (0, 15, 30, 45)}
PRICES |= {at(hour, minute, day=31): 0.5 for hour in (2, 3) for minute in (0, 15, 30, 45)}


# --- Reconstruction ---------------------------------------------------------

def test_a_night_is_one_session():
    sessions = savings.reconstruct_sessions(A_NIGHT)

    assert len(sessions) == 1
    assert sessions[0]["start_percent"] == 70
    assert sessions[0]["end_percent"] == 80
    assert sessions[0]["charging_slots"] == [at(2, 0, 31), at(2, 15, 31), at(2, 30, 31)]


def test_unplugging_and_replugging_is_two_sessions():
    sessions = savings.reconstruct_sessions([
        run(18, 0, "READY_FOR_CHARGING", 70),
        run(19, 0, "CONNECT_CABLE", 70),
        run(20, 0, "READY_FOR_CHARGING", 70),
    ])

    assert len(sessions) == 2


def test_a_failed_tick_does_not_split_a_session():
    """
    The cluster's DNS fails a few times an hour. Treating a tick that could not
    read the car as "unplugged" would shatter one night into several sessions
    and invent a plug-in moment in the middle of the night for each of them.
    """
    sessions = savings.reconstruct_sessions([
        run(18, 0, "READY_FOR_CHARGING", 70),
        run(18, 4, None, None, error="could not reach the Skoda API"),
        run(18, 8, "READY_FOR_CHARGING", 70),
    ])

    assert len(sessions) == 1
    assert sessions[0]["error_count"] == 1


def test_the_session_starts_when_the_car_says_it_did():
    """
    We hear about a plug-in up to ~8 minutes late. The car's own timestamp is
    what the baseline is priced from, so it has to win over our poll time.
    """
    sessions = savings.reconstruct_sessions([
        run(18, 8, "READY_FOR_CHARGING", 70, captured=at(18, 1)),
    ])

    assert sessions[0]["started_at"] == at(18, 1)


def test_the_poll_time_is_used_when_the_car_reports_none():
    sessions = savings.reconstruct_sessions([run(18, 8, "READY_FOR_CHARGING", 70)])

    assert sessions[0]["started_at"] == at(18, 8)


# --- Energy -----------------------------------------------------------------

def test_energy_comes_from_the_state_of_charge_delta():
    session = savings.reconstruct_sessions(A_NIGHT)[0]

    assert savings.session_energy_kwh(session, 82.0) == pytest.approx(8.2)


def test_a_falling_state_of_charge_is_not_negative_energy():
    """A plugged-in car can spend charge on preconditioning."""
    session = savings.reconstruct_sessions([
        run(18, 0, "READY_FOR_CHARGING", 70),
        run(19, 0, "READY_FOR_CHARGING", 68),
    ])[0]

    assert savings.session_energy_kwh(session, 82.0) == 0.0


def test_energy_is_split_evenly_across_the_charging_slots():
    session = savings.reconstruct_sessions(A_NIGHT)[0]
    distribution = savings.attribute_energy(session, 82.0)

    assert set(distribution) == {at(2, 0, 31), at(2, 15, 31), at(2, 30, 31)}
    assert sum(distribution.values()) == pytest.approx(8.2)
    assert distribution[at(2, 0, 31)] == pytest.approx(8.2 / 3)


def test_a_session_that_never_charged_attributes_nothing():
    session = savings.reconstruct_sessions([
        run(18, 0, "READY_FOR_CHARGING", 80),
        run(19, 0, "READY_FOR_CHARGING", 80),
    ])[0]

    assert savings.attribute_energy(session, 82.0) == {}


# --- Baseline ---------------------------------------------------------------

def test_the_baseline_fills_consecutive_slots_from_the_plug_in():
    """8.2 kWh at 11 kW is 44.7 minutes, so three slots from 18:00."""
    session = savings.reconstruct_sessions(A_NIGHT)[0]
    distribution = savings.baseline_energy(session, 82.0, 11.0)

    assert sorted(distribution) == [at(18, 0), at(18, 15), at(18, 30)]
    assert distribution[at(18, 0)] == 2.75
    assert distribution[at(18, 15)] == 2.75
    assert sum(distribution.values()) == pytest.approx(8.2)


def test_the_first_baseline_slot_is_pro_rated():
    """
    Plugging in at 18:10 gets five minutes of the 18:00 slot, not fifteen.
    Without this the baseline would claim energy in a slot that had already
    mostly elapsed, and price it at that slot's rate.
    """
    session = savings.reconstruct_sessions([
        run(18, 10, "READY_FOR_CHARGING", 70),
        run(19, 0, "READY_FOR_CHARGING", 80),
    ])[0]
    distribution = savings.baseline_energy(session, 82.0, 11.0)

    # Five minutes at 11 kW.
    assert distribution[at(18, 0)] == pytest.approx(11.0 * 5 / 60)


# --- Pricing ----------------------------------------------------------------

def test_a_missing_price_makes_the_cost_unknown_rather_than_partial():
    """
    A partial sum would silently understate whichever side it fell on, and an
    understated baseline flatters the savings. Unpriced is the honest answer.
    """
    assert savings.cost({at(2, 0, 31): 4.0, at(9, 0): 4.0}, PRICES) is None


def test_cost_multiplies_each_slot_by_its_own_price():
    assert savings.cost({at(18, 0): 2.0, at(2, 0, 31): 2.0}, PRICES) == 2.0 * 2.0 + 2.0 * 0.5


# --- The whole thing --------------------------------------------------------

def test_the_worked_example():
    """
    8.2 kWh either way. Charging on plug-in would have bought all of it at
    2.00 kr/kWh; the cheap slots bought it at 0.50.
    """
    session = savings.reconstruct_sessions(A_NIGHT)[0]
    summary = savings.summarise(session, SETTINGS, PRICES)

    assert summary["energy_kwh"] == pytest.approx(8.2)
    assert summary["baseline_cost"] == pytest.approx(16.4)
    assert summary["actual_cost"] == pytest.approx(4.1)
    assert summary["savings"] == pytest.approx(12.3)


def test_savings_are_unknown_when_either_side_is_unpriced():
    session = savings.reconstruct_sessions(A_NIGHT)[0]
    summary = savings.summarise(session, SETTINGS, {})

    assert summary["savings"] is None
    assert summary["energy_kwh"] == pytest.approx(8.2)
