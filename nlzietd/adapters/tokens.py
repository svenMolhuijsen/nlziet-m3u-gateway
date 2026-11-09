"""File-backed token store.

Three deliberate choices:

1. JSON, not a database. One row of state, human-inspectable, no dependency.
2. Write to a temp file then os.replace. A kill -9 mid-write must not leave a
   truncated token file, because that reads as "logged out" and triggers a
   full login for no reason.
3. Best-effort chmod 0600. On POSIX this matters; on Windows it silently does
   nothing, which is why the failure is swallowed rather than raised.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..domain.models import Tokens


class FileTokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Tokens | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return Tokens(
                access=raw["access"],
                id_token=raw.get("id_token", ""),
                refresh=raw.get("refresh"),
                expires_at=int(raw["expires_at"]),
            )
        except (OSError, ValueError, KeyError):
            # A missing or corrupt file is not an error: it means "no session".
            return None

    def save(self, tokens: Tokens) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "access": tokens.access,
                "id_token": tokens.id_token,
                "refresh": tokens.refresh,
                "expires_at": tokens.expires_at,
            },
            indent=2,
        )
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self._path)  # atomic on both POSIX and NTFS
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def clear(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            pass
