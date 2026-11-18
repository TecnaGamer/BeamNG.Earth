// Shared helpers of the BeamNG.Earth preload patches (bngearth_gcpatch.c, bngearth_texkeep.c; README.md).
#pragma once
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <elf.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

// The BeamNG.Earth level's flag (groundcover.lua M.PATCH_FLAG): the patches act only while it exists.
#define BNGE_LEVEL_FLAG "beamngearth_gcpatch_on"
#define BNGE_FLAG_POLL_MS 250

// The game's user folder's temp: BNGEARTH_USER_DIR (the launcher's --user-dir), else the native game's default.
static inline void bnge_temp_path(char *out, size_t n, const char *name) {
    const char *user = getenv("BNGEARTH_USER_DIR");
    if (user && *user) snprintf(out, n, "%s/temp/%s", user, name);
    else snprintf(out, n, "%s/.local/share/BeamNG/BeamNG.drive/current/temp/%s", getenv("HOME") ? getenv("HOME") : "", name);
}

// One line for the player / the mod's diagnostics: stderr (with `tag`) and <user>/temp/<file> (overwritten).
static inline void bnge_report(const char *tag, const char *file, const char *msg) {
    fprintf(stderr, "[%s] %s\n", tag, msg);
    char path[512];
    bnge_temp_path(path, sizeof path, file);
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "%ld pid %d: %s\n", (long)time(NULL), (int)getpid(), msg);
    fclose(f);
}

// The main game process: argv[0] ends in BeamNG.drive.x64 and no argument is a CEF --type=.
static inline int bnge_is_main_game(void) {
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

// The main program (the first object dl_iterate_phdr reports): load base and its PT_LOAD segments.
struct bnge_main {
    uintptr_t base; int seen; int nseg;
    struct { uintptr_t start, end; int flags; } seg[16];   // vaddr ranges (base added), p_memsz
};

static inline int bnge_find_main_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    struct bnge_main *m = data;
    if (m->seen) return 1;
    m->seen = 1;
    m->base = (uintptr_t)info->dlpi_addr;
    for (int i = 0; i < info->dlpi_phnum && m->nseg < 16; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type != PT_LOAD) continue;
        m->seg[m->nseg].start = m->base + ph->p_vaddr;
        m->seg[m->nseg].end = m->base + ph->p_vaddr + ph->p_memsz;
        m->seg[m->nseg].flags = (int)ph->p_flags;
        m->nseg++;
    }
    return 1;
}

static inline void bnge_find_main(struct bnge_main *m) {
    memset(m, 0, sizeof *m);
    dl_iterate_phdr(bnge_find_main_cb, m);
}

// Is [vaddr, vaddr + n) inside one of the main program's segments (all `flags` set)?
static inline int bnge_mapped(const struct bnge_main *m, uintptr_t vaddr, size_t n, int flags) {
    uintptr_t a = m->base + vaddr;
    for (int i = 0; i < m->nseg; i++)
        if (a >= m->seg[i].start && a + n <= m->seg[i].end && (m->seg[i].flags & flags) == flags) return 1;
    return 0;
}

// A page (read + write) within rel32 reach of `site` (both directions, 1 MB steps), or NULL.
#include <sys/mman.h>
#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif
static inline unsigned char *bnge_near_page(uintptr_t site) {
    long pagesz = sysconf(_SC_PAGESIZE);
    const uintptr_t step = 1u << 20, reach = 0x7ff00000u;
    for (uintptr_t d = step; d < reach; d += step) {
        for (int dir = 0; dir < 2; dir++) {
            uintptr_t a = dir ? site + d : site - d;
            if (!dir && site < d + 0x10000) continue;
            a &= ~(uintptr_t)(pagesz - 1);
            void *p = mmap((void *)a, (size_t)pagesz, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
            if (p == MAP_FAILED) continue;
            if ((uintptr_t)p != a) { munmap(p, (size_t)pagesz); continue; }   // (an old kernel took it as a hint)
            return p;
        }
    }
    return NULL;
}
