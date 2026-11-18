// Unit test of the dtsmooth filter (bngearth_dtfilter.h): cc -O2 -o /tmp/t test_dtfilter.c -lm && /tmp/t
// (tools/gcpatch/build.sh runs it).
#include "bngearth_dtfilter.h"
#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails;
#define CHECK(c, ...) do { if (!(c)) { fails++; printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

static double g_dtreal;
static uint8_t g_xrobj[0x300];
static uint8_t *g_xrptr;

static void setup(struct bnge_dtf *f, double repay) {
    memset(f, 0, sizeof *f);
    bnge_dtf_defaults(f);
    f->repay = repay;
    f->dtreal = &g_dtreal;
    f->xr = &g_xrptr;
    f->xr_off = 0x204;
    f->enabled = 1;
    g_xrptr = NULL;
    g_dtreal = 0.010;
}

static unsigned rng = 12345;
static double urand(void) { rng = rng * 1103515245u + 12345u; return ((rng >> 8) & 0xffffff) / 16777216.0; }

int main(void) {
    // the layout the Windows launcher writes by offset (tools/gcpatch/enginepatch.py DTF_FIELDS)
    CHECK(offsetof(struct bnge_dtf, dtreal) == 0x28, "dtreal");
    CHECK(offsetof(struct bnge_dtf, xr) == 0x30, "xr");
    CHECK(offsetof(struct bnge_dtf, xr_off) == 0x38, "xr_off");
    CHECK(offsetof(struct bnge_dtf, n) == 0x3c, "n");
    CHECK(offsetof(struct bnge_dtf, enabled) == 0x40, "enabled");
    CHECK(offsetof(struct bnge_dtf, init) == 0x44, "init");
    CHECK(offsetof(struct bnge_dtf, top_n) == 0x48, "top_n");
    CHECK(offsetof(struct bnge_dtf, ring) == 0x50, "ring");
    CHECK(offsetof(struct bnge_dtf, typ) == 0xd0, "typ");
    CHECK(offsetof(struct bnge_dtf, head) == 0x150, "head");
    CHECK(offsetof(struct bnge_dtf, frames) == 0x190, "frames");
    CHECK(offsetof(struct bnge_dtf, repaid) == 0x1a8, "repaid");
    CHECK(sizeof(struct bnge_dtf) == 0x1b0, "size %zu", sizeof(struct bnge_dtf));

    struct bnge_dtf f;
    // steady frames: dtReal = the frame time
    setup(&f, 0.05);
    for (int i = 0; i < 200; i++) bnge_dtf_step(&f, 0.008);
    CHECK(fabs(g_dtreal - 0.008) < 1e-6, "steady %.6f", g_dtreal);

    // uneven frames: game time stays within a few ms of real time, no slow drift
    setup(&f, 0.0);
    double W = 0, G = 0, worst = 0;
    for (int i = 0; i < 20000; i++) {
        double raw = (i % 3 == 2 ? 0.015 : 0.0055) + ((i / 40) % 2 ? 0.004 : 0.0) + 0.002 * urand();
        bnge_dtf_step(&f, raw);
        W += raw; G += g_dtreal;
        if (i > 100 && fabs(G - W) > worst) worst = fabs(G - W);
    }
    CHECK(worst < 0.012, "locked to the clock (worst %.4f s)", worst);
    CHECK(f.capped == 0, "no frame of these capped (%llu)", (unsigned long long)f.capped);

    // a hitch, drop: its excess is lost, no frame runs fast by more than the cap's own share
    setup(&f, 0.0);
    for (int i = 0; i < 100; i++) bnge_dtf_step(&f, 0.008);
    bnge_dtf_step(&f, 0.088); double d1 = g_dtreal;
    bnge_dtf_step(&f, 0.178);
    double peak = 0, sum = 0;
    for (int i = 0; i < 100; i++) { bnge_dtf_step(&f, 0.008); if (g_dtreal > peak) peak = g_dtreal; }
    CHECK(d1 < 0.020, "the hitch frame capped (%.4f)", d1);
    CHECK(f.dropped > 0.2, "excess dropped (%.3f)", f.dropped);
    CHECK(peak < 0.016, "after the hitch: no catch-up burst (peak %.4f)", peak);

    // a hitch, repay 5 %: paid back at no more than ~1.05x (+ the capped share), over time
    setup(&f, 0.05);
    for (int i = 0; i < 100; i++) bnge_dtf_step(&f, 0.008);
    bnge_dtf_step(&f, 0.088); sum = g_dtreal - 0.088;
    bnge_dtf_step(&f, 0.178); sum += g_dtreal - 0.178;
    int fast = 0; peak = 0;
    for (int i = 0; i < 2000; i++) {
        bnge_dtf_step(&f, 0.008);
        if (i > 20 && g_dtreal > peak) peak = g_dtreal;
        if (g_dtreal > 0.008 * 1.06) fast++;
        sum += g_dtreal - 0.008;     // game time minus real time
    }
    CHECK(peak <= 0.008 * 1.051, "repaid slowly (peak x%.3f)", peak / 0.008);
    CHECK(f.debt < 1e-9 && f.repaid > 0.2 && f.dropped < 1e-9, "debt paid (repaid %.4f, dropped %.4f)", f.repaid, f.dropped);
    CHECK(fabs(sum) < 0.001, "all game time back (game - real %.4f s)", sum);
    CHECK(fast < 25, "few frames over 1.06x (%d)", fast);

    // a slower frame rate for good (30 fps): followed within a few frames
    setup(&f, 0.05);
    for (int i = 0; i < 100; i++) bnge_dtf_step(&f, 0.008);
    int k = -1;
    for (int i = 0; i < 100; i++) { bnge_dtf_step(&f, 0.033); if (k < 0 && g_dtreal > 0.031 && g_dtreal < 0.033 * 1.10) k = i; }
    CHECK(k >= 0 && k <= 10, "8 -> 33 ms followed in %d frames (the first frames repaid, the lag caught up)", k);

    // stock left alone: disabled, OpenXR session, NaN / > 1 s
    setup(&f, 0.05); f.enabled = 0; g_dtreal = 0.0123;
    bnge_dtf_step(&f, 0.5);
    CHECK(g_dtreal == 0.0123, "disabled: untouched");
    setup(&f, 0.05); g_xrptr = g_xrobj; g_xrobj[0x204] = 1; g_dtreal = 0.0123;
    bnge_dtf_step(&f, 0.5);
    CHECK(g_dtreal == 0.0123, "OpenXR: untouched");
    g_xrobj[0x204] = 0;
    bnge_dtf_step(&f, 0.007);
    CHECK(g_dtreal != 0.0123, "OpenXR object without a session: filtered");
    setup(&f, 0.05); g_dtreal = 0.0123;
    bnge_dtf_step(&f, NAN); CHECK(g_dtreal == 0.0123, "NaN: untouched");
    bnge_dtf_step(&f, 1.5); CHECK(g_dtreal == 0.0123, "> 1 s: untouched");

    // mode 2 with a cap of 2 s = the engine's own filter: the 2-frame hitch replayed (the pulse it removes)
    setup(&f, 0.0); f.enabled = 2; f.hitch_abs = 2.0; g_dtreal = 0.009;
    for (int i = 0; i < 300; i++) bnge_dtf_step(&f, 0.009);
    bnge_dtf_step(&f, 0.088); bnge_dtf_step(&f, 0.178);
    peak = 0;
    for (int i = 0; i < 60; i++) { bnge_dtf_step(&f, 0.009); if (g_dtreal > peak) peak = g_dtreal; }
    CHECK(peak > 0.016, "stock replays the hitch (peak %.4f)", peak);

    if (fails) { printf("%d failed\n", fails); return 1; }
    printf("test_dtfilter: ok\n");
    return 0;
}
