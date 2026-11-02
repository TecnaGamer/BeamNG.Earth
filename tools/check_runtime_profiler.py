"""Offline profiler and integration checks. Uses the existing optional LuaJIT runtime."""
from pathlib import Path
import argparse
import os
import json
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lua-runtime', default=str(ROOT / 'build/lua-runtime'))
    args = parser.parse_args()
    os.chdir(ROOT)
    temp = ROOT / 'build/runtime-profiler/tmp'
    temp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temp)
    os.environ['TMP'] = os.environ['TEMP'] = str(temp)
    sys.path[:0] = [str(ROOT / "server"), str(ROOT / "server/tests")]
    import unittest
    names = ["test_runtimeprofiler", "test_runtimeprofiler_server", "test_ioprofiler", "test_netfetch", "test_cache", "test_pools", "test_selfupdate", "test_peerwork",
             "test_protocol_agreement.AgreementTableTest", "test_protocol_agreement.ConstantsTest", "test_protocol"]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    ok = result.wasSuccessful()
    report = {"python": {"run": result.testsRun, "failures": len(result.failures),
                         "errors": len(result.errors), "skipped": len(result.skipped)}, "lua": {}}
    for name in ("profiler", "profiler_view", "profiler_ui", "profiler_modes", "net_profile", "ioprofile_ui", "frameprofile", "settings", "core_stubbed", "core_robustness"):
        code = (f"import sys;sys.path.insert(0,{args.lua_runtime!r});from lupa.luajit21 import LuaRuntime;"
                f"LuaRuntime().execute(\"arg={{[0]='mod/tests/test_{name}.lua'}};dofile(arg[0])\")")
        proc = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        print(proc.stdout, end='')
        if proc.stderr: print(proc.stderr, file=sys.stderr, end='')
        counts = re.search(r'(\d+) passed, (\d+) failed', proc.stdout)
        report['lua'][name] = {"exit_code": proc.returncode,
                               "passed": int(counts[1]) if counts else None,
                               "failed": int(counts[2]) if counts else None}
        ok = ok and proc.returncode == 0
    report['native_game_acceptance'] = 'pending'
    report['status'] = 'passed' if ok else 'failed'
    (ROOT / 'build/runtime-profiler/verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
