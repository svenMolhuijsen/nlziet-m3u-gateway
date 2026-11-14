"""The pairing channel, as a video every IPTV player can read.

A `tvg-logo` is a thumbnail in a list, not a screen. When the only thing in the
room is a TV with a player on it, the pairing entry has to be a channel you can
press play on and then look at - so the daemon encodes the card into a real
HLS stream and hands the player a normal `#EXTINF` pointing at it.

Why H.264 in MPEG-TS over HLS: that is the intersection of what players
actually implement, including a set-top box that has not been updated in years.
DASH and fragmented MP4 need a smarter player; this does not.

Why ffmpeg and not Python: encoding H.264 in a standard-library daemon is not
a thing, and shipping an encoder would be absurd. So ffmpeg is optional. When
it is not installed the playlist falls back to the QR as `tvg-logo` - a worse
screen, but the code is still there to type, and nothing pretends to work.

The clip is a still image, so it is encoded once per code and cached. Ten
minutes of 5 fps video is a few megabytes and about a second of CPU for a
still frame; the code itself only lives five minutes, which is why the clip
outlasts it and a player that keeps the channel open does not run into the end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .card import pairing_card


class StreamUnavailable(Exception):
    """No encoder, or it refused to produce a clip."""


def _tail(data: bytes, limit: int = 400) -> str:
    text = data.decode("utf-8", "replace").strip()
    return text[-limit:] if len(text) > limit else text


def _run(argv: list[str], cwd: Path, timeout: float) -> None:
    """Run ffmpeg in `cwd`; raise with its last words when it fails."""
    # CREATE_NO_WINDOW: the daemon usually runs hidden, and a console window
    # flashing on a living-room PC every time somebody pairs is a bug report.
    flags = 0x08000000 if os.name == "nt" else 0
    result = subprocess.run(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=flags,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {_tail(result.stderr)}")


class PairingStream:
    def __init__(
        self,
        ffmpeg: str | None = None,
        runner: Callable[[list[str], Path, float], None] | None = None,
        duration: int = 600,
        fps: int = 5,
        segment: int = 10,
        width: int = 1280,
        height: int = 720,
        keep: int = 2,
        timeout: float = 180,
    ) -> None:
        # An explicit path first, because a service manager's PATH is not a
        # login shell's PATH; then whatever is on PATH. Resolved once: if it is
        # missing at start-up it will not appear halfway through a pairing.
        self._ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self._explicit = bool(ffmpeg)
        self._runner = runner or _run
        self._duration = duration
        self._fps = fps
        self._segment = segment
        self._width = width
        self._height = height
        self._keep = keep
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[bytes, dict[str, bytes]]] = {}
        # Set when an encode fails: a broken encoder must not make every
        # playlist request wait for the same failure. Once it is set the
        # playlist quietly goes back to the logo, which at least draws.
        self._broken = ""

    # -- public -------------------------------------------------------------

    def _resolve(self) -> None:
        """Look again for an encoder that appeared after the daemon started.

        Installing ffmpeg on a Windows service means editing PATH, which the
        running process never sees. Checking on each `available()` makes a
        restart unnecessary; an explicit NLZIETD_FFMPEG path is never replaced.
        """
        if not self._ffmpeg and not self._explicit:
            self._ffmpeg = shutil.which("ffmpeg")

    def available(self) -> bool:
        self._resolve()
        return bool(self._ffmpeg) and not self._broken

    def playlist(self, code: str, url: str, page: str) -> bytes:
        """The HLS playlist for `code`, encoded now if this is the first ask.

        Synchronous on purpose: the alternative is a placeholder stream that
        plays nothing, and a player has no way to tell the user to wait. One
        request blocks for a second, then every player on the network is
        served from cache.
        """
        return self.play(code, url, page)[0]

    def play(self, code: str, url: str, page: str) -> tuple[bytes, dict[str, bytes]]:
        with self._lock:
            self._resolve()
            cached = self._cache.get(code)
            if cached is not None:
                return cached
            if not self._ffmpeg:
                raise StreamUnavailable(
                    "ffmpeg is not installed; the pairing entry falls back to "
                    "the QR image"
                )
            try:
                built = self._build(code, url, page)
            except Exception as err:  # noqa: BLE001 - any failure is the same to us
                self._broken = str(err)
                raise StreamUnavailable(str(err)) from err
            self._cache[code] = built
            while len(self._cache) > self._keep:
                self._cache.pop(next(iter(self._cache)))
            return built

    def segment(self, code: str, name: str) -> bytes | None:
        """One segment of a clip that was built before; None if we have none.

        Never encodes: a segment request is a player in the middle of
        play-out, and making it wait for ffmpeg would be a stall it cannot
        explain. A miss is a 404, which players retry or skip.
        """
        with self._lock:
            cached = self._cache.get(code)
            if cached is None:
                return None
            return cached[1].get(name)

    # -- internals ----------------------------------------------------------

    def _build(self, code: str, url: str, page: str) -> tuple[bytes, dict[str, bytes]]:
        card = pairing_card(url, code, page, self._width, self._height)
        gop = self._fps * self._segment
        argv = [
            self._ffmpeg or "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            # One still image, read at the output rate: no scaler surprises,
            # no audio, and the loop makes it as long as the clip needs to be.
            "-loop", "1", "-framerate", str(self._fps), "-i", "card.png",
            "-t", str(self._duration),
            "-vf", f"scale={self._width}:{self._height},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
            "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
            "-r", str(self._fps),
            # A keyframe on every segment boundary: without it a player that
            # joins late waits for a keyframe it may never see.
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-f", "hls",
            "-hls_time", str(self._segment),
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", "seg%03d.ts",
            "index.m3u8",
        ]
        with tempfile.TemporaryDirectory(prefix="nlzietd-pair-") as tmp:
            work = Path(tmp)
            (work / "card.png").write_bytes(card)
            self._runner(argv, work, self._timeout)
            text = (work / "index.m3u8").read_text(encoding="utf-8")
            segments = {
                path.name: path.read_bytes()
                for path in sorted(work.glob("seg*.ts"))
            }
            if not segments:
                raise RuntimeError("ffmpeg wrote a playlist but no segments")
            return _rewrite(text, code), segments


def _rewrite(playlist: str, code: str) -> bytes:
    """Point ffmpeg's relative segment names at this daemon's route.

    They stay relative to `/pair/stream.m3u8`, and the code is in the path so
    a segment can only ever belong to the clip that listed it: a player that
    has half a clip from the previous code keeps getting bytes, while a new
    pairing gets a new directory instead of a mix.
    """
    lines = []
    for line in playlist.splitlines():
        if not line or line.startswith("#"):
            lines.append(line)
            continue
        lines.append(f"stream/{code}/{line.strip()}")
    return ("\n".join(lines) + "\n").encode("utf-8")
