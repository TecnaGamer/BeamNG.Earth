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
// Nothing on disk is changed. It acts only in the main BeamNG.drive.x64 process (not the CEF helpers,
// not Steam), and only if the handler's first 0x36 bytes are those of the build it was written for (0.39.4
// Linux, x86-64) at its address; in another build (a game update: BeamNG 0.40, 2026-10-19) the game's code
// is searched for the handler's core (`test sil, 1; jne; and esi, 0x0E; je`, the jump distance masked) and patched
// only if exactly one place matches (0.40: one, its prologue uses other registers); otherwise it does nothing.
// Without the launch option the game is stock.
#define _GNU_SOURCE
#include <elf.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

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
    fprintf(stderr, "[bngearth-gcpatch] %s\n", msg);
    const char *home = getenv("HOME");
    if (!home) return;
    char path[512];
    snprintf(path, sizeof path, "%s/.local/share/BeamNG/BeamNG.drive/current/temp/beamngEarth_gcpatch.txt", home);
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "%ld pid %d: %s\n", (long)time(NULL), (int)getpid(), msg);
    fclose(f);
}

// The main game process: argv[0] ends in BeamNG.drive.x64 and no argument is a CEF --type=.
static int is_main_game(void) {
    FILE *f = fopen("/proc/self/cmdline", "rb");
    if (!f) return 0;
    char buf[8192];
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    if (n == 0) return 0;
    buf[n] = 0;
    size_t len0 = strlen(buf);
    const char *exe = "BeamNG.drive.x64";
    size_t le = strlen(exe);
    if (len0 < le || strcmp(buf + len0 - le, exe) != 0) return 0;
    for (size_t i = len0 + 1; i < n; i += strlen(buf + i) + 1)
        if (strncmp(buf + i, "--type=", 7) == 0) return 0;
    return 1;
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

__attribute__((constructor)) static void bngearth_gcpatch(void) {
    if (!is_main_game()) return;
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
    long pagesz = sysconf(_SC_PAGESIZE);
    uintptr_t page = (uintptr_t)imm & ~(uintptr_t)(pagesz - 1);
    // writable for this one byte, before the game starts any thread; then read+exec again
    if (mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        report("not applied: mprotect refused%s", NULL);
        return;
    }
    *imm = PATCHED;
    mprotect((void *)page, (size_t)pagesz, PROT_READ | PROT_EXEC);
    report("applied: grass kept across terrain block moves%s", how);
}
