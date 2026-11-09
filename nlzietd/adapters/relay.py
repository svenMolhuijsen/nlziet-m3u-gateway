"""A pass-through HTTP client, by design dumb.

This adapter does not parse what it fetches: segments are bytes, manifests are
bytes until the transport service edits one, licences are bytes in both
directions. That is why it returns a `Reply` instead of raising on 4xx - the
player asked, the CDN answered 404, and the player should be told 404 rather
than receive an invented error from us.

The only intelligence here is header hygiene: hop-by-hop headers must not be
forwarded (they describe *our* connection to the CDN, not the player's), and
Content-Length is dropped because the web layer writes the length of the body
it actually has after rewriting a manifest.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from ..domain.errors import UpstreamError
from ..domain.models import Reply
from ..ports import Relay

# Connection-scoped headers. Forwarding these makes the player and the CDN
# disagree about framing in ways that show up as truncated media.
#
# `date` and `server` are dropped too: the web layer writes its own, and two
# Date headers on one response is the kind of thing a strict HTTP client
# rejects outright.
_DROP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}) | {"content-length", "set-cookie", "date", "server"}


class HttpRelay(Relay):
    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

    # -- port ---------------------------------------------------------------

    def get(self, url: str, headers: dict | None = None) -> Reply:
        return self._send("GET", url, headers or {}, None)

    def post(self, url: str, body: bytes, headers: dict | None = None) -> Reply:
        return self._send("POST", url, headers or {}, body)

    # -- internals ----------------------------------------------------------

    def _send(self, method: str, url: str, headers: dict, body: bytes | None) -> Reply:
        request_headers = dict(headers)
        # Identity: urllib does not decompress, so asking for anything else
        # would hand the player a gzip body labelled as plain bytes.
        request_headers.setdefault("Accept-Encoding", "identity")
        # Cloudflare in front of api.nlziet.nl answers a request with urllib's
        # default User-Agent with "error code: 1010" and no body. The licence
        # proxy needs a UA that exists; the app identifiers are enough, no
        # browser impersonation required.
        request_headers.setdefault("User-Agent", "nlzietd/0.2")
        req = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return self._reply(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as err:
            # An answered "no" is data: keep the status and the body (the
            # licence proxy explains itself in JSON, for instance).
            return self._reply(err.code, err.headers, err.read())
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise UpstreamError(f"{method} {url.split('?')[0]}: {err}")

    @staticmethod
    def _reply(status: int, headers, body: bytes) -> Reply:
        clean = {
            key.lower(): value
            for key, value in headers.items()
            if key.lower() not in _DROP
        }
        return Reply(status=status, headers=clean, body=body)
