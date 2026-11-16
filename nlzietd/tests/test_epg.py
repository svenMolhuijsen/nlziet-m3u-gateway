"""EPG: the XMLTV builder and the /epg.xml route.

The guide is titles and times - nothing secret, nothing that plays. So these
tests pin the two things that actually break a guide in a player: the channel
`id` in XMLTV must equal the M3U `tvg-id` (or nothing binds), and the times
must be XMLTV's `YYYYMMDDHHMMSS +ZZZZ` (or the player drops the programme). A
fake api/catalogue/session keeps it offline.
"""

from __future__ import annotations

import unittest

from nlzietd.domain.models import Channel, EpgEntry
from nlzietd.web.epg import EpgService, _xmltv_time
from nlzietd.web.player import playlist
from nlzietd.web.server import Request, build_router


def _channel(cid, name, number):
    return Channel(id=cid, asset_id=cid, name=name, number=number,
                   logo=f"https://logo/{cid}.png", replay=True)


class FakeCatalogue:
    def __init__(self, channels):
        self._channels = channels

    def channels(self):
        return self._channels


class FakeSession:
    def __init__(self, token="user-token"):
        self._token = token

    def token(self, force=False):
        return self._token


class FakeApi:
    """Returns a fixed day per channel, and records the calls made."""

    def __init__(self, by_channel):
        self._by_channel = by_channel
        self.calls = []

    def epg(self, access_token, channel_id, date):
        self.calls.append((channel_id, date))
        return self._by_channel.get(channel_id, [])


class XmltvTimeTests(unittest.TestCase):
    def test_offset_iso_becomes_xmltv(self):
        self.assertEqual(
            _xmltv_time("2026-11-16T00:51:58+01:00"), "20261116005158 +0100"
        )

    def test_unparsable_is_empty(self):
        self.assertEqual(_xmltv_time("not a date"), "")
        self.assertEqual(_xmltv_time(""), "")


class EpgBuildTests(unittest.TestCase):
    def _service(self, days=1):
        channels = {
            "npo1": _channel("npo1", "NPO 1", 1),
            "rtl4": _channel("rtl4", "RTL 4", 4),
        }
        api = FakeApi({
            "npo1": [
                EpgEntry("npo1", "WNL Op Zondag", "Afl. 10", "Met gasten.",
                         "2026-11-16T00:51:58+01:00", "2026-11-16T01:55:03+01:00"),
            ],
            "rtl4": [
                EpgEntry("rtl4", "RTL Nieuws", "", "",
                         "2026-11-16T07:30:00+01:00", "2026-11-16T08:00:00+01:00"),
            ],
        })
        svc = EpgService(api=api, catalogue=FakeCatalogue(channels),
                         session=FakeSession(), days=days)
        return svc, api

    def test_channels_declared_with_catalogue_ids(self):
        svc, _ = self._service()
        doc = svc.xmltv()
        # The channel id must be the tvg-id a player matches on.
        self.assertIn('<channel id="npo1">', doc)
        self.assertIn("<display-name>NPO 1</display-name>", doc)
        self.assertIn('<icon src="https://logo/npo1.png" />', doc)

    def test_programme_times_are_xmltv(self):
        svc, _ = self._service()
        doc = svc.xmltv()
        self.assertIn(
            '<programme start="20261116005158 +0100" '
            'stop="20261116015503 +0100" channel="npo1">',
            doc,
        )
        self.assertIn("<title>WNL Op Zondag</title>", doc)
        self.assertIn("<sub-title>Afl. 10</sub-title>", doc)

    def test_empty_subtitle_and_desc_are_omitted(self):
        svc, _ = self._service()
        doc = svc.xmltv()
        # rtl4 has neither; its programme must not carry empty tags.
        self.assertIn('channel="rtl4"', doc)
        self.assertNotIn("<sub-title></sub-title>", doc)
        self.assertNotIn("<desc></desc>", doc)

    def test_document_is_cached(self):
        svc, api = self._service()
        svc.xmltv()
        first = len(api.calls)
        svc.xmltv()
        self.assertEqual(len(api.calls), first)  # second call served from cache

    def test_one_channel_failure_is_not_fatal(self):
        channels = {
            "npo1": _channel("npo1", "NPO 1", 1),
            "boom": _channel("boom", "Boom TV", 9),
        }

        class HalfBrokenApi:
            def epg(self, access_token, channel_id, date):
                if channel_id == "boom":
                    raise RuntimeError("500 from upstream")
                return [EpgEntry("npo1", "Show", "", "",
                                 "2026-11-16T10:00:00+01:00",
                                 "2026-11-16T11:00:00+01:00")]

        svc = EpgService(api=HalfBrokenApi(), catalogue=FakeCatalogue(channels),
                         session=FakeSession(), days=1)
        doc = svc.xmltv()
        # Both channels still declared; the working one still has its programme.
        self.assertIn('<channel id="npo1">', doc)
        self.assertIn('<channel id="boom">', doc)
        self.assertIn("<title>Show</title>", doc)

    def test_escaping(self):
        channels = {"c": _channel("c", "A & B", 1)}
        api = FakeApi({"c": [EpgEntry("c", "Tom & Jerry <3", "", "x>y",
                                      "2026-11-16T10:00:00+01:00",
                                      "2026-11-16T11:00:00+01:00")]})
        svc = EpgService(api=api, catalogue=FakeCatalogue(channels),
                         session=FakeSession(), days=1)
        doc = svc.xmltv()
        self.assertIn("A &amp; B", doc)
        self.assertIn("Tom &amp; Jerry &lt;3", doc)
        self.assertNotIn("<title>Tom & Jerry", doc)


class PlaylistUrlTvgTests(unittest.TestCase):
    def test_header_carries_epg_url(self):
        channels = {"npo1": _channel("npo1", "NPO 1", 1)}
        text = playlist("https://host.ts.net", channels)
        self.assertTrue(
            text.startswith('#EXTM3U url-tvg="https://host.ts.net/epg.xml"')
        )


class EpgRouteTests(unittest.TestCase):
    class StubCat:
        def channels(self):
            return {"npo1": _channel("npo1", "NPO 1", 1)}

    class StubSession:
        def token(self, force=False):
            return "t"

        def status(self):
            return {"authenticated": True}

    class FakeEpg:
        def xmltv(self, force=False):
            return '<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>'

    def _router(self, epg):
        cat = self.StubCat()
        return build_router(
            catalogue=cat,
            playback=object(),
            session=self.StubSession(),
            profiles=object(),
            transport=object(),
            debug=False,
            epg=epg,
        )

    def _get(self, router, path="/epg.xml"):
        return router.dispatch(Request(
            method="GET", path=path, raw_path=path, query={}, headers={}, body=b"",
        ))

    def test_epg_route_serves_xml(self):
        reply = self._get(self._router(self.FakeEpg()))
        self.assertEqual(reply.status, 200)
        self.assertIn(b"application/xml", reply.headers["content-type"].encode())
        self.assertIn(b"<tv>", reply.body)

    def test_epg_route_404_when_absent(self):
        reply = self._get(self._router(None))
        self.assertEqual(reply.status, 404)


if __name__ == "__main__":
    unittest.main()
