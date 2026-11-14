"""Live smoke test: the real adapters, the real network, one channel.

Deliberately not a unit test - it needs a paired session and internet, so it
stays out of `tests/`. What it proves, in order:

    1. the paired session yields a usable access token
    2. the session service persists them (a second, provider-less session
       reads them back - that is the restart case)
    3. the catalogue maps a channel to a name
    4. EPG -> profile token -> handshake produces a ticket
    5. the manifest is fetchable, rewritten with our BaseURL, and the init
       segment it points at is fetchable through the transport service
    6. the licence proxy answers a challenge

Steps 5 and 6 are the ones that matter for the design: they prove the player
would get manifest, media and licence from *us*, while the CDM does the
decrypting. Pair once with `python -m nlzietd login` first:

    python -m nlzietd.smoke
"""

from __future__ import annotations

import os
import re
import sys

# Allow `python nlzietd/smoke.py` as well as `python -m nlzietd.smoke`.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nlzietd.adapters.tokens import FileTokenStore  # noqa: E402
from nlzietd.app import build  # noqa: E402
from nlzietd.config import Settings  # noqa: E402
from nlzietd.services.session import SessionService  # noqa: E402

CHANNEL = "npo1"
BASE = f"http://127.0.0.1:8099/seg/{CHANNEL}/"


def fail(step: str, detail: object) -> int:
    print(f"   FAILED: {step}: {detail}")
    return 1


def main() -> int:
    settings = Settings.from_env()
    app = build(settings)

    print("1. login")
    try:
        access = app.session.token()
    except Exception as err:
        return fail("login", err)
    print(f"   ok - access token {len(access)} chars")
    print(f"   status: {app.session.status()}")

    print("2. persistence (simulating a restart)")
    reloaded = SessionService(
        store=FileTokenStore(settings.token_file),
        provider=None,
        skew=settings.refresh_skew,
    )
    state = reloaded.status()
    print(f"   {state}")
    if not state.get("authenticated"):
        return fail("persistence", "token did not survive to disk")

    print("3. catalogue")
    channels = app.playback.channels()
    print(f"   {len(channels)} channels, first: {list(channels.values())[0].name}")
    if CHANNEL not in channels:
        return fail("catalogue", f"{CHANNEL} missing")

    print(f"4. ticket for {CHANNEL}")
    try:
        ticket = app.playback.ticket(CHANNEL)
    except Exception as err:
        return fail("ticket", err)
    print(f"   title:    {ticket.title}")
    print(f"   content:  {ticket.content_id}")
    print(f"   session:  {ticket.session_id}")
    print(f"   manifest: {ticket.manifest_url.split('?')[0]}")
    print(f"   licence:  {ticket.licence_url.split('?')[0]}")
    print(f"   headers:  {sorted(ticket.headers)}")

    print("5. manifest through the transport")
    manifest = app.transport.manifest(CHANNEL, BASE)
    print(f"   HTTP {manifest.status}, {len(manifest.body)} bytes, "
          f"content-type={manifest.headers.get('content-type')}")
    if manifest.status != 200:
        return fail("manifest", manifest.body[:200])
    text = manifest.body.decode("utf-8", "replace")
    if f"<BaseURL>{BASE}</BaseURL>" not in text:
        return fail("manifest", "BaseURL was not injected")

    init = re.search(r'initialization="([^"]+)"', text)
    if not init:
        return fail("manifest", "no SegmentTemplate initialization")
    # Pick the lowest video representation: it exists in every ladder, so the
    # test does not break when a new top rendition appears.
    path = init.group(1).replace("$RepresentationID$", "V0")
    print(f"   init path: {path.split('?')[0]}")
    segment = app.transport.segment(CHANNEL, path)
    print(f"   segment HTTP {segment.status}, {len(segment.body)} bytes, "
          f"content-type={segment.headers.get('content-type')}")
    if segment.status != 200 or len(segment.body) < 100:
        return fail("segment", segment.body[:200])

    print("6. licence proxy")
    try:
        licence = app.transport.licence(CHANNEL, b"\x08\x04", "application/octet-stream")
    except Exception as err:
        return fail("licence", err)
    print(f"   HTTP {licence.status}, {len(licence.body)} bytes, "
          f"content-type={licence.headers.get('content-type')}")
    if licence.status != 200:
        return fail("licence", licence.body[:200])

    print("\nall good: manifest, segment and licence all came back through nlzietd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
