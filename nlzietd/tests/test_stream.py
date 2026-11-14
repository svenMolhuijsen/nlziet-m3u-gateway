"""Pairing-stream tests: encoding is a program we do not ship.

The clip is produced by a subprocess, so the interesting behaviour is not the
video but the seams around it - which command is built, what the playlist says
the segments are called, what happens when the encoder is missing or broken,
and how long a built clip is kept. A fake runner stands in for ffmpeg; a real
run is checked separately with a frame extract and a QR decoder.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from nlzietd.web.stream import PairingStream, StreamUnavailable, _rewrite

CODE = "ABC123"
URL = "https://id.nlziet.nl/device?user_code=ABC123"
PAGE = "https://id.nlziet.nl/device"
PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXTINF:10.000000,\n"
    "seg000.ts\n"
    "#EXTINF:10.000000,\n"
    "seg001.ts\n"
    "#EXT-X-ENDLIST\n"
)


class Recorder:
    """Fake ffmpeg: writes the files it is supposed to write, and remembers."""

    def __init__(self, with_segments: bool = True, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.cards: list[bytes] = []
        self.workdirs: list[Path] = []
        self.with_segments = with_segments
        self.fail = fail

    def __call__(self, argv: list[str], cwd: Path, timeout: float) -> None:
        self.calls.append(argv)
        self.workdirs.append(cwd)
        self.cards.append((cwd / "card.png").read_bytes())
        if self.fail:
            raise RuntimeError("no encoder for libx264")
        (cwd / "index.m3u8").write_text(PLAYLIST, encoding="utf-8")
        if self.with_segments:
            (cwd / "seg000.ts").write_bytes(b"TS ZERO")
            (cwd / "seg001.ts").write_bytes(b"TS ONE")


def stream(recorder: Recorder | None = None, **kwargs) -> PairingStream:
    options = dict(ffmpeg="ffmpeg", runner=recorder or Recorder(), duration=60)
    options.update(kwargs)
    return PairingStream(**options)


class CommandTests(unittest.TestCase):
    def test_the_command_is_h264_in_mpegts_for_hls(self):
        recorder = Recorder()
        stream(recorder).playlist(CODE, URL, PAGE)
        argv = recorder.calls[0]
        self.assertEqual(argv[0], "ffmpeg")
        for pair in (
            ("-c:v", "libx264"),
            ("-profile:v", "main"),
            ("-pix_fmt", "yuv420p"),
            ("-tune", "stillimage"),
            ("-f", "hls"),
            ("-hls_time", "10"),
            ("-hls_playlist_type", "vod"),
            ("-hls_segment_filename", "seg%03d.ts"),
            ("-vf", "scale=1280:720,format=yuv420p"),
        ):
            self.assertIn(pair[0], argv)
            self.assertEqual(argv[argv.index(pair[0]) + 1], pair[1])
        self.assertEqual(argv[-1], "index.m3u8")
        # One keyframe per segment: a player joining late must not wait for a
        # GOP that never comes.
        self.assertEqual(argv[argv.index("-g") + 1], "50")
        self.assertEqual(argv[argv.index("-keyint_min") + 1], "50")
        self.assertEqual(argv[argv.index("-sc_threshold") + 1], "0")
        self.assertIn("-nostdin", argv)

    def test_the_card_is_written_before_the_encoder_runs(self):
        recorder = Recorder()
        stream(recorder).playlist(CODE, URL, PAGE)
        image = recorder.cards[0]
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", image[16:24]), (1280, 720))

    def test_a_temporary_directory_is_used_and_removed(self):
        recorder = Recorder()
        stream(recorder).playlist(CODE, URL, PAGE)
        self.assertTrue(recorder.workdirs[0].name.startswith("nlzietd-pair-"))
        self.assertFalse(recorder.workdirs[0].exists())


class PlaylistTests(unittest.TestCase):
    def test_segment_uris_point_at_this_daemons_route(self):
        body = stream().playlist(CODE, URL, PAGE).decode("utf-8")
        self.assertIn("stream/ABC123/seg000.ts", body)
        self.assertIn("stream/ABC123/seg001.ts", body)
        self.assertIn("#EXT-X-ENDLIST", body)
        self.assertIn("#EXT-X-TARGETDURATION:10", body)
        self.assertNotIn("\nseg000.ts\n", body)

    def test_the_rewrite_keeps_tags_and_only_touches_uris(self):
        self.assertEqual(
            _rewrite("#EXTM3U\n#EXTINF:10,\nseg000.ts\n", "ABC"),
            b"#EXTM3U\n#EXTINF:10,\nstream/ABC/seg000.ts\n",
        )

    def test_segments_are_served_from_the_cached_clip(self):
        svc = stream()
        svc.playlist(CODE, URL, PAGE)
        self.assertEqual(svc.segment(CODE, "seg000.ts"), b"TS ZERO")
        self.assertEqual(svc.segment(CODE, "seg001.ts"), b"TS ONE")
        self.assertIsNone(svc.segment(CODE, "seg999.ts"))
        self.assertIsNone(svc.segment("NOPE", "seg000.ts"))

    def test_a_clip_is_encoded_once_per_code(self):
        recorder = Recorder()
        svc = stream(recorder)
        first = svc.playlist(CODE, URL, PAGE)
        second = svc.playlist(CODE, URL, PAGE)
        self.assertEqual(first, second)
        self.assertEqual(len(recorder.calls), 1)
        svc.playlist("SECOND", URL, PAGE)
        self.assertEqual(len(recorder.calls), 2)
        self.assertIn("stream/SECOND/seg000.ts", svc.playlist("SECOND", URL, PAGE).decode())

    def test_the_cache_keeps_two_clips_so_an_expired_code_still_plays(self):
        recorder = Recorder()
        svc = stream(recorder, keep=2)
        svc.playlist("ONE", URL, PAGE)
        svc.playlist("TWO", URL, PAGE)
        svc.playlist("THREE", URL, PAGE)
        self.assertIsNone(svc.segment("ONE", "seg000.ts"))
        self.assertEqual(svc.segment("TWO", "seg000.ts"), b"TS ZERO")
        self.assertEqual(svc.segment("THREE", "seg000.ts"), b"TS ZERO")


class FailureTests(unittest.TestCase):
    def test_no_ffmpeg_means_no_stream_but_a_honest_answer(self):
        with mock.patch("nlzietd.web.stream.shutil.which", return_value=None):
            svc = PairingStream(ffmpeg=None, runner=Recorder())
        self.assertFalse(svc.available())
        with self.assertRaises(StreamUnavailable) as caught:
            svc.playlist(CODE, URL, PAGE)
        self.assertIn("ffmpeg", str(caught.exception))

    def test_an_explicit_path_wins_over_path_lookup(self):
        with mock.patch("nlzietd.web.stream.shutil.which", return_value="other") as which:
            svc = PairingStream(ffmpeg=r"C:\tools\ffmpeg.exe", runner=Recorder())
            self.assertTrue(svc.available())
            self.assertEqual(svc._ffmpeg, r"C:\tools\ffmpeg.exe")
            which.assert_not_called()

    def test_an_encoder_installed_after_startup_is_found_without_a_restart(self):
        found = [None]
        with mock.patch(
            "nlzietd.web.stream.shutil.which",
            side_effect=lambda _name: found[0],
        ):
            svc = PairingStream(ffmpeg=None, runner=Recorder())
            self.assertFalse(svc.available())
            found[0] = "ffmpeg"
            self.assertTrue(svc.available())
            self.assertIn(
                b"stream/ABC123/seg000.ts", svc.playlist(CODE, URL, PAGE)
            )

    def test_a_broken_encoder_degrades_to_the_picture(self):
        recorder = Recorder(fail=True)
        svc = stream(recorder)
        with self.assertRaises(StreamUnavailable) as caught:
            svc.playlist(CODE, URL, PAGE)
        self.assertIn("libx264", str(caught.exception))
        self.assertFalse(svc.available())

    def test_a_playlist_without_segments_is_not_a_stream(self):
        svc = stream(Recorder(with_segments=False))
        with self.assertRaises(StreamUnavailable):
            svc.playlist(CODE, URL, PAGE)
        self.assertFalse(svc.available())

    def test_a_failure_does_not_poison_a_later_code_forever(self):
        # Once broken, the service stays broken: retrying an encoder that just
        # failed on every request would hammer the machine. The playlist
        # switches back to the QR logo instead.
        recorder = Recorder(fail=True)
        svc = stream(recorder)
        with self.assertRaises(StreamUnavailable):
            svc.playlist(CODE, URL, PAGE)
        recorder.fail = False
        self.assertFalse(svc.available())
        self.assertEqual(len(recorder.calls), 1)


if __name__ == "__main__":
    unittest.main()
