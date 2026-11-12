"""Device-grant tests: the pairing state machine, without a network.

The interesting part is not the HTTP, it is which 400 means what. RFC 8628
answers `authorization_pending` until a human acts, `slow_down` when we poll too
fast, `access_denied` when they refuse, and `expired_token` when the window has
closed - all with the same status. Collapsing them into "400, give up" kills a
pairing that was about to succeed; treating all of them as "keep going" polls
the identity server forever. Both are pinned here.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from nlzietd.adapters import identity
from nlzietd.domain.errors import ApiError, LoginError
from nlzietd.domain.models import DeviceGrant, Tokens


def _grant(now: int = 1000) -> DeviceGrant:
    return DeviceGrant(
        device_code="dev-code",
        user_code="TEST01",
        verification_uri="https://nlziet.nl/koppel",
        expires_at=now + 300,
        interval=5,
    )


def _api_error(error: str) -> ApiError:
    return ApiError(400, "connect/token", json.dumps({"error": error}))


class Clock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Sleeps:
    """Fake sleep that also moves the clock, so expiry is testable."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.now += seconds


class DeviceLoginTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sleep = Sleeps(self.clock)
        self.login = identity.OidcDeviceLogin(
            "https://id.example", clock=self.clock, sleep=self.sleep
        )

    # -- pairing ------------------------------------------------------------

    def test_begin_returns_what_the_human_needs(self):
        with mock.patch.object(identity, "_post_device", return_value={
            "device_code": "dev-code",
            "user_code": "TEST01",
            "verification_uri": "https://nlziet.nl/koppel",
            "expires_in": 300,
            "interval": 5,
        }):
            grant = self.login.begin()
        self.assertEqual(grant.user_code, "TEST01")
        self.assertEqual(grant.verification_uri, "https://nlziet.nl/koppel")
        self.assertEqual(grant.expires_at, 1300)

    def test_pending_then_a_token(self):
        ready = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        with mock.patch.object(
            identity, "_post_token",
            side_effect=[_api_error("authorization_pending"),
                         _api_error("authorization_pending"),
                         ready],
        ):
            tokens = self.login.complete(_grant())
        self.assertEqual(tokens.access, "a")
        self.assertEqual(tokens.refresh, "r")
        # The server's interval is a floor, and it was waited out twice.
        self.assertEqual(self.sleep.calls, [5, 5])

    def test_slow_down_widens_the_interval(self):
        ready = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        with mock.patch.object(
            identity, "_post_token",
            side_effect=[_api_error("slow_down"), ready],
        ):
            self.login.complete(_grant())
        self.assertEqual(self.sleep.calls, [10])

    def test_access_denied_stops_instead_of_retrying(self):
        with mock.patch.object(
            identity, "_post_token", side_effect=_api_error("access_denied"),
        ) as post:
            with self.assertRaises(LoginError):
                self.login.complete(_grant())
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.sleep.calls, [])

    def test_an_expired_grant_is_never_polled(self):
        grant = _grant(now=self.clock.now - 400)
        with mock.patch.object(identity, "_post_token") as post:
            with self.assertRaises(LoginError):
                self.login.complete(grant)
        self.assertEqual(post.call_count, 0)

    # -- renewal ------------------------------------------------------------

    def test_obtain_without_a_refresh_token_says_how_to_pair(self):
        with self.assertRaises(LoginError) as caught:
            self.login.obtain(Tokens("a", "i", None, 1))
        self.assertIn("login", str(caught.exception))

    def test_obtain_renews_with_the_refresh_token(self):
        with mock.patch.object(identity, "_post_token", return_value={
            "access_token": "fresh", "refresh_token": "r2", "expires_in": 3600,
        }) as post:
            tokens = self.login.obtain(Tokens("old", "i", "r1", 1))
        self.assertEqual(tokens.access, "fresh")
        form = post.call_args[0][1]
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "r1")

    def test_a_revoked_refresh_token_asks_for_a_new_pairing(self):
        with mock.patch.object(
            identity, "_post_token", side_effect=_api_error("invalid_grant"),
        ):
            with self.assertRaises(LoginError) as caught:
                self.login.obtain(Tokens("old", "i", "r1", 1))
        self.assertIn("pair", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
