"""api.nlziet.nl client.

The three calls the gateway needs, and no more. Keeping the adapter this
narrow means the day NLZiet renames an endpoint, the diff is three methods
instead of a scattering of URL strings.

Everything is Bearer-authenticated, but *which* token differs and that is the
one thing worth knowing here:

* `/v7/profile` and `/v8/epg/...` accept the user token from login.
* `/v9/stream/handshake` only accepts the profile-scoped token minted from
  that user token (`ProfileTokens`). Passing the user token there returns a
  403-ish failure that looks like an entitlement problem and is not.

Endpoint versions come from the TV app's own `ENDPOINT_VERSIONS` map, so they
are what the vendor's client uses today, not what the docs happened to say.
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

from ..domain.errors import ApiError
from ..domain.models import Program
from ..ports import NlzietApi

# NLZiet's own web player identifies itself this way; the handshake returns a
# manifest shaped for it (and the licence server accepts the matching headers).
PLAYER_NAME = "BitmovinWeb"

# The TV app sends these on every call. `Nlziet-AppName` is load-bearing: the
# API uses it to decide which client contract it is talking to. The rest is
# honest mimicry - without a plausible User-Agent the CDN in front of the API
# answers with a Cloudflare block page instead of JSON.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _pick_live(locations: list[dict], now: datetime.datetime) -> dict | None:
    """The program that is on now, out of the list the endpoint returns.

    The endpoint is called "live" and normally puts the current program first,
    but "first" is an ordering assumption the API never promised. Filtering on
    `endAt` is cheap and makes the last seconds of a broadcast - when the list
    can still contain yesterday's entry - behave.
    """
    live = []
    for row in locations:
        try:
            ends = datetime.datetime.fromisoformat(row["endAt"])
        except (KeyError, TypeError, ValueError):
            continue  # unparsable row: fall back to position below
        if ends > now:
            live.append((row.get("startAt") or "", row))
    if live:
        # Latest start wins: with a trailing old entry present, this is the
        # one that is actually on air.
        return max(live, key=lambda pair: pair[0])[1]
    return locations[0] if locations else None


class HttpNlzietApi(NlzietApi):
    def __init__(self, base: str, app_base: str, timeout: int = 20) -> None:
        self._base = base.rstrip("/")
        self._app = app_base.rstrip("/")
        self._timeout = timeout

    # -- internals ----------------------------------------------------------

    def _get(self, path: str, token: str) -> dict | list:
        req = urllib.request.Request(
            f"{self._base}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # The API is same-origin for the web app; pretending to be it
                # keeps the request inside the behaviour NLZiet expects.
                "Origin": self._app,
                "Referer": f"{self._app}/",
                "User-Agent": _UA,
                "Nlziet-AppName": "SmartTv",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            # 401 is not a generic failure: it means "get a new token", and
            # the service layer acts on that distinction. Keep the status.
            raise ApiError(err.code, path, body)
        except (urllib.error.URLError, TimeoutError) as err:
            # No status to report: the service turns this into a 503, but it
            # must not look like an auth failure or we would re-login on a
            # flaky wifi hiccup.
            raise ApiError(0, path, str(err))

    # -- port ---------------------------------------------------------------

    def profiles(self, access_token: str) -> list[dict]:
        data = self._get("/v7/profile", access_token)
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict) and row.get("id")]
        # Defensive: a wrapper object would still be findable, rather than
        # silently yielding "this account has no profiles".
        if isinstance(data, dict):
            for key in ("profiles", "data", "items"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [r for r in rows if isinstance(r, dict) and r.get("id")]
        raise ApiError(200, "/v7/profile", "no profile list in response")

    def live_program(self, access_token: str, channel_id: str) -> Program:
        query = urllib.parse.urlencode({"channel": channel_id})
        data = self._get(f"/v8/epg/programlocations/live?{query}", access_token)
        rows = data.get("data") if isinstance(data, dict) else None
        if not rows:
            raise ApiError(200, "epg/live", f"no live data for {channel_id}")

        entry = rows[0]
        chosen = _pick_live(
            entry.get("programLocations") or [],
            datetime.datetime.now(datetime.timezone.utc),
        )
        if not chosen:
            raise ApiError(200, "epg/live", f"no program on air for {channel_id}")

        return Program(
            content_id=str(chosen.get("contentItemId") or ""),
            asset_id=str(chosen.get("assetId") or ""),
            title=str(chosen.get("title") or ""),
            channel=str((entry.get("channel") or {}).get("id") or channel_id),
            start=str(chosen.get("startAt") or ""),
            end=str(chosen.get("endAt") or ""),
        )

    def handshake(self, profile_token: str, content_id: str, channel: str) -> dict:
        """Allocate a live stream.

        `offsetType=Live` is what the TV sends when the viewer picks a channel
        (StreamOffsetType.LIVE). Without it the session comes back as
        `videoType: Restart`, i.e. "start where the program started", which is
        the wrong answer for live TV even though the manifest URL looks
        identical.
        """
        query = urllib.parse.urlencode({
            "id": content_id,
            "context": "Live",
            "channel": channel,
            "playerName": PLAYER_NAME,
            "drmType": "Widevine",
            "sourceType": "DASH",
            "offsetType": "Live",
            "preferLowLatency": "false",
        })
        data = self._get(f"/v9/stream/handshake?{query}", profile_token)
        if not isinstance(data, dict):
            raise ApiError(200, "handshake", "unexpected payload")
        return data
