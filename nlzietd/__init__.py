"""nlzietd - a small NLZiet gateway for machines on your own network.

Design invariant, and the reason this package is allowed to exist:

    Nothing here decrypts anything.

We fetch a DASH manifest and a licence URL from NLZiet and hand both to a
player that owns a Widevine CDM - a browser through EME, or Kodi through
inputstream.adaptive. Segments are relayed byte for byte with the headers
NLZiet expects. There is no key exchange code in this tree, and a change
that needs one is a change in the wrong direction.

Everything else (login, token refresh, channel metadata, EPG) is ordinary
client work against the account the user already pays for.
"""

__version__ = "0.2.0"
