"""The mod's UI scripts must parse as ES modules, the way the game's UI loads them.

2026-11-21: an edit to earthSettingsStore.js put two new settings inside another entry. The game's UI failed to
transform the module, so the whole BeamNG.Earth menu tab and the map route disappeared, and it reached a dev build
because no test loaded the UI scripts. `node --check file.js` treats a .js file as CommonJS and did not catch it;
checking a copy named .mjs parses it as an ES module and does (verified on the broken version).
Skipped when node is not installed."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
UI = ROOT / "mod" / "beamng_earth" / "ui"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ModUiParseTest(unittest.TestCase):
    def test_every_ui_module_parses(self):
        scripts = sorted(p for p in UI.rglob("*.js") if "node_modules" not in p.parts)
        self.assertTrue(scripts, "no UI scripts found")
        bad = []
        with tempfile.TemporaryDirectory() as tmp:
            for p in scripts:
                copy = pathlib.Path(tmp, "m.mjs")
                copy.write_bytes(p.read_bytes())
                r = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
                if r.returncode != 0:
                    err = next((ln for ln in r.stderr.splitlines() if "Error" in ln), r.stderr.strip()[:200])
                    bad.append(f"{p.relative_to(ROOT)}: {err}")
        self.assertFalse(bad, "\n".join(bad))

    def test_the_check_catches_an_entry_inside_an_entry(self):
        broken = 'export const S = [\n  { id: "a", desc: "x",\n  { id: "b" },\n    note: 1 },\n];\n'
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp, "m.mjs")
            copy.write_text(broken)
            r = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
