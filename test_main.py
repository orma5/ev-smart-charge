"""
Tests for the decision logic. No network, no configuration required.

This app has no HTTP surface - when it breaks, the only symptom is a car that
did not charge overnight. These cover the arithmetic that decides that.
"""
from datetime import datetime, timedelta

import main


def slots(*specs):
    """Build price slots from (hour, minute, price) triples."""
    return [
        {"time_start": datetime(2026, 8, 30, hour, minute), "price": price}
        for hour, minute, price in specs
    ]


# --- slot_start -------------------------------------------------------------

def test_slot_start_rounds_down_to_the_quarter():
    assert main.slot_start(datetime(2026, 8, 30, 6, 20)) == datetime(2026, 8, 30, 6, 15)
    assert main.slot_start(datetime(2026, 8, 30, 6, 14)) == datetime(2026, 8, 30, 6, 0)
    assert main.slot_start(datetime(2026, 8, 30, 6, 59)) == datetime(2026, 8, 30, 6, 45)


def test_slot_start_discards_seconds():
    assert main.slot_start(datetime(2026, 8, 30, 6, 15, 42)) == datetime(2026, 8, 30, 6, 15)


# --- next_departure ---------------------------------------------------------

def test_departure_later_today_stays_today():
    assert main.next_departure(datetime(2026, 8, 30, 6, 20), 7) == datetime(2026, 8, 30, 7, 0)


def test_departure_already_passed_rolls_to_tomorrow():
    assert main.next_departure(datetime(2026, 8, 30, 7, 30), 7) == datetime(2026, 8, 31, 7, 0)


def test_departure_exactly_now_rolls_to_tomorrow():
    """
    The boundary the old code got wrong: it decided this twice, with `>` in one
    place and `>=` in the other, and disagreed with itself at exactly the
    departure hour.
    """
    assert main.next_departure(datetime(2026, 8, 30, 7, 0), 7) == datetime(2026, 8, 31, 7, 0)


# --- slots_to_departure -----------------------------------------------------

def test_slots_to_departure_counts_the_slot_in_progress():
    # 06:20 -> 07:00 is 40 minutes, but the 06:15 slot is still usable, so 3.
    assert main.slots_to_departure(datetime(2026, 8, 30, 6, 20), 7) == 3


def test_slots_to_departure_is_never_negative_after_departure_hour():
    """Regression: the old formula returned -2 here."""
    assert main.slots_to_departure(datetime(2026, 8, 30, 7, 30), 7) == 94


def test_slots_to_departure_across_midnight():
    # 23:00 -> 07:00 next day is 8 hours.
    assert main.slots_to_departure(datetime(2026, 8, 30, 23, 0), 7) == 32


# --- slots_needed_to_charge -------------------------------------------------

def test_no_slots_needed_when_already_at_limit():
    assert main.slots_needed_to_charge(80, 80, 77.0, 11.0) == 0
    assert main.slots_needed_to_charge(95, 80, 77.0, 11.0) == 0


def test_slots_needed_rounds_up():
    # 30% of 77 kWh = 23.1 kWh; at 11 kW that is 2.1 h = 8.4 slots -> 9.
    assert main.slots_needed_to_charge(50, 80, 77.0, 11.0) == 9


def test_slots_needed_scales_with_the_gap():
    assert main.slots_needed_to_charge(70, 80, 100.0, 10.0) == 4  # exactly 1 h


# --- cheapest_slots ---------------------------------------------------------

def test_cheapest_slots_sorts_by_price_and_limits_count():
    prices = slots((1, 0, 0.9), (2, 0, 0.1), (3, 0, 0.5))
    picked = main.cheapest_slots(prices, datetime(2026, 8, 31, 7, 0), 2)
    assert [p["price"] for p in picked] == [0.1, 0.5]


def test_cheapest_slots_excludes_slots_after_departure():
    prices = slots((1, 0, 0.9), (8, 0, 0.01))
    picked = main.cheapest_slots(prices, datetime(2026, 8, 30, 7, 0), 5)
    assert [p["time_start"].hour for p in picked] == [1]


# --- should_charge_now ------------------------------------------------------

def test_charges_when_the_current_slot_is_among_the_cheapest():
    now = datetime(2026, 8, 30, 2, 5)
    prices = slots((1, 0, 0.9), (2, 0, 0.1), (3, 0, 0.5))
    assert main.should_charge_now(prices, now, datetime(2026, 8, 30, 7, 0), 1) is True


def test_does_not_charge_when_a_cheaper_slot_is_still_ahead():
    now = datetime(2026, 8, 30, 1, 5)
    prices = slots((1, 0, 0.9), (2, 0, 0.1), (3, 0, 0.5))
    assert main.should_charge_now(prices, now, datetime(2026, 8, 30, 7, 0), 1) is False


# --- toggle_charging --------------------------------------------------------

class FakeResponse:
    status_code = 200


def test_turning_on_does_not_report_an_unknown_command(monkeypatch, capsys):
    """
    Regression: the `else` was bound to the "OFF" test, so every successful
    "ON" also printed 'Received unknown command'. It did that 196 times in the
    NUC's logs before this was fixed.
    """
    monkeypatch.setattr(main.requests, "post", lambda **kwargs: FakeResponse())
    config = {"HA_BASE_URL": "http://ha", "HA_TOKEN": "t", "HA_EV_CHARGE_SWITCH": "switch.ev"}

    main.toggle_charging(config, "ON")

    output = capsys.readouterr().out
    assert "EV started charging" in output
    assert "unknown command" not in output


def test_turning_off_reports_stopped(monkeypatch, capsys):
    monkeypatch.setattr(main.requests, "post", lambda **kwargs: FakeResponse())
    config = {"HA_BASE_URL": "http://ha", "HA_TOKEN": "t", "HA_EV_CHARGE_SWITCH": "switch.ev"}

    main.toggle_charging(config, "OFF")

    output = capsys.readouterr().out
    assert "EV stopped charging" in output
    assert "unknown command" not in output


def test_an_actually_unknown_command_is_reported_and_sends_nothing(monkeypatch, capsys):
    def explode(**kwargs):
        raise AssertionError("no request should be sent for an unknown command")

    monkeypatch.setattr(main.requests, "post", explode)

    main.toggle_charging({}, "SIDEWAYS")

    assert "unknown command" in capsys.readouterr().out


# --- config -----------------------------------------------------------------

def test_missing_configuration_names_every_missing_variable(monkeypatch):
    for name in ("PRICE_ZONE", "DEPARTURE_HOUR", "HA_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    try:
        main.load_config()
    except SystemExit as exc:
        message = str(exc)
        assert "PRICE_ZONE is not set" in message
        assert "DEPARTURE_HOUR is not set" in message
        assert "HA_TOKEN is not set" in message
    else:
        raise AssertionError("expected SystemExit")


def test_malformed_number_is_reported_with_its_value(monkeypatch):
    monkeypatch.setenv("DEPARTURE_HOUR", "seven")

    try:
        main.load_config()
    except SystemExit as exc:
        assert "DEPARTURE_HOUR='seven' is not a valid int" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
