# A BeamNG.Earth BeamMP server in a container (beammp/README.md, "Docker"): the Linux host bundle of tools/build_host.py (the
# official BeamMP binary, checked against its pinned digest, Earth's plugin and client mod, licenses and source), and
# curl for the plugin's server list announce. Build from the repository's root: docker build -f docker/Dockerfile .
FROM python:3.12-slim AS bundle
WORKDIR /src
COPY . .
RUN python tools/build_host.py --platform linux --out /dist \
 && python -m zipfile -e /dist/BeamNG.Earth-Host-*-Linux-x86_64.zip /host \
 && chmod 755 /host/BeamMP-Server /host/run_host.sh \
 && mv /host/ServerConfig.toml /host/ServerConfig.default.toml

FROM ubuntu:22.04
# (the binary is BeamMP's Ubuntu 22.04 build: libc and libstdc++ only)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 1000 --user-group --home-dir /beammp beammp \
 && mkdir /data && chown beammp:beammp /data
COPY --from=bundle --chown=beammp:beammp /host /beammp
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/bnge-entrypoint
ARG VERSION=dev
LABEL org.opencontainers.image.title="BeamNG.Earth BeamMP server" \
      org.opencontainers.image.description="BeamMP server with the BeamNG.Earth plugin and client mod" \
      org.opencontainers.image.source="https://github.com/TecnaGamer/BeamNG.Earth" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT AND AGPL-3.0-only"
WORKDIR /beammp
USER beammp
VOLUME /data
EXPOSE 30814/tcp 30814/udp
ENTRYPOINT ["tini", "--", "bnge-entrypoint"]
