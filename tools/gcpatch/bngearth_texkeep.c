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
// Found by code, not by address (2026-11-22: BeamNG 0.40.6 moved a vtable by 0x20 and the old exact-address check
// dropped texkeep for every player): createTexture, the pack loader (both call sites, fixed offsets in it), destroySelf
// and requestDeleteTexture (destroySelf's callee) are matched against masked whole-function signatures
// (bngearth_sigs.h, mksigs.py), each exactly once; the two call sites must load the same profile; the vtable slots are
// the relocated data words holding createTexture's address. On any difference it does nothing and says why (and the
// build id) in <user>/temp/beamngEarth_texkeep.txt. On by default; off: BNGEARTH_TEXKEEP_OFF=1 or BNGEARTH_TEXKEEP=0. BNGEARTH_TEXKEEP_DRY=1: installed but
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
#include "bngearth_sigs.h"
#include <pthread.h>
#include <stdatomic.h>
#include <sys/mman.h>
#include <strings.h>

#define TAG "bngearth-texkeep"
#define STATUS_FILE "beamngEarth_texkeep.txt"
#define LOG_FILE "beamngEarth_texkeep.log"

// The engine functions, found by their masked whole-function signatures (bngearth_sigs.h, mksigs.py; reference
// BeamNG.drive 0.40.6 Linux, the 2026-11-19 executable). Offsets inside the matched pack loader (tk_loader):
#define SITE_CACHED_OFF 0x69f           // lea rdx,[profile]; mov rsi,r15; mov rdi,r13; call rax  (cache file up to date)
#define SITE_PACKED_OFF 0xacf           // lea rdx,[profile]; mov rsi,r15; mov rdi,r13; call rbx  (just re-packed)
#define SITE_CALL_LEN 15
#define SITE_DISP 3                     // the lea's rip displacement (both: the same profile)
#define DESTROY_REQ_REL 0x3b, 0x3f      // destroySelf: call requestDeleteTexture
#define MAX_SLOTS 16                    // vtable slots holding createTexture (0.40.6: 3, one per texture manager)
static const unsigned char SITE_CACHED_TAIL[8] = {0x4c, 0x89, 0xfe, 0x4c, 0x89, 0xef, 0xff, 0xd0};
static const unsigned char SITE_PACKED_TAIL[8] = {0x4c, 0x89, 0xfe, 0x4c, 0x89, 0xef, 0xff, 0xd3};

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

static create_fn g_orig;
static request_delete_fn g_request_delete;
static uintptr_t g_ret_cached, g_ret_packed, g_destroy_self;
static int g_dry, g_log;
static uintptr_t g_main_base;
static char g_found[256];                   // how the targets were found (the status line)
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
            snprintf(what, sizeof what, "other(%#lx)", (unsigned long)(ret - g_main_base));
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
             "re-packed calls %llu (%.0f ms); evicted %llu, dropped %llu, foreign %llu (%s)",
             state, g_nkeep, atomic_load(&st_kept_mb), hits, misses, g_dry ? "stock (dry run)" : "misses", miss_ms,
             misses ? miss_ms / misses : 0.0, packed, packed_ms, (unsigned long long)atomic_load(&st_evicted),
             (unsigned long long)atomic_load(&st_dropped), (unsigned long long)atomic_load(&st_foreign), g_found);
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
            char msg[900];
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

// Where everything is in this build: NULL, or why texkeep cannot act. slots: the relocated data words that hold
// createTexture's address (the texture managers' vtables; every caller through them gets the hook, which is the stock
// function for all but the pack loader's cached call).
struct tk_targets {
    uintptr_t create, loader, request_delete, destroy_self, ret_cached, ret_packed;
    uintptr_t *slots[MAX_SLOTS];
    int nslots;
};

static const char *found_once(const struct bnge_main *m, const struct bnge_sig *s, uintptr_t *at, char *why, size_t n) {
    int k = bnge_sig_find(m, s, at);
    if (k == 1) return NULL;
    snprintf(why, n, "%s: %d matches of its code", s->name, k);
    return why;
}

static const char *tk_locate(const struct bnge_main *m, struct tk_targets *t, char *why, size_t n) {
    memset(t, 0, sizeof *t);
    if (found_once(m, &tk_create, &t->create, why, n) || found_once(m, &tk_loader, &t->loader, why, n) ||
        found_once(m, &tk_destroy_self, &t->destroy_self, why, n)) return why;
    t->request_delete = bnge_rel_target(t->destroy_self, DESTROY_REQ_REL);
    if (!bnge_mapped(m, t->request_delete - m->base, tk_request_delete.len, PF_X) ||
        !bnge_sig_at(&tk_request_delete, (const unsigned char *)t->request_delete))
        return "requestDeleteTexture (destroySelf's call) differs";
    uintptr_t c = t->loader + SITE_CACHED_OFF, p = t->loader + SITE_PACKED_OFF;
    if (memcmp((const void *)(c + 7), SITE_CACHED_TAIL, 8) != 0 || memcmp((const void *)(p + 7), SITE_PACKED_TAIL, 8) != 0)
        return "the pack loader's calls differ";
    if (bnge_rel_target(c, SITE_DISP, 7) != bnge_rel_target(p, SITE_DISP, 7))
        return "the pack loader's two calls use different profiles";
    t->ret_cached = c + SITE_CALL_LEN;
    t->ret_packed = p + SITE_CALL_LEN;
    // the vtable slots: aligned words of the relocated read-only data (RELRO: .data.rel.ro) equal to createTexture
    for (uintptr_t a = (m->relro_start + 7) & ~(uintptr_t)7; a + 8 <= m->relro_end; a += 8) {
        if (*(const uintptr_t *)a != t->create) continue;
        if (t->nslots == MAX_SLOTS) return "createTexture is referenced from too many places";
        t->slots[t->nslots++] = (uintptr_t *)a;
    }
    if (t->nslots == 0) return "no vtable holds createTexture";
    return NULL;
}

#ifndef BNGE_SIGTEST
__attribute__((constructor)) static void bngearth_texkeep(void) {
    if (!bnge_is_main_game()) return;
    // (on by default since the 2026-11-14 teleport soaks, README.md; off: BNGEARTH_TEXKEEP_OFF=1 or BNGEARTH_TEXKEEP=0.
    // The 2026-11-05 Genoa teleport crash once blamed on it ran without it: that dump has no texkeep watcher thread)
    const char *on_ = getenv("BNGEARTH_TEXKEEP");
    const char *off_ = getenv("BNGEARTH_TEXKEEP_OFF");
    if ((off_ && *off_ && strcmp(off_, "0")) || (on_ && !strcmp(on_, "0"))) {
        bnge_report_sticky(TAG, STATUS_FILE, "off (BNGEARTH_TEXKEEP_OFF / BNGEARTH_TEXKEEP=0)");
        return;
    }
    struct bnge_main m;
    bnge_find_main(&m);
    if (!m.seen) { bnge_report_sticky(TAG, STATUS_FILE, "not applied: the game's code was not found"); return; }
    char why[160], msg[400], build[48];
    bnge_build_id(build, sizeof build);
    struct tk_targets t;
    if (tk_locate(&m, &t, why, sizeof why)) {
        snprintf(msg, sizeof msg, "not applied: another game build (%s; build id %s); stock", why, build);
        bnge_report_sticky(TAG, STATUS_FILE, msg);
        return;
    }
    g_main_base = m.base;
    g_orig = (create_fn)t.create;
    g_request_delete = (request_delete_fn)t.request_delete;
    g_destroy_self = t.destroy_self;
    g_ret_cached = t.ret_cached;
    g_ret_packed = t.ret_packed;
    snprintf(g_found, sizeof g_found, "found by signature: createTexture %#lx, pack loader %#lx, %d vtable slots; build id %s",
             (unsigned long)(t.create - m.base), (unsigned long)(t.loader - m.base), t.nslots, build);
    g_dry = getenv("BNGEARTH_TEXKEEP_DRY") && strcmp(getenv("BNGEARTH_TEXKEEP_DRY"), "0") && *getenv("BNGEARTH_TEXKEEP_DRY");
    g_log = getenv("BNGEARTH_TEXKEEP_LOG") && strcmp(getenv("BNGEARTH_TEXKEEP_LOG"), "0") && *getenv("BNGEARTH_TEXKEEP_LOG");

    // The slots (RELRO, read-only after relocation): writable for each 8-byte store, then read-only again.
    long pagesz = sysconf(_SC_PAGESIZE);
    for (int i = 0; i < t.nslots; i++) {
        uintptr_t a = (uintptr_t)t.slots[i];
        uintptr_t page = a & ~(uintptr_t)(pagesz - 1);
        if (mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_WRITE) != 0) {
            // (earlier slots stay hooked: the hook is stock for every caller until the level flag shows up)
            bnge_report_sticky(TAG, STATUS_FILE, "not applied: mprotect refused");
            return;
        }
        __atomic_store_n((uintptr_t *)a, (uintptr_t)hook_create, __ATOMIC_RELEASE);
        mprotect((void *)page, (size_t)pagesz, PROT_READ);
    }
    // (without the watcher the level flag never shows up: the hook stays stock for every caller)
    pthread_t th;
    if (pthread_create(&th, NULL, watch, NULL) != 0) {
        bnge_report_sticky(TAG, STATUS_FILE, "not applied: no thread for the level flag");
        return;
    }
    pthread_detach(th);
    snprintf(msg, sizeof msg, "%s (%s)", g_dry ? "ready (dry run): counting while the BeamNG.Earth level runs"
                                               : "ready: applied while the BeamNG.Earth level runs", g_found);
    bnge_report(TAG, STATUS_FILE, msg);
}
#endif
