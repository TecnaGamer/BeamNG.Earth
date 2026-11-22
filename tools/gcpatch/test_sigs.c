// Offline check of the Linux patches' target search (bngearth_sigscan.h) against a BeamNG.drive.x64 file:
//   cc -O2 -o test_sigs test_sigs.c -DPATCH_TERFAST (or -DPATCH_TEXKEEP) -lm -pthread;   ./test_sigs BeamNG.drive.x64 [mode]
// The file is laid out as the loader does (each PT_LOAD at base + vaddr, R_X86_64_RELATIVE relocations applied, so
// the vtables hold real addresses), then the patch's own locate function runs on it exactly as in the game.
// mode: "ok" (default) prints the targets; "flip" changes one matched byte of the first function and expects a
// refusal; "dup" copies the first function's code into free code space and expects "2 matches".
#define BNGE_SIGTEST 1
#if defined(PATCH_TERFAST)
#include "bngearth_terfast.c"
#define IS_TERFAST 1
#elif defined(PATCH_TEXKEEP)
#include "bngearth_texkeep.c"
#define IS_TERFAST 0
#else
#error define PATCH_TERFAST or PATCH_TEXKEEP
#endif
#include <fcntl.h>
#include <sys/stat.h>

static int load(const char *path, struct bnge_main *m) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) { perror(path); return 0; }
    struct stat st;
    fstat(fd, &st);
    unsigned char *f = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (f == MAP_FAILED) return 0;
    const Elf64_Ehdr *eh = (const Elf64_Ehdr *)f;
    const Elf64_Phdr *ph = (const Elf64_Phdr *)(f + eh->e_phoff);
    uint64_t top = 0;
    for (int i = 0; i < eh->e_phnum; i++)
        if (ph[i].p_type == PT_LOAD && ph[i].p_vaddr + ph[i].p_memsz > top) top = ph[i].p_vaddr + ph[i].p_memsz;
    unsigned char *img = mmap(NULL, top + 0x10000, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (img == MAP_FAILED) return 0;
    memset(m, 0, sizeof *m);
    m->seen = 1;
    m->base = (uintptr_t)img;
    uint64_t rela = 0, relasz = 0;
    for (int i = 0; i < eh->e_phnum; i++) {
        if (ph[i].p_type == PT_GNU_RELRO) { m->relro_start = m->base + ph[i].p_vaddr; m->relro_end = m->relro_start + ph[i].p_memsz; }
        if (ph[i].p_type == PT_DYNAMIC) {
            const Elf64_Dyn *d = (const Elf64_Dyn *)(f + ph[i].p_offset);
            for (; d->d_tag != DT_NULL; d++) {
                if (d->d_tag == DT_RELA) rela = d->d_un.d_ptr;
                if (d->d_tag == DT_RELASZ) relasz = d->d_un.d_val;
            }
        }
        if (ph[i].p_type != PT_LOAD) continue;
        memcpy(img + ph[i].p_vaddr, f + ph[i].p_offset, ph[i].p_filesz);
        m->seg[m->nseg].start = m->base + ph[i].p_vaddr;
        m->seg[m->nseg].end = m->base + ph[i].p_vaddr + ph[i].p_memsz;
        m->seg[m->nseg].end_file = m->base + ph[i].p_vaddr + ph[i].p_filesz;
        m->seg[m->nseg].flags = (int)ph[i].p_flags;
        m->nseg++;
    }
    // (DT_RELA is a vaddr: the loaded image has it)
    for (uint64_t off = 0; rela && off + sizeof(Elf64_Rela) <= relasz; off += sizeof(Elf64_Rela)) {
        const Elf64_Rela *r = (const Elf64_Rela *)(img + rela + off);
        if (ELF64_R_TYPE(r->r_info) == R_X86_64_RELATIVE) {
            uint64_t v = m->base + (uint64_t)r->r_addend;
            memcpy(img + r->r_offset, &v, 8);
        }
    }
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s BeamNG.drive.x64 [ok|flip|dup]\n", argv[0]); return 2; }
    const char *mode = argc > 2 ? argv[2] : "ok";
    struct bnge_main m;
    if (!load(argv[1], &m)) return 2;
    char why[160];
    const char *r;
#if IS_TERFAST
    struct tf_targets t;
    const struct bnge_sig *sig0 = &tf_bounds;
#else
    struct tk_targets t;
    const struct bnge_sig *sig0 = &tk_create;
#endif
    if (strcmp(mode, "ok") != 0) {
        uintptr_t at;
        if (bnge_sig_find(&m, sig0, &at) != 1) { printf("FAIL: %s not found before the change\n", sig0->name); return 1; }
        if (!strcmp(mode, "flip")) {
            uint32_t i = sig0->len / 2;
            while (!((sig0->mask[i >> 3] >> (i & 7)) & 1)) i++;
            ((unsigned char *)at)[i] ^= 0x01;
        } else {                   // dup: into the last bytes of the executable segment's file part
            for (int k = 0; k < m.nseg; k++)
                if (m.seg[k].flags & PF_X) {
                    unsigned char *dst = (unsigned char *)((m.seg[k].end_file - sig0->len - 64) & ~(uintptr_t)63);
                    memcpy(dst, (void *)at, sig0->len);
                }
        }
    }
#if IS_TERFAST
    r = tf_locate(&m, &t, why, sizeof why);
    if (!r) printf("terfast: bounds %#lx mats %#lx vb %#lx xform %#lx mats_tail %#lx get_transform %#lx vb_scan %#lx "
                   "vb_body %#lx vb_after %#lx\n", t.bounds - m.base, t.mats - m.base, t.vb - m.base, t.xform - m.base,
                   t.mats_tail - m.base, t.get_transform - m.base, t.vb_scan - m.base, t.vb_body - m.base,
                   t.vb_after - m.base);
#else
    r = tk_locate(&m, &t, why, sizeof why);
    if (!r) {
        printf("texkeep: create %#lx loader %#lx request_delete %#lx destroy_self %#lx ret_cached %#lx ret_packed %#lx slots",
               t.create - m.base, t.loader - m.base, t.request_delete - m.base, t.destroy_self - m.base,
               t.ret_cached - m.base, t.ret_packed - m.base);
        for (int i = 0; i < t.nslots; i++) printf(" %#lx", (uintptr_t)t.slots[i] - m.base);
        printf("\n");
    }
#endif
    if (r) printf("refused: %s\n", r);
    if (!strcmp(mode, "ok")) return r ? 1 : 0;
    return r ? 0 : 1;              // flip / dup: a refusal is the pass
}
