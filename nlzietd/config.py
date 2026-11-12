"""Configuration, built once at start-up and then frozen.

Why a frozen dataclass instead of a dict or a global: every layer receives
exactly the settings it needs, and no layer can mutate them halfway through a
request. Why environment variables: the service has to survive an unattended
restart, and env is the one input a scheduled task or systemd unit can supply
without a config-file parser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    state_dir: Path
    api_base: str
    id_base: str
    app_base: str
    catalog_url: str
    refresh_skew: int
    debug_routes: bool
    profile_id: str | None
    # TLS is not a nicety here: browsers only expose EME (and therefore
    # Widevine) in a secure context, so a plain http URL on a LAN or tailnet
    # address can never play, no matter how correct the manifest is. Two files
    # rather than one: certificate and key have different lifetimes and
    # permissions, and every tool that issues them writes them apart.
    tls_cert: Path | None
    tls_key: Path | None
    tls_port: int
    # One line per request. Off by default because a daemon relaying segments
    # would otherwise write a line every few seconds forever; turned on when
    # something is wrong and you need to see what the player actually asked
    # for - which is the only way to debug a player that hangs instead of
    # reporting an error.
    log_requests: bool
    # Explicit path to ffmpeg. Empty means "whatever is on PATH", and if there
    # is none the pairing channel degrades to a QR image instead of failing:
    # encoding a video is the only thing here that needs a program we do not
    # ship, so it is an enhancement, not a dependency.
    ffmpeg: str | None

    # Derived paths live here so nothing else has to know the layout on disk.
    @property
    def token_file(self) -> Path:
        return self.state_dir / "tokens.json"

    @property
    def catalog_cache(self) -> Path:
        return self.state_dir / "channels.json"

    @property
    def device_page(self) -> str:
        """Where a pairing code is typed, with `{code}` left for the code.

        Deliberately not the API's `verification_uri`: that is
        `nlziet.nl/koppel`, which only *explains* the procedure. The page that
        accepts a code is the identity server's own `/device`, and it asks for
        a login first - which is why pairing feels different from the app.
        """
        return f"{self.id_base.rstrip('/')}/device?user_code={{code}}"

    @classmethod
    def from_env(cls) -> "Settings":
        here = Path(__file__).resolve().parent
        return cls(
            # 0.0.0.0 on purpose: the point is other machines on the LAN/tailnet.
            host=os.environ.get("NLZIETD_HOST", "0.0.0.0"),
            port=_int("NLZIETD_PORT", 8099),
            state_dir=Path(os.environ.get("NLZIETD_STATE", here / "state")),
            api_base=os.environ.get("NLZIETD_API", "https://api.nlziet.nl"),
            id_base=os.environ.get("NLZIETD_ID", "https://id.nlziet.nl"),
            app_base=os.environ.get("NLZIETD_APP", "https://app.nlziet.nl"),
            catalog_url=os.environ.get(
                "NLZIETD_CATALOG",
                "https://dut-epg.github.io/nlziet/channels.json",
            ),
            # Refresh a minute before expiry: a ticket minted with a token that
            # dies mid-handshake produces a confusing 401 instead of a retry.
            refresh_skew=_int("NLZIETD_SKEW", 60),
            debug_routes=os.environ.get("NLZIETD_DEBUG", "1") != "0",
            # Empty/absent means "the first profile on the account". Set it to
            # a profile id only when playback must be attributed to a specific
            # family member (kids profile, parental lock).
            profile_id=os.environ.get("NLZIET_PROFILE") or None,
            # Empty means "plain http only", which is fine for Kodi (it brings
            # its own CDM and ignores the secure-context rule) and for
            # localhost in a browser.
            tls_cert=_path("NLZIETD_TLS_CERT"),
            tls_key=_path("NLZIETD_TLS_KEY"),
            # Separate port from the http one on purpose: the same process
            # serves both, and a Kodi box that already points at http:// keeps
            # working while a browser switches to https://.
            tls_port=_int("NLZIETD_TLS_PORT", 8443),
            log_requests=os.environ.get("NLZIETD_LOG") == "1",
            ffmpeg=os.environ.get("NLZIETD_FFMPEG") or None,
        )
