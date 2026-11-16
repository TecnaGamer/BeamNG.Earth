"""The mod UI's engineLua calls without a callback are Lua chunks (the game's bridge, BeamNGAPI.engineLua, sends the
text as is; only with a callback does it wrap it as an expression): each must compile as a statement. A bare
"a and a.f() or nil" there was the 3D map button's "fatal Lua error" (2026-11-08), as an add-on's page the day before."""
import os
import re
import shutil
import subprocess
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                  "mod", "beamng_earth", "ui")
# engineLua("...") / engineLua(`...`) closed straight after the string: no callback
CALL = re.compile(r'engineLua\(\s*(?P<q>["`])(?P<code>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)', re.S)


def no_callback_calls():
    for root, _, files in os.walk(UI):
        for f in files:
            if not f.endswith((".vue", ".js")) or ".min." in f:
                continue
            p = os.path.join(root, f)
            with open(p, encoding="utf-8", errors="replace") as fh:
                s = fh.read()
            for m in CALL.finditer(s):
                code = m.group("code")
                if m.group("q") == "`":         # template literal: its ${...} parts stand in as a call (a value, or a field call)
                    code = re.sub(r"\$\{[^}]*\}", "x()", code)
                yield os.path.relpath(p, UI), s[:m.start()].count("\n") + 1, code


@unittest.skipUnless(shutil.which("luajit"), "luajit not installed")
class NoCallbackCallsCompile(unittest.TestCase):
    def test_statements(self):
        calls = list(no_callback_calls())
        self.assertGreater(len(calls), 20)
        script = "local bad = {}\n" + "".join(
            f"if not loadstring({code!r}) then bad[#bad + 1] = {f'{f}:{n}'!r} end\n"
            for f, n, code in calls) + "print(table.concat(bad, '\\n'))\n"
        out = subprocess.run(["luajit", "-"], input=script, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "", "engineLua calls without a callback that are not Lua statements:\n"
                         + out.stdout)


if __name__ == "__main__":
    unittest.main()
