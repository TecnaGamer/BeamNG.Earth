/* efw7shim.dll - Windows 7 / Vista stand-ins for the few newer APIs that Rust-built extension modules import:
 *   bcryptprimitives.dll!ProcessPrng            (Windows 10)  -> RtlGenRandom (advapi32!SystemFunction036)
 *   api-ms-win-core-synch-l1-2-0.dll!WaitOnAddress / WakeByAddressSingle / WakeByAddressAll (Windows 8)
 *     -> one global critical section + condition variable (spurious wake-ups are allowed by WaitOnAddress'
 *        contract: callers re-check the value). Wake takes the same lock, so no wake-up can be lost.
 * patch_pyd.py points the .pyd's import table at this DLL. */
#include <windows.h>

static CRITICAL_SECTION g_cs;
static CONDITION_VARIABLE g_cv;
typedef BOOLEAN (WINAPI *RtlGenRandom_t)(PVOID, ULONG);
static RtlGenRandom_t g_rng;

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID r) {
    (void)h; (void)r;
    if (reason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_cs);
        InitializeConditionVariable(&g_cv);
        HMODULE a = LoadLibraryA("advapi32.dll");
        if (a) g_rng = (RtlGenRandom_t)GetProcAddress(a, "SystemFunction036");
    }
    return TRUE;
}

__declspec(dllexport) BOOL WINAPI ProcessPrng(PBYTE data, SIZE_T len) {
    if (!g_rng) return FALSE;
    while (len) {
        ULONG n = len > 0x10000000u ? 0x10000000u : (ULONG)len;
        if (!g_rng(data, n)) return FALSE;
        data += n; len -= n;
    }
    return TRUE;
}

static int same(volatile VOID *a, PVOID b, SIZE_T n) {
    volatile BYTE *x = (volatile BYTE *)a; BYTE *y = (BYTE *)b;
    for (SIZE_T i = 0; i < n; i++) if (x[i] != y[i]) return 0;
    return 1;
}

__declspec(dllexport) BOOL WINAPI WaitOnAddress(volatile VOID *addr, PVOID undesired, SIZE_T size, DWORD ms) {
    BOOL ok = TRUE;
    EnterCriticalSection(&g_cs);
    if (same(addr, undesired, size)) {
        ok = SleepConditionVariableCS(&g_cv, &g_cs, ms);     /* FALSE + ERROR_TIMEOUT on timeout */
    }
    LeaveCriticalSection(&g_cs);
    return ok;
}

__declspec(dllexport) VOID WINAPI WakeByAddressSingle(PVOID addr) {
    (void)addr;
    EnterCriticalSection(&g_cs); WakeAllConditionVariable(&g_cv); LeaveCriticalSection(&g_cs);
}
__declspec(dllexport) VOID WINAPI WakeByAddressAll(PVOID addr) {
    (void)addr;
    EnterCriticalSection(&g_cs); WakeAllConditionVariable(&g_cv); LeaveCriticalSection(&g_cs);
}
