"""
The web UI: what it is doing now, what it has done, what that saved, and the
settings that steer it.

Nothing here calls the Skoda API, and nothing here may be made to. The public
API allows 20 requests an hour shared across reads and commands, and the
scheduler needs all of them; a page that polled the car would let an open
browser tab stop the car from charging. Every screen reads Postgres only.

Charts are inline SVG generated here rather than drawn by a charting library.
Three screens of bars did not justify a build step, a package.json, or a CDN
dependency on a LAN application that has to work when the internet does not.
"""
from datetime import datetime, timedelta

from flask import Flask, redirect, render_template, request, url_for
from markupsafe import Markup

import db
import savings

DEFAULT_DAYS = 30

# Long enough to be an obvious mistake rather than a slow page: at four minutes
# a tick, a year of history is ~130k rows, which is fine for Postgres and not
# fine for one HTML table.
MAX_DAYS = 366

# An em dash for a figure that does not exist, and a non-breaking space so
# amounts never wrap between the number and its unit.
DASH = "—"
NBSP = " "


# What each recorded decision means in words. The stored values are terse
# because they are a machine's log; these are what a person reads.
DECISIONS = {
    "cable-disconnected": "Not plugged in",
    "battery-full": "At target charge",
    "disabled": "Smart charging off",
    "charging-now-no-time": "Charging - too little time to wait",
    "charging-cheap-slot": "Charging - cheap slot",
    "waiting-for-cheaper": "Waiting for a cheaper slot",
    "no-prices": "No prices - left unchanged",
    "error": "Failed",
}


def _kr(value):
    """Kronor, with a space for thousands as Swedish writes them."""
    if value is None:
        return DASH
    return f"{value:,.2f}".replace(",", NBSP) + NBSP + "kr"


def _kwh(value):
    if value is None:
        return DASH
    return f"{value:,.1f}".replace(",", NBSP) + NBSP + "kWh"


def _when(moment):
    """
    Absolute, and to the minute. Relative times ("2 days ago") read nicely
    and are useless here: whether a session started at 23:45 or 00:15 is the
    whole point of the thing.
    """
    return DASH if moment is None else f"{moment:%a %d %b, %H:%M}"


def _ago(moment):
    """Relative, for the one place it is the question being asked: liveness."""
    if moment is None:
        return "never"

    seconds = (datetime.now() - moment).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{moment:%d %b %H:%M}"


def create_app(config):
    """
    Build the app around a validated config.

    A factory rather than a module-level app so tests can hand it a fake
    config, and so nothing connects to Postgres at import time.
    """
    app = Flask(__name__)
    app.jinja_env.filters.update(
        kr=_kr, kwh=_kwh, when=_when, ago=_ago,
        decision=lambda value: DECISIONS.get(value, value),
    )

    def period():
        """The requested window, defaulting to a month and clamped to a year."""
        try:
            days = int(request.args.get("days", DEFAULT_DAYS))
        except ValueError:
            days = DEFAULT_DAYS
        days = max(1, min(days, MAX_DAYS))
        return days, datetime.now() - timedelta(days=days)

    @app.route("/healthz")
    def healthz():
        """
        Deliberately does not touch Postgres. Readiness should mean "this pod
        is serving", not "the NUC is up" - a database blip should not pull the
        pod out of Traefik and take the settings screen down with it.
        """
        return "ok", 200

    @app.route("/")
    def overview():
        days, since = period()
        with db.connect(config) as conn:
            sessions = _sessions(conn, config, since)
            latest = db.latest_run(conn)
            settings = db.load_settings(conn)

        return render_template(
            "overview.html",
            days=days,
            latest=latest,
            settings=settings,
            totals=_totals(sessions),
            chart=daily_savings_chart(sessions),
            sessions=list(reversed(sessions))[:5],
        )

    @app.route("/history")
    def history():
        days, since = period()
        with db.connect(config) as conn:
            sessions = _sessions(conn, config, since)
            prices = savings.by_slot(db.load_prices(conn, config["PRICE_ZONE"], since))
            runs = db.load_runs(conn, since)

        return render_template(
            "history.html",
            days=days,
            sessions=[
                (session, session_chart(session, prices))
                for session in reversed(sessions)
            ],
            # Newest first, and capped: this is for spotting the failures the
            # CronJob used to hide, not for reading a month of ticks.
            recent=list(reversed(runs))[:60],
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        with db.connect(config) as conn:
            if request.method == "POST":
                settings, problems = parse_settings(request.form)
                if not problems:
                    db.save_settings(conn, settings)
                    return redirect(url_for("settings_page", saved=1))
                return render_template(
                    "settings.html", settings=settings, problems=problems, saved=False
                )

            return render_template(
                "settings.html",
                settings=db.load_settings(conn),
                problems={},
                saved="saved" in request.args,
            )

    return app


def _sessions(conn, config, since):
    """Every session in the window, priced. Oldest first."""
    settings = db.load_settings(conn)
    runs = db.load_runs(conn, since)
    prices = savings.by_slot(db.load_prices(conn, config["PRICE_ZONE"], since))

    return [
        savings.summarise(session, settings, prices)
        for session in savings.reconstruct_sessions(runs)
    ]


def _totals(sessions):
    """
    Headline figures for the window.

    Sessions that could not be priced are counted separately rather than as
    zero, so a total that is missing a night says so instead of quietly
    reading low.
    """
    priced = [s for s in sessions if s["savings"] is not None]

    return {
        "sessions": len(sessions),
        "energy_kwh": sum(s["energy_kwh"] for s in sessions),
        "actual_cost": sum(s["actual_cost"] for s in priced),
        "baseline_cost": sum(s["baseline_cost"] for s in priced),
        "savings": sum(s["savings"] for s in priced),
        "unpriced": len(sessions) - len(priced),
    }


# --- Form parsing -----------------------------------------------------------

# name -> (label, cast, low, high). The bounds are what makes a typo in a text
# field unable to reach the car: a departure hour of 25 or a battery of 0 kWh
# would otherwise sit in the database and quietly break every decision.
FIELDS = {
    "departure_hour": ("Departure hour", int, 0, 23),
    "charger_speed_kw": ("Charger speed (kW)", float, 0.1, 350.0),
    "battery_capacity_kwh": ("Battery capacity (kWh)", float, 1.0, 500.0),
    "charge_limit_percent": ("Charge limit fallback (%)", int, 1, 100),
}


def parse_settings(form):
    """
    Validate a submitted settings form.

    Returns (settings, problems). `settings` always has every key, holding the
    submitted value where it parsed, so an invalid form can be re-rendered with
    what the user typed rather than throwing their edits away.
    """
    settings = {}
    problems = {}

    for name, (label, cast, low, high) in FIELDS.items():
        # A number input in a Swedish locale displays 11.0 as "11,0". Chrome
        # normalises that back before submitting, but not every browser does,
        # and a decimal comma is what someone typing the value by hand will
        # reach for anyway.
        raw = (form.get(name) or "").strip().replace(",", ".")
        try:
            value = cast(raw)
        except ValueError:
            settings[name] = raw
            problems[name] = f"{label} must be a number"
            continue

        settings[name] = value
        if not low <= value <= high:
            problems[name] = f"{label} must be between {low} and {high}"

    # An unchecked checkbox is absent from the form rather than false.
    settings["smart_charging_enabled"] = "smart_charging_enabled" in form

    return settings, problems


# --- Charts -----------------------------------------------------------------
# Plain SVG built as strings. Colours come from CSS custom properties so the
# charts follow the page into dark mode instead of needing a second palette.

def daily_savings_chart(sessions, width=760, height=190):
    """Savings per day across the window, one bar per day that had a session."""
    by_day = {}
    for session in sessions:
        if session["savings"] is not None:
            day = session["started_at"].date()
            by_day[day] = by_day.get(day, 0.0) + session["savings"]

    if not by_day:
        return None

    days = sorted(by_day)
    top = max(max(by_day.values()), 0.01)
    pad_bottom, pad_top = 22, 8
    plot = height - pad_bottom - pad_top
    step = width / len(days)
    bar = max(2.0, min(step - 3, 26.0))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img"'
        f' aria-label="Savings per day in kronor">'
    ]

    # A zero line, because a day where smart charging saved nothing (you
    # plugged in at midnight and the cheap slots were the only slots) is a real
    # outcome and should read as zero rather than as missing.
    parts.append(
        f'<line x1="0" y1="{pad_top + plot}" x2="{width}" y2="{pad_top + plot}"'
        f' class="axis"/>'
    )

    for index, day in enumerate(days):
        value = by_day[day]
        tall = max(1.0, plot * value / top)
        x = index * step + (step - bar) / 2
        y = pad_top + plot - tall
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{tall:.1f}"'
            f' rx="2" class="bar"><title>{day:%a %d %b}: {value:.2f} kr</title></rect>'
        )

    # Label the ends only. Anything denser overlaps at a month's width, and the
    # per-bar tooltips carry the exact dates anyway.
    parts.append(
        f'<text x="0" y="{height - 6}" class="tick">{days[0]:%d %b}</text>'
    )
    if len(days) > 1:
        parts.append(
            f'<text x="{width}" y="{height - 6}" class="tick" text-anchor="end">'
            f'{days[-1]:%d %b}</text>'
        )

    parts.append("</svg>")
    return Markup("".join(parts))


def session_chart(session, prices, width=300, height=44):
    """
    One session as a price strip: a bar per slot, highlighted where it charged.

    This is the picture that answers "why did it charge then" - the cheap
    slots are visibly the short ones, and the highlights should sit on them.
    """
    window = _session_slots(session)
    priced = [(slot, prices.get(slot)) for slot in window]
    known = [price for _, price in priced if price is not None]

    if not known:
        return None

    top = max(max(known), 0.01)
    charged = set(session["charging_slots"])
    step = width / len(priced)
    bar = max(1.0, step - 0.6)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="strip" role="img"'
        f' aria-label="Price per slot, with charging slots highlighted">'
    ]
    for index, (slot, price) in enumerate(priced):
        if price is None:
            continue
        tall = max(1.0, height * price / top)
        css = "slot charged" if slot in charged else "slot"
        parts.append(
            f'<rect x="{index * step:.2f}" y="{height - tall:.2f}" width="{bar:.2f}"'
            f' height="{tall:.2f}" class="{css}">'
            f'<title>{slot:%H:%M} - {price:.2f} kr/kWh</title></rect>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def _session_slots(session):
    """Every 15-minute slot the session spanned, plug-in to unplug."""
    slot = savings.slot_start(session["started_at"])
    end = session["ended_at"]

    slots = []
    while slot <= end and len(slots) < 4 * 24 * 3:
        slots.append(slot)
        slot += timedelta(minutes=savings.SLOT_MINUTES)
    return slots
