"""Every thread's stack of this process written to the server log in ONE piece (a slow road tile, a slow cell plan):
faulthandler writes to a file descriptor, line by line; several worker processes dumping at once onto the shared stderr
interleaved character by character into an unreadable log (2026-11-06). Dumped to a temporary file first, then logged
as one record."""
import logging
import os
import tempfile
import threading

log = logging.getLogger("earthd.stackdump")


def dump(what):
    """Log every thread's stack of this process under the heading `what` (one warning)."""
    import faulthandler
    try:
        with tempfile.TemporaryFile("w+") as fh:
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
            fh.seek(0)
            text = fh.read()
        log.warning("%s (process %d); its threads' stacks:\n%s", what, os.getpid(), text)
    except Exception:           # (diagnostics only)
        pass


def watch(seconds, what):
    """A timer that dumps (dump(what)) when not cancelled within `seconds`. Returns it (cancel() when done)."""
    t = threading.Timer(seconds, dump, args=(what,))
    t.daemon = True
    t.start()
    return t
