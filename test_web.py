"""
Tests for the web UI, with the database stubbed at the db module.

These render the real templates through Flask's test client rather than
asserting on view-function return values. A Jinja typo is the most likely way
this breaks, and it is invisible to any test that stops short of rendering.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

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
    monkeypatch.setattr(db, "latest_reading", lambda conn: A_NIGHT[-1])

    return web.create_app(CONFIG).test_client()


# --- Rendering --------------------------------------------------------------

def test_the_overview_reports_what_was_saved(client):
    """8.2 kWh bought at 0.50 instead of 2.00 is 12.30 kr saved."""
    body = client.get("/").get_data(as_text=True)

    assert "12.30" in body
    assert "Saved vs charging on plug-in" in body


def test_the_overview_says_the_decision_in_words(client):
    """An icon alone told a sighted reader too little; the words sit beside it."""
    body = client.get("/").get_data(as_text=True)

    assert "Not plugged in" in body


def test_the_overview_falls_back_to_the_last_battery_reading(monkeypatch, client):
    """
    Most ticks find nothing on the home charger and never read the car, so the
    latest run usually has no battery figure. The overview shows the last run
    that did, and says how old it is.
    """
    away = run(9, 0, None, None, day=31, decision="not-at-home")
    monkeypatch.setattr(db, "latest_run", lambda conn: away)

    body = client.get("/").get_data(as_text=True)

    assert 'class="soc">80%' in body
    assert "Battery as of" in body
    assert "Not on the home charger" in body


def test_the_overview_says_how_much_charge_is_left_to_add(monkeypatch, client):
    """34% to an 80% target on an 82 kWh battery is 37.7 kWh."""
    waiting = run(22, 40, "READY_FOR_CHARGING", 34, decision="waiting-for-cheaper")
    monkeypatch.setattr(db, "latest_run", lambda conn: waiting)
    monkeypatch.setattr(db, "latest_reading", lambda conn: waiting)

    body = client.get("/").get_data(as_text=True)

    assert "37.7" in body
    assert "Battery as of" not in body


def test_every_decision_has_an_icon():
    """One without would fall back to a question mark on the overview."""
    assert set(web.DECISION_ICONS) == set(web.DECISIONS)
    assert set(web.DECISION_ICONS.values()) | {"help"} <= set(web.ICONS)


def test_every_icon_a_template_names_is_drawn():
    """
    A name missing from ICONS renders an empty box - not even the name, as the
    icon font used to - so nothing on the page would say an icon had gone.
    """
    templates = Path(web.__file__).parent / "templates"
    names = set()
    for path in templates.glob("*.html"):
        names |= set(re.findall(r"icon\('(\w+)'", path.read_text(encoding="utf-8")))

    assert names
    assert names <= set(web.ICONS), names - set(web.ICONS)


def test_the_overview_says_when_energy_is_an_estimate(client):
    """
    Where the charger metered the energy the number is measured; where it did
    not, it is inferred from state of charge. Saying which is not decoration -
    it is the difference between a figure someone can trust and one they will
    later feel misled by, and the two are not interchangeable: the metered one
    is ~10% higher because it counts the charging losses.
    """
    body = client.get("/").get_data(as_text=True)

    assert "charger's own meter" in body
    assert "estimated" in body


# --- Installable on a phone -------------------------------------------------

def test_the_manifest_is_served_and_parses(client):
    """
    A malformed manifest does not break a page - the browser silently declines
    to offer "add to home screen" and nothing says why. Parsing it here is the
    only thing that would notice.
    """
    response = client.get("/static/manifest.json")

    assert response.status_code == 200

    manifest = json.loads(response.get_data(as_text=True))
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    # Chrome wants both sizes before it will treat the app as installable.
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}


def test_the_icons_the_manifest_promises_exist(client):
    """
    The manifest naming an icon that 404s is the same silent failure. These
    are separate files copied separately into the image, so the reference and
    the file can drift apart without anything complaining.
    """
    manifest = json.loads(client.get("/static/manifest.json").get_data(as_text=True))

    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]

    # iOS ignores the manifest entirely and looks for this one by convention.
    assert client.get("/static/apple-touch-icon.png").status_code == 200


def test_every_page_links_the_manifest_and_the_ios_icon(client):
    for path in ("/", "/history", "/settings"):
        body = client.get(path).get_data(as_text=True)

        assert 'rel="manifest"' in body, path
        assert 'rel="apple-touch-icon"' in body, path
        assert 'name="apple-mobile-web-app-capable"' in body, path


def test_the_overview_refreshes_itself(client):
    """
    Left on a home screen this page would otherwise claim "2 min ago" all
    night, which reads as the scheduler having died.
    """
    body = client.get("/").get_data(as_text=True)

    assert "location.reload()" in body
    assert "visibilityState" in body


def test_the_history_page_renders_sessions_and_runs(client):
    body = client.get("/history").get_data(as_text=True)

    assert "Charging sessions" in body
    assert "Waiting for a cheaper slot" in body
    assert "<svg" in body


def test_the_history_names_each_decision_in_words(client):
    body = client.get("/history").get_data(as_text=True)

    assert "Charging - cheap slot" in body
    assert "At target charge" in body


def test_repeated_decisions_collapse_into_one_row():
    """
    A night of waiting is a hundred identical ticks. Merged, the rows that
    differ are the ones left to read.
    """
    rows = web.collapse_runs(A_NIGHT)

    assert [row["decision"] for row in rows] == [
        "cable-disconnected",
        "battery-full",
        "charging-cheap-slot",
        "waiting-for-cheaper",
        "cable-disconnected",
    ]

    charging = rows[2]
    assert charging["count"] == 3
    assert (charging["started_at"], charging["ended_at"]) == (at(2, 0, day=31), at(2, 30, day=31))
    assert (charging["start_percent"], charging["end_percent"]) == (70, 77)


def test_failures_are_never_collapsed():
    """Each failure carries its own message, and they are what the list is for."""
    failed = [
        run(18, 0, None, None, decision="error", error="timed out"),
        run(18, 4, None, None, decision="error", error="timed out"),
    ]

    assert len(web.collapse_runs(failed)) == 2


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
    monkeypatch.setattr(db, "latest_reading", lambda conn: None)

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
