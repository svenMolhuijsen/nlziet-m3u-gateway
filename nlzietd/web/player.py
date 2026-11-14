"""The two things a human opens: a browser player and an M3U.

Nothing here is a service: it is HTML and text, generated from the catalogue.
It has to know three URLs per channel (manifest, licence, segment base) and no
more, which is why it lives in `web` and not in a service layer.

Why Shaka: it is the reference EME/DASH player in a browser, so a Widevine
failure reports a readable error instead of a black rectangle. It is loaded
from a CDN because bundling a 400 KB player into a stdlib-only daemon would be
the only dependency in the project; the page says so when the CDN is
unreachable.

Why the M3U carries `#KODIPROP` lines: Kodi's `inputstream.adaptive` needs to
be told the stream is DASH-with-Widevine *before* it opens the URL, and it
cannot infer the licence endpoint from the manifest, because that endpoint is
ours.
"""

from __future__ import annotations

from ..domain.models import Channel

# One placeholder, replaced with the initial channel id. Kept as a plain
# string rather than a template engine: the page has no other variables, and
# a format string would collide with every `{` in the JavaScript.
PLAYER_PAGE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nlzietd</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.4 system-ui, sans-serif; background: #0b0d10; color: #e8eaed; }
  header { display: flex; gap: .75rem; align-items: center; padding: .75rem 1rem; flex-wrap: wrap; }
  h1 { font-size: 1rem; font-weight: 600; margin: 0; letter-spacing: .02em; }
  select { background: #161a20; color: inherit; border: 1px solid #2a3038; border-radius: .4rem; padding: .35rem .5rem; font: inherit; }
  main { padding: 0 1rem 1rem; }
  video { width: 100%; max-height: calc(100vh - 8rem); background: #000; border-radius: .5rem; }
  #status { margin: .5rem 0 0; color: #9aa4b2; font-size: .85rem; white-space: pre-wrap; }
  #status.error { color: #ff8f8f; }
  /* The pairing panel is meant to be read from a sofa, so the code is large
     and the QR is the widest thing on the page. */
  #pairing { max-width: 32rem; margin: 2.5rem auto 1rem; text-align: center; }
  #pairing h2 { font-size: 1.05rem; font-weight: 600; margin: 0 0 .35rem; }
  #pairing p { margin: .5rem 0; color: #9aa4b2; font-size: .9rem; }
  #qrbox { display: inline-block; background: #fff; border-radius: .6rem; padding: .6rem; line-height: 0; }
  #qrbox[hidden] { display: none; }
  #qrbox img { width: 13rem; height: 13rem; image-rendering: pixelated; }
  #paircode { font: 700 1.9rem/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .22em; color: #e8eaed; }
  #pairlink { color: #8ab4f8; word-break: break-all; }
  #pairstate { color: #6f7885; font-size: .8rem; }
  #pairretry { background: #1f2630; color: #e8eaed; border: 1px solid #2f3946; border-radius: .4rem; padding: .45rem .9rem; font: inherit; }
  #pairretry[hidden] { display: none; }
</style>
</head>
<body>
<header>
  <h1>nlzietd</h1>
  <select id="channel" aria-label="Kanaal"></select>
</header>
<main>
  <section id="pairing" hidden>
    <h2>Dit apparaat is nog niet gekoppeld</h2>
    <p id="pairhint">koppelcode ophalen…</p>
    <div id="qrbox" hidden><img id="qrimg" alt="QR-code met de koppelcode" width="208" height="208"></div>
    <p id="paircode"></p>
    <p><a id="pairlink" target="_blank" rel="noreferrer"></a></p>
    <p id="pairstate"></p>
    <button id="pairretry" hidden>Opnieuw</button>
  </section>
  <video id="video" controls autoplay muted playsinline></video>
  <p id="status">laden…</p>
</main>
<script src="https://cdn.jsdelivr.net/npm/shaka-player@4/dist/shaka-player.compiled.js"></script>
<script>
// The page is served from the same origin as the manifest, so every URL is
// built from location.origin. That is what makes it work unchanged on
// localhost, a LAN address and a tailnet name.
const origin = location.origin;
const video = document.getElementById('video');
const select = document.getElementById('channel');
const statusEl = document.getElementById('status');

function say(text, isError) {
  statusEl.textContent = text;
  statusEl.classList.toggle('error', !!isError);
}

// EME - the browser API behind Widevine - only exists in a secure context:
// https, or http on localhost. Chrome and Edge therefore leave
// navigator.requestMediaKeySystemAccess undefined on a plain http page served
// from a LAN or tailnet address, and Shaka can only say MISSING_EME_SUPPORT
// (6020). Checking here first means the page names the actual problem instead
// of quoting a DRM error code at someone standing in their living room.
const emeAvailable = typeof navigator.requestMediaKeySystemAccess === 'function';

// The codes worth translating. Everything else falls through to Shaka's own
// message, which is at least in English rather than a number.
const DRM_HINTS = {
  6001: 'deze browser heeft geen Widevine, of staat het niet toe op deze pagina',
  6002: 'de browser kon de CDM niet starten',
  6007: 'de licentieserver wees het verzoek af',
  6008: 'het antwoord van de licentieserver werd geweigerd',
  6020: 'Widevine werkt alleen via https of http://localhost',
};

function describe(detail) {
  const code = detail && detail.code;
  const hint = DRM_HINTS[code];
  if (hint) return 'afspelen mislukt: ' + hint + ' (code ' + code + ')';
  return 'afspelen mislukt: ' + ((detail && detail.message) || detail);
}

// A dead session looks like a network failure to Shaka, which is unhelpful.
// The daemon knows better: /api/status says whether the token on disk is still
// alive. Ask it, and when it is not, ask for a code instead of pointing at
// environment variables - the person reading this is holding a remote, not a
// shell.
let recovering = false;

async function explainFailure(detail) {
  try {
    const session = (await (await fetch(origin + '/api/status')).json()).session || {};
    if (session.authenticated === false) {
      if (!recovering && !pairingPromise) {
        recovering = true;
        waitForPairing().then((ok) => {
          recovering = false;
          // A reload is the shortest path back to a clean player: the channel
          // list, the profile token and the handshake are all one pairing
          // away from being wrong.
          if (ok) location.reload();
        });
      }
      return 'de sessie van nlzietd is verlopen. Koppel opnieuw in het venster hierboven; daarna herlaadt de pagina.';
    }
  } catch (err) {
    // The daemon is unreachable; the original error says more than this.
  }
  return describe(detail);
}

// -- pairing ---------------------------------------------------------------
// When there is no session, the page asks the daemon for a code and shows it,
// the way a TV does. That is not a nicety: the daemon may be on a shelf with
// nobody at a keyboard, and the person holding the NLZiet password may be on
// the sofa. Everything below is presentation; the state machine is server-side
// (`PairingService`), and this page only reads it.
const pairingEl = document.getElementById('pairing');
const qrBox = document.getElementById('qrbox');
const qrImg = document.getElementById('qrimg');
const codeEl = document.getElementById('paircode');
const linkEl = document.getElementById('pairlink');
const hintEl = document.getElementById('pairhint');
const stateEl = document.getElementById('pairstate');
const retryEl = document.getElementById('pairretry');

let retryClick = null;
retryEl.addEventListener('click', () => {
  const resolve = retryClick;
  retryClick = null;
  if (resolve) resolve();
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const minutes = (seconds) => Math.max(0, Math.ceil(seconds / 60));

async function sessionState() {
  return (await (await fetch(origin + '/api/status')).json()).session || {};
}

async function pairRequest(method) {
  const response = await fetch(origin + '/api/pair', { method });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || String(response.status));
  return body;
}

function showPairing() {
  pairingEl.hidden = false;
  video.hidden = true;
  say('');
}

function hidePairing() {
  pairingEl.hidden = true;
  video.hidden = false;
}

function renderPairing(view) {
  retryEl.hidden = true;
  if (view.state === 'pending' && view.code) {
    codeEl.textContent = view.code;
    linkEl.href = view.url;
    linkEl.textContent = view.url;
    // The image comes from the daemon, not from a client-side QR library: the
    // same code then reaches an M3U player through tvg-logo, and the page works
    // without a CDN.
    qrImg.src = origin + '/pair/qr.png?code=' + encodeURIComponent(view.code);
    qrBox.hidden = false;
    hintEl.textContent = 'Scan de code of open de link. Log in met je NLZIET-account en bevestig.';
    stateEl.textContent = 'nog ' + minutes(view.expires_in) + ' min geldig';
  } else if (view.state === 'paired') {
    hintEl.textContent = 'gekoppeld, de zenders worden geladen…';
    stateEl.textContent = '';
  } else if (view.state === 'expired') {
    hintEl.textContent = 'de code is verlopen, er komt zo een nieuwe…';
    stateEl.textContent = '';
  } else if (view.state === 'failed') {
    hintEl.textContent = 'koppelen is niet gelukt.';
    stateEl.textContent = view.detail || '';
    retryEl.hidden = false;
  } else {
    hintEl.textContent = 'koppelcode ophalen…';
  }
}

let pairingPromise = null;

function waitForPairing() {
  // One pairing at a time: the daemon allows exactly one live code, and two
  // callers would fight over which one is on screen.
  if (pairingPromise) return pairingPromise;
  pairingPromise = runPairing().then(
    (ok) => { pairingPromise = null; return ok; },
    (err) => {
      pairingPromise = null;
      hintEl.textContent = 'koppelen mislukt: ' + err.message;
      return false;
    },
  );
  return pairingPromise;
}

async function runPairing() {
  showPairing();
  let view = await pairRequest('POST');
  while (true) {
    renderPairing(view);
    if (view.state === 'paired') {
      // Tokens were saved and the session cache invalidated before the state
      // changed, so this read is confirmation rather than hope.
      const session = await sessionState().catch(() => ({}));
      if (session.authenticated) {
        hidePairing();
        return true;
      }
    } else if (view.state === 'expired') {
      await sleep(1000);
      view = await pairRequest('POST').catch(() => view);
      continue;
    } else if (view.state === 'failed') {
      // Wait for a click instead of retrying on its own: a failure that
      // repeats unattended is a daemon hammering the identity server.
      await new Promise((resolve) => { retryClick = resolve; });
      view = await pairRequest('POST').catch(() => view);
      continue;
    }
    await sleep(2500);
    view = await pairRequest('GET').catch(() => view);
  }
}

async function boot() {
  if (!window.shaka) {
    say('Shaka Player kon niet worden geladen (cdn.jsdelivr.net onbereikbaar). '
      + 'De streams zelf werken zonder internet naar de CDN van NLZiet.', true);
    return;
  }
  if (!emeAvailable) {
    explainInsecureContext();
    return;
  }
  shaka.polyfill.installAll();
  if (!shaka.Player.isBrowserSupported()) {
    say('Deze browser kan geen DASH/Widevine afspelen.', true);
    return;
  }
  try {
    if (!(await sessionState()).authenticated) {
      // Not being logged in is a state, not an error: a fresh install looks
      // exactly like this, and the page can get out of it by itself.
      if (!await waitForPairing()) return;
    }
  } catch (err) {
    // /api/status is unreachable; let the player try and report the real thing.
  }
  start();
}

// Someone reached us over plain http from another machine. Nothing is broken:
// the daemon answers, the manifest would be fine - the browser simply hides
// EME outside a secure context. Ask the daemon which port it serves https on
// and name it, so the fix is one click away instead of a manpage.
async function explainInsecureContext() {
  let httpsPort = '';
  try {
    const status = await (await fetch(origin + '/api/status')).json();
    if (status.https_port) httpsPort = ':' + status.https_port;
  } catch (err) {
    // /api/status is unauthenticated and local; if it fails the daemon is
    // gone and this message is the least of anyone's problems.
  }
  say('Deze pagina is niet beveiligd (http) en Widevine bestaat alleen in een '
    + 'beveiligde context. Open https://<deze-machine>' + httpsPort + '/ '
    + '(de naam die in het certificaat staat, bijvoorbeeld de Tailscale-naam) '
    + 'of, op deze machine zelf, http://localhost:' + (location.port || '8099') + '/.', true);
}

async function start() {
  const player = new shaka.Player();
  await player.attach(video);
  player.addEventListener('error', async (event) => {
    // detail.code is the only thing that says *why* (manifest, DRM, network).
    say(await explainFailure(event.detail), true);
  });

  let channels = [];
  try {
    const response = await fetch(origin + '/api/channels');
    const body = await response.json();
    if (!response.ok) {
      // The daemon is up but its catalogue is not: say which, instead of
      // showing "no channels" as if the subscription were empty.
      say('kan de zenderlijst niet ophalen: '
        + (body.detail || body.error || response.status), true);
      return;
    }
    channels = body;
  } catch (err) {
    say('kan de zenderlijst niet ophalen: ' + err, true);
    return;
  }
  if (!channels.length) {
    say('geen zenders in de catalogus', true);
    return;
  }

  for (const channel of channels) {
    const option = document.createElement('option');
    option.value = channel.id;
    option.textContent = channel.number + '. ' + channel.name;
    select.appendChild(option);
  }

  // Deep link: /?channel=npo1, otherwise the first channel.
  const wanted = new URLSearchParams(location.search).get('channel');
  const initial = channels.some((c) => c.id === wanted) ? wanted : channels[0].id;
  select.value = initial;
  select.addEventListener('change', () => load(select.value));
  load(initial);

  async function load(channelId) {
    // The licence endpoint is per channel because the licence token is bound
    // to one stream session; switching channels means a new one.
    player.configure({
      drm: { servers: { 'com.widevine.alpha': origin + '/licence/' + channelId } },
    });
    say('verbinden met ' + channelId + '…');
    try {
      await player.unload();
      await player.load(origin + '/manifest/' + channelId + '.mpd');
      say('speelt: ' + channelId);
      history.replaceState(null, '', '?channel=' + encodeURIComponent(channelId));
    } catch (err) {
      // load() rejects with the same shaka.util.Error the event carries, so it
      // gets the same translation - plus, when the failure is really "the
      // session died overnight", the one sentence that explains it.
      say(await explainFailure(err), true);
    }
  }
}

// Everything above is a declaration; this is the one entry point, so no `let`
// is read before it is initialised.
boot();
</script>
</body>
</html>
"""


def player_page() -> str:
    return PLAYER_PAGE


def pairing_playlist(base: str, view: dict, stream: bool = False) -> str:
    """The playlist an M3U player gets while there is no session.

    An M3U player can show a name, a logo and, when you press play, a stream.
    So the code goes in the name (first, because names get truncated), the QR in
    `tvg-logo`, and the entry points at the pairing *video* when ffmpeg is
    available - a full-screen card with the same QR, big enough to scan from a
    sofa. Without an encoder the entry falls back to the QR image itself:
    smaller, but still a code on the screen.

    Why the playlist mints the code instead of waiting for the page: whoever
    opens the playlist on a TV is the person who needs the code.
    """
    lines = ["#EXTM3U"]
    if view.get("code"):
        image = f"{base}/pair/qr.png"
        entry = f"{base}/pair/stream.m3u8" if stream else image
        lines.append(
            f'#EXTINF:-1 tvg-id="nlzietd-pair" tvg-name="NLZIET koppelen" '
            f'tvg-logo="{image}" group-title="NLZIET koppelen",'
            f'{view["code"]} · log in op id.nlziet.nl/device'
        )
        if stream:
            # These are plain H.264/TS, not DASH+Widevine: the KODIPROP lines
            # belong to the real channels, and a player must not try to ask for
            # a licence for a card it drew itself.
            lines.append("#KODIPROP:inputstream=inputstream.adaptive")
            lines.append("#KODIPROP:inputstream.adaptive.manifest_type=hls")
        lines.append(entry)
    else:
        # The daemon could not mint a code (identity server unreachable, or no
        # pairing support). Say so in the name instead of showing channel
        # entries that would all fail; the page has the details.
        lines.append(
            '#EXTINF:-1 tvg-id="nlzietd-pair" '
            'group-title="NLZIET koppelen",NLZIET niet gekoppeld · open de '
            'webpagina van nlzietd'
        )
        lines.append(f"{base}/")
    return "\n".join(lines) + "\n"


def playlist(base: str, channels: dict[str, Channel], android: bool = False) -> str:
    """An M3U for Kodi's `inputstream.adaptive` — and for Android apps that
    read the same `#KODIPROP` lines.

    `license_key` has two shapes. Kodi wants the four-field form
    `url|headers|request data|response`; empty headers and `R{SSM}|R` mean POST
    the raw challenge as the body and take the raw response back — exactly what
    our licence route speaks. Native Android TV players (OTT Navigator, Prysm)
    read those same `#KODIPROP` lines but want a *plain* licence URL: their
    ExoPlayer/MediaDrm already sends the raw challenge by itself. `android=True`
    emits that simpler form, so `?android=1` on the playlist URL serves them.
    """
    lines = ["#EXTM3U"]
    for channel in channels.values():
        lines.append(
            f'#EXTINF:-1 tvg-id="{channel.id}" tvg-name="{channel.name}" '
            f'tvg-logo="{channel.logo}" group-title="NLZiet",{channel.name}'
        )
        lines.append("#KODIPROP:inputstream=inputstream.adaptive")
        lines.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
        lines.append("#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha")
        licence = f"{base}/licence/{channel.id}"
        if not android:
            licence += "||R{SSM}|R"
        lines.append(f"#KODIPROP:inputstream.adaptive.license_key={licence}")
        lines.append(f"{base}/manifest/{channel.id}.mpd")
    return "\n".join(lines) + "\n"
