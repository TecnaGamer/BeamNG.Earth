"""Engine patches for the Windows game, applied by the launcher to the running BeamNG.drive.x64.exe (no files on disk
change, no DLL is loaded into the game). The Linux game gets the same patches from the preload library
tools/gcpatch (README.md there); Windows has no LD_PRELOAD, so the launcher writes them into the game's memory with
the documented debugging API (OpenProcess, VirtualAllocEx, WriteProcessMemory, VirtualProtectEx) after the game
started, as debuggers and trainers do.

dtsmooth (steady game speed): the engine's frame-time filter replays every hitch at up to ~2x speed and tracks uneven
frame times slowly (0.63-1.23x game speed over quarter seconds measured on Windows, 2026-11-03). The main loop calls
its MainLoopTimer add with xmm0 = raw right after it stored dtReal; that call's rel32 is pointed at a block written
near the game's code (tools/gcpatch/win: a trampoline that keeps every argument register, the filter of
tools/gcpatch/bngearth_dtfilter.h compiled to Win64 machine code, its state), which overwrites dtReal once a frame.
The code is found by its instructions (displacements masked; one match only), checked against the build's own
addresses, and patched with the game's threads suspended for the 4-byte write. While the BeamNG.Earth level flag
(<user>/temp/beamngearth_gcpatch_on, written by the mod) exists the filter runs; otherwise the block leaves dtReal as
the engine made it. Status: <user>/temp/beamngEarth_dtsmooth.txt (as the Linux library).

Off unless BNGEARTH_DTSMOOTH_WIN=1 until checked on a Windows PC (tools/gcpatch/README.md "Windows" has the steps);
BNGEARTH_DTSMOOTH_OFF=1 / BNGEARTH_DTSMOOTH=0 always win.

  python enginepatch.py --scan PATH\\TO\\BeamNG.drive.x64.exe     (offline: does this build match?)
  python enginepatch.py --attach --user-dir DIR                   (Windows: patch the running game, follow the flag)
"""
import os
import re
import struct
import sys
import threading
import time

TAG = "enginepatch"
STATUS_FILE = "beamngEarth_dtsmooth.txt"
LEVEL_FLAG = "beamngearth_gcpatch_on"
CTL_FILE = "beamngearth_dtsmooth_ctl"    # (tests: BNGEARTH_DTSMOOTH_CTL=1, as the Linux library)
FLAG_POLL_S = 0.25
GAME_EXE = "BeamNG.drive.x64.exe"

# the block (tools/gcpatch/win/dtsmooth_tramp.asm)
BLOCK_SIZE = 0x2000
BLOCK_FILTER = 0x100
BLOCK_ORIG = 0x1000
BLOCK_STATE = 0x1010
# struct bnge_dtf (bngearth_dtfilter.h; test_dtfilter.c checks these offsets)
DTF_SIZE = 0x1b0
DTF = {"hitch_abs": (0x00, "d"), "hitch_rel": (0x08, "d"), "tau": (0x10, "d"), "repay": (0x18, "d"),
       "debt_max": (0x20, "d"), "dtreal": (0x28, "Q"), "xr": (0x30, "Q"), "xr_off": (0x38, "i"), "n": (0x3c, "i"),
       "enabled": (0x40, "i"), "init": (0x44, "i"), "top_n": (0x48, "i"), "frames": (0x190, "Q"), "capped": (0x198, "Q"),
       "dropped": (0x1a0, "d"), "repaid": (0x1a8, "d")}
# defaults (bnge_dtf_defaults)
DTF_DEFAULTS = {"hitch_abs": 0.010, "hitch_rel": 1.0, "tau": 4.0, "repay": 0.10, "debt_max": 0.5, "n": 3, "top_n": 16}


def _pat(spec):
    return re.compile(b"".join(b"." if t == "??" else re.escape(bytes([int(t, 16)])) for t in spec.split()), re.S)


# The filter's start, after the timer call (0.40.4 VA 0x1407b20b3 in the 2026-11-06 build): raw = fmin(1, t); xmm3 =
# raw; dtRaw = raw; ... xmm1 = dtReal; xmm1 -= raw
SIG = _pat("0f 28 c8 f2 0f 10 05 ?? ?? ?? ?? e8 ?? ?? ?? ?? 0f 28 d8 f2 0f 11 05 ?? ?? ?? ?? f2 0f 10 15 ?? ?? ?? ?? "
           "f2 0f 11 55 ?? f2 0f 10 05 ?? ?? ?? ?? f2 0f 11 44 24 ?? f2 0f 10 0d ?? ?? ?? ?? f2 0f 5c cb")
SIG_DTRAW = 19          # f2 0f 11 05 <dtRaw>: movsd [dtRaw], xmm0
SIG_DTREAL = 54         # f2 0f 10 0d <dtReal>: movsd xmm1, [dtReal]
# Its end: the OpenXR manager stored after its lazy creation; xmm0 = dtRaw; if its session runs dtReal = raw; then the
# MainLoopTimer add (the call hooked)
TAIL = _pat("48 89 05 ?? ?? ?? ?? f2 0f 10 05 ?? ?? ?? ?? 80 b8 ?? ?? ?? ?? 00 74 08 f2 0f 11 05 ?? ?? ?? ?? "
            "e8 ?? ?? ?? ??")
TAIL_SEARCH = 0x300
TAIL_XR, TAIL_DTRAW, TAIL_XROFF, TAIL_DTREAL, TAIL_CALL = 0, 7, 17, 24, 32


def _rip(buf, insn_off, disp_off, insn_len, va_of_buf0):
    d = struct.unpack_from("<i", buf, insn_off + disp_off)[0]
    return va_of_buf0 + insn_off + insn_len + d


def find_dtsmooth(code, code_va):
    """The hook in `code` (bytes of the game's executable section, mapped at code_va): a dict of virtual addresses
    (site: the call's first byte; orig: its target; dtreal, dtraw, xr: globals; xr_off), or (None, reason)."""
    hits = [m.start() for m in SIG.finditer(code)]
    if len(hits) != 1:
        return None, f"the frame-time filter was not found in this build ({len(hits)} matches)"
    s = hits[0]
    dtraw = _rip(code, s + SIG_DTRAW, 4, 8, code_va)
    dtreal = _rip(code, s + SIG_DTREAL, 4, 8, code_va)
    window = code[s: s + TAIL_SEARCH]
    tails = [m.start() for m in TAIL.finditer(window)]
    if len(tails) != 1:
        return None, f"the filter's end differs in this build ({len(tails)} matches)"
    t = s + tails[0]
    xr = _rip(code, t + TAIL_XR, 3, 7, code_va)
    if _rip(code, t + TAIL_DTRAW, 4, 8, code_va) != dtraw or _rip(code, t + TAIL_DTREAL, 4, 8, code_va) != dtreal:
        return None, "the filter's end uses other globals"
    # the computed dtReal is stored before (movsd [dtReal], xmm1) and the manager was read from the same slot
    store = re.compile(re.escape(b"\xf2\x0f\x11\x0d"), re.S)
    if not any(_rip(code, s + m.start(), 4, 8, code_va) == dtreal for m in store.finditer(code[s: t])):
        return None, "the filter's dtReal store was not found"
    load = re.compile(re.escape(b"\x48\x8b\x05"), re.S)
    if not any(_rip(code, s + m.start(), 3, 7, code_va) == xr for m in load.finditer(code[s: t])):
        return None, "the OpenXR manager's read was not found"
    xr_off = struct.unpack_from("<i", code, t + TAIL_XROFF)[0]
    if not 0 < xr_off < 0x10000:
        return None, "the OpenXR flag's offset is unexpected"
    site = t + TAIL_CALL
    orig = _rip(code, site, 1, 5, code_va)
    return {"site": code_va + site, "orig": orig, "dtreal": dtreal, "dtraw": dtraw, "xr": xr, "xr_off": xr_off,
            "rel32": struct.unpack_from("<i", code, site + 1)[0]}, None


def pe_sections(read, base):
    """[(name, rva, vsize, characteristics)] of the PE image at `base`, read with read(addr, n)."""
    hdr = read(base, 0x1000)
    if hdr[:2] != b"MZ":
        raise ValueError("not a PE image")
    pe = struct.unpack_from("<I", hdr, 0x3c)[0]
    if hdr[pe:pe + 4] != b"PE\0\0":
        raise ValueError("no PE header")
    nsec = struct.unpack_from("<H", hdr, pe + 6)[0]
    opt = struct.unpack_from("<H", hdr, pe + 20)[0]
    out = []
    for i in range(nsec):
        o = pe + 24 + opt + 40 * i
        name, vsize, rva = hdr[o:o + 8].rstrip(b"\0").decode("ascii", "replace"), *struct.unpack_from("<II", hdr, o + 8)
        ch = struct.unpack_from("<I", hdr, o + 36)[0]
        out.append((name, rva, vsize, ch))
    return out


def scan_file(path):
    """Offline: find_dtsmooth on an executable file as the loader would map it (at its preferred base)."""
    with open(path, "rb") as f:
        data = f.read()
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    opt = struct.unpack_from("<H", data, pe + 20)[0]
    image_base = struct.unpack_from("<Q", data, pe + 24 + 24)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    for i in range(nsec):
        o = pe + 24 + opt + 40 * i
        name = data[o:o + 8].rstrip(b"\0")
        vsize, rva, rsize, roff = struct.unpack_from("<IIII", data, o + 8)
        if name == b".text":
            code = data[roff: roff + min(vsize, rsize)]
            return find_dtsmooth(code, image_base + rva)
    return None, "no .text section"


def block_bytes(hook, params=None):
    """The block to write at its address: code page (trampoline + filter) and data page (orig + state)."""
    import enginepatch_code as ec
    b = bytearray(BLOCK_SIZE)
    b[0:len(ec.TRAMP)] = ec.TRAMP
    b[BLOCK_FILTER:BLOCK_FILTER + len(ec.FILTER)] = ec.FILTER
    struct.pack_into("<Q", b, BLOCK_ORIG, hook["orig"])
    p = dict(DTF_DEFAULTS)
    p.update(params or {})
    st = dict(p, dtreal=hook["dtreal"], xr=hook["xr"], xr_off=hook["xr_off"], enabled=0, init=0)
    for k, v in st.items():
        off, fmt = DTF[k]
        struct.pack_into("<" + fmt, b, BLOCK_STATE + off, v)
    return bytes(b)


def env_params():
    p = {}
    for k, name, lo, hi in (("repay", "REPAY", 0.0, 1.0), ("hitch_abs", "HITCH", 0.0005, 2.0),
                            ("tau", "TAU", 1.0, 1000.0), ("n", "N", 1, 8)):
        v = os.environ.get("BNGEARTH_DTSMOOTH_" + name)
        try:
            if v and lo <= float(v) <= hi:
                p[k] = int(float(v)) if k == "n" else float(v)
        except ValueError:
            pass
    return p


def env_off():
    off = os.environ.get("BNGEARTH_DTSMOOTH_OFF", "")
    return (off not in ("", "0")) or os.environ.get("BNGEARTH_DTSMOOTH") == "0"


def env_on_windows():
    return os.environ.get("BNGEARTH_DTSMOOTH_WIN") == "1"


def report(user_dir, msg, log=None):
    line = f"[{TAG}] dtsmooth: {msg}"
    (log or (lambda s: print(s, file=sys.stderr)))(line)
    if user_dir:
        try:
            os.makedirs(os.path.join(user_dir, "temp"), exist_ok=True)
            with open(os.path.join(user_dir, "temp", STATUS_FILE), "w") as f:
                f.write(f"{int(time.time())} launcher pid {os.getpid()}: {msg}\n")
        except OSError:
            pass


# --- Windows process access ------------------------------------------------------------------------------------------

class WinProcess:
    """The documented Win32 calls on another process (ctypes). Windows (and Wine / Proton) only."""

    PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400 | 0x0800   # VM_OPERATION | VM_READ | VM_WRITE |
    #                                                               QUERY_INFORMATION | SUSPEND_RESUME
    MEM_COMMIT, MEM_RESERVE, MEM_RELEASE, MEM_FREE = 0x1000, 0x2000, 0x8000, 0x10000
    PAGE_READWRITE, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE = 0x04, 0x20, 0x40
    TH32CS_SNAPTHREAD, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x4, 0x8, 0x10
    THREAD_SUSPEND_RESUME = 0x0002

    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes as w
        self.ct, self.w = ctypes, w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k = k
        k.OpenProcess.restype = w.HANDLE
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t)]
        k.WriteProcessMemory.argtypes = k.ReadProcessMemory.argtypes
        k.VirtualAllocEx.restype = ctypes.c_void_p
        k.VirtualAllocEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD, w.DWORD]
        k.VirtualFreeEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD]
        k.VirtualProtectEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t, w.DWORD, ctypes.POINTER(w.DWORD)]
        k.FlushInstructionCache.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.OpenThread.restype = w.HANDLE
        k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.SuspendThread.argtypes = [w.HANDLE]
        k.SuspendThread.restype = w.DWORD
        k.ResumeThread.argtypes = [w.HANDLE]
        k.ResumeThread.restype = w.DWORD
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", w.DWORD), ("PartitionId", w.WORD), ("RegionSize", ctypes.c_size_t),
                        ("State", w.DWORD), ("Protect", w.DWORD), ("Type", w.DWORD)]
        self.MBI = MBI
        k.VirtualQueryEx.restype = ctypes.c_size_t
        k.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        self.pid = pid
        self.h = k.OpenProcess(self.PROCESS_ACCESS, False, pid)
        if not self.h:
            raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")

    def close(self):
        if self.h:
            self.k.CloseHandle(self.h)
            self.h = None

    def alive(self):
        code = self.w.DWORD()
        return bool(self.k.GetExitCodeProcess(self.h, self.ct.byref(code))) and code.value == 259   # STILL_ACTIVE

    def read(self, addr, n):
        buf = self.ct.create_string_buffer(n)
        got = self.ct.c_size_t()
        if not self.k.ReadProcessMemory(self.h, self.ct.c_void_p(addr), buf, n, self.ct.byref(got)) or got.value != n:
            raise OSError(f"ReadProcessMemory({addr:#x}, {n}) failed: {self.ct.get_last_error()}")
        return buf.raw

    def write(self, addr, data):
        got = self.ct.c_size_t()
        if not self.k.WriteProcessMemory(self.h, self.ct.c_void_p(addr), data, len(data), self.ct.byref(got)) \
                or got.value != len(data):
            raise OSError(f"WriteProcessMemory({addr:#x}, {len(data)}) failed: {self.ct.get_last_error()}")

    def protect(self, addr, n, prot):
        old = self.w.DWORD()
        if not self.k.VirtualProtectEx(self.h, self.ct.c_void_p(addr), n, prot, self.ct.byref(old)):
            raise OSError(f"VirtualProtectEx({addr:#x}) failed: {self.ct.get_last_error()}")
        return old.value

    def flush(self, addr, n):
        self.k.FlushInstructionCache(self.h, self.ct.c_void_p(addr), n)

    def module_base(self, name):
        """The base of the process's module `name` (its main executable), or None."""
        ct, w = self.ct, self.w

        class ME32(ct.Structure):
            _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                        ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD), ("modBaseAddr", ct.c_void_p),
                        ("modBaseSize", w.DWORD), ("hModule", w.HMODULE), ("szModule", ct.c_char * 256),
                        ("szExePath", ct.c_char * 260)]
        k = self.k
        snap = k.CreateToolhelp32Snapshot(self.TH32CS_SNAPMODULE | self.TH32CS_SNAPMODULE32, self.pid)
        if not snap or snap == w.HANDLE(-1).value:
            return None
        try:
            me = ME32()
            me.dwSize = ct.sizeof(ME32)
            ok = k.Module32First(snap, ct.byref(me))
            while ok:
                if me.szModule.decode("mbcs", "replace").lower() == name.lower():
                    return me.modBaseAddr
                ok = k.Module32Next(snap, ct.byref(me))
        finally:
            k.CloseHandle(snap)
        return None

    def threads(self):
        ct, w = self.ct, self.w

        class TE32(ct.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ThreadID", w.DWORD),
                        ("th32OwnerProcessID", w.DWORD), ("tpBasePri", w.LONG), ("tpDeltaPri", w.LONG),
                        ("dwFlags", w.DWORD)]
        k = self.k
        snap = k.CreateToolhelp32Snapshot(self.TH32CS_SNAPTHREAD, 0)
        out = []
        if not snap or snap == w.HANDLE(-1).value:
            return out
        try:
            te = TE32()
            te.dwSize = ct.sizeof(TE32)
            ok = k.Thread32First(snap, ct.byref(te))
            while ok:
                if te.th32OwnerProcessID == self.pid:
                    out.append(te.th32ThreadID)
                ok = k.Thread32Next(snap, ct.byref(te))
        finally:
            k.CloseHandle(snap)
        return out

    def alloc_near(self, target, size):
        """size bytes committed read-write within +-2 GB (minus margins) of target, or None."""
        mbi = self.MBI()
        gran = 0x10000          # the allocation granularity
        reach = 0x7ff00000 - size
        lo, hi = max(0x10000, target - reach), target + reach
        up = lambda x: (x + gran - 1) // gran * gran         # noqa: E731
        down = lambda x: x // gran * gran                    # noqa: E731

        def query(a):
            if not self.k.VirtualQueryEx(self.h, self.ct.c_void_p(a), self.ct.byref(mbi), self.ct.sizeof(mbi)):
                return None
            return mbi.BaseAddress or 0, mbi.RegionSize, mbi.State

        def take(a):
            return self.k.VirtualAllocEx(self.h, self.ct.c_void_p(a), size, self.MEM_COMMIT | self.MEM_RESERVE,
                                         self.PAGE_READWRITE)
        a = up(target)                      # upward from the target
        while a <= hi:
            q = query(a)
            if not q or q[1] == 0:
                break
            base, rsize, state = q
            if state == self.MEM_FREE:
                c = up(max(a, base))
                if c + size <= base + rsize and c <= hi:
                    p = take(c)
                    if p:
                        return p
            a = up(base + rsize)
        a = down(target) - gran             # then downward
        while a >= lo:
            q = query(a)
            if not q or q[1] == 0:
                break
            base, rsize, state = q
            if state == self.MEM_FREE:
                c = down(min(a, base + rsize - size))
                if c >= base and c >= lo:
                    p = take(c)
                    if p:
                        return p
            a = down(base) - gran
        return None

    def free(self, addr):
        self.k.VirtualFreeEx(self.h, self.ct.c_void_p(addr), 0, self.MEM_RELEASE)

    def suspended(self):
        """A context manager: every thread of the process suspended (best effort) inside."""
        proc = self

        class _S:
            def __enter__(self):
                self.hs = []
                for tid in proc.threads():
                    th = proc.k.OpenThread(proc.THREAD_SUSPEND_RESUME, False, tid)
                    if th:
                        if proc.k.SuspendThread(th) != 0xFFFFFFFF:
                            self.hs.append(th)
                        else:
                            proc.k.CloseHandle(th)
                return self

            def __exit__(self, *exc):
                for th in self.hs:
                    proc.k.ResumeThread(th)
                    proc.k.CloseHandle(th)
                return False
        return _S()


def game_pid():
    """The pid of the running BeamNG.drive.x64.exe (Windows / Wine), or None."""
    import ctypes
    from ctypes import wintypes as w

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == w.HANDLE(-1).value:
        return None
    try:
        pe = PE32()
        pe.dwSize = ctypes.sizeof(PE32)
        ok = k.Process32First(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.decode("mbcs", "replace").lower() == GAME_EXE.lower():
                return pe.th32ProcessID
            ok = k.Process32Next(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return None


class DtSmoothWin:
    """dtsmooth in one running Windows game: apply() once, then follow the level flag (a thread) until the game ends."""

    def __init__(self, proc, user_dir, log=None):
        self.p, self.user_dir, self.log = proc, user_dir, log
        self.block = None
        self.mode = 1
        self.stop = threading.Event()

    def apply(self, params=None):
        import enginepatch_code  # noqa: F401  (the code must exist before anything is written)
        base = self.p.module_base(GAME_EXE)
        if not base:
            return f"{GAME_EXE} is not loaded in process {self.p.pid}"
        text = [s for s in pe_sections(self.p.read, base) if s[0] == ".text"]
        if not text:
            return "no .text section in the game"
        _, rva, vsize, _ = text[0]
        code = self.p.read(base + rva, vsize)
        hook, why = find_dtsmooth(code, base + rva)
        if not hook:
            return why
        site = hook["site"]
        if self.p.read(site, 5) != b"\xe8" + struct.pack("<i", hook["rel32"]):
            return "the call changed under us"
        blk = self.p.alloc_near(site, BLOCK_SIZE)
        if not blk:
            return "no memory near the game's code"
        rel = blk - (site + 5)
        if not -0x80000000 <= rel < 0x80000000:
            self.p.free(blk)
            return "the block is out of reach"
        try:
            self.p.write(blk, block_bytes(hook, params))
            self.p.protect(blk, 0x1000, WinProcess.PAGE_EXECUTE_READ)      # code page; the data page stays RW
            self.p.flush(blk, 0x1000)
            with self.p.suspended():
                old = self.p.protect(site, 5, WinProcess.PAGE_EXECUTE_READWRITE)
                try:
                    self.p.write(site + 1, struct.pack("<i", rel))
                finally:
                    self.p.protect(site, 5, old)
                self.p.flush(site, 5)
        except OSError as e:
            return f"writing failed ({e})"
        self.block = blk
        self.hook = hook
        return None

    def set_enabled(self, mode):
        off, _ = DTF["enabled"]
        self.p.write(self.block + BLOCK_STATE + off, struct.pack("<i", mode))

    def stats(self):
        raw = self.p.read(self.block + BLOCK_STATE, DTF_SIZE)
        return {k: struct.unpack_from("<" + DTF[k][1], raw, DTF[k][0])[0] for k in ("frames", "capped", "dropped",
                                                                                    "repaid")}

    def read_ctl(self, path, last):
        """The test control file (as the Linux library's, tools/framerec.lua): "mode hitch_abs hitch_rel tau repay
        debt_max n", applied when it changes."""
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            return last
        if text == last:
            return last
        try:
            parts = text.split()
            mode, h, hr, tau, r, dm, n = parts[:7]
            vals = {"hitch_abs": float(h), "hitch_rel": float(hr), "tau": float(tau), "repay": float(r),
                    "debt_max": float(dm), "n": int(n), "top_n": int(parts[7]) if len(parts) > 7 else 16, "init": 0}
            for k, v in vals.items():
                off, fmt = DTF[k]
                self.p.write(self.block + BLOCK_STATE + off, struct.pack("<" + fmt, v))
            self.mode = int(mode)
        except (ValueError, OSError):
            pass
        return text

    def follow(self):
        """Until the game ends (or stop): the filter on while the level flag exists."""
        flag = os.path.join(self.user_dir, "temp", LEVEL_FLAG) if self.user_dir else None
        ctl = os.path.join(self.user_dir, "temp", CTL_FILE) if self.user_dir and \
            os.environ.get("BNGEARTH_DTSMOOTH_CTL") == "1" else None
        on, mode, last_ctl = None, None, None
        while not self.stop.is_set() and self.p.alive():
            if ctl:
                last_ctl = self.read_ctl(ctl, last_ctl)
            want = bool(flag and os.path.exists(flag))
            if want != on or (want and mode != self.mode):
                try:
                    self.set_enabled(self.mode if want else 0)
                except OSError:
                    break
                mode = self.mode
                if want:
                    report(self.user_dir, "applied: steady game speed (BeamNG.Earth level; launcher patch)", self.log)
                elif on is not None:
                    try:
                        s = self.stats()
                        report(self.user_dir, "stock: another level (last level: %d frames, %d capped, %.2f s dropped, "
                               "%.2f s repaid)" % (s["frames"], s["capped"], s["dropped"], s["repaid"]), self.log)
                    except OSError:
                        pass
                on = want
            self.stop.wait(FLAG_POLL_S)


def attach(user_dir, wait_s=600.0, log=None, block=True):
    """Windows: wait for the game process, patch it and follow the level flag (in a thread unless block). The
    DtSmoothWin, or None (the status file says why)."""
    if env_off():
        report(user_dir, "off (BNGEARTH_DTSMOOTH_OFF / BNGEARTH_DTSMOOTH=0)", log)
        return None
    if not env_on_windows():
        report(user_dir, "off on Windows until tested there (BNGEARTH_DTSMOOTH_WIN=1 turns it on)", log)
        return None
    t0 = time.monotonic()
    pid = None
    while time.monotonic() - t0 < wait_s:
        pid = game_pid()
        if pid:
            break
        time.sleep(1.0)
    if not pid:
        report(user_dir, "not applied: the game process was not found", log)
        return None
    # (the executable must be mapped and its loader done with relocations: a few seconds after the process shows up)
    time.sleep(3.0)
    try:
        proc = WinProcess(pid)
    except OSError as e:
        report(user_dir, f"not applied: {e}", log)
        return None
    d = DtSmoothWin(proc, user_dir, log)
    why = d.apply(env_params())
    if why:
        report(user_dir, f"not applied: {why}; stock", log)
        proc.close()
        return None
    report(user_dir, "ready: applied while the BeamNG.Earth level runs (launcher patch, block at %#x, call at %#x)"
           % (d.block, d.hook["site"]), log)
    if block:
        d.follow()
    else:
        threading.Thread(target=d.follow, name="dtsmooth-flag", daemon=True).start()
    return d


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scan", metavar="EXE", help="offline: find the hook in an executable file")
    ap.add_argument("--attach", action="store_true", help="Windows: patch the running game and follow the level flag")
    ap.add_argument("--user-dir", help="the game's user folder (the level flag and status file)")
    ap.add_argument("--wait", type=float, default=600.0)
    a = ap.parse_args(argv)
    if a.scan:
        hook, why = scan_file(a.scan)
        if not hook:
            print("not found:", why)
            return 1
        print({k: (hex(v) if k != "xr_off" else v) for k, v in hook.items()})
        return 0
    if a.attach:
        os.environ.setdefault("BNGEARTH_DTSMOOTH_WIN", "1")      # (asked for explicitly)
        d = attach(a.user_dir, a.wait)
        return 0 if d else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
