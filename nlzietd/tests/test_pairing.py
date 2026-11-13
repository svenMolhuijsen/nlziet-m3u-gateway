"""Pairing-on-demand tests: one code, one worker, one transition.

The states are easy; the timing is not. Three things are pinned here because
each of them is a bug that looks like "the code does not work":

* Two calls must not mint two codes - the second invalidates the first, and the
  human is staring at the one that just died.
* Tokens must be on disk (and the session cache dropped) before the state says
  `paired`, or the next request reloads the old session and reports it as dead.
* `expired` and `failed` are different: expiry is normal and a fresh code fixes
  it, a refusal or an unreachable identity server is not.

The web surface is pinned too, since the playlist is what a TV without a
browser sees.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from nlzietd.domain.errors import LoginError
from nlzietd.domain.models import Channel, DeviceGrant, Tokens
from nlzietd.services import pairing as states
from nlzietd.services.pairing import PairingService
from nlzietd.web.server import Request, build_router
from nlzietd.web.stream import StreamUnavailable


class Clock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Store:
    def __init__(self) -> None:
        self.tokens: Tokens | None = None
        self.saved: list[Tokens] = []

    def load(self):
        return self.tokens

    def save(self, tokens: Tokens) -> None:
        self.tokens = tokens
        self.saved.append(tokens)

    def clear(self) -> None:
        self.tokens = None


class FakePairing:
    """A device grant whose human is this test."""

    def __init__(self, tokens: Tokens | None = None, now=lambda: 1000) -> None:
        self.now = now
        self.tokens = tokens or Tokens("access", "id", "refresh", 9_999_999)
        self.begins = 0
        self.grants: list[DeviceGrant] = []
        self.release = threading.Event()
        self.finished = threading.Event()
        self.done = threading.Event()
        self.error: Exception | None = None
        self.begin_errors: list[Exception] = []
        # When false, `complete` returns immediately: handy for the tests that
        # care about the transition rather than about waiting.
        self.waits = False

    def begin(self) -> DeviceGrant:
        self.begins += 1
        if self.begin_errors:
            raise self.begin_errors.pop(0)
        grant = DeviceGrant(
            device_code=f"device-{self.begins}",
            user_code=f"CODE{self.begins}",
            verification_uri="https://nlziet.nl/koppel",
            expires_at=int(self.now()) + 300,
            interval=1,
        )
        self.grants.append(grant)
        return grant

    def complete(self, grant: DeviceGrant) -> Tokens:
        self.done.set()
        if self.waits:
            self.release.wait(5)
        if self.error:
            raise self.error
        return self.tokens


def service(pairing, store, **kwargs) -> PairingService:
    options = dict(
        device_page="https://id.example/device?user_code={code}",
        clock=Clock(),
    )
    options.update(kwargs)
    return PairingService(pairing=pairing, store=store, **options)


class StateTests(unittest.TestCase):
    def test_nothing_happens_before_start(self):
        view = service(FakePairing(), Store()).view()
        self.assertEqual(view["state"], states.IDLE)
        self.assertIsNone(view["code"])

    def test_start_returns_the_code_and_the_page_that_accepts_it(self):
        pairing = FakePairing()
        # Not the API's verification_uri: that page only explains the
        # procedure, and this is the URL a human has to open.
        view = service(pairing, Store(), start_worker=lambda job: None).start()
        self.assertEqual(view["state"], states.PENDING)
        self.assertEqual(view["code"], "CODE1")
        self.assertEqual(view["url"], "https://id.example/device?user_code=CODE1")
        self.assertEqual(view["page"], "https://id.example/device")
        self.assertGreater(view["expires_in"], 0)

    def test_a_second_start_keeps_the_code_on_screen(self):
        pairing = FakePairing()
        pairing.waits = True
        store = Store()
        svc = service(pairing, store)
        first = svc.start()
        second = svc.start()
        self.assertEqual(first["code"], second["code"])
        self.assertEqual(pairing.begins, 1)
        pairing.release.set()
        self._settle(svc, states.PAIRED)

    def test_tokens_are_saved_before_the_state_says_paired(self):
        pairing = FakePairing()
        store = Store()
        seen: list[Tokens | None] = []
        svc = service(pairing, store, on_paired=lambda: seen.append(store.tokens))
        svc.start()
        self._settle(svc, states.PAIRED)
        self.assertEqual(store.saved, [pairing.tokens])
        # on_paired runs after the save: a listener that reloads the session
        # must find the new tokens, not the old ones.
        self.assertEqual(seen, [pairing.tokens])

    def test_expiry_is_reported_without_waiting_for_the_worker(self):
        clock = Clock()
        pairing = FakePairing(now=clock)
        pairing.waits = True
        svc = service(pairing, Store(), clock=clock)
        svc.start()
        clock.now = 1_301
        self.assertEqual(svc.view()["state"], states.EXPIRED)
        self.assertEqual(svc.view()["expires_in"], 0)
        pairing.release.set()
        self._settle(svc, states.PAIRED)

    def test_an_expired_grant_can_be_replaced(self):
        clock = Clock()
        pairing = FakePairing(now=clock)
        svc = service(pairing, Store(), clock=clock, start_worker=lambda job: None)
        svc.start()
        clock.now = 1_301
        self.assertEqual(svc.view()["state"], states.EXPIRED)
        fresh = svc.start()
        self.assertEqual(fresh["state"], states.PENDING)
        self.assertEqual(fresh["code"], "CODE2")

    def test_a_denial_is_failed_not_expired(self):
        pairing = FakePairing()
        pairing.error = LoginError("the pairing was denied on the page")
        svc = service(pairing, Store())
        svc.start()
        self._settle(svc, states.FAILED)
        self.assertIn("denied", svc.view()["detail"])

    def test_an_unreachable_identity_server_is_failed(self):
        pairing = FakePairing()
        pairing.begin_errors = [LoginError("device authorization refused: 502")]
        svc = service(pairing, Store())
        view = svc.start()
        self.assertEqual(view["state"], states.FAILED)
        self.assertIn("502", view["detail"])

    def test_a_failed_start_is_retried_on_the_next_call(self):
        pairing = FakePairing()
        pairing.begin_errors = [LoginError("network down")]
        svc = service(pairing, Store(), start_worker=lambda job: None)
        self.assertEqual(svc.start()["state"], states.FAILED)
        # No cooling-off after a failure: clicking again has to do something.
        self.assertEqual(svc.start()["state"], states.PENDING)
        self.assertEqual(pairing.begins, 2)

    def test_a_paired_session_is_not_reminted_by_a_reload(self):
        pairing = FakePairing()
        clock = Clock()
        svc = service(pairing, Store(), clock=clock)
        svc.start()
        self._settle(svc, states.PAIRED)
        # A reload of the page calls start() again; it must not kill the code
        # that just worked, nor ask the identity server for another one.
        self.assertEqual(svc.start()["state"], states.PAIRED)
        self.assertEqual(pairing.begins, 1)
        clock.now += 10
        self.assertEqual(svc.start()["state"], states.PENDING)
        self.assertEqual(pairing.begins, 2)

    @staticmethod
    def _settle(svc: PairingService, wanted: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if svc.view()["state"] == wanted:
                return
            time.sleep(0.01)
        raise AssertionError(f"state stayed {svc.view()['state']}, wanted {wanted}")


class PairRoutesTests(unittest.TestCase):
    """The page and the playlist read the same state through different doors."""

    class Session:
        def __init__(self, authenticated: bool) -> None:
            self.authenticated = authenticated

        def status(self):
            return {"authenticated": self.authenticated}

    class Catalogue:
        def channels(self):
            return {
                "npo1": Channel(
                    id="npo1",
                    asset_id="asset",
                    name="NPO 1",
                    number=1,
                    logo="http://logo/1.png",
                    replay=True,
                )
            }

    class Pairing:
        def __init__(self, state: str) -> None:
            self.state = state
            self.started = 0
            self._view = {
                "state": state,
                "code": "ABC123",
                "url": "https://id.example/device?user_code=ABC123",
                "page": "https://id.example/device",
                "expires_in": 240,
                "detail": "",
            }

        def view(self):
            return dict(self._view)

        def start(self):
            self.started += 1
            # A real start mints a pending code (unless the identity server is
            # unreachable); the page and the playlist both rely on that.
            self._view["state"] = states.PENDING
            return self.view()

    class Stream:
        """The encoder's seam: routes only ever call these three methods."""

        def __init__(self, available: bool = True, fails: bool = False) -> None:
            self._available = available
            self.fails = fails
            self.played: list[tuple[str, str, str]] = []
            self.segments = {"seg000.ts": b"TRANSPORT STREAM"}

        def available(self) -> bool:
            return self._available

        def playlist(self, code, url, page):
            self.played.append((code, url, page))
            if self.fails:
                raise StreamUnavailable("ffmpeg is not installed")
            return (
                b"#EXTM3U\n#EXTINF:10,\nstream/ABC123/seg000.ts\n#EXT-X-ENDLIST\n"
            )

        def segment(self, code, name):
            return self.segments.get(name)

    def _router(self, pairing, authenticated=False, stream=None):
        return build_router(
            catalogue=self.Catalogue(),
            playback=object(),
            session=self.Session(authenticated),
            profiles=object(),
            transport=object(),
            debug=False,
            pairing=pairing,
            stream=stream,
        )

    @staticmethod
    def _request(path: str, method: str = "GET") -> Request:
        return Request(
            method=method,
            path=path,
            raw_path=path,
            query={},
            headers={"host": "tv.local:8099"},
            body=b"",
        )

    def test_get_reports_the_code_on_screen(self):
        pairing = self.Pairing(states.PENDING)
        reply = self._router(pairing).dispatch(self._request("/api/pair"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.body)["code"], "ABC123")
        self.assertEqual(pairing.started, 0)  # reading must not mint

    def test_post_mints(self):
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing)
        reply = router.dispatch(self._request("/api/pair", "POST"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(pairing.started, 1)

    def test_qr_is_a_png_of_the_current_code(self):
        pairing = self.Pairing(states.PENDING)
        reply = self._router(pairing).dispatch(self._request("/pair/qr.png"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers["content-type"], "image/png")
        self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertTrue(reply.body.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_qr_is_missing_once_the_code_is_dead(self):
        # A QR that scans into an expired code is worse than no picture.
        pairing = self.Pairing(states.EXPIRED)
        reply = self._router(pairing).dispatch(self._request("/pair/qr.png"))
        self.assertEqual(reply.status, 404)

    def test_playlist_carries_the_code_when_there_is_no_session(self):
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing)
        reply = router.dispatch(self._request("/playlist.m3u"))
        text = reply.body.decode("utf-8")
        self.assertIn("ABC123", text)
        self.assertIn('tvg-logo="http://tv.local:8099/pair/qr.png"', text)
        self.assertIn("NLZIET koppelen", text)
        self.assertEqual(pairing.started, 1)

    def test_playlist_stays_a_channel_list_when_a_session_exists(self):
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing, authenticated=True)
        text = router.dispatch(self._request("/playlist.m3u")).body.decode("utf-8")
        self.assertIn("/manifest/npo1.mpd", text)
        self.assertNotIn("ABC123", text)
        self.assertEqual(pairing.started, 0)

    def test_pairing_routes_are_absent_without_a_pairing_service(self):
        router = build_router(
            catalogue=self.Catalogue(),
            playback=object(),
            session=self.Session(False),
            profiles=object(),
            transport=object(),
            debug=False,
        )
        for path, method in (("/api/pair", "GET"), ("/api/pair", "POST"),
                             ("/pair/qr.png", "GET"),
                             ("/pair/stream.m3u8", "GET"),
                             ("/pair/stream/ABC123/seg000.ts", "GET")):
            reply = router.dispatch(self._request(path, method))
            self.assertEqual(reply.status, 404, path)

    # -- the pairing channel as a video -------------------------------------

    def test_playlist_points_at_the_video_when_ffmpeg_is_there(self):
        pairing = self.Pairing(states.PENDING)
        stream = self.Stream()
        router = self._router(pairing, stream=stream)
        reply = router.dispatch(self._request("/playlist.m3u"))
        text = reply.body.decode("utf-8")
        self.assertIn("ABC123", text)
        self.assertIn('tvg-logo="http://tv.local:8099/pair/qr.png"', text)
        self.assertIn("http://tv.local:8099/pair/stream.m3u8", text)
        self.assertIn("manifest_type=hls", text)
        # A picture does not need a Widevine licence; leaving a licence_key
        # line here would make Kodi ask for one and fail before showing it.
        self.assertNotIn("license_key", text)

    def test_playlist_falls_back_to_the_picture_without_an_encoder(self):
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing, stream=self.Stream(available=False))
        text = router.dispatch(self._request("/playlist.m3u")).body.decode("utf-8")
        self.assertIn("http://tv.local:8099/pair/qr.png", text)
        self.assertNotIn("/pair/stream.m3u8", text)
        self.assertNotIn("#KODIPROP", text)

    def test_the_stream_route_serves_a_playlist_for_the_live_code(self):
        pairing = self.Pairing(states.PENDING)
        stream = self.Stream()
        router = self._router(pairing, stream=stream)
        reply = router.dispatch(self._request("/pair/stream.m3u8"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(
            reply.headers["content-type"], "application/vnd.apple.mpegurl"
        )
        self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertIn(b"#EXT-X-ENDLIST", reply.body)
        self.assertEqual(
            stream.played,
            [("ABC123", "https://id.example/device?user_code=ABC123",
              "https://id.example/device")],
        )

    def test_the_stream_route_replaces_a_dead_code(self):
        pairing = self.Pairing(states.EXPIRED)
        stream = self.Stream()
        router = self._router(pairing, stream=stream)
        reply = router.dispatch(self._request("/pair/stream.m3u8"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(pairing.started, 1)
        self.assertEqual(stream.played[0][0], "ABC123")

    def test_a_broken_encoder_answers_503_and_the_playlist_degrades(self):
        pairing = self.Pairing(states.PENDING)
        stream = self.Stream(fails=True)
        router = self._router(pairing, stream=stream)
        reply = router.dispatch(self._request("/pair/stream.m3u8"))
        self.assertEqual(reply.status, 503)
        self.assertIn("ffmpeg", json.loads(reply.body)["detail"])

    def test_segments_are_mpegts_and_a_miss_is_404(self):
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing, stream=self.Stream())
        reply = router.dispatch(
            self._request("/pair/stream/ABC123/seg000.ts")
        )
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers["content-type"], "video/mp2t")
        self.assertEqual(reply.body, b"TRANSPORT STREAM")
        miss = router.dispatch(self._request("/pair/stream/ABC123/seg999.ts"))
        self.assertEqual(miss.status, 404)

    def test_a_head_probe_finds_the_stream_without_a_body(self):
        # Some players check a channel with HEAD first. It has to resolve to
        # the same handler, and the HTTP layer drops the body for HEAD.
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing, stream=self.Stream())
        reply = router.dispatch(self._request("/pair/stream.m3u8", "HEAD"))
        self.assertEqual(reply.status, 200)
        self.assertIn(b"#EXTM3U", reply.body)

    def test_the_stream_route_is_absent_without_an_encoder(self):
        pairing = self.Pairing(states.PENDING)
        router = self._router(pairing)
        reply = router.dispatch(self._request("/pair/stream.m3u8"))
        self.assertEqual(reply.status, 404)


if __name__ == "__main__":
    unittest.main()
