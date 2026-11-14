"""Tests for the local M3U checker.

A playlist is a wire format, not a Python object: the thing that breaks in a
player is usually a line it did not expect, or a channel it tried to open in
the wrong way. These tests pin the two shapes the daemon emits (the pairing
entry and the channel list) and the one decision that matters downstream -
whether an entry needs a CDM.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from nlzietd.tools import m3u_player as mp

PAIRING_PLAYLIST = """#EXTM3U
#EXTINF:-1 tvg-id="nlzietd-pair" tvg-name="NLZIET koppelen" tvg-logo="http://tv.local:8099/pair/qr.png" group-title="NLZIET koppelen",ABC123 · log in op id.nlziet.nl/device
#KODIPROP:inputstream=inputstream.adaptive
#KODIPROP:inputstream.adaptive.manifest_type=hls
http://tv.local:8099/pair/stream.m3u8
"""

IMAGE_FALLBACK = """#EXTM3U
#EXTINF:-1 tvg-id="nlzietd-pair" group-title="NLZIET koppelen",NLZIET niet gekoppeld
http://tv.local:8099/pair/qr.png
"""

CHANNELS = """#EXTM3U
#EXTINF:-1 tvg-id="npo1" tvg-name="NPO 1" tvg-logo="http://logo/1.png" group-title="NLZiet",NPO 1
#KODIPROP:inputstream=inputstream.adaptive
#KODIPROP:inputstream.adaptive.manifest_type=mpd
#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha
#KODIPROP:inputstream.adaptive.license_key=http://tv.local:8099/licence/npo1||R{SSM}|R
http://tv.local:8099/manifest/npo1.mpd
#EXTINF:-1 tvg-id="rtl4" tvg-name="RTL 4" tvg-logo="http://logo/4.png" group-title="NLZiet",RTL 4
http://tv.local:8099/manifest/rtl4.mpd
"""


class ParseTests(unittest.TestCase):
    def test_the_pairing_entry_is_plain_hls_not_drm(self):
        entries = mp.parse(PAIRING_PLAYLIST)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertIn("ABC123", entry.name)
        self.assertEqual(entry.url, "http://tv.local:8099/pair/stream.m3u8")
        self.assertEqual(entry.kind, "hls")
        self.assertFalse(entry.drm)
        self.assertEqual(entry.attrs["tvg-id"], "nlzietd-pair")
        self.assertEqual(
            entry.props["inputstream.adaptive.manifest_type"], "hls"
        )

    def test_a_manifest_type_alone_does_not_make_a_stream_drm(self):
        entry = mp.parse(PAIRING_PLAYLIST)[0]
        self.assertIn("inputstream.adaptive.manifest_type", entry.props)
        self.assertFalse(entry.drm)

    def test_the_image_fallback_is_not_a_video(self):
        entry = mp.parse(IMAGE_FALLBACK)[0]
        self.assertEqual(entry.kind, "image")
        self.assertFalse(entry.drm)

    def test_real_channels_are_marked_as_needing_a_cdm(self):
        entries = mp.parse(CHANNELS)
        self.assertEqual([e.name for e in entries], ["NPO 1", "RTL 4"])
        self.assertTrue(entries[0].drm)
        self.assertEqual(entries[0].kind, "widevine")
        self.assertIn("license_key", " ".join(entries[0].props))
        # The second entry has no KODIPROP lines at all: a player must not
        # inherit properties from the channel before it.
        self.assertFalse(entries[1].drm)
        self.assertEqual(entries[1].props, {})

    def test_unrelated_tags_and_empty_lines_are_ignored(self):
        text = "#EXTM3U\n#EXTVLCOPT:network-caching=1000\n\n" + IMAGE_FALLBACK
        self.assertEqual(len(mp.parse(text)), 1)

    def test_an_extinf_without_a_url_is_not_an_entry(self):
        text = "#EXTM3U\n#EXTINF:-1,orphan\n" + IMAGE_FALLBACK
        entries = mp.parse(text)
        self.assertEqual(len(entries), 1)
        self.assertIn("niet gekoppeld", entries[0].name)


class BehaviourTests(unittest.TestCase):
    def test_check_reports_the_status_and_type(self):
        response = mock.MagicMock()
        response.status = 200
        response.headers = {"content-type": "application/vnd.apple.mpegurl"}
        response.read.return_value = b"#EXTM3U\n"
        response.__enter__.return_value = response
        with mock.patch.object(mp.urllib.request, "urlopen", return_value=response):
            line = mp.check(mp.parse(PAIRING_PLAYLIST)[0])
        self.assertIn("200", line)
        self.assertIn("mpegurl", line)

    def test_vlc_is_preferred_over_ffplay(self):
        with mock.patch.object(mp.shutil, "which", side_effect=["vlc", "ffplay"]):
            self.assertEqual(mp.find_player(), "vlc")

    def test_ffplay_next_to_an_explicit_ffmpeg_is_a_fallback(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "ffmpeg.exe").write_bytes(b"")
            (folder / "ffplay.exe").write_bytes(b"")
            with mock.patch.object(mp.shutil, "which", return_value=None):
                with mock.patch.dict(
                    "os.environ", {"NLZIETD_FFMPEG": str(folder / "ffmpeg.exe")}
                ):
                    self.assertEqual(
                        mp.find_player(), str(folder / "ffplay.exe")
                    )

    def test_playing_a_drm_channel_explains_instead_of_launching(self):
        with mock.patch.object(mp.subprocess, "Popen") as popen:
            with redirect_stdout(io.StringIO()) as out:
                result = mp.play(mp.parse(CHANNELS)[0], "http://tv.local:8099")
        self.assertEqual(result, 1)
        self.assertIn("Widevine", out.getvalue())
        popen.assert_not_called()

    def test_list_action_prints_every_entry(self):
        with TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "playlist.m3u"
            playlist.write_text(PAIRING_PLAYLIST, encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                result = mp.main(["--file", str(playlist), "--list"])
        self.assertEqual(result, 0)
        self.assertIn("[hls", out.getvalue())
        self.assertIn("ABC123", out.getvalue())


if __name__ == "__main__":
    unittest.main()
