/* The Windows terfast code as flat code at its block offset (0x100; the stub and the trampolines before it), its
   state on the block's data page. */
SECTIONS {
    . = 0x100;
    .text : { *(.text .text.*) *(.rodata .rodata.* .rodata.cst*) }
    tf_state = 0x1000;
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.data*) *(.bss*) *(.got*) *(.rela*) *(.dynamic) }
}
