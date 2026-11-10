"""Turning a channel into a playable ticket.

Three calls, in order, and each one is a different kind of thing:

    channel id --(EPG live)--> content item --(profile token)-->
    handshake --> manifest + licence

The output is a `Ticket`: NLZiet's CDN URL, NLZiet's licence URL, and the
headers those two want. Nothing here decrypts anything - the player owns the
CDM, and the only secret in a Ticket is a licence capability that expires with
the session.

Two cache lifetimes, because two callers want opposite things:

* `TICKET_TTL` (seconds) for the debug route, which is polled by a human and
  should not allocate a stream every refresh, but should not show a stale one
  either.
* `STREAM_TTL` (tens of minutes) for the proxy. A DASH player re-fetches a
  dynamic manifest every couple of seconds; minting a handshake per poll would
  allocate a stream session per poll and hit NLZiet's simultaneous-stream
  limit (1 on Basis) in under a minute. The URL's own `hdnts` token is good for
  ~11h, so reusing one session for half an hour is well inside its life.

Why 401 is handled here: `api` and `OidcProfileToken` raise typed failures, but
only this layer knows that the right response to a 401 is "drop both tokens
and try exactly once". Retrying more than once turns an expired account into a
login storm; retrying zero times turns a rotated token into a dead player.
"""

from __future__ import annotations

import time

from ..domain.errors import ApiError
from ..domain.models import Channel, Ticket
from ..ports import Catalogue, NlzietApi
from .profile import ProfileSessions
from .session import SessionService

TICKET_TTL = 15
STREAM_TTL = 30 * 60


class PlaybackService:
    def __init__(
        self,
        api: NlzietApi,
        catalogue: Catalogue,
        session: SessionService,
        profiles: ProfileSessions,
        clock=time.time,
    ) -> None:
        self._api = api
        self._catalogue = catalogue
        self._session = session
        self._profiles = profiles
        self._clock = clock
        self._tickets: dict[str, tuple[float, Ticket]] = {}

    def channel(self, channel_id: str) -> Channel:
        channels = self._catalogue.channels()
        if channel_id not in channels:
            raise KeyError(channel_id)
        return channels[channel_id]

    def channels(self) -> dict[str, Channel]:
        """The catalogue, through the service, so callers never reach past it."""
        return self._catalogue.channels()

    def ticket(
        self,
        channel_id: str,
        ttl: int = TICKET_TTL,
        force: bool = False,
    ) -> Ticket:
        """A ticket for `channel_id`, cached for at most `ttl` seconds.

        The cache stores when a ticket was *minted*, not when it expires, so a
        short-lived caller (debug) and a long-lived one (proxy) can share one
        entry without either extending the other's life: each applies its own
        `ttl` to the same timestamp.
        """
        now = self._clock()
        entry = self._tickets.get(channel_id)
        if not force and entry and entry[0] + ttl > now:
            return entry[1]

        channel = self.channel(channel_id)
        try:
            # `force` bypasses the *ticket* cache, not the token caches: a
            # stale signed URL is fixed by a new handshake, and a handshake
            # with the tokens we already hold is exactly that.
            ticket = self._mint(channel, force_token=False)
        except ApiError as err:
            if err.status != 401:
                raise
            ticket = self._mint(channel, force_token=True)

        self._tickets[channel_id] = (now, ticket)
        return ticket

    # -- internals ----------------------------------------------------------

    def _mint(self, channel: Channel, force_token: bool) -> Ticket:
        if force_token:
            # Both layers: the handshake needs the profile token, which is
            # derived from the user token, so either being stale looks the
            # same from here.
            self._session.invalidate()
            self._profiles.invalidate()

        access = self._session.token(force=force_token)
        program = self._api.live_program(access, channel.id)
        profile = self._profiles.token(access, force=force_token)
        handshake = self._api.handshake(profile, program.content_id, channel.id)

        manifest_url = handshake.get("manifestUrl")
        if not manifest_url:
            # No manifest means no stream; this is a protocol change, not a
            # "play nothing quietly" case.
            raise ApiError(200, "handshake", "no manifestUrl in response")

        drm = handshake.get("drm") or {}
        licence_url = drm.get("licenseUrl") or ""
        headers = dict(drm.get("headers") or {})

        # The licence blob is passed as a query parameter, exactly as the TV
        # app does it. It is base64 (no '+', so no double-encoding trap) and
        # it authorises one licence request for this stream session only - it
        # is not the bearer token and must not be confused with one.
        blob = headers.get("Nlziet-License")
        if licence_url and blob:
            licence_url = f"{licence_url}?license={blob}"

        return Ticket(
            manifest_url=str(manifest_url),
            licence_url=licence_url,
            headers=headers,
            content_id=program.content_id,
            channel=channel.id,
            session_id=str(handshake.get("streamSessionId") or ""),
            title=program.title,
        )
