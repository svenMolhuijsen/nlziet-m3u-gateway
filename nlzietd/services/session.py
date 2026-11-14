"""Token lifecycle: cache, validate, refresh, persist.

The interesting decisions:

* One lock, held across the whole obtain-refresh-persist sequence. Without it
  two concurrent requests both find an expired token and both hit
  /connect/token; NLZiet rotates refresh tokens, so the loser gets a dead one
  and the user is silently logged out. Cheap lock, expensive bug.

* Validation is a real API call (GET /v7/profile), not just a clock check.
  A token can be revoked while its `exp` is still in the future, and the one
  call that would fail later is the handshake - in the middle of playback.

* `invalidate()` exists because the API layer can tell us "401" but cannot
  know what to do about it. The retry policy lives one level up.
"""

from __future__ import annotations

import threading
import time

from ..domain.errors import NotAuthenticated
from ..domain.models import Tokens
from ..ports import SessionProvider, TokenStore


class SessionService:
    def __init__(
        self,
        store: TokenStore,
        provider: SessionProvider | None,
        skew: int,
        clock=time.time,
    ) -> None:
        self._store = store
        self._provider = provider
        self._skew = skew
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Tokens | None = None

    # -- public -------------------------------------------------------------

    def token(self, force: bool = False) -> str:
        """A usable access token, or NotAuthenticated."""
        with self._lock:
            current = self._cached or self._store.load()

            if current and not force and current.fresh(int(self._clock()), self._skew):
                self._cached = current
                return current.access

            if self._provider is None:
                raise NotAuthenticated(
                    "no session on file and no provider configured to renew one"
                )

            fresh = self._provider.obtain(current)
            self._store.save(fresh)
            self._cached = fresh
            return fresh.access

    def invalidate(self) -> None:
        """Forget the cached token so the next call re-obtains one.

        Deliberately does not clear the store: the refresh token is probably
        still good, and forcing a full login on every stray 401 is how you
        get rate-limited.
        """
        with self._lock:
            self._cached = None

    def status(self) -> dict:
        current = self._cached or self._store.load()
        # With only device pairing there is exactly one way to recover by
        # ourselves: a refresh token on disk. Without one, a human has to pair
        # again - so `can_relogin` is a statement about that token, not about
        # whether a provider object happens to be wired in.
        can_relogin = self._provider is not None and bool(
            current and current.refresh
        )
        if not current:
            return {"authenticated": False, "can_relogin": False}
        now = int(self._clock())
        left = current.expires_at - now
        # Never the token itself: this dict is served over HTTP.
        return {
            # A token that is about to die is not a session. Reporting
            # "authenticated" while the next request will 503 - or trigger a
            # login - is the kind of status that sends someone hunting a bug
            # that is not there.
            "authenticated": left > self._skew,
            # Always the absolute remainder, so a negative number is the honest
            # "expired N seconds ago" rather than a rounded zero.
            "expires_in": left,
            "has_refresh": bool(current.refresh),
            # Whether the daemon can renew silently. False means a new pairing
            # is needed; the page will offer one by itself.
            "can_relogin": can_relogin,
        }
