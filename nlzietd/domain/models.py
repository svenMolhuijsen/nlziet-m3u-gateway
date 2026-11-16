"""The values that travel between layers.

Everything here is frozen. Tokens and playback tickets are passed around and
cached; mutable ones are how a proxy ends up with one request quietly editing
the state another request is about to use.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tokens:
    """The OIDC result, in the shape the rest of the code cares about.

    expires_at is an absolute epoch second, not "expires_in". Relative values
    are only correct at the moment they are received, and we persist these.
    """

    access: str
    id_token: str
    refresh: str | None
    expires_at: int

    def fresh(self, now: int, skew: int) -> bool:
        # skew: treat a token as dead slightly before it really is.
        return self.expires_at - skew > now


@dataclass(frozen=True)
class DeviceGrant:
    """A pairing window: what the user types, where, and how long it lasts.

    This is the RFC 8628 device grant as data. It exists so the adapter that
    polls and the CLI that prints do not have to agree on a tuple: `expires_at`
    is absolute for the same reason Tokens.expires_at is, and the code is only
    ever shown to a human, never logged.
    """

    device_code: str
    user_code: str
    verification_uri: str
    expires_at: int
    interval: int


@dataclass(frozen=True)
class Channel:
    """One linear channel, as metadata only - nothing here can play it."""

    id: str
    asset_id: str
    name: str
    number: int
    logo: str
    replay: bool


@dataclass(frozen=True)
class Program:
    """One EPG entry: what is on a channel right now.

    `content_id` is what the handshake wants; `asset_id` is the broadcaster's
    id for the same broadcast and shows up again inside the handshake response.
    Keeping both means the rest of the code never has to re-derive one from the
    other by guessing.

    `start`/`end` stay ISO strings on purpose: they are informational (they
    end up in the playlist and the debug JSON), never compared or sorted here.
    Parsing them would only add a timezone question nobody asks.
    """

    content_id: str
    asset_id: str
    title: str
    channel: str
    start: str
    end: str


@dataclass(frozen=True)
class EpgEntry:
    """One programme in the guide, as XMLTV needs it.

    Distinct from `Program` on purpose: `Program` is "what to hand the
    handshake right now" (content/asset ids matter, times are informational);
    an `EpgEntry` is "what to draw in a guide" (times are load-bearing and get
    reformatted, ids do not matter). Keeping them apart means the EPG builder
    never carries a content id it would never use, and `live_program` never
    grows guide fields.

    `start`/`end` stay the ISO strings the API returns (with offset, e.g.
    `2026-11-16T00:51:58+01:00`); the XMLTV layer reformats them. `subtitle`
    and `description` are often empty - a channel with sparse metadata is
    normal, not an error.
    """

    channel: str
    title: str
    subtitle: str
    description: str
    start: str
    end: str
    is_movie: bool = False


@dataclass(frozen=True)
class Ticket:
    """What a player needs to start one stream, and nothing more.

    Deliberately not "the video": manifest_url points at NLZiet's CDN, the
    licence at their DRM proxy, and headers are whatever those two require.
    The player does the Widevine exchange; we never see a key.

    Why the licence URL already carries the licence token as a query parameter:
    that is the shape the TV app uses (`?license=<base64 blob>`). The blob is
    a capability that NLZiet's API turns into an EZDRM request; it is not an
    account credential, and it expires with the handshake.
    """

    manifest_url: str
    licence_url: str
    headers: dict = field(default_factory=dict)
    content_id: str = ""
    channel: str = ""
    session_id: str = ""
    title: str = ""

    def __repr__(self) -> str:  # keep tokens out of logs
        names = sorted(self.headers)
        return (
            f"Ticket(channel={self.channel!r}, content={self.content_id!r}, "
            f"session={self.session_id!r}, headers={names})"
        )


@dataclass(frozen=True)
class Reply:
    """One HTTP response, in transit between layers.

    The transport service builds these from upstream responses, and the web
    layer writes them out unchanged. Using one shared type instead of tuples
    means "bytes, headers, status" can never be assembled in the wrong order -
    a mistake that produces a blank player and no error message.

    Header keys are lowercase throughout: HTTP header names are
    case-insensitive, and normalising once at the edge removes a class of
    "why is there no Content-Type" bugs (the CDN in front of NLZiet already
    sends them lowercase, so mixed case here would be a matter of luck).
    """

    status: int
    headers: dict
    body: bytes

    def with_header(self, name: str, value: str) -> "Reply":
        # Copy-on-write: a reply is frozen so one caller cannot edit another's.
        headers = dict(self.headers)
        headers[name.lower()] = value
        return Reply(self.status, headers, self.body)
