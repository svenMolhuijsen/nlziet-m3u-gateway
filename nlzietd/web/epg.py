"""The programme guide, as XMLTV.

Why this is a service and not an adapter: the shape of the output (XMLTV) and
the decision to assemble it channel-by-channel across several days are domain
choices, while the one HTTP call it leans on (`NlzietApi.epg`) is already an
adapter. The split follows the rest of the daemon - the adapter knows one
endpoint, the service knows what a guide *is*.

Why it caches the whole document rather than per-channel rows: every player
asks for `/epg.xml` as one file, refreshes it a few times a day, and never
asks for one channel. The guide for a given day changes slowly (a schedule
edit, not a per-second manifest), so one cached document with a lifetime of
hours spares the account ~38 calls per day per refresh. The cache is keyed by
nothing but time: there is only ever one guide.

Why a channel that fails is skipped, not fatal: a 38-channel guide that goes
blank because one channel's listings 500'd is worse than a 37-channel guide.
The builder logs the gap (to stdout, like the rest) and carries on; a player
shows "no information" for that one channel, which is the honest result.

Nothing here is secret: the guide is titles and times, the same thing printed
in a TV magazine. The user token reaches it only to satisfy the API.
"""

from __future__ import annotations

import datetime
import html
import threading
import time

from ..domain.models import Channel, EpgEntry
from ..ports import Catalogue, NlzietApi
from ..services.session import SessionService

# The guide changes on the scale of a schedule edit, not a stream poll. Two
# hours means a player that refreshes a few times a day almost always hits the
# cache, while a same-day programme change still appears within one TV-evening.
DOC_TTL = 2 * 3600

# How far ahead to build. Three days is what a player's guide view can show
# without the file growing unwieldy (38 channels * ~60 programmes * 3 days is
# a few hundred KB of XML, gzip-friendly and well within a TV's appetite).
DEFAULT_DAYS = 3


def _xmltv_time(iso: str) -> str:
    """ISO-8601-with-offset -> XMLTV's `YYYYMMDDHHMMSS +0100`.

    The API already hands back an offset (`2026-11-16T00:51:58+01:00`), so this
    is a reformat, not a timezone computation. A value we cannot parse becomes
    an empty string, and the caller drops a programme with no start rather than
    emitting a malformed `start=` a player would choke on.
    """
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S %z")


class EpgService:
    def __init__(
        self,
        api: NlzietApi,
        catalogue: Catalogue,
        session: SessionService,
        days: int = DEFAULT_DAYS,
        clock=time.time,
    ) -> None:
        self._api = api
        self._catalogue = catalogue
        self._session = session
        self._days = days
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: tuple[float, str] | None = None

    def xmltv(self, force: bool = False) -> str:
        """The full guide as an XMLTV document, cached for `DOC_TTL`.

        One lock around the build so that two players asking at once do not
        both fan out 38*N calls; the loser waits and gets the winner's result.
        """
        with self._lock:
            now = self._clock()
            if not force and self._cache and self._cache[0] + DOC_TTL > now:
                return self._cache[1]
            doc = self._build()
            self._cache = (now, doc)
            return doc

    # -- internals ----------------------------------------------------------

    def _dates(self) -> list[str]:
        today = datetime.date.today()
        return [(today + datetime.timedelta(days=d)).isoformat()
                for d in range(self._days)]

    def _build(self) -> str:
        channels = self._catalogue.channels()
        access = self._session.token()
        dates = self._dates()

        parts: list[str] = []
        parts.append('<?xml version="1.0" encoding="UTF-8"?>')
        parts.append('<tv generator-info-name="nlzietd">')

        # Channel declarations first, as XMLTV wants: id must equal the M3U
        # tvg-id (the catalogue key) so a player matches programmes to rows.
        for cid, ch in channels.items():
            parts.append(f'  <channel id="{html.escape(cid)}">')
            parts.append(f'    <display-name>{html.escape(ch.name)}</display-name>')
            if ch.logo:
                parts.append(f'    <icon src="{html.escape(ch.logo)}" />')
            parts.append('  </channel>')

        # Then every programme. De-duplicate on (channel, start): consecutive
        # days overlap at midnight and the API returns the boundary programme
        # in both days' responses.
        seen: set[tuple[str, str]] = set()
        for cid, ch in channels.items():
            for date in dates:
                try:
                    entries = self._api.epg(access, cid, date)
                except Exception as err:  # noqa: BLE001 - one channel/day gap, not fatal
                    print(f"epg: {cid} {date} skipped: {err}", flush=True)
                    continue
                for e in entries:
                    key = (cid, e.start)
                    if key in seen:
                        continue
                    seen.add(key)
                    parts.append(self._programme(cid, e))

        parts.append('</tv>')
        return "\n".join(parts)

    @staticmethod
    def _programme(cid: str, e: EpgEntry) -> str:
        start = _xmltv_time(e.start)
        stop = _xmltv_time(e.end)
        if not start:
            return ""  # unusable without a start; a player skips a stop-less one too
        title = html.escape(e.title or "")
        line = [f'  <programme start="{start}"'
                + (f' stop="{stop}"' if stop else "")
                + f' channel="{html.escape(cid)}">']
        line.append(f'    <title>{title}</title>')
        if e.subtitle:
            line.append(f'    <sub-title>{html.escape(e.subtitle)}</sub-title>')
        if e.description:
            line.append(f'    <desc>{html.escape(e.description)}</desc>')
        if e.is_movie:
            line.append('    <category>Movie</category>')
        line.append('  </programme>')
        return "\n".join(line)
