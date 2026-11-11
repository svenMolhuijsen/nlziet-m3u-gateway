"""Relaying a stream to a player without touching the encryption.

Three routes, one idea: the player must see URLs that point at *us*, because
the CDN's URLs carry signed tokens that expire and the player's CDM needs a
licence exchange it can only start through us.

The manifest is the only thing rewritten, and only at one place: a
`<BaseURL>` element pointing at `/seg/<channel>/`. Every segment template in
NLZiet's manifests is relative (`sc-gaFEEw/$RepresentationID$_t$Time$.m4s`),
so one BaseURL moves the whole segment space through this service without
touching a single `<SegmentTemplate>`. That is deliberately the smallest
possible edit: every attribute we leave alone is an attribute the player and
the packager can still agree on.

Why segments are proxied at all, when the CDN URL is signed and could be
handed over directly: the signed token in the manifest is bound to an ACL and
an expiry, and rotates when the ticket rotates. If the player held the CDN
URL, a ticket refresh would strand it mid-playback. Proxying keeps the URL the
player knows stable and lets us swap the upstream behind it.

What this layer will not do: decrypt, remux, or inspect media. It moves bytes
and rewrites one XML element. The Widevine challenge and licence travel
through untouched, which is what keeps this on the right side of the law and
of the CDM's licence agreement.
"""

from __future__ import annotations

import re
import urllib.parse
from xml.sax.saxutils import escape

from ..domain.errors import ApiError, BadRequest
from ..domain.models import Reply, Ticket
from ..ports import Relay
from .playback import STREAM_TTL, PlaybackService

# The opening tag only: attributes vary (profiles, type, publishTime...), so
# matching the whole element would be brittle. MPD is XML, and this is the one
# place in the service that edits XML at all.
_MPD_OPEN = re.compile(r"<MPD\b[^>]*>", re.IGNORECASE)
_BASE_URL = re.compile(r"<BaseURL\b[^>]*>.*?</BaseURL>", re.IGNORECASE | re.DOTALL)

# Statuses that mean "this ticket's signed URL is no longer good", as opposed
# to a real upstream failure. Only these are worth a fresh handshake.
_STALE = frozenset({401, 403, 404, 410})

_MANIFEST_TYPE = "application/dash+xml"


def inject_base(text: str, base_url: str) -> str:
    """Point every relative segment at `base_url`.

    Inserted directly after the `<MPD ...>` open tag because that is the
    highest-precedence BaseURL in the hierarchy, so it applies to all periods,
    adaptation sets and representations at once - and it is the only level
    where a DASH client is guaranteed to look.

    If the manifest ever ships its own MPD-level BaseURL, it is replaced
    instead of duplicated: two siblings at the top level is invalid DASH, and
    players differ in which one they pick.
    """
    opening = _MPD_OPEN.search(text)
    if opening is None:
        # Not a manifest we understand. Hand it back untouched rather than
        # mangling XML we cannot reason about - the player's error is clearer
        # than ours would be.
        return text

    start = opening.end()
    # A BaseURL at this level can only appear before the first Period; look in
    # that window so a BaseURL inside a Period is never mistaken for it.
    period = text.find("<Period", start)
    limit = period if period != -1 else len(text)
    head = text[start:limit]

    replacement = f"<BaseURL>{escape(base_url)}</BaseURL>"
    if _BASE_URL.search(head):
        head = _BASE_URL.sub(replacement, head, count=1)
    else:
        head = replacement + head
    return text[:start] + head + text[limit:]


class TransportService:
    def __init__(self, playback: PlaybackService, relay: Relay) -> None:
        self._playback = playback
        self._relay = relay

    # -- manifest -----------------------------------------------------------

    def manifest(self, channel_id: str, base_url: str) -> Reply:
        """Fetch the live MPD and rewrite its BaseURL to point at us.

        `base_url` is built by the web layer from the request's Host header,
        not from configuration: the same daemon answers on localhost, a LAN
        address and a tailnet name, and the player must be sent back to the
        name *it* used or a phone on the tailnet would be told to fetch from
        an address it cannot reach.
        """
        ticket = self._playback.ticket(channel_id, ttl=STREAM_TTL)
        upstream = self._relay.get(ticket.manifest_url, dict(ticket.headers))
        if upstream.status in _STALE:
            # A signed URL that stopped working before our TTL ran out (clock
            # skew, an early rotation at the CDN). One forced handshake is the
            # fix; a second failure is real and gets relayed as-is.
            ticket = self._playback.ticket(channel_id, ttl=STREAM_TTL, force=True)
            upstream = self._relay.get(ticket.manifest_url, dict(ticket.headers))
        if upstream.status != 200:
            return upstream

        text = upstream.body.decode("utf-8", "replace")
        body = inject_base(text, base_url).encode("utf-8")
        return Reply(
            200,
            {
                "content-type": _MANIFEST_TYPE,
                # Dynamic manifest with a 2s minimumUpdatePeriod: a cache here
                # would freeze the timeline for the player.
                "cache-control": "no-store",
            },
            body,
        )

    # -- segments -----------------------------------------------------------

    def segment(self, channel_id: str, rest: str, range_header: str | None = None) -> Reply:
        """Relay one segment (or init file) to the CDN.

        `rest` is whatever came after `/seg/<channel>/`, i.e. a path relative
        to the manifest's own directory. It is joined onto the *manifest URL*,
        not onto the CDN root, so the `hdntl` query tokens the manifest carries
        stay part of the request.
        """
        ticket = self._playback.ticket(channel_id, ttl=STREAM_TTL)
        target = self._segment_url(ticket, rest)

        headers = dict(ticket.headers)
        if range_header:
            # Range requests must survive the hop: players use them to seek in
            # an already-buffered segment, and answering 200 to a Range request
            # makes some of them restart the whole file.
            headers["Range"] = range_header
        return self._relay.get(target, headers)

    @staticmethod
    def _segment_url(ticket: Ticket, rest: str) -> str:
        # Refusing anything that is not a plain relative path is what stops
        # this daemon from being an open proxy on the LAN: `//host/path` is a
        # network-path reference and `scheme:...` an absolute one, and both
        # would leave NLZiet's CDN.
        if not rest or rest.startswith("/") or "://" in rest:
            raise BadRequest("segment path must be relative")
        target = urllib.parse.urljoin(ticket.manifest_url, rest)
        if urllib.parse.urlparse(target).netloc != urllib.parse.urlparse(
            ticket.manifest_url
        ).netloc:
            raise BadRequest("segment path escapes the stream host")
        return target

    # -- licence ------------------------------------------------------------

    def licence(
        self,
        channel_id: str,
        body: bytes,
        content_type: str = "application/octet-stream",
    ) -> Reply:
        """Pass a Widevine challenge through and return the licence.

        The challenge is opaque (a protobuf the CDM produced) and the licence
        is opaque too. The `?license=` query on the URL identifies the stream
        session; the `Nlziet-License` header is sent along as well because the
        TV app's player sends both and the proxy has changed its mind about
        which one it reads before.
        """
        ticket = self._playback.ticket(channel_id, ttl=STREAM_TTL)
        if not ticket.licence_url:
            raise ApiError(200, "licence", "ticket has no licence URL")

        headers = dict(ticket.headers)
        headers["Content-Type"] = content_type
        return self._relay.post(ticket.licence_url, body, headers)
