"""
The deployed process: a scheduler thread deciding whether to charge, and a web
server showing what it decided.

One process, one replica. That is not incidental - two of these would each poll
on their own timer against a budget of 20 Skoda requests an hour, and could
issue contradictory start and stop commands within the same 15-minute price
slot. The CronJob this replaces got single-flight behaviour for free from
concurrencyPolicy: Forbid; a Deployment has to be told (replicas: 1, and
strategy: Recreate so a rollout never briefly runs two).
"""
import threading
import time
import traceback
from datetime import datetime

import db
import main
import web
from waitress import serve

# Four minutes, and this number is Skoda's rate limit rather than a preference:
# 20 requests an hour shared across reads and commands, so 15 polls an hour
# leaves 5 for start/stop. Do not lower it without taking those requests from
# somewhere else.
#
# It is not about price resolution - prices move in 15-minute slots - but about
# noticing the cable going in, because the car draws power the moment it is
# connected and this is the only thing that can veto it. Even so, the interval
# is the smaller half of that delay: the API serves a snapshot the car pushed on
# its own schedule, which measurement put at ~3-4 minutes after a state change.
POLL_SECONDS = 240

PORT = 8000
WEB_THREADS = 4

# How long without a completed tick before the scheduler is presumed wedged
# and the pod should be restarted. Three missed ticks, so a single slow run
# or a retried connection does not trip it.
STALE_AFTER_SECONDS = 3 * POLL_SECONDS

# Written by the scheduler thread, read by the liveness probe. A lone
# assignment of a datetime needs no lock: it is atomic, and the reader only
# ever wants the most recent value.
last_tick = None


def tick(config):
    """One decision, with its own connection."""
    with db.connect(config) as conn:
        main.run_once(conn, config)


def scheduler(config):
    """
    Decide every POLL_SECONDS, forever.

    Every exception is swallowed and logged rather than allowed out. Under the
    CronJob a failed run exited non-zero and the next Job started four minutes
    later regardless; here the loop *is* the retry, so letting an exception
    escape would end smart charging silently until somebody noticed the car had
    not charged overnight. The failure is still recorded - run_once writes its
    row before re-raising - so it shows up on the history screen.
    """
    global last_tick

    while True:
        started = time.monotonic()
        try:
            tick(config)
        except Exception:
            print(f"Run failed at {datetime.now():%Y-%m-%d %H:%M:%S}:")
            traceback.print_exc()

        # Set whether or not the tick succeeded. A failed run still proves the
        # loop is turning, which is all this timestamp claims. Restarting the
        # pod would not fix an unreachable Skoda API, and every boot spends
        # quota on an immediate read.
        last_tick = time.monotonic()

        # Measured from the start of the tick, so a slow run does not push the
        # schedule later and later and quietly drop below 15 polls an hour.
        time.sleep(max(0, POLL_SECONDS - (time.monotonic() - started)))


def run():
    config = main.load_config()

    # Before the thread starts, and deliberately not guarded: an unreachable
    # database or a broken schema should stop the pod at boot, where it is
    # visible, rather than let it serve empty pages over a dead backend.
    with db.connect(config) as conn:
        db.init_schema(conn)

    threading.Thread(target=scheduler, args=(config,), daemon=True).start()

    print(f"Serving on port {PORT}, deciding every {POLL_SECONDS}s")
    serve(flask_app(config), host="0.0.0.0", port=PORT, threads=WEB_THREADS)


def flask_app(config):
    """
    The web app, plus the one route that belongs to the scheduler rather than
    to the UI.

    /livez lives here so web.py stays ignorant of the scheduler. It restores
    what the CronJob got from activeDeadlineSeconds: a wedged run used to be
    killed and show up as a failed Job, whereas a wedged thread inside a
    long-running pod is invisible - the pod still serves pages perfectly while
    the car never charges. /healthz cannot cover this, because it deliberately
    answers "is this pod serving", which stays true.
    """
    app = web.create_app(config)

    @app.route("/livez")
    def livez():
        if last_tick is None:
            # Boot. The probe's initialDelaySeconds covers the first tick.
            return "starting", 200

        idle = time.monotonic() - last_tick
        if idle > STALE_AFTER_SECONDS:
            return f"no completed run in {int(idle)}s", 503
        return "ok", 200

    return app


if __name__ == "__main__":
    run()
