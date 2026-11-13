// terfast for the Windows game (tools/gcpatch/README.md "terfast", enginepatch_terfast.py): the three sample scans of
// a terrain block swap's cell tree (TerrCell::_updateBounds, ::_updateMaterials, the hole scan at the start of
// ::_updateVertexBuffer) without their two divisions per sample, as bngearth_terfast.c does for the Linux game. Built
// on Linux into position-independent Win64-ABI machine code (build_terfast.sh: freestanding, no libc, .text + .rodata
// flat) that the launcher writes into the game's memory next to its state (struct tf_state, the block's data page).
//
// Windows build facts (BeamNG.drive.x64.exe 0.40.4, 2026-11-06; the same TerrCell layout as the Linux build):
// TerrainFile * = [[block + 0x260] + 0x20] + 0x8 (Linux: + 0x18), height scale at block + 0x3c4 (Linux 0x3e4),
// getTransform = vtable slot 0x1c0 / 8, _updateMaterials always ends in its tail call.
#include <stdint.h>

#define MS __attribute__((ms_abi))
#define SPAN 65

#define CELL_X 0x20
#define CELL_Y 0x24
#define CELL_SIZE 0x28
#define CELL_HOLES 0x34
#define CELL_BLOCK 0x50
#define CELL_BOX 0x5c
#define CELL_WORLD 0x74
#define CELL_RADIUS 0x114
#define CELL_MASK 0x138
#define BLOCK_FILE 0x260
#define BLOCK_SQUARE 0x340
#define FILE_SIZE 0x20
#define FILE_LAYERS 0x28
#define FILE_HEIGHTS 0x40

// The block's data page (enginepatch_terfast.py STATE: the same offsets).
struct tf_state {
    uint64_t stock_bounds;      // 0x00 trampolines: the displaced entry instructions, then on into the stock function
    uint64_t stock_mats;        // 0x08
    uint64_t xform;             // 0x10 (cell + 0x74, const MatrixF *, const Box3F *)
    uint64_t mats_tail;         // 0x18 (cell)
    uint64_t vb_after;          // 0x20 the hole scan's stub goes on here (past the stock loop) ...
    uint64_t vb_body;           // 0x28 ... or here (the stock loop; eax = the cell's size)
    uint32_t on;                // 0x30 the BeamNG.Earth level runs (the launcher follows the level flag)
    uint32_t check;             // 0x34 run the stock code too and count differences
    uint32_t hscale_off;        // 0x38 TerrainBlock: the height scale (float)
    uint32_t holder_off;        // 0x3c the Resource's holder: its pointer to the TerrainFile's owner
    uint32_t transform_slot;    // 0x40 vtable byte offset of getTransform
    uint32_t pending_flag;      // 0x44
    uint64_t pending_cell;      // 0x48 (check mode: the leaf whose stock hole flag is compared at its bounds)
    uint64_t n_bounds, n_mats, n_holes, n_stock, n_diff;    // 0x50 .. 0x70
    uint64_t holes_fn;          // 0x78 tf_holes (the stub's call)
};
extern struct tf_state tf_state __attribute__((visibility("hidden")));

typedef void (MS *cell_fn)(void *);
typedef void (MS *xform_fn)(void *, const void *, const void *);
typedef const void *(MS *transform_fn)(void *);

static inline uint32_t rd32(const void *p, uint32_t off) { uint32_t v; __builtin_memcpy(&v, (const char *)p + off, 4); return v; }
static inline float rdf(const void *p, uint32_t off) { float v; __builtin_memcpy(&v, (const char *)p + off, 4); return v; }
static inline void *rdp(const void *p, uint32_t off) { void *v; __builtin_memcpy(&v, (const char *)p + off, 8); return v; }

static inline const void *cell_file(const struct tf_state *s, const void *cell, void **block_out, uint32_t *fsize) {
    void *block = rdp(cell, CELL_BLOCK);
    if (!block) return 0;
    void *holder = rdp(block, BLOCK_FILE);
    if (!holder) return 0;
    void *res = rdp(holder, s->holder_off);
    if (!res) return 0;
    const void *file = rdp(res, 0x8);
    if (!file) return 0;
    uint32_t n = rd32(file, FILE_SIZE);
    if (n < SPAN || n > 16384 || !rdp(file, FILE_LAYERS) || !rdp(file, FILE_HEIGHTS)) return 0;
    *block_out = block;
    *fsize = n;
    return file;
}

static inline int same_bytes(const void *a, const void *b, uint32_t n) {
    const unsigned char *x = a, *y = b;
    for (uint32_t i = 0; i < n; i++) if (x[i] != y[i]) return 0;
    return 1;
}

__attribute__((used)) MS void tf_bounds(void *cell) {
    struct tf_state *s = &tf_state;
    if (s->pending_cell == (uint64_t)(uintptr_t)cell) {     // (check mode: the stock hole scan of this leaf has run)
        if ((*((unsigned char *)cell + CELL_HOLES) != 0) != (s->pending_flag != 0)) s->n_diff++;
        s->pending_cell = 0;
    }
    void *block = 0;
    uint32_t n = 0;
    const void *file = s->on ? cell_file(s, cell, &block, &n) : 0;
    uint32_t x0 = rd32(cell, CELL_X), y0 = rd32(cell, CELL_Y), stride = rd32(cell, CELL_SIZE) >> 6;
    float hs = file ? rdf(block, s->hscale_off) : 0.0f, sq = file ? rdf(block, BLOCK_SQUARE) : 0.0f;
    if (!file || !(hs >= 0.0f) || !(sq > 0.0f) || stride == 0 || x0 > 0x10000000u || y0 > 0x10000000u || stride > 0x10000u) {
        s->n_stock++;
        ((cell_fn)(uintptr_t)s->stock_bounds)(cell);
        return;
    }
    const uint16_t *h = rdp(file, FILE_HEIGHTS);
    uint32_t col[SPAN];
    for (uint32_t i = 0; i < SPAN; i++) col[i] = (x0 + i) % n;        // (the engine indexes the heights unstrided)
    int plain = col[SPAN - 1] == col[0] + SPAN - 1;
    unsigned lo = 0xffff, hi = 0;
    for (uint32_t j = 0; j < SPAN; j++) {
        const uint16_t *row = h + (uint64_t)((y0 + j) % n) * n;
        if (plain) {
            const uint16_t *p = row + col[0];
            for (uint32_t i = 0; i < SPAN; i++) { unsigned v = p[i]; lo = v < lo ? v : lo; hi = v > hi ? v : hi; }
        } else {
            for (uint32_t i = 0; i < SPAN; i++) { unsigned v = row[col[i]]; lo = v < lo ? v : lo; hi = v > hi ? v : hi; }
        }
    }
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
    float r2 = dy * dy + dx * dx;
    r2 = r2 + dz * dz;
    float radius = __builtin_sqrtf(r2) * 0.5f;
    s->n_bounds++;
    if (s->check) {
        ((cell_fn)(uintptr_t)s->stock_bounds)(cell);
        if (!same_bytes((char *)cell + CELL_BOX, box, sizeof box) || !same_bytes((char *)cell + CELL_RADIUS, &radius, 4))
            s->n_diff++;
        return;
    }
    __builtin_memcpy((char *)cell + CELL_BOX, box, sizeof box);
    __builtin_memcpy((char *)cell + CELL_RADIUS, &radius, 4);
    void **vtbl = rdp(block, 0);
    const void *matrix = ((transform_fn)vtbl[s->transform_slot / 8])(block);
    ((xform_fn)(uintptr_t)s->xform)((char *)cell + CELL_WORLD, matrix, (char *)cell + CELL_BOX);
}

__attribute__((used)) MS void tf_mats(void *cell) {
    struct tf_state *s = &tf_state;
    void *block = 0;
    uint32_t n = 0;
    const void *file = s->on ? cell_file(s, cell, &block, &n) : 0;
    uint32_t x0 = rd32(cell, CELL_X), y0 = rd32(cell, CELL_Y), stride = rd32(cell, CELL_SIZE) >> 6;
    if (!file || stride == 0 || x0 > 0x10000000u || y0 > 0x10000000u || stride > 0x10000u) {
        s->n_stock++;
        ((cell_fn)(uintptr_t)s->stock_mats)(cell);
        return;
    }
    const uint8_t *layers = rdp(file, FILE_LAYERS);
    uint32_t col[SPAN];
    for (uint32_t i = 0; i < SPAN; i++) col[i] = (x0 + i * stride) % n;
    int plain = stride == 1 && col[SPAN - 1] == col[0] + SPAN - 1;
    uint64_t mask = 0;
    for (uint32_t j = 0; j < SPAN; j++) {
        const uint8_t *row = layers + (uint64_t)((y0 + j * stride) % n) * n;
        if (plain) {
            const uint8_t *p = row + col[0];
            for (uint32_t i = 0; i < SPAN; i++) { unsigned l = p[i]; if (l <= 31) mask |= (uint64_t)1 << l; }
        } else {
            for (uint32_t i = 0; i < SPAN; i++) { unsigned l = row[col[i]]; if (l <= 31) mask |= (uint64_t)1 << l; }
        }
    }
    s->n_mats++;
    if (s->check) {
        ((cell_fn)(uintptr_t)s->stock_mats)(cell);          // (with its tail call)
        uint64_t got;
        __builtin_memcpy(&got, (char *)cell + CELL_MASK, 8);
        if (got != mask) s->n_diff++;
        return;
    }
    __builtin_memcpy((char *)cell + CELL_MASK, &mask, 8);
    ((cell_fn)(uintptr_t)s->mats_tail)(cell);
}

// The hole scan (the stub's call; `file` = the engine's r14). 0: the flag is set, go on past the stock loop; 1: run
// the stock loop. As the engine: sample (i, j) at clamp(float(x0) + float(i) * step, 0, n) truncated, modulo n (the
// same for y), step = float(size) / 64; a row ends at its first hole.
__attribute__((used)) MS int tf_holes(void *cell, const void *file) {
    struct tf_state *s = &tf_state;
    uint32_t size = rd32(cell, CELL_SIZE);
    if (size > 0x40) return 0;                              // (the engine skips the scan too)
    void *block = 0;
    uint32_t n = 0;
    if (!s->on || !file || cell_file(s, cell, &block, &n) != file) { s->n_stock++; return 1; }
    const uint8_t *layers = rdp(file, FILE_LAYERS);
    float step = (float)(int64_t)size * 0.015625f, fs = (float)(int64_t)n;
    float fx0 = (float)(int32_t)rd32(cell, CELL_X), fy0 = (float)(int32_t)rd32(cell, CELL_Y);
    uint32_t col[SPAN], row[SPAN];
    int plain = 1;
    for (int i = 0; i < SPAN; i++) {
        float x = (float)i * step + fx0, y = (float)i * step + fy0;
        x = x < fs ? x : fs; x = x > 0.0f ? x : 0.0f;
        y = fs < y ? fs : y; y = y > 0.0f ? y : 0.0f;
        col[i] = (uint32_t)(int64_t)x % n;
        row[i] = (uint32_t)(int64_t)y % n;
        if (col[i] != col[0] + (uint32_t)i) plain = 0;
    }
    int found = 0;
    for (int j = 0; j < SPAN && !found; j++) {
        const uint8_t *r = layers + (uint64_t)row[j] * n;
        if (plain) { const uint8_t *p = r + col[0]; for (int i = 0; i < SPAN; i++) if (p[i] == 0xff) { found = 1; break; } }
        else for (int i = 0; i < SPAN; i++) if (r[col[i]] == 0xff) { found = 1; break; }
    }
    s->n_holes++;
    if (s->check) {
        s->pending_cell = (uint64_t)(uintptr_t)cell;
        s->pending_flag = (uint32_t)found;
        return 1;
    }
    if (found) *((unsigned char *)cell + CELL_HOLES) = 1;
    return 0;
}
