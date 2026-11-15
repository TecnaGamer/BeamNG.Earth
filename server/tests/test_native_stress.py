"""The native crash reproducer must keep evidence and never report a failed run as passing."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "tools/stress_native.py"
SPEC = importlib.util.spec_from_file_location("native_stress", SCRIPT)
stress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stress)


class SupervisorTest(unittest.TestCase):
    def run_child(self, code, timeout=10):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name) / "run"
        command = [sys.executable, "-u", "-c", code, str(folder)]
        return stress.supervise(command, folder, timeout), folder

    def test_nonzero_exit_preserves_fatal_stderr(self):
        result, folder = self.run_child("import sys; print('fatal native marker',file=sys.stderr); sys.exit(7)")
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)
        self.assertIn("fatal native marker", (folder / "stderr.log").read_text())

    def test_success_without_result_is_a_failure(self):
        result, _ = self.run_child("pass")
        self.assertFalse(result["ok"])

    def test_corrupt_result_is_a_failure(self):
        result, _ = self.run_child("import sys,pathlib; (pathlib.Path(sys.argv[1])/'result.json').write_text('{')")
        self.assertFalse(result["ok"])

    def test_timeout_terminates_the_owned_child_and_preserves_its_log(self):
        result, folder = self.run_child(
            "import sys,time; print('entered native call',file=sys.stderr,flush=True); time.sleep(60)", timeout=2)
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertIn("entered native call", (folder / "stderr.log").read_text())

    def test_success_requires_an_explicit_completed_result(self):
        result, _ = self.run_child(
            "import sys,pathlib; (pathlib.Path(sys.argv[1])/'result.json').write_text('{\"ok\":true}')")
        self.assertTrue(result["ok"])

    def test_existing_output_is_refused_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            sentinel = path / "sentinel"
            sentinel.write_text("keep")
            with self.assertRaises(FileExistsError):
                stress.main(["--out", str(path)])
            self.assertEqual(sentinel.read_text(), "keep")
