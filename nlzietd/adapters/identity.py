"""NLZiet's OIDC identity server, driven headlessly.

Why this is hand-rolled instead of "just use a library": the flow needs the
plain-authorization-code dance with a public client and PKCE, and it is a
login *form* in the middle of it. A generic OIDC client would still have to
scrape and POST that form, so the library buys nothing and hides the one
thing that actually goes wrong (a redirect that carries the auth code).

Why every hop is followed by hand: urllib follows redirects silently, and the
authorization code lives in the Location header of one of those redirects.
Auto-following throws it away. `walk()` records each hop instead, so we can
pick the code out of whichever one carries it.

Why the form fields are literal: they are ASP.NET Identity's names
(ReturnUrl / EmailAddress / Password / __RequestVerificationToken / button).
If NLZiet redesigns the login page this breaks loudly at the POST, not
silently somewhere downstream.
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from ..domain.errors import ApiError, LoginError
from ..domain.models import DeviceGrant, Tokens
from ..ports import ProfileTokens

CLIENT_ID = "triple-web"
SCOPE = "openid api"
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn 3xx into a normal response so we can read its Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _visible_text(html: str, limit: int = 400) -> str:
    """Enough page text to explain a failure to a human, and no more."""
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()[:limit]


class OidcPasswordLogin:
    """SessionProvider backed by the identity server's login form."""

    def __init__(self, id_base: str, app_base: str, email: str, password: str) -> None:
        self._id = id_base.rstrip("/")
        self._app = app_base.rstrip("/")
        self._email = email
        self._password = password

    # -- port ---------------------------------------------------------------

    def obtain(self, previous: Tokens | None) -> Tokens:
        """Refresh if we can, otherwise log in from scratch."""
        if previous and previous.refresh:
            try:
                return self._refresh(previous.refresh)
            except LoginError:
                # A dead refresh token is normal (password change, NLZiet
                # logout, expiry). Falling through to a full login is the
                # recovery, so this is intentionally not an error path.
                pass
        return self._login()

    # -- http plumbing ------------------------------------------------------

    def _opener(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
        )

    def _fetch(self, op, url, data=None, headers=None):
        head = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            head["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            head.update(headers)
        req = urllib.request.Request(
            url, data=body, headers=head,
            method="POST" if body is not None else "GET",
        )
        try:
            with op.open(req, timeout=30) as resp:
                # Header names arrive lowercase from the CDN in front of
                # id.nlziet.nl; normalising here stops a whole class of bugs.
                return (resp.status,
                        {k.lower(): v for k, v in resp.headers.items()},
                        resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            return (err.code,
                    {k.lower(): v for k, v in err.headers.items()},
                    err.read().decode("utf-8", "replace"))

    def _walk(self, op, url, limit=8):
        """Follow redirects one at a time, remembering every hop."""
        seen = []
        status, headers, body = self._fetch(op, url)
        seen.append((url, status))
        for _ in range(limit):
            if status not in (301, 302, 303, 307, 308):
                break
            nxt = urllib.parse.urljoin(url, headers.get("location", ""))
            if not nxt:
                break
            url = nxt
            status, headers, body = self._fetch(op, url)
            seen.append((url, status))
        return url, status, headers, body, seen

    # -- the flow -----------------------------------------------------------

    def _authorize_url(self, challenge: str) -> str:
        state = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "redirect_uri": f"{self._app}/callback",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        })
        return f"{self._id}/connect/authorize?{query}"

    def _login(self) -> Tokens:
        verifier = re.sub(
            r"[^a-zA-Z0-9]+", "",
            base64.urlsafe_b64encode(os.urandom(40)).decode(),
        )
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().replace("=", "")

        op = self._opener()
        login_url, status, _, html, seen = self._walk(op, self._authorize_url(challenge))
        if status != 200:
            hops = " -> ".join(f"{u[:80]}({s})" for u, s in seen)
            raise LoginError(f"authorize chain ended at {status}: {hops}")

        token = self._form_token(html)
        if not token:
            raise LoginError("login page had no __RequestVerificationToken")

        params = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query)
        if "ReturnUrl" not in params:
            raise LoginError("login URL carried no ReturnUrl; form shape changed")

        status, headers, body = self._fetch(op, login_url, data={
            "ReturnUrl": params["ReturnUrl"][0],
            "EmailAddress": self._email,
            "Password": self._password,
            "RememberLogin": "true",
            "button": "login",
            "__RequestVerificationToken": token,
        }, headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Origin": self._id,
            "Referer": login_url,
        })
        if status == 200:
            # ASP.NET re-renders the form on bad credentials. Surface its own
            # words rather than guessing between "wrong password" and "blocked".
            raise LoginError(f"credentials rejected: {_visible_text(body)!r}")
        if status not in (301, 302, 303):
            raise LoginError(f"credential POST returned {status}")

        loc = urllib.parse.urljoin(self._id, headers.get("location", ""))
        _, _, _, _, hops = self._walk(op, loc)

        auth_code = None
        for hop_url, _ in [(loc, 0)] + hops:
            found = urllib.parse.parse_qs(urllib.parse.urlparse(hop_url).query)
            if found.get("code"):
                auth_code = found["code"][0]
                break
        if not auth_code:
            raise LoginError("no authorization code in the redirect chain")

        return self._exchange({
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": auth_code,
            "redirect_uri": f"{self._app}/callback",
            "code_verifier": verifier,
        })

    def _refresh(self, refresh_token: str) -> Tokens:
        return self._exchange({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        })

    def _exchange(self, form: dict) -> Tokens:
        try:
            return _tokens_from(_post_token(self._id, form))
        except ApiError as err:
            raise LoginError(str(err))

    @staticmethod
    def _form_token(html: str) -> str | None:
        # Attribute order is not guaranteed; try both shapes before giving up.
        for pattern in (
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
        ):
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None


# -- device pairing ---------------------------------------------------------
# The TV app authenticates with the OAuth device grant (RFC 8628) and asks for
# `offline_access`. That is the only place NLZiet hands out a refresh token: the
# web login returns none, which is exactly why a password-logged-in daemon dies
# every eight hours. Pairing once with the TV's own client id buys a session
# that renews itself, and no password has to be stored anywhere.
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

    Why not silently fall back to a password login when the refresh token dies:
    a provider that quietly does something else than its name says is how a
    daemon ends up hammering a login endpoint at 3 a.m. The failure is explicit
    and the fix is one command.
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
    enough). Treating that as "credentials rejected" would log the user out
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

