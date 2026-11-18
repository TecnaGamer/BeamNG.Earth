"""numpy's AVX-512 kernels held back, so that an AVX-512 CPU (Zen 4 and newer, Intel's server and some desktop parts)
computes what an AVX2 CPU computes (determinism: every player's plans and tiles the same bits, GitHub #1169).

numpy picks some kernels by the CPU at import: on an AVX-512 machine its quicksort / argsort / partition take
x86-simd-sort's AVX-512 code, whose order of equal keys is another one (the PC's AVX2 argsort and the scalar one
already disagree on ties), and its transcendental functions take SVML routines. NPY_DISABLE_CPU_FEATURES, read when
numpy is first imported, turns those targets off; numpy then takes the AVX2 (X86_V3) kernels, as on every other
player's CPU. It must be in the environment before the process imports numpy: earthd.py applies it at its very top,
and the processes it starts inherit it.

Care: numpy refuses to start (RuntimeError) when a name in the variable is part of its compiled baseline, which a
build for -march=x86-64-v4 (a distribution's own numpy) has; and an unknown name is an ImportWarning (an error under
-W error). So: nothing set off x86 or on a CPU without AVX-512 (nothing to hold back there); the release builds'
numpy (a PyPI wheel, baseline X86_V2) by its version's names; any other numpy on an AVX-512 CPU asked in a short
subprocess which AVX-512 targets it dispatches outside its baseline."""
import os
import platform
import subprocess
import sys

ENV = "NPY_DISABLE_CPU_FEATURES"
# numpy >= 2.4 groups the x86 targets by microarchitecture level; older releases name each AVX-512 feature set
NAMES_2_4 = ("X86_V4", "AVX512_ICL", "AVX512_SPR")
NAMES_OLD = ("AVX512F", "AVX512CD", "AVX512_KNL", "AVX512_KNM", "AVX512_SKX", "AVX512_CLX", "AVX512_CNL", "AVX512_ICL",
             "AVX512_SPR")
_PROBE = ("import numpy._core._multiarray_umath as m\n"
          "b = set(m.__cpu_baseline__)\n"
          "print(' '.join(f for f in m.__cpu_dispatch__ if f.startswith(('X86_V4', 'AVX512', 'AVX10')) and f not in b))")


def _x86():
    return platform.machine().lower() in ("x86_64", "amd64", "x86", "i386", "i686", "x64")


def has_avx512():
    """True / False: whether this CPU has AVX-512 (F); None where it cannot be told cheaply."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if line.startswith("flags"):
                        return " avx512f" in line
            return None
        if sys.platform == "win32":
            import ctypes
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(41))   # PF_AVX512F_INSTRUCTIONS_AVAILABLE
        if sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "hw.optional.avx512f"], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() == "1"
    except Exception:
        pass
    return None


def _numpy_version():
    """(major, minor) of the numpy this process would import (without importing it), or None."""
    try:
        from importlib.metadata import version
        v = version("numpy")
    except Exception:
        return None
    try:
        major, minor = v.split(".")[:2]
        return int(major), int("".join(c for c in minor if c.isdigit()) or 0)
    except ValueError:
        return None


def _wheel_names():
    v = _numpy_version()
    return NAMES_OLD if v is not None and v < (2, 4) else NAMES_2_4


def _probe():
    """numpy's own list (a subprocess of this interpreter: the parent must not import numpy before the variable is
    set), or None when it cannot be asked."""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _PROBE], capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in os.environ.items() if k != ENV})
    except Exception:
        return None
    return tuple(r.stdout.split()) if r.returncode == 0 else None


def features():
    """The names to put in NPY_DISABLE_CPU_FEATURES here, () when there is nothing to hold back (or no safe way)."""
    if not _x86() or has_avx512() is False:
        return ()
    if getattr(sys, "frozen", False):       # (the packaged app: a PyPI wheel of numpy, baseline X86_V2)
        return _wheel_names()
    return _probe() or ()


def apply(env=None):
    """Set NPY_DISABLE_CPU_FEATURES in env (default os.environ) unless it is set already (inherited from the parent
    that set it, or the player's own); returns its value. Call before numpy is imported."""
    env = os.environ if env is None else env
    if ENV in env:
        return env[ENV]
    # (empty where nothing is held back: numpy reads that as no names, and the children need not ask again)
    env[ENV] = " ".join(features())
    return env[ENV]


def avx512_active():
    """The AVX-512 targets numpy dispatches in this process ([] when held back or absent). After numpy's import."""
    try:
        import numpy._core._multiarray_umath as m
    except ImportError:         # (numpy 1.x)
        import numpy.core._multiarray_umath as m
    return [f for f in m.__cpu_dispatch__ if f.startswith(("X86_V4", "AVX512", "AVX10")) and m.__cpu_features__.get(f)]
