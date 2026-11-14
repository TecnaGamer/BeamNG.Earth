#!/bin/sh
# Build the Windows terfast code, check enginepatch_terfast_code.py holds exactly it, and run it on Linux on stand-in
# terrain cells against the engine's loops written out sample by sample (test_terfast.c). Needs gcc, ld, objcopy, nasm.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${BNGE_BLOB_OUT:-$HERE/build}
BNGE_BLOB_OUT=$OUT sh "$HERE/build_terfast.sh" --check
gcc -O2 -Wall -Wextra -ffp-contract=off -o "$OUT/test_terfast" "$HERE/test_terfast.c" -lm
sym() { awk -v s="$1" '$3 == s { print "0x" $1 }' "$OUT/terfast_win.nm"; }
"$OUT/test_terfast" "$OUT/terfast_stub.bin" "$OUT/terfast_win.bin" "$(sym tf_bounds)" "$(sym tf_mats)" "$(sym tf_holes)"
