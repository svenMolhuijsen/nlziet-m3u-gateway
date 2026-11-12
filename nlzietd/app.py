"""Composition root: the only module that knows every layer at once.

Assembling here, rather than importing adapters inside services, is what makes
the dependency rule enforceable by reading one file. It is also the seam where
"password login" becomes "session lifted off the TV": swap the provider, leave
everything below untouched.

`App` exists so callers (the server, the smoke test) get the assembled graph
as one object instead of a growing tuple, and so no module has to import
`app` just to know what a built service looks like.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

from .adapters.api import HttpNlzietApi
from .adapters.catalog import DutCatalogue
from .adapters.identity import OidcDeviceLogin, OidcPasswordLogin, OidcProfileToken
from .adapters.relay import HttpRelay
from .adapters.tokens import FileTokenStore
from .config import Settings
from .ports import SessionProvider, TokenStore
from .services.pairing import EXPIRED, FAILED, PAIRED, PairingService
from .services.playback import PlaybackService
from .services.profile import ProfileSessions
from .services.session import SessionService
from .services.transport import TransportService
from .web.server import Router, build_router, make_server, server_context
from .web.stream import PairingStream


class App:
    """The assembled daemon. A plain object, not a container."""

    def __init__(
        self,
        settings: Settings,
        session: SessionService,
        profiles: ProfileSessions,
        playback: PlaybackService,
        transport: TransportService,
        pairing: PairingService,
        stream: PairingStream,
        router: Router,
    ) -> None:
        self.settings = settings
        self.session = session
        self.profiles = profiles
        self.playback = playback
        self.transport = transport
        self.pairing = pairing
        self.stream = stream
        self.router = router


def build_provider(settings: Settings, store: TokenStore) -> SessionProvider:
    """Choose how we authenticate.

    A paired session wins over credentials in the environment: pairing is the
    better session (a refresh token, no password anywhere) and it is what the
    browser's pairing panel writes. Falling back to the password when a refresh
    token exists would also throw that token away on the next renewal.

    Password login is for machines that were never paired: it can recover from
    anything, including a revoked session. Both satisfy the same port, so
    nothing below this function notices which one it got.
    """
    paired = store.load()
    if paired is not None and paired.refresh:
        return OidcDeviceLogin(settings.id_base)
    email = os.environ.get("NLZIET_EMAIL")
    password = os.environ.get("NLZIET_PASSWORD")
    if email and password:
        return OidcPasswordLogin(
            settings.id_base, settings.app_base, email, password
        )
    return OidcDeviceLogin(settings.id_base)


def pair(settings: Settings) -> int:
    """`python -m nlzietd login`: pair this machine, once, the way a TV does.

    Interactive on purpose: the code has to reach a human, and a daemon that
    prints pairing codes into a log nobody reads is not more convenient than
    typing five characters once.

    The printing is all this function does; the pairing itself is the same
    service the browser panel drives, so there is one implementation of "show a
    code, wait for a human, store the session".
    """
    store = FileTokenStore(settings.token_file)
    pairing = PairingService(
        pairing=OidcDeviceLogin(settings.id_base),
        store=store,
        device_page=settings.device_page,
    )
    view = pairing.start()
    if view["state"] != "pending":
        print(f"pairing failed: {view['detail']}", file=sys.stderr)
        return 1

    minutes = max(1, view["expires_in"] // 60)
    print(f"Pairing code: {view['code']}")
    print(f"Open {view['url']}")
    print(
        "Log in there with your NLZiet account (the identity server, so a "
        "separate login from the app), confirm, and this command finishes.",
        flush=True,
    )
    print(f"The code is good for about {minutes} minute(s); waiting…", flush=True)

    try:
        while True:
            view = pairing.view()
            if view["state"] in (PAIRED, EXPIRED, FAILED):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped before the code was entered", file=sys.stderr)
        return 1

    if view["state"] != PAIRED:
        print(f"pairing failed: {view['detail']}", file=sys.stderr)
        return 1

    print(f"paired: session stored in {settings.token_file}", flush=True)
    tokens = store.load()
    if tokens is None or not tokens.refresh:
        # Worth shouting about: without a refresh token this machine is back to
        # the eight-hour cliff, and the pairing did not buy what it promised.
        print(
            "warning: no refresh token came back, so this session still expires "
            "in a few hours",
            flush=True,
        )
    return 0


def build(settings: Settings) -> App:
    store = FileTokenStore(settings.token_file)
    session = SessionService(
        store=store,
        provider=build_provider(settings, store),
        skew=settings.refresh_skew,
    )
    api = HttpNlzietApi(settings.api_base, settings.app_base)
    profiles = ProfileSessions(
        api=api,
        tokens=OidcProfileToken(settings.id_base),
        profile_id=settings.profile_id,
        skew=settings.refresh_skew,
    )
    catalogue = DutCatalogue(settings.catalog_url, settings.catalog_cache)
    playback = PlaybackService(
        api=api,
        catalogue=catalogue,
        session=session,
        profiles=profiles,
    )
    transport = TransportService(playback=playback, relay=HttpRelay())
    # The web layer pairs on demand, for the same reason a TV does: whoever is
    # looking at the screen may be the one holding the password. `on_paired`
    # is what makes the waiting request find the new session.
    pairing = PairingService(
        pairing=OidcDeviceLogin(settings.id_base),
        store=store,
        device_page=settings.device_page,
        on_paired=session.invalidate,
    )
    # Optional on purpose: without ffmpeg the pairing entry is still a channel
    # (the QR image), just not a video. Resolving the encoder here, once, keeps
    # the web layer from knowing about PATH or a config variable.
    stream = PairingStream(ffmpeg=settings.ffmpeg)
    router = build_router(
        catalogue,
        playback,
        session,
        profiles,
        transport,
        settings.debug_routes,
        tls_port=settings.tls_port if settings.tls_cert else None,
        pairing=pairing,
        stream=stream,
    )
    return App(settings, session, profiles, playback, transport, pairing, stream, router)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    settings = Settings.from_env()
    if args and args[0] in {"login", "pair"}:
        return pair(settings)
    app = build(settings)
    server = make_server(settings.host, settings.port, app.router,
                         log=settings.log_requests)

    # The https listener is optional, but without it nothing in a browser can
    # play: EME is a secure-context API, so Chrome/Edge hide it from a plain
    # http page on a LAN or tailnet address and Shaka fails with
    # MISSING_EME_SUPPORT (6020) before it even fetches a manifest.
    https = None
    if settings.tls_cert and settings.tls_key:
        tls = server_context(settings.tls_cert, settings.tls_key)
        https = make_server(settings.host, settings.tls_port, app.router, tls,
                            log=settings.log_requests)

    state = app.session.status()
    print(
        f"nlzietd on http://{settings.host}:{settings.port}  "
        f"auth={'yes' if state.get('authenticated') else 'no'}  "
        f"state={settings.state_dir}",
        flush=True,
    )
    if https is not None:
        print(
            f"nlzietd on https://{settings.host}:{settings.tls_port}  "
            "(browsers must use this: EME needs a secure context)",
            flush=True,
        )
        # One process, two listeners: the https one gets its own thread so the
        # http one keeps the main-thread shutdown path and both die on ^C.
        threading.Thread(target=https.serve_forever, daemon=True).start()
    elif settings.debug_routes:
        print(
            "no NLZIETD_TLS_CERT/KEY: plain http only. Fine for Kodi and for "
            "http://localhost, but a browser on a LAN/tailnet address will "
            "refuse to play.",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        server.server_close()
        if https is not None:
            https.shutdown()
            https.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
