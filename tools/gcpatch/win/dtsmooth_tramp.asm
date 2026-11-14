; dtsmooth for the Windows game (tools/gcpatch/README.md, enginepatch.py): the trampoline at the start of the block
; the launcher writes into the game. The game's main loop calls its MainLoopTimer add with xmm0 = raw right after it
; stored dtReal (and the OpenXR override); that call's rel32 is pointed here. Win64 ABI: the call site may lose every
; volatile register, but rcx/rdx/r8/r9 and xmm0-xmm3 may be the callee's arguments: all volatile registers are kept
; for it. The filter (dtsmooth_filter.c, ms_abi) keeps the non-volatile ones itself.
;
; Block layout (enginepatch.py BLOCK_*): 0x0000 this code, 0x0100 the filter, 0x1000 (a read-write page) the
; original callee's address, 0x1010 the filter's state (struct bnge_dtf).
;   nasm -f bin -o dtsmooth_tramp.bin dtsmooth_tramp.asm
bits 64
default rel
org 0

FILTER  equ $$ + 0x0100
ORIG    equ $$ + 0x1000
STATE   equ $$ + 0x1010

tramp:
    push rcx
    push rdx
    push r8
    push r9
    push r10
    push r11
    push rax                        ; entry rsp = 8 mod 16; + 7 * 8 = 0 mod 16
    sub rsp, 0x80                   ; 0x20 shadow space + xmm0-xmm5
    movdqu [rsp + 0x20], xmm0
    movdqu [rsp + 0x30], xmm1
    movdqu [rsp + 0x40], xmm2
    movdqu [rsp + 0x50], xmm3
    movdqu [rsp + 0x60], xmm4
    movdqu [rsp + 0x70], xmm5
    lea rcx, [STATE]                ; bnge_dtf_win(state, raw)
    movapd xmm1, xmm0
    call FILTER
    movdqu xmm0, [rsp + 0x20]
    movdqu xmm1, [rsp + 0x30]
    movdqu xmm2, [rsp + 0x40]
    movdqu xmm3, [rsp + 0x50]
    movdqu xmm4, [rsp + 0x60]
    movdqu xmm5, [rsp + 0x70]
    add rsp, 0x80
    pop rax
    pop r11
    pop r10
    pop r9
    pop r8
    pop rdx
    pop rcx
    jmp [ORIG]                      ; on to the original callee (its return goes to the game's call site)
