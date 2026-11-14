"""Server-surface tests: the scheme a reply is built from, and TLS config.

Why these exist as tests: the scheme is not cosmetic. A browser only exposes
Widevine in a secure context, so an https player page that is handed an http://
segment base is blocked as mixed content - and the same bug shows up as
"afspelen mislukt: 6020" at load time when the page itself is plain http. Both
are invisible in a unit test of the player, so they are pinned here instead.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from nlzietd.config import Settings
from nlzietd.domain.models import Reply
from nlzietd.web.server import Request, build_router


class SchemeTests(unittest.TestCase):
    def _request(self, **kwargs) -> Request:
        base = dict(
            method="GET",
            path="/",
            raw_path="/",
            query={},
            headers={},
            body=b"",
        )
        base.update(kwargs)
        return Request(**base)

    def test_plain_socket_is_http(self):
        self.assertEqual(self._request().scheme, "http")

    def test_tls_socket_is_https(self):
        self.assertEqual(self._request(secure=True).scheme, "https")

    def test_proxy_header_is_believed(self):
        request = self._request(headers={"x-forwarded-proto": "https"})
        self.assertEqual(request.scheme, "https")

    def test_own_socket_beats_a_wrong_header(self):
        # A client can send any header it likes; the socket cannot lie.
        request = self._request(
            secure=True, headers={"x-forwarded-proto": "http"}
        )
        self.assertEqual(request.scheme, "https")


class RouterSchemeTests(unittest.TestCase):
    """The manifest handler must tell the player where the segments are.

    The player resolves every relative segment against what we inject, so this
    is the one place where the scheme is decided for the whole playback.
    """

    class StubCatalogue:
        def channels(self):
            return {}

    class StubTransport:
        def __init__(self) -> None:
            self.base: str | None = None

        def manifest(self, channel_id: str, base: str) -> Reply:
            self.base = base
            return Reply(200, {"content-type": "application/dash+xml"}, b"<MPD/>")

    def _router(self):
        transport = self.StubTransport()
        router = build_router(
            catalogue=self.StubCatalogue(),
            playback=object(),  # unused by the manifest route
            session=object(),
            profiles=object(),
            transport=transport,
            debug=False,
        )
        return router, transport

    def test_manifest_over_tls_points_segments_at_https(self):
        router, transport = self._router()
        reply = router.dispatch(
            Request(
                method="GET",
                path="/manifest/npo1.mpd",
                raw_path="/manifest/npo1.mpd",
                query={},
                headers={"host": "desktop.example.ts.net:8443"},
                body=b"",
                secure=True,
            )
        )
        self.assertEqual(reply.status, 200)
        self.assertEqual(
            transport.base, "https://desktop.example.ts.net:8443/seg/npo1/"
        )

    def test_manifest_over_http_keeps_http(self):
        router, transport = self._router()
        router.dispatch(
            Request(
                method="GET",
                path="/manifest/npo1.mpd",
                raw_path="/manifest/npo1.mpd",
                query={},
                headers={"host": "127.0.0.1:8099"},
                body=b"",
            )
        )
        self.assertEqual(transport.base, "http://127.0.0.1:8099/seg/npo1/")


class StatusRouteTests(unittest.TestCase):
    """The player page asks this to learn where the https listener is."""

    class Stub:
        def channels(self):
            return {}

        def status(self):
            return {"ok": True}

    def _status(self, tls_port):
        stub = self.Stub()
        router = build_router(
            catalogue=stub,
            playback=object(),
            session=stub,
            profiles=stub,
            transport=object(),
            debug=False,
            tls_port=tls_port,
        )
        reply = router.dispatch(
            Request(
                method="GET",
                path="/api/status",
                raw_path="/api/status",
                query={},
                headers={},
                body=b"",
            )
        )
        return json.loads(reply.body)

    def test_https_port_is_advertised(self):
        self.assertEqual(self._status(8443)["https_port"], 8443)

    def test_no_https_listener_is_null(self):
        self.assertIsNone(self._status(None)["https_port"])


class TlsSettingsTests(unittest.TestCase):
    def test_absent_cert_means_http_only(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertIsNone(settings.tls_cert)
        self.assertIsNone(settings.tls_key)
        self.assertEqual(settings.tls_port, 8443)

    def test_cert_and_key_are_paths(self):
        env = {
            "NLZIETD_TLS_CERT": "C:/state/tls/live.crt",
            "NLZIETD_TLS_KEY": "C:/state/tls/live.key",
            "NLZIETD_TLS_PORT": "9443",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        # Path renders separators for the host OS, so compare against Path
        # rather than a hardcoded Windows string: the same settings must be
        # valid when the gateway runs as a service on Linux.
        self.assertEqual(settings.tls_cert, Path(env["NLZIETD_TLS_CERT"]))
        self.assertEqual(settings.tls_key, Path(env["NLZIETD_TLS_KEY"]))
        self.assertIsInstance(settings.tls_cert, Path)
        self.assertEqual(settings.tls_port, 9443)


class SetupRouteTests(unittest.TestCase):
    """/setup is a standalone page listing the links and supported apps."""

    class Stub:
        def channels(self):
            return {}

        def status(self):
            return {"ok": True}

    def _router(self):
        stub = self.Stub()
        return build_router(
            catalogue=stub,
            playback=object(),
            session=stub,
            profiles=stub,
            transport=object(),
            debug=False,
        )

    def test_setup_page_is_served(self):
        reply = self._router().dispatch(
            Request(
                method="GET",
                path="/setup",
                raw_path="/setup",
                query={},
                headers={},
                body=b"",
            )
        )
        self.assertEqual(reply.status, 200)
        self.assertIn(b"text/html", reply.headers["content-type"].encode())
        body = reply.body.decode("utf-8")
        # The link and a couple of app rows must be present.
        self.assertIn("/playlist.m3u", body)
        self.assertIn("Kodi", body)
        self.assertIn("OTT Navigator", body)
        self.assertIn("TiviMate", body)


if __name__ == "__main__":
    unittest.main()
