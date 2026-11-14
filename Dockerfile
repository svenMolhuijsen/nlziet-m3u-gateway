# nlziet-m3u-gateway
#
# The daemon has no third-party Python packages: it is standard library only.
# ffmpeg is the one exception to "nothing to install", and it is optional at
# runtime - it encodes the pairing video channel. Baking it into the image is
# what makes a container behave like a desktop install, so a TV on the LAN gets
# a full-screen QR instead of a thumbnail.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# The token store and the catalogue cache must outlive the container. Creating
# /state before VOLUME also makes Docker initialise a named volume with the
# right ownership, so a non-root process can write tokens.json.
RUN groupadd --gid 10001 nlzietd \
 && useradd --uid 10001 --gid nlzietd --no-create-home nlzietd \
 && mkdir -p /state \
 && chown nlzietd:nlzietd /state

WORKDIR /app
COPY nlzietd/ ./nlzietd/
COPY README.md ./

ENV NLZIETD_STATE=/state \
    NLZIETD_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME /state
EXPOSE 8099 8443

USER nlzietd

# /healthz needs no session and no upstream, so this checks the process itself
# rather than whether the NLZiet account is currently reachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3)" || exit 1

ENTRYPOINT ["python", "-m", "nlzietd"]
