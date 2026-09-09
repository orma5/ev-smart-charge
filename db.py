"""
Postgres persistence: the settings the UI edits, the log of every scheduler
tick, and the price slots the savings maths is computed from.

Connections are opened per unit of work rather than held open. This process has
two writers - the scheduler thread and Flask request handlers - and a
long-lived connection shared between them would need locking, plus reconnect
handling for every time the NUC's Postgres restarts. Connecting per tick and
per request is a few milliseconds over the LAN and removes both problems.

Nothing in here calls the Skoda API, and nothing that serves a web request is
allowed to. The 20-requests-per-hour budget belongs entirely to the scheduler;
a browser refresh must not be able to spend it.
"""
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(config):
    """Open a connection. Caller is responsible for closing it - use `with`."""
    return psycopg.connect(
        host=config["DATABASE_HOST"],
        port=config["DATABASE_PORT"],
        dbname=config["DATABASE_NAME"],
        user=config["DATABASE_USER"],
        password=config["DATABASE_PASSWORD"],
        row_factory=dict_row,
        # Without this a network blip leaves the scheduler thread blocked in
        # recv() indefinitely, which would stop charging decisions with no
        # error anywhere - the exact silent-hang failure the CronJob's
        # activeDeadlineSeconds was introduced to make impossible.
        connect_timeout=10,
    )


def init_schema(conn):
    """Apply schema.sql. Idempotent, so this runs at every boot."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA.read_text())
    conn.commit()


# --- Settings ---------------------------------------------------------------

def load_settings(conn):
    """
    The single settings row, with numerics as floats.

    psycopg returns `numeric` as Decimal, which is right for money and wrong
    for arithmetic that already lives in floats (kWh, kW, hours). Converting
    here keeps the decision logic taking plain numbers, as its tests do.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM settings WHERE id")
        row = cur.fetchone()

    return {
        "departure_hour": row["departure_hour"],
        "charger_speed_kw": float(row["charger_speed_kw"]),
        "battery_capacity_kwh": float(row["battery_capacity_kwh"]),
        "charge_limit_percent": row["charge_limit_percent"],
        "smart_charging_enabled": row["smart_charging_enabled"],
    }



def save_settings(conn, settings):
    """
    Replace the settings row. Validation belongs to the caller - see
    web.parse_settings, which has to report problems back to a form anyway.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE settings SET
                departure_hour         = %(departure_hour)s,
                charger_speed_kw       = %(charger_speed_kw)s,
                battery_capacity_kwh   = %(battery_capacity_kwh)s,
                charge_limit_percent   = %(charge_limit_percent)s,
                smart_charging_enabled = %(smart_charging_enabled)s,
                updated_at             = now()
            WHERE id
            """,
            settings,
        )
    conn.commit()

# --- Runs -------------------------------------------------------------------

def record_run(conn, run):
    """Append one tick to the log."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runs (at, car_captured_at, charging_state,
                              battery_percent, target_percent, slot_start,
                              decision, quota_remaining, error)
            VALUES (%(at)s, %(car_captured_at)s, %(charging_state)s,
                    %(battery_percent)s, %(target_percent)s, %(slot_start)s,
                    %(decision)s, %(quota_remaining)s, %(error)s)
            """,
            run,
        )
    conn.commit()


# --- Prices -----------------------------------------------------------------

def store_prices(conn, zone, slots):
    """
    Upsert a day of slots.

    ON CONFLICT DO UPDATE rather than DO NOTHING: a day fetched before it was
    fully published would otherwise pin whatever partial values arrived first.
    """
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO prices (zone, slot_start, price)
            VALUES (%s, %s, %s)
            ON CONFLICT (zone, slot_start) DO UPDATE SET price = EXCLUDED.price
            """,
            [(zone, slot["time_start"], slot["price"]) for slot in slots],
        )
    conn.commit()


def count_prices_for_day(conn, zone, date):
    """How many slots are already stored for a calendar day."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM prices WHERE zone = %s AND slot_start::date = %s",
            (zone, date),
        )
        return cur.fetchone()["n"]


def load_prices(conn, zone, since):
    """Stored slots from `since` onwards, in the shape the decision logic uses."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slot_start, price FROM prices"
            " WHERE zone = %s AND slot_start >= %s ORDER BY slot_start",
            (zone, since),
        )
        return [
            {"time_start": row["slot_start"], "price": float(row["price"])}
            for row in cur.fetchall()
        ]


# --- Reading the log back ---------------------------------------------------

def load_runs(conn, since):
    """
    Every tick from `since` onwards, oldest first.

    Ordering is not incidental: savings.reconstruct_sessions walks this in
    sequence to find where each plug-in-to-unplug session begins and ends.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE at >= %s ORDER BY at", (since,))
        return cur.fetchall()


def latest_run(conn):
    """The most recent tick, or None if the scheduler has not run yet."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 1")
        return cur.fetchone()
