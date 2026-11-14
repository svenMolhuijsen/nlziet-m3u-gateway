# nlzietd

A small NLZiet gateway for the machines on your own network. It logs in with
your own account, finds what is on a channel right now, and serves the pieces a
player needs to play it:

```
/                        a Shaka Player page (browser, Widevine via EME - needs https)
                         and, when there is no session, a pairing screen
/api/status              session/profile/catalogue state, and the https port
/api/pair                pairing state; POST mints a code
/pair/qr.png             the live code as a QR image (page, and M3U channel logo)
/pair/stream.m3u8        the pairing card as an HLS video channel
/pair/stream/<code>/…   its MPEG-TS segments
/playlist.m3u            an M3U for Kodi's inputstream.adaptive
/manifest/<ch>.mpd       the live DASH manifest, rewritten to point at us
/seg/<ch>/...            media segments, relayed byte for byte
/licence/<ch>            the Widevine licence exchange, relayed byte for byte
```

**Nothing here decrypts anything.** There is no key handling in this tree. The
player owns the CDM; nlzietd hands it a manifest, a licence URL and a stream of
bytes. A change that needs keys is a change in the wrong direction.

Not affiliated with NLZiet. It plays the subscription you already pay for, on
the machines in your own home; it is not a way to share streams with anyone
else.

## Run

Pair this machine once, so the daemon has a session that renews itself:

```
python -m nlzietd login
```

Then start it (add `NLZIETD_TLS_CERT`/`NLZIETD_TLS_KEY` for the https listener):

```
python -m nlzietd
```

Credentials in the environment work too — `set NLZIET_EMAIL=…` and
`set NLZIET_PASSWORD=…` — but that session cannot renew itself; see
[sessions](#sessions-pair-once-then-it-renews).

You can also skip the command: start it unpaired and open the page, or point a
player at the playlist. Both ask the identity server for a code and show it,
which is the same pairing without a terminal.

For the playlist to be a **video** channel rather than just a QR thumbnail,
install ffmpeg (`winget install Gyan.FFmpeg` on Windows) so it is on `PATH`, or
point `NLZIETD_FFMPEG` at the executable. It is optional: without it the
pairing entry still shows the code and its QR as an image. See
[the pairing channel](#the-pairing-channel).

Then open `https://<this-machine>:8443/` from a machine on the same LAN or
tailnet — see [browser playback](#browser-playback-needs-https) for why the
https URL and not the http one. Kodi uses the plain http port, below.

`NLZIETD_PORT` changes the http port, `NLZIETD_TLS_PORT` the https one,
`NLZIETD_STATE` the state directory.

| variable | default | meaning |
| --- | --- | --- |
| `NLZIET_EMAIL`, `NLZIET_PASSWORD` | – | optional password login, used only when no pairing exists |
| `NLZIET_PROFILE` | first profile | profile id the streams are attributed to |
| `NLZIETD_HOST` | `0.0.0.0` | bind address (LAN/tailnet on purpose, do not expose publicly) |
| `NLZIETD_PORT` | `8099` | listen port (http, for Kodi and for localhost in a browser) |
| `NLZIETD_TLS_CERT` | – | certificate chain, PEM; enables the https listener |
| `NLZIETD_TLS_KEY` | – | private key, PEM |
| `NLZIETD_TLS_PORT` | `8443` | listen port for https |
| `NLZIETD_STATE` | `nlzietd/state` | token file and catalogue cache |
| `NLZIETD_FFMPEG` | `ffmpeg` on `PATH` | encoder for the pairing video channel; absent means QR-image fallback |
| `NLZIETD_DEBUG` | `1` | enables `/api/ticket/<ch>` |
| `NLZIETD_LOG` | `0` | one line per request (paths without the `hdntl` token) |

The token file holds a bearer token for your account. It is written `0600`
where the OS supports it. Do not share the state directory.

## Browser playback needs https

This is the one deployment detail that bites everyone, so it is worth its own
section. A browser only exposes EME — the API behind Widevine — in a *secure
context*: `https://`, or `http://localhost`. On a plain http page served from a
LAN or tailnet address, Chrome and Edge leave
`navigator.requestMediaKeySystemAccess` undefined, and Shaka Player fails
immediately with:

```
afspelen mislukt: Shaka Error 6020
```

6020 is `MISSING_EME_SUPPORT`. Nothing is wrong with the manifest, the licence or
the account: the browser refused to hand out the CDM. The page now detects this
before loading anything and names the https URL instead of the error code, and
`/api/status` advertises the https port so it can.

Two ways to get a certificate worth trusting:

**Tailscale (recommended).** If this machine is on a tailnet with HTTPS
certificates enabled in the admin console, Tailscale will issue a real
Let's Encrypt certificate for its `*.ts.net` name. No trust store to touch, and
every device on the tailnet can verify it:

```
tailscale cert --cert-file state/tls/live.crt --key-file state/tls/live.key <machine>.<tailnet>.ts.net
set NLZIETD_TLS_CERT=state\tls\live.crt
set NLZIETD_TLS_KEY=state\tls\live.key
```

Those certificates last 90 days; re-run `tailscale cert` (same filenames) and
restart the daemon. Only the `*.ts.net` name is on the certificate, so use that
name in the browser — the LAN IP will show a name mismatch.

**Your own certificate.** Any certificate with `serverAuth` fits, as long as the
name you browse to is in its SANs and the certificate is trusted by the client.
A self-signed one works after importing it into the Windows certificate store,
but Kodi's `inputstream.adaptive` verifies against its own CA bundle, which is
why the http listener stays available and separate.

Both listeners serve the same routes: the scheme is taken from the connection,
so an https request gets `https://` segment URLs in the manifest (an http URL
inside an https page is blocked as mixed content) and an http request keeps
`http://`.

## How playback works

1. `GET /v8/epg/programlocations/live?channel=<ch>` gives the content id of
   what is on air, with the user token.
2. `POST id.nlziet.nl/connect/token` with `grant_type=profile` exchanges that
   user token for a **profile-scoped** token. The handshake only answers to the
   second one.
3. `GET /v9/stream/handshake?...&offsetType=Live` allocates a stream and
   returns a signed Akamai manifest URL plus a licence URL and a
   `Nlziet-License` blob.
4. The manifest is fetched and **one** element is added: a `<BaseURL>` pointing
   at `/seg/<ch>/`. Every segment in NLZiet's manifests is relative, so this
   moves the whole media space through this daemon without touching the
   segment templates.
5. The player reads the Widevine PSSH from the untouched manifest, sends its
   challenge to `/licence/<ch>`, and we forward it to NLZiet's licence proxy
   with the ticket's capability token. The licence comes back opaque.

A ticket (handshake) is cached per channel for 30 minutes, because a DASH
player re-fetches a dynamic manifest every couple of seconds and a handshake
per poll would burn through the simultaneous-stream limit. A stale signed URL
is retried once with a fresh handshake.

## Sessions: pair once, then it renews

There are two ways in, and they differ in one thing that matters:

| how | where the token comes from | renews itself |
| --- | --- | --- |
| **pairing** (recommended) | `python -m nlzietd login`, once | yes — refresh token, no password on disk |
| password | `NLZIET_EMAIL`/`NLZIET_PASSWORD` in the environment | no — the web login returns no refresh token |

### Pairing

```
python -m nlzietd login
```

It prints a code and a link like `https://id.nlziet.nl/device?user_code=…`. Open
that on a phone or laptop, log in with the NLZiet account, confirm, and the
command writes the session to the state directory.

Two details that cost time if nobody tells you: the code is typed at
**`id.nlziet.nl/device`**, not at `nlziet.nl/koppel` (which is only the
instructions page the TV displays), and the identity server asks for its own
login — separate from `app.nlziet.nl`, which is why it feels different from
pairing inside the app.

Why this beats a password: the TV's client (`triple-web-tv`) asks for
`api openid offline_access`, so the session comes back with a **refresh token**.
The access token lives one hour, the daemon renews it silently, and it saves the
rotated refresh token every time. Nothing expires at eight hours and no password
sits anywhere on disk. If the pairing is ever revoked (NLZiet's device list, a
password change), the daemon says so and asks for `login` again — it does not
quietly fall back to something else.

### Pairing from the screen

The daemon does not have to be paired by the person who started it, and that
person does not need a shell. With no session:

* **The page** (`/`) asks for a code, prints it large enough to read from a
  sofa, shows the matching QR, and polls. Scan or type, log in, confirm — the
  page picks up the new session and loads the channels by itself. When a code
  expires it mints another, because the human is still standing there.
* **The playlist** does the same for players that are not browsers. With no
  session `/playlist.m3u` is a single channel: the code is in the name, the QR
  is its `tvg-logo`, and pressing play opens a **video of the code** full
  screen. Open the playlist on the TV to get a fresh code.

The QR is drawn by the daemon (`web/qr.py`, a small level-M encoder) rather than
by a CDN script or a third-party QR service: the code is short-lived but it is
still a login approval, and it should not leave the house. The page, the playlist
logo and the video all render the same symbol, so there is one implementation to
get right.

### The pairing channel

A TV in the living room sometimes has nothing but an IPTV player — no browser,
no keyboard, no second screen. A logo is a thumbnail in a list, not something
you can scan, so while unpaired the entry is a real channel you can press play
on:

```
/pair/stream.m3u8            H.264, 1280x720, 5 fps, no audio, no DRM
/pair/stream/<code>/segNNN.ts  MPEG-TS segments, 10 s each
```

H.264 in MPEG-TS over HLS is the lowest common denominator of IPTV players,
including set-top boxes that only ever learned HLS. The card shows the header
`KOPPEL DIT APPARAAT`, a 410 px QR, the six-character code in large letters,
and `ID.NLZIET.NL/DEVICE` for typing it on a phone. It is drawn in pure Python
(`web/card.py`, a 5x7 bitmap font) instead of with ffmpeg `drawtext`, so no
font discovery or filter escaping is needed on any OS.

The card is encoded once per code (about five seconds of CPU, ~1.7 MB for a ten
minute clip) and cached; every player on the network then gets the same bytes.
ffmpeg is the only thing here that is not in the Python standard library:

* **With ffmpeg** — on `PATH`, or set with `NLZIETD_FFMPEG` — the M3U entry
  points at `/pair/stream.m3u8`, with `#KODIPROP:...manifest_type=hls` for Kodi.
  No `license_key`: this channel has no DRM.
* **Without it** — the same entry points at `/pair/qr.png` and the code stays in
  the channel name. Worse to look at, still enough to pair.
* If an encoder is present but fails, the first request answers `503` and every
  later playlist falls back to the image rather than retrying a broken binary.

The clip is a VOD playlist that outlasts the five-minute code. When a player
re-opens the channel after the clip ends, `/pair/stream.m3u8` notices the code
has expired and mints a new one, so a dead code is never left on the screen.
`HEAD` probes work too, for players that check a channel before reading it.

Anybody who can reach the port can ask for a code. That grants nothing by
itself — approving it still needs the account password at `id.nlziet.nl` — but
it does mean the pairing screen is as reachable as everything else here. Treat
the port as trusted; it already serves your streams.

### Password login

`grant_type=password` returns an access token and **no** refresh token, so after
about 8 hours the session is over. That is NLZiet's design, not a gap here. With
credentials set, the next request that needs a token logs in again, saves the
new token `0600`, and carries on; without them those requests answer `503 not
authenticated`.

Details worth knowing:

- Logging in is **lazy**, not scheduled: a quiet night costs nothing, and a
  login happens only when someone actually presses play. One lock spans the
  whole obtain-and-persist sequence, so ten parallel requests after expiry
  produce one login, not ten (ten would look like an attack to the identity
  server).
- A stream that is **already playing** survives the cliff for a while: its
  segment URLs are signed by the CDN, and the daemon only needs a fresh
  handshake when the cached ticket passes 30 minutes. So expect playback to
  stop within half an hour of expiry, not at the same second.
- `/api/status` is the honest read-out: `expires_in` goes negative once the
  token is dead, `has_refresh` says whether silent renewal is possible at all,
  and `can_relogin` whether the daemon can dig itself out.

Running it unattended on Windows without pairing means putting the credentials
somewhere the daemon can read at logon. A wrapper next to a Scheduled Task keeps
them out of the repository and out of shell history:

```bat
@echo off
set NLZIET_EMAIL=you@example.com
set NLZIET_PASSWORD=...
set NLZIETD_TLS_CERT=C:\nlzietd\state\tls\live.crt
set NLZIETD_TLS_KEY=C:\nlzietd\state\tls\live.key
cd /d C:\nlzietd
python -m nlzietd
```

Or skip that entirely: pair once with `python -m nlzietd login`, then the wrapper
only needs the TLS variables (plus `NLZIET_PROFILE` if you want a specific
profile), and the daemon renews by itself.

## Kodi

Point Kodi at `http://<this-machine>:8099/playlist.m3u`. The playlist carries
the `#KODIPROP` lines `inputstream.adaptive` needs (DASH + `com.widevine.alpha`
+ licence URL). VLC will not play these streams: it has no Widevine CDM.

With no session, that URL is not a channel list but the pairing entry described
[above](#the-pairing-channel): one channel whose name is the code, whose icon
is the QR, and whose stream is a full-screen card showing both. It is plain
HLS/H.264, so the `#KODIPROP` lines say `manifest_type=hls` and ask for no
licence; the DASH/Widevine properties above belong to the real channels.

Keep Kodi on http: it brings its own CDM, so the secure-context rule does not
apply to it, and its certificate bundle would reject a self-signed certificate.
The playlist is built from the request, so an http request gets http URLs
throughout — no mixed content, no certificate to install.

## Layers

```
domain/     frozen values and errors; imports nothing
ports.py    Protocols: what services may depend on
adapters/   network/disk only: api, identity, relay, tokens, catalog
services/   orchestration: session, profile, playback, transport, pairing
web/        routing, HTML, M3U, QR, the pairing card and its HLS clip
app.py      composition root; the only file that knows every layer
```

Services never import adapters. The password login and the device-paired session
are two `SessionProvider`s; which one a given machine uses is an `app.py`
decision, not a refactor. Pairing lives in a service rather than in a route
because the code has a lifetime and the wait outlives any request: the CLI and
the page drive the same state machine, one of them just prints instead of
drawing.

## Limits, honestly

- The paired session can be revoked from your NLZiet device list at any time;
  that is their feature, not a bug here. Recovery is one `python -m nlzietd
  login`.
- Refresh tokens rotate: the daemon writes the new one atomically after each
  renewal. Kill it in the millisecond between the exchange and the write and you
  may have to pair again.
- One profile per daemon (`NLZIET_PROFILE`), because the handshake is
  profile-scoped.
- One stream session per channel at a time, reused for 30 minutes. Two players
  on the same channel share it; different channels each allocate their own,
  and NLZiet's plan limit (1 on Basis) still applies.
- No authentication on the HTTP server: keep it on the LAN or tailnet. That
  includes the pairing screen — anyone who can open it can get a code, though
  approving the code still needs the account password.
- The pairing video needs ffmpeg, which is optional and not bundled. It is a
  ten-minute VOD clip of a still image; after it ends, re-opening the channel
  mints a new code. It is not a live channel, so a player left on it overnight
  eventually returns to its channel list.
- A desktop browser gets Widevine **L3**, which is software decryption: NLZiet's
  licence server decides what that is allowed to be, and it may cap quality or
  refuse a title. iOS Safari cannot play any of this (FairPlay only), and
  VLC/mpv/Plex have no CDM at all.
