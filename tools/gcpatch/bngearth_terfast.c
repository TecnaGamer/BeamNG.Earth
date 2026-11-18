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
// Exact build only: both functions (whole) and the two engine functions the replacements call are compared by hash at
// startup; on any difference nothing is patched and <user>/temp/beamngEarth_terfast.txt says so. The entry of each
// function (after its endbr64) is replaced by a 14-byte jump before the game's code runs; the displaced instructions
// (position independent) live in a trampoline, so the stock function stays callable: it runs outside the BeamNG.Earth
// level (the level flag), for any input the fast path does not expect, and for BNGEARTH_TERFAST_CHECK=1, which runs
// both and counts differences in the status file. Off: BNGEARTH_TERFAST_OFF=1.
#include "bngearth_common.h"
#include <pthread.h>
#include <stdatomic.h>
#include <sys/mman.h>
#include <math.h>

#define TAG "bngearth-terfast"
#define STATUS_FILE "beamngEarth_terfast.txt"

// BeamNG.drive 0.40.4 Linux (BinLinux/BeamNG.drive.x64 sha256 4fa0771d463ab31f..., the 2026-11-06 build): vaddrs
#define FN_BOUNDS 0x1735dc0UL       // TerrCell::_updateBounds()
#define FN_BOUNDS_LEN 784
#define FN_BOUNDS_HASH 0x688389e2581ddf6dULL
#define FN_MATS 0x1735c00UL         // TerrCell::_updateMaterials()
#define FN_MATS_LEN 448
#define FN_MATS_HASH 0xc9167855df1b6175ULL
#define FN_XFORM 0x12b00d0UL        // (cell + 0x74: its world box / OBB, const MatrixF *, const Box3F *)
#define FN_XFORM_HASH 0x4090fa4293264356ULL
#define FN_MATS_TAIL 0x1733c30UL    // (cell): after the mask, for cells with a vertex buffer slot
#define FN_MATS_TAIL_HASH 0x91776d6d5c9e59b9ULL
#define FN_VB 0x1734b30UL           // TerrCell::_updateVertexBuffer()
#define FN_VB_LEN 2624
#define FN_VB_HASH 0x25953ffe82c2346aULL
#define VB_SCAN 0x1734c55UL         // mov eax, [rbx + 0x28]; cmp eax, 0x40; ja VB_AFTER: the hole scan (rbx cell, r13 file)
#define VB_SCAN_BODY 0x1734c61UL    // its loop (eax = the cell's size)
#define VB_AFTER 0x1734d72UL
#define VB_STEP_K 0x35a8810UL       // float 1 / 64
#define CELL_HOLES 0x34             // u8: the leaf holds a hole sample (layer 255)
#define FN_GET_TRANSFORM 0x80cd80UL // SceneObject::getTransform: lea rax, [rdi + 0x130]
#define HEAD_HASH_LEN 48
#define ENTRY_SKIP 4                // endbr64
#define ENTRY_LEN 14                // the displaced instructions (whole, position independent)
static const unsigned char ENTRY_BOUNDS[ENTRY_LEN] = {0x55, 0x48, 0x8d, 0x77, 0x5c, 0x48, 0x89, 0xe5, 0x41, 0x57, 0x41, 0x56, 0x41, 0x55};
static const unsigned char ENTRY_MATS[ENTRY_LEN] = {0x55, 0x48, 0x89, 0xe5, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54, 0x49, 0x89, 0xfc, 0x53};

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

static uintptr_t g_base;
static cell_fn g_stock_bounds, g_stock_mats, g_mats_tail;
static xform_fn g_xform;
static atomic_int g_on, g_check;
static atomic_ullong st_bounds, st_mats, st_holes, st_stock, st_diff;
static uintptr_t g_vb_after, g_vb_stock;        // where the hole scan's stub goes on: past the loop / into the stock loop
static float g_step_k;
static void *g_pending_cell;                    // (check mode, main thread: the leaf whose stock hole flag is compared next)
static int g_pending_flag;

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
    if (!atomic_load_explicit(&g_on, memory_order_relaxed) || !file || cell_file(cell, &block, &n) != file) {
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
        g_pending_cell = cell;
        g_pending_flag = found;
        return g_vb_stock;
    }
    if (found) *((unsigned char *)cell + CELL_HOLES) = 1;
    return g_vb_after;
}

static void fast_bounds(void *cell) {
    if (g_pending_cell == cell) {               // (check mode: the stock hole scan of this leaf has run)
        if ((*((unsigned char *)cell + CELL_HOLES) != 0) != g_pending_flag)
            atomic_fetch_add_explicit(&st_diff, 1, memory_order_relaxed);
        g_pending_cell = NULL;
    }
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
        if (memcmp((char *)cell + CELL_BOX, box, sizeof box) != 0 || memcmp(&rr, &radius, 4) != 0)
            atomic_fetch_add_explicit(&st_diff, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&st_bounds, 1, memory_order_relaxed);
        return;
    }
    memcpy((char *)cell + CELL_BOX, box, sizeof box);
    memcpy((char *)cell + CELL_RADIUS, &radius, 4);
    void **vtbl = rdp(block, 0);
    transform_fn get = (transform_fn)vtbl[BLOCK_VTBL_TRANSFORM / 8];
    const void *matrix = (uintptr_t)get == g_base + FN_GET_TRANSFORM ? (const char *)block + BLOCK_MATRIX : get(block);
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
        if (got != mask) atomic_fetch_add_explicit(&st_diff, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&st_mats, 1, memory_order_relaxed);
        return;
    }
    memcpy((char *)cell + CELL_MASK, &mask, 8);
    atomic_fetch_add_explicit(&st_mats, 1, memory_order_relaxed);
    if ((int32_t)rd32(cell, CELL_VB) != -1) g_mats_tail(cell);
}

static uint64_t fnv(const unsigned char *p, size_t n) {
    uint64_t h = 0xcbf29ce484222325ULL;
    for (size_t i = 0; i < n; i++) h = (h ^ p[i]) * 0x100000001b3ULL;
    return h;
}

static void report(const char *msg) { bnge_report(TAG, STATUS_FILE, msg); }

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
static void hook_holes(const struct bnge_main *m) {
    static const unsigned char SITE[12] = {0x8b, 0x43, 0x28, 0x83, 0xf8, 0x40, 0x0f, 0x87, 0x11, 0x01, 0x00, 0x00};
    if (!bnge_mapped(m, FN_VB, FN_VB_LEN, PF_X) || !bnge_mapped(m, VB_STEP_K, 4, PF_R) ||
        fnv((unsigned char *)(m->base + FN_VB), FN_VB_LEN) != FN_VB_HASH ||
        memcmp((void *)(m->base + VB_SCAN), SITE, sizeof SITE) != 0) return;
    memcpy(&g_step_k, (void *)(m->base + VB_STEP_K), 4);
    if (g_step_k != 0.015625f) return;
    unsigned char *site = (unsigned char *)(m->base + VB_SCAN);
    unsigned char *stub = bnge_near_page((uintptr_t)site);
    if (!stub) return;
    unsigned char *p = stub;
    uintptr_t fn = (uintptr_t)bnge_terfast_holes, body = m->base + VB_SCAN_BODY, after = m->base + VB_AFTER;
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
    long pagesz = sysconf(_SC_PAGESIZE);
    if (mprotect(stub, (size_t)pagesz, PROT_READ | PROT_EXEC) != 0) return;
    g_vb_after = after;
    g_vb_stock = (uintptr_t)(stub + 32);
    uintptr_t page = (uintptr_t)site & ~(uintptr_t)(pagesz - 1);
    size_t len = (size_t)(((uintptr_t)site + 5 - page + pagesz - 1) & ~(uintptr_t)(pagesz - 1));
    if (mprotect((void *)page, len, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) return;
    int32_t rel = (int32_t)((intptr_t)stub - (intptr_t)(site + 5));
    site[0] = 0xe9;
    memcpy(site + 1, &rel, 4);
    __sync_synchronize();
    mprotect((void *)page, len, PROT_READ | PROT_EXEC);
}

static void *watch_flag(void *arg) {
    (void)arg;
    char flag[512];
    bnge_temp_path(flag, sizeof flag, BNGE_LEVEL_FLAG);
    int on = -1;
    unsigned long long shown = ~0ULL;
    int ticks = 0;
    for (;;) {
        int want = access(flag, F_OK) == 0;
        atomic_store(&g_on, want);
        unsigned long long done = atomic_load(&st_bounds) + atomic_load(&st_mats) + atomic_load(&st_holes);
        // the status file: at every level change, and every ~5 s while cells are being scanned
        if (want != on || (done != shown && ++ticks >= 20)) {
            char msg[256];
            snprintf(msg, sizeof msg, "%s: cell scans fast %llu bounds / %llu layers / %llu holes, stock %llu%s", want ?
                "applied (BeamNG.Earth level)" : "ready (stock outside the BeamNG.Earth level)",
                atomic_load(&st_bounds), atomic_load(&st_mats), atomic_load(&st_holes), atomic_load(&st_stock), "");
            if (atomic_load(&g_check)) {
                size_t l = strlen(msg);
                snprintf(msg + l, sizeof msg - l, "; check: %llu differences", atomic_load(&st_diff));
            }
            report(msg);
            on = want; shown = done; ticks = 0;
        }
        struct timespec ts = {0, BNGE_FLAG_POLL_MS * 1000000L};
        nanosleep(&ts, NULL);
    }
    return NULL;
}

__attribute__((constructor)) static void bngearth_terfast(void) {
    if (!bnge_is_main_game()) return;
    if (getenv("BNGEARTH_TERFAST_OFF")) { report("off (BNGEARTH_TERFAST_OFF set)"); return; }
    struct bnge_main m;
    bnge_find_main(&m);
    if (!m.seen) { report("not applied: the game's code was not found"); return; }
    if (!bnge_mapped(&m, FN_BOUNDS, FN_BOUNDS_LEN, PF_X) || !bnge_mapped(&m, FN_MATS, FN_MATS_LEN, PF_X) ||
        !bnge_mapped(&m, FN_XFORM, HEAD_HASH_LEN, PF_X) || !bnge_mapped(&m, FN_MATS_TAIL, HEAD_HASH_LEN, PF_X) ||
        !bnge_mapped(&m, FN_GET_TRANSFORM, 8, PF_X) ||
        fnv((unsigned char *)(m.base + FN_BOUNDS), FN_BOUNDS_LEN) != FN_BOUNDS_HASH ||
        fnv((unsigned char *)(m.base + FN_MATS), FN_MATS_LEN) != FN_MATS_HASH ||
        fnv((unsigned char *)(m.base + FN_XFORM), HEAD_HASH_LEN) != FN_XFORM_HASH ||
        fnv((unsigned char *)(m.base + FN_MATS_TAIL), HEAD_HASH_LEN) != FN_MATS_TAIL_HASH) {
        report("not applied: another game build (the terrain cell functions differ)");
        return;
    }
    unsigned char *tramp = mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (tramp == MAP_FAILED) { report("not applied: no memory for the trampolines"); return; }
    g_base = m.base;
    g_xform = (xform_fn)(m.base + FN_XFORM);
    g_mats_tail = (cell_fn)(m.base + FN_MATS_TAIL);
    g_stock_bounds = (cell_fn)tramp;
    g_stock_mats = (cell_fn)(tramp + 64);
    atomic_store(&g_check, getenv("BNGEARTH_TERFAST_CHECK") != NULL);
    // (the flag off until the watcher sees the level: the replacements run the stock functions meanwhile)
    if (!hook(m.base + FN_BOUNDS, ENTRY_BOUNDS, tramp, fast_bounds)) { report("not applied: the bounds entry could not be patched"); return; }
    if (!hook(m.base + FN_MATS, ENTRY_MATS, tramp + 64, fast_mats)) { report("half applied: the layer scan's entry could not be patched"); }
    mprotect(tramp, 4096, PROT_READ | PROT_EXEC);
    hook_holes(&m);
    pthread_t t;
    if (pthread_create(&t, NULL, watch_flag, NULL) != 0) { report("installed, but no thread for the level flag: stock"); return; }
    pthread_detach(t);
}
