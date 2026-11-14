# Quick start

Four ways to run `nlziet-m3u-gateway`. They are independent — pick the one that
fits the machine you have. Nothing here needs a NLZiet password in a config
file, nothing decrypts anything, and no option puts the daemon on the public
internet.

**New here? Use Docker without Tailscale (section 1). Two commands, one URL.**

## Choose your path

| you want | path | players use |
| --- | --- | --- |
| Try it in two minutes on one machine | **1. Docker, no Tailscale** | `http://localhost:8099/playlist.m3u` |
| A home server for the LAN | **1. Docker, no Tailscale** | `http://<lan-host>:8099/playlist.m3u` |
| Reach it from a TV/phone anywhere, privately | **2. Docker + Tailscale** | `https://<node>.<tailnet>.ts.net/playlist.m3u` |
| No Docker on that machine | **3. Python, no Tailscale** | `http://localhost:8099/playlist.m3u` |
| Python on a machine already on a tailnet | **4. Python + Tailscale** | `https://<machine>.<tailnet>.ts.net/playlist.m3u` |

### The one rule that decides everything

A browser only exposes EME (Widevine) on a **secure context**: `https://`, or
`http://localhost`. Kodi and most IPTV players bring their own CDM and are fine
with plain http. So:

- `http://localhost:8099/` works in a browser on the machine itself.
- `http://<lan-ip>:8099/` works for Kodi on other machines, but a browser there
  will fail with `Shaka Error 6020` until you give it https (sections 2 or 4,
  or your own certificate).
- Tailscale Serve and the daemon's own TLS listener both provide that https.

### Three ways to get https — and where the TLS variables fit

`NLZIETD_TLS_CERT` and `NLZIETD_TLS_KEY` are just **paths to two PEM files**
(a certificate chain and its private key). Setting both enables the daemon's
own https listener on port 8443. Without them it is plain http only. They are
optional, not Tailscale-specific, and nothing else in the project reads them.

| how | who terminates TLS | TLS variables? | renewal |
| --- | --- | --- | --- |
| **Tailscale Serve** (sections 2 and 4A) | Tailscale, with a certificate for the `*.ts.net` name | **no** | automatic |
| your own certificate (section 1) | nlzietd on 8443 | **yes** | up to you |
| `tailscale cert` + own listener (section 4B) | nlzietd on 8443 | **yes** | re-run every 90 days |

So `tailscale cert` *produces* those two files, and the variables *consume*
them. With Tailscale Serve the daemon stays on plain http 8099 and Tailscale
handles TLS — no files, no renewal, no variables. Either way the http port stays
available for Kodi.

Keep it private: there is no authentication on the HTTP server. Every path
below stays on a LAN or a tailnet. **Funnel is never used.**

## First run: pairing (all paths)

A fresh daemon has no session, so `/playlist.m3u` is not a channel list yet: it
is **one channel** whose name is the pairing code and whose stream is a
full-screen QR card. Open the playlist in an M3U player, scan the code, log in
at `id.nlziet.nl/device` and confirm. The daemon notices by itself.

Alternatively pair from a terminal:

```
python -m nlzietd login                              # Python installs
docker compose exec nlzietd python -m nlzietd login  # Docker installs
```

The session is saved in `nlzietd/state/tokens.json` (Python) or the `state`
volume (Docker). It renews itself with a refresh token; back that file/volume
up. **Do not share it** — it is a bearer token for your account.

---

## 1. Docker, no Tailscale (recommended to start)

Requires Docker. The image contains Python 3.12 and ffmpeg, so the pairing
video channel works immediately.

```
docker compose up --build -d
docker compose logs -f
```

| what | URL |
| --- | --- |
| M3U for Kodi / IPTV players | `http://localhost:8099/playlist.m3u` |
| browser player on this machine | `http://localhost:8099/` |
| Kodi on another machine | `http://<lan-host>:8099/playlist.m3u` |
| health check | `http://localhost:8099/healthz` |

- If `8099`/`8443` are taken on the host, set the host ports without editing
  anything: `NLZIETD_HTTP_PORT=18099 NLZIETD_HTTPS_PORT=18443 docker compose up -d`.
- For **browsers on other machines**, plain http is not enough. Either use a
  certificate you own (mount it into `/state/tls`, set `NLZIETD_TLS_CERT` and
  `NLZIETD_TLS_KEY`, and browse to port 8443), or take section 2 — Tailscale
  gives you a trusted certificate with no files to renew.
- `docker compose down` keeps the token volume; `down -v` deletes it and you
  have to pair again.

## 2. Docker + Tailscale (its own node)

The gateway becomes a node on your tailnet with **no published ports at all** —
nothing listens on the LAN. `tailscaled` runs as a sidecar in userspace mode,
which works even where `/dev/net/tun` is unavailable (Docker Desktop on
Windows).

Prerequisites in the Tailscale admin console: enable **MagicDNS** and **HTTPS
certificates** (DNS tab), then create an auth key (Settings → Keys). An
ephemeral, tagged key is fine.

```
TS_AUTHKEY=tskey-auth-... docker compose -f compose.tailnet.yml up --build -d
docker compose -f compose.tailnet.yml exec tailscale tailscale serve --bg 8099
```

The second command prints the URL, for example
`https://nlziet.<your-tailnet>.ts.net/`. Serve terminates TLS with a
certificate Tailscale provisions and renews, so browsers get a secure context
and Widevine works — the daemon itself needs no certificate files.

| what | URL |
| --- | --- |
| browser player | `https://nlziet.<your-tailnet>.ts.net/` |
| M3U on the tailnet | `https://nlziet.<your-tailnet>.ts.net/playlist.m3u` |

- `TS_HOSTNAME=my-tv` changes the node name (default `nlziet`).
- `tailscale serve --bg` saves the config in the node state, so it survives a
  restart; `tailscale serve reset` removes it.
- Keep `TS_AUTHKEY` out of the repository — put it in a gitignored `.env` or
  pass it inline as above.

## 3. Python, no Tailscale

Requires Python 3.12+. The daemon has **no third-party Python packages**.
ffmpeg is optional: with it the pairing entry is a video channel; without it
the same entry falls back to the QR image (the code is still in the channel
name). Install it (`winget install Gyan.FFmpeg` on Windows) or point
`NLZIETD_FFMPEG` at the executable.

```
python -m nlzietd login     # once; or start unpaired and use the playlist
python -m nlzietd
```

| what | URL |
| --- | --- |
| M3U | `http://localhost:8099/playlist.m3u` |
| browser on this machine | `http://localhost:8099/` |
| Kodi on the LAN | `http://<lan-host>:8099/playlist.m3u` |

- `NLZIETD_PORT`, `NLZIETD_TLS_PORT` and `NLZIETD_STATE` change the ports and
  state directory.
- For browsers on other machines, supply your own certificate:
  `set NLZIETD_TLS_CERT=...\live.crt` and `set NLZIETD_TLS_KEY=...\live.key`,
  then browse to `https://<host>:8443/`. Kodi keeps using plain http.
- Running unattended: see the Scheduled Task wrapper in [README.md](README.md).

## 4. Python + Tailscale

Two options, both on a machine that already runs the Tailscale client.

**A. Tailscale Serve (no certificate files).** Let the host's Tailscale
terminate TLS and proxy to the daemon's plain http port:

```
python -m nlzietd
tailscale serve --bg 8099
```

Use `https://<machine>.<tailnet>.ts.net/` in a browser and
`https://<machine>.<tailnet>.ts.net/playlist.m3u` for Kodi. Serve preserves the
`Host` header and sets `x-forwarded-proto: https`, which is what the daemon
uses to build its URLs. `http://<machine>:8099/` stays available for Kodi.

**B. The daemon's own TLS listener.** Issue a real Let's Encrypt certificate
for the machine's MagicDNS name, then point the daemon at it:

```
tailscale cert --cert-file state/tls/live.crt --key-file state/tls/live.key <machine>.<tailnet>.ts.net
set NLZIETD_TLS_CERT=state\tls\live.crt
set NLZIETD_TLS_KEY=state\tls\live.key
python -m nlzietd
```

Browsers use `https://<machine>.<tailnet>.ts.net:8443/`; Kodi uses http on
8099. Those certificates last 90 days — re-run `tailscale cert` and restart.

---

## Verify it works

```
curl http://localhost:8099/healthz
python -m nlzietd.tools.m3u_player --list
python -m nlzietd.tools.m3u_player --check
python -m unittest discover -s nlzietd/tests -t .
```

`--list` shows the parsed channels, `--check` fetches every URL in the
playlist, and `--play N` opens an entry in VLC/ffplay (or explains which client
you need for a Widevine channel).

## When something looks wrong

| symptom | cause / fix |
| --- | --- |
| `Shaka Error 6020` in a browser | no secure context — use https or `http://localhost` (sections 2 or 4, or your own certificate) |
| VLC plays the pairing channel but no TV channels | VLC has no Widevine CDM; use Kodi (`inputstream.adaptive`) or Chrome/Edge |
| playlist keeps showing one pairing channel | no session yet — scan the QR and confirm at `id.nlziet.nl/device` |
| pairing entry is an image, not a video | ffmpeg missing — install it or set `NLZIETD_FFMPEG`; the code is still in the channel name |
| `8099` already in use | `NLZIETD_PORT` (Python) or `NLZIETD_HTTP_PORT` (Docker) |
| container cannot write tokens | the `/state` bind mount must be writable by uid 10001 |
| Tailscale node stuck after a bad key | `docker compose -f compose.tailnet.yml down -v` and start again |

Still stuck? `/api/status` reports whether the session, the profile and the
channel catalogue are alive.
