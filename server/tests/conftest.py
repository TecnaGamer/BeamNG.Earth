"""Suite-wide guards for pytest.

No test opens a page in the player's browser: os.startfile and webbrowser.open fail the test that reaches them unmocked
(a test that means to open a page patches them itself, which replaces the guard). On Windows updates.open_page opens a
page with os.startfile whatever popen it is handed, and the add-ons test that passed only popen opened someone/Some-Mod's
releases page in the browser on every run of the suite, again after its own stub reached main but not the branch the
suites were run on.

No test holds the GPU unless it is the GPU's own test run: BNGE_GPU=0 for the suite (set here, before any test imports
fast or earthfast), BNGE_TEST_GPU=1 to run with it (one process, no xdist: `BNGE_TEST_GPU=1 python -m pytest
tests/test_colormatch.py -k gpu`). Every xdist worker that imported test_colormatch made a wgpu device on the NVIDIA
driver (its skipUnless(fast.gpu()) runs at collection; bc7 and colormatch encode there too), and with it OBS's Vulkan
layer was loaded. Two workers of a suite stopped by a timeout on 2026-11-18 were left orphaned with a device open and
stuck in their exit for 13 hours holding 10.4 GB, memory Windows could not give back without a reboot; the evals
that ran meanwhile died of "memory allocation of N bytes failed".

Every test has a time limit (bnge_timeouts.py): one that hangs fails with every thread's stack, and the run goes on."""
import os
import webbrowser

import pytest

from bnge_timeouts import (pytest_configure, pytest_runtest_logreport,       # noqa: F401  (the hooks)
                           pytest_runtest_protocol)

if os.environ.get("BNGE_TEST_GPU") != "1":
    os.environ["BNGE_GPU"] = "0"


def _no_page(*args, **kwargs):
    raise AssertionError(f"a test opened a real page in the browser (mock it): {args!r}")


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setattr(os, "startfile", _no_page, raising=False)
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, _no_page)


@pytest.fixture(autouse=True)
def _no_interchanges_remembered():
    """What the process remembers of the road network is cleared before each test: a test that asks for it meets an
    earlier test's otherwise."""
    import linenet
    linenet.reset()             # (the process's line network: an earlier test's lines)


def _store_kept():
    """The process's plan store (grading._ACTIVE_STORE, the last one made: the tracks', decks' and tunnels' heights
    are read from it) as it was before, handed back after: a test, class or module that made one leaves none behind.
    An earlier test's store answered a later test's rails, which it never planned (strict: SolveMissing, #118)."""
    import grading
    was = grading._ACTIVE_STORE
    yield
    grading._ACTIVE_STORE = was


_no_plan_store_remembered = pytest.fixture(autouse=True)(_store_kept)
_no_plan_store_remembered_by_class = pytest.fixture(autouse=True, scope="class")(_store_kept)
_no_plan_store_remembered_by_module = pytest.fixture(autouse=True, scope="module")(_store_kept)
