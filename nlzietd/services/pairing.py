"""Pairing on demand: the screen asks, the daemon mints a code, a human approves.

Why this is a service and not three lines in a route:

* A pairing is state with a lifetime. The code on screen has to stay the same
  code while someone walks to their phone, and only one grant may be live at a
  time - every `begin()` is a request to the identity server.
* Waiting for approval takes as long as the human takes, which is longer than
  any HTTP request should stay open. So the poll loop runs on a worker and the
  browser asks "are we there yet" on its own schedule.
* Success is a transition, not a response: tokens are written to the store and
  the session cache has to be told to reload. Whoever called `start()` is long
  gone by then.

The service therefore owns the live grant, the worker, and that transition.
The web layer only reads `view()` and calls `start()`; the CLI does the same and
waits.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from ..domain.errors import LoginError
from ..ports import DevicePairing, TokenStore

# States, in the order a pairing moves through them. `paired` and `expired`
# are terminal for one grant; `start()` mints a fresh code after either.
IDLE = "idle"
PENDING = "pending"
PAIRED = "paired"
EXPIRED = "expired"
FAILED = "failed"


class PairingService:
    def __init__(
        self,
        pairing: DevicePairing,
        store: TokenStore,
        device_page: str,
        on_paired: Callable[[], None] = lambda: None,
        clock=time.time,
        start_worker: Callable[[Callable[[], None]], None] | None = None,
        min_interval: float = 5.0,
    ) -> None:
        self._pairing = pairing
        self._store = store
        # "{code}" is substituted: the API's own verification_uri is the
        # instruction page, not the page that accepts a code, so the caller
        # supplies the URL that works for its deployment.
        self._device_page = device_page
        self._page = device_page.split("?")[0]
        self._on_paired = on_paired
        self._clock = clock
        self._min_interval = min_interval
        self._start_worker = start_worker or self._thread
        self._lock = threading.Lock()
        # Serialises start() without holding the state lock across a network
        # call: `begin()` can take half a minute to time out, and `view()` has
        # to keep answering while it does.
        self._starting = threading.Lock()
        self._state = IDLE
        self._grant = None
        self._detail = ""
        self._last_begin = 0.0

    # -- public -------------------------------------------------------------

    def start(self) -> dict:
        """Begin a pairing, or hand back the one already on screen."""
        with self._starting:
            with self._lock:
                current = self._view_locked()
                if current["state"] == PENDING:
                    # Someone is already looking at a code; minting another
                    # would invalidate the one in their hand.
                    return current
                now = self._clock()
                if (
                    self._state == PAIRED
                    and now - self._last_begin < self._min_interval
                ):
                    # A page that reloads three times must not become three
                    # grants. Only pending/paired are worth coalescing: after
                    # `expired` or `failed` the point of the call is a retry.
                    return current
                self._last_begin = now

            try:
                grant = self._pairing.begin()
            except LoginError as err:
                with self._lock:
                    self._state, self._detail = FAILED, str(err)
                    return self._view_locked()

            with self._lock:
                self._grant = grant
                self._state = PENDING
                self._detail = ""
                view = self._view_locked()
            # Outside the lock, and after the view is built: a worker that
            # finishes instantly (a stub, or a very fast human) must not
            # deadlock on a lock its caller still holds.
            self._start_worker(lambda: self._wait(grant))
            return view

    def view(self) -> dict:
        """What a screen needs to draw, and nothing that is a secret.

        A device code is a short-lived capability to *ask* for a login, not a
        credential: it still needs the account password on the other side.
        """
        with self._lock:
            return self._view_locked()

    # -- internals ----------------------------------------------------------

    def _view_locked(self) -> dict:
        grant = self._grant
        if (
            self._state == PENDING
            and grant is not None
            and int(self._clock()) >= grant.expires_at
        ):
            # The worker reaches the same conclusion within one poll interval;
            # saying it here means the screen does not show a dead code.
            self._state = EXPIRED
            self._detail = "the pairing code expired before it was entered"
        return {
            "state": self._state,
            "code": grant.user_code if grant else None,
            # The full URL, code included: it is what the QR encodes and what
            # the link opens, so nobody has to type six characters.
            "url": self._device_page.format(code=grant.user_code)
            if grant
            else None,
            # Host and path only, for text that already shows the code.
            "page": self._page,
            "expires_in": max(0, grant.expires_at - int(self._clock()))
            if grant is not None and self._state == PENDING
            else 0,
            "detail": self._detail,
        }

    def _wait(self, grant) -> None:
        """Worker: block until the grant resolves, then act on the outcome."""
        try:
            tokens = self._pairing.complete(grant)
        except LoginError as err:
            # Expiry and denial are both LoginError; the clock tells them apart
            # without parsing a message written by somebody else's server.
            expired = int(self._clock()) >= grant.expires_at
            self._finish(EXPIRED if expired else FAILED, str(err))
            return
        except Exception as err:  # noqa: BLE001 - a worker must not die silently
            self._finish(FAILED, str(err))
            return

        # Persist before announcing: a listener that reloads the session must
        # find the new tokens on disk, not the old ones.
        self._store.save(tokens)
        self._on_paired()
        self._finish(PAIRED, "")

    def _finish(self, state: str, detail: str) -> None:
        with self._lock:
            self._state = state
            self._detail = detail

    @staticmethod
    def _thread(job: Callable[[], None]) -> None:
        threading.Thread(target=job, daemon=True).start()
