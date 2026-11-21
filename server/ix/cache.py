"""The artifacts' cache (SPEC.md): off unless BNE_IX_CACHE is set, and then written by the game, by pytest and by the
evals alike. Each step's artifact is keyed by what it was made from (key_of): its step and FORMAT, the laws it read,
its parents' keys and its inputs' bytes, so a change to one step changes its key and every later step's, and earlier
artifacts are reused as they are."""
import hashlib
import os
import tempfile

from . import artifacts

ROOT = os.environ.get("BNE_IX_CACHE") or None


def laws_digest(*modules_and_names):
    """A sha1 hex of the repr of the named constants: ("gradeshapes", ["JOIN_M", ...]), ...; the laws a step read."""
    import importlib
    h = hashlib.sha1()
    for name, names in modules_and_names:
        mod = importlib.import_module(str(name))
        for n in names:
            h.update(("%s.%s=%r" % (name, n, getattr(mod, n))).encode())
    return h.hexdigest()


def key_of(step, fmt, laws, parents, inputs_bytes):
    """sha1 hex over the schema, the step and its format, the laws digest, the sorted parents' keys and the inputs'
    bytes (each length-prefixed, so no boundary is ambiguous). Deterministic."""
    if isinstance(inputs_bytes, (bytes, bytearray, memoryview)):
        inputs_bytes = [inputs_bytes]
    h = hashlib.sha1()
    h.update(("ix schema %d step %d format %d laws %s" % (artifacts.SCHEMA, int(step), int(fmt), laws)).encode())
    for parent in sorted(parents):
        h.update((" parent %s" % parent).encode())
    for b in inputs_bytes:
        b = bytes(b)
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
    return h.hexdigest()


def path_of(step, key):
    """ROOT/s<step>/<key[:2]>/<key>.npz."""
    return os.path.join(ROOT, "s%d" % int(step), key[:2], key + ".npz")


def get(step, key):
    """The artifact at that key, or None (no cache, nothing there, or a broken file: never an exception for the game)."""
    if not ROOT:
        return None
    try:
        return artifacts.load(path_of(step, key))
    except Exception:
        return None


def put(artifact):
    """Write artifact under its meta's key: a temp file in the same directory, then os.replace (atomic, safe across
    processes). Nothing without ROOT."""
    if not ROOT:
        return None
    path = path_of(artifact.meta.step, artifact.meta.key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(artifacts.to_bytes(artifact))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
