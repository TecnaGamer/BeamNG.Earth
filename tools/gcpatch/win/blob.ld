/* The Windows dtsmooth filter as flat code at its block offset (dtsmooth_tramp.asm FILTER): the entry first. */
ENTRY(bnge_dtf_win)
SECTIONS {
    . = 0x100;
    .text : { *(.text.entry) *(.text .text.*) *(.rodata .rodata.* .rodata.cst*) }
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.data*) *(.bss*) *(.got*) *(.rela*) *(.dynamic) }
}
