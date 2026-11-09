#!/bin/sh
# The container's start (beammp/README.md, "Docker"): ServerConfig.toml kept in /data (the host bundle's on first start;
# BeamMP itself applies BEAMMP_* to it and writes them back before the plugin reads it), the plugin's store, bake and
# spool kept in /data, BNGE_* set the plugin's time, weather and listing. BNGE_ROOT / BNGE_DATA: other folders (tests).
set -eu
root=${BNGE_ROOT:-/beammp}
data=${BNGE_DATA:-/data}
plugin=$root/Resources/Server/BeamNGEarth

fail() { echo "bnge-entrypoint: $*" >&2; exit 2; }
is() { case $2 in *'
'*) return 1;; esac; printf '%s' "$2" | grep -Eqx "$1"; }   # (grep -x matches line by line)

# every value checked before anything changes: a bad one stops here, not with a broken main.lua or config
time=${BNGE_TIME:-}; speed=${BNGE_TIME_SPEED:-}; weather=${BNGE_WEATHER:-}; listed=${BNGE_LISTED:-}
[ -z "$time" ] || is 'real|([01][0-9]|2[0-3]):[0-5][0-9]' "$time" || fail "BNGE_TIME must be real or HH:MM"
[ -z "$speed" ] || is '[0-9]+(\.[0-9]+)?' "$speed" || fail "BNGE_TIME_SPEED must be a number (0, 1, 60, ...)"
[ -z "$weather" ] || is 'real|clear|cloudy|rain|storm|snow|fog' "$weather" \
  || fail "BNGE_WEATHER must be real, clear, cloudy, rain, storm, snow or fog"
[ -z "$listed" ] || is 'true|false' "$listed" || fail "BNGE_LISTED must be true or false"

[ -f "$data/ServerConfig.toml" ] || cp "$root/ServerConfig.default.toml" "$data/ServerConfig.toml"
rm -f "$root/ServerConfig.toml"
ln -s "$data/ServerConfig.toml" "$root/ServerConfig.toml"
for d in store bake spool; do
  mkdir -p "$data/$d"
  rm -rf "${plugin:?}/$d"
  ln -s "$data/$d" "$plugin/$d"
done

# the plugin's settings near the top of main.lua, each its own line there
set_g() { sed -i "s|^G\.$1 = .*|G.$1 = $2|" "$plugin/main.lua"; }
[ -z "$time" ] || set_g TIME "'$time'"
[ -z "$speed" ] || set_g TIME_SPEED "$speed"
[ -z "$weather" ] || set_g WEATHER "'$weather'"
[ -z "$listed" ] || set_g LISTED "$listed"

cd "$root"
exec ./BeamMP-Server "$@"
