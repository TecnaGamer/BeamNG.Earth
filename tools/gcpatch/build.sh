#!/bin/sh
# Build the preload library (README.md): tools/gcpatch/build/libbngearth_gcpatch.so
# (the grass patch bngearth_gcpatch.c, texkeep bngearth_texkeep.c, dtsmooth bngearth_dtsmooth.c with its filter
# bngearth_dtfilter.h: one library, a switch and a status file each). The filter's unit test runs first.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${BNGE_GCPATCH_OUT:-$HERE/build}
mkdir -p "$OUT"
${CC:-gcc} -O2 -Wall -Wextra -o "$OUT/test_dtfilter" "$HERE/test_dtfilter.c" -lm
"$OUT/test_dtfilter"
${CC:-gcc} -shared -fPIC -O2 -Wall -Wextra -fcf-protection=full -pthread -o "$OUT/libbngearth_gcpatch.so" \
    "$HERE/bngearth_gcpatch.c" "$HERE/bngearth_texkeep.c" "$HERE/bngearth_dtsmooth.c"
echo "built $OUT/libbngearth_gcpatch.so"
echo "Steam > BeamNG.drive > Properties > Launch options:"
echo "  LD_PRELOAD=\"$OUT/libbngearth_gcpatch.so:\$LD_PRELOAD\" %command%"
