"""build_contributors.py [REV]: the GitHub users whose pull requests are merged into REV (default HEAD), for the mod's
About page (mod/.../ui-vue/mods/beamngEarth/contributors.js). Run before a release; needs the gh CLI (read-only calls).

A pull request counts when the history names it as merged or ported: a merge commit's subject naming #N ("Merge PR #779
(...)", "Merge pull request #12 from ...", "Merge the reviewed PRs: #673 #643 ..."), or a commit taking its code ("PR
#471 port", "ported from PR #363 by ...", "from PR #711 by ..."). Each number is looked up on GitHub: issues (a merge
subject's "GitHub #602") and the repository owner's own pull requests drop out. A merged branch named for its pull
request counts too ("Merge branch 'pr720'").

The merged commits' own authors count as well (a pull request opened by one person can carry another's commits: #720's
are cullenwerks'): each author's GitHub account, as GitHub links one of their commits (an address linked to none
is left out: a name alone could be anyone's). The lookups are kept in .claude/work/contributors_cache.json, so a
rerun asks GitHub only for new numbers and authors."""
import json
import os
import re
import subprocess
import sys

REPO = "TecnaGamer/BeamNG.Earth"
OWNER = "TecnaGamer"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "mod", "beamng_earth", "ui", "ui-vue", "mods", "beamngEarth", "contributors.js")
CACHE = os.path.join(ROOT, ".claude", "work", "contributors_cache.json")

TAKEN = (re.compile(r"\bPR #(\d+) port\b"), re.compile(r"\bfrom PR #(\d+)"), re.compile(r"#(\d+) port\b"))


def pr_numbers(rev):
    subjects = subprocess.run(["git", "log", "--format=%s", rev], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.splitlines()
    out = set()
    for s in subjects:
        if s.startswith("Merge"):
            out.update(int(n) for n in re.findall(r"#(\d+)", s))
            out.update(int(n) for n in re.findall(r"\bpr[-_]?(\d+)\b", s, re.I))     # ("Merge branch 'pr720'")
        for rx in TAKEN:
            out.update(int(n) for n in rx.findall(s))
    return sorted(out)


SKIP_EMAILS = ("noreply@anthropic.com",)       # (an AI co-author's commits: the person who merged them is listed)
NOT_PEOPLE = ("invalid-email-address",)       # (GitHub's placeholder for a commit made with "your@email.com")


def commit_authors(rev):
    """{email: (one commit sha, author name)} of REV's non-merge commits."""
    out = {}
    log = subprocess.run(["git", "log", "--no-merges", "--format=%H%x09%ae%x09%an", rev], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    for line in log:
        sha, email, name = (line.split("\t") + ["", ""])[:3]
        if email and email.lower() not in SKIP_EMAILS:
            out.setdefault(email.lower(), (sha, name))
    return out


def author_login(sha):
    """The GitHub account GitHub links a commit's author address to (None when it links none: an author name alone
    could be anyone's account -- "rdss@localhost" matched a stranger named RDSS, 2026-11-05)."""
    r = subprocess.run(["gh", "api", f"repos/{REPO}/commits/{sha}", "--jq", ".author.login // \"\""],
                       capture_output=True, text=True)
    login = r.stdout.strip() if r.returncode == 0 else ""
    return login if login and login not in NOT_PEOPLE else None


def lookup(n):
    """(login, is_pr) of number n on GitHub; (None, False) for an issue or a number that is gone."""
    r = subprocess.run(["gh", "api", f"repos/{REPO}/pulls/{n}", "--jq", ".user.login"], capture_output=True, text=True)
    if r.returncode != 0:
        return None, False
    return r.stdout.strip() or None, True


def main():
    rev = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}
    by = {}
    for n in pr_numbers(rev):
        k = str(n)
        if k not in cache:
            login, is_pr = lookup(n)
            cache[k] = {"login": login, "pr": is_pr}
        c = cache[k]
        login = c.get("login")
        if not c.get("pr") or not login or login == OWNER or login.endswith("[bot]"):
            continue
        by.setdefault(login, []).append(n)
    for email, (sha, _name) in commit_authors(rev).items():
        k = "email2:" + email
        if k not in cache:
            cache[k] = {"login": author_login(sha)}
        login = cache[k].get("login")
        if login and login != OWNER and not login.endswith("[bot]") and login.lower() != "claude":
            by.setdefault(login, [])
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)
    rows = sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    body = ",\n".join(f'  {{ login: {json.dumps(login)}, prs: {json.dumps(sorted(prs))} }}' for login, prs in rows)
    with open(OUT, "w", newline="\n") as f:
        f.write("// The GitHub users whose pull requests are merged into this version (tools/build_contributors.py writes\n"
                "// this file: rerun it before a release). Most merged pull requests first.\n"
                f"export const CONTRIBUTORS = [\n{body}\n]\n")
    print(f"{len(rows)} contributors, {sum(len(p) for _l, p in rows)} pull requests -> {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
