// texkeep: keep BeamNG.drive's packed terrain textures across terrain material rebuilds (BeamNG.Earth; README.md).
// Part of the same preload library as bngearth_gcpatch.c (one LD_PRELOAD for both; its own switch and status file).
//
// Every .ter swap (and every TerrainBlock::updateMaterial, the mod's setBase) makes the engine's next draw drop the
// terrain's shared material data and rebuild it (TerrainCellMaterial init). For each material it asks the texture
// manager for 2 sets x 3 packed textures, art/terrainMaterialCache/<xxh64 of the 4 source paths>.dds, and the
// manager creates each one again from its DDS (a Vulkan image + a staging upload): ~92 % of that frame's CPU, of which
// ~69 % are packs whose content did not change (only the base colour pack, the block's photo, is new per block). The
// manager's own 5 s cache of released textures does not catch them.
//
// The hook replaces slot 5 (createTexture(const Path&, GFXTextureProfile*)) of the texture manager's vtables. Only for
// the call from TerrainCellMaterial's pack loader where the engine has just checked that the cache file is up to date
// (its return address 0x173f2ee), it hands back a texture it keeps (one strong reference of its own), if it has one
// for the same path and profile; otherwise it calls the stock function and keeps the result. The loader's second call
// (0x173f71e, after it re-packed an outdated or missing cache file) always goes to the stock function and drops a kept
// texture of that path first. Kept textures are released least-recently-used first beyond MAX_KEEP entries or
// MAX_KEEP_MB, and all of them when the BeamNG.Earth level ends (the level flag gone). Every other caller and every
// other level: the stock function, nothing kept.
//
// Exact build only (BeamNG.drive 0.40.4 Linux, the 2026-11-06 executable; ported from the 2026-10-29 one on 2026-11-13): the code and data it relies on are compared
// byte for byte at startup (the vtable slots' relocated values, createTexture's prologue, both call sites,
// requestDeleteTexture's and destroySelf's code); on any difference it does nothing and says why in
// <user>/temp/beamngEarth_texkeep.txt. Opt-in: BNGEARTH_TEXKEEP=1 (BNGEARTH_TEXKEEP_OFF=1 wins). BNGEARTH_TEXKEEP_DRY=1: installed but
// never hands back a kept texture (counts and times the stock calls only: an A/B baseline with the same overhead).
// BNGEARTH_TEXKEEP_LOG=1: one line per terrain pack call in <user>/temp/beamngEarth_texkeep.log.
//
// Engine facts used (verified in the disassembly):
// - createTexture(this=manager, rsi=Path*, rdx=profile) returns a raw GFXTextureObject* (the caller takes the
//   reference). A Path is 6 String handles (+0x00..+0x28) and flags (+0x30); a String handle points to a rep with the
//   length (u32) at +4 and the characters at +0x20.
// - GFXTextureObject: vptr +0, strong count (i32) +0x10, device +0x50, width / height (u32) +0x80 / +0x84. The count's
//   states: n > 0 alive; 0xfff0bdc1 = 0 and waiting in the manager's delete list (a handle may revive it); -1 / -2 dying
//   / dead. Acquire: n > 0 or 0xfff0bdc1 -> max(n, 0) + 1. Release: n > 1 -> n - 1; 1 -> -1, then
//   vtable[2] destroySelf (0xc7d4d0): lock device+0x88, requestDeleteTexture(device+0xb0 manager, obj) (0xc6a630),
//   unlock. The pack loader calls createTexture with that device mutex held, so a release that would destroy the object
//   while this thread owns the mutex calls requestDeleteTexture itself (what destroySelf does under the lock) instead of
//   locking a second time.
#include "bngearth_common.h"
#include <pthread.h>
#include <stdatomic.h>
#include <sys/mman.h>
#include <strings.h>

#define TAG "bngearth-texkeep"
#define STATUS_FILE "beamngEarth_texkeep.txt"
#define LOG_FILE "beamngEarth_texkeep.log"

// BeamNG.drive 0.40 Linux (BinLinux/BeamNG.drive.x64 sha256 4fa0771d463ab31f...): vaddrs (PIE, base added at runtime)
#define CREATE_TEXTURE 0xc6ece0UL       // GFXTextureManager::createTexture(const Path&, GFXTextureProfile*)
#define REQUEST_DELETE 0xc6a630UL       // GFXTextureManager::requestDeleteTexture(GFXTextureObject*)
#define DESTROY_SELF 0xc7d4d0UL         // GFXTextureObject::destroySelf (vtable[2] of the texture objects)
#define SITE_CACHED_CALL 0x173f2dfUL    // lea rdx,[profile]; mov rsi,r15; mov rdi,r13; call rax  (cache file up to date)
#define SITE_PACKED_CALL 0x173f70fUL    // lea rdx,[profile]; mov rsi,r15; mov rdi,r13; call rbx  (just re-packed)
#define SITE_CALL_LEN 15
static const uintptr_t VTABLE_SLOTS[3] = {0x4d6ad50UL, 0x4de3298UL, 0x4e2fcc8UL};   // slot 5 of the 3 managers' vtables

static const unsigned char SIG_CREATE[24] = {
    0xf3, 0x0f, 0x1e, 0xfa, 0x55, 0x48, 0x89, 0xe5, 0x41, 0x57, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54,
    0x53, 0x48, 0x81, 0xec, 0x28, 0x04, 0x00, 0x00};
static const unsigned char SIG_SITE_CACHED[SITE_CALL_LEN] = {
    0x48, 0x8d, 0x15, 0x5a, 0xe0, 0x7a, 0x03, 0x4c, 0x89, 0xfe, 0x4c, 0x89, 0xef, 0xff, 0xd0};
static const unsigned char SIG_SITE_PACKED[SITE_CALL_LEN] = {
    0x48, 0x8d, 0x15, 0x2a, 0xdc, 0x7a, 0x03, 0x4c, 0x89, 0xfe, 0x4c, 0x89, 0xef, 0xff, 0xd3};
// requestDeleteTexture: the start, up to its use of +0x30 (name), +0x70 (delete time), +0x2c (weak count)
static const unsigned char SIG_REQUEST_DELETE[64] = {
    0xf3, 0x0f, 0x1e, 0xfa, 0x55, 0x48, 0x89, 0xe5, 0x41, 0x57, 0x41, 0x56, 0x41, 0x55, 0x41, 0x54,
    0x49, 0x89, 0xf4, 0x53, 0x48, 0x89, 0xfb, 0x48, 0x8d, 0x7e, 0x30, 0x48, 0x83, 0xec, 0x08, 0xe8,
    0x9c, 0x2a, 0xe0, 0xff, 0x41, 0x89, 0xc0, 0x31, 0xc0, 0x45, 0x84, 0xc0, 0x0f, 0x84, 0x16, 0x01,
    0x00, 0x00, 0x4d, 0x8d, 0x6c, 0x24, 0x2c, 0x41, 0x89, 0x44, 0x24, 0x70, 0xf0, 0x41, 0x83, 0x44};
// destroySelf, whole: rbx = obj+0x50 (device); lock rbx+0x88; rdi = [rbx+0xb0]; requestDeleteTexture; unlock
static const unsigned char SIG_DESTROY_SELF[0x54] = {
    0xf3, 0x0f, 0x1e, 0xfa, 0x55, 0x48, 0x89, 0xe5, 0x41, 0x55, 0x41, 0x54, 0x49, 0x89, 0xfc, 0x53,
    0x48, 0x83, 0xec, 0x08, 0x48, 0x8b, 0x5f, 0x50, 0x4c, 0x8d, 0xab, 0x88, 0x00, 0x00, 0x00, 0x4c,
    0x89, 0xef, 0xe8, 0x89, 0x8f, 0x99, 0xff, 0x85, 0xc0, 0x75, 0x29, 0x48, 0x8b, 0xbb, 0xb0, 0x00,
    0x00, 0x00, 0x48, 0x85, 0xff, 0x74, 0x08, 0x4c, 0x89, 0xe6, 0xe8, 0x21, 0xd1, 0xfe, 0xff, 0x4c,
    0x89, 0xef, 0xe8, 0x49, 0x8f, 0x99, 0xff, 0x48, 0x83, 0xc4, 0x08, 0x31, 0xc0, 0x5b, 0x41, 0x5c,
    0x41, 0x5d, 0x5d, 0xc3};

#define OBJ_REFS 0x10
#define OBJ_DEVICE 0x50
#define OBJ_WIDTH 0x80
#define OBJ_HEIGHT 0x84
#define DEV_MUTEX 0x88
#define DEV_TEXMGR 0xb0
#define REFS_ZERO ((int)0xfff0bdc1)   // "0, in the delete list, revivable"

#define MAX_KEEP 96                    // entries (6 per material + slack; detail mode lists up to ~10 materials)
#define MAX_KEEP_MB 160                // estimated at 1 byte per pixel + mips (the packs are BC7 DDS: 4096² ~ 22 MB)
#define KEY_MAX 1024
#define OFF_RELEASE_TICKS 8            // x BNGE_FLAG_POLL_MS = 2 s

typedef void *(*create_fn)(void *mgr, void *path, void *profile);
typedef void (*request_delete_fn)(void *mgr, void *obj);
typedef void (*destroy_fn)(void *obj);

static uintptr_t g_base;
static create_fn g_orig;
static request_delete_fn g_request_delete;
static uintptr_t g_ret_cached, g_ret_packed, g_destroy_self;
static int g_dry, g_log;
static atomic_int g_on;                     // the level flag (watcher thread)

struct entry { char *key; void *obj; void *vptr; unsigned long long used, gen; unsigned mb_est; };
static struct entry g_keep[MAX_KEEP + 1];
static int g_nkeep;
static unsigned long long g_tick;
// Rebuild generations: a pack-loader call more than GEN_GAP_NS after the previous one starts a new rebuild. An entry
// not asked for in the last KEEP_GENS rebuilds goes (an old block's photo: the server deletes its file when the block
// is pruned, and a live texture whose file goes makes the engine log a failed reload).
#define GEN_GAP_NS 300000000ULL
#define KEEP_GENS 2
static unsigned long long g_gen, g_last_call_ns;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

// statistics (status file)
static atomic_ullong st_hits, st_misses, st_packed, st_evicted, st_dropped, st_foreign, st_miss_ns, st_packed_ns;
static atomic_uint st_kept_mb;
static FILE *g_logf;

static inline int *refs(void *obj) { return (int *)((char *)obj + OBJ_REFS); }

static unsigned long long now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000000000ULL + (unsigned long long)ts.tv_nsec;
}

// A strong reference of ours, as the engine's handle assignment takes one; 0 if the object is dying.
static int acquire(void *obj) {
    int *rc = refs(obj);
    int c = __atomic_load_n(rc, __ATOMIC_ACQUIRE);
    for (;;) {
        if (!(c > 0 || c == REFS_ZERO)) return 0;
        int n = (c > 0 ? c : 0) + 1;
        if (__atomic_compare_exchange_n(rc, &c, n, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return 1;
    }
}

// Our reference back. The last one destroys the object as the engine's release does: destroySelf, or, under the device
// mutex this thread already holds (the pack loader's call), destroySelf's body without the second lock.
// held: the caller is the pack loader's call (the engine holds the device mutex on this thread); 0: the watcher thread
// (never inside the engine: destroySelf takes the mutex itself).
static void release(void *obj, int held) {
    int *rc = refs(obj);
    int c = __atomic_load_n(rc, __ATOMIC_ACQUIRE);
    for (;;) {
        if (c > 1) {
            if (__atomic_compare_exchange_n(rc, &c, c - 1, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return;
            continue;
        }
        if (c != 1) { atomic_fetch_add(&st_dropped, 1); return; }   // not ours any more: leave it alone
        if (!__atomic_compare_exchange_n(rc, &c, -1, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) continue;
        void **vt = *(void ***)obj;
        if (held) {
            char *dev = *(char **)((char *)obj + OBJ_DEVICE);
            void *mgr = *(void **)(dev + DEV_TEXMGR);
            if (mgr) g_request_delete(mgr, obj);
        } else {
            ((destroy_fn)vt[2])(obj);
        }
        return;
    }
}

// "profile|s0|s1|...|s5" from the Path's six Strings; 0 if a String does not look like one.
static int path_key(void *path, void *profile, char *out, size_t n) {
    int k = snprintf(out, n, "%p", profile);
    for (int i = 0; i < 6; i++) {
        const char *rep = *(const char **)((char *)path + 8 * i);
        if (!rep) return 0;
        unsigned len = *(const unsigned *)(rep + 4);
        if (len > 512 || (size_t)k + 1 + len + 1 > n) return 0;
        out[k++] = '|';
        memcpy(out + k, rep + 0x20, len);
        k += (int)len;
    }
    out[k] = 0;
    return 1;
}

static int find(const char *key) {
    for (int i = 0; i < g_nkeep; i++) if (strcmp(g_keep[i].key, key) == 0) return i;
    return -1;
}

static void take_out(int i, void **victims, int *nv) {
    victims[(*nv)++] = g_keep[i].obj;
    free(g_keep[i].key);
    g_keep[i] = g_keep[--g_nkeep];
}

static unsigned kept_mb(void) {
    unsigned mb = 0;
    for (int i = 0; i < g_nkeep; i++) mb += g_keep[i].mb_est;
    return mb;
}

// (the watcher thread only: see watch)
static void release_all(void) {
    void *victims[MAX_KEEP + 1];
    int nv = 0;
    pthread_mutex_lock(&g_lock);
    while (g_nkeep > 0) take_out(g_nkeep - 1, victims, &nv);
    atomic_store(&st_kept_mb, 0);
    pthread_mutex_unlock(&g_lock);
    for (int i = 0; i < nv; i++) release(victims[i], 0);
}

static void log_call(const char *what, int site, const char *key, void *obj, unsigned long long ns) {
    if (!g_log) return;
    if (!g_logf) {
        char p[512];
        bnge_temp_path(p, sizeof p, LOG_FILE);
        g_logf = fopen(p, "a");
        if (!g_logf) { g_log = 0; return; }
    }
    unsigned w = obj ? *(unsigned *)((char *)obj + OBJ_WIDTH) : 0, h = obj ? *(unsigned *)((char *)obj + OBJ_HEIGHT) : 0;
    const char *name = strchr(key, '|');      // (after the profile)
    fprintf(g_logf, "%.3f site%d %s %ux%u %.2f ms refs %d %s\n", now_ns() / 1e9, site, what, w, h, ns / 1e6,
            obj ? *refs(obj) : 0, name ? name + 1 : key);
    fflush(g_logf);
}

static void *hook_create(void *mgr, void *path, void *profile) {
    uintptr_t ret = (uintptr_t)__builtin_return_address(0);
    int site = ret == g_ret_cached ? 1 : ret == g_ret_packed ? 2 : 0;
    if (!atomic_load_explicit(&g_on, memory_order_relaxed))
        return g_orig(mgr, path, profile);    // (the level ended: stock; the watcher lets the kept textures go)
    char key[KEY_MAX];
    if (!site) {
        if (g_log && path_key(path, profile, key, sizeof key) && strcasestr(key, "terrainmaterialcache")) {
            void *obj = g_orig(mgr, path, profile);
            char what[48];
            snprintf(what, sizeof what, "other(%#lx)", (unsigned long)(ret - g_base));
            log_call(what, 0, key, obj, 0);
            return obj;
        }
        return g_orig(mgr, path, profile);
    }
    if (!path_key(path, profile, key, sizeof key) || !strcasestr(key, "terrainmaterialcache"))
        return g_orig(mgr, path, profile);

    if (site == 2) {                          // re-packed: the stock texture, nothing of ours kept for this path
        void *victims[1];
        int nv = 0;
        pthread_mutex_lock(&g_lock);
        int i = find(key);
        if (i >= 0) take_out(i, victims, &nv);
        pthread_mutex_unlock(&g_lock);
        if (nv) { release(victims[0], 1); atomic_fetch_add(&st_evicted, 1); }
        unsigned long long t0 = now_ns();
        void *obj = g_orig(mgr, path, profile);
        unsigned long long dt = now_ns() - t0;
        atomic_fetch_add(&st_packed, 1);
        atomic_fetch_add(&st_packed_ns, dt);
        log_call("packed", 2, key, obj, dt);
        return obj;
    }

    unsigned long long now = now_ns();
    void *stale[MAX_KEEP + 1];
    int nstale = 0;
    pthread_mutex_lock(&g_lock);
    if (now - g_last_call_ns > GEN_GAP_NS) {             // a new rebuild: entries unused for KEEP_GENS rebuilds go
        g_gen++;
        for (int k = g_nkeep - 1; k >= 0; k--)
            if (g_keep[k].gen + KEEP_GENS < g_gen) { take_out(k, stale, &nstale); atomic_fetch_add(&st_evicted, 1); }
        atomic_store(&st_kept_mb, kept_mb());
    }
    g_last_call_ns = now;
    pthread_mutex_unlock(&g_lock);
    for (int k = 0; k < nstale; k++) release(stale[k], 1);

    if (!g_dry) {
        pthread_mutex_lock(&g_lock);
        int i = find(key);
        void *hit = NULL;
        if (i >= 0) {
            void *obj = g_keep[i].obj;
            int c = *refs(obj);
            if (c >= 1 && *(void **)obj == g_keep[i].vptr) {
                g_keep[i].used = ++g_tick;
                g_keep[i].gen = g_gen;
                hit = obj;
            } else {                          // not as we left it: forget it without touching it
                free(g_keep[i].key);
                g_keep[i] = g_keep[--g_nkeep];
                atomic_fetch_add(&st_dropped, 1);
            }
        }
        pthread_mutex_unlock(&g_lock);
        if (hit) {
            atomic_fetch_add(&st_hits, 1);
            log_call("hit", 1, key, hit, 0);
            return hit;
        }
    }

    unsigned long long t0 = now_ns();
    void *obj = g_orig(mgr, path, profile);
    unsigned long long dt = now_ns() - t0;
    atomic_fetch_add(&st_misses, 1);
    atomic_fetch_add(&st_miss_ns, dt);
    log_call(g_dry ? "dry" : "miss", 1, key, obj, dt);
    if (!obj || g_dry) return obj;
    void **vt = *(void ***)obj;
    if ((uintptr_t)vt[2] != g_destroy_self) { atomic_fetch_add(&st_foreign, 1); return obj; }
    if (!acquire(obj)) return obj;

    void *victims[MAX_KEEP + 1];
    int nv = 0;
    unsigned w = *(unsigned *)((char *)obj + OBJ_WIDTH), h = *(unsigned *)((char *)obj + OBJ_HEIGHT);
    unsigned long long bytes = (unsigned long long)w * h * 4 / 3;
    pthread_mutex_lock(&g_lock);
    int i = find(key);
    if (i >= 0) take_out(i, victims, &nv);    // (another thread kept one meanwhile)
    g_keep[g_nkeep].key = strdup(key);
    if (!g_keep[g_nkeep].key) {
        pthread_mutex_unlock(&g_lock);
        for (int k = 0; k < nv; k++) release(victims[k], 1);
        release(obj, 1);
        return obj;
    }
    g_keep[g_nkeep].obj = obj;
    g_keep[g_nkeep].vptr = vt;
    g_keep[g_nkeep].used = ++g_tick;
    g_keep[g_nkeep].gen = g_gen;
    g_keep[g_nkeep].mb_est = (unsigned)((bytes + (1 << 20) - 1) >> 20);
    g_nkeep++;
    while (g_nkeep > MAX_KEEP || (g_nkeep > 1 && kept_mb() > MAX_KEEP_MB)) {
        int lru = 0;
        for (int k = 1; k < g_nkeep; k++) if (g_keep[k].used < g_keep[lru].used) lru = k;
        take_out(lru, victims, &nv);
        atomic_fetch_add(&st_evicted, 1);
    }
    atomic_store(&st_kept_mb, kept_mb());
    pthread_mutex_unlock(&g_lock);
    for (int k = 0; k < nv; k++) release(victims[k], 1);
    return obj;
}

static void status_line(char *msg, size_t n, const char *state) {
    unsigned long long hits = atomic_load(&st_hits), misses = atomic_load(&st_misses), packed = atomic_load(&st_packed);
    double miss_ms = atomic_load(&st_miss_ns) / 1e6, packed_ms = atomic_load(&st_packed_ns) / 1e6;
    snprintf(msg, n, "%s; kept %d (~%u MB est.); cached-pack calls: %llu hits, %llu %s (%.0f ms, %.2f ms each); "
             "re-packed calls %llu (%.0f ms); evicted %llu, dropped %llu, foreign %llu",
             state, g_nkeep, atomic_load(&st_kept_mb), hits, misses, g_dry ? "stock (dry run)" : "misses", miss_ms,
             misses ? miss_ms / misses : 0.0, packed, packed_ms, (unsigned long long)atomic_load(&st_evicted),
             (unsigned long long)atomic_load(&st_dropped), (unsigned long long)atomic_load(&st_foreign));
}

// The level flag -> g_on; the status file every 2 s while something changed.
static void *watch(void *arg) {
    (void)arg;
    char flag[512];
    bnge_temp_path(flag, sizeof flag, BNGE_LEVEL_FLAG);
    unsigned long long last_sum = ~0ULL;
    int last_on = -1, tick = 0, off_ticks = 0;
    for (;;) {
        int on = access(flag, F_OK) == 0;
        atomic_store(&g_on, on);
        // The level ended: the kept textures go from here, OFF_RELEASE_TICKS polls later (the hook hands nothing out
        // once the flag is off, and a texture it handed out just before is referenced by the engine by then).
        off_ticks = on ? 0 : off_ticks + 1;
        if (!on && off_ticks >= OFF_RELEASE_TICKS && g_nkeep) release_all();
        unsigned long long sum = atomic_load(&st_hits) + atomic_load(&st_misses) + atomic_load(&st_packed) +
                                 atomic_load(&st_evicted) + (unsigned long long)g_nkeep;
        if (on != last_on || (++tick % 8 == 0 && sum != last_sum)) {
            char msg[512];
            status_line(msg, sizeof msg, on ? (g_dry ? "applied (dry run: counting only), BeamNG.Earth level"
                                                     : "applied: terrain packs kept across rebuilds (BeamNG.Earth level)")
                                            : "stock: another level");
            bnge_report(TAG, STATUS_FILE, msg);
            last_on = on;
            last_sum = sum;
        }
        struct timespec ts = {0, BNGE_FLAG_POLL_MS * 1000000L};
        nanosleep(&ts, NULL);
    }
    return NULL;
}

static int same(const struct bnge_main *m, uintptr_t vaddr, const unsigned char *sig, size_t n) {
    return bnge_mapped(m, vaddr, n, PF_R | PF_X) && memcmp((const void *)(m->base + vaddr), sig, n) == 0;
}

__attribute__((constructor)) static void bngearth_texkeep(void) {
    if (!bnge_is_main_game()) return;
    // (opt-in, BNGEARTH_TEXKEEP=1. The 2026-11-03 review's deadlock paths are gone since 2026-11-13: no mutex-owner
    // probe (the pack loader's calls release under the engine's lock, the watcher thread without it) and nothing is
    // released from other callers of createTexture. Still to do before it is on by default: a teleport soak (a crash
    // at a teleport's teardown was seen with it on, 2026-11-05, cause unknown))
    const char *on_ = getenv("BNGEARTH_TEXKEEP");
    if (!on_ || !*on_ || !strcmp(on_, "0")) {
        bnge_report(TAG, STATUS_FILE, "off (opt-in: BNGEARTH_TEXKEEP=1)");
        return;
    }
    if (getenv("BNGEARTH_TEXKEEP_OFF") && *getenv("BNGEARTH_TEXKEEP_OFF") && strcmp(getenv("BNGEARTH_TEXKEEP_OFF"), "0")) {
        bnge_report(TAG, STATUS_FILE, "off (BNGEARTH_TEXKEEP_OFF set)");
        return;
    }
    struct bnge_main m;
    bnge_find_main(&m);
    if (!m.seen) { bnge_report(TAG, STATUS_FILE, "not applied: the game's code was not found"); return; }
    const char *why = NULL;
    if (!same(&m, CREATE_TEXTURE, SIG_CREATE, sizeof SIG_CREATE)) why = "createTexture differs";
    else if (!same(&m, SITE_CACHED_CALL, SIG_SITE_CACHED, SITE_CALL_LEN)) why = "the pack loader's cached call differs";
    else if (!same(&m, SITE_PACKED_CALL, SIG_SITE_PACKED, SITE_CALL_LEN)) why = "the pack loader's re-pack call differs";
    else if (!same(&m, REQUEST_DELETE, SIG_REQUEST_DELETE, sizeof SIG_REQUEST_DELETE)) why = "requestDeleteTexture differs";
    else if (!same(&m, DESTROY_SELF, SIG_DESTROY_SELF, sizeof SIG_DESTROY_SELF)) why = "destroySelf differs";
    for (int i = 0; !why && i < 3; i++) {
        if (!bnge_mapped(&m, VTABLE_SLOTS[i], 8, PF_R)) why = "a vtable slot is not mapped";
        else if (*(uintptr_t *)(m.base + VTABLE_SLOTS[i]) != m.base + CREATE_TEXTURE) why = "a vtable slot differs";
    }
    if (why) {
        char msg[256];
        snprintf(msg, sizeof msg, "not applied: another game build (%s); stock", why);
        bnge_report(TAG, STATUS_FILE, msg);
        return;
    }
    g_base = m.base;
    g_orig = (create_fn)(m.base + CREATE_TEXTURE);
    g_request_delete = (request_delete_fn)(m.base + REQUEST_DELETE);
    g_destroy_self = m.base + DESTROY_SELF;
    g_ret_cached = m.base + SITE_CACHED_CALL + SITE_CALL_LEN;
    g_ret_packed = m.base + SITE_PACKED_CALL + SITE_CALL_LEN;
    g_dry = getenv("BNGEARTH_TEXKEEP_DRY") && strcmp(getenv("BNGEARTH_TEXKEEP_DRY"), "0") && *getenv("BNGEARTH_TEXKEEP_DRY");
    g_log = getenv("BNGEARTH_TEXKEEP_LOG") && strcmp(getenv("BNGEARTH_TEXKEEP_LOG"), "0") && *getenv("BNGEARTH_TEXKEEP_LOG");

    // The slots (RELRO, read-only after relocation): writable for the three 8-byte stores, then read-only again.
    long pagesz = sysconf(_SC_PAGESIZE);
    for (int i = 0; i < 3; i++) {
        uintptr_t a = m.base + VTABLE_SLOTS[i];
        uintptr_t page = a & ~(uintptr_t)(pagesz - 1);
        if (mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_WRITE) != 0) {
            // (earlier slots stay hooked: the hook is stock for every caller until the level flag shows up)
            bnge_report(TAG, STATUS_FILE, "not applied: mprotect refused");
            return;
        }
        __atomic_store_n((uintptr_t *)a, (uintptr_t)hook_create, __ATOMIC_RELEASE);
        mprotect((void *)page, (size_t)pagesz, PROT_READ);
    }
    // (without the watcher the level flag never shows up: the hook stays stock for every caller)
    pthread_t t;
    if (pthread_create(&t, NULL, watch, NULL) != 0) {
        bnge_report(TAG, STATUS_FILE, "not applied: no thread for the level flag");
        return;
    }
    pthread_detach(t);
    bnge_report(TAG, STATUS_FILE, g_dry ? "ready (dry run): counting while the BeamNG.Earth level runs"
                                        : "ready: applied while the BeamNG.Earth level runs");
}
