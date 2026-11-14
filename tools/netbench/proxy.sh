#!/bin/bash
# The measurement cache proxy (cacheproxy.py): start | stop | status. One proxy, one store, shared by every run.
#   NB_DATA (default build/netbench: store/ the answers and misses.log, mitm-conf/ the proxy's CA and log),
#   NB_PROXY_PORT (default 8899)
# Prints the environment a server needs to use it (HTTPS_PROXY, SSL_CERT_FILE, ...) with `env`.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DATA=${NB_DATA:-$(dirname "$(dirname "$HERE")")/build/netbench}
STORE=$DATA/store
PORT=${NB_PROXY_PORT:-8899}
CONF=$DATA/mitm-conf
BUNDLE=$CONF/bundle.pem
PIDF=$CONF/mitmdump.pid
SYS_CA=/etc/ssl/certs/ca-certificates.crt

running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

case "${1:-status}" in
  start)
    running && { echo "proxy already running (pid $(cat "$PIDF"))"; exit 0; }
    mkdir -p "$STORE" "$CONF"
    setsid uv tool run --from mitmproxy mitmdump --set flow_detail=0 --listen-host 127.0.0.1 -p "$PORT" \
      --set confdir="$CONF" --set connection_strategy=lazy --set cache_dir="$STORE" \
      -s "$HERE/cacheproxy.py" >> "$CONF/mitmdump.log" 2>&1 < /dev/null &
    echo $! > "$PIDF"
    for _ in $(seq 60); do [ -f "$CONF/mitmproxy-ca-cert.pem" ] && ss -ltn | grep -q "127.0.0.1:$PORT " && break; sleep 0.5; done
    ss -ltn | grep -q "127.0.0.1:$PORT " || { echo "proxy did not start (see $CONF/mitmdump.log)"; exit 1; }
    cat "$SYS_CA" "$CONF/mitmproxy-ca-cert.pem" > "$BUNDLE"
    echo "proxy up on 127.0.0.1:$PORT, store $STORE"
    ;;
  stop)
    running && { kill -INT "$(cat "$PIDF")"; sleep 2; }
    rm -f "$PIDF"; echo "proxy stopped"
    ;;
  status)
    running && echo "running (pid $(cat "$PIDF")), store $STORE, $(wc -l < "$STORE/misses.log" 2>/dev/null || echo 0) misses so far" \
      || echo "not running"
    ;;
  env)
    echo "HTTPS_PROXY=http://127.0.0.1:$PORT HTTP_PROXY=http://127.0.0.1:$PORT NO_PROXY=127.0.0.1,localhost" \
         "SSL_CERT_FILE=$BUNDLE REQUESTS_CA_BUNDLE=$BUNDLE"
    ;;
  *) echo "usage: $0 start|stop|status|env"; exit 2 ;;
esac
