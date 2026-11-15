"""A tiny threaded HTTP server, stdlib only.

Why not Flask/FastAPI: this process serves one household. Its hot path is
segment relaying, which is byte shovelling, not routing. A framework would add
a dependency and an install step on a box whose only job is to stay running -
so `http.server` with a tiny router is the right size.

The router is explicit rather than decorator-based: one place lists every
route, which is what you want when auditing what this thing exposes on your
LAN.

Why errors are mapped here and not in each handler: a route should read as
"what it does", and every route fails in the same handful of ways (unknown
channel, no session, upstream said no, path we refuse to relay). Mapping the
domain errors to statuses once means a new route gets correct failure
behaviour without remembering to copy it.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .. import __version__
from ..domain.errors import (
    ApiError,
    BadRequest,
    LoginError,
    NotAuthenticated,
    UpstreamError,
)
from ..domain.models import Reply
from ..ports import Catalogue
from ..services.playback import PlaybackService
from ..services.profile import ProfileSessions
from ..services.session import SessionService
from ..services.transport import TransportService
from .player import (
    pairing_playlist,
    player_page,
    playlist,
    setup_page,
    wants_android_flavor,
)
from .qr import png as qr_png
from .stream import PairingStream, StreamUnavailable

Handler = Callable[["Request", re.Match], Reply]

# A Widevine challenge is a few KB; a manifest is smaller. Refusing anything
# past this keeps a LAN neighbour from making the daemon allocate arbitrarily
# much memory with one POST.
MAX_BODY = 1 << 20


@dataclass(frozen=True)
class Request:
    """What a handler is allowed to see about one HTTP request.

    `raw_path` keeps the query string exactly as the player sent it. It is
    needed for segments: the `hdntl` token lives in the query, and
    re-serialising the parsed query would re-encode characters the CDN is
    strict about.
    """

    method: str
    path: str
    raw_path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    # Whether this request arrived over TLS. It is a fact about the socket,
    # not something the client can claim, so it is set by the server.
    secure: bool = False

    @property
    def host(self) -> str:
        """The authority the player used, so we can send it back to itself."""
        return self.headers.get("host") or "localhost"

    @property
    def user_agent(self) -> str:
        """The client's User-Agent, lowercased. Used only to pick the licence
        flavor when the URL does not say; never trusted for anything else."""
        return self.headers.get("user-agent", "").lower()

    @property
    def scheme(self) -> str:
        # The scheme the player used, because every URL we hand back has to stay
        # on it: an https page that receives an http:// segment URL is blocked
        # as mixed content, and a browser only exposes EME on https. So believe
        # our own socket first, then the header a terminating proxy sets.
        if self.secure or self.headers.get("x-forwarded-proto") == "https":
            return "https"
        return "http"


def _json(payload, status: int = 200) -> Reply:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Reply(status, {"content-type": "application/json; charset=utf-8"}, body)


def _error(status: int, message: str, **extra) -> Reply:
    payload = {"error": message}
    payload.update(extra)
    return _json(payload, status)


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append((method, re.compile(f"^{pattern}$"), handler))

    def dispatch(self, request: Request) -> Reply:
        # Some players probe a stream with HEAD before they GET it. It is the
        # same resource, so route it as a GET; `_respond` sends the headers and
        # the length, but no body. A HEAD on a POST-only route stays a 404.
        method = "GET" if request.method == "HEAD" else request.method
        for route_method, pattern, handler in self._routes:
            match = pattern.match(request.path)
            if not match or route_method != method:
                continue
            try:
                return handler(request, match)
            except KeyError as err:
                return _error(404, "unknown channel", channel=str(err).strip("'"))
            except BadRequest as err:
                return _error(400, str(err))
            except (NotAuthenticated, LoginError) as err:
                return _error(503, "not authenticated", detail=str(err))
            except ApiError as err:
                # The upstream said no; the number tells the player (and me)
                # whether it was auth, entitlement or a broken endpoint.
                return _error(
                    502, "upstream refused", upstream_status=err.status,
                    detail=str(err),
                )
            except UpstreamError as err:
                return _error(502, "upstream unreachable", detail=str(err))
        return _error(404, "not found", path=request.path)


def build_router(
    catalogue: Catalogue,
    playback: PlaybackService,
    session: SessionService,
    profiles: ProfileSessions,
    transport: TransportService,
    debug: bool,
    tls_port: int | None = None,
    pairing=None,
    stream: PairingStream | None = None,
) -> Router:
    router = Router()

    def health(_request, _match):
        return _json({"ok": True})

    def status(_request, _match):
        try:
            count = len(catalogue.channels())
        except Exception:  # noqa: BLE001
            # A status endpoint that goes blank because one part of it failed
            # is useless exactly when it is needed. The session fields are what
            # decide whether the pairing screen appears, and those never need
            # the catalogue.
            count = None
        return _json({
            "session": session.status(),
            "profile": profiles.status(),
            "channels": count,
            # Advertised so a player page opened over http can tell the user
            # which https URL to use instead: EME exists only in a secure
            # context, and a browser does not know our other listener exists.
            "https_port": tls_port,
        })

    def channels(_request, _match):
        rows = [
            {
                "id": c.id,
                "name": c.name,
                "number": c.number,
                "logo": c.logo,
                "replay": c.replay,
            }
            for c in catalogue.channels().values()
        ]
        return _json(rows)

    def ticket(_request, match):
        if not debug:
            return _error(404, "debug routes disabled")
        t = playback.ticket(match.group("id"))
        return _json({
            "channel": t.channel,
            "title": t.title,
            "content": t.content_id,
            "session": t.session_id,
            "manifest": t.manifest_url,
            # The licence URL carries a capability token; showing the host is
            # enough to debug which proxy answered.
            "licence_host": urllib.parse.urlparse(t.licence_url).netloc,
            # Named, not valued: this endpoint is a debugging aid, and a
            # browser tab is not the place for a bearer header.
            "header_names": sorted(t.headers),
        })

    def manifest(request, match):
        channel_id = match.group("id")
        # Trailing slash matters: it makes every relative segment resolve
        # against this directory instead of replacing the last path part.
        base = f"{request.scheme}://{request.host}/seg/{channel_id}/"
        return transport.manifest(channel_id, base)

    def segment(request, match):
        channel_id = match.group("id")
        prefix = f"/seg/{channel_id}/"
        # Slice the raw path rather than re-encoding the parsed one: the hdntl
        # query token must reach the CDN byte-for-byte as the manifest had it.
        rest = request.raw_path[len(prefix):] if request.raw_path.startswith(prefix) else ""
        return transport.segment(channel_id, rest, request.headers.get("range"))

    def licence(request, match):
        return transport.licence(
            match.group("id"),
            request.body,
            request.headers.get("content-type", "application/octet-stream"),
        )

    def home(_request, _match):
        # HTML lives in the web layer; this handler only sets the type.
        return Reply(
            200,
            {"content-type": "text/html; charset=utf-8"},
            player_page().encode("utf-8"),
        )

    def setup(_request, _match):
        # Standalone "connect your player" page: live links + the app table.
        return Reply(
            200,
            {"content-type": "text/html; charset=utf-8"},
            setup_page().encode("utf-8"),
        )

    def pair_view(_request, _match):
        if pairing is None:
            return _error(404, "pairing not available")
        return _json(pairing.view())

    def pair_start(_request, _match):
        if pairing is None:
            return _error(404, "pairing not available")
        # Synchronous on purpose: minting a code is one POST, and the panel
        # wants the code in the same response that asks for it. The waiting is
        # the worker's job, not this request's.
        return _json(pairing.start())

    def pair_qr(_request, _match):
        """The code as an image, because a playlist cannot draw text.

        `tvg-logo` is the only place an M3U player shows a picture, so this is
        how a QR reaches a TV that is not running our page.
        """
        if pairing is None:
            return _error(404, "pairing not available")
        view = pairing.view()
        if view["state"] != "pending" or not view["url"]:
            # A code that has expired is not worth encoding: it would scan into
            # a dead end, which is worse than a missing picture.
            return _error(404, "no pairing code on screen", state=view["state"])
        return Reply(
            200,
            {
                "content-type": "image/png",
                # The image is the current code and nothing else.
                "cache-control": "no-store",
            },
            qr_png(view["url"]),
        )

    def m3u(request, _match):
        base = f"{request.scheme}://{request.host}"
        if pairing is not None and not session.status().get("authenticated"):
            # An M3U player can show a channel name, a logo and a stream, so
            # when there is no session the playlist becomes the pairing screen:
            # opening it mints a code, the name carries it, the logo is its QR,
            # and the entry plays a video of the same thing (when ffmpeg is
            # there to encode it).
            view = pairing.start()
            return Reply(
                200,
                {"content-type": "audio/x-mpegurl; charset=utf-8"},
                pairing_playlist(
                    base, view, stream=bool(stream and stream.available())
                ).encode("utf-8"),
            )
        # Which licence-key shape: Kodi's four-field `||R{SSM}|R` wrapper, or
        # the plain URL Android MediaDrm players (OTT Navigator, Prysm) want.
        # An explicit `?android=1`/`?android=0` (or `?flavor=android`) always
        # wins; otherwise we read the User-Agent so the right flavor is served
        # with no query string at all.
        query = request.query
        if "android" in query or "flavor" in query:
            android = (
                query.get("android", [""])[0] not in ("", "0")
                or query.get("flavor", [""])[0] == "android"
            )
        else:
            android = wants_android_flavor(request.user_agent)
        text = playlist(base, catalogue.channels(), android=android)
        return Reply(
            200,
            {"content-type": "audio/x-mpegurl; charset=utf-8"},
            text.encode("utf-8"),
        )

    def pair_stream(_request, _match):
        """The pairing entry as a channel: H.264 in HLS, no audio, no DRM.

        This is what a player that cannot draw anything but a video gets. The
        code it shows is whatever is on screen now; if nothing is, one is
        minted, so pressing the channel again after an hour gives a live code
        rather than a frozen one that has expired.
        """
        if pairing is None or stream is None or not stream.available():
            return _error(404, "no pairing stream")
        view = pairing.view()
        if view["state"] != "pending":
            view = pairing.start()
        if view["state"] != "pending" or not view["code"]:
            return _error(
                503, "no pairing code",
                detail=view.get("detail") or view["state"],
            )
        try:
            body = stream.playlist(view["code"], view["url"], view["page"])
        except StreamUnavailable as err:
            return _error(503, "pairing stream unavailable", detail=str(err))
        return Reply(
            200,
            {
                "content-type": "application/vnd.apple.mpegurl",
                # The playlist names the current code's segments; a cached copy
                # would keep pointing at a clip for a code that is gone.
                "cache-control": "no-store",
            },
            body,
        )

    def pair_segment(_request, match):
        if stream is None:
            return _error(404, "no pairing stream")
        data = stream.segment(match.group("code"), match.group("name"))
        if data is None:
            return _error(404, "no such segment")
        return Reply(
            200,
            # MPEG-TS, because that is what an HLS player expects for H.264.
            {"content-type": "video/mp2t", "cache-control": "no-store"},
            data,
        )

    router.add("GET", r"/", home)
    router.add("GET", r"/setup", setup)
    router.add("GET", r"/healthz", health)
    router.add("GET", r"/api/status", status)
    router.add("GET", r"/api/channels", channels)
    router.add("GET", r"/api/pair", pair_view)
    router.add("POST", r"/api/pair", pair_start)
    router.add("GET", r"/pair/qr\.png", pair_qr)
    router.add("GET", r"/pair/stream\.m3u8", pair_stream)
    router.add(
        "GET", r"/pair/stream/(?P<code>[A-Za-z0-9]+)/(?P<name>seg\d+\.ts)",
        pair_segment,
    )
    router.add("GET", r"/api/ticket/(?P<id>[a-z0-9_]+)", ticket)
    router.add("GET", r"/manifest/(?P<id>[a-z0-9_]+)\.mpd", manifest)
    router.add("GET", r"/seg/(?P<id>[a-z0-9_]+)/(?P<rest>.+)", segment)
    router.add("GET", r"/playlist\.m3u", m3u)
    router.add("POST", r"/licence/(?P<id>[a-z0-9_]+)", licence)
    return router


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    """A TLS context for the https listener.

    Why this is opt-in and separate from the http listener: Kodi's
    inputstream.adaptive verifies certificates against its own bundle, so a
    self-signed certificate would break a working Kodi setup, while a browser
    refuses to expose EME without https. One process, two listeners, each
    client picks the one it can handle.

    Verification stays on (no CERT_NONE anywhere): the daemon holds the user's
    session, so a client that cannot verify us should not be talking to us.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # TLS 1.2 is the floor: Widevine licence POSTs carry a capability token.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def make_server(
    host: str,
    port: int,
    router: Router,
    tls: ssl.SSLContext | None = None,
    log: bool = False,
) -> ThreadingHTTPServer:
    class _Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address) -> None:
            # Players drop connections all the time: a seek abandons a segment,
            # a tab closes mid-download, a licence POST is retried. That is
            # normal traffic, not a fault to spill a traceback over on a daemon
            # that runs unattended for days.
            if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
                return
            super().handle_error(request, client_address)

    class _Handler(BaseHTTPRequestHandler):
        server_version = f"nlzietd/{__version__}"
        protocol_version = "HTTP/1.1"  # keep-alive: players reuse connections
        # Read by every Request this handler builds: see Request.scheme.
        secure = tls is not None

        def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            self._respond("GET")

        def do_HEAD(self):  # noqa: N802 - some players probe before they read
            self._respond("HEAD")

        def do_POST(self):  # noqa: N802
            self._respond("POST")

        def _request(self, method: str) -> Request:
            split = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY:
                # Read and discard nothing; answering and closing is enough.
                raise ValueError("request body too large")
            return Request(
                method=method,
                path=split.path,
                raw_path=self.path,
                query=urllib.parse.parse_qs(split.query, keep_blank_values=True),
                headers={k.lower(): v for k, v in self.headers.items()},
                body=self.rfile.read(length) if length else b"",
                secure=self.secure,
            )

        def _respond(self, method: str) -> None:
            try:
                reply = router.dispatch(self._request(method))
            except ValueError as err:
                # We refused to read the body, so the rest of it is still in
                # the socket; keep-alive would read it as the next request.
                self.close_connection = True
                reply = _error(413, str(err))
            if log:
                # Path without the hdntl token: it is a capability that is
                # worth keeping out of scrollback and out of any log that gets
                # pasted into an issue.
                path = self.path.split("?", 1)[0]
                print(
                    f"{method} {path} -> {reply.status} ({len(reply.body)} B)",
                    flush=True,
                )
            self.send_response(reply.status)
            for key, value in reply.headers.items():
                self.send_header(key, value)
            # Always our own length: after a manifest rewrite the upstream one
            # would be a lie, and with keep-alive a wrong length hangs the
            # player instead of failing it.
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(reply.body)

        def log_message(self, fmt, *args):  # quieter than the default
            return

    server = _Server((host, port), _Handler)
    if tls is not None:
        # Wrap the listening socket rather than each connection: accept() then
        # hands out SSL sockets, and a client whose handshake fails breaks
        # only its own handler thread, not the accept loop.
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    return server
