"""Errors that cross a layer boundary.

They live in `domain` because they are part of the contract: an adapter raises
`ApiError(401)`, and a service is allowed to act on that 401. Defining them
here means no service ever needs to import an adapter to catch one - which is
the rule this module exists to keep.
"""

from __future__ import annotations


class ApiError(RuntimeError):
    """A provider said no. `status` is meaningful: 401 means "new token"."""

    def __init__(self, status: int, what: str, body: str = "") -> None:
        super().__init__(f"{what}: HTTP {status} {body[:120]}")
        self.status = status
        # Kept verbatim: an OAuth endpoint says *why* in the body
        # ("authorization_pending", "invalid_grant") and the layer above has to
        # be able to read that instead of pattern-matching a message.
        self.body = body


class LoginError(RuntimeError):
    """Authentication could not be completed."""


class NotAuthenticated(RuntimeError):
    """No session, and no way to mint one right now."""


class CatalogueError(RuntimeError):
    """Channel metadata is unavailable (offline with no cache, usually)."""


class UpstreamError(RuntimeError):
    """A relayed resource (CDN, licence proxy) could not be reached at all.

    Distinct from ApiError: ApiError means "that endpoint answered no", which
    we relay; UpstreamError means there was no answer, which is a 502 we
    produce ourselves. Collapsing the two would turn a dead CDN into an empty
    but successful response.
    """


class BadRequest(RuntimeError):
    """The caller asked for something that cannot be valid.

    Used by the transport layer for segment paths it refuses to relay (an
    absolute URL would turn this service into an open proxy), so the web layer
    can answer 400 instead of pretending the route does not exist.
    """
