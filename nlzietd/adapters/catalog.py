"""Channel metadata.

Why not api.nlziet.nl: the API exposes EPG *locations* keyed by an asset id,
but not a channel list with names and logos. The Kodi add-on solves that by
reading a static `channels.json` published by the DUT project, which is also
where the asset-id mapping comes from. Using it keeps this adapter honest -
it is explicitly "somebody else's catalogue", and the TTL says so.

Why caching lives here rather than in a service: it is a property of *this
source* (a static file that changes rarely), not of the domain. A different
catalogue implementation would cache differently or not at all.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from ..domain.errors import UpstreamError
from ..domain.models import Channel
from ..ports import Catalogue

CACHE_TTL = 7 * 24 * 3600  # the file is a channel map; weekly is plenty


class DutCatalogue(Catalogue):
    def __init__(self, url: str, cache_path: Path, timeout: int = 20) -> None:
        self._url = url
        self._cache = cache_path
        self._timeout = timeout
        self._memory: dict[str, Channel] | None = None

    def channels(self) -> dict[str, Channel]:
        if self._memory is None:
            self._memory = self._parse(self._raw())
        return self._memory

    # -- fetching -----------------------------------------------------------

    def _raw(self) -> list | dict:
        if self._cache.exists():
            age = time.time() - self._cache.stat().st_mtime
            if age < CACHE_TTL:
                try:
                    return json.loads(self._cache.read_text(encoding="utf-8"))
                except ValueError:
                    pass  # corrupt cache is just a cache miss

        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as err:
            # Offline with a stale cache is better than no channels at all.
            if self._cache.exists():
                try:
                    return json.loads(self._cache.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            # A transport failure is an upstream failure, not a crash: the
            # router turns this into a 502 with a message, and a fresh install
            # with no internet still reaches its pairing screen.
            raise UpstreamError(f"channel catalogue unreachable: {err}") from err

        self._cache.parent.mkdir(parents=True, exist_ok=True)
        self._cache.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    @staticmethod
    def _parse(payload: list | dict) -> dict[str, Channel]:
        rows = payload.values() if isinstance(payload, dict) else payload
        out: dict[str, Channel] = {}
        for row in rows:
            try:
                channel = Channel(
                    id=str(row["id"]),
                    # assetid is what the EPG endpoint wants; usually equal to
                    # id, but not something to assume.
                    asset_id=str(row.get("assetid") or row["id"]),
                    name=str(row["name"]),
                    number=int(row.get("channelno") or 0),
                    logo=str(row.get("icon") or ""),
                    replay=str(row.get("replay", "0")) == "1",
                )
            except (KeyError, TypeError, ValueError):
                continue  # one malformed row must not cost us the channel list
            out[channel.id] = channel
        return dict(sorted(out.items(), key=lambda kv: kv[1].number))
