"""
Minimal client for the Zaptec cloud API: is a car plugged into *our* charger,
and how much energy has this session delivered.

This exists to answer a question the Skoda API answers badly. Skoda can say the
cable is in, but not which cable - and it says so from a snapshot the car
pushed on its own schedule, measured at up to 37 minutes old while parked. The
charger is mains-powered and permanently online, so its answer is current, and
it is by definition about the charger at home. One value settles both "is it
plugged in" and "is it plugged in *here*".

The two APIs have opposite shapes and it is worth keeping that straight:

    Skoda    20 requests per hour, shared across reads and commands, no burst.
             Every request is rationed. Owns battery state and start/stop.
    Zaptec   10 requests per second per account. Effectively unrationed at our
             cadence. Owns connectedness, location and metered energy.

So nothing here needs the quota bookkeeping that main.Skoda carries. What it
does need is to fail soft: an unreachable Zaptec must not stop the car
charging, because the charger being unreachable says nothing about whether the
car is on it. Callers get None and carry on as though the car were at home.
"""
import time

import requests

ZAPTEC_TIMEOUT = 10

# Observation ids on the charger state endpoint. Zaptec returns state as a flat
# list of {stateId, valueAsString} rather than a named object, so these numbers
# are the field names.
OPERATION_MODE = 710
SESSION_ENERGY = 553

# ChargerOperatingMode, from the API's own enum:
#   0 Unknown  1 Disconnected  2 Connected_Requesting
#   3 Connected_Charging       5 Connected_Finished
#
# Tested by exclusion, the same way main.CABLE_DISCONNECTED is: only an
# explicit Disconnected means no car. Unknown (0) reads as connected on
# purpose - it is the charger admitting it does not know, and treating that as
# "car is elsewhere" would stop a charge on the strength of a shrug.
DISCONNECTED = 1
CHARGING = 3

# Refresh this long before the token actually expires, so a tick that starts
# just inside the window does not fail on a token that dies mid-request.
TOKEN_MARGIN_SECONDS = 60


class ZaptecError(Exception):
    """The charger's state could not be read."""


class Zaptec:
    """
    Reads one charger. The token is cached on the instance and reused until it
    is nearly expired, so a long-lived scheduler authenticates a few times a
    day rather than on every tick.
    """

    def __init__(self, config):
        self.base = config["ZAPTEC_API_BASE"].rstrip("/")
        self.username = config["ZAPTEC_USERNAME"]
        self.password = config["ZAPTEC_PASSWORD"]
        self.charger_id = config["ZAPTEC_CHARGER_ID"]
        self._token = None
        self._expires_at = 0.0

    def _authenticate(self):
        """OAuth2 password grant. Zaptec offers no other flow for a script."""
        try:
            response = requests.post(
                f"{self.base}/oauth/token",
                data={
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                },
                timeout=ZAPTEC_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ZaptecError(f"could not reach Zaptec to authenticate: {exc}") from exc

        if response.status_code != 200:
            raise ZaptecError(f"Zaptec rejected the login ({response.status_code})")

        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = time.monotonic() + payload.get("expires_in", 3600)

    def _headers(self):
        if self._token is None or time.monotonic() > self._expires_at - TOKEN_MARGIN_SECONDS:
            self._authenticate()
        return {"Authorization": f"Bearer {self._token}", "accept": "application/json"}

    def state(self):
        """
        Read the charger. Returns (mode, session_energy_kwh).

        `mode` is the ChargerOperatingMode integer; `session_energy_kwh` is the
        running total for the session in progress, or None when the charger
        does not report one (it has no session, or the firmware omits it).

        One request covers both: the state endpoint returns every observation
        the charger publishes, so asking for connectedness gets the meter for
        free.
        """
        try:
            response = requests.get(
                f"{self.base}/api/chargers/{self.charger_id}/state",
                headers=self._headers(),
                timeout=ZAPTEC_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ZaptecError(f"could not reach Zaptec: {exc}") from exc

        if response.status_code != 200:
            raise ZaptecError(
                f"Zaptec state returned {response.status_code}: {response.text[:200]}"
            )

        observations = _by_state_id(response.json())

        mode = _as_int(observations.get(OPERATION_MODE))
        if mode is None:
            raise ZaptecError("no ChargerOperationMode in the charger state")

        return mode, _as_float(observations.get(SESSION_ENERGY))


def _by_state_id(payload):
    """
    Flatten the state list into {StateId: ValueAsString}.

    Read case-insensitively because the API and its own specification disagree:
    the published schema names these `stateId` and `valueAsString`, and the
    live endpoint returns `StateId` and `ValueAsString`. Building to the spec
    alone produced a client that found no observations at all and raised on
    every tick. Since there is no telling which of the two Zaptec considers
    correct, accept either rather than pin the one that happens to be live.
    """
    observations = {}
    for observation in payload or []:
        fields = {key.lower(): value for key, value in (observation or {}).items()}
        observations[fields.get("stateid")] = fields.get("valueasstring")
    return observations


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def car_connected(mode):
    """
    True unless the charger explicitly reports no car.

    Note this is a narrower claim than Skoda's equivalent: it is false when the
    car is plugged in somewhere else, which is the entire point of consulting
    the charger rather than the car.
    """
    return mode != DISCONNECTED
