#!/bin/bash
# Headless A/B network measurement behind the cache proxy (proxy.sh): a base server (before) and this checkout's server
# (after, uncommitted changes included), each from an empty cache, through netprobe.py's places.
# Writes build/netbench/runs/NAME/{before,after}/ and NAME/report.md (ab_milestones.py, ab_bars.py, ab_changes.py,
# ab_hosts.py).
#
#   bench.sh NAME [--split]      --split: one server per place, NB_JOBS at once (each place from a cold cache)
#
#   NB_BASE_REF   the base commit (default ab-dev-baseline), checked out detached in build/netbench/base
#   NB_AFTER_REF  the after commit, checked out detached in build/netbench/after (default: this checkout as it is, so
#                 edits made while a run goes reach the servers started after them)
#   NB_JOBS       servers at once with --split (default 6): each takes ~10 GB of memory, and a server drops its caches
#                 once the machine has under 3 GB free (downloads again what it dropped: noise in the counts)
#   NB_LINK       MBIT:RTT_MS: a player's line (netprobe --link): the times are load times on it. Use a low NB_JOBS (2):
#                 servers sharing the CPU are slower; and NB_FRESH_BASE=1: a reused base ran beside other places than
#                 the after side does (its background photo analyses, CPU-bound, went at half the pace beside a city)
#   NB_UNTIL      a place ends once this part has loaded (netprobe --until: ground, roads, trees, far, loaded)
#   NB_PLACES     the places measured, by label (default all five)
#   NB_FRESH_BASE 1: measure the base again. Otherwise a base run is reused (build/netbench/base-runs/<key>) while its
#                 commit, the mode and the netbench code that shapes a run (netprobe, sitepatch, cacheproxy) are the same
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$(dirname "$HERE")")
DATA=${NB_DATA:-$REPO/build/netbench}
BASE=$DATA/base
NAME=${1:?usage: bench.sh NAME [--split]}
SPLIT=${2:-}
OUT=$DATA/runs/$NAME
PLACES=(lonepine=36.596718,-118.169991 hartford=41.7637,-72.6851 amsterdam=52.3730,4.8924 lyon=45.7640,4.8357
        sanfrancisco=37.8080,-122.4177)
SLOWEST_FIRST=(hartford amsterdam sanfrancisco lonepine lyon)     # --split's start order (the run ends sooner)
if [ -n "${NB_PLACES:-}" ]; then
  keep=" $NB_PLACES "
  PLACES=($(for p in "${PLACES[@]}"; do [[ $keep != *" ${p%%=*} "* ]] || echo "$p"; done))
  SLOWEST_FIRST=($(for p in "${SLOWEST_FIRST[@]}"; do [[ $keep != *" $p "* ]] || echo "$p"; done))
fi
PROBE_ARGS=(${NB_LINK:+--link "$NB_LINK"} ${NB_UNTIL:+--until "$NB_UNTIL"})
[ -e "$OUT" ] && { echo "$OUT exists: pick a new name"; exit 1; }
log() { echo "$(date +%H:%M:%S) $*"; }
rev() { echo "$(git -C "$1" rev-parse --short HEAD)$(git -C "$1" diff --quiet HEAD -- . ':!build' || echo +dirty)"; }
place() { for p in "${PLACES[@]}"; do [ "${p%%=*}" = "$1" ] && echo "$p"; done; }
checkout() {   # dir ref: a detached worktree of ref, the checkout's .venv in it
  if [ ! -d "$1" ]; then
    git -C "$REPO" worktree add --detach "$1" "$2"
    ln -s "$REPO/.venv" "$1/.venv"
  else
    git -C "$1" checkout -q --detach "$2"
  fi
}

checkout "$BASE" "${NB_BASE_REF:-ab-dev-baseline}"
AFTER=$REPO
if [ -n "${NB_AFTER_REF:-}" ]; then
  AFTER=$DATA/after
  checkout "$AFTER" "$NB_AFTER_REF"
fi
KEY=$(git -C "$BASE" rev-parse --short HEAD)-$(m=${SPLIT#--}; echo "${m:-session}")-$( (cat "$HERE/netprobe.py" \
      "$HERE/sitepatch/sitecustomize.py" "$HERE/cacheproxy.py" "$HERE/link.py"; echo "${PLACES[*]} ${PROBE_ARGS[*]}") \
      | sha256sum | cut -c1-10)
BASE_RUN=$DATA/base-runs/$KEY
[ "${NB_FRESH_BASE:-}" = 1 ] && rm -rf "$BASE_RUN"
[ -f "$BASE_RUN/complete" ] || rm -rf "$BASE_RUN"
declare -A ROOT=([before]=$BASE [after]=$AFTER) DEST=([before]=$BASE_RUN [after]=$OUT/after)
SIDES=(after)
[ -d "$BASE_RUN" ] || SIDES=(before after)
mkdir -p "$OUT"
for s in before after; do echo "$s $(rev "${ROOT[$s]}")" >> "$OUT/commits.txt"; done
"$HERE/proxy.sh" start
PROXY_ENV=$("$HERE/proxy.sh" env)
MISSES=$DATA/store/misses.log
MISSES0=$(wc -l < "$MISSES" 2>/dev/null || echo 0)

probe() {   # root out places...
  mkdir -p "$(dirname "$2")"
  env $PROXY_ENV python3 "$HERE/netprobe.py" --root "$1" --out "$2" "${PROBE_ARGS[@]}" --places "${@:3}" > "$2.out" 2>&1
}

log "$NAME: before = $(rev "$BASE") ($([ ${#SIDES[@]} = 1 ] && echo "reused: $KEY" || echo measured)), after = $(rev "$AFTER")"
fail=0
if [ "$SPLIT" = "--split" ]; then
  for label in "${SLOWEST_FIRST[@]}"; do     # (both builds of a place together: the same load for the pair)
    for s in "${SIDES[@]}"; do
      while [ "$(jobs -rp | wc -l)" -ge "${NB_JOBS:-6}" ]; do wait -n || fail=1; done
      probe "${ROOT[$s]}" "${DEST[$s]}/$label" "$(place "$label")" &
    done
  done
else
  for s in "${SIDES[@]}"; do probe "${ROOT[$s]}" "${DEST[$s]}" "${PLACES[@]}" & done
fi
while [ "$(jobs -rp | wc -l)" -gt 0 ]; do wait -n || fail=1; done
if [ "$SPLIT" = "--split" ]; then
  for s in "${SIDES[@]}"; do python3 "$HERE/merge.py" "${DEST[$s]}" "${PLACES[@]%%=*}" || fail=1; done
fi
if [ "$fail" = 0 ]; then
  [ "${SIDES[0]}" = before ] && touch "$BASE_RUN/complete"
else
  log "$NAME: a probe failed (see $OUT/after*.out, $BASE_RUN*.out)"
fi
ln -s "$BASE_RUN" "$OUT/before"

MISSES1=$(wc -l < "$MISSES" 2>/dev/null || echo 0)
{
  echo "# $NAME"; echo; sed 's/^/    /' "$OUT/commits.txt"; echo "    base run: $BASE_RUN"; echo
  echo "Upstream requests during this run (proxy misses): $((MISSES1 - MISSES0))"; echo
  echo "Line: ${NB_LINK:-none (network free)}; until: ${NB_UNTIL:-settled}"; echo
  AB_ROOT=$OUT python3 "$HERE/ab_milestones.py"; echo
  for s in before after; do
    echo "## $s: places"; echo; echo '```'; cat "$OUT/$s/places.jsonl"; echo '```'; echo
  done
  AB_ROOT=$OUT python3 "$HERE/ab_bars.py"; echo
  echo '```'; AB_ROOT=$OUT python3 "$HERE/ab_changes.py"; AB_ROOT=$OUT python3 "$HERE/ab_hosts.py"; echo '```'
} > "$OUT/report.md" 2>&1
log "$NAME: done, $((MISSES1 - MISSES0)) upstream requests, report $OUT/report.md"
