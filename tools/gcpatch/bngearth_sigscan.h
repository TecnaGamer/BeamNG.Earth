// Masked whole-function signatures (mksigs.py) searched in the game's code (bngearth_terfast.c, bngearth_texkeep.c).
// A patch acts only on functions found exactly once with every unmasked byte equal: the same instructions, registers,
// structure offsets and immediates as the build the signature was made from; only rip displacements and the rel32 of
// calls / jumps leaving the function may differ (a rebuild moves them). Addresses beyond a function (its callees, the
// constants it loads) are read from the matched code at offsets of the signature and checked by the caller.
#pragma once
#include "bngearth_common.h"

struct bnge_sig {
    const char *name;
    uint32_t len;
    const unsigned char *code;
    const unsigned char *mask;      // bit i (byte i / 8, bit i % 8): byte i must equal code[i]
    uint32_t anchor, anchor_len;    // a run of matched bytes searched first
    uintptr_t ref;                  // the reference build's vaddr (reports only)
};

static inline int bnge_sig_at(const struct bnge_sig *s, const unsigned char *p) {
    for (uint32_t i = 0; i < s->len; i++)
        if ((s->mask[i >> 3] >> (i & 7)) & 1)
            if (p[i] != s->code[i]) return 0;
    return 1;
}

// The number of matches of `s` in the main program's executable segments; *out = the (last) match's runtime address.
// Each search is a memmem of the anchor over the code (~50 MB: a few ms) and a full compare at each anchor hit.
static inline int bnge_sig_find(const struct bnge_main *m, const struct bnge_sig *s, uintptr_t *out) {
    int n = 0;
    *out = 0;
    const unsigned char *needle = s->code + s->anchor;
    for (int k = 0; k < m->nseg; k++) {
        if (!(m->seg[k].flags & PF_X)) continue;
        const unsigned char *lo = (const unsigned char *)m->seg[k].start, *hi = (const unsigned char *)m->seg[k].end;
        const unsigned char *p = lo + s->anchor;
        while (p + s->anchor_len <= hi) {
            const unsigned char *q = memmem(p, (size_t)(hi - p), needle, s->anchor_len);
            if (!q) break;
            const unsigned char *f = q - s->anchor;
            if (f >= lo && f + s->len <= hi && bnge_sig_at(s, f)) { n++; *out = (uintptr_t)f; }
            p = q + 1;
        }
    }
    return n;
}

// The target of a rel32 / rip displacement whose 4 bytes are at fn + disp_off and whose instruction ends at fn + end.
static inline uintptr_t bnge_rel_target(uintptr_t fn, size_t disp_off, size_t end) {
    int32_t d;
    memcpy(&d, (const void *)(fn + disp_off), 4);
    return fn + end + (intptr_t)d;
}

// The ELF build id of the main program (hex, "" if none): which build a status line talks about.
struct bnge_buildid_cb { char *out; size_t n; int seen; };
static inline int bnge_buildid_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    struct bnge_buildid_cb *c = data;
    if (c->seen) return 1;
    c->seen = 1;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type != PT_NOTE) continue;
        const unsigned char *p = (const unsigned char *)(info->dlpi_addr + ph->p_vaddr), *e = p + ph->p_memsz;
        while (p + 12 <= e) {
            uint32_t nsz, dsz, type;
            memcpy(&nsz, p, 4); memcpy(&dsz, p + 4, 4); memcpy(&type, p + 8, 4);
            const unsigned char *name = p + 12, *desc = name + ((nsz + 3) & ~3u);
            if (desc + dsz > e) break;
            if (type == 3 && nsz == 4 && memcmp(name, "GNU", 4) == 0) {       // NT_GNU_BUILD_ID
                size_t k = 0;
                for (uint32_t j = 0; j < dsz && k + 3 <= c->n; j++) k += (size_t)snprintf(c->out + k, c->n - k, "%02x", desc[j]);
                return 1;
            }
            p = desc + ((dsz + 3) & ~3u);
        }
    }
    return 1;
}
static inline void bnge_build_id(char *out, size_t n) {
    struct bnge_buildid_cb c = {out, n, 0};
    out[0] = 0;
    dl_iterate_phdr(bnge_buildid_cb, &c);
}
