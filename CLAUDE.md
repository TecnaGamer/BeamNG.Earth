# Testing

- **Long runs.** Start any long run (tests, drivability, an eval build) as a background command and wait for its exit notification. Then read the end of its output.
  - Never wait by polling a file for a success line (`until grep ...`, a Monitor watching for "steps" or "ok"). A run that crashes never prints that line, and the loop waits for hours.
- **Limits.** Every run has a time limit, either its own (`--timeout`) or `timeout N cmd`. A run that ends with no result is a failure to read, not a wait.
- **Drivability.** `python server/tests/drivability.py [SITE ...]` (BNE_SHAPES=1) measures all sites in minutes.
  - Its last line is the summary. It exits 1 when a site failed, crashed, timed out or got worse.
  - The baseline (`server/tests/fixtures/drivability_baseline.json`) is main's measurement, and `_measured_on` names the commit. Compare a branch against it directly; don't re-measure main.
  - When main moves, rewrite the baseline in its own PR with `--update`, after checking each site that got worse.
- **Junction evals.** `server/evals/` is run by hand only, never by CI or `tools/run_tests.py`: `earth_eval.py --before-from <upstream capture> --out <dir>` draws each junction of `junctions.json` against upstream (source `eval_lock.sh` first: one build at a time), `junction_page.py` makes its page, and `python -m pytest server/evals/tests` tests the tools.
- **Linux native module.** Build `server/earthfast.abi3.so` against glibc 2.35 in WSL or Linux (never the host's glibc, never `--host-glibc`): clone into the Linux filesystem (not `/mnt/c`), have rustup stable, `pip install --user ziglang`, `cargo install --locked cargo-zigbuild`, cmake, build-essential and nasm, run `python3 tools/build_native.py` (about 7 min), then check `objdump -T server/earthfast.abi3.so | grep -o 'GLIBC_[0-9.]*' | sort -V | tail -1` is 2.35 or lower, `cd server && python3 -c "import earthfast; print(earthfast.VERSION)"`, and `PYTHONPATH=server python3 -m pytest server/tests/test_linenet.py -q`; delete the clone's `native/earthfast/target` afterwards (about 700 MB).
- **No ad-hoc copies.** Don't keep a copy of a test runner pinned to one worktree's path. Run the repository's own scripts from the worktree you are testing.
- **Clean up.** Kill your own background loops and processes before you stop.
