#!/bin/sh
# deploy_serverlist.sh: install a freshly built collector (native/serverlist) on the machine that runs it, as a
# systemd user service, and restart it. See hosting/README.md and docs/HOSTING.md.
#
#   BOX=user@host [PUBLIC_URL=https://collector.example.org] [AERIAL=on|off] [REPORTS_MAX_GB=2] \
#       hosting/deploy_serverlist.sh
#
#   BOX             where to install (SSH, key authentication; the user the service runs as)
#   PUBLIC_URL      the collector's public https address behind your reverse proxy; the checks at the end use it.
#                   Unset: the checks run on the machine itself, against 127.0.0.1:47640.
#   AERIAL          on (default): the aerial on-demand front door spools requests into ~/bnge-builder/aerial-spool,
#                   where the builder service (bnge-aerial-builder) takes them. off: the endpoint answers "refused".
#   REPORTS_MAX_GB  disk cap of the stored player reports (default 2)
#
# What it does on the machine:
#   - copies the binary in; the previous one is kept as bnge-serverlist.prev (roll back: move it back, restart);
#   - installs hosting/systemd/bnge-serverlist.service when no unit of that name is there yet;
#   - creates the private admin token once (~/bnge-serverlist/telemetry.env, mode 600; never printed);
#   - rewrites the drop-ins bnge-serverlist.service.d/{telemetry,limits}.conf (change this script, not those files);
#   - restarts the service and checks the public endpoints.
#
# Build first (a static binary runs on any x86-64 Linux):
#   cd native/serverlist && cargo build --release --target x86_64-unknown-linux-musl
# (the musl target needs a C compiler for it, e.g. zig as CC), or build on the machine itself and set BIN.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
: "${BOX:?set BOX=user@host (the machine that runs the collector)}"
PUBLIC_URL=${PUBLIC_URL:-}
AERIAL=${AERIAL:-on}
REPORTS_MAX_GB=${REPORTS_MAX_GB:-2}
BIN=${BIN:-$REPO/native/serverlist/target/x86_64-unknown-linux-musl/release/bnge-serverlist}
case "$AERIAL" in on|off) ;; *) echo "AERIAL must be on or off"; exit 1 ;; esac
case "$REPORTS_MAX_GB" in *[!0-9.]*|"") echo "REPORTS_MAX_GB must be a number"; exit 1 ;; esac
[ -x "$BIN" ] || { echo "no binary at $BIN: build it first (see the header of this script)"; exit 1; }

ssh -o BatchMode=yes "$BOX" 'mkdir -p ~/bnge-serverlist ~/.config/systemd/user'
scp -q "$BIN" "$BOX:bnge-serverlist/bnge-serverlist.new"
scp -q "$HERE/systemd/bnge-serverlist.service" "$BOX:bnge-serverlist/bnge-serverlist.service.template"

ssh -o BatchMode=yes "$BOX" "AERIAL=$AERIAL REPORTS_MAX_GB=$REPORTS_MAX_GB sh -s" <<'REMOTE'
set -eu
cd ~/bnge-serverlist
U=~/.config/systemd/user
# the private admin token: made once, kept on this machine only
[ -f telemetry.env ] || { umask 077; printf "BNGE_TELEMETRY_TOKEN=%s\n" "$(head -c 24 /dev/urandom | base64 | tr -d "/+=")" > telemetry.env; }
chmod 600 telemetry.env
mkdir -p telemetry reports "$U"; chmod 700 reports
[ -f "$U/bnge-serverlist.service" ] || cp bnge-serverlist.service.template "$U/bnge-serverlist.service"
mkdir -p "$U/bnge-serverlist.service.d"
SPOOL=""
if [ "$AERIAL" = on ]; then
    mkdir -p ~/bnge-builder/aerial-spool; chmod 700 ~/bnge-builder ~/bnge-builder/aerial-spool
    SPOOL="Environment=BNGE_AERIAL_SPOOL=%h/bnge-builder/aerial-spool"
fi
printf "[Service]\nEnvironmentFile=%%h/bnge-serverlist/telemetry.env\nEnvironment=BNGE_TELEMETRY_DIR=%%h/bnge-serverlist/telemetry\nEnvironment=BNGE_REPORTS_DIR=%%h/bnge-serverlist/reports\nEnvironment=BNGE_REPORTS_MAX_GB=%s\n%s\n" \
    "$REPORTS_MAX_GB" "$SPOOL" > "$U/bnge-serverlist.service.d/telemetry.conf"
printf "[Service]\nLimitNOFILE=65536\nRestartSec=3\nRestart=always\n" > "$U/bnge-serverlist.service.d/limits.conf"
[ ! -f bnge-serverlist ] || cp -p bnge-serverlist bnge-serverlist.prev
mv bnge-serverlist.new bnge-serverlist
systemctl --user daemon-reload
systemctl --user enable bnge-serverlist >/dev/null 2>&1 || true
systemctl --user restart bnge-serverlist
sleep 2
systemctl --user is-active bnge-serverlist
REMOTE

# The checks: public pages answer, the write endpoints refuse an empty body, the admin views refuse a missing token.
if [ -n "$PUBLIC_URL" ]; then
    U=${PUBLIC_URL%/}; run() { sh -c "$1"; }                         # from here, through the proxy
else
    U=http://127.0.0.1:47640; run() { ssh -o BatchMode=yes "$BOX" "$1"; }  # on the machine itself
fi
for p in servers stats players; do
    run "curl -s -o /dev/null -w '$p %{http_code} %{size_download} bytes\n' '$U/$p'"
done
run "curl -s -o /dev/null -w 'report (empty POST, want 400) %{http_code}\n' -X POST '$U/report'"
run "curl -s -o /dev/null -w 'stats/private (no token, want 401) %{http_code}\n' '$U/stats/private'"
run "curl -s -w ' aerial/status (want 200 {\"state\": ...})\n' '$U/aerial/status?cell=0,0'"
