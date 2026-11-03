// Keep BeamNG.drive's grass when a TerrainBlock is re-imported or moved (BeamNG.Earth; docs/ARCHITECTURE.md
// "Ground surfaces"). Loaded into the game with LD_PRELOAD (Steam launch options, see README.md).
//
// The engine's GroundCover terrain-update handler frees every grass cell over a TerrainBlock that was
// imported or moved. BeamNG.Earth moves its physics block under the player every ~128 m while the ground
// it samples stays the same, so that free is pure waste (and a visible blink or a slow frame). This
// library changes one byte of the handler in the game's memory at startup: `and esi, 0x0E` (keep the
// height / layer / empty update bits) becomes `and esi, 0`, so the handler returns before freeing
// anything. Its lightmap path (free everything) stays. The mod rebuilds the grass itself where the
// world really moves (teleports, floating-origin rebases).
//
// Only while the BeamNG.Earth level runs (the user, 2026-10-20: the mod effectively off on other maps, where a terrain
// edited in the World Editor needs its grass updated): the mod creates the flag file temp/beamngearth_gcpatch_on in
// the game's user folder when its level starts and removes it when it ends (groundcover.lua; lower case: the game's
// file system writes new names so); a thread here looks for it every FLAG_POLL_MS and writes the byte back and forth.
// Stock until the flag shows up.
//
// Nothing on disk is changed. It acts only in the main BeamNG.drive.x64 process (not the CEF helpers,
// not Steam), and only if the handler's first 0x36 bytes are those of the build it was written for (0.39.4
// Linux, x86-64) at its address; in another build (a game update: BeamNG 0.40, 2026-10-19) the game's code
// is searched for the handler's core (`test sil, 1; jne; and esi, 0x0E; je`, the jump distance masked) and patched
// only if exactly one place matches (0.40: one, its prologue uses other registers); otherwise it does nothing.
// Without the launch option the game is stock.
#define _GNU_SOURCE
#include "bngearth_common.h"
#include <elf.h>
#include <link.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define FLAG_POLL_MS 250

#define HANDLER 0x1540350UL /* GroundCover terrain-update handler (vaddr == file offset) */
#define IMM 0x154037fUL     /* imm8 of `and esi, 0x0E` at 0x154037d (83 e6 0e) */
#define STOCK 0x0e
#define PATCHED 0x00

static const unsigned char SIGNATURE[0x36] = {
    0xf3, 0x0f, 0x1e, 0xfa, 0x55, 0x48, 0x89, 0xe5, 0x41, 0x55, 0x41, 0x54, 0x49, 0x89, 0xfc, 0x53,
    0x48, 0x83, 0xec, 0x38, 0x64, 0x48, 0x8b, 0x04, 0x25, 0x28, 0x00, 0x00, 0x00, 0x48, 0x89, 0x45,
    0xd8, 0x31, 0xc0, 0x40, 0xf6, 0xc6, 0x01, 0x0f, 0x85, 0x53, 0x01, 0x00, 0x00, 0x83, 0xe6, 0x0e,
    0x0f, 0x84, 0x30, 0x01, 0x00, 0x00,
};

// One line for the player / the mod's diagnostics: stderr and <user>/temp/beamngEarth_gcpatch.txt.
static void report(const char *fmt, const char *detail) {
    char msg[256];
    snprintf(msg, sizeof msg, fmt, detail ? detail : "");
    bnge_report("bngearth-gcpatch", "beamngEarth_gcpatch.txt", msg);
}

#define IMM_OFF (IMM - HANDLER)   /* 0x2F: the imm8 within the signature */


struct mainprog { uintptr_t base; int covered; int seen; uintptr_t xs[8], xe[8]; int nx; };

// The first object dl_iterate_phdr reports is the main program; is HANDLER in its executable PT_LOAD?
static int find_main(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    struct mainprog *m = data;
    if (m->seen) return 1;
    m->seen = 1;
    m->base = (uintptr_t)info->dlpi_addr;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type == PT_LOAD && (ph->p_flags & PF_X)) {
            if (ph->p_vaddr <= HANDLER && HANDLER + sizeof SIGNATURE <= ph->p_vaddr + ph->p_filesz)
                m->covered = 1;
            if (m->nx < 8) {
                m->xs[m->nx] = m->base + ph->p_vaddr;
                m->xe[m->nx] = m->base + ph->p_vaddr + ph->p_filesz;
                m->nx++;
            }
        }
    }
    return 1;
}

static unsigned char *g_imm = NULL;
static const char *g_how = "";

// The byte: PATCHED or STOCK. The page writable for that one byte, then read+exec again (the code keeps running: x86
// sees the old or the new immediate, and the handler runs on the game's main thread at terrain updates only).
static int set_byte(unsigned char v) {
    long pagesz = sysconf(_SC_PAGESIZE);
    uintptr_t page = (uintptr_t)g_imm & ~(uintptr_t)(pagesz - 1);
    if (mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) return 0;
    *(volatile unsigned char *)g_imm = v;
    __sync_synchronize();
    mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_EXEC);
    return 1;
}

// Applied while the mod's flag file exists (its level runs), stock otherwise.
static void *watch_flag(void *arg) {
    (void)arg;
    char flag[512];
    bnge_temp_path(flag, sizeof flag, BNGE_LEVEL_FLAG);
    int on = 0;
    for (;;) {
        int want = access(flag, F_OK) == 0;
        if (want != on) {
            if (!set_byte(want ? PATCHED : STOCK)) {
                report("stopped: mprotect refused%s", NULL);
                return NULL;
            }
            on = want;
            report(on ? "applied: grass kept across terrain block moves (BeamNG.Earth level)%s"
                      : "stock: another level (the grass follows terrain edits)%s", on ? g_how : NULL);
        }
        struct timespec ts = {0, FLAG_POLL_MS * 1000000L};
        nanosleep(&ts, NULL);
    }
    return NULL;
}

__attribute__((constructor)) static void bngearth_gcpatch(void) {
    if (!bnge_is_main_game()) return;
    if (getenv("BNGEARTH_GCPATCH_OFF")) {
        report("off (BNGEARTH_GCPATCH_OFF set)%s", NULL);
        return;
    }
    struct mainprog m;
    memset(&m, 0, sizeof m);
    dl_iterate_phdr(find_main, &m);
    if (!m.seen) {
        report("not applied: the game's code was not found%s", NULL);
        return;
    }
    unsigned char *code = NULL;
    if (m.covered) {
        code = (unsigned char *)(m.base + HANDLER);
        for (size_t i = 0; i < sizeof SIGNATURE; i++) {
            if (i == IMM_OFF) continue;
            if (code[i] != SIGNATURE[i]) { code = NULL; break; }
        }
    }
    const char *how = "";
    unsigned char *imm = NULL;
    if (code) imm = code + IMM_OFF;
    else {
        // another build: search its code for the handler's core, `test sil, 1; jne rel32; and esi, 0x0E; je rel32`,
        // one match only (0.40 (2026-10-19): the same core in a prologue with other registers, at 0x15fb3c5)
        static const unsigned char CORE[15] = {0x40, 0xf6, 0xc6, 0x01, 0x0f, 0x85, 0, 0, 0, 0, 0x83, 0xe6, 0x0e, 0x0f, 0x84};
        int matches = 0;
        for (int k = 0; k < m.nx; k++) {
            for (unsigned char *p = (unsigned char *)m.xs[k]; p + sizeof CORE <= (unsigned char *)m.xe[k]; p++) {
                if (p[0] != 0x40 || p[1] != 0xf6) continue;
                size_t i = 0;
                for (; i < sizeof CORE; i++) {
                    if (i >= 6 && i <= 9) continue;         // the jne's rel32
                    if (p[i] != CORE[i]) break;
                }
                if (i == sizeof CORE) { imm = p + 12; matches++; }
            }
        }
        if (matches != 1) {
            char d[32];
            snprintf(d, sizeof d, "%d", matches);
            report("not applied: the handler was not found in this build (%s matches)", d);
            return;
        }
        how = " (found by its signature in this build)";
    }
    if (*imm == PATCHED) { report("already applied%s", NULL); return; }
    if (*imm != STOCK) { report("not applied: unexpected byte (another build)%s", NULL); return; }
    g_imm = imm;
    g_how = how;
    pthread_t t;
    if (pthread_create(&t, NULL, watch_flag, NULL) != 0) {
        report("not applied: no thread for the level flag%s", NULL);
        return;
    }
    pthread_detach(t);
    report("ready: applied while the BeamNG.Earth level runs%s", how);
}
