"""Profile-scoped tokens: the second half of NLZiet's auth.

Why this is its own service and not three lines inside playback: the token is
a session of its own with its own lifetime (~8h), its own failure mode (the
identity server throttles bursts) and its own cache key (profile + the user
token it was derived from). Putting it next to the handshake would mean
re-minting a token on every cache miss of the ticket, which is exactly the
burst that gets you a 403.

Why the profile id is resolved lazily: `/v7/profile` needs a working user
token, so it cannot happen at start-up when there may be none. The list is
account metadata and does not change between requests, so resolving once and
remembering is enough - and `NLZIET_PROFILE` skips it entirely for a household
that wants a specific profile rather than the first one.
"""

from __future__ import annotations

import threading
import time

from ..domain.errors import LoginError
from ..domain.models import Tokens
from ..ports import NlzietApi, ProfileTokens


class ProfileSessions:
    def __init__(
        self,
        api: NlzietApi,
        tokens: ProfileTokens,
        profile_id: str | None,
        skew: int,
        clock=time.time,
    ) -> None:
        self._api = api
        self._tokens = tokens
        self._profile_id = profile_id or None
        self._skew = skew
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Tokens | None = None
        self._parent: str | None = None

    # -- public -------------------------------------------------------------

    def token(self, access_token: str, force: bool = False) -> str:
        """A profile token derived from `access_token`.

        `force` is the 401 path: the cached profile token is presumed dead and
        replaced, without touching the user session (which may still be fine).
        """
        with self._lock:
            current = self._cached
            if (
                not force
                and current is not None
                and self._parent == access_token
                and current.fresh(int(self._clock()), self._skew)
            ):
                return current.access

            profile_id = self._profile_id or self._resolve(access_token)
            fresh = self._tokens.mint(access_token, profile_id)
            self._cached = fresh
            # Record which user token this was derived from. If the user
            # session was renewed (a re-login after expiry), the old profile
            # token still has hours left but belongs to a session NLZiet may
            # have revoked; deriving again is the cheap, correct answer.
            self._parent = access_token
            return fresh.access

    def invalidate(self) -> None:
        """Forget the profile token; keep the resolved profile id."""
        with self._lock:
            self._cached = None
            self._parent = None

    def status(self) -> dict:
        current = self._cached
        if current is None:
            return {"profile": self._profile_id}
        return {
            "profile": self._profile_id,
            "expires_in": current.expires_at - int(self._clock()),
        }

    # -- internals ----------------------------------------------------------

    def _resolve(self, access_token: str) -> str:
        # Called under the lock: two threads must not both walk the list and
        # then disagree about which profile is "the" one.
        rows = self._api.profiles(access_token)
        for row in rows:
            profile_id = row.get("id")
            if profile_id:
                self._profile_id = str(profile_id)
                return self._profile_id
        raise LoginError("account has no profiles")
