// dtsmooth: no game-speed pulse after a hitch (BeamNG.Earth; README.md). Part of the same preload library as
// bngearth_gcpatch.c (one LD_PRELOAD; its own switch and status file).
//
// BeamNG's FrameTime::update (Linux 0.40: 0x120ad00, once per frame) turns the measured frame time `raw` into the
// smoothed dtReal that physics and game time advance by. It keeps a "time debt" (game time vs real time): after a
// hitch (a 100 ms frame) dtReal first lags, then the filter repays the whole hitch, running the game at up to ~2x
// speed for ~36 frames. At speed that reads as a pulse (shadow-cache-pulse / time-pulsing notes). This patch feeds
// the filter s = min(raw, dtReal_prev + C) instead of raw: a hitch longer than the previous dtReal + C enters the
// filter (and its debt, history and the main-loop timer it advances) only as + C, so game time drops the excess
// instead of replaying it. dtRaw (Lua's raw frame time) stays raw. C = 10 ms (BNGEARTH_DTSMOOTH_C, seconds, for
// tests). With C >= 1.0 the patched code computes exactly what the stock code does (raw <= 1.0), so on/off is one
// aligned 8-byte store of C: the code is patched once at startup, before the game's own code runs, with C = 2.0
// (stock), and a thread sets C while the BeamNG.Earth level flag exists (other maps: stock).
//
// The 95 bytes 0x120ad1c..0x120ad7b (raw = fmin(1, t); dtRaw = raw; e = clamp(prev - raw, -0.1, 0.1) with a
// comisd/ja to a cold block) become (minsd raw,1.0; dtRaw = raw; s = min(prev + C, raw); e = clamp(prev - s, -0.1,
// 0.1); the rest as before with s in raw's register). C lives in that cold block (0x120aed0: `movapd xmm0,xmm1; jmp
// back`), only reachable from the replaced ja. Another build: the region is found by its instructions (rip
// displacements and the call masked; one match only) and the patch is built from that build's own addresses;
// otherwise nothing is patched. Opt-in: BNGEARTH_DTSMOOTH=1 (BNGEARTH_DTSMOOTH_OFF=1 wins). Status: <user>/temp/beamngEarth_dtsmooth.txt.
#include "bngearth_common.h"
#include <pthread.h>
#include <sys/mman.h>

#define TAG "bngearth-dtsmooth"
#define STATUS_FILE "beamngEarth_dtsmooth.txt"
#define REGION 0x120ad1cUL                 // BeamNG.drive 0.40 Linux (sha256 a365912e...)
#define REGION_LEN 0x5f
#define C_STOCK 2.0
#define C_DEFAULT 0.010

// The stock region, 0xff = masked (rip displacements, the fmin call, the ja's rel32)
static const unsigned char SIG[REGION_LEN] = {
    0x66, 0x0f, 0x28, 0xc8,                                  // +00 movapd xmm1,xmm0             (t)
    0xf2, 0x0f, 0x10, 0x05, 0xff, 0xff, 0xff, 0xff,          // +04 movsd  xmm0,[1.0]
    0xe8, 0xff, 0xff, 0xff, 0xff,                            // +0c call   fmin                  raw = fmin(1, t)
    0xf2, 0x0f, 0x10, 0x35, 0xff, 0xff, 0xff, 0xff,          // +11 movsd  xmm6,[dtReal]         prev
    0xf2, 0x0f, 0x10, 0x0d, 0xff, 0xff, 0xff, 0xff,          // +19 movsd  xmm1,[-0.1]
    0x66, 0x0f, 0x28, 0xd0,                                  // +21 movapd xmm2,xmm0             raw
    0xf2, 0x0f, 0x11, 0x05, 0xff, 0xff, 0xff, 0xff,          // +25 movsd  [dtRaw],xmm0
    0xf2, 0x0f, 0x10, 0x1d, 0xff, 0xff, 0xff, 0xff,          // +2d movsd  xmm3,[0.999]
    0xf2, 0x0f, 0x59, 0x1d, 0xff, 0xff, 0xff, 0xff,          // +35 mulsd  xmm3,[debt]
    0x66, 0x0f, 0x28, 0xc6,                                  // +3d movapd xmm0,xmm6
    0xf2, 0x0f, 0x5c, 0xc2,                                  // +41 subsd  xmm0,xmm2             e = prev - raw
    0x66, 0x0f, 0x2f, 0xc8,                                  // +45 comisd xmm1,xmm0
    0x0f, 0x87, 0xff, 0xff, 0xff, 0xff,                      // +49 ja     cold (e = -0.1)
    0xf2, 0x0f, 0x10, 0x0d, 0xff, 0xff, 0xff, 0xff,          // +4f movsd  xmm1,[0.1]
    0xf2, 0x0f, 0x5d, 0xc8,                                  // +57 minsd  xmm1,xmm0
    0x66, 0x0f, 0x28, 0xc1                                 // +5b movapd xmm0,xmm1             e
};
static const unsigned char SIG_MASK_FROM[] = {0x08, 0x0d, 0x15, 0x1d, 0x29, 0x31, 0x39, 0x4b, 0x53};  // 4-byte holes

static int masked(size_t i) {
    for (size_t k = 0; k < sizeof SIG_MASK_FROM; k++)
    {
        if (i >= SIG_MASK_FROM[k] && i < (size_t)SIG_MASK_FROM[k] + 4) 
        {
            return 1;
        }
    }
    return 0;
}

static int matches(const unsigned char *p) {
    for (size_t i = 0; i < REGION_LEN; i++)
    {
        if (!masked(i) && p[i] != SIG[i]) 
        {
            return 0;
        }   
    }
    
    return 1;
}

static uintptr_t rip_target(const unsigned char *p, size_t disp_off, size_t insn_end) {
    int32_t d;
    memcpy(&d, p + disp_off, 4);
    return (uintptr_t)(p + insn_end) + (intptr_t)d;
}

// Append an instruction `op` + rip disp32 to `target`.
static void emit_rip(unsigned char *out, size_t *k, const unsigned char *region, const unsigned char *op, size_t n, uintptr_t target) {
    memcpy(out + *k, op, n);
    *k += n;
    int32_t d = (int32_t)((intptr_t)target - (intptr_t)(region + *k + 4));
    memcpy(out + *k, &d, 4);
    *k += 4;
}
static void emit(unsigned char *out, size_t *k, const unsigned char *b, size_t n) { 
    memcpy(out + *k, b, n); *k += n; 
}

// The patched region for `region` (a match of SIG) into code[]; C's slot (the cold block) into *cold. 0 + *why if the
// surroundings are not as expected.
static int build_patch(const unsigned char *region, unsigned char *code, uintptr_t *cold_out, const char **why) {
    uintptr_t one = rip_target(region, 0x08, 0x0c);
    uintptr_t dtreal = rip_target(region, 0x15, 0x19);
    uintptr_t m01 = rip_target(region, 0x1d, 0x21);
    uintptr_t dtraw = rip_target(region, 0x29, 0x2d);
    uintptr_t k999 = rip_target(region, 0x31, 0x35);
    uintptr_t debt = rip_target(region, 0x39, 0x3d);
    uintptr_t cold = rip_target(region, 0x4b, 0x4f);
    uintptr_T p01 = rip_target(region, 0x53, 0x57);
    // the cold block: movapd xmm0,xmm1; jmp rel32 back to the region's end; 8-byte aligned (C's slot)
    const unsigned char *cb = (const unsigned char *)cold;
    if ((cold & 7) || cb[0] != 0x66 || cb[1] != 0x0f || cb[2] != 0x28 || cb[3] != 0xc1 || cb[4] != 0xe9 || rip_target(cb, 5, 9) != (uintptr_t)(region + REGION_LEN)) {
        *why = "the filter's cold block differs";
        return 0;
    }
    double c1, cm, cp, ck;      // the constants it uses
    memcpy(&c1, (void *)one, 8); memcpy(&cm, (void *)m01, 8); memcpy(&cp, (void *)p01, 8); memcpy(&ck, (void *)k999, 8);
    if (c1 != 1.0 || cm != -0.1 || cp != 0.1 || ck != 0.999) { 
        *why = "the filter's constants differ"; 
        return 0;
    }
    size_t k = 0;
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x5d\x05", 4, one);     // minsd  xmm0,[1.0]    raw
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x11\x05", 4, dtraw);   // movsd  [dtRaw],xmm0
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x10\x35", 4, dtreal);  // movsd  xmm6,[dtReal] prev
    emit(code, &k, (const unsigned char *)"\x66\x0f\x28\xd6", 4);                      // movapd xmm2,xmm6
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x58\x15", 4, cold);    // addsd  xmm2,[C]
    emit(code, &k, (const unsigned char *)"\xf2\x0f\x5d\xd0", 4);                      // minsd  xmm2,xmm0     s
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x10\x1d", 4, k999);    // movsd  xmm3,[0.999]
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x59\x1d", 4, debt);    // mulsd  xmm3,[debt]
    emit(code, &k, (const unsigned char *)"\x66\x0f\x28\xc6", 4);                      // movapd xmm0,xmm6
    emit(code, &k, (const unsigned char *)"\xf2\x0f\x5c\xc2", 4);                      // subsd  xmm0,xmm2     e
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x5f\x05", 4, m01);     // maxsd  xmm0,[-0.1]
    emit_rip(code, &k, region, (const unsigned char *)"\xf2\x0f\x5d\x05", 4, p01);     // minsd  xmm0,[0.1]
    emit(code, &k, (const unsigned char *)"\x0f\x1f\x84\x00\x00\x00\x00\x00", 8);      // nop8
    emit(code, &k, (const unsigned char *)"\x0f\x1f\x80\x00\x00\x00\x00", 7);          // nop7
    if (k != REGION_LEN) { 
        *why = "patch size mismatch (bug)"; 
        return 0;
    }
    *cold_out = cold;
    return 1;
}

static volatile double *g_c;     // C (inside the game's code page)
static double g_c_on = C_DEFAULT;

static int store_c(double v) {
    long pagesz = sysconf(_SC_PAGESIZE);
    uintptr_t page = (uintptr_t)g_c & ~(uintptr_t)(pagesz - 1);
    if (mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) 
    {
        return 0;
    }
    
    uint64_t bits;
    memcpy(&bits, &v, 8);
    __atomic_store_n((volatile uint64_t *)g_c, bits, __ATOMIC_RELEASE);   // one aligned 8-byte store
    mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_EXEC);
    return 1;
}

static void *watch(void *arg) {
    const char *how = arg;
    char flag[512];
    bnge_temp_path(flag, sizeof flag, BNGE_LEVEL_FLAG);
    int on = 0;
    for (;;) {
        int want = access(flag, F_OK) == 0;
        if (want != on) {
            if (!store_c(want ? g_c_on : C_STOCK)) { bnge_report(TAG, STATUS_FILE, "stopped: mprotect refused"); return NULL; }
            on = want;
            char msg[256];
            if (on) 
            {
                snprintf(msg, sizeof msg, "applied: game time drops hitches instead of catching up, C = %.1f ms " "(BeamNG.Earth level)%s", g_c_on * 1000, how);
            }
            else 
            {
                snprintf(msg, sizeof msg, "stock: another level");
            }
            bnge_report(TAG, STATUS_FILE, msg);
        }
        struct timespec ts = {0, BNGE_FLAG_POLL_MS * 1000000L};
        nanosleep(&ts, NULL);
    }
    return NULL;
}

__attribute__((constructor)) static void bngearth_dtsmooth(void) {
    if (!bnge_is_main_game()) return;
    // (opt-in for now, BNGEARTH_DTSMOOTH=1: it ends the fast-forward after a hitch, but the hitch's time is lost instead,
    // game time 0.963-0.967 of real time over a Manhattan minute against 0.979-0.987 stock, 2026-11-03; to be tuned)
    const char *on = getenv("BNGEARTH_DTSMOOTH");
    const char *off = getenv("BNGEARTH_DTSMOOTH_OFF");
    if (!on || !*on || !strcmp(on, "0") || (off && *off && strcmp(off, "0"))) {
        bnge_report(TAG, STATUS_FILE, "off (opt-in: BNGEARTH_DTSMOOTH=1)");
        return;
    }
    const char *cs = getenv("BNGEARTH_DTSMOOTH_C");
    if (cs && *cs) {
        double v = atof(cs);
        if (v > 0 && v <= C_STOCK) g_c_on = v;
    }
    struct bnge_main m;
    bnge_find_main(&m);
    if (!m.seen) { 
        bnge_report(TAG, STATUS_FILE, "not applied: the game's code was not found"); 
        return; 
    }

    unsigned char *region = NULL;
    const char *how = "";
    if (bnge_mapped(&m, REGION, REGION_LEN, PF_R | PF_X) && matches((unsigned char *)(m.base + REGION)))
    {
        region = (unsigned char *)(m.base + REGION);
    }
    else {
        int n = 0;
        for (int s = 0; s < m.nseg; s++) {
            if (!(m.seg[s].flags & PF_X)) 
            {
                continue;
            }
            for (unsigned char *p = (unsigned char *)m.seg[s].start; p + REGION_LEN <= (unsigned char *)m.seg[s].end; p++) {
                if (p[0] != 0x66 || p[1] != 0x0f || p[2] != 0x28 || p[3] != 0xc8 || p[4] != 0xf2) 
                {
                    continue;
                }
                if (matches(p)) { 
                    region = p; 
                    n++; 
                }
            }
        }
        if (n != 1) {
            char msg[128];
            snprintf(msg, sizeof msg, "not applied: the frame-time filter was not found in this build (%d matches); stock", n);
            bnge_report(TAG, STATUS_FILE, msg);
            return;
        }
        how = " (found by its signature in this build)";
    }
    if (!bnge_mapped(&m, rip_target(region, 0x4b, 0x4f) - m.base, 9, PF_R | PF_X)) {
        bnge_report(TAG, STATUS_FILE, "not applied: the filter's cold block is outside the code; stock");
        return;
    }
    unsigned char code[REGION_LEN];
    uintptr_t cold = 0;
    const char *why = "";
    if (!build_patch(region, code, &cold, &why)) {
        char msg[160];
        snprintf(msg, sizeof msg, "not applied: %s; stock", why);
        bnge_report(TAG, STATUS_FILE, msg);
        return;
    }

    // Patch now (the constructor: the game's own code has not started), C first so the new code reads stock.
    long pagesz = sysconf(_SC_PAGESIZE);
    uintptr_t lo = (uintptr_t)region & ~(uintptr_t)(pagesz - 1);
    uintptr_t hi = (cold + 8 > (uintptr_t)region + REGION_LEN ? cold + 8 : (uintptr_t)region + REGION_LEN);
    uintptr_t lo2 = cold < (uintptr_t)region ? (cold & ~(uintptr_t)(pagesz - 1)) : lo;
    size_t len = ((hi + pagesz - 1) & ~(uintptr_t)(pagesz - 1)) - lo2;
    if (mprotect((void *)lo2, len, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        bnge_report(TAG, STATUS_FILE, "not applied: mprotect refused; stock");
        return;
    }
    double stock = C_STOCK;
    memcpy((void *)cold, &stock, 8);
    memcpy(region, code, REGION_LEN);
    __builtin___clear_cache((char *)region, (char *)region + REGION_LEN);
    mprotect((void *)lo2, len, PROT_READ | PROT_EXEC);
    g_c = (volatile double *)cold;

    pthread_t t;
    if (pthread_create(&t, NULL, watch, (void *)how) != 0) {
        bnge_report(TAG, STATUS_FILE, "patched but no thread for the level flag: stock (C = 2.0)");
        return;
    }
    pthread_detach(t);
    char msg[160];
    snprintf(msg, sizeof msg, "ready: applied while the BeamNG.Earth level runs%s", how);
    bnge_report(TAG, STATUS_FILE, msg);
}
