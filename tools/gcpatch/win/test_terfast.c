// Runs the Windows terfast code (enginepatch_terfast_code.py's bytes, built by build_terfast.sh) on Linux, laid out as
// the launcher lays it out in the game, on stand-in cells / block / TerrainFile with the Windows build's offsets, and
// compares every result with the engine's loops written out sample by sample (two modulos per sample, as the
// disassembly reads). test_terfast.sh builds and runs it.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#define MS __attribute__((ms_abi))
static int fails;
#define CHECK(c, ...) do { if (!(c)) { if (fails++ < 20) { printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } } while (0)

static unsigned char *readall(const char *path, size_t *n) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(2); }
    unsigned char *buf = malloc(0x2000);
    *n = fread(buf, 1, 0x2000, f);
    fclose(f);
    return buf;
}

// stand-ins (the Windows offsets)
struct file { char pad[0x20]; uint32_t size; uint32_t pad2; uint8_t *layers; char pad3[0x10]; uint16_t *heights; };
struct owner { char pad[8]; struct file *file; };
struct holder { char pad[0x20]; struct owner *owner; };
static float g_matrix[16];
static int g_xforms, g_tails;
static void *g_last_world;
static MS const void *get_transform(void *block) { (void)block; return g_matrix; }
static MS void xform(void *world, const void *m, const void *box) { g_xforms++; g_last_world = world; (void)m; (void)box; }
static MS void mats_tail(void *cell) { (void)cell; g_tails++; }

static uint32_t rd32(const void *p, int off) { uint32_t v; memcpy(&v, (const char *)p + off, 4); return v; }

// The engine's loops, sample by sample.
static void ref_bounds(void *cell, const struct file *f, float sq, float hs, float box[6], float *radius) {
    uint32_t x0 = rd32(cell, 0x20), y0 = rd32(cell, 0x24), stride = rd32(cell, 0x28) >> 6, n = f->size;
    float mn[3] = {3.4028234663852886e+38f, 3.4028234663852886e+38f, 3.4028234663852886e+38f}, mx[3] = {-mn[0], -mn[0], -mn[0]};
    for (uint32_t j = 0; j < 65; j++)
        for (uint32_t i = 0; i < 65; i++) {
            float p[3] = {(float)(int64_t)(uint32_t)(x0 + i * stride) * sq, (float)(int64_t)(uint32_t)(y0 + j * stride) * sq,
                          (float)(int)f->heights[((y0 + j) % n) * n + (x0 + i) % n] * hs};
            for (int k = 0; k < 3; k++) { mn[k] = p[k] < mn[k] ? p[k] : mn[k]; mx[k] = mx[k] > p[k] ? mx[k] : p[k]; }
        }
    memcpy(box, mn, 12); memcpy(box + 3, mx, 12);
    float dx = mx[0] - mn[0], dy = mx[1] - mn[1], dz = mx[2] - mn[2];
    *radius = sqrtf(dy * dy + dx * dx + dz * dz) * 0.5f;
}

static uint64_t ref_mats(void *cell, const struct file *f) {
    uint32_t x0 = rd32(cell, 0x20), y0 = rd32(cell, 0x24), stride = rd32(cell, 0x28) >> 6, n = f->size;
    uint64_t m = 0;
    for (uint32_t j = 0; j < 65; j++)
        for (uint32_t i = 0; i < 65; i++) {
            uint8_t l = f->layers[((y0 + j * stride) % n) * n + (x0 + i * stride) % n];
            if (l != 0xff && l <= 0x1f) m |= (uint64_t)1 << l;
        }
    return m;
}

static int ref_holes(void *cell, const struct file *f) {
    uint32_t size = rd32(cell, 0x28), n = f->size;
    if (size > 0x40) return 0;
    float step = (float)(int64_t)size * 0.015625f, fs = (float)(int64_t)n;
    float fx0 = (float)(int32_t)rd32(cell, 0x20), fy0 = (float)(int32_t)rd32(cell, 0x24);
    int found = 0;
    for (int j = 0; j < 65; j++)
        for (int i = 0; i < 65; i++) {
            float x = (float)i * step + fx0, y = (float)j * step + fy0;
            x = x < fs ? x : fs; x = x > 0.0f ? x : 0.0f;
            y = fs < y ? fs : y; y = y > 0.0f ? y : 0.0f;
            if (f->layers[((uint32_t)(int64_t)y % n) * n + (uint32_t)(int64_t)x % n] == 0xff) { found = 1; break; }
        }
    return found;
}

static struct file g_file;
static float g_sq = 1.0f, g_hs = 0.0078125f;
static MS void stock_bounds(void *cell) {       // (the trampoline's stand-in: the reference, with the engine's side effects)
    float box[6], r;
    ref_bounds(cell, &g_file, g_sq, g_hs, box, &r);
    memcpy((char *)cell + 0x5c, box, 24); memcpy((char *)cell + 0x114, &r, 4);
    xform((char *)cell + 0x74, g_matrix, (char *)cell + 0x5c);
}
static MS void stock_mats(void *cell) { uint64_t m = ref_mats(cell, &g_file); memcpy((char *)cell + 0x138, &m, 8); mats_tail(cell); }

int main(int argc, char **argv) {
    if (argc < 6) return 2;
    size_t ns, nc;
    unsigned char *stub = readall(argv[1], &ns), *code = readall(argv[2], &nc);
    unsigned long e_bounds = strtoul(argv[3], 0, 0), e_mats = strtoul(argv[4], 0, 0), e_holes = strtoul(argv[5], 0, 0);
    unsigned char *blk = mmap(NULL, 0x2000, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    memcpy(blk, stub, ns);
    memcpy(blk + 0x100, code, nc);
    uint64_t *st = (uint64_t *)(blk + 0x1000);
    uint32_t *st32 = (uint32_t *)(blk + 0x1000);
    st[0] = (uint64_t)stock_bounds; st[1] = (uint64_t)stock_mats; st[2] = (uint64_t)xform; st[3] = (uint64_t)mats_tail;
    st32[0x30 / 4] = 1; st32[0x34 / 4] = 0; st32[0x38 / 4] = 0x3c4; st32[0x3c / 4] = 0x20; st32[0x40 / 4] = 0x1c0;
    st[0x78 / 8] = (uint64_t)(blk + e_holes);
    typedef void (MS *cell_fn)(void *);
    typedef int (MS *holes_fn)(void *, const void *);
    cell_fn f_bounds = (cell_fn)(blk + e_bounds), f_mats = (cell_fn)(blk + e_mats);
    holes_fn f_holes = (holes_fn)(blk + e_holes);

    void *vtbl[0x1c0 / 8 + 1];
    vtbl[0x1c0 / 8] = (void *)get_transform;
    unsigned char *block = calloc(1, 0x400);
    struct owner own = {.file = &g_file};
    struct holder hold = {.owner = &own};
    memcpy(block, &(void *){vtbl}, 8);
    memcpy(block + 0x260, &(void *){&hold}, 8);
    memcpy(block + 0x340, &g_sq, 4);
    memcpy(block + 0x3c4, &g_hs, 4);
    srand(7);
    int cells = 0;
    for (int n = 128; n <= 1024; n *= 2) {
        g_file.size = (uint32_t)n;
        g_file.layers = malloc((size_t)n * n);
        g_file.heights = malloc((size_t)n * n * 2);
        for (int k = 0; k < n * n; k++) {
            g_file.heights[k] = (uint16_t)(rand() % 65536);
            int r = rand() % 4000;
            g_file.layers[k] = r == 0 ? 0xff : r == 1 ? 40 : (uint8_t)(rand() % 9);
        }
        for (int size = 64; size <= n; size *= 2)
            for (int y0 = 0; y0 < n; y0 += size)
                for (int x0 = 0; x0 < n; x0 += size) {
                    unsigned char *cell = calloc(1, 0x158);
                    uint32_t v[3] = {(uint32_t)x0, (uint32_t)y0, (uint32_t)size};
                    memcpy(cell + 0x20, v, 12);
                    memcpy(cell + 0x50, &block, 8);
                    cells++;
                    if (size == 64) {
                        float box[6], r, rb;
                        ref_bounds(cell, &g_file, g_sq, g_hs, box, &r);
                        int x = g_xforms;
                        f_bounds(cell);
                        memcpy(&rb, cell + 0x114, 4);
                        CHECK(memcmp(cell + 0x5c, box, 24) == 0 && memcmp(&rb, &r, 4) == 0, "bounds n %d cell %d,%d", n, x0, y0);
                        CHECK(g_xforms == x + 1 && g_last_world == cell + 0x74, "xform call");
                    }
                    uint64_t m = ref_mats(cell, &g_file), got;
                    int t = g_tails;
                    f_mats(cell);
                    memcpy(&got, cell + 0x138, 8);
                    CHECK(got == m && g_tails == t + 1, "mats n %d size %d cell %d,%d: %llx vs %llx", n, size, x0, y0,
                          (unsigned long long)got, (unsigned long long)m);
                    int h = ref_holes(cell, &g_file);
                    cell[0x34] = 0;
                    int rc = f_holes(cell, &g_file);
                    CHECK(rc == 0 && (cell[0x34] != 0) == h, "holes n %d size %d cell %d,%d: %d vs %d", n, size, x0, y0, cell[0x34], h);
                    // check mode: the stock code's results stay, no difference counted
                    st32[0x34 / 4] = 1;
                    uint64_t d0 = st[0x70 / 8];
                    cell[0x34] = 0;
                    CHECK(f_holes(cell, &g_file) == (size <= 64), "holes check mode rc");
                    cell[0x34] = (unsigned char)h;          // (what the stock loop would leave)
                    if (size == 64) f_bounds(cell);
                    f_mats(cell);
                    CHECK(st[0x70 / 8] == d0, "check mode counted a difference");
                    st[0x48 / 8] = 0;
                    st32[0x34 / 4] = 0;
                    free(cell);
                }
        // the level flag off: the stock functions run
        unsigned char *cell = calloc(1, 0x158);
        uint32_t v[3] = {0, 0, 64};
        memcpy(cell + 0x20, v, 12);
        memcpy(cell + 0x50, &block, 8);
        st32[0x30 / 4] = 0;
        uint64_t s0 = st[0x68 / 8];
        f_bounds(cell); f_mats(cell);
        CHECK(f_holes(cell, &g_file) == 1, "holes with the flag off");
        CHECK(st[0x68 / 8] == s0 + 3, "stock count");
        st32[0x30 / 4] = 1;
        free(cell);
        free(g_file.layers); free(g_file.heights);
    }
    printf("test_terfast: %d cells, %s (%d failures)\n", cells, fails ? "FAILED" : "ok", fails);
    return fails != 0;
}
