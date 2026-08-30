"""
Tests for the decision logic and the Skoda API client. No network required.

This app has no HTTP surface - when it breaks, the only symptom is a car that
did not charge overnight. These cover the arithmetic that decides that, and the
quota handling that the 20-requests-per-hour budget makes load-bearing.
"""
from datetime import datetime

import pytest

import main


def slots(*specs):
    """Build price slots from (hour, minute, price) triples."""
    return [
        {"time_start": datetime(2026, 8, 30, hour, minute), "price": price}
        for hour, minute, price in specs
    ]


CONFIG = {
    "SKODA_API_BASE": "https://api.example",
    "SKODA_VIN": "TMBJC7NY2MF016495",
    "SKODA_API_KEY": "key",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


# --- slot_start -------------------------------------------------------------

def test_slot_start_rounds_down_to_the_quarter():
    assert main.slot_start(datetime(2026, 8, 30, 6, 20)) == datetime(2026, 8, 30, 6, 15)
    assert main.slot_start(datetime(2026, 8, 30, 6, 14)) == datetime(2026, 8, 30, 6, 0)
    assert main.slot_start(datetime(2026, 8, 30, 6, 59)) == datetime(2026, 8, 30, 6, 45)


# --- next_departure ---------------------------------------------------------

def test_departure_later_today_stays_today():
    assert main.next_departure(datetime(2026, 8, 30, 6, 20), 7) == datetime(2026, 8, 30, 7, 0)


def test_departure_already_passed_rolls_to_tomorrow():
    assert main.next_departure(datetime(2026, 8, 30, 7, 30), 7) == datetime(2026, 8, 31, 7, 0)


def test_departure_exactly_now_rolls_to_tomorrow():
    """
    The boundary the original got wrong: it decided this twice, with `>` in one
    place and `>=` in the other, and disagreed with itself at the departure hour.
    """
    assert main.next_departure(datetime(2026, 8, 30, 7, 0), 7) == datetime(2026, 8, 31, 7, 0)


# --- slots_to_departure -----------------------------------------------------

def test_slots_to_departure_counts_the_slot_in_progress():
    assert main.slots_to_departure(datetime(2026, 8, 30, 6, 20), 7) == 3


def test_slots_to_departure_is_never_negative_after_departure_hour():
    """Regression: the original formula returned -2 here, which meant 'charge
    now at any price' for the whole 07:00-07:59 hour, every morning."""
    assert main.slots_to_departure(datetime(2026, 8, 30, 7, 30), 7) == 94


def test_slots_to_departure_across_midnight():
    assert main.slots_to_departure(datetime(2026, 8, 30, 23, 0), 7) == 32


# --- slots_needed_to_charge -------------------------------------------------

def test_no_slots_needed_when_already_at_limit():
    assert main.slots_needed_to_charge(80, 80, 77.0, 11.0) == 0
    assert main.slots_needed_to_charge(95, 80, 77.0, 11.0) == 0


def test_slots_needed_rounds_up():
    # 30% of 77 kWh = 23.1 kWh; at 11 kW that is 2.1 h = 8.4 slots -> 9.
    assert main.slots_needed_to_charge(50, 80, 77.0, 11.0) == 9


# --- cheapest_slots / should_charge_now -------------------------------------

def test_cheapest_slots_sorts_by_price_and_limits_count():
    prices = slots((1, 0, 0.9), (2, 0, 0.1), (3, 0, 0.5))
    picked = main.cheapest_slots(prices, datetime(2026, 8, 31, 7, 0), 2)
    assert [p["price"] for p in picked] == [0.1, 0.5]


def test_cheapest_slots_excludes_slots_after_departure():
    prices = slots((1, 0, 0.9), (8, 0, 0.01))
    picked = main.cheapest_slots(prices, datetime(2026, 8, 30, 7, 0), 5)
    assert [p["time_start"].hour for p in picked] == [1]


def test_charges_when_the_current_slot_is_among_the_cheapest():
    prices = slots((1, 0, 0.9), (2, 0, 0.1), (3, 0, 0.5))
    assert main.should_charge_now(prices, datetime(2026, 8, 30, 2, 5),
                                  datetime(2026, 8, 30, 7, 0), 1) is True


def test_does_not_charge_when_a_cheaper_slot_is_still_ahead():
    prices = slots((1, 0, 0.9), (2, 0, 0.1), (3, 0, 0.5))
    assert main.should_charge_now(prices, datetime(2026, 8, 30, 1, 5),
                                  datetime(2026, 8, 30, 7, 0), 1) is False


# --- cable_connected --------------------------------------------------------

def test_only_connect_cable_means_unplugged():
    assert main.cable_connected("CONNECT_CABLE") is False
    for state in ("CHARGING", "CONSERVING", "READY_FOR_CHARGING",
                  "DISCHARGING", "CHARGING_INTERRUPTED"):
        assert main.cable_connected(state) is True


def test_unknown_states_are_treated_as_connected():
    """
    The spec warns new values may be added and clients must tolerate them.
    Testing by exclusion means a new state does not read as 'unplugged' and
    silently stop the car charging.
    """
    assert main.cable_connected("SOME_FUTURE_STATE") is True


# --- read_charging ----------------------------------------------------------

def vehicle_payload(state="CHARGING", percent=55, target=80):
    return {
        "vin": "TMBJC7NY2MF016495",
        "charging": {
            "status": {"state": state, "battery": {"stateOfChargeInPercent": percent}},
            "settings": {"targetStateOfChargeInPercent": target},
        },
    }


def test_read_charging_extracts_the_three_fields():
    assert main.read_charging(vehicle_payload()) == ("CHARGING", 55, 80)


def test_read_charging_allows_a_missing_target():
    payload = vehicle_payload()
    del payload["charging"]["settings"]["targetStateOfChargeInPercent"]
    assert main.read_charging(payload) == ("CHARGING", 55, None)


def test_read_charging_raises_when_charging_is_absent():
    """A vehicle response can omit `charging` entirely and report it in errors."""
    with pytest.raises(main.SkodaError):
        main.read_charging({"vin": "x", "errors": [{"type": "CHARGING_UNSUPPORTED"}]})


def test_read_charging_raises_when_battery_is_absent():
    payload = vehicle_payload()
    payload["charging"]["status"]["battery"] = {}
    with pytest.raises(main.SkodaError):
        main.read_charging(payload)


# --- Skoda client: quota ----------------------------------------------------

def test_vehicle_records_remaining_quota(monkeypatch):
    def fake(method, url, **kwargs):
        assert kwargs["params"] == {"include": "charging"}
        return FakeResponse(200, vehicle_payload(), {"RateLimit-Remaining": "14"})

    monkeypatch.setattr(main.requests, "request", fake)
    skoda = main.Skoda(CONFIG)

    assert skoda.vehicle()["vin"] == "TMBJC7NY2MF016495"
    assert skoda.remaining == 14


def test_429_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(
        main.requests, "request",
        lambda method, url, **kw: FakeResponse(429, {}, {"RateLimit-Reset": "600"}),
    )
    with pytest.raises(main.RateLimited):
        main.Skoda(CONFIG).vehicle()


def test_command_accepts_202(monkeypatch, capsys):
    monkeypatch.setattr(
        main.requests, "request",
        lambda method, url, **kw: FakeResponse(202, {}, {"RateLimit-Remaining": "4"}),
    )
    skoda = main.Skoda(CONFIG)
    skoda.remaining = 5
    skoda.set_charging("ON")

    assert "start requested" in capsys.readouterr().out


def test_command_refuses_when_quota_is_gone(monkeypatch):
    def explode(method, url, **kw):
        raise AssertionError("no request should be sent with no quota left")

    monkeypatch.setattr(main.requests, "request", explode)
    skoda = main.Skoda(CONFIG)
    skoda.remaining = 0

    with pytest.raises(main.RateLimited):
        skoda.set_charging("OFF")


def test_an_unknown_command_sends_nothing(monkeypatch, capsys):
    def explode(method, url, **kw):
        raise AssertionError("no request should be sent for an unknown command")

    monkeypatch.setattr(main.requests, "request", explode)
    main.Skoda(CONFIG).set_charging("SIDEWAYS")

    assert "unknown command" in capsys.readouterr().out


# --- config -----------------------------------------------------------------

def test_missing_configuration_names_every_missing_variable(monkeypatch):
    for name in ("PRICE_ZONE", "DEPARTURE_HOUR", "SKODA_API_KEY", "SKODA_VIN"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as caught:
        main.load_config()

    message = str(caught.value)
    assert "PRICE_ZONE is not set" in message
    assert "DEPARTURE_HOUR is not set" in message
    assert "SKODA_API_KEY is not set" in message
    assert "SKODA_VIN is not set" in message


def test_malformed_number_is_reported_with_its_value(monkeypatch):
    monkeypatch.setenv("DEPARTURE_HOUR", "seven")

    with pytest.raises(SystemExit) as caught:
        main.load_config()

    assert "DEPARTURE_HOUR='seven' is not a valid int" in str(caught.value)
