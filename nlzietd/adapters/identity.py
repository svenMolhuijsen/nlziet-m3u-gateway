"""NLZiet's OIDC identity server, driven headlessly.

There is exactly one way in: the OAuth device grant (RFC 8628) that the TV app
uses. It returns a refresh token, so the daemon renews itself and no password
is ever stored. `obtain()` renews an existing session; `begin()`/`complete()`
are the one-time pairing a human does from a phone or the built-in page.

The profile-scoped token is a separate grant (`grant_type=profile`): the
handshake only answers to that one, and it is minted from the user token.

Why this is hand-rolled instead of "just use a library": the calls here are
plain form POSTs whose statuses mean different things - an
`authorization_pending` device poll is not a refusal. A generic OIDC client
would hide exactly the distinction the callers care about.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..domain.errors import ApiError, LoginError
from ..domain.models import DeviceGrant, Tokens
from ..ports import ProfileTokens

CLIENT_ID = "triple-web"
# The TV app's OPENID_SCOPE is "api openid offline_access". The profile grant
# rejects offline_access (a profile token is not refreshable), and with it the
# request comes back as a bare invalid_grant - so scope is spelled out here.
PROFILE_SCOPE = "api openid"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Token endpoint statuses that mean "try again shortly", not "you are wrong".
_RETRYABLE = frozenset({403, 429, 500, 502, 503, 504})


def _tokens_from(payload: dict) -> Tokens:
    if "access_token" not in payload:
        raise LoginError(f"token response without access_token: {list(payload)}")
    return Tokens(
        access=payload["access_token"],
        id_token=payload.get("id_token", ""),
        refresh=payload.get("refresh_token"),
        # Absolute time: the value is persisted and read back later, so a
        # relative "expires_in" would be wrong the moment it is written.
        expires_at=int(time.time()) + int(payload.get("expires_in", 3600)),
    )


def _post(base: str, path: str, form: dict, bearer: str | None = None,
          timeout: int = 30) -> dict:
    """POST a form to one of the identity server's endpoints.

    Raises ApiError with the endpoint's status, because the callers care about
    different statuses: login turns everything into LoginError, the profile
    grant retries transient ones, and the device grant reads the OAuth `error`
    field out of a 400. Deciding that here would hide the distinction in the
    wrong layer.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=urllib.parse.urlencode(form).encode(),
        headers=headers,
        method="POST",
    )
    try:
        # The token endpoint has no cookies to keep, so a plain opener is
        # enough here - no jar, no redirect tricks.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        raise ApiError(err.code, path.strip("/"), err.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError) as err:
        raise ApiError(0, path.strip("/"), str(err))


def _post_token(base: str, form: dict, bearer: str | None = None,
                timeout: int = 30) -> dict:
    return _post(base, "/connect/token", form, bearer, timeout)


def _post_device(base: str, form: dict, timeout: int = 30) -> dict:
    return _post(base, "/connect/deviceauthorization", form, None, timeout)


# -- device pairing ---------------------------------------------------------
# The TV app authenticates with the OAuth device grant (RFC 8628) and asks for
# `offline_access`. That is the only place NLZiet hands out a refresh token.
# Pairing once with the TV's own client id buys a session that renews itself,
# and no password has to be stored anywhere.
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TV_CLIENT_ID = "triple-web-tv"
TV_SCOPE = "api openid offline_access"


def _oauth_error(err: ApiError) -> str:
    """The `error` field of an RFC 6749 error response, or "".

    The device grant answers 400 for every non-final state, so the status alone
    says nothing: `authorization_pending` means keep polling, `access_denied`
    means the human said no. Only the body can tell them apart.
    """
    try:
        return str(json.loads(err.body).get("error", ""))
    except (ValueError, AttributeError):
        return ""


class OidcDeviceLogin:
    """SessionProvider for a device-paired session, plus the pairing itself.

    Two jobs, one adapter: they share the client id and the endpoints, and
    splitting them would only move the coupling somewhere else. `obtain()` is
    what the daemon uses; `begin()`/`complete()` are what `python -m nlzietd
    login` uses, once per machine or whenever the pairing is revoked.

    Why a dead refresh token is an error rather than a chance to try something
    else: a provider that quietly does more than its name says is how a daemon
    ends up hammering a login endpoint at 3 a.m. The failure is explicit and
    the fix is one command: pair again.
    """

    def __init__(
        self,
        id_base: str,
        client_id: str = TV_CLIENT_ID,
        scope: str = TV_SCOPE,
        clock=time.time,
        sleep=time.sleep,
    ) -> None:
        self._id = id_base.rstrip("/")
        self._client_id = client_id
        self._scope = scope
        self._clock = clock
        self._sleep = sleep

    # -- SessionProvider ----------------------------------------------------

    def obtain(self, previous: Tokens | None) -> Tokens:
        if previous and previous.refresh:
            try:
                return _tokens_from(_post_token(self._id, {
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "refresh_token": previous.refresh,
                }))
            except (LoginError, ApiError) as err:
                raise LoginError(
                    "the paired session is no longer valid "
                    "(revoked, or expired after a long silence); pair this "
                    "machine again with `python -m nlzietd login`"
                ) from err
        raise LoginError(
            "no paired session yet; pair this machine once with "
            "`python -m nlzietd login`"
        )

    # -- pairing ------------------------------------------------------------

    def begin(self) -> DeviceGrant:
        """Start a pairing: a short code, and the page where it is entered."""
        try:
            payload = _post_device(self._id, {
                "client_id": self._client_id,
                "scope": self._scope,
            })
        except ApiError as err:
            raise LoginError(f"device authorization refused: {err}") from err

        try:
            return DeviceGrant(
                device_code=payload["device_code"],
                user_code=payload["user_code"],
                # The plain URI plus a code is the shape the TV app shows;
                # verification_uri_complete is only a convenience variant.
                verification_uri=payload.get("verification_uri")
                or payload["verification_uri_complete"],
                expires_at=int(self._clock()) + int(payload.get("expires_in", 300)),
                interval=max(1, int(payload.get("interval", 5))),
            )
        except KeyError as err:
            raise LoginError(f"device authorization response is incomplete: {err}") from err

    def complete(self, grant: DeviceGrant) -> Tokens:
        """Poll until someone enters the code.

        The cadence is the server's, not ours: `interval` is a floor, and
        `slow_down` raises it (RFC 8628 §3.5). Polling faster than asked is how
        a pairing gets rate-limited into looking like a broken code.
        """
        interval = grant.interval
        while True:
            if int(self._clock()) >= grant.expires_at:
                raise LoginError(
                    "the pairing code expired before it was entered; run "
                    "`python -m nlzietd login` for a fresh one"
                )
            try:
                payload = _post_token(self._id, {
                    "grant_type": DEVICE_GRANT,
                    "client_id": self._client_id,
                    "device_code": grant.device_code,
                })
            except ApiError as err:
                error = _oauth_error(err)
                if error == "authorization_pending":
                    self._sleep(interval)
                    continue
                if error == "slow_down":
                    interval += 5
                    self._sleep(interval)
                    continue
                if error == "access_denied":
                    raise LoginError("the pairing was denied on the page") from err
                if error == "expired_token":
                    raise LoginError(
                        "the pairing code expired; run `python -m nlzietd login` "
                        "for a fresh one"
                    ) from err
                raise LoginError(f"device code refused: {err}") from err
            return _tokens_from(payload)


class OidcProfileToken(ProfileTokens):
    """Derives the profile-scoped token from a user token.

    This is a plain `grant_type=profile` POST with the user's access token as
    the bearer. `client_id` must be `triple-web`; the TV app's own client id
    (`triple-web-tv`) is registered for the device-code grant and answers
    `invalid_grant` here.

    Why the retry loop: id.nlziet.nl sits behind Cloudflare and returns a bare
    403 with no body under even modest bursts (five profile tokens in a row is
    enough). Treating that as a rejected login would log the user out
    for no reason, so transient statuses back off and retry. Time is slept
    here rather than in a service because the knowledge "this endpoint does
    that" belongs to this adapter.
    """

    def __init__(
        self,
        id_base: str,
        attempts: int = 4,
        backoff: float = 1.5,
        timeout: int = 30,
    ) -> None:
        self._id = id_base.rstrip("/")
        self._attempts = attempts
        self._backoff = backoff
        self._timeout = timeout

    def mint(self, access_token: str, profile_id: str) -> Tokens:
        form = {
            "grant_type": "profile",
            "client_id": CLIENT_ID,
            "scope": PROFILE_SCOPE,
            "profile": profile_id,
        }
        last: ApiError | None = None
        for attempt in range(self._attempts):
            try:
                return _tokens_from(
                    _post_token(self._id, form, bearer=access_token,
                                timeout=self._timeout)
                )
            except ApiError as err:
                last = err
                if err.status not in _RETRYABLE:
                    break
                time.sleep(self._backoff * (attempt + 1))
        raise LoginError(f"profile token: {last}")

