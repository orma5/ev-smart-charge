"""
Tests for the web UI, with the database stubbed at the db module.

These render the real templates through Flask's test client rather than
asserting on view-function return values. A Jinja typo is the most likely way
this breaks, and it is invisible to any test that stops short of rendering.
"""
from datetime import datetime, timedelta

import pytest

import db
import web

CONFIG = {"PRICE_ZONE": "SE3"}

SETTINGS = {
    "departure_hour": 7,
    "charger_speed_kw": 11.0,
    "battery_capacity_kwh": 82.0,
    "charge_limit_percent": 80,
    "smart_charging_enabled": True,
}


def at(hour, minute, day=30):
    return datetime(2026, 8, day, hour, minute)


def run(hour, minute, state, percent, day=30, decision="recorded", error=None):
    return {
        "at": at(hour, minute, day),
        "car_captured_at": None,
        "charging_state": state,
        "battery_percent": percent,
        "target_percent": 80,
        "slot_start": at(hour, minute - minute % 15, day),
        "decision": decision,
        "quota_remaining": 14,
        "error": error,
    }


A_NIGHT = [
    run(17, 56, "CONNECT_CABLE", 70, decision="cable-disconnected"),
    run(18, 0, "READY_FOR_CHARGING", 70, decision="waiting-for-cheaper"),
    run(2, 0, "CHARGING", 70, day=31, decision="charging-cheap-slot"),
    run(2, 15, "CHARGING", 74, day=31, decision="charging-cheap-slot"),
    run(2, 30, "CHARGING", 77, day=31, decision="charging-cheap-slot"),
    run(2, 45, "READY_FOR_CHARGING", 80, day=31, decision="battery-full"),
    run(7, 0, "CONNECT_CABLE", 80, day=31, decision="cable-disconnected"),
]

PRICES = [{"time_start": at(h, m), "price": 2.0}
          for h in (18, 19) for m in (0, 15, 30, 45)]
PRICES += [{"time_start": at(h, m, day=31), "price": 0.5}
           for h in (2, 3) for m in (0, 15, 30, 45)]


class FakeConn:
    """psycopg connections are their own context manager; so is this."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client(monkeypatch):
    """A test client with every database call answered from memory."""
    monkeypatch.setattr(db, "connect", lambda config: FakeConn())
    monkeypatch.setattr(db, "load_settings", lambda conn: dict(SETTINGS))
    monkeypatch.setattr(db, "load_runs", lambda conn, since: list(A_NIGHT))
    monkeypatch.setattr(db, "load_prices", lambda conn, zone, since: list(PRICES))
    monkeypatch.setattr(db, "latest_run", lambda conn: A_NIGHT[-1])

    return web.create_app(CONFIG).test_client()


# --- Rendering --------------------------------------------------------------

def test_the_overview_reports_what_was_saved(client):
    """8.2 kWh bought at 0.50 instead of 2.00 is 12.30 kr saved."""
    body = client.get("/").get_data(as_text=True)

    assert "12.30" in body
    assert "saved vs charging on plug-in" in body


def test_the_overview_says_when_energy_is_an_estimate(client):
    """
    The number is inferred from state of charge, not metered. Saying so is not
    decoration - it is the difference between a figure someone can trust and
    one they will later feel misled by.
    """
    body = client.get("/").get_data(as_text=True)

    assert "there is no meter in the loop" in body


def test_the_history_page_renders_sessions_and_runs(client):
    body = client.get("/history").get_data(as_text=True)

    assert "Charging sessions" in body
    assert "Waiting for a cheaper slot" in body
    assert "<svg" in body


def test_a_failed_run_is_visible_in_the_history(monkeypatch, client):
    """The whole point of recording failures: they used to be invisible."""
    monkeypatch.setattr(db, "load_runs", lambda conn, since: [
        run(18, 4, None, None, decision="error", error="could not reach the Skoda API"),
    ])

    body = client.get("/history").get_data(as_text=True)

    assert "could not reach the Skoda API" in body


def test_an_empty_database_still_renders(monkeypatch, client):
    """First boot: no runs, no prices, nothing to divide by."""
    monkeypatch.setattr(db, "load_runs", lambda conn, since: [])
    monkeypatch.setattr(db, "load_prices", lambda conn, zone, since: [])
    monkeypatch.setattr(db, "latest_run", lambda conn: None)

    assert client.get("/").status_code == 200
    assert client.get("/history").status_code == 200


def test_healthz_does_not_touch_the_database(monkeypatch):
    """
    Readiness must mean "this pod serves", not "the NUC is up". If it touched
    Postgres, a database blip would pull the pod out of Traefik and take the
    settings screen down with it.
    """
    def explode(config):
        raise AssertionError("healthz connected to the database")

    monkeypatch.setattr(db, "connect", explode)

    assert web.create_app(CONFIG).test_client().get("/healthz").status_code == 200


def test_the_window_is_clamped_to_something_renderable(client):
    """A hand-edited ?days= must not turn into a year-long HTML table."""
    assert client.get("/?days=99999").status_code == 200
    assert client.get("/?days=nonsense").status_code == 200
    assert client.get("/?days=-5").status_code == 200


# --- Settings ---------------------------------------------------------------

def test_the_settings_form_shows_the_current_values(client):
    body = client.get("/settings").get_data(as_text=True)

    assert 'value="7"' in body
    assert "checked" in body


def test_saving_valid_settings_writes_and_redirects(monkeypatch, client):
    saved = []
    monkeypatch.setattr(db, "save_settings", lambda conn, settings: saved.append(settings))

    response = client.post("/settings", data={
        "departure_hour": "6",
        "charger_speed_kw": "7.4",
        "battery_capacity_kwh": "82",
        "charge_limit_percent": "90",
        "smart_charging_enabled": "on",
    })

    assert response.status_code == 302
    assert saved == [{
        "departure_hour": 6,
        "charger_speed_kw": 7.4,
        "battery_capacity_kwh": 82.0,
        "charge_limit_percent": 90,
        "smart_charging_enabled": True,
    }]


def test_an_out_of_range_value_is_rejected_without_saving(monkeypatch, client):
    """
    A departure hour of 25 would sit in the database breaking every decision
    quietly, so the form is where it has to be stopped.
    """
    saved = []
    monkeypatch.setattr(db, "save_settings", lambda conn, settings: saved.append(settings))

    response = client.post("/settings", data={
        "departure_hour": "25",
        "charger_speed_kw": "11",
        "battery_capacity_kwh": "82",
        "charge_limit_percent": "80",
    })

    assert saved == []
    assert "must be between 0 and 23" in response.get_data(as_text=True)


def test_a_rejected_form_keeps_what_was_typed(client):
    """Re-rendering with the values wiped would be its own small betrayal."""
    body = client.post("/settings", data={
        "departure_hour": "half seven",
        "charger_speed_kw": "11",
        "battery_capacity_kwh": "82",
        "charge_limit_percent": "80",
    }).get_data(as_text=True)

    assert "half seven" in body
    assert "must be a number" in body


def test_an_unchecked_toggle_reads_as_off():
    """An unchecked checkbox is absent from the form, not false."""
    settings, problems = web.parse_settings({
        "departure_hour": "7",
        "charger_speed_kw": "11",
        "battery_capacity_kwh": "82",
        "charge_limit_percent": "80",
    })

    assert problems == {}
    assert settings["smart_charging_enabled"] is False


# --- Charts -----------------------------------------------------------------

def test_the_daily_chart_is_empty_when_nothing_is_priced():
    assert web.daily_savings_chart([]) is None


def test_the_session_strip_marks_the_slots_it_charged_in():
    """
    The picture that answers "why then". Three of the session's slots were
    charged in, so three bars carry the highlight class.
    """
    import savings

    session = savings.reconstruct_sessions(A_NIGHT)[0]
    strip = web.session_chart(session, savings.by_slot(PRICES))

    assert str(strip).count("slot charged") == 3


def test_an_unpriced_session_has_no_strip():
    import savings

    session = savings.reconstruct_sessions(A_NIGHT)[0]

    assert web.session_chart(session, {}) is None


# --- Formatting -------------------------------------------------------------

def test_missing_figures_render_as_a_dash_not_a_zero():
    """A session that could not be priced saved an unknown amount, not zero."""
    assert web._kr(None) == web.DASH
    assert web._kwh(None) == web.DASH


def test_liveness_is_relative_but_times_are_absolute():
    assert web._ago(datetime.now() - timedelta(seconds=30)) == "just now"
    assert web._ago(datetime.now() - timedelta(minutes=12)) == "12 min ago"
    assert "23:45" in web._when(datetime(2026, 8, 30, 23, 45))


def test_a_decimal_comma_is_accepted():
    """
    A number input in a Swedish locale renders 11.0 as "11,0". Chrome hands it
    back normalised; not every browser does, and it is what a person typing the
    value will reach for regardless.
    """
    settings, problems = web.parse_settings({
        "departure_hour": "7",
        "charger_speed_kw": "7,4",
        "battery_capacity_kwh": "82",
        "charge_limit_percent": "80",
    })

    assert problems == {}
    assert settings["charger_speed_kw"] == 7.4
