#!/bin/sh
# Build the Windows dtsmooth code, check enginepatch_code.py holds exactly it, and run it on Linux behind a stand-in
# call site (test_blob.c). Needs gcc, ld, objcopy, nasm.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${BNGE_BLOB_OUT:-$HERE/build}
BNGE_BLOB_OUT=$OUT sh "$HERE/build_blob.sh" --check
nasm -f elf64 -o "$OUT/test_stubs.o" "$HERE/test_stubs.asm"
gcc -O2 -Wall -Wextra -o "$OUT/test_blob" "$HERE/test_blob.c" "$OUT/test_stubs.o" -lm -z noexecstack
"$OUT/test_blob" "$OUT/dtsmooth_tramp.bin" "$OUT/dtsmooth_filter.bin"
