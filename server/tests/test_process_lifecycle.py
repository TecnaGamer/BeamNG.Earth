"""Real Windows process-tree cleanup, using only owned fixtures and ephemeral ports.

No game process is inspected or terminated. Every fixture has a safety lifetime;
cleanup retains process handles so PID reuse cannot target another process.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "server")]
# Windows venv python.exe is a redirector, not the process running the script.
# multiprocessing itself bypasses it; do the same for these parent/PID fixtures.
PYTHON = getattr(sys, "_base_executable", sys.executable)


def publish(root, name, value):
    path = root / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def fixture(role, directory, kind):
    root = Path(directory)
    # Even if the test process itself crashes, fixtures cannot remain indefinitely.
    timer = threading.Timer(45, lambda: os._exit(99))
    timer.daemon = True
    timer.start()
    command = [PYTHON, str(Path(__file__).resolve())]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if role == "--fixture-parent":
        env = dict(os.environ, BNGEARTH_PARENT=str(os.getpid()))
        child = subprocess.Popen(command + ["--fixture-guard", str(root), kind], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=flags)
        publish(root, "parent.json", {"pid": os.getpid(), "child": child.pid})
    elif role == "--fixture-guard":
        if kind == "launcher":
            import launcher
            launcher.watch_parent()
            subprocess.Popen(command + ["--fixture-leaf", str(root), kind],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
        else:
            import blockworker
            # Actual initializer and parent guard, with no geographic/network work.
            blockworker.init(str(root / "cache"), True, False, False, False, False, None)
    if role != "--fixture-parent":
        endpoint = socket.socket()
        endpoint.bind(("127.0.0.1", 0))
        endpoint.listen(1)
        name = "leaf.json" if role == "--fixture-leaf" else "guard.json"
        publish(root, name, {"pid": os.getpid(), "port": endpoint.getsockname()[1]})
    threading.Event().wait(45)


@unittest.skipUnless(os.name == "nt", "Windows process-handle and taskkill integration")
class ProcessLifecycleTest(unittest.TestCase):
    def setUp(self):
        import ctypes
        from ctypes import wintypes
        self.temp = tempfile.TemporaryDirectory(prefix="bnge-lifecycle-test-")
        self.root = Path(self.temp.name)
        self.handles, self.children = [], []
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.k32.WaitForSingleObject.restype = wintypes.DWORD
        self.k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.k32.TerminateProcess.restype = wintypes.BOOL
        self.k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.k32.CloseHandle.restype = wintypes.BOOL
        self.addCleanup(self.cleanup_fixtures)

    def cleanup_fixtures(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        # These handles were opened while our fixtures were alive. Never find or
        # kill processes by image name, and never reopen a possibly reused PID.
        for handle in self.handles:
            if self.k32.WaitForSingleObject(handle, 0) == 258:
                self.k32.TerminateProcess(handle, 98)
                self.k32.WaitForSingleObject(handle, 5000)
            self.k32.CloseHandle(handle)
        self.temp.cleanup()

    def start(self, kind, direct=False):
        env = dict(os.environ, BNGEARTH_PARENT=str(os.getpid()))
        env.pop("BNGE_WORLD_BUNDLE", None)
        env.pop("BNGE_TRACEMALLOC", None)
        env.pop("BNGE_SAMPLE", None)
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
            [p for p in sys.path if p and "site-packages" in p] +
            ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])))
        role = "--fixture-guard" if direct else "--fixture-parent"
        child = subprocess.Popen([PYTHON, str(Path(__file__).resolve()), role,
                                  str(self.root), kind], cwd=ROOT, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(child)
        return child

    def ready(self, name):
        path = self.root / name
        deadline = time.monotonic() + 15
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), f"fixture did not become ready: {name}")
        state = json.loads(path.read_text(encoding="utf-8"))
        handle = self.k32.OpenProcess(0x00100001, False, state["pid"])  # SYNCHRONIZE | TERMINATE
        self.assertTrue(handle, f"fixture already exited: {name}")
        self.handles.append(handle)
        self.assertEqual(self.k32.WaitForSingleObject(handle, 0), 258, "fixture must initially be alive")
        return state, handle

    def exited_and_reusable(self, state, handle):
        self.assertEqual(self.k32.WaitForSingleObject(handle, 8000), 0,
                         f"fixture {state['pid']} survived its parent/stop request")
        with socket.socket() as endpoint:
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            endpoint.bind(("127.0.0.1", state["port"]))
            endpoint.listen(1)

    def test_launcher_parent_exit_stops_guard_and_descendant(self):
        parent = self.start("launcher")
        guard, guard_handle = self.ready("guard.json")
        leaf, leaf_handle = self.ready("leaf.json")
        parent.kill()
        parent.wait(timeout=5)
        self.exited_and_reusable(guard, guard_handle)
        self.exited_and_reusable(leaf, leaf_handle)

    def test_worker_initializer_guard_exits_with_server(self):
        parent = self.start("worker")
        worker, handle = self.ready("guard.json")
        parent.kill()
        parent.wait(timeout=5)
        self.exited_and_reusable(worker, handle)

    def test_explicit_stop_reaps_owned_server_tree(self):
        import launcher
        server = self.start("launcher", direct=True)
        guard, guard_handle = self.ready("guard.json")
        leaf, leaf_handle = self.ready("leaf.json")
        launcher.stop_server(server)
        self.exited_and_reusable(guard, guard_handle)
        self.exited_and_reusable(leaf, leaf_handle)
        self.assertIsNotNone(server.poll())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1].startswith("--fixture-"):
        fixture(*sys.argv[1:])
    else:
        unittest.main()
