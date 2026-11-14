// dtsmooth: steady game speed (BeamNG.Earth; README.md). Part of the same preload library as bngearth_gcpatch.c
// (one LD_PRELOAD; its own switch and status file).
//
// BeamNG's FrameTime::update (Linux 0.40: one caller, the main loop) turns the measured frame time `raw` into the
// smoothed dtReal that physics and game time advance by. Its filter tracks frame times slowly and keeps a "time
// debt": while our streaming makes frame times uneven the game runs at 0.84-1.19x speed over quarter seconds, and
// after a hitch it replays the whole hitch at up to ~2x speed for ~0.5 s (the "pulse"). bngearth_dtfilter.h has the
// replacement filter; this file runs it once a frame right after the engine's own, overwriting dtReal.
//
// The hook: FrameTime::update ends in a tail call `jmp <MainLoopTimer add>(raw)` (e9 rel32, xmm0 = raw), after
// dtReal and the OpenXR override are stored. Its rel32 is pointed at a 14-byte stub (`jmp [rip]` + our trampoline's
// address) in a page mapped within +-2 GB of the game's code; the trampoline saves the argument registers, calls
// bnge_dt_hook(raw) and jumps on to the original target. One 4-byte store at startup (in this constructor, before the
// game's code runs); the filter itself is switched by g_dtf.enabled (one aligned store) while the BeamNG.Earth level
// flag exists, so other maps keep the stock dtReal.
//
// The function is found by the instructions of its start (rip displacements and calls masked; one match only) and
// its tail by its own instructions within 0x300 bytes after (the stores to the same dtReal, the OpenXR test); in any
// other shape nothing is patched. On by default (2026-11-11 measurements in README.md); BNGEARTH_DTSMOOTH_OFF=1 or
// BNGEARTH_DTSMOOTH=0 turns it off. Status: <user>/temp/beamngEarth_dtsmooth.txt.
//
// Tests (env): BNGEARTH_DTSMOOTH_REPAY / _HITCH (s) / _TAU (frames) / _N / _MODE (2: the stock filter with the old
// C cap, for A/B); BNGEARTH_DTSMOOTH_CTL=1 reads <user>/temp/beamngearth_dtsmooth_ctl every 250 ms
// ("mode hitch_abs hitch_rel tau repay debt_max n [top_n]") so one session can switch between variants (tools/framerec).
#include "bngearth_common.h"
#include "bngearth_dtfilter.h"
#include <pthread.h>
#include <sys/mman.h>

#define TAG "bngearth-dtsmooth"
#define STATUS_FILE "beamngEarth_dtsmooth.txt"
#define CTL_FILE "beamngearth_dtsmooth_ctl"
#define SIG_LEN 0x5f
#define TAIL_SEARCH 0x300

// The function's start after the timer call: raw = fmin(1, t); prev = dtReal; dtRaw = raw; debt*0.999; e = prev - raw
// with a clamp (0xff = masked: rip displacements, the fmin call, the ja's rel32)
static const unsigned char SIG[SIG_LEN] = {
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
    0x66, 0x0f, 0x28, 0xc1,                                  // +5b movapd xmm0,xmm1             e
};
static const unsigned char SIG_HOLES[] = {0x08, 0x0d, 0x15, 0x1d, 0x29, 0x31, 0x39, 0x4b, 0x53};  // 4-byte holes

// The tail: dtReal = prev + alpha*delta; the OpenXR manager (lazily made); if its session runs dtReal = raw; then
// the tail call with xmm0 = raw.
#define TAIL_LEN 0x3d
static const unsigned char TAIL[TAIL_LEN] = {
    0x4c, 0x8b, 0x25, 0xff, 0xff, 0xff, 0xff,                // +00 mov    r12,[xr manager]
    0xf2, 0x0f, 0x58, 0xce,                                  // +07 addsd  xmm1,xmm6
    0xf2, 0x0f, 0x11, 0x0d, 0xff, 0xff, 0xff, 0xff,          // +0b movsd  [dtReal],xmm1
    0x4d, 0x85, 0xe4,                                        // +13 test   r12,r12
    0x74, 0xff,                                              // +16 je     (make it)
    0x41, 0x80, 0xbc, 0x24, 0xff, 0xff, 0xff, 0xff, 0x00,    // +18 cmp    byte [r12+off],0
    0x74, 0x08,                                              // +21 je     +8
    0xf2, 0x0f, 0x11, 0x15, 0xff, 0xff, 0xff, 0xff,          // +23 movsd  [dtReal],xmm2      (raw)
    0x48, 0x83, 0xc4, 0x30,                                  // +2b add    rsp,0x30
    0x66, 0x0f, 0x28, 0xc2,                                  // +2f movapd xmm0,xmm2
    0x41, 0x5c, 0x41, 0x5d, 0x5d,                            // +33 pop r12; pop r13; pop rbp
    0xe9, 0xff, 0xff, 0xff, 0xff,                            // +38 jmp    MainLoopTimer add (tail call)
};

static int sig_hole(size_t i) {
    for (size_t k = 0; k < sizeof SIG_HOLES; k++)
        if (i >= SIG_HOLES[k] && i < (size_t)SIG_HOLES[k] + 4) return 1;
    return 0;
}
static int sig_match(const unsigned char *p) {
    for (size_t i = 0; i < SIG_LEN; i++) if (!sig_hole(i) && p[i] != SIG[i]) return 0;
    return 1;
}
static int tail_hole(size_t i) {
    return (i >= 0x03 && i < 0x07) || (i >= 0x0f && i < 0x13) || i == 0x17 || (i >= 0x1c && i < 0x20) ||
           (i >= 0x27 && i < 0x2b) || (i >= 0x39 && i < 0x3d);
}
static int tail_match(const unsigned char *p) {
    for (size_t i = 0; i < TAIL_LEN; i++) if (!tail_hole(i) && p[i] != TAIL[i]) return 0;
    return 1;
}
static uintptr_t rip_target(const unsigned char *p, size_t disp_off, size_t insn_end) {
    int32_t d;
    memcpy(&d, p + disp_off, 4);
    return (uintptr_t)(p + insn_end) + (intptr_t)d;
}

struct bnge_dtf g_dtf __attribute__((visibility("hidden")));
uintptr_t g_dt_orig __attribute__((visibility("hidden")));     // the tail call's own target

__attribute__((visibility("hidden"), used)) void bnge_dt_hook(double raw) { bnge_dtf_step(&g_dtf, raw); }

// Entered by a jmp in place of the tail call: rsp as at a function's entry, xmm0 = raw (the target's argument).
// Saves what the target may take as arguments, runs the filter, goes on to the target.
__asm__(
    ".text\n"
    ".p2align 4\n"
    ".globl bnge_dt_tramp\n"
    ".hidden bnge_dt_tramp\n"
    ".type bnge_dt_tramp,@function\n"
    "bnge_dt_tramp:\n"
    "  endbr64\n"
    "  sub $0x78, %rsp\n"
    "  movdqu %xmm0, 0x00(%rsp)\n"
    "  movdqu %xmm1, 0x10(%rsp)\n"
    "  movdqu %xmm2, 0x20(%rsp)\n"
    "  movdqu %xmm3, 0x30(%rsp)\n"
    "  mov %rdi, 0x40(%rsp)\n"
    "  mov %rsi, 0x48(%rsp)\n"
    "  mov %rdx, 0x50(%rsp)\n"
    "  mov %rcx, 0x58(%rsp)\n"
    "  mov %r8, 0x60(%rsp)\n"
    "  mov %r9, 0x68(%rsp)\n"
    "  mov %rax, 0x70(%rsp)\n"
    "  call bnge_dt_hook\n"
    "  movdqu 0x00(%rsp), %xmm0\n"
    "  movdqu 0x10(%rsp), %xmm1\n"
    "  movdqu 0x20(%rsp), %xmm2\n"
    "  movdqu 0x30(%rsp), %xmm3\n"
    "  mov 0x40(%rsp), %rdi\n"
    "  mov 0x48(%rsp), %rsi\n"
    "  mov 0x50(%rsp), %rdx\n"
    "  mov 0x58(%rsp), %rcx\n"
    "  mov 0x60(%rsp), %r8\n"
    "  mov 0x68(%rsp), %r9\n"
    "  mov 0x70(%rsp), %rax\n"
    "  add $0x78, %rsp\n"
    "  jmp *g_dt_orig(%rip)\n"
    ".size bnge_dt_tramp, .-bnge_dt_tramp\n");
extern void bnge_dt_tramp(void) __attribute__((visibility("hidden")));

// A page within rel32 reach of `site` (both directions, 1 MB steps), or NULL.
static unsigned char *near_page(uintptr_t site) {
    long pagesz = sysconf(_SC_PAGESIZE);
    const uintptr_t step = 1u << 20, reach = 0x7ff00000u;
    for (uintptr_t d = step; d < reach; d += step) {
        for (int dir = 0; dir < 2; dir++) {
            uintptr_t a = dir ? site + d : site - d;
            if (!dir && site < d + 0x10000) continue;
            a &= ~(uintptr_t)(pagesz - 1);
            void *p = mmap((void *)a, (size_t)pagesz, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE,
                           -1, 0);
            if (p == MAP_FAILED) continue;
            if ((uintptr_t)p != a) { munmap(p, (size_t)pagesz); continue; }   // (an old kernel took it as a hint)
            return p;
        }
    }
    return NULL;
}

static double env_d(const char *name, double def, double lo, double hi) {
    const char *s = getenv(name);
    if (!s || !*s) return def;
    double v = atof(s);
    return (v >= lo && v <= hi) ? v : def;
}

static int g_mode = 1;
static int g_ctl;

static void params_text(char *out, size_t n) {
    snprintf(out, n, "mode %d, hitch %.1f ms / x%.2f, tau %.1f, n %d, repay %.2f (max %.0f ms)", g_mode,
             g_dtf.hitch_abs * 1000, g_dtf.hitch_rel, g_dtf.tau, g_dtf.n, g_dtf.repay, g_dtf.debt_max * 1000);
}

// The test control file: "mode hitch_abs hitch_rel tau repay debt_max n [top_n]"; applied when it changes.
static void read_ctl(void) {
    static char last[256];
    char path[512], buf[256];
    bnge_temp_path(path, sizeof path, CTL_FILE);
    FILE *f = fopen(path, "r");
    if (!f) return;
    size_t k = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[k] = 0;
    if (!strcmp(buf, last)) return;
    memcpy(last, buf, sizeof last);
    int mode, n, top_n = 16;
    double h, hr, tau, r, dm;
    if (sscanf(buf, "%d %lf %lf %lf %lf %lf %d %d", &mode, &h, &hr, &tau, &r, &dm, &n, &top_n) < 7) return;
    g_dtf.hitch_abs = h; g_dtf.hitch_rel = hr; g_dtf.tau = tau; g_dtf.repay = r; g_dtf.debt_max = dm; g_dtf.n = n;
    g_dtf.top_n = top_n;
    g_mode = mode;
    g_dtf.init = 0;
}

static void *watch(void *arg) {
    const char *how = arg;
    char flag[512];
    bnge_temp_path(flag, sizeof flag, BNGE_LEVEL_FLAG);
    int on = 0, last_mode = -1;
    for (;;) {
        if (g_ctl) read_ctl();
        int want = access(flag, F_OK) == 0;
        int mode = want ? g_mode : 0;
        if (mode != last_mode) {
            __atomic_store_n(&g_dtf.enabled, mode, __ATOMIC_RELEASE);
            char msg[400], p[200];
            params_text(p, sizeof p);
            if (want) snprintf(msg, sizeof msg, "applied: steady game speed (BeamNG.Earth level; %s)%s", p, how);
            else if (on) snprintf(msg, sizeof msg, "stock: another level (last level: %llu frames, %llu capped, %.2f s "
                                  "dropped, %.2f s repaid)", (unsigned long long)g_dtf.frames,
                                  (unsigned long long)g_dtf.capped, g_dtf.dropped, g_dtf.repaid);
            else snprintf(msg, sizeof msg, "ready: applied while the BeamNG.Earth level runs%s", how);
            bnge_report(TAG, STATUS_FILE, msg);
            last_mode = mode;
            on = want;
        }
        struct timespec ts = {0, BNGE_FLAG_POLL_MS * 1000000L};
        nanosleep(&ts, NULL);
    }
    return NULL;
}

static int env_off(void) {
    const char *on = getenv("BNGEARTH_DTSMOOTH");
    const char *off = getenv("BNGEARTH_DTSMOOTH_OFF");
    if (off && *off && strcmp(off, "0")) return 1;
    return on && !strcmp(on, "0");
}

__attribute__((constructor)) static void bngearth_dtsmooth(void) {
    if (!bnge_is_main_game()) return;
    if (env_off()) { bnge_report(TAG, STATUS_FILE, "off (BNGEARTH_DTSMOOTH_OFF / BNGEARTH_DTSMOOTH=0)"); return; }
    bnge_dtf_defaults(&g_dtf);
    g_dtf.repay = env_d("BNGEARTH_DTSMOOTH_REPAY", g_dtf.repay, 0.0, 1.0);
    g_dtf.hitch_abs = env_d("BNGEARTH_DTSMOOTH_HITCH", g_dtf.hitch_abs, 0.0005, 2.0);
    g_dtf.tau = env_d("BNGEARTH_DTSMOOTH_TAU", g_dtf.tau, 1.0, 1000.0);
    g_dtf.n = (int32_t)env_d("BNGEARTH_DTSMOOTH_N", g_dtf.n, 1, BNGE_DTF_RING);
    g_mode = (int)env_d("BNGEARTH_DTSMOOTH_MODE", 1, 1, 2);
    g_ctl = getenv("BNGEARTH_DTSMOOTH_CTL") && !strcmp(getenv("BNGEARTH_DTSMOOTH_CTL"), "1");

    struct bnge_main m;
    bnge_find_main(&m);
    if (!m.seen) { bnge_report(TAG, STATUS_FILE, "not applied: the game's code was not found"); return; }
    unsigned char *region = NULL;
    int n = 0;
    for (int s = 0; s < m.nseg; s++) {
        if (!(m.seg[s].flags & PF_X)) continue;
        for (unsigned char *p = (unsigned char *)m.seg[s].start; p + SIG_LEN + TAIL_SEARCH <= (unsigned char *)m.seg[s].end; p++) {
            if (p[0] != 0x66 || p[1] != 0x0f || p[2] != 0x28 || p[3] != 0xc8 || p[4] != 0xf2) continue;
            if (sig_match(p)) { region = p; n++; }
        }
    }
    if (n != 1) {
        char msg[128];
        snprintf(msg, sizeof msg, "not applied: the frame-time filter was not found in this build (%d matches); stock", n);
        bnge_report(TAG, STATUS_FILE, msg);
        return;
    }
    uintptr_t dtreal = rip_target(region, 0x15, 0x19);
    unsigned char *tail = NULL;
    for (unsigned char *p = region + SIG_LEN; p < region + SIG_LEN + TAIL_SEARCH; p++)
        if (tail_match(p)) { if (tail) { tail = NULL; break; } tail = p; }
    if (!tail || rip_target(tail, 0x0f, 0x13) != dtreal || rip_target(tail, 0x27, 0x2b) != dtreal) {
        bnge_report(TAG, STATUS_FILE, "not applied: the filter's end differs in this build; stock");
        return;
    }
    if (!bnge_mapped(&m, dtreal - m.base, 8, PF_R | PF_W)) {
        bnge_report(TAG, STATUS_FILE, "not applied: dtReal is not in the game's data; stock");
        return;
    }
    int32_t xr_off;
    memcpy(&xr_off, tail + 0x1c, 4);
    unsigned char *site = tail + 0x38;       // e9 rel32
    g_dt_orig = rip_target(site, 1, 5);
    g_dtf.dtreal = (double *)dtreal;
    g_dtf.xr = (uint8_t **)rip_target(tail, 0x03, 0x07);
    g_dtf.xr_off = xr_off;

    unsigned char *stub = near_page((uintptr_t)site);
    if (!stub) { bnge_report(TAG, STATUS_FILE, "not applied: no memory near the game's code; stock"); return; }
    uintptr_t tramp = (uintptr_t)&bnge_dt_tramp;
    memcpy(stub, "\xff\x25\x00\x00\x00\x00", 6);             // jmp [rip+0]
    memcpy(stub + 6, &tramp, 8);
    long pagesz = sysconf(_SC_PAGESIZE);
    if (mprotect(stub, (size_t)pagesz, PROT_READ | PROT_EXEC) != 0) {
        bnge_report(TAG, STATUS_FILE, "not applied: mprotect refused; stock");
        return;
    }
    intptr_t rel = (intptr_t)stub - (intptr_t)(site + 5);
    if (rel != (int32_t)rel) { bnge_report(TAG, STATUS_FILE, "not applied: the stub is out of reach; stock"); return; }

    // (the constructor: the game's own code has not started; enabled = 0 keeps the stock dtReal until the level flag)
    uintptr_t lo = (uintptr_t)site & ~(uintptr_t)(pagesz - 1);
    uintptr_t hi = ((uintptr_t)site + 5 + pagesz - 1) & ~(uintptr_t)(pagesz - 1);
    if (mprotect((void *)lo, hi - lo, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        bnge_report(TAG, STATUS_FILE, "not applied: mprotect refused; stock");
        return;
    }
    int32_t r32 = (int32_t)rel;
    memcpy(site + 1, &r32, 4);
    __builtin___clear_cache((char *)site, (char *)site + 5);
    mprotect((void *)lo, hi - lo, PROT_READ | PROT_EXEC);

    pthread_t t;
    if (pthread_create(&t, NULL, watch, (void *)(g_ctl ? " (test control file on)" : "")) != 0) {
        bnge_report(TAG, STATUS_FILE, "hooked but no thread for the level flag: stock");
        return;
    }
    pthread_detach(t);
}
