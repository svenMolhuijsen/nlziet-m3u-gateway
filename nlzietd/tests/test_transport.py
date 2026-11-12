"""Transport tests: the rewriting and relaying rules, without a network.

These are where the fiddly parts live - a BaseURL in the right place, a query
token that survives URL joining, a path that cannot be turned into an open
proxy. They are pure functions plus fake collaborators, so they run in
milliseconds and pin the behaviour that is hard to debug through a player.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from nlzietd.domain.errors import ApiError, BadRequest
from nlzietd.domain.models import Reply, Ticket
from nlzietd.services.transport import TransportService, inject_base

MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" '
    'minimumUpdatePeriod="PT2S">\n'
    '  <Period id="p0" start="PT0S">\n'
    '    <AdaptationSet mimeType="video/mp4">\n'
    '      <SegmentTemplate media="sc-gaFEEw/$RepresentationID$_t$Time$.m4s?hdntl=t" />\n'
    "    </AdaptationSet>\n"
    "  </Period>\n"
    "</MPD>\n"
)

TICKET = Ticket(
    manifest_url="https://cdn.example/cenc/npo1/dash.mpd?start=1&hdnts=tok",
    licence_url="https://api.example/v9/license/proxy/Widevine?license=blob",
    headers={"Nlziet-License": "blob"},
    channel="npo1",
)


class StubPlayback:
    def __init__(self, ticket: Ticket = TICKET) -> None:
        self.ticket_value = ticket
        self.calls = 0
        self.forced = 0

    def ticket(self, channel_id, ttl=0, force=False):
        self.calls += 1
        if force:
            self.forced += 1
            # A forced mint must produce a *different* URL: reusing the old
            # one would make the retry pointless.
            self.ticket_value = replace(
                self.ticket_value,
                manifest_url="https://cdn.example/cenc/npo1/dash.mpd?start=2&hdnts=new",
            )
        return self.ticket_value


class FakeRelay:
    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, bytes, dict]] = []

    def get(self, url, headers=None):
        self.gets.append((url, dict(headers or {})))
        if not self.replies:
            raise AssertionError(f"unexpected GET {url}")
        return self.replies.pop(0)

    def post(self, url, body, headers=None):
        self.posts.append((url, body, dict(headers or {})))
        if not self.replies:
            raise AssertionError(f"unexpected POST {url}")
        return self.replies.pop(0)


def ok(body: str | bytes, status: int = 200) -> Reply:
    payload = body.encode() if isinstance(body, str) else body
    return Reply(status, {"content-type": "application/dash+xml"}, payload)


class InjectBaseTests(unittest.TestCase):
    def test_inserts_a_base_url_directly_after_the_mpd_tag(self):
        out = inject_base(MANIFEST, "http://box:8099/seg/npo1/")
        self.assertIn(
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" '
            'minimumUpdatePeriod="PT2S"><BaseURL>http://box:8099/seg/npo1/</BaseURL>',
            out,
        )
        # It must be at MPD level, not inside the Period.
        self.assertLess(out.index("<BaseURL>"), out.index("<Period"))

    def test_replaces_an_existing_top_level_base_url_instead_of_duplicating(self):
        manifest = MANIFEST.replace(
            'minimumUpdatePeriod="PT2S">',
            'minimumUpdatePeriod="PT2S"><BaseURL>https://cdn.example/cenc/npo1/</BaseURL>',
        )
        out = inject_base(manifest, "http://box/seg/npo1/")
        self.assertEqual(out.count("<BaseURL>"), 1)
        self.assertIn("<BaseURL>http://box/seg/npo1/</BaseURL>", out)

    def test_a_base_url_inside_a_period_is_left_alone(self):
        manifest = MANIFEST.replace(
            '<Period id="p0" start="PT0S">',
            '<Period id="p0" start="PT0S"><BaseURL>https://cdn.example/x/</BaseURL>',
        )
        out = inject_base(manifest, "http://box/seg/npo1/")
        self.assertIn("<BaseURL>https://cdn.example/x/</BaseURL>", out)
        self.assertIn("<BaseURL>http://box/seg/npo1/</BaseURL>", out)

    def test_non_mpd_payload_is_returned_untouched(self):
        self.assertEqual(inject_base("<html>nope</html>", "http://box/"), "<html>nope</html>")


class ManifestTests(unittest.TestCase):
    def test_manifest_is_fetched_with_ticket_headers_and_rewritten(self):
        relay = FakeRelay(ok(MANIFEST))
        transport = TransportService(StubPlayback(), relay)

        reply = transport.manifest("npo1", "http://box:8099/seg/npo1/")

        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers["content-type"], "application/dash+xml")
        self.assertIn(b"<BaseURL>http://box:8099/seg/npo1/</BaseURL>", reply.body)
        self.assertEqual(relay.gets[0][0], TICKET.manifest_url)
        self.assertEqual(relay.gets[0][1], TICKET.headers)

    def test_a_stale_signed_url_forces_one_new_handshake(self):
        playback = StubPlayback()
        relay = FakeRelay(ok("denied", 403), ok(MANIFEST))
        transport = TransportService(playback, relay)

        reply = transport.manifest("npo1", "http://box/seg/npo1/")

        self.assertEqual(reply.status, 200)
        self.assertEqual(playback.forced, 1)
        self.assertIn("hdnts=new", relay.gets[1][0])

    def test_a_real_upstream_failure_is_relayed_as_is(self):
        relay = FakeRelay(ok("boom", 500))
        transport = TransportService(StubPlayback(), relay)

        reply = transport.manifest("npo1", "http://box/seg/npo1/")

        self.assertEqual(reply.status, 500)
        self.assertEqual(reply.body, b"boom")


class SegmentTests(unittest.TestCase):
    def test_relative_segment_is_joined_onto_the_manifest_directory(self):
        relay = FakeRelay(ok(b"media", 200))
        transport = TransportService(StubPlayback(), relay)

        transport.segment("npo1", "sc-gaFEEw/V0_t123.m4s?hdntl=exp=1~acl=/cenc/npo1/*")

        self.assertEqual(
            relay.gets[0][0],
            "https://cdn.example/cenc/npo1/sc-gaFEEw/V0_t123.m4s?hdntl=exp=1~acl=/cenc/npo1/*",
        )
        self.assertEqual(relay.gets[0][1], TICKET.headers)

    def test_range_header_survives_the_hop(self):
        relay = FakeRelay(ok(b"part", 206))
        transport = TransportService(StubPlayback(), relay)

        reply = transport.segment("npo1", "init.mp4", "bytes=0-1023")

        self.assertEqual(reply.status, 206)
        self.assertEqual(relay.gets[0][1]["Range"], "bytes=0-1023")

    def test_absolute_and_network_paths_are_refused(self):
        transport = TransportService(StubPlayback(), FakeRelay())
        for rest in ("https://evil.example/x.m4s", "//evil.example/x.m4s", "/etc/passwd"):
            with self.assertRaises(BadRequest):
                transport.segment("npo1", rest)


class LicenceTests(unittest.TestCase):
    def test_challenge_is_posted_to_the_licence_url(self):
        relay = FakeRelay(Reply(200, {"content-type": "application/octet-stream"}, b"licence"))
        transport = TransportService(StubPlayback(), relay)

        reply = transport.licence("npo1", b"\x08\x04", "application/octet-stream")

        self.assertEqual(reply.body, b"licence")
        url, body, headers = relay.posts[0]
        self.assertEqual(url, TICKET.licence_url)
        self.assertEqual(body, b"\x08\x04")
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        # The capability header goes along, as the TV's player sends it.
        self.assertEqual(headers["Nlziet-License"], "blob")

    def test_a_ticket_without_a_licence_url_is_an_error(self):
        transport = TransportService(StubPlayback(replace(TICKET, licence_url="")), FakeRelay())
        with self.assertRaises(ApiError):
            transport.licence("npo1", b"\x08\x04")


if __name__ == "__main__":
    unittest.main()
