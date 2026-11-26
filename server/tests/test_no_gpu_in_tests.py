"""The suite runs without the GPU unless asked (conftest: BNGE_GPU=0 unless BNGE_TEST_GPU=1)."""
import os
import subprocess
import sys

import fast


def test_the_suite_holds_no_gpu_unless_it_is_the_gpus_own_run():
    if os.environ.get("BNGE_TEST_GPU") == "1":
        return
    assert os.environ.get("BNGE_GPU") == "0"
    assert fast.gpu() is None


def test_a_server_the_tests_start_inherits_it():
    out = subprocess.run([sys.executable, "-c", "import os; print(os.environ.get('BNGE_GPU'))"], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == ("0" if os.environ.get("BNGE_TEST_GPU") != "1" else os.environ.get("BNGE_GPU", "None"))
