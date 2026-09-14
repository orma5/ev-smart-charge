"""
The web UI: what it is doing now, what it has done, what that saved, and the
settings that steer it.

Nothing here calls the Skoda API, and nothing here may be made to. The public
API allows 20 requests an hour shared across reads and commands, and the
scheduler needs all of them; a page that polled the car would let an open
browser tab stop the car from charging. Every screen reads Postgres only.

Charts are inline SVG generated here rather than drawn by a charting library.
Three screens of bars did not justify a build step or a package.json.
"""
from datetime import date, datetime, timedelta

from flask import Flask, redirect, render_template, request, url_for
from flask.json.provider import DefaultJSONProvider
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
    "not-at-home": "Not on the home charger",
    "battery-full": "At target charge",
    "disabled": "Smart charging off",
    "charging-now-no-time": "Charging - too little time to wait",
    "charging-cheap-slot": "Charging - cheap slot",
    "waiting-for-cheaper": "Waiting for a cheaper slot",
    "no-prices": "No prices - left unchanged",
    "error": "Failed",
}

# The icon each decision is drawn with, beside its words. Keys into ICONS.
DECISION_ICONS = {
    "cable-disconnected": "power_off",
    "not-at-home": "wrong_location",
    "battery-full": "battery_full",
    "disabled": "pause_circle",
    "charging-now-no-time": "timer",
    "charging-cheap-slot": "bolt",
    "waiting-for-cheaper": "hourglass_top",
    "no-prices": "money_off",
    "error": "error",
}

# Line icons on a 24px grid, stroked in currentColor so they take the colour of
# the text beside them. Inline SVG rather than an icon font: one drawing style
# across every screen, and nothing that renders as its own name while a font
# is still loading.
ICONS = {name: Markup(svg) for name, svg in {
    # Decisions
    "power_off": '<path d="M9 3v4M15 3v4M7 7h10v4a5 5 0 0 1-10 0zM12 16v5"/>',
    "wrong_location": '<path d="M4 11.5 12 5l8 6.5M6 10v10h12V10M3 3l18 18"/>',
    "battery_full": '<rect x="2.5" y="7" width="17" height="10" rx="2"/><path d="M22 10.5v3M7 12l2.5 2.5 5-5"/>',
    "pause_circle": '<circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/>',
    "timer": '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 10v3.5l2.5 1.5M9.5 2.5h5"/>',
    "bolt": '<path d="M13 2.5 4.5 13.5H11l-1 8 8.5-11H12z"/>',
    "hourglass_top": '<path d="M6 3h12M6 21h12M8 3v3.5l4 5.5 4-5.5V3M8 21v-3.5l4-5.5 4 5.5V21"/>',
    "money_off": '<path d="M20.5 12.5l-8 8a1.5 1.5 0 0 1-2.1 0l-6.9-6.9V3.5h10.1l6.9 6.9a1.5 1.5 0 0 1 0 2.1zM3 3l18 18"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V13M12 16.5v.01"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.4 2.3c-.6.3-.9.8-.9 1.4v.3M12 16.5v.01"/>',
    # Everything else
    "warning": '<path d="M12 3.5 21.5 20h-19zM12 10v4.5M12 17.5v.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "home": '<path d="M3 12l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    "history": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    # Sliders rather than a cog: at 22px a cog's teeth collapse into a circle
    # with spikes, which reads as a brightness icon.
    "tune": '<path d="M4 7h16M4 12h16M4 17h16"/><circle class="knob" cx="9" cy="7" r="2"/>'
            '<circle class="knob" cx="15" cy="12" r="2"/><circle class="knob" cx="8" cy="17" r="2"/>',
}.items()}


def _kr(value):
    """Kronor, with a space for thousands as Swedish writes them."""
    if value is None:
        return DASH
    return f"{value:,.2f}".replace(",", NBSP) + NBSP + "kr"


def _kwh(value):
    if value is None:
        return DASH
    return f"{value:,.1f}".replace(",", NBSP) + NBSP + "kWh"


def _pct(value):
    """
    A percentage, or a dash.

    A session can have no percentages at all now: the charger reports a car on
    it while every Skoda read in the window fails. Rare, but "None%" on a
    dashboard reads as a bug rather than as missing data.
    """
    return DASH if value is None else f"{value}%"


def _when(moment):
    """
    Absolute, and to the minute. Relative times ("2 days ago") read nicely
    and are useless here: whether a session started at 23:45 or 00:15 is the
    whole point of the thing.
    """
    return DASH if moment is None else f"{moment:%a %d %b, %H:%M}"


class _JSONProvider(DefaultJSONProvider):
    """
    Dates as ISO 8601 with no offset. Flask's default writes them as RFC 822
    dates marked GMT, and these are naive Stockholm times: a client believing
    the label would shift every one of them by an hour or two.
    """

    @staticmethod
    def default(o):
        if isinstance(o, date):
            return o.isoformat()
        return DefaultJSONProvider.default(o)


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
    app.json = _JSONProvider(app)
    app.jinja_env.filters.update(
        kr=_kr, kwh=_kwh, pct=_pct, when=_when, ago=_ago,
        decision=lambda value: DECISIONS.get(value, value),
        # A decision added in main.py without an icon here must still show
        # something, or the one thing the overview is for renders blank.
        decision_icon=lambda value: DECISION_ICONS.get(value, "help"),
    )
    app.jinja_env.globals["icons"] = ICONS

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
            reading = db.latest_reading(conn)
            settings = db.load_settings(conn)

        return render_template(
            "overview.html",
            days=days,
            latest=latest,
            reading=reading,
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
            recent=collapse_runs(runs)[:60],
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

    # --- JSON, for the mobile app -------------------------------------------
    # The screens above as data. Postgres only, for the same reason they are:
    # nothing a client asks for may spend the scheduler's Skoda budget.

    @app.route("/api/overview")
    def api_overview():
        days, since = period()
        with db.connect(config) as conn:
            sessions = _sessions(conn, config, since)
            latest = db.latest_run(conn)
            reading = db.latest_reading(conn)
            settings = db.load_settings(conn)

        return {
            "days": days,
            "latest": _api_run(latest),
            "reading": _api_run(reading),
            "settings": settings,
            "totals": _totals(sessions),
            "daily_savings": [
                {"day": day, "savings": value} for day, value in daily_savings(sessions)
            ],
            "sessions": list(reversed(sessions))[:5],
        }

    @app.route("/api/history")
    def api_history():
        days, since = period()
        with db.connect(config) as conn:
            sessions = _sessions(conn, config, since)
            prices = savings.by_slot(db.load_prices(conn, config["PRICE_ZONE"], since))
            runs = db.load_runs(conn, since)

        return {
            "days": days,
            "sessions": [
                {**session, "strip": session_strip(session, prices)}
                for session in reversed(sessions)
            ],
            "recent": [
                {
                    **row,
                    "decision_label": DECISIONS.get(row["decision"], row["decision"]),
                    "decision_icon": DECISION_ICONS.get(row["decision"], "help"),
                }
                for row in collapse_runs(runs)[:60]
            ],
        }

    @app.route("/api/settings", methods=["GET", "PUT"])
    def api_settings():
        with db.connect(config) as conn:
            if request.method == "GET":
                return db.load_settings(conn)

            # parse_settings reads a form: every value a string, the toggle
            # present only when on. Passed through as JSON, a departure hour of
            # 0 would read as blank.
            body = request.get_json(silent=True) or {}
            form = {name: str(body.get(name, "")) for name in FIELDS}
            if body.get("smart_charging_enabled"):
                form["smart_charging_enabled"] = "on"

            settings, problems = parse_settings(form)
            if problems:
                return {"settings": settings, "problems": problems}, 422
            db.save_settings(conn, settings)
            return settings

    return app


def _api_run(run):
    """The fields of one run the app shows, with its decision in words."""
    if run is None:
        return None
    return {
        "at": run["at"],
        "decision": run["decision"],
        "decision_label": DECISIONS.get(run["decision"], run["decision"]),
        "decision_icon": DECISION_ICONS.get(run["decision"], "help"),
        "charging_state": run["charging_state"],
        "battery_percent": run["battery_percent"],
        "target_percent": run["target_percent"],
        "error": run["error"],
    }


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


def collapse_runs(runs):
    """
    The runs log for reading: newest first, with back-to-back runs that saw
    the same state and decided the same thing merged into one row.

    At a tick every four minutes a night of waiting is a hundred identical
    rows, and a page of those buries the one row that differs. Failures are
    never merged - each carries its own message, and they are what this list
    is for.

    `runs` must be ordered oldest first, as db.load_runs returns them.
    """
    rows = []
    for run in runs:
        last = rows[-1] if rows else None
        percent = run["battery_percent"]

        if (last is not None and not run["error"] and not last["error"]
                and run["decision"] == last["decision"]
                and run["charging_state"] == last["charging_state"]):
            last["ended_at"] = run["at"]
            last["count"] += 1
            if percent is not None:
                last["end_percent"] = percent
                if last["start_percent"] is None:
                    last["start_percent"] = percent
            continue

        rows.append({
            "started_at": run["at"],
            "ended_at": run["at"],
            "count": 1,
            "decision": run["decision"],
            "charging_state": run["charging_state"],
            "error": run["error"],
            "start_percent": percent,
            "end_percent": percent,
        })

    return list(reversed(rows))


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

def daily_savings(sessions):
    """(day, kronor) for each day that had a priced session, oldest first."""
    by_day = {}
    for session in sessions:
        if session["savings"] is not None:
            day = session["started_at"].date()
            by_day[day] = by_day.get(day, 0.0) + session["savings"]
    return sorted(by_day.items())


def daily_savings_chart(sessions, width=760, height=120):
    """Savings per day across the window, one bar per day that had a session."""
    by_day = dict(daily_savings(sessions))

    if not by_day:
        return None

    days = list(by_day)
    top = max(max(by_day.values()), 0.01)
    pad_bottom, pad_top = 1, 8
    plot = height - pad_bottom - pad_top
    step = width / len(days)
    bar = max(2.0, min(step - 3, 26.0))

    # Stretched to the box CSS gives it rather than scaled, so a phone gets the
    # same bar height as a desktop instead of a chart a third as tall. That is
    # also why the date labels are HTML below it: text inside a scaled-down
    # SVG shrank to about 4px on a phone.
    parts = [
        '<div class="chart">'
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img"'
        f' aria-label="Savings per day in kronor">'
    ]

    # A zero line, because a day where smart charging saved nothing (you
    # plugged in at midnight and the cheap slots were the only slots) is a real
    # outcome and should read as zero rather than as missing.
    parts.append(
        f'<line x1="0" y1="{pad_top + plot}" x2="{width}" y2="{pad_top + plot}"'
        f' class="axis" vector-effect="non-scaling-stroke"/>'
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

    parts.append("</svg>")

    # Label the ends only. Anything denser overlaps at a month's width, and the
    # per-bar tooltips carry the exact dates anyway.
    parts.append(f'<div class="ticks"><span>{days[0]:%d %b}</span>')
    if len(days) > 1:
        parts.append(f'<span>{days[-1]:%d %b}</span>')
    parts.append("</div></div>")

    return Markup("".join(parts))


def session_chart(session, prices, width=300, height=44):
    """
    One session as a price strip: a bar per slot, highlighted where it charged.

    This is the picture that answers "why did it charge then" - the cheap
    slots are visibly the short ones, and the highlights should sit on them.
    """
    strip = session_strip(session, prices)

    if strip is None:
        return None

    top = max(max(s["price"] for s in strip if s["price"] is not None), 0.01)
    step = width / len(strip)
    bar = max(1.0, step - 0.6)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="strip" role="img"'
        f' aria-label="Price per slot, with charging slots highlighted">'
    ]
    for index, s in enumerate(strip):
        slot, price = s["slot"], s["price"]
        if price is None:
            continue
        tall = max(1.0, height * price / top)
        css = "slot charged" if s["charged"] else "slot"
        parts.append(
            f'<rect x="{index * step:.2f}" y="{height - tall:.2f}" width="{bar:.2f}"'
            f' height="{tall:.2f}" class="{css}">'
            f'<title>{slot:%H:%M} - {price:.2f} kr/kWh</title></rect>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def session_strip(session, prices):
    """
    Every slot of a session with its price and whether it charged, or None
    when none of them is priced. The data behind session_chart, which the app
    draws itself.
    """
    charged = set(session["charging_slots"])
    strip = [
        {"slot": slot, "price": prices.get(slot), "charged": slot in charged}
        for slot in _session_slots(session)
    ]
    return strip if any(s["price"] is not None for s in strip) else None


def _session_slots(session):
    """Every 15-minute slot the session spanned, plug-in to unplug."""
    slot = savings.slot_start(session["started_at"])
    end = session["ended_at"]

    slots = []
    while slot <= end and len(slots) < 4 * 24 * 3:
        slots.append(slot)
        slot += timedelta(minutes=savings.SLOT_MINUTES)
    return slots
