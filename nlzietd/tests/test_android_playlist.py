"""Tests for the Android TV playlist variant.

Kodi wants `license_key=url||R{SSM}|R`; native Android TV players (OTT
Navigator, Prysm) read the same `#KODIPROP` lines but want a plain licence URL,
because their MediaDrm already POSTs the raw challenge. Both forms must keep
pointing at the same `/licence/<ch>` proxy, and the choice must come from the
query string rather than from a second URL nobody can find.
"""

from __future__ import annotations

import unittest

from nlzietd.domain.models import Channel
from nlzietd.web.player import playlist
from nlzietd.web.server import Request, build_router


def channels() -> dict[str, Channel]:
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


class VariantTests(unittest.TestCase):
    def test_default_is_the_kodi_four_field_form(self):
        text = playlist("http://tv:8099", channels())
        self.assertIn(
            "#KODIPROP:inputstream.adaptive.license_key="
            "http://tv:8099/licence/npo1||R{SSM}|R",
            text,
        )

    def test_android_variant_is_a_plain_licence_url(self):
        text = playlist("http://tv:8099", channels(), android=True)
        self.assertIn(
            "#KODIPROP:inputstream.adaptive.license_key=http://tv:8099/licence/npo1\n",
            text,
        )
        self.assertNotIn("R{SSM}", text)
        self.assertIn("com.widevine.alpha", text)
        self.assertIn("manifest_type=mpd", text)

    def test_both_variants_keep_the_dash_manifest(self):
        for android in (False, True):
            text = playlist("https://node.ts.net", channels(), android=android)
            self.assertIn("https://node.ts.net/manifest/npo1.mpd", text)


class RouteTests(unittest.TestCase):
    class Catalogue:
        def channels(self):
            return channels()

    class Session:
        def status(self):
            return {"authenticated": True}

    def _request(self, query: dict) -> Request:
        raw = "/playlist.m3u"
        if query:
            raw += "?android=1"
        return Request(
            method="GET",
            path="/playlist.m3u",
            raw_path=raw,
            query=query,
            headers={"host": "tv:8099"},
            body=b"",
        )

    def _router(self):
        return build_router(
            catalogue=self.Catalogue(),
            playback=object(),
            session=self.Session(),
            profiles=object(),
            transport=object(),
            debug=False,
        )

    def test_no_query_keeps_kodi_default(self):
        text = self._router().dispatch(self._request({})).body.decode("utf-8")
        self.assertIn("||R{SSM}|R", text)

    def test_android_query_drops_the_wrapper(self):
        text = self._router().dispatch(
            self._request({"android": ["1"]})
        ).body.decode("utf-8")
        self.assertNotIn("R{SSM}", text)
        self.assertIn(
            "#KODIPROP:inputstream.adaptive.license_key=http://tv:8099/licence/npo1\n",
            text,
        )

    def test_flavor_android_alias_works(self):
        reply = self._router().dispatch(
            self._request({"flavor": ["android"]})
        )
        self.assertNotIn(b"R{SSM}", reply.body)

    def test_android_zero_is_not_the_variant(self):
        reply = self._router().dispatch(self._request({"android": ["0"]}))
        self.assertIn(b"R{SSM}", reply.body)


if __name__ == "__main__":
    unittest.main()
