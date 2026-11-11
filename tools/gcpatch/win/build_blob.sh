#!/bin/sh
# Build the Windows dtsmooth machine code (README.md "Windows") on Linux and write it into enginepatch_code.py (the
# launcher's copy; checked by tools/gcpatch/win/test_blob.sh). Needs gcc, ld, objcopy, nasm.
#   tools/gcpatch/win/build_blob.sh            (re)write enginepatch_code.py
#   tools/gcpatch/win/build_blob.sh --check    fail if enginepatch_code.py differs from a fresh build
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
OUT=${BNGE_BLOB_OUT:-$HERE/build}
mkdir -p "$OUT"
# freestanding, Win64 ABI entry (ms_abi), no red zone (Windows has none), SSE2 baseline, no calls to libc
gcc -O2 -std=c11 -Wall -Wextra -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-stack-protector \
    -fcf-protection=none -fno-asynchronous-unwind-tables -fno-unwind-tables -fPIC -mno-red-zone -march=x86-64 \
    -mtune=generic -fno-jump-tables -c -o "$OUT/dtsmooth_filter.o" "$HERE/dtsmooth_filter.c"
# only rip-relative references inside the code: no data, no bss, no absolute relocations, no calls out
if objdump -h "$OUT/dtsmooth_filter.o" | awk '$2 ~ /^\.(data|bss)/ && strtonum("0x" $3) > 0 {bad=1} END {exit !bad}'; then
    echo "build_blob: the filter has data / bss" >&2; exit 1
fi
if readelf -rW "$OUT/dtsmooth_filter.o" | grep -E 'R_X86_64_(64|32|32S|GOT|PLT)' >/dev/null; then
    echo "build_blob: the filter has absolute / external relocations:" >&2
    readelf -rW "$OUT/dtsmooth_filter.o" >&2; exit 1
fi
ld -nostdlib -static --no-dynamic-linker -T "$HERE/blob.ld" -o "$OUT/dtsmooth_filter.elf" "$OUT/dtsmooth_filter.o"
objcopy -O binary -j .text "$OUT/dtsmooth_filter.elf" "$OUT/dtsmooth_filter.bin"
nasm -f bin -o "$OUT/dtsmooth_tramp.bin" "$HERE/dtsmooth_tramp.asm"
# the entry at the block's FILTER offset (0x100)
nm "$OUT/dtsmooth_filter.elf" | grep -q '^0000000000000100 T bnge_dtf_win$' || { echo "build_blob: entry not at 0x100" >&2; exit 1; }
python3 -I "$HERE/write_code.py" "$OUT/dtsmooth_tramp.bin" "$OUT/dtsmooth_filter.bin" "$ROOT/enginepatch_code.py" "$@"
