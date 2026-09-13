"""
Tests for the Zaptec client. No network.

The charger is now the thing that decides whether a tick charges at all, so a
misread here does not produce a wrong number on a dashboard - it produces a car
that sat at home all night without charging, which nothing reports.
"""
import pytest

import zaptec

CONFIG = {
    "ZAPTEC_API_BASE": "https://zaptec.example",
    "ZAPTEC_USERNAME": "user",
    "ZAPTEC_PASSWORD": "secret",
    "ZAPTEC_CHARGER_ID": "charger-uuid",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def observations(**by_id):
    """
    The flat list the state endpoint returns.

    PascalCase because that is what the live API sent on 2026-09-13, not what
    its published schema says. Fixtures built to the schema passed happily
    against a shape the API never sends - the same trap read_charging fell into
    with Skoda's top-level `vehicle` wrapper.
    """
    return [
        {"StateId": int(state_id), "ValueAsString": value}
        for state_id, value in by_id.items()
    ]


def client(monkeypatch, state_response, token_calls=None):
    """A client whose token request always succeeds and whose state is fixed."""
    token_calls = [] if token_calls is None else token_calls

    def fake_post(url, **kwargs):
        token_calls.append(kwargs.get("data"))
        return FakeResponse(payload={"access_token": "t", "expires_in": 3600})

    monkeypatch.setattr(zaptec.requests, "post", fake_post)
    monkeypatch.setattr(zaptec.requests, "get", lambda url, **kwargs: state_response)
    return zaptec.Zaptec(CONFIG), token_calls


# --- Reading the charger ----------------------------------------------------

def test_state_reads_the_mode_and_the_meter(monkeypatch):
    charger, _ = client(monkeypatch, FakeResponse(
        payload=observations(**{"710": "3", "553": "12.5"})
    ))

    assert charger.state() == (3, 12.5)


def test_the_documented_spelling_is_read_too(monkeypatch):
    """
    The live API sends StateId; its own published schema says stateId. Neither
    spelling may be the one that breaks, because there is no telling which one
    Zaptec will decide is correct.
    """
    charger, _ = client(monkeypatch, FakeResponse(
        payload=[{"stateId": 710, "valueAsString": "3"}]
    ))

    assert charger.state() == (3, None)


def test_a_charger_with_no_session_reports_no_energy(monkeypatch):
    """
    The meter is optional and the mode is not. A charger sitting idle still has
    to answer the connectedness question, so a missing 553 cannot be an error.
    """
    charger, _ = client(monkeypatch, FakeResponse(payload=observations(**{"710": "1"})))

    assert charger.state() == (1, None)


def test_a_missing_operating_mode_is_an_error(monkeypatch):
    """
    Not silently treated as at-home. The fail-open decision belongs to the
    caller, which records that it happened; swallowing it here would hide a
    charger that had stopped reporting entirely.
    """
    charger, _ = client(monkeypatch, FakeResponse(payload=observations(**{"553": "1.0"})))

    with pytest.raises(zaptec.ZaptecError):
        charger.state()


def test_an_unparseable_mode_is_an_error(monkeypatch):
    charger, _ = client(monkeypatch, FakeResponse(
        payload=observations(**{"710": "not a number"})
    ))

    with pytest.raises(zaptec.ZaptecError):
        charger.state()


def test_an_unparseable_meter_reading_is_tolerated(monkeypatch):
    """The mode is what the decision needs; the meter only affects a number."""
    charger, _ = client(monkeypatch, FakeResponse(
        payload=observations(**{"710": "3", "553": ""})
    ))

    assert charger.state() == (3, None)


def test_a_failed_state_request_raises(monkeypatch):
    charger, _ = client(monkeypatch, FakeResponse(status_code=503, text="down"))

    with pytest.raises(zaptec.ZaptecError):
        charger.state()


def test_an_unreachable_charger_raises_zaptec_error(monkeypatch):
    """requests' own exception must not escape into the scheduler."""
    def unreachable(url, **kwargs):
        raise zaptec.requests.ConnectionError("no route to host")

    monkeypatch.setattr(zaptec.requests, "post",
                        lambda url, **kwargs: FakeResponse(
                            payload={"access_token": "t", "expires_in": 3600}))
    monkeypatch.setattr(zaptec.requests, "get", unreachable)

    with pytest.raises(zaptec.ZaptecError):
        zaptec.Zaptec(CONFIG).state()


# --- The token --------------------------------------------------------------

def test_the_token_is_reused_across_ticks(monkeypatch):
    """
    A scheduler that authenticated on every tick would log in 360 times a day
    for no reason, and Zaptec's guidance is explicit about not hammering it.
    """
    charger, tokens = client(monkeypatch, FakeResponse(
        payload=observations(**{"710": "3"})
    ))

    charger.state()
    charger.state()
    charger.state()

    assert len(tokens) == 1
    assert tokens[0]["grant_type"] == "password"


def test_a_nearly_expired_token_is_renewed(monkeypatch):
    """
    Renewed before it actually expires, so a tick that starts just inside the
    window does not fail on a token that dies mid-request.
    """
    tokens = []

    def fake_post(url, **kwargs):
        tokens.append(kwargs.get("data"))
        return FakeResponse(payload={
            "access_token": "t",
            "expires_in": zaptec.TOKEN_MARGIN_SECONDS / 2,
        })

    monkeypatch.setattr(zaptec.requests, "post", fake_post)
    monkeypatch.setattr(zaptec.requests, "get",
                        lambda url, **kwargs: FakeResponse(
                            payload=observations(**{"710": "3"})))

    charger = zaptec.Zaptec(CONFIG)
    charger.state()
    charger.state()

    assert len(tokens) == 2


def test_a_rejected_login_raises(monkeypatch):
    monkeypatch.setattr(zaptec.requests, "post",
                        lambda url, **kwargs: FakeResponse(status_code=400))

    with pytest.raises(zaptec.ZaptecError):
        zaptec.Zaptec(CONFIG).state()


# --- car_connected ----------------------------------------------------------

def test_only_an_explicit_disconnected_means_no_car():
    assert not zaptec.car_connected(zaptec.DISCONNECTED)

    for mode in (0, 2, 3, 5):
        assert zaptec.car_connected(mode)


def test_an_unrecognised_mode_reads_as_connected():
    """
    Zaptec may add values. Tested by exclusion for the same reason
    main.CABLE_DISCONNECTED is: a value we do not know must not be able to stop
    a charge.
    """
    assert zaptec.car_connected(99)
