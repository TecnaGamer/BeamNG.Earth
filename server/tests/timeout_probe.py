"""Tests that hang on purpose: test_timeouts.py runs them in a subprocess (their name does not start with test_, so
nothing collects them otherwise). The limits come from the environment the subprocess is given."""
import signal
import time

import pytest


def test_ok_before():
    pass


def test_hangs_in_a_sleep():
    time.sleep(600)


def test_swallows_exceptions():
    while True:
        try:
            time.sleep(600)
        except Exception:
            pass


def test_ignores_the_interrupt():
    """The main thread cannot be reached: the process is killed after the grace."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    time.sleep(600)


@pytest.mark.max_seconds(1)
def test_a_marker_never_lowers_the_limit():
    time.sleep(4)


@pytest.mark.max_seconds(30)
def test_a_marker_raises_the_limit():
    time.sleep(3)


def test_ok_after():
    pass
