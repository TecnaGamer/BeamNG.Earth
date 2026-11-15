"""A failed module's Unicode log must not hide the test-runner summary."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('module_runner', Path(__file__).resolve().parents[2]/'tools/run_tests.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class LogOutput(unittest.TestCase):
    def test_non_ansi_failure_tail_is_preserved(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1252')
        with patch.object(runner.sys, 'stdout', stream):
            runner.Runner([], 1, 10, '.', {}).say('FAIL: bad \ufffd \u2192')
        self.assertIn(b'FAIL: bad \\ufffd \\u2192', raw.getvalue())

    def test_utf8_diagnostic_stays_readable(self):
        stream = io.StringIO()
        with patch.object(runner.sys, 'stdout', stream):
            runner.Runner([], 1, 10, '.', {}).say('FAIL: \u2192')
        self.assertIn('FAIL: \u2192', stream.getvalue())
