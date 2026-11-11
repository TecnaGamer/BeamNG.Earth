; test_blob.c's stand-ins for the game: a call site (run_site) that calls the trampoline the way the game's main loop
; calls its MainLoopTimer add, with known values in every register, and the original callee (capture) that records
; what reached it. nasm -f elf64.
bits 64
default rel

section .data
global g_save, g_tramp
g_save:  dq 0           ; uint64_t[32]
g_tramp: dq 0

section .rodata
xs: dq 1001.0, 1002.0, 1003.0, 1004.0, 1005.0, 1006.0, 1007.0, 1008.0, 1009.0, 1010.0, 1011.0, 1012.0, 1013.0, 1014.0, 1015.0

section .text
; void run_site(void *tramp, double raw, uint64_t *save)  (SysV)
; save[0..13]: at the callee: rax rcx rdx r8 r9 r10 r11, xmm0..xmm5 (low 8 bytes), its rsp
; save[16..33]: after the call: rbx rbp rsi rdi r12 r13 r14 r15, xmm6..xmm15
global run_site
run_site:
    push rbx
    push rbp
    push r12
    push r13
    push r14
    push r15
    sub rsp, 8                      ; 0 mod 16 at the call below
    mov [g_save], rsi
    mov [g_tramp], rdi
    mov rbx, 0x1000000000000001
    mov rbp, 0x1000000000000002
    mov rsi, 0x1000000000000003
    mov rdi, 0x1000000000000004
    mov r12, 0x1000000000000005
    mov r13, 0x1000000000000006
    mov r14, 0x1000000000000007
    mov r15, 0x1000000000000008
    mov rcx, 0x2000000000000001
    mov rdx, 0x2000000000000002
    mov r8,  0x2000000000000003
    mov r9,  0x2000000000000004
    mov r10, 0x2000000000000005
    mov r11, 0x2000000000000006
    mov rax, 0x2000000000000007
    movsd xmm1, [xs + 0x00]
    movsd xmm2, [xs + 0x08]
    movsd xmm3, [xs + 0x10]
    movsd xmm4, [xs + 0x18]
    movsd xmm5, [xs + 0x20]
    movsd xmm6, [xs + 0x28]
    movsd xmm7, [xs + 0x30]
    movsd xmm8, [xs + 0x38]
    movsd xmm9, [xs + 0x40]
    movsd xmm10, [xs + 0x48]
    movsd xmm11, [xs + 0x50]
    movsd xmm12, [xs + 0x58]
    movsd xmm13, [xs + 0x60]
    movsd xmm14, [xs + 0x68]
    movsd xmm15, [xs + 0x70]
    call [g_tramp]
    push rax
    mov rax, [g_save]
    mov [rax + 0x80], rbx
    mov [rax + 0x88], rbp
    mov [rax + 0x90], rsi
    mov [rax + 0x98], rdi
    mov [rax + 0xa0], r12
    mov [rax + 0xa8], r13
    mov [rax + 0xb0], r14
    mov [rax + 0xb8], r15
    movsd [rax + 0xc0], xmm6
    movsd [rax + 0xc8], xmm7
    movsd [rax + 0xd0], xmm8
    movsd [rax + 0xd8], xmm9
    movsd [rax + 0xe0], xmm10
    movsd [rax + 0xe8], xmm11
    movsd [rax + 0xf0], xmm12
    movsd [rax + 0xf8], xmm13
    movsd [rax + 0x100], xmm14
    movsd [rax + 0x108], xmm15
    pop rax
    add rsp, 8
    pop r15
    pop r14
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; the original callee: records the registers it got, returns to the call site
global capture
capture:
    push rax
    mov rax, [g_save]
    mov [rax + 0x08], rcx
    mov [rax + 0x10], rdx
    mov [rax + 0x18], r8
    mov [rax + 0x20], r9
    mov [rax + 0x28], r10
    mov [rax + 0x30], r11
    movsd [rax + 0x38], xmm0
    movsd [rax + 0x40], xmm1
    movsd [rax + 0x48], xmm2
    movsd [rax + 0x50], xmm3
    movsd [rax + 0x58], xmm4
    movsd [rax + 0x60], xmm5
    lea rcx, [rsp + 8]
    mov [rax + 0x68], rcx
    pop rcx
    mov [rax + 0x00], rcx
    ret
