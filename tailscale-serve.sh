#!/bin/sh
# Start tailscaled, wait for the node to come up, then publish the gateway with
# Tailscale Serve - all in one container start. This is what lets section 2 of
# QUICKSTART be a single `docker compose up` with no hand-run second command.
#
# The official image's own entrypoint (containerboot) brings tailscaled up and
# authenticates with TS_AUTHKEY. We run it in the background, wait until the
# node reports Running, then enable Serve for the gateway's http port. Serve
# config lives in the node state volume, so a restart keeps it; we set it every
# start anyway because it is idempotent and cheap.
set -eu

# Port the daemon listens on inside this shared namespace (loopback http).
SERVE_PORT="${NLZIETD_PORT:-8099}"

# Hand off to the image's real entrypoint in the background so we can watch for
# the node to be ready and then configure Serve.
/usr/local/bin/containerboot &
boot_pid=$!

echo "nlziet: tailscaled starting, waiting for the node to come up..."

# Without TS_AUTHKEY, tailscaled prints a one-time browser login link instead of
# authenticating silently. Surface it loudly so nobody has to go dig a key out
# of the admin console: click the link, approve the node, done.
if [ -z "${TS_AUTHKEY:-}" ]; then
  echo "nlziet: no TS_AUTHKEY set - using interactive browser login."
  echo "nlziet: watch this log for a line like"
  echo "nlziet:   To authenticate, visit: https://login.tailscale.com/a/XXXXXXXX"
  echo "nlziet: open it, approve this node, and setup continues automatically."
fi

# Wait (up to ~2 min) for the node to reach the Running state. tailscale status
# exits non-zero until the backend is up, so this also covers the socket not
# existing yet. With interactive login this also waits for you to click the link.
i=0
until tailscale status --json 2>/dev/null | grep -q '"BackendState"[[:space:]]*:[[:space:]]*"Running"'; do
  i=$((i + 1))
  if [ "$i" -gt 300 ]; then
    echo "nlziet: node did not reach Running within 300s." >&2
    if [ -z "${TS_AUTHKEY:-}" ]; then
      echo "nlziet: did you open the login link above and approve the node?" >&2
    else
      echo "nlziet: check TS_AUTHKEY (expired/invalid?) and that MagicDNS + HTTPS certificates are enabled in the tailnet admin console." >&2
    fi
    # Keep the container alive on tailscaled so logs are inspectable.
    wait "$boot_pid"
    exit 1
  fi
  sleep 1
done

# Publish the gateway. --bg stores the config in node state and returns.
# Serve provisions and renews the *.ts.net certificate, giving browsers a
# secure context (EME/Widevine) with no certificate files in the daemon.
echo "nlziet: node is up; enabling Tailscale Serve for http://127.0.0.1:${SERVE_PORT}"
tailscale serve --bg "${SERVE_PORT}" || {
  echo "nlziet: 'tailscale serve' failed. The most common cause is HTTPS certificates not being enabled in the tailnet DNS settings." >&2
}

# Print the URL so success is visible in `docker compose logs`, and tell the
# user the ONE next step: open the page and pair the NLZiet account. Until that
# happens the gateway serves only the pairing channel, not the TV channels.
dns_name="$(tailscale status --json 2>/dev/null | grep -o '"DNSName"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/' | sed 's/\.$//')"
if [ -n "${dns_name:-}" ]; then
  url="https://${dns_name}/"
  echo ""
  echo "nlziet: =============================================================="
  echo "nlziet:  Tailscale is done. One step left: open this and pair NLZiet:"
  echo "nlziet:"
  echo "nlziet:      ${url}"
  echo "nlziet:"
  echo "nlziet:  Log in with your NLZiet account and confirm. The channel list"
  echo "nlziet:  then appears by itself. Playlist: ${url}playlist.m3u"
  echo "nlziet: =============================================================="
  echo ""
else
  echo "nlziet: Serve enabled, but could not read this node's name."
  echo "nlziet: Run 'tailscale status' to find the https URL, then open it and pair NLZiet."
fi

# Watch for the NLZiet account to get paired, then print the final "paste this
# into your IPTV app" block - the confirmation that it is actually ready. Runs
# in the background so it never holds up tailscaled. The daemon is reachable on
# loopback because both containers share this network namespace.
if [ -n "${dns_name:-}" ]; then
  (
    status_url="http://127.0.0.1:${SERVE_PORT}/api/status"
    playlist="https://${dns_name}/playlist.m3u"
    # Poll until authenticated true. No hard timeout: the user may pair hours
    # later, and a quiet watcher costs nothing.
    while true; do
      if tailscale status >/dev/null 2>&1; then :; fi
      body="$(wget -qO- "$status_url" 2>/dev/null || true)"
      case "$body" in
        *'"authenticated": true'*|*'"authenticated":true'*)
          echo ""
          echo "nlziet: =============================================================="
          echo "nlziet:  Paired - the gateway is ready. Put this one link in your"
          echo "nlziet:  IPTV app (Kodi: PVR IPTV Simple Client -> M3U -> Remote URL):"
          echo "nlziet:"
          echo "nlziet:      ${playlist}"
          echo "nlziet:"
          echo "nlziet:  It serves the right form for your app automatically (Kodi,"
          echo "nlziet:  OTT Navigator, Prysm). Or just open https://${dns_name}/ in a browser."
          echo "nlziet: =============================================================="
          echo ""
          break
          ;;
      esac
      sleep 5
    done
  ) &
fi

# Stay attached to tailscaled; when it exits, the container exits.
wait "$boot_pid"
