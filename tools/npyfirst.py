"""Imported first by the build tools (tools/build_*.py, bake_*.py, rebase*.py, profile_build.py): the server folder on
sys.path and server/npyguard.py applied, so numpy's AVX-512 kernels are held back before anything imports numpy, in the
tool's own process and the processes it starts (server/tests/test_npyguard.py checks the order)."""
import os
import sys

_SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _SERVER not in sys.path:
    sys.path.append(_SERVER)        # (appended: a tool's own sys.path order, set after this, is kept)
import npyguard  # noqa: E402,F401
