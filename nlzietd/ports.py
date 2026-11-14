"""Ports: what the services are allowed to depend on.

Services import from this module; services never import from `adapters`.
That inversion is the whole point of the layout. Concretely: the daemon only
knows it holds a `SessionProvider`; whether that is the device-paired session
or a test double is an `app.py` decision, not a refactor.

These are `Protocol`s rather than ABCs because the adapters should not have
to inherit anything - a test double is just an object with the right methods.
"""

from __future__ import annotations

from typing import Protocol

from .domain.models import Channel, DeviceGrant, Program, Reply, Tokens


class TokenStore(Protocol):
    """Where tokens live between runs.

    Kept narrow on purpose: the service owns when to save, the store owns how.
    """

    def load(self) -> Tokens | None: ...

    def save(self, tokens: Tokens) -> None: ...

    def clear(self) -> None: ...


class SessionProvider(Protocol):
    """Renews a device-paired session.

    `previous` is a hint, not a contract: the provider refreshes with its
    refresh token if it has one. With no refresh token there is no silent
    recovery - a human has to pair the machine again, and the provider says
    so instead of trying something else.
    """

    def obtain(self, previous: Tokens | None) -> Tokens: ...


class DevicePairing(Protocol):
    """Interactive half of the device grant: start a pairing, wait for a human.

    Separate from `SessionProvider` because the two happen at different times:
    the daemon renews quietly in the background, while pairing is something a
    person starts - from the command line, or from the page when it finds no
    session. One service drives both callers.
    """

    def begin(self) -> DeviceGrant:
        """Ask for a code and where to enter it."""

    def complete(self, grant: DeviceGrant) -> Tokens:
        """Block until the code is entered, the grant expires, or it is denied."""


class ProfileTokens(Protocol):
    """Mints the profile-scoped token.

    NLZiet has two token scopes and the handshake only answers to the second:
    the user token (from login) opens the catalogue and the EPG, while a
    profile token - derived from the user token and one profile id - is what
    allocates a stream. Keeping the mint behind a port means the derivation
    (a `grant_type=profile` POST today) can change without touching playback.
    """

    def mint(self, access_token: str, profile_id: str) -> Tokens: ...


class NlzietApi(Protocol):
    """The subset of api.nlziet.nl this gateway actually uses.

    Three calls, each a different kind of thing: account metadata (profiles),
    what is on right now (EPG live), and the stream handshake. Nothing here
    knows about caching, retries or tokens - those are service decisions.
    """

    def profiles(self, access_token: str) -> list[dict]:
        """The account's profiles. The first one is the default."""

    def live_program(self, access_token: str, channel_id: str) -> Program:
        """What is on this channel at this moment."""

    def handshake(self, profile_token: str, content_id: str, channel: str) -> dict:
        """Allocate a stream for a live content item; returns the raw payload."""


class Catalogue(Protocol):
    """Channel metadata source, by key."""

    def channels(self) -> dict[str, Channel]: ...


class Relay(Protocol):
    """A plain HTTP client for bytes we pass through untouched.

    Separate from `NlzietApi` because the two have opposite failure models.
    The API returns JSON we parse, so a bad status is an exception. The relay
    returns manifests and media segments whose status and headers belong to
    the player: a 404 from the CDN must reach the player as a 404, not be
    turned into an exception we then have to invent a status for.
    """

    def get(self, url: str, headers: dict | None = None) -> Reply: ...

    def post(self, url: str, body: bytes, headers: dict | None = None) -> Reply: ...
