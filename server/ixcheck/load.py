"""Reads a run's cached artifacts (BNE_IX_CACHE's layout s<step>/<xx>/<key>.npz) into memory for the checks."""
import os

import ix.artifacts as artifacts


def scan(root):
    """{"steps": {step: [artifact, ...]}, "by_key": {key: artifact}, "errors": [(path, why)]}: every .npz under root,
    its records rebuilt. A file that will not load is named in errors, never raised: a cache the checks read is not
    theirs to fail on."""
    steps, by_key, errors = {}, {}, []
    for dirpath, dirnames, filenames in os.walk(os.fspath(root)):
        dirnames.sort()
        for f in sorted(filenames):
            if not f.endswith(".npz"):
                continue
            path = os.path.join(dirpath, f)
            try:
                art = artifacts.load(path)
                step = int(art.meta.step)
            except Exception as e:                   # a foreign or stale file: reported, not fatal
                errors.append((path, "%s: %s" % (type(e).__name__, e)))
                continue
            key = f[:-len(".npz")]
            by_key[key] = art
            steps.setdefault(step, []).append(art)
    for arts in steps.values():
        arts.sort(key=lambda a: str(a.meta.key))     # the report's order is the cache's, not the disk's
    return {"steps": steps, "by_key": by_key, "errors": errors}


def parents_of(art, scanned):
    """The artifact's parents and theirs, one per step (the nearest generation wins): the checks get their context by
    step, whatever chain of keys the run left."""
    out, seen, queue = {}, set(), list(art.meta.parents)
    while queue:
        key = queue.pop(0)
        if key in seen:
            continue
        seen.add(key)
        p = scanned["by_key"].get(key)
        if p is None:                                # a pruned or partial cache: the checks that need it skip themselves
            continue
        out.setdefault(p.meta.step, p)
        queue.extend(p.meta.parents)
    return out
