// terfast: the terrain block swap's cell scan without its divisions (BeamNG.Earth; README.md).
// Part of the same preload library as bngearth_gcpatch.c (its own switch and status file).
//
// A .ter swap (TerrainBlock::setFile) rebuilds the block's cell tree (TerrCell::init). For every 64 x 64 leaf cell the
// engine scans 65 x 65 samples twice: TerrCell::_updateBounds (the cell's box from the heights) and
// TerrCell::_updateMaterials (which layers the cell holds). Both loops compute each sample's index as
// (y % size) * size + (x % size) with two 32-bit divisions per sample, and the bounds loop stores the box after every
// sample: 25 ns a sample on a Steam Deck, 12.5 ms of a 512² block's 23 ms swap (perf, 2026-11-13; 4x that for 1024²).
// terfast replaces both functions with the same computation done row by row (the wrap resolved once per column, the
// heights' minimum / maximum as integers): the same box, radius and layer mask bit for bit, ~40x faster.
// A third scan of the same kind sits at the start of TerrCell::_updateVertexBuffer (does a leaf hold a hole sample:
// 65 x 65 float coordinates, two divisions each, ~6 ms a 512² swap): its loop is jumped over (a 5-byte jump at its
// first instruction to a stub near the game's code) and the flag set from a row scan with the engine's own
// coordinate arithmetic.
//
// Found by code, not by address (2026-11-22, after BeamNG 0.40.6 moved data the old exact-address check had pinned):
// the three functions, the two engine functions the replacements call and getTransform are matched against masked
// whole-function signatures (bngearth_sigs.h, made by mksigs.py: every byte but rip displacements and calls / jumps
// out of the function), each exactly once, and the constants they load are checked (+-FLT_MAX, 0.5, 1 / 64). The
// structure offsets the replacements read are all inside those functions' code, so a build that changed any of them
// does not match: on any difference nothing is patched and <user>/temp/beamngEarth_terfast.txt says "not applied:
// another game build (why; build id)". The entry of each function (after its endbr64) is replaced by a 14-byte jump before the game's code runs; the
// displaced instructions (position independent) live in a trampoline, so the stock function stays callable: it runs
// outside the BeamNG.Earth level (the level flag), for any input the fast path does not expect, and for
// BNGEARTH_TERFAST_CHECK=1, which runs both and counts differences in the status file. Off: BNGEARTH_TERFAST_OFF=1.
#include "bngearth_common.h"
#include "bngearth_sigs.h"
#include <pthread.h>
#include <stdatomic.h>
#include <sys/mman.h>
#include <math.h>

#define TAG "bngearth-terfast"
#define STATUS_FILE "beamngEarth_terfast.txt"

// The engine functions, found by their masked whole-function signatures (bngearth_sigs.h, mksigs.py; reference:
// BeamNG.drive 0.40.6 Linux, the 2026-11-19 executable; the 2026-11-06 one had the same code at the same addresses).
// Offsets inside a matched function (its bytes equal the signature's there, the instruction lengths with them):
#define B_INIT_LO_DISP 0x1f, 0x23       // bounds: mov rdx, [rip] (box max y z: -FLT_MAX x2)
#define B_INIT_HI_DISP 0x26, 0x2a       // bounds: movaps xmm0, [rip] (box min x y z, max x: FLT_MAX x3, -FLT_MAX)
#define B_HALF_DISP 0x284, 0x288        // bounds: mulss xmm0, [rip] (0.5: radius = length / 2)
#define B_GETXF_DISP 0x28b, 0x28f       // bounds: lea rdx, [rip] (SceneObject::getTransform, devirtualised)
#define B_XFORM_REL 0x2d6, 0x2da        // bounds: jmp <the world box> (tail call)
#define M_TAIL_REL 0x17c, 0x180         // mats: jmp <tail> (cells with a vertex buffer slot)
#define VB_SCAN_OFF 0x125               // vb: mov eax, [rbx + 0x28]; cmp eax, 0x40; ja VB_AFTER: the hole scan (rbx cell, r13 file)
#define VB_BODY_OFF 0x131               // vb: its loop (eax = the cell's size)
#define VB_AFTER_OFF 0x242
#define VB_STEP_DISP 0x148, 0x14c       // vb: the loop's float 1 / 64
#define ENTRY_SKIP 4                // endbr64
#define ENTRY_LEN 14                // the displaced instructions (whole, position independent)
static const unsigned char ENTRY_BOUNDS[ENTRY_LEN] = {0x55, 0x48, 0x8d, 0x77, 0x5c, 0x48, 0x89, 0xe5, 0x41, 0x57, 0x41, 0x56, 0x41, 0x55};
static const unsigned char ENTRY_MATS[ENTRY_LEN] = {0x55, 0x48, 0x89, 0xe5, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54, 0x49, 0x89, 0xfc, 0x53};
static const unsigned char VB_SITE[12] = {0x8b, 0x43, 0x28, 0x83, 0xf8, 0x40, 0x0f, 0x87, 0x11, 0x01, 0x00, 0x00};

// Where everything is in this build (tf_locate).
struct tf_targets {
    uintptr_t bounds, mats, xform, mats_tail, vb, get_transform, vb_scan, vb_body, vb_after;
    float step_k;
};

#define CELL_HOLES 0x34             // u8: the leaf holds a hole sample (layer 255)

// TerrCell
#define CELL_X 0x20                 // u32: the cell's first sample
#define CELL_Y 0x24
#define CELL_SIZE 0x28              // u32: samples across (64 for a leaf)
#define CELL_BLOCK 0x50
#define CELL_VB 0x58                // i32: -1 = none
#define CELL_BOX 0x5c               // 6 floats: min x y z, max x y z (object space)
#define CELL_WORLD 0x74
#define CELL_RADIUS 0x114
#define CELL_MASK 0x138             // u64: bit per layer index < 32 present
// TerrainBlock
#define BLOCK_VTBL_TRANSFORM 0x1c8
#define BLOCK_MATRIX 0x130
#define BLOCK_FILE 0x260            // Resource<TerrainFile>: holder -> +0x18 -> +0x8 = TerrainFile *
#define BLOCK_SQUARE 0x340          // float
#define BLOCK_HSCALE 0x3e4          // float: metres per height unit
// TerrainFile
#define FILE_SIZE 0x20              // u32
#define FILE_LAYERS 0x28            // u8 *
#define FILE_HEIGHTS 0x40           // u16 *
#define SPAN 65                     // samples a cell scans per axis

typedef void (*cell_fn)(void *cell);
typedef void (*xform_fn)(void *world, const void *matrix, const void *box);
typedef const void *(*transform_fn)(void *block);

static uintptr_t g_get_transform;                // SceneObject::getTransform (the devirtualised case)
static cell_fn g_stock_bounds, g_stock_mats, g_mats_tail;
static xform_fn g_xform;
static atomic_int g_on, g_check;
static atomic_ullong st_bounds, st_mats, st_holes, st_stock, st_diff, st_diff_b, st_diff_m, st_diff_h, st_pend_lost,
    st_holes_checked, st_diff_h_fast;
static char g_found[256];                       // how the targets were found (the status line)
static uintptr_t g_vb_after, g_vb_stock;        // where the hole scan's stub goes on: past the loop / into the stock loop
static float g_step_k;
static void *g_pending_cell;                    // (check mode, main thread: the leaf whose stock hole flag is compared next)
static const void *g_pending_file;
static int g_pending_flag;                      // (what the fast path would leave: the flag before | found)

static inline uint32_t rd32(const void *p, size_t off) { uint32_t v; memcpy(&v, (const char *)p + off, 4); return v; }
static inline float rdf(const void *p, size_t off) { float v; memcpy(&v, (const char *)p + off, 4); return v; }
static inline void *rdp(const void *p, size_t off) { void *v; memcpy(&v, (const char *)p + off, 8); return v; }

// The block's TerrainFile with a square size the scan can use, or NULL (then the stock function runs).
static inline const void *cell_file(const void *cell, void **block_out, uint32_t *fsize) {
    void *block = rdp(cell, CELL_BLOCK);
    if (!block) return NULL;
    void *holder = rdp(block, BLOCK_FILE);
    if (!holder) return NULL;
    void *res = rdp(holder, 0x18);
    if (!res) return NULL;
    const void *file = rdp(res, 0x8);
    if (!file) return NULL;
    uint32_t n = rd32(file, FILE_SIZE);
    if (n < SPAN || n > 16384 || !rdp(file, FILE_LAYERS) || !rdp(file, FILE_HEIGHTS)) return NULL;
    *block_out = block;
    *fsize = n;
    return file;
}

// The hole scan of TerrCell::_updateVertexBuffer for `cell` (the stub's call; `file` = the engine's r13). Returns
// where the stub jumps: past the stock loop (the flag set here), or into it (anything unexpected, the check mode).
// As the engine: sample (i, j) of 65 x 65 is at x = float(x0) + float(i) * step, step = float(size) / 64, clamped to
// [0, n] (above n: n), truncated, modulo n; the same for y, except that a negative y becomes 0 after the comparison
// with n; a row ends at its first hole.
uintptr_t bnge_terfast_holes(void *cell, const void *file) {
    uint32_t size = rd32(cell, CELL_SIZE);
    if (size > 0x40) return g_vb_after;                 // (the engine skips the scan too)
    void *block;
    uint32_t n;
    if (!atomic_load_explicit(&g_on, memory_order_relaxed) || !file || g_step_k != 0.015625f ||
        cell_file(cell, &block, &n) != file) {
        atomic_fetch_add_explicit(&st_stock, 1, memory_order_relaxed);
        return g_vb_stock;
    }
    const uint8_t *layers = rdp(file, FILE_LAYERS);
    float step = (float)(int32_t)size * g_step_k, fs = (float)(int64_t)n;
    float fx0 = (float)(int32_t)rd32(cell, CELL_X), fy0 = (float)(int32_t)rd32(cell, CELL_Y);
    uint32_t col[SPAN], row[SPAN];
    int plain = 1;
    for (int i = 0; i < SPAN; i++) {
        float x = fx0 + (float)i * step;
        if (x > fs) x = fs; else x = 0.0f > x ? 0.0f : x;
        col[i] = (uint32_t)(int64_t)x % n;
        float y = fy0 + (float)i * step;
        int64_t yi = y > fs ? (int64_t)fs : (0.0f <= y ? (int64_t)y : 0);
        row[i] = (uint32_t)yi % n;
        if (col[i] != col[0] + (uint32_t)i) plain = 0;
    }
    int found = 0;
    for (int j = 0; j < SPAN && !found; j++) {
        const uint8_t *r = layers + (size_t)row[j] * n;
        if (plain) found = memchr(r + col[0], 0xff, SPAN) != NULL;
        else for (int i = 0; i < SPAN; i++) if (r[col[i]] == 0xff) { found = 1; break; }
    }
    atomic_fetch_add_explicit(&st_holes, 1, memory_order_relaxed);
    if (atomic_load_explicit(&g_check, memory_order_relaxed)) {     // the stock loop runs; compared at the leaf's bounds
        if (g_pending_cell) atomic_fetch_add_explicit(&st_pend_lost, 1, memory_order_relaxed);
        g_pending_cell = cell;
        g_pending_file = file;
        g_pending_flag = found || *((unsigned char *)cell + CELL_HOLES) != 0;
        return g_vb_stock;
    }
    if (found) *((unsigned char *)cell + CELL_HOLES) = 1;
    return g_vb_after;
}

// (check mode) The stock hole loop has just run for `cell`: its flag against what the row scan found. Called from the
// check stub put in place of _updateVertexBuffer's call right after the loop (VB_AFTER), only with BNGEARTH_TERFAST_CHECK.
void bnge_terfast_vbcheck(void *cell) {
    if (!cell || g_pending_cell != cell) return;
    int stock = *((unsigned char *)cell + CELL_HOLES) != 0;
    atomic_fetch_add_explicit(&st_holes_checked, 1, memory_order_relaxed);
    if (stock != g_pending_flag) {
        atomic_fetch_add_explicit(&st_diff, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&st_diff_h, 1, memory_order_relaxed);
        if (g_pending_flag) atomic_fetch_add_explicit(&st_diff_h_fast, 1, memory_order_relaxed);
        static int logged;
        if (logged < 40 && getenv("BNGEARTH_TERFAST_CHECKLOG")) {     // (what the two scans saw)
            logged++;
            char path[512];
            bnge_temp_path(path, sizeof path, "beamngEarth_terfast_check.log");
            FILE *f = fopen(path, "a");
            if (f) {
                const void *file = g_pending_file;
                uint32_t n = rd32(file, FILE_SIZE), size = rd32(cell, CELL_SIZE);
                int32_t x0 = (int32_t)rd32(cell, CELL_X), y0 = (int32_t)rd32(cell, CELL_Y);
                const uint8_t *layers = rdp(file, FILE_LAYERS);
                fprintf(f, "cell %p x0 %d y0 %d size %u n %u stock %d fast %d holes:", cell, x0, y0, size, n, stock,
                        g_pending_flag);
                int k = 0;
                for (int j = -2; j <= 66 && k < 12; j++)
                    for (int i = -2; i <= 66 && k < 12; i++) {
                        long xx = ((long)x0 + i) % (long)n, yy = ((long)y0 + j) % (long)n;
                        if (xx < 0) xx += n;
                        if (yy < 0) yy += n;
                        if (layers[(size_t)yy * n + (size_t)xx] == 0xff) { fprintf(f, " (%d,%d)", i, j); k++; }
                    }
                fprintf(f, "\n");
                fclose(f);
            }
        }
    }
    g_pending_cell = NULL;
}

static void fast_bounds(void *cell) {
    void *block;
    uint32_t n;
    const void *file = atomic_load_explicit(&g_on, memory_order_relaxed) ? cell_file(cell, &block, &n) : NULL;
    uint32_t x0 = rd32(cell, CELL_X), y0 = rd32(cell, CELL_Y), stride = rd32(cell, CELL_SIZE) >> 6;
    float hs = file ? rdf(block, BLOCK_HSCALE) : 0.0f, sq = file ? rdf(block, BLOCK_SQUARE) : 0.0f;
    // (the positions must not wrap in 32 bits, and the height scale must keep the heights' order)
    if (!file || !(hs >= 0.0f) || !(sq > 0.0f) || stride == 0 || x0 > 0x10000000u || y0 > 0x10000000u || stride > 0x10000u) {
        atomic_fetch_add_explicit(&st_stock, 1, memory_order_relaxed);
        g_stock_bounds(cell);
        return;
    }
    const uint16_t *h = rdp(file, FILE_HEIGHTS);
    uint32_t col[SPAN];
    for (uint32_t i = 0; i < SPAN; i++) col[i] = (x0 + i) % n;        // (the engine indexes the heights unstrided)
    unsigned lo = 0xffff, hi = 0;
    for (uint32_t j = 0; j < SPAN; j++) {
        const uint16_t *row = h + (size_t)((y0 + j) % n) * n;
        if (col[SPAN - 1] == col[0] + SPAN - 1) {                    // no wrap in this cell's columns
            const uint16_t *p = row + col[0];
            for (uint32_t i = 0; i < SPAN; i++) { unsigned v = p[i]; lo = v < lo ? v : lo; hi = v > hi ? v : hi; }
        } else {
            for (uint32_t i = 0; i < SPAN; i++) { unsigned v = row[col[i]]; lo = v < lo ? v : lo; hi = v > hi ? v : hi; }
        }
    }
    // as the engine: min / max against +-FLT_MAX of x = float(x0 + i * stride) * square (rising with i), y the same,
    // z = float(height) * scale
    float minx = 3.4028234663852886e+38f, miny = minx, minz = minx, maxx = -minx, maxy = -minx, maxz = -minx;
    for (uint32_t i = 0; i < SPAN; i++) {
        float x = (float)(int64_t)(x0 + i * stride) * sq, y = (float)(int64_t)(y0 + i * stride) * sq;
        minx = x < minx ? x : minx; maxx = x > maxx ? x : maxx;
        miny = y < miny ? y : miny; maxy = y > maxy ? y : maxy;
    }
    float zl = (float)(int)lo * hs, zh = (float)(int)hi * hs;
    minz = zl < minz ? zl : minz; maxz = zh > maxz ? zh : maxz;
    float box[6] = {minx, miny, minz, maxx, maxy, maxz};
    float dx = maxx - minx, dy = maxy - miny, dz = maxz - minz;
    float r2 = dx * dx + dy * dy;
    r2 = r2 + dz * dz;
    float radius = sqrtf(r2) * 0.5f;
    if (atomic_load_explicit(&g_check, memory_order_relaxed)) {
        g_stock_bounds(cell);
        float rr;
        memcpy(&rr, (char *)cell + CELL_RADIUS, 4);
        if (memcmp((char *)cell + CELL_BOX, box, sizeof box) != 0 || memcmp(&rr, &radius, 4) != 0) {
            atomic_fetch_add_explicit(&st_diff, 1, memory_order_relaxed);
            atomic_fetch_add_explicit(&st_diff_b, 1, memory_order_relaxed);
        }
        atomic_fetch_add_explicit(&st_bounds, 1, memory_order_relaxed);
        return;
    }
    memcpy((char *)cell + CELL_BOX, box, sizeof box);
    memcpy((char *)cell + CELL_RADIUS, &radius, 4);
    void **vtbl = rdp(block, 0);
    transform_fn get = (transform_fn)vtbl[BLOCK_VTBL_TRANSFORM / 8];
    const void *matrix = (uintptr_t)get == g_get_transform ? (const char *)block + BLOCK_MATRIX : get(block);
    g_xform((char *)cell + CELL_WORLD, matrix, (char *)cell + CELL_BOX);
    atomic_fetch_add_explicit(&st_bounds, 1, memory_order_relaxed);
}

static void fast_mats(void *cell) {
    void *block;
    uint32_t n;
    const void *file = atomic_load_explicit(&g_on, memory_order_relaxed) ? cell_file(cell, &block, &n) : NULL;
    uint32_t x0 = rd32(cell, CELL_X), y0 = rd32(cell, CELL_Y), stride = rd32(cell, CELL_SIZE) >> 6;
    if (!file || stride == 0 || x0 > 0x10000000u || y0 > 0x10000000u || stride > 0x10000u) {
        atomic_fetch_add_explicit(&st_stock, 1, memory_order_relaxed);
        g_stock_mats(cell);
        return;
    }
    const uint8_t *layers = rdp(file, FILE_LAYERS);
    uint32_t col[SPAN];
    for (uint32_t i = 0; i < SPAN; i++) col[i] = (x0 + i * stride) % n;
    int plain = stride == 1 && col[SPAN - 1] == col[0] + SPAN - 1;
    uint64_t mask = 0;
    for (uint32_t j = 0; j < SPAN; j++) {
        const uint8_t *row = layers + (size_t)((y0 + j * stride) % n) * n;
        if (plain) {
            const uint8_t *p = row + col[0];
            for (uint32_t i = 0; i < SPAN; i++) { unsigned l = p[i]; if (l <= 31) mask |= (uint64_t)1 << l; }
        } else {
            for (uint32_t i = 0; i < SPAN; i++) { unsigned l = row[col[i]]; if (l <= 31) mask |= (uint64_t)1 << l; }
        }
    }
    if (atomic_load_explicit(&g_check, memory_order_relaxed)) {
        g_stock_mats(cell);              // (with its tail call)
        uint64_t got;
        memcpy(&got, (char *)cell + CELL_MASK, 8);
        if (got != mask) {
            atomic_fetch_add_explicit(&st_diff, 1, memory_order_relaxed);
            atomic_fetch_add_explicit(&st_diff_m, 1, memory_order_relaxed);
        }
        atomic_fetch_add_explicit(&st_mats, 1, memory_order_relaxed);
        return;
    }
    memcpy((char *)cell + CELL_MASK, &mask, 8);
    atomic_fetch_add_explicit(&st_mats, 1, memory_order_relaxed);
    if ((int32_t)rd32(cell, CELL_VB) != -1) g_mats_tail(cell);
}

static void report(const char *msg) { bnge_report(TAG, STATUS_FILE, msg); }
static void report_sticky(const char *msg) { bnge_report_sticky(TAG, STATUS_FILE, msg); }

// jmp [rip + 0]; <target>
static void write_jump(unsigned char *at, uintptr_t target) {
    at[0] = 0xff; at[1] = 0x25; at[2] = at[3] = at[4] = at[5] = 0;
    memcpy(at + 6, &target, 8);
}

static int hook(uintptr_t fn, const unsigned char *entry, unsigned char *tramp, void (*repl)(void *)) {
    unsigned char *code = (unsigned char *)fn;
    if (memcmp(code + ENTRY_SKIP, entry, ENTRY_LEN) != 0) return 0;
    memcpy(tramp, entry, ENTRY_LEN);
    write_jump(tramp + ENTRY_LEN, fn + ENTRY_SKIP + ENTRY_LEN);
    long pagesz = sysconf(_SC_PAGESIZE);
    uintptr_t page = fn & ~(uintptr_t)(pagesz - 1);
    size_t len = (size_t)((fn + ENTRY_SKIP + ENTRY_LEN - page + pagesz - 1) & ~(uintptr_t)(pagesz - 1));
    if (mprotect((void *)page, len, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) return 0;
    write_jump(code + ENTRY_SKIP, (uintptr_t)repl);
    __sync_synchronize();
    mprotect((void *)page, len, PROT_READ | PROT_EXEC);
    return 1;
}

// The hole scan's stub (a page within rel32 reach of the site):
//   +0   mov rdi, rbx; mov rsi, r13; mov rax, bnge_terfast_holes; call rax; jmp rax
//   +32  (the stock loop again) mov eax, [rbx + 0x28]; cmp eax, 0x40; ja +13; mov r11, VB_SCAN_BODY; jmp r11;
//        mov r11, VB_AFTER; jmp r11
static int hook_holes(const struct tf_targets *t) {
    unsigned char *site = (unsigned char *)t->vb_scan;
    g_step_k = t->step_k;                           // (the loop's 1 / 64, checked by tf_locate)
    unsigned char *stub = bnge_near_page((uintptr_t)site);
    if (!stub) return 0;
    unsigned char *p = stub;
    uintptr_t fn = (uintptr_t)bnge_terfast_holes, body = t->vb_body, after = t->vb_after;
    static const unsigned char A[] = {0x48, 0x89, 0xdf, 0x4c, 0x89, 0xee, 0x48, 0xb8};
    memcpy(p, A, sizeof A); p += sizeof A;
    memcpy(p, &fn, 8); p += 8;
    *p++ = 0xff; *p++ = 0xd0;                       // call rax
    *p++ = 0xff; *p++ = 0xe0;                       // jmp rax
    p = stub + 32;
    static const unsigned char B[] = {0x8b, 0x43, 0x28, 0x83, 0xf8, 0x40, 0x77, 0x0d, 0x49, 0xbb};
    memcpy(p, B, sizeof B); p += sizeof B;
    memcpy(p, &body, 8); p += 8;
    *p++ = 0x41; *p++ = 0xff; *p++ = 0xe3;          // jmp r11
    *p++ = 0x49; *p++ = 0xbb;
    memcpy(p, &after, 8); p += 8;
    *p++ = 0x41; *p++ = 0xff; *p++ = 0xe3;
    // check mode: the call right after the loop (VB_AFTER: call rel32, a function taking no arguments: the loop has
    // used every argument register) goes through a stub that compares the stock flag first (bnge_terfast_vbcheck)
    unsigned char *after_call = (unsigned char *)after;
    uintptr_t call_target = 0;
    if (atomic_load(&g_check) && after_call[0] == 0xe8) {
        int32_t r0;
        memcpy(&r0, after_call + 1, 4);
        call_target = (uintptr_t)(after_call + 5) + (intptr_t)r0;
        uintptr_t chk = (uintptr_t)bnge_terfast_vbcheck;
        p = stub + 96;
        static const unsigned char C[] = {0x50, 0x51, 0x52, 0x56, 0x57, 0x41, 0x50, 0x41, 0x51, 0x41, 0x52, 0x41, 0x53,
                                          0x48, 0x83, 0xec, 0x10, 0x48, 0x89, 0xdf, 0x48, 0xb8};   // (9 pushes after the
                                                                                                    // call: rsp 16-aligned)
        memcpy(p, C, sizeof C); p += sizeof C;
        memcpy(p, &chk, 8); p += 8;
        static const unsigned char D[] = {0xff, 0xd0, 0x48, 0x83, 0xc4, 0x10, 0x41, 0x5b, 0x41, 0x5a, 0x41, 0x59, 0x41,
                                          0x58, 0x5f, 0x5e, 0x5a, 0x59, 0x58, 0x49, 0xbb};
        memcpy(p, D, sizeof D); p += sizeof D;
        memcpy(p, &call_target, 8); p += 8;
        *p++ = 0x41; *p++ = 0xff; *p++ = 0xe3;      // jmp r11
    }
    long pagesz = sysconf(_SC_PAGESIZE);
    if (mprotect(stub, (size_t)pagesz, PROT_READ | PROT_EXEC) != 0) return 0;
    if (call_target) {
        uintptr_t pg = (uintptr_t)after_call & ~(uintptr_t)(pagesz - 1);
        size_t ln = (size_t)(((uintptr_t)after_call + 5 - pg + pagesz - 1) & ~(uintptr_t)(pagesz - 1));
        if (mprotect((void *)pg, ln, PROT_READ | PROT_WRITE | PROT_EXEC) == 0) {
            int32_t rc = (int32_t)((intptr_t)(stub + 96) - (intptr_t)(after_call + 5));
            memcpy(after_call + 1, &rc, 4);
            __sync_synchronize();
            mprotect((void *)pg, ln, PROT_READ | PROT_EXEC);
        }
    }
    g_vb_after = after;
    g_vb_stock = (uintptr_t)(stub + 32);
    uintptr_t page = (uintptr_t)site & ~(uintptr_t)(pagesz - 1);
    size_t len = (size_t)(((uintptr_t)site + 5 - page + pagesz - 1) & ~(uintptr_t)(pagesz - 1));
    if (mprotect((void *)page, len, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) return 0;
    int32_t rel = (int32_t)((intptr_t)stub - (intptr_t)(site + 5));
    site[0] = 0xe9;
    memcpy(site + 1, &rel, 4);
    __sync_synchronize();
    mprotect((void *)page, len, PROT_READ | PROT_EXEC);
    return 1;
}

static void *watch_flag(void *arg) {
    (void)arg;
    char flag[512];
    bnge_temp_path(flag, sizeof flag, BNGE_LEVEL_FLAG);
    // BNGEARTH_TERFAST_CTL=1 (A/B tests in one session, tools: the V0 runner): <user>/temp/beamngearth_terfast_ctl
    // starting with '0' keeps the stock scans in the level too
    char ctl[512];
    bnge_temp_path(ctl, sizeof ctl, "beamngearth_terfast_ctl");
    const char *ce = getenv("BNGEARTH_TERFAST_CTL");
    int use_ctl = ce && *ce && strcmp(ce, "0");
    int on = -1, held = 0;
    unsigned long long shown = ~0ULL;
    int ticks = 0;
    for (;;) {
        int want = access(flag, F_OK) == 0;
        if (want && use_ctl) {
            FILE *f = fopen(ctl, "r");
            int c = f ? fgetc(f) : EOF;
            if (f) fclose(f);
            held = c == '0';
            if (held) want = 0;
        }
        atomic_store(&g_on, want);
        unsigned long long done = atomic_load(&st_bounds) + atomic_load(&st_mats) + atomic_load(&st_holes);
        // the status file: at every level change, and every ~5 s while cells are being scanned
        if (want != on || (done != shown && ++ticks >= 20)) {
            char msg[512];
            snprintf(msg, sizeof msg, "%s: cell scans fast %llu bounds / %llu layers / %llu holes, stock %llu (%s)", want ?
                "applied (BeamNG.Earth level)" : held ? "ready (held stock by BNGEARTH_TERFAST_CTL)" :
                "ready (stock outside the BeamNG.Earth level)",
                atomic_load(&st_bounds), atomic_load(&st_mats), atomic_load(&st_holes), atomic_load(&st_stock), g_found);
            if (atomic_load(&g_check)) {
                size_t l = strlen(msg);
                snprintf(msg + l, sizeof msg - l, "; check: %llu differences (bounds %llu, layers %llu, holes %llu of "
                         "%llu checked, %llu of them found only by the fast scan; hole checks not paired %llu)",
                         atomic_load(&st_diff), atomic_load(&st_diff_b), atomic_load(&st_diff_m), atomic_load(&st_diff_h),
                         atomic_load(&st_holes_checked), atomic_load(&st_diff_h_fast), atomic_load(&st_pend_lost));
            }
            report(msg);
            on = want; shown = done; ticks = 0;
        }
        struct timespec ts = {0, BNGE_FLAG_POLL_MS * 1000000L};
        nanosleep(&ts, NULL);
    }
    return NULL;
}


static const char *found_once(const struct bnge_main *m, const struct bnge_sig *s, uintptr_t *at, char *why, size_t n) {
    int k = bnge_sig_find(m, s, at);
    if (k == 1) return NULL;
    snprintf(why, n, "%s: %d matches of its code", s->name, k);
    return why;
}

// NULL when every function and constant is as the patch expects, else why not (the status file says it).
static int is_code(const struct bnge_main *m, uintptr_t a, size_t n) { return bnge_mapped(m, a - m->base, n, PF_X); }

static const char *tf_locate(const struct bnge_main *m, struct tf_targets *t, char *why, size_t n) {
    memset(t, 0, sizeof *t);
    if (found_once(m, &tf_bounds, &t->bounds, why, n) || found_once(m, &tf_mats, &t->mats, why, n) ||
        found_once(m, &tf_vb, &t->vb, why, n)) return why;
    t->xform = bnge_rel_target(t->bounds, B_XFORM_REL);
    t->get_transform = bnge_rel_target(t->bounds, B_GETXF_DISP);
    t->mats_tail = bnge_rel_target(t->mats, M_TAIL_REL);
    if (!is_code(m, t->xform, tf_xform.len) || !bnge_sig_at(&tf_xform, (const unsigned char *)t->xform))
        return "the world box function (the bounds' tail call) differs";
    if (!is_code(m, t->get_transform, tf_get_transform.len) ||
        !bnge_sig_at(&tf_get_transform, (const unsigned char *)t->get_transform))
        return "SceneObject::getTransform differs";
    if (!is_code(m, t->mats_tail, tf_mats_tail.len) || !bnge_sig_at(&tf_mats_tail, (const unsigned char *)t->mats_tail))
        return "the layer scan's tail call differs";
    // the constants the replacements reproduce: the box's start (+-FLT_MAX), the radius' 0.5, the hole scan's 1 / 64
    uintptr_t lo = bnge_rel_target(t->bounds, B_INIT_LO_DISP), hi = bnge_rel_target(t->bounds, B_INIT_HI_DISP);
    uintptr_t half = bnge_rel_target(t->bounds, B_HALF_DISP), step = bnge_rel_target(t->vb, VB_STEP_DISP);
    if (!bnge_mapped(m, lo - m->base, 8, PF_R) || !bnge_mapped(m, hi - m->base, 16, PF_R) ||
        !bnge_mapped(m, half - m->base, 4, PF_R) || !bnge_mapped(m, step - m->base, 4, PF_R))
        return "a constant of the cell functions is not mapped";
    const float big = 3.4028234663852886e+38f;
    float init[6], h;
    memcpy(init, (const void *)hi, 16);
    memcpy(init + 4, (const void *)lo, 8);
    memcpy(&h, (const void *)half, 4);
    memcpy(&t->step_k, (const void *)step, 4);
    if (init[0] != big || init[1] != big || init[2] != big || init[3] != -big || init[4] != -big || init[5] != -big)
        return "the cell box's start values differ";
    if (h != 0.5f || t->step_k != 0.015625f) return "a constant of the cell functions differs";
    if (memcmp((const void *)(t->bounds + ENTRY_SKIP), ENTRY_BOUNDS, ENTRY_LEN) != 0 ||
        memcmp((const void *)(t->mats + ENTRY_SKIP), ENTRY_MATS, ENTRY_LEN) != 0)
        return "a function's entry differs";
    t->vb_scan = t->vb + VB_SCAN_OFF;
    t->vb_body = t->vb + VB_BODY_OFF;
    t->vb_after = t->vb + VB_AFTER_OFF;
    if (memcmp((const void *)t->vb_scan, VB_SITE, sizeof VB_SITE) != 0) return "the hole scan differs";
    return NULL;
}

#ifndef BNGE_SIGTEST
__attribute__((constructor)) static void bngearth_terfast(void) {
    if (!bnge_is_main_game()) return;
    if (getenv("BNGEARTH_TERFAST_OFF")) { report_sticky("off (BNGEARTH_TERFAST_OFF set)"); return; }
    struct bnge_main m;
    bnge_find_main(&m);
    if (!m.seen) { report_sticky("not applied: the game's code was not found"); return; }
    char why[160], msg[400], build[48];
    bnge_build_id(build, sizeof build);
    struct tf_targets t;
    if (tf_locate(&m, &t, why, sizeof why)) {
        snprintf(msg, sizeof msg, "not applied: another game build (%s; build id %s); stock", why, build);
        report_sticky(msg);
        return;
    }
    unsigned char *tramp = mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (tramp == MAP_FAILED) { report_sticky("not applied: no memory for the trampolines"); return; }
    g_get_transform = t.get_transform;
    g_xform = (xform_fn)t.xform;
    g_mats_tail = (cell_fn)t.mats_tail;
    g_stock_bounds = (cell_fn)tramp;
    g_stock_mats = (cell_fn)(tramp + 64);
    atomic_store(&g_check, getenv("BNGEARTH_TERFAST_CHECK") != NULL);
    // (the flag off until the watcher sees the level: the replacements run the stock functions meanwhile)
    if (!hook(t.bounds, ENTRY_BOUNDS, tramp, fast_bounds)) { report_sticky("not applied: the bounds entry could not be patched"); return; }
    int mats_ok = hook(t.mats, ENTRY_MATS, tramp + 64, fast_mats);
    mprotect(tramp, 4096, PROT_READ | PROT_EXEC);
    int holes_ok = hook_holes(&t);
    snprintf(g_found, sizeof g_found, "found by signature: bounds %#lx, layers %#lx, holes %#lx; build id %s%s%s",
             (unsigned long)(t.bounds - m.base), (unsigned long)(t.mats - m.base), (unsigned long)(t.vb_scan - m.base), build,
             mats_ok ? "" : "; HALF APPLIED: the layer scan's entry could not be patched",
             holes_ok ? "" : "; the hole scan could not be patched");
    report(mats_ok ? "installed (stock until the BeamNG.Earth level starts)" : "half applied: the layer scan's entry could not be patched");
    pthread_t th;
    if (pthread_create(&th, NULL, watch_flag, NULL) != 0) { report_sticky("not applied: no thread for the level flag (stock)"); return; }
    pthread_detach(th);
}
#endif
