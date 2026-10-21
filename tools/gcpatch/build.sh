#!/bin/sh
# Build the grass-keeping preload library (README.md): tools/gcpatch/build/libbngearth_gcpatch.so
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HERE/build"
${CC:-gcc} -shared -fPIC -O2 -Wall -Wextra -o "$HERE/build/libbngearth_gcpatch.so" "$HERE/bngearth_gcpatch.c"
echo "built $HERE/build/libbngearth_gcpatch.so"
echo "Steam > BeamNG.drive > Properties > Launch options:"
echo "  LD_PRELOAD=\"$HERE/build/libbngearth_gcpatch.so:\$LD_PRELOAD\" %command%"
