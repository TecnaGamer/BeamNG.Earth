#!/bin/sh
# Build the Windows terfast machine code (README.md "terfast") on Linux and write it into enginepatch_terfast_code.py
# (the launcher's copy). Needs gcc, ld, objcopy, nasm.
#   tools/gcpatch/win/build_terfast.sh            (re)write enginepatch_terfast_code.py
#   tools/gcpatch/win/build_terfast.sh --check    fail if it differs from a fresh build
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
OUT=${BNGE_BLOB_OUT:-$HERE/build}
mkdir -p "$OUT"
# freestanding, Win64 ABI entries (ms_abi), no red zone, SSE2 baseline, no calls to libc
gcc -O2 -std=c11 -Wall -Wextra -ffreestanding -fno-builtin -fno-tree-loop-distribute-patterns -fno-stack-protector \
    -fcf-protection=none -fno-asynchronous-unwind-tables -fno-unwind-tables -fPIC -mno-red-zone -march=x86-64 \
    -mtune=generic -fno-jump-tables -fno-math-errno -ffp-contract=off -c -o "$OUT/terfast_win.o" "$HERE/terfast_win.c"
if objdump -h "$OUT/terfast_win.o" | awk '$2 ~ /^\.(data|bss)/ && strtonum("0x" $3) > 0 {bad=1} END {exit !bad}'; then
    echo "build_terfast: the code has data / bss" >&2; exit 1
fi
if readelf -rW "$OUT/terfast_win.o" | grep -E 'R_X86_64_(64|32|32S|GOT|PLT)' >/dev/null; then
    echo "build_terfast: absolute / external relocations:" >&2
    readelf -rW "$OUT/terfast_win.o" >&2; exit 1
fi
ld -nostdlib -static --no-dynamic-linker -T "$HERE/terfast.ld" -o "$OUT/terfast_win.elf" "$OUT/terfast_win.o"
objcopy -O binary -j .text "$OUT/terfast_win.elf" "$OUT/terfast_win.bin"
nasm -f bin -o "$OUT/terfast_stub.bin" "$HERE/terfast_stub.asm"
nm "$OUT/terfast_win.elf" > "$OUT/terfast_win.nm"
python3 -I "$HERE/write_terfast.py" "$OUT/terfast_stub.bin" "$OUT/terfast_win.bin" "$OUT/terfast_win.nm" \
    "$ROOT/enginepatch_terfast_code.py" "$@"
