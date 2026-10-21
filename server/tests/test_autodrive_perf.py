"""Portable drive telemetry: discovery, CPU intervals and Windows native API smoke coverage."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import autodrive_perf as ad  # noqa: E402


class PortableImportTest(unittest.TestCase):
    def test_import_does_not_need_posix_sysconf(self):
        spec = importlib.util.spec_from_file_location("autodrive_perf_portable_test", TOOLS / "autodrive_perf.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(os, "sysconf", side_effect=AssertionError("POSIX only"), create=True):
            spec.loader.exec_module(module)

    def test_help_needs_no_game_or_process_discovery(self):
        result = subprocess.run([sys.executable, str(TOOLS / "autodrive_perf.py"), "--help"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--server-pid", result.stdout)
        self.assertIn("--game-pid", result.stdout)


class ProcSamplerTest(unittest.TestCase):
    def sampler(self, procs, **kwargs):
        reader = SimpleNamespace(procs=Mock(return_value=procs), cpu_rss=Mock(return_value=(0.0, 10.0)))
        with patch.object(os, "name", "nt"), patch.object(ad, "WindowsProcs", return_value=reader) as factory:
            sampler = ad.ProcSampler(**kwargs)
        return sampler, reader, factory

    def test_detects_launcher_workers_game_and_packaged_server(self):
        # Descendants arrive out of order; renderer subprocesses and unrelated Python must stay out.
        sampler, _, _ = self.sampler({
            4: (3, "python.exe"),
            3: (2, "python.exe --multiprocessing-fork"),
            2: (1, r'"C:\Python\python.exe" "D:\BeamNG.Earth\launcher.py"'),
            8: (2, r'"C:\Games\BeamNG.drive\Bin64\BeamNG.drive.x64.exe" -userpath C:\Users\test'),
            9: (8, r'"C:\Games\BeamNG.drive.x64.exe" --type=renderer'),
            10: (1, r'"C:\Python\python.exe" "D:\other\launcher.py"'),
            11: (1, "BeamNG.Earth-1.5.3-beta.exe"),
            12: (11, "worker.exe"),
            13: (11, "BeamNG.drive.exe"),
            14: (13, "renderer.exe"),
        })
        self.assertEqual(sampler.groups(), {"server": [2, 3, 4, 11, 12], "game": [8, 13]})

    def test_explicit_pids_choose_one_session_and_include_workers(self):
        sampler, _, factory = self.sampler({2: (1, "python.exe"), 3: (2, "python.exe"),
                                            5: (1, "earthd.py"), 8: (1, "game.exe")},
                                           server_pid=2, game_pid=8)
        factory.assert_called_once_with(discover=False)
        self.assertEqual(sampler.groups(), {"server": [2, 3], "game": [8]})

    def test_absent_explicit_pid_does_not_fall_back_to_other_session(self):
        sampler, _, _ = self.sampler({5: (1, "earthd.py"), 8: (1, "BeamNG.drive.x64.exe")},
                                     server_pid=2, game_pid=3)
        self.assertEqual(sampler.groups(), {"server": [], "game": []})

    def test_explicit_launcher_root_excludes_selected_game_and_other_game_trees(self):
        sampler, _, _ = self.sampler({
            2: (1, "python.exe"),
            3: (2, "worker.exe"),
            8: (2, "custom_game.exe"),
            9: (8, "renderer.exe"),
            10: (2, "BeamNG.drive.x64.exe"),
            11: (10, "helper.exe"),
        }, server_pid=2, game_pid=8)
        self.assertEqual(sampler.groups(), {"server": [2, 3], "game": [8]})

    def test_linux_launcher_game_branch_is_excluded_from_server(self):
        sampler, _, _ = self.sampler({
            2: (1, "python3 /opt/BeamNG.Earth/launcher.py"),
            3: (2, "python3 /opt/BeamNG.Earth/launcher.py --server"),
            4: (3, "python3 --multiprocessing-fork"),
            5: (2, "/mnt/games/steamapps/common/BeamNG.drive/BinLinux/BeamNG.drive.x64 -console"),
            6: (5, "renderer"),
        })
        self.assertEqual(sampler.groups(), {"server": [2, 3, 4], "game": [5]})

    def test_linux_game_discovery_supports_other_steam_libraries(self):
        sampler, _, _ = self.sampler({})
        sampler.windows = None
        with patch.object(ad, "_procs", return_value={
            4: (1, "python3 /opt/BeamNG.Earth/server/earthd.py"),
            5: (1, "/mnt/games/steamapps/common/BeamNG.drive/BinLinux/BeamNG.drive.x64 -console"),
        }):
            self.assertEqual(sampler.groups(), {"server": [4], "game": [5]})

    def test_appimage_server_and_workers_are_discovered_outside_named_project(self):
        sampler, _, _ = self.sampler({})
        sampler.windows = None
        with patch.object(ad, "_procs", return_value={
            4: (1, "/tmp/.mount_earth/app.AppImage --server --port 19001"),
            5: (4, "python3 --multiprocessing-fork"),
            6: (1, "/tmp/.mount_earth/app.AppImage"),
        }):
            self.assertEqual(sampler.groups(), {"server": [4, 5], "game": []})

    def test_cpu_is_one_core_percent_and_unavailable_until_a_second_sample(self):
        sampler, reader, _ = self.sampler({2: (1, "earthd.py")})
        reader.cpu_rss.side_effect = [(10.0, 100.2), (None, None), (12.5, 102.6), (None, None)]
        with patch.object(ad.time, "monotonic", side_effect=[10.0, 12.0]):
            first, second = sampler.sample(), sampler.sample()
        self.assertEqual(first["server"], {"cpu": None, "rssMB": 100, "procs": 1})
        self.assertEqual(second["server"], {"cpu": 125.0, "rssMB": 103, "procs": 1})
        self.assertEqual(second["game"], {"cpu": None, "rssMB": None, "procs": 0})

    def test_worker_churn_and_failed_reads_reset_cpu_baseline(self):
        sampler, reader, _ = self.sampler({2: (1, "earthd.py")})
        sampler.groups = Mock(side_effect=[{"server": [2]}, {"server": [2, 3]}, {"server": [2, 3]},
                                           {"server": [2, 3]}, {"server": [2, 3]}])
        reader.cpu_rss.side_effect = [(10.0, 100), (500.0, 200), (None, None), (505.0, 200), (506.0, 200)]
        with patch.object(ad.time, "monotonic", side_effect=[1, 2, 3, 4, 5]):
            samples = [sampler.sample()["server"] for _ in range(5)]
        self.assertEqual([s["cpu"] for s in samples], [None, None, None, None, 100.0])
        self.assertIsNone(samples[2]["rssMB"])

    def test_cpu_summary_keeps_available_samples_after_a_process_read_fails(self):
        rows = [{"procs": {"server": {"cpu": 100, "rssMB": 123}, "game": {"cpu": 50, "rssMB": None}}},
                {"procs": {"server": {"cpu": None, "rssMB": None}}}, {"procs": None}]
        self.assertEqual(ad._cpu(rows), {"server": {"avg": 1.0, "max": 1.0, "rssMB": 123},
                                        "game": {"avg": 0.5, "max": 0.5, "rssMB": None}})


class WindowsDiscoveryTest(unittest.TestCase):
    def test_cim_singleton_and_empty_output(self):
        for output, expected in (("", {}), ('{"ProcessId":123,"CommandLine":"python earthd.py"}',
                                           {123: "python earthd.py"})):
            with self.subTest(output=output), patch.object(ad.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                    patch.object(ad.subprocess, "run", return_value=SimpleNamespace(stdout=output)) as run:
                self.assertEqual(ad.WindowsProcs._commands(), expected)
                self.assertIn("-NoProfile", run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_cim_failure_leaves_explicit_pid_sampling_available(self):
        with patch.object(ad.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(ad.subprocess, "run", side_effect=OSError("unavailable")), \
                patch("sys.stderr") as stderr:
            self.assertEqual(ad.WindowsProcs._commands(), {})
            self.assertIn("--server-pid", "".join(c.args[0] for c in stderr.write.call_args_list))

    @unittest.skipUnless(os.name == "nt", "Windows native API smoke test")
    def test_native_snapshot_and_memory_cpu_read_current_process(self):
        reader = ad.WindowsProcs(discover=False)
        self.assertIn(os.getpid(), reader.procs())
        cpu, rss = reader.cpu_rss([os.getpid()])
        self.assertGreaterEqual(cpu, 0)
        self.assertGreater(rss, 0)
        self.assertEqual(reader.cpu_rss([]), (None, None))
        self.assertEqual(reader.cpu_rss([0xFFFFFFFF]), (None, None))


if __name__ == "__main__":
    unittest.main()
