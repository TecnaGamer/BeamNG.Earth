// dtsmooth for the Windows game: the filter of bngearth_dtfilter.h as one Win64-ABI function, built on Linux into
// position-independent machine code (build_blob.sh: gcc -ffreestanding, ms_abi, no red zone, no libc; .text and
// .rodata flat, rip-relative only) that the launcher writes into the game's memory (enginepatch.py).
#include "../bngearth_dtfilter.h"

__attribute__((ms_abi, used, section(".text.entry"))) void bnge_dtf_win(struct bnge_dtf *f, double raw) {
    bnge_dtf_step(f, raw);
}
