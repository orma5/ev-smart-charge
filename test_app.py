"""
Tests for the scheduler loop.

Only one property really matters here, and it is the one the move from a
CronJob to a long-running process put at risk: a tick that raises must not end
the loop. Under cron, a crashed run was retried by the next Job; here nothing
else is watching.
"""
import pytest

import app


def test_a_failed_tick_does_not_stop_the_loop(monkeypatch, capsys):
    """
    Three ticks: the first raises, the loop must still reach the third. If this
    regresses, smart charging stops at the first DNS blip and stays stopped
    until somebody notices the car did not charge overnight.
    """
    ticks = []

    def flaky(config):
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise RuntimeError("could not reach the Skoda API")
        if len(ticks) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(app, "tick", flaky)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)

    with pytest.raises(KeyboardInterrupt):
        app.scheduler({})

    assert len(ticks) == 3

    # print_exc goes to stderr, the "Run failed" line to stdout; the failure has
    # to be legible in `kubectl logs` either way.
    printed = capsys.readouterr()
    assert "Run failed" in printed.out
    assert "could not reach the Skoda API" in printed.err


def test_the_interval_is_measured_from_the_start_of_the_tick(monkeypatch):
    """
    A slow tick must not push the next one later. Sleeping a flat interval
    after a 60-second run would make it a 300-second cycle, which is 12 polls
    an hour rather than 15 - quietly under the rate budget it was chosen to fit.
    """
    slept = []
    # start of tick, the last_tick stamp, then the elapsed measurement.
    clock = iter([0.0, 60.0, 60.0])

    monkeypatch.setattr(app, "tick", lambda config: None)
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: slept.append(seconds) or (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        app.scheduler({})

    assert slept == [app.POLL_SECONDS - 60.0]


# --- /livez -----------------------------------------------------------------
# The property the CronJob got from activeDeadlineSeconds: a wedged run was
# killed and showed as a failed Job. A wedged thread in a long-running pod is
# invisible instead - the pod serves pages fine while the car never charges.

def livez(monkeypatch, last_tick, now=1000.0):
    monkeypatch.setattr(app, "last_tick", last_tick)
    monkeypatch.setattr(app.time, "monotonic", lambda: now)
    monkeypatch.setattr(app.db, "connect", lambda config: None)

    return app.flask_app({"PRICE_ZONE": "SE3"}).test_client().get("/livez")


def test_livez_is_ok_while_ticks_are_landing(monkeypatch):
    assert livez(monkeypatch, last_tick=1000.0 - app.POLL_SECONDS).status_code == 200


def test_livez_tolerates_a_single_missed_tick(monkeypatch):
    """One slow run or a retried connection must not restart the pod."""
    assert livez(monkeypatch, last_tick=1000.0 - app.POLL_SECONDS * 2).status_code == 200


def test_livez_fails_once_the_scheduler_has_clearly_stopped(monkeypatch):
    response = livez(monkeypatch, last_tick=1000.0 - app.STALE_AFTER_SECONDS - 1)

    assert response.status_code == 503
    assert b"no completed run" in response.data


def test_livez_is_ok_before_the_first_tick(monkeypatch):
    """Boot: initialDelaySeconds covers the first run, not a 503 loop."""
    assert livez(monkeypatch, last_tick=None).status_code == 200
