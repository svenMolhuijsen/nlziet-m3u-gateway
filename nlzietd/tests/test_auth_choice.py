"""There is exactly one way in: device pairing.

This test pins the removal of the password-login option. The legacy variable
names below appear ONLY in this file, as a regression guard: no code reads
them, no docs advertise them, and a stray value in the environment must not
change how the daemon authenticates or bring the old adapter back as a
"convenient" fallback.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from nlzietd.adapters import identity
from nlzietd.adapters.identity import OidcDeviceLogin
from nlzietd.app import build
from nlzietd.config import Settings


class OneWayInTests(unittest.TestCase):
    def test_the_password_adapter_is_gone(self):
        self.assertFalse(hasattr(identity, "OidcPasswordLogin"))
        self.assertFalse(hasattr(identity, "_visible_text"))

    def test_env_credentials_do_not_change_the_provider(self):
        with mock.patch.dict(os.environ, {
            "NLZIET_EMAIL": "someone@example.com",
            "NLZIET_PASSWORD": "not-a-real-password",
        }):
            app = build(Settings.from_env())
        self.assertIsInstance(app.session._provider, OidcDeviceLogin)

    def test_pairing_service_is_still_wired(self):
        app = build(Settings.from_env())
        # The page and the CLI both drive this one service; without it there
        # would be no way to get the first refresh token.
        self.assertIsNotNone(app.pairing)


if __name__ == "__main__":
    unittest.main()
