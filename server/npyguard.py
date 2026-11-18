"""numpy's AVX-512 kernels held back, so that an AVX-512 CPU (Zen 4 and newer, Intel's server and some desktop parts)
computes what an AVX2 CPU computes (determinism: every player's plans and tiles the same bits, GitHub #1169).

numpy picks some kernels by the CPU at import: on an AVX-512 machine its quicksort / argsort / partition take
x86-simd-sort's AVX-512 code, whose order of equal keys is another one (the PC's AVX2 argsort and the scalar one
already disagree on ties), and its transcendental functions take SVML routines. NPY_DISABLE_CPU_FEATURES, read when
numpy is first imported, turns those targets off; numpy then takes the AVX2 (X86_V3) kernels, as on every other
player's CPU. It must be in the environment before the process imports numpy: importing this module sets it, and
every entry point (earthd.py, sharehelper.py, the bake and build tools, portcheck) imports it first; the processes they
start inherit it (tests/test_npyguard.py checks the entry points).

Care: numpy refuses to start (RuntimeError) when a name in the variable is part of its compiled baseline, which a
build for -march=x86-64-v4 (a distribution's own numpy) has; and an unknown name is an ImportWarning (an error under
-W error). So: nothing set off x86 or on a CPU without AVX-512 (nothing to hold back there); the packaged app the names
its build took from the numpy it bundled (tools/build_app.py, checked in the built executable: launcher.py
--check-numpy); any other numpy on an AVX-512 CPU asked in a child of this interpreter with this process's sys.path
which AVX-512 targets it dispatches outside its baseline, and used only when a second child imports numpy with them
under -W error and dispatches no AVX-512 kernel then."""
import os
import platform
import subprocess
import sys

ENV = "NPY_DISABLE_CPU_FEATURES"
# numpy >= 2.4 groups the x86 targets by microarchitecture level; older releases name each AVX-512 feature set
NAMES_2_4 = ("X86_V4", "AVX512_ICL", "AVX512_SPR")
NAMES_OLD = ("AVX512F", "AVX512CD", "AVX512_KNL", "AVX512_KNM", "AVX512_SKX", "AVX512_CLX", "AVX512_CNL", "AVX512_ICL",
             "AVX512_SPR")
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


FORCE_ENV = "BNGE_NPYGUARD_FORCE"       # "1": as on an AVX-512 CPU, whatever this one has (tests, the build's check)
FROZEN_FILE = "npyguard_frozen.json"    # the packaged app's names, made from its own numpy by tools/build_app.py


def frozen_manifest():
    """The packaged app's manifest ({"numpy": version, "names": [...]}, FROZEN_FILE beside the modules, written by
    tools/build_app.py from the numpy it bundled). Raises OSError / ValueError when it is missing or malformed."""
    import json
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), FROZEN_FILE), encoding="utf-8") as fh:
        doc = json.load(fh)
    if not (isinstance(doc, dict) and isinstance(doc.get("numpy"), str) and isinstance(doc.get("names"), list)
            and all(isinstance(n, str) for n in doc["names"])):
        raise ValueError(f"{FROZEN_FILE}: not a manifest of numpy's version and names")
    return doc


def _frozen_names():
    """The packaged app's names at run time: the manifest's (the release's build checked them: launcher.py
    --check-numpy, self_check); the PyPI wheels' names for the numpy version where it is missing (the fallback; the
    build's check never takes it)."""
    try:
        return tuple(frozen_manifest()["names"])
    except (OSError, ValueError):
        return _wheel_names()


_PROBE = ("import json, sys\n"
          "sys.path[:] = json.loads(sys.argv[1])\n"
          "import numpy._core._multiarray_umath as m\n"
          "b = set(m.__cpu_baseline__)\n"
          "print(' '.join(f for f in m.__cpu_dispatch__ if f.startswith(('X86_V4', 'AVX512', 'AVX10')) and f not in b))")
_CHECK = ("import json, sys, warnings\n"
          "sys.path[:] = json.loads(sys.argv[1])\n"
          "warnings.simplefilter('error')\n"
          "import numpy\n"
          "import npyguard\n"
          "print(json.dumps(npyguard.avx512_active()))")


def _child(code, names=None):
    """code run by this interpreter with this process's own sys.path and environment (ENV: names, or none): isolated
    (-I: not from the current folder, no PYTHON* variables, no user site folder), so the numpy it imports is the one this
    process imports. The CompletedProcess, or None when it cannot run."""
    import json
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if names is not None:
        env[ENV] = " ".join(names)
    try:
        return subprocess.run([sys.executable, "-I", "-c", code, json.dumps(sys.path)], capture_output=True,
                              text=True, timeout=60, env=env)
    except Exception:
        return None


def _probe():
    """The AVX-512 targets this process's numpy dispatches outside its baseline (asked in a child: this process must
    not import numpy before the variable is set), or None when it cannot be asked."""
    r = _child(_PROBE)
    return tuple(r.stdout.split()) if r is not None and r.returncode == 0 else None


def _validate(names):
    """numpy imports in a child with names set, warnings as errors, and dispatches no AVX-512 kernel then."""
    r = _child(_CHECK, names)
    return r is not None and r.returncode == 0 and r.stdout.strip() == "[]"


def features():
    """The names to put in NPY_DISABLE_CPU_FEATURES here, () when there is nothing to hold back (or no safe way)."""
    forced = os.environ.get(FORCE_ENV) == "1"
    if not _x86() or not forced and has_avx512() is False:
        return ()
    if getattr(sys, "frozen", False):       # (the packaged app: its build's own names, checked by the build)
        return _frozen_names()
    names = _probe()
    if not names or not _validate(names):   # (a numpy that would not start with them: dispatch as it is)
        return ()
    return names


def apply(env=None):
    """Set NPY_DISABLE_CPU_FEATURES in env (default os.environ) unless it is set already (inherited from the parent
    that set it, or the player's own); returns its value. Call before numpy is imported: importing this module does it
    for os.environ, so an entry point's first import is this module (tests/test_npyguard.py checks them)."""
    env = os.environ if env is None else env
    if ENV in env:
        return env[ENV]
    # (empty where nothing is held back: numpy reads that as no names, and the children need not ask again)
    env[ENV] = " ".join(features())
    return env[ENV]


def self_check():
    """(ok, message) in a process that has not imported numpy yet: this build's names set (whatever the CPU), numpy
    imported with warnings as errors; ok when it starts, the names are exactly numpy's AVX-512 dispatch targets outside
    its baseline (the full set: none missing, none extra) and no AVX-512 kernel dispatches. The packaged app: its
    manifest must be readable and of the numpy it bundles (version and names; no fallback). The release build's check
    (launcher.py --check-numpy)."""
    if "numpy" in sys.modules:
        return False, "numpy was imported before the check"
    want_version = None
    if getattr(sys, "frozen", False):
        try:
            doc = frozen_manifest()
        except (OSError, ValueError) as e:
            return False, f"no readable {FROZEN_FILE}: {type(e).__name__}: {e}"
        names, want_version = tuple(doc["names"]), doc["numpy"]
    else:
        names = _probe()
        if names is None:
            return False, "numpy could not be asked for its AVX-512 targets"
    os.environ[ENV] = " ".join(names)
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            import numpy
            m = _umath()
    except Exception as e:
        return False, f"numpy did not import with {ENV}={os.environ[ENV]!r}: {type(e).__name__}: {e}"
    if want_version is not None and want_version != numpy.__version__:
        return False, f"{FROZEN_FILE} is of numpy {want_version}, the app has {numpy.__version__}"
    base512 = [f for f in m.__cpu_baseline__ if f.startswith(("X86_V4", "AVX512", "AVX10"))]
    if base512:         # (a numpy built for x86-64-v4: it would not start on an AVX2 CPU at all)
        return False, f"numpy's baseline needs AVX-512: {base512}"
    full = dispatch_names()
    if sorted(names) != sorted(full):
        return False, f"names {sorted(names)} are not numpy's AVX-512 targets outside its baseline {sorted(full)}"
    bad = [n for n in names if n not in m.__cpu_dispatch__ or n in m.__cpu_baseline__]
    if bad:
        return False, f"not numpy's dispatch targets outside its baseline: {bad}"
    on = avx512_active()
    if on:
        return False, f"AVX-512 kernels still dispatched: {on}"
    return True, f"numpy {numpy.__version__}: {ENV}={os.environ[ENV]!r}"


def dispatch_names():
    """This process's numpy's AVX-512 targets outside its baseline (numpy imported here: tools/build_app.py's, the
    numpy it bundles; the names don't depend on the CPU or on what the variable holds back)."""
    m = _umath()
    base = set(m.__cpu_baseline__)
    return tuple(f for f in m.__cpu_dispatch__ if f.startswith(("X86_V4", "AVX512", "AVX10")) and f not in base)


def _umath():
    try:
        import numpy._core._multiarray_umath as m
    except ImportError:         # (numpy 1.x)
        import numpy.core._multiarray_umath as m
    return m


def avx512_active():
    """The AVX-512 targets numpy dispatches in this process ([] when held back or absent). After numpy's import."""
    m = _umath()
    return [f for f in m.__cpu_dispatch__ if f.startswith(("X86_V4", "AVX512", "AVX10")) and m.__cpu_features__.get(f)]


apply()       # (on import: an entry point imports this module first, before anything imports numpy)
