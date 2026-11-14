// Runs the Windows dtsmooth code (enginepatch_code.py's bytes, built by build_blob.sh) on Linux, laid out as the
// launcher lays it out in the game, behind a stand-in call site (test_stubs.asm): every register the Win64 ABI lets
// the callee take must reach it unchanged, every non-volatile one must survive, and dtReal must come out as the
// Linux library's filter computes it. test_blob.sh builds and runs it.
#include "../bngearth_dtfilter.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

extern void run_site(void *tramp, double raw, uint64_t *save);
extern void capture(void);

static int fails;
#define CHECK(c, ...) do { if (!(c)) { fails++; printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

static unsigned char *readall(const char *path, size_t *n) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(2); }
    static unsigned char buf[2][0x2000];
    static int k;
    *n = fread(buf[k], 1, sizeof buf[k], f);
    fclose(f);
    return buf[k++];
}

static double g_dtreal_blob, g_dtreal_ref;

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "test_blob TRAMP.bin FILTER.bin\n"); return 2; }
    size_t nt, nf;
    unsigned char *tr = readall(argv[1], &nt), *fi = readall(argv[2], &nf);
    unsigned char *blk = mmap(NULL, 0x2000, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (blk == MAP_FAILED) { perror("mmap"); return 2; }
    memcpy(blk, tr, nt);
    memcpy(blk + 0x100, fi, nf);
    uintptr_t orig = (uintptr_t)&capture;
    memcpy(blk + 0x1000, &orig, 8);
    struct bnge_dtf *f = (struct bnge_dtf *)(blk + 0x1010), ref;
    memset(f, 0, sizeof *f);
    bnge_dtf_defaults(f);
    f->dtreal = &g_dtreal_blob;
    f->enabled = 1;
    memcpy(&ref, f, sizeof ref);
    ref.dtreal = &g_dtreal_ref;
    g_dtreal_blob = g_dtreal_ref = 0.010;

    uint64_t save[40];
    unsigned rng = 7;
    double maxdiff = 0;
    for (int i = 0; i < 5000; i++) {
        rng = rng * 1103515245u + 12345u;
        double raw = 0.006 + ((rng >> 8) & 0xffff) / 65536.0 * 0.012 + (i % 700 == 699 ? 0.15 : 0.0);
        if (i == 3000) { f->repay = 0.0; ref.repay = 0.0; }
        if (i == 4000) { f->enabled = 2; ref.enabled = 2; f->hitch_abs = ref.hitch_abs = 0.010; }
        memset(save, 0, sizeof save);
        run_site(blk, raw, save);
        bnge_dtf_step(&ref, raw);
        double d = fabs(g_dtreal_blob - g_dtreal_ref);
        if (d > maxdiff) maxdiff = d;
        if (i < 3) {
            static const uint64_t vol[7] = {0x2000000000000007, 0x2000000000000001, 0x2000000000000002,
                                            0x2000000000000003, 0x2000000000000004, 0x2000000000000005,
                                            0x2000000000000006};
            for (int k = 0; k < 7; k++) CHECK(save[k] == vol[k], "volatile register %d reached the callee changed", k);
            double x0; memcpy(&x0, &save[7], 8);
            CHECK(x0 == raw, "xmm0 (raw) reached the callee: %g", x0);
            for (int k = 1; k <= 5; k++) { double x; memcpy(&x, &save[7 + k], 8); CHECK(x == 1000.0 + k, "xmm%d at the callee %g", k, x); }
            CHECK((save[13] & 15) == 8, "the callee entered with rsp = 8 mod 16 (%llx)", (unsigned long long)save[13]);
            for (int k = 0; k < 8; k++) CHECK(save[16 + k] == 0x1000000000000001ull + k, "non-volatile register %d lost", k);
            for (int k = 0; k < 10; k++) { double x; memcpy(&x, &save[24 + k], 8); CHECK(x == 1006.0 + k, "xmm%d lost: %g", 6 + k, x); }
        }
    }
    CHECK(maxdiff < 1e-12, "dtReal as the Linux filter (max diff %g)", maxdiff);
    CHECK(f->frames == 5000 && f->capped > 0, "the state advanced (%llu frames, %llu capped)",
          (unsigned long long)f->frames, (unsigned long long)f->capped);
    // left stock while disabled
    f->enabled = 0; g_dtreal_blob = 0.0123;
    run_site(blk, 0.5, save);
    CHECK(g_dtreal_blob == 0.0123, "disabled: dtReal untouched");
    if (fails) { printf("%d failed\n", fails); return 1; }
    printf("test_blob: ok (trampoline %zu bytes, filter %zu bytes, max diff %g)\n", nt, nf, maxdiff);
    return 0;
}
