"""Tests that prove the layering, not just the code.

Every fake here is a plain object with the right methods - no mock library, no
inheritance. That is the point of `Protocol` ports: a service cannot tell the
difference between this and the real adapter, so these tests exercise the
real control flow (cache, retry, refresh) instead of a stubbed-out copy of it.

Run with:  python -m unittest discover -s nlzietd/tests -t .
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nlzietd.adapters.tokens import FileTokenStore
from nlzietd.domain.errors import ApiError, LoginError, NotAuthenticated
from nlzietd.domain.models import Channel, Program, Tokens
from nlzietd.services.playback import STREAM_TTL, TICKET_TTL, PlaybackService
from nlzietd.services.profile import ProfileSessions
from nlzietd.services.session import SessionService


class Clock:
    """Controllable time, so TTL tests never sleep."""

    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeStore:
    def __init__(self, tokens: Tokens | None = None) -> None:
        self.tokens = tokens
        self.saves = 0

    def load(self):
        return self.tokens

    def save(self, tokens):
        self.tokens = tokens
        self.saves += 1

    def clear(self):
        self.tokens = None


class FakeProviderWithExpiry:
    def __init__(self, now: Clock, expires_in: int = 3600) -> None:
        self.calls = 0
        self.clock = now
        self.expires_in = expires_in

    def obtain(self, previous):
        self.calls += 1
        return Tokens(
            access=f"token-{self.calls}",
            id_token="id",
            refresh="refresh",
            expires_at=int(self.clock()) + self.expires_in,
        )


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = FakeStore()
        self.provider = FakeProviderWithExpiry(self.clock)
        self.session = SessionService(
            store=self.store, provider=self.provider, skew=60, clock=self.clock
        )

    def test_token_is_minted_once_and_cached(self):
        first = self.session.token()
        second = self.session.token()
        self.assertEqual(first, second)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.saves, 1)

    def test_expired_token_triggers_exactly_one_obtain(self):
        self.store.tokens = Tokens("old", "id", "refresh", int(self.clock()) - 10)
        self.assertEqual(self.session.token(), "token-1")
        self.assertEqual(self.provider.calls, 1)

    def test_token_inside_skew_is_refreshed_early(self):
        # 30s of life left, 60s skew: treat as dead.
        self.store.tokens = Tokens("old", "id", "refresh", int(self.clock()) + 30)
        self.assertEqual(self.session.token(), "token-1")

    def test_force_reobtains(self):
        self.session.token()
        self.session.token(force=True)
        self.assertEqual(self.provider.calls, 2)

    def test_without_provider_it_refuses_politely(self):
        bare = SessionService(store=FakeStore(), provider=None, skew=60,
                              clock=self.clock)
        with self.assertRaises(NotAuthenticated):
            bare.token()

    def test_status_says_expired_when_the_token_has_died(self):
        # The 8-hour cliff: a dead token on disk is not a session, and this is
        # the field a monitor (or a person) reads to know it.
        self.store.tokens = Tokens("old", "id", "refresh", int(self.clock()) - 10)
        status = self.session.status()
        self.assertFalse(status["authenticated"])
        self.assertLess(status["expires_in"], 0)

    def test_status_says_whether_it_can_recover_by_itself(self):
        self.store.tokens = Tokens("old", "id", None, int(self.clock()) - 10)
        self.assertTrue(self.session.status()["can_relogin"])

        bare = SessionService(store=FakeStore(), provider=None, skew=60,
                              clock=self.clock)
        self.assertFalse(bare.status()["can_relogin"])
        self.assertFalse(bare.status()["authenticated"])

    def test_status_counts_a_fresh_token_as_authenticated(self):
        self.store.tokens = Tokens("live", "id", None, int(self.clock()) + 3600)
        status = self.session.status()
        self.assertTrue(status["authenticated"])
        self.assertGreater(status["expires_in"], 3500)


# -- profile tokens ---------------------------------------------------------


class FakeProfilesApi:
    def __init__(self, rows=None) -> None:
        self.rows = rows if rows is not None else [{"id": "p1"}, {"id": "p2"}]
        self.calls = 0

    def profiles(self, access_token):
        self.calls += 1
        return list(self.rows)


class FakeProfileTokens:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.mints: list[tuple[str, str]] = []

    def mint(self, access_token, profile_id):
        self.mints.append((access_token, profile_id))
        return Tokens(
            access=f"profile-{len(self.mints)}",
            id_token="",
            refresh=None,
            expires_at=int(self.clock()) + 3600,
        )


class ProfileSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeProfilesApi()
        self.tokens = FakeProfileTokens(self.clock)
        self.profiles = ProfileSessions(
            api=self.api, tokens=self.tokens, profile_id=None,
            skew=60, clock=self.clock,
        )

    def test_first_profile_is_resolved_once(self):
        self.assertEqual(self.profiles.token("user-1"), "profile-1")
        self.assertEqual(self.profiles.token("user-1"), "profile-1")
        self.assertEqual(self.api.calls, 1)
        self.assertEqual(self.tokens.mints, [("user-1", "p1")])

    def test_configured_profile_skips_the_lookup(self):
        profiles = ProfileSessions(
            api=self.api, tokens=self.tokens, profile_id="p2",
            skew=60, clock=self.clock,
        )
        self.assertEqual(profiles.token("user-1"), "profile-1")
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.tokens.mints, [("user-1", "p2")])

    def test_a_new_parent_token_forces_a_new_derivation(self):
        # The old profile token still has hours left, but it belongs to a user
        # session that was renewed; re-deriving is how a revoked session is
        # noticed at all.
        self.profiles.token("user-1")
        self.profiles.token("user-2")
        self.assertEqual(len(self.tokens.mints), 2)

    def test_expired_profile_token_is_reminted(self):
        self.profiles.token("user-1")
        self.clock.now += 3600
        self.profiles.token("user-1")
        self.assertEqual(len(self.tokens.mints), 2)

    def test_force_remints_without_clearing_the_profile_id(self):
        self.profiles.token("user-1")
        self.profiles.token("user-1", force=True)
        self.assertEqual(len(self.tokens.mints), 2)
        self.assertEqual(self.api.calls, 1)

    def test_account_without_profiles_is_a_login_error(self):
        bare = ProfileSessions(
            api=FakeProfilesApi(rows=[]), tokens=self.tokens, profile_id=None,
            skew=60, clock=self.clock,
        )
        with self.assertRaises(LoginError):
            bare.token("user-1")


# -- playback ---------------------------------------------------------------


class FakeCatalogue:
    def channels(self):
        return {
            "npo1": Channel("npo1", "npo1", "NPO 1", 1, "", True),
        }


HANDSHAKE = {
    "manifestUrl": "https://cdn.example/cenc/npo1/dash.mpd?start=1&hdnts=tok",
    "streamSessionId": "sess-1",
    "drm": {
        "licenseUrl": "https://api.example/v9/license/proxy/Widevine",
        "headers": {"Nlziet-License": "blob==", "X-Custom-Data": "secret"},
    },
}


class FakeApi:
    def __init__(self) -> None:
        self.live_calls = 0
        self.handshakes = 0

    def profiles(self, access_token):
        return [{"id": "p1"}]

    def live_program(self, access_token, channel_id):
        self.live_calls += 1
        if access_token == "stale":
            raise ApiError(401, "epg/live")
        return Program("content-1", "asset-1", "Ivo op zondag", channel_id,
                       "2026-11-15T19:02:44+01:00", "2026-11-15T20:00:03+01:00")

    def handshake(self, profile_token, content_id, channel):
        self.handshakes += 1
        return dict(HANDSHAKE)


class StubSession:
    """Only the methods PlaybackService is allowed to use."""

    def __init__(self) -> None:
        self.invalidations = 0
        self.stale = False

    def token(self, force=False):
        if force:
            self.stale = False
        return "stale" if self.stale else "fresh"

    def invalidate(self):
        self.invalidations += 1
        self.stale = False


class StubProfiles:
    def __init__(self) -> None:
        self.invalidations = 0

    def token(self, access_token, force=False):
        return "profile-token"

    def invalidate(self):
        self.invalidations += 1


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeApi()
        self.session = StubSession()
        self.profiles = StubProfiles()
        self.playback = PlaybackService(
            api=self.api, catalogue=FakeCatalogue(),
            session=self.session, profiles=self.profiles, clock=self.clock,
        )

    def test_ticket_carries_manifest_licence_and_session(self):
        ticket = self.playback.ticket("npo1")
        self.assertEqual(ticket.manifest_url, HANDSHAKE["manifestUrl"])
        self.assertEqual(
            ticket.licence_url,
            "https://api.example/v9/license/proxy/Widevine?license=blob==",
        )
        self.assertEqual(ticket.headers["Nlziet-License"], "blob==")
        self.assertEqual(ticket.channel, "npo1")
        self.assertEqual(ticket.content_id, "content-1")
        self.assertEqual(ticket.session_id, "sess-1")
        self.assertEqual(ticket.title, "Ivo op zondag")

    def test_401_is_retried_once_with_fresh_tokens(self):
        self.session.stale = True
        self.playback.ticket("npo1")
        self.assertEqual(self.api.live_calls, 2)
        self.assertEqual(self.session.invalidations, 1)
        self.assertEqual(self.profiles.invalidations, 1)

    def test_debug_ttl_expires_but_a_long_lived_caller_reuses_the_mint(self):
        self.playback.ticket("npo1", ttl=TICKET_TTL)
        self.clock.now += TICKET_TTL + 1
        # Same minted ticket, asked for with the proxy's longer TTL: still
        # good, because the CDN token outlives the debug cache.
        self.playback.ticket("npo1", ttl=STREAM_TTL)
        self.assertEqual(self.api.live_calls, 1)

        self.clock.now += STREAM_TTL
        self.playback.ticket("npo1", ttl=STREAM_TTL)
        self.assertEqual(self.api.live_calls, 2)

    def test_force_bypasses_the_cache(self):
        self.playback.ticket("npo1")
        self.playback.ticket("npo1", force=True)
        self.assertEqual(self.api.live_calls, 2)

    def test_unknown_channel_is_a_key_error(self):
        with self.assertRaises(KeyError):
            self.playback.ticket("does-not-exist")

    def test_ticket_repr_does_not_leak_header_values(self):
        text = repr(self.playback.ticket("npo1"))
        self.assertIn("X-Custom-Data", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("blob==", text)


class TokenStoreTests(unittest.TestCase):
    def test_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileTokenStore(Path(tmp) / "tokens.json")
            self.assertIsNone(store.load())
            store.save(Tokens("a", "i", "r", 123))
            loaded = store.load()
            self.assertEqual(loaded.access, "a")
            self.assertEqual(loaded.refresh, "r")
            store.clear()
            self.assertIsNone(store.load())

    def test_corrupt_file_reads_as_no_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(FileTokenStore(path).load())


if __name__ == "__main__":
    unittest.main()
