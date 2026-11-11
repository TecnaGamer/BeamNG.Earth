// The frame-time filter of dtsmooth (README.md), shared by the Linux preload library (bngearth_dtsmooth.c) and the
// Windows code blob the launcher writes into the game (tools/gcpatch/win/, enginepatch.py): freestanding C, no libc
// calls, no globals, so the same source compiles to position-independent machine code for both.
//
// The engine moves the world each frame by dtReal, its smoothed frame time. Its own filter (FrameTime::update) tracks
// the measured frame time `raw` slowly (a 13-40 frame time constant) and keeps a "time debt": over 0.25 s the game ran
// at 0.84-1.19x speed while our streaming made frame times uneven, and after a hitch it ran up to ~2x for ~0.5 s to
// replay the hitch. This filter replaces its result, once a frame, after the engine computed it:
//   hitch: raw > top + max(hitch_abs, hitch_rel * mean)   (top: the longest of the last top_n ordinary frames, so
//          a regular slow frame - 1 in 3 - is no hitch; at most BNGE_DTF_STREAK in a row: then a new frame rate)
//   s    = raw, or for a hitch the mean, its excess raw - mean going to the debt
//   dt   = mean(last n samples) + err / tau               err = sum(s) - sum(dt): game time locked to the clock
//   debt = min(debt_max, debt + excess); dt += min(debt, repay * dt)   (repay 0: a hitch's excess is dropped)
// so game time follows real time within a few frames (no slow drift, no catch-up burst) and a hitch's time is paid
// back at no more than (1 + repay)x (or dropped). The stock dtReal is left as it is while `enabled` is 0, while
// the engine's OpenXR session is active (it then uses raw itself) and for any input it does not expect.
//
// mode 2 (A/B tests only): the engine's own filter re-implemented with the old dtsmooth cap s = min(raw, prev + C)
// (C = hitch_abs), to compare against without the 2026-11-03 patch of the engine's code.
#pragma once
#include <stdint.h>

#define BNGE_DTF_RING 16
#define BNGE_DTF_STREAK 3      // long frames in a row taken as hitches; more: a slower frame rate

// Field order and sizes are fixed (the launcher writes this struct into the Windows game by offset: enginepatch.py
// DTF_FIELDS mirrors it; test_dtfilter.c checks the offsets).
struct bnge_dtf {
    // parameters (the host sets them; read every frame)
    double hitch_abs;       // +0x00 s
    double hitch_rel;       // +0x08
    double tau;             // +0x10 frames
    double repay;           // +0x18 fraction of a frame's dt (0: drop)
    double debt_max;        // +0x20 s
    // the engine's globals (absolute addresses, the host fills them)
    double *dtreal;         // +0x28 dtReal (written)
    uint8_t **xr;           // +0x30 the OpenXR manager's pointer (0: none)
    int32_t xr_off;         // +0x38 its session-active byte
    int32_t n;              // +0x3c samples averaged, 1..BNGE_DTF_RING
    int32_t enabled;        // +0x40 0 stock, 1 this filter, 2 the stock filter with the old cap (tests)
    int32_t init;           // +0x44 0: (re)start from the engine's dtReal at the next frame
    int32_t top_n;          // +0x48 frames `top` is taken over, 1..BNGE_DTF_RING
    int32_t streak;         // +0x4c hitch frames in a row
    // state
    double ring[BNGE_DTF_RING];   // +0x50 the samples s
    double typ[BNGE_DTF_RING];    // +0xd0 the same, a capped one replaced by the mean (what `top` is taken over)
    int32_t head, count;    // +0x150
    double ref, err, debt;  // +0x158 ref: the mean of the last n samples
    double prev2, sdebt, h1, h2;  // +0x170 mode 2: the stock filter's own state
    // statistics (diagnostics: the status file / the launcher's log)
    uint64_t frames, capped;      // +0x190
    double dropped, repaid;       // +0x1a0
};                                // 0x1b0 bytes

static inline double bnge_dtf_min(double a, double b) { return a < b ? a : b; }
static inline double bnge_dtf_max(double a, double b) { return a > b ? a : b; }

// The stock FrameTime::update (0.40, reproduced from its machine code) on the sample s = min(raw, prev + C).
static inline double bnge_dtf_stock(struct bnge_dtf *f, double raw) {
    double prev = f->prev2;
    double s = bnge_dtf_min(raw, prev + f->hitch_abs);
    double e = bnge_dtf_max(-0.1, bnge_dtf_min(0.1, prev - s));
    f->sdebt = 0.999 * f->sdebt + e;
    double h3 = f->h2 > 0 ? f->h2 : s, h2 = f->h1 > 0 ? f->h1 : s;
    f->h2 = h2; f->h1 = s;
    double m = bnge_dtf_max(bnge_dtf_min(s, h2), bnge_dtf_min(bnge_dtf_max(s, h2), h3));
    double target = m - 0.025 > s ? m - 0.025 : bnge_dtf_min(m + 0.025, s);
    double delta = target - prev;
    double a = f->sdebt * delta < 0 ? 0.075 : 0.025;
    f->prev2 = prev + a * delta;
    if (raw > s) { f->capped++; f->dropped += raw - s; }
    return f->prev2;
}

// One frame: raw = the engine's measured frame time (s, already min(1, t)). Writes *f->dtreal unless left stock.
static inline void bnge_dtf_step(struct bnge_dtf *f, double raw) {
    int mode = f->enabled;
    if (mode != 1 && mode != 2) { f->init = 0; return; }
    if (f->xr) {
        uint8_t *x = *f->xr;
        if (x && x[f->xr_off]) { f->init = 0; return; }
    }
    if (!(raw >= 0.0) || raw > 1.0) { f->init = 0; return; }     // (NaN included)
    int n = f->n < 1 ? 1 : (f->n > BNGE_DTF_RING ? BNGE_DTF_RING : f->n);
    double tau = f->tau >= 1.0 ? f->tau : 1.0;
    if (!f->init) {
        double d = *f->dtreal;
        if (!(d > 0.0 && d < 1.0)) d = raw;
        for (int i = 0; i < BNGE_DTF_RING; i++) { f->ring[i] = d; f->typ[i] = d; }
        f->head = 0; f->count = n; f->streak = 0; f->ref = d; f->err = 0.0; f->debt = 0.0;
        f->prev2 = d; f->sdebt = 0.0; f->h1 = -1.0; f->h2 = -1.0;
        f->init = 1;
    }
    f->frames++;
    if (mode == 2) { *f->dtreal = bnge_dtf_stock(f, raw); return; }

    int tn = f->top_n < 1 ? 1 : (f->top_n > BNGE_DTF_RING ? BNGE_DTF_RING : f->top_n);
    double ref = f->ref, top = 0.0;
    for (int i = 0; i < tn; i++) top = bnge_dtf_max(top, f->typ[(f->head - i + BNGE_DTF_RING) % BNGE_DTF_RING]);
    double cap = top + bnge_dtf_max(f->hitch_abs, f->hitch_rel * ref);
    double s = raw, x = 0.0;
    if (raw > cap && f->streak < BNGE_DTF_STREAK) {
        // a hitch: the clock sees an ordinary frame (ref), its excess goes to the debt (or is dropped)
        s = ref; x = raw - ref;
        f->streak++;
        f->capped++;
        if (f->repay > 0.0) {
            double room = f->debt_max - f->debt;
            double add = bnge_dtf_min(x, room > 0.0 ? room : 0.0);
            f->debt += add;
            f->dropped += x - add;
        } else {
            f->dropped += x;
        }
    } else {
        f->streak = 0;          // (also after BNGE_DTF_STREAK long frames in a row: a new frame rate, followed)
    }
    f->head = (f->head + 1) % BNGE_DTF_RING;
    f->ring[f->head] = s;
    f->typ[f->head] = x > 0.0 ? ref : s;
    if (f->count < n) f->count++;
    if (f->count > n) f->count = n;
    double sum = 0.0;
    for (int i = 0; i < n; i++) sum += f->ring[(f->head - i + BNGE_DTF_RING) % BNGE_DTF_RING];
    double m = sum / n;
    double dt = m + f->err / tau;
    if (dt < 0.0) dt = 0.0;
    f->err = bnge_dtf_max(-0.1, bnge_dtf_min(0.1, f->err + s - dt));
    if (f->repay > 0.0 && f->debt > 0.0) {
        double r = bnge_dtf_min(f->debt, f->repay * dt);
        f->debt -= r; f->repaid += r; dt += r;
    } else if (f->repay <= 0.0) {
        f->debt = 0.0;
    }
    f->ref = m;
    *f->dtreal = dt;
}

// The defaults (README.md: measured 2026-11-11).
static inline void bnge_dtf_defaults(struct bnge_dtf *f) {
    f->hitch_abs = 0.010; f->hitch_rel = 1.0; f->tau = 4.0; f->repay = 0.10; f->debt_max = 0.5; f->n = 3;
    f->top_n = 16;
}
