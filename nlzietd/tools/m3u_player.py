"""A local M3U test player, for the machine that runs the daemon.

Why this exists: the web page at `/` is a player for one channel, but it is not
an M3U player. To check that the *playlist* behaves the way a TV expects, you
need something that reads `#EXTINF`/`#KODIPROP` lines and acts on them. This
tool does that with nothing but the standard library.

It is deliberately honest about DRM. VLC and ffplay can play the pairing
channel (plain H.264/HLS), but no desktop player without a CDM can play the
real channels; rather than launching something that will fail with a cryptic
error, `--play` names the two clients that can (Kodi with
`inputstream.adaptive`, or a Widevine browser on localhost).

This is a development tool, not part of the daemon: nothing imports it, the
daemon never needs VLC or ffplay, and `--play` is the only path that looks for
one. Run it as `python -m nlzietd.tools.m3u_player`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_URL = "http://localhost:8099/playlist.m3u"
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


@dataclass(frozen=True)
class Entry:
    name: str
    url: str
    attrs: dict = field(default_factory=dict)
    props: dict = field(default_factory=dict)

    @property
    def drm(self) -> bool:
        """A KODIPROP licence type means the stream needs a Widevine CDM."""
        return any("license_type" in key for key in self.props)

    @property
    def kind(self) -> str:
        if self.drm:
            return "widevine"
        if ".m3u8" in self.url:
            return "hls"
        return "image" if ".png" in self.url else "stream"


def _extinf(line: str) -> tuple[str, dict]:
    body = line.split(":", 1)[-1].strip()
    head, _, rest = body.partition(" ")
    attrs = dict(_ATTR.findall(rest))
    # The display name follows the last comma. Attribute values may contain
    # commas inside quotes, so a plain split() on "," would cut them in half.
    comma = rest.rfind(",")
    name = rest[comma + 1:].strip() if comma >= 0 else head
    return name, attrs


def parse(text: str) -> list[Entry]:
    """The entries of an M3U, in the order a player would show them."""
    entries: list[Entry] = []
    pending: tuple[str, dict, dict] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name, attrs = _extinf(line)
            pending = (name, attrs, {})
        elif line.startswith("#KODIPROP:") and pending is not None:
            key, _, value = line[len("#KODIPROP:"):].partition("=")
            pending[2][key] = value
        elif not line.startswith("#") and pending is not None:
            name, attrs, props = pending
            entries.append(Entry(name=name, url=line, attrs=attrs, props=props))
            pending = None
    return entries


def check(entry: Entry, timeout: float = 10.0) -> str:
    """One line describing whether the entry's URL answers at all."""
    try:
        with urllib.request.urlopen(entry.url, timeout=timeout) as resp:
            sample = resp.read(256)
            return (
                f"{resp.status} {resp.headers.get('content-type', '?')} "
                f"({len(sample)} B read)"
            )
    except urllib.error.HTTPError as err:
        return f"{err.code} {err.reason}"
    except Exception as err:  # noqa: BLE001 - an unreachable daemon is the answer
        return f"unreachable: {err}"


def find_player() -> str | None:
    """VLC first (it knows M3U), then ffplay, then ffplay beside ffmpeg."""
    for name in ("vlc", "ffplay"):
        found = shutil.which(name)
        if found:
            return found
    explicit = os.environ.get("NLZIETD_FFMPEG")
    if explicit:
        sibling = Path(explicit).with_name("ffplay" + Path(explicit).suffix)
        if sibling.exists():
            return str(sibling)
    return None


def play(entry: Entry, base: str) -> int:
    if entry.drm:
        print("This channel is Widevine-protected; VLC/ffplay have no CDM.")
        print("Use Kodi (inputstream.adaptive) with the playlist, or open")
        print(f"  {base}/  in Chrome/Edge (localhost is a secure context).")
        return 1
    player = find_player()
    if player is None:
        print("no local player found: install VLC, or ffmpeg (for ffplay).")
        return 1
    args = [player, entry.url]
    if Path(player).stem == "ffplay":
        args = [player, "-hide_banner", "-loglevel", "error", entry.url]
    print(f"launching {Path(player).name}: {entry.url}", flush=True)
    subprocess.Popen(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="playlist URL")
    parser.add_argument("--file", help="read a local .m3u instead of --url")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="list entries (default)")
    action.add_argument("--check", action="store_true", help="check every URL")
    action.add_argument("--play", type=int, metavar="N", help="play entry N")
    options = parser.parse_args(argv)

    if options.file:
        text = Path(options.file).read_text(encoding="utf-8")
    else:
        try:
            with urllib.request.urlopen(options.url, timeout=10) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as err:  # noqa: BLE001
            print(f"cannot read {options.url}: {err}", file=sys.stderr)
            return 1

    entries = parse(text)
    if not entries:
        print("playlist has no entries", file=sys.stderr)
        return 1
    base = options.url.rsplit("/playlist.m3u", 1)[0]

    if options.check:
        for index, entry in enumerate(entries, 1):
            print(f"{index:2}. {entry.name}\n    {check(entry)}")
        return 0
    if options.play is not None:
        if not 1 <= options.play <= len(entries):
            print(f"no entry {options.play} (1..{len(entries)})", file=sys.stderr)
            return 1
        return play(entries[options.play - 1], base)

    for index, entry in enumerate(entries, 1):
        print(f"{index:2}. [{entry.kind:8}] {entry.name}\n    {entry.url}")
    print(f"\n{len(entries)} entry/entries. Use --check or --play N.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
