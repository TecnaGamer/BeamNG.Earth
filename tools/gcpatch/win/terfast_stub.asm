; terfast for the Windows game (terfast_win.c, enginepatch_terfast.py): the stub the hole scan's first instruction
; (cmp eax, 0x40; ja past-the-loop, in TerrCell::_updateVertexBuffer: rdi = the cell, r14 = the TerrainFile, eax = the
; cell's size) is pointed at. The function's frame holds the 32-byte home area and rsp is aligned there (it calls
; other functions from the same frame); every register the loop or the code after it reads is non-volatile (rdi,
; r13, r14, xmm6, xmm7) or set again by them.
;   nasm -f bin -o terfast_stub.bin terfast_stub.asm
bits 64
default rel
org 0

STATE       equ $$ + 0x1000
VB_AFTER    equ STATE + 0x20
VB_BODY     equ STATE + 0x28
HOLES_FN    equ STATE + 0x78

holes_stub:
    mov rcx, rdi
    mov rdx, r14
    call [HOLES_FN]                 ; tf_holes(cell, file): 0 = the flag is set, 1 = run the stock loop
    test eax, eax
    jnz .stock
    jmp [VB_AFTER]
.stock:
    mov eax, [rdi + 0x28]
    cmp eax, 0x40
    ja .after
    jmp [VB_BODY]
.after:
    jmp [VB_AFTER]
