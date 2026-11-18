"""Suite-wide guards for pytest.

No test opens a page in the player's browser: os.startfile and webbrowser.open fail the test that reaches them unmocked
(a test that means to open a page patches them itself, which replaces the guard). On Windows updates.open_page opens a
page with os.startfile whatever popen it is handed, and the add-ons test that passed only popen opened someone/Some-Mod's
releases page in the browser on every run of the suite, again after its own stub reached main but not the branch the
suites were run on."""
import os
import webbrowser

import pytest


def _no_page(*args, **kwargs):
    raise AssertionError(f"a test opened a real page in the browser (mock it): {args!r}")


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setattr(os, "startfile", _no_page, raising=False)
    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, _no_page)
