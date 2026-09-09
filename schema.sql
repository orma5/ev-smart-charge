-- Applied at every boot. Every statement here is idempotent, so this file is
-- both the migration for an empty database and a no-op against a live one.
-- There is no migration tool: four tables that change once a year do not earn
-- Alembic, and `schema_version` below is enough to hang a future ALTER off.
--
-- Timestamps are `timestamp without time zone` on purpose. The application
-- works in naive Europe/Stockholm datetimes throughout, because that is the
-- clock the price slots are published against; storing UTC here would mean
-- converting on every read and write for no benefit. The known cost is the
-- autumn DST hour, where 02:00-03:00 occurs twice and two different slots can
-- land on the same timestamp - the same ambiguity the script has always had.

CREATE TABLE IF NOT EXISTS schema_version (
    version    integer PRIMARY KEY,
    applied_at timestamp NOT NULL DEFAULT now()
);

INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;


-- Exactly one row, enforced by the primary key being a boolean that CHECK
-- pins to true: a second INSERT can only ever conflict.
--
-- The defaults are the values the CronJob carried as environment variables
-- until they became user-editable. Seeding them here rather than reading them
-- from the ConfigMap means there is no second copy that can silently disagree
-- with what the UI shows - after first boot, this table is the only source.
CREATE TABLE IF NOT EXISTS settings (
    id                     boolean PRIMARY KEY DEFAULT true CHECK (id),
    departure_hour         integer NOT NULL DEFAULT 7,
    charger_speed_kw       numeric NOT NULL DEFAULT 11.0,
    battery_capacity_kwh   numeric NOT NULL DEFAULT 82,
    -- Fallback only. The car reports its own targetStateOfChargeInPercent and
    -- that wins whenever present, so this is used only if it stops reporting.
    charge_limit_percent   integer NOT NULL DEFAULT 80,
    -- Replaces input_boolean.smart_charging in Home Assistant, which is why
    -- this application no longer talks to Home Assistant at all.
    smart_charging_enabled boolean NOT NULL DEFAULT true,
    updated_at             timestamp NOT NULL DEFAULT now()
);

INSERT INTO settings (id) VALUES (true) ON CONFLICT DO NOTHING;


-- One row per scheduler tick, including the ticks that failed or decided to do
-- nothing. This is the append-only log the history screen reads and the
-- savings maths reconstructs sessions from.
--
-- Note there is deliberately no `sessions` table. A plug-in-to-unplug session
-- is entirely derivable from this log in order, so deriving it at read time
-- keeps one source of truth and makes the reconstruction a pure function that
-- can be corrected and re-run over all of history. A maintained table would
-- need a state machine at write time and could then disagree with the log it
-- came from.
CREATE TABLE IF NOT EXISTS runs (
    id              bigserial PRIMARY KEY,
    at              timestamp NOT NULL,
    -- When the CAR last reported to Skoda's cloud, not when we polled. Stored
    -- because it dates a plug-in far more accurately than `at` can: the car's
    -- own push delay plus the poll interval put `at` up to ~8 minutes late,
    -- and the savings baseline is measured from the moment the cable went in.
    car_captured_at timestamp,
    -- Null when the tick never got a reading at all - Skoda unreachable, or
    -- the hourly quota exhausted. Those ticks are still recorded: a gap in the
    -- log is a fact the history screen and the savings maths both need, and it
    -- used to be visible only as a failed Job in kubectl.
    charging_state  text,
    battery_percent integer,
    target_percent  integer,
    slot_start      timestamp NOT NULL,
    decision        text NOT NULL,
    quota_remaining integer,
    error           text
);

CREATE INDEX IF NOT EXISTS runs_at_idx ON runs (at);


-- Every price slot we have ever fetched, kept forever.
--
-- This is not just a cache to save requests. The upstream API only serves a
-- rolling recent window, so without storing these, last month's savings become
-- uncomputable the moment that window moves past them.
CREATE TABLE IF NOT EXISTS prices (
    zone       text NOT NULL,
    slot_start timestamp NOT NULL,
    price      numeric NOT NULL,
    PRIMARY KEY (zone, slot_start)
);
