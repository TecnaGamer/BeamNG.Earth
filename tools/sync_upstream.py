"""Bring the Finnish fork up to date with TecnaGamer's BeamNG.Earth (the Finnish fork): fetch upstream's main, merge
it into the finland branch, run the tests that cover the fork's changes, push to the fork. Stops before pushing on a
merge conflict or a failing test, saying what to look at.

    python tools/sync_upstream.py            (merge, test, push)
    python tools/sync_upstream.py --no-push  (merge and test only)

The same runs on GitHub every few hours (.github/workflows/sync-upstream.yml), which also builds the release.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSTREAM_URL = "https://github.com/TecnaGamer/BeamNG.Earth.git"
UPSTREAM, BRANCH, FORK = "upstream", "finland", "fork"
# the modules that cover the fork's changes (roads, grading, bridges, signs); the full suite takes ~10 minutes
TESTS = ("test_grading", "test_roads", "test_roadmesh", "test_approaches", "test_tunnels", "test_furniture",
         "test_sidewalk_collision", "test_bridgetypes")


def git(*args, check=True, capture=False):
    r = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=capture)
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed{': ' + r.stderr.strip() if capture else ''}")
    return r.stdout.strip() if capture else r.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-push", action="store_true", help="merge and test, but do not push to the fork")
    args = ap.parse_args()

    if git("status", "--porcelain", capture=True):
        sys.exit("Uncommitted changes: commit or stash them first.")
    if git("branch", "--show-current", capture=True) != BRANCH:
        sys.exit(f"Switch to the {BRANCH} branch first: git switch {BRANCH}")
    if UPSTREAM not in git("remote", capture=True).split():
        git("remote", "add", UPSTREAM, UPSTREAM_URL)
    git("fetch", "--quiet", UPSTREAM, "main")
    behind = git("rev-list", "--count", f"HEAD..{UPSTREAM}/main", capture=True)
    if behind == "0":
        print("Already up to date with upstream.")
        return
    print(f"Merging {behind} upstream commit(s) ...")
    if git("merge", "--no-edit", f"{UPSTREAM}/main", check=False):
        files = git("diff", "--name-only", "--diff-filter=U", capture=True)
        git("merge", "--abort", check=False)
        sys.exit("Merge conflict in:\n  " + "\n  ".join(files.splitlines())
                 + "\nNothing was changed. Merge by hand (git merge upstream/main) or ask for help with these files.")

    venv = os.path.join(ROOT, ".venv", "Scripts", "python.exe") if os.name == "nt" else \
        os.path.join(ROOT, ".venv", "bin", "python")
    py = venv if os.path.exists(venv) else sys.executable
    print("Running the tests ...")
    if subprocess.run([py, os.path.join("tools", "run_tests.py"), *TESTS], cwd=ROOT).returncode:
        sys.exit("Tests failed after the merge: it is committed here but not pushed. Look at the failures; "
                 "git reset --hard ORIG_HEAD undoes the merge.")
    if args.no_push:
        print("Merged and tested; not pushed (--no-push).")
        return
    git("push", FORK, BRANCH)
    print(f"Merged, tested and pushed to {FORK}/{BRANCH}. Restart run_server.bat to use it.")


if __name__ == "__main__":
    main()
