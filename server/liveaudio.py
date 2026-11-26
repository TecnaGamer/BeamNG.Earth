"""Live weather sounds: process plumbing between the mod and native/weathersynth (the user, 2026-11-25: "You can
have a setting switch between live and pre-recorded").

The mod's 'weather_audio' (liveaudio.lua) goes, one JSON line each, to a `weathersynth live-serve` child that plays the
rain and the thunder out of the default audio device. Its state lines ({"state": "live", "latencyMs": ...} every
0.5 s, or {"state": "failed", "reason": ...}) come back to the mod as 'audio_state'. All synthesis, scheduling and
fading is in Rust; this file only starts, feeds and stops the process (glue, per the Rust-first rule).

- **Owner:** one child per server, owned by the connection that last asked for 'live'. The owner's disconnect or the
  server's end stop it. 'prerecorded' goes to the child like any line: it falls quiet and, with no 'live' line for
  its LINGER_S (60 s, serve.rs), ends by itself (exit 0); a switch back to Live before that keeps the same child (the
  2026-11-26 playtest's three starts in two minutes were Live / Pre-recorded / Live switches, each a new process and
  a new device open).
- **Never blocking:** the event loop never writes to the child. Its lines go into a small bounded queue that a writer
  thread drains. When the queue is full the oldest line is dropped, and its thunders ride on in the next one.
- **Missing binary:** 'off' with a reason, no retry.
- **Failed start:** an OSError (Windows Defender scanning a freshly unpacked exe), an exit, or a 'failed' line sends
  'failed' with the reason and retries after RETRY_S (5 s, then 30 s). After that the mod stays on its files until
  the setting goes Pre-recorded and back, or a new connection asks. A child live for LIVE_RESET_S resets the count.
- **Logging:** the child's stderr goes into the server log, rate-limited.
- **Hung game:** the child fades itself out when the lines stop for 1 s, so a hung game never leaves the rain playing.
"""
import collections
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time

log = logging.getLogger("liveaudio")

ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.dirname(os.path.abspath(__file__))
EXE = "weathersynth.exe" if os.name == "nt" else "weathersynth"
# the fields passed on to the synth (protocol_table.json weather_audio; anything else is dropped)
FIELDS = ("mode", "rainMmH", "windMs", "shelter", "tunnel", "cabin", "volume", "thunderVolume", "muted", "duck", "t",
          "thunderSet", "synthRain", "rainSet", "rainVol", "roofNear", "enclosed", "thunders")
# (play: a final the game plays from its file, the synth only ducks; dropped here before 2026-11-26, so the synth
# played those too, a second thunder up to LATE_MAX_S after the file's)
THUNDER_FIELDS = ("id", "at", "d", "kind", "gain", "seed", "final", "play")
STATE_FIELDS = ("state", "reason", "latencyMs", "thunderSets", "rainSets", "readyThunders")
MAX_THUNDERS = 32            # per message (a hint burst is a few)
QUEUE_MAX = 8                # lines waiting for the child (2 s of heartbeats)
RETRY_S = (5.0, 30.0)        # the waits before the retries after a failed start
LIVE_RESET_S = 60.0          # a child live this long before it stopped: its failure gets the retries again
STDERR_JOIN_S = 2.0          # after the child exits, the stderr reader gets this long to deliver its last lines
STOP_WAIT_S = 2.0            # a stopped child that has not exited by then is killed
LOG_LINES_PER_MIN = 20       # the child's stderr in the server log, at most this many lines a minute


def candidates():
    """Our own places for the binary: next to the app's modules (the packaged app's sys._MEIPASS, or the source
    tree's root: next to server/), bin/ there, server/ itself (a test tree), the source tree's release build."""
    out = [os.path.join(ROOT, EXE), os.path.join(ROOT, "bin", EXE), os.path.join(SERVER, EXE),
           os.path.join(ROOT, "native", "weathersynth", "target", "release", EXE)]
    return list(dict.fromkeys(out))


def find_binary(env=os.environ):
    """The weathersynth executable: BNE_WEATHERSYNTH, else our own places (candidates); None when there is none.
    Never PATH (on Windows that searches the current directory too)."""
    p = env.get("BNE_WEATHERSYNTH")
    if p:
        return p if os.path.isfile(p) else None
    for cand in candidates():
        if os.path.isfile(cand):
            return cand
    return None


def ensure_exec(path):
    """Our own bundled binary made executable when a copy lost the bit (a zip drops it): only for a path in
    candidates(), never one given by the environment."""
    if os.name == "nt" or path not in candidates():
        return
    try:
        mode = os.stat(path).st_mode
        if not mode & stat.S_IXUSR:
            os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            log.info("weathersynth: made %s executable", path)
    except OSError as e:
        log.warning("weathersynth: could not make %s executable: %s", path, e)


def child_env(env=os.environ, frozen=None):
    """The synth's environment: in a frozen Linux app (PyInstaller) the bundle's LD_LIBRARY_PATH would reach the child
    and could load the bundle's libraries before the system's (libasound and its ALSA plugins must be the player's
    own): PyInstaller's saved original back, or none."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    out = dict(env)
    if frozen and os.name != "nt":
        orig = out.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            out["LD_LIBRARY_PATH"] = orig
        else:
            out.pop("LD_LIBRARY_PATH", None)
    return out


def clean(msg):
    """The synth's line for a weather_audio message: the known fields only, the thunders' known keys only."""
    out = {k: msg[k] for k in FIELDS if k in msg and k != "thunders"}
    th = msg.get("thunders")
    if isinstance(th, list):
        out["thunders"] = [{k: t[k] for k in THUNDER_FIELDS if k in t} for t in th[:MAX_THUNDERS] if isinstance(t, dict)]
    return out


def state_message(line):
    """An 'audio_state' from one of the synth's stdout lines, or None (not a state line)."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("state"), str):
        return None
    out = {"type": "audio_state", "state": d["state"]}
    if isinstance(d.get("reason"), str):
        out["reason"] = d["reason"][:200]
    if isinstance(d.get("latencyMs"), (int, float)) and not isinstance(d.get("latencyMs"), bool):
        out["latencyMs"] = float(d["latencyMs"])
    if isinstance(d.get("thunderSets"), list):
        out["thunderSets"] = [v for v in d["thunderSets"][:4] if v in ("generated", "recorded")]
    if isinstance(d.get("rainSets"), list):
        out["rainSets"] = [v for v in d["rainSets"][:4] if v in ("generated", "recorded")]
    if isinstance(d.get("readyThunders"), list):
        out["readyThunders"] = [[v[0][:64], float(v[1])] for v in d["readyThunders"][:24]
                                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)
                                and isinstance(v[1], (int, float)) and not isinstance(v[1], bool)]
    return out


class LineQueue:
    """The lines for one child: bounded, never blocking the caller. A full queue drops its oldest line and carries that
    line's thunders into the newest (a final thunder is never lost to a slow child)."""

    def __init__(self, maxlen=QUEUE_MAX):
        self.items = collections.deque()
        self.maxlen = maxlen
        self.cv = threading.Condition()
        self.closed = False
        self.dropped = 0

    def put(self, obj):
        with self.cv:
            if self.closed:
                return
            if len(self.items) >= self.maxlen:
                old = self.items.popleft()
                self.dropped += 1
                carry = old.get("thunders") or []
                if carry:
                    obj = dict(obj)
                    obj["thunders"] = (carry + (obj.get("thunders") or []))[-MAX_THUNDERS:]
            self.items.append(obj)
            self.cv.notify()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify()

    def get(self):
        """The next line, or None once closed and empty."""
        with self.cv:
            while not self.items and not self.closed:
                self.cv.wait()
            return self.items.popleft() if self.items else None


class RateLog:
    """At most n lines a minute into the log, then one note of how many were left out."""

    def __init__(self, n=LOG_LINES_PER_MIN, clock=time.monotonic):
        self.n, self.clock = n, clock
        self.t0, self.count, self.skipped = None, 0, 0

    def line(self, text):
        now = self.clock()
        if self.t0 is None or now - self.t0 >= 60:
            if self.skipped:
                log.warning("weathersynth: %d more lines left out of the log", self.skipped)
            self.t0, self.count, self.skipped = now, 0, 0
        if self.count < self.n:
            self.count += 1
            log.warning("weathersynth: %s", text)
        else:
            self.skipped += 1


class LiveAudio:
    """call(fn, *args): run fn on the event loop (default: the owning connection's loop.call_soon_threadsafe);
    popen / binary / clock: injectable for tests."""

    def __init__(self, call=None, binary=find_binary, popen=subprocess.Popen, clock=time.monotonic):
        self.call = call
        self.binary = binary
        self.popen = popen
        self.clock = clock
        self.proc = None
        self.queue = None
        self.owner = None
        self.blocked = set()        # connections that stay on the files (no binary, retries used up)
        self.fails = {}             # connection -> failed starts in a row
        self.retry_at = {}          # connection -> when to try again (clock)

    # (event loop) a connection's weather_audio
    def on_message(self, conn, msg):
        if msg.get("mode") != "live":
            self.forget(conn)
            if self.owner is conn and self.proc is not None:
                self.queue.put(clean(msg))      # (quiet; it ends by itself after LINGER_S unless Live comes back)
            if self.owner is conn or self.owner is None:
                if msg.get("mode") == "prerecorded":
                    conn.send({"type": "audio_state", "state": "off"})
            return
        if conn in self.blocked:
            return
        if conn in self.retry_at:
            if self.clock() < self.retry_at[conn]:
                return
            del self.retry_at[conn]
        if self.owner is not conn:
            self.stop()
        if self.proc is None and not self.start(conn):
            return
        self.queue.put(clean(msg))

    # (event loop) a connection closed
    def on_close(self, conn):
        self.forget(conn)
        if self.owner is conn:
            self.stop()

    def forget(self, conn):
        self.blocked.discard(conn)
        self.fails.pop(conn, None)
        self.retry_at.pop(conn, None)

    # (event loop) a start that failed: retried after RETRY_S, then the files for good
    def failed(self, conn, reason):
        n = self.fails.get(conn, 0) + 1
        self.fails[conn] = n
        if n <= len(RETRY_S):
            wait = RETRY_S[n - 1]
            self.retry_at[conn] = self.clock() + wait
            reason = f"{reason} (trying again in {wait:.0f} s)"
        else:
            self.blocked.add(conn)
        log.warning("weathersynth: %s", reason)
        if not getattr(conn, "closing", False):
            conn.send({"type": "audio_state", "state": "failed", "reason": reason[:200]})

    def start(self, conn):
        exe = self.binary() if callable(self.binary) else self.binary
        if not exe:
            self.blocked.add(conn)
            conn.send({"type": "audio_state", "state": "off", "reason": "no weathersynth binary in this build"})
            return False
        ensure_exec(exe)
        try:
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            proc = self.popen([exe, "live-serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, bufsize=0, env=child_env(), **kw)
        except OSError as e:
            self.failed(conn, f"could not start weathersynth: {e}")
            return False
        q = LineQueue()
        self.proc, self.queue, self.owner = proc, q, conn
        call = self.call or conn.loop.call_soon_threadsafe
        stderr_tail = []
        err = threading.Thread(target=self._stderr, args=(proc, stderr_tail), daemon=True, name="weathersynth-err")
        for t in (err,
                  threading.Thread(target=self._stdout, args=(proc, conn, stderr_tail, call, err), daemon=True,
                                   name="weathersynth-out"),
                  threading.Thread(target=self._writer, args=(proc, q), daemon=True, name="weathersynth-in")):
            t.start()
        log.info("weathersynth live-serve started (%s)", exe)
        return True

    def stop(self):
        proc, q = self.proc, self.queue
        self.proc, self.queue, self.owner = None, None, None
        if proc is None:
            return
        proc.stopping = True
        q.close()                   # (the writer closes stdin once drained: EOF, the synth fades out and exits)

        def reap():
            try:
                proc.wait(timeout=STOP_WAIT_S)
            except Exception:
                proc.kill()         # (a child that stopped reading: the blocked writer gets a broken pipe and ends)
        threading.Thread(target=reap, daemon=True, name="weathersynth-reap").start()

    def _writer(self, proc, q):
        try:
            while True:
                obj = q.get()
                if obj is None:
                    break
                proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except (OSError, ValueError):
            pass                    # (it died: the stdout reader reports it)
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _stderr(self, proc, tail):
        rl = RateLog()
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                rl.line(line)
                tail[:] = [line]
        proc.stderr.close()

    def _stdout(self, proc, conn, tail, call, err=None):
        for raw in iter(proc.stdout.readline, b""):
            m = state_message(raw.decode("utf-8", "replace"))
            if m is not None:
                call(self._forward, proc, conn, m)
        rc = proc.wait()
        proc.stdout.close()
        if rc == 0 and not getattr(proc, "stopping", False):
            call(self._ended, proc)             # (its own end: no Live line for LINGER_S; a new Live starts another)
        elif not getattr(proc, "stopping", False):
            if err is not None:
                err.join(STDERR_JOIN_S)         # (its last stderr line may still be in the pipe: the reason must not race it)
            reason = tail[0] if tail else f"weathersynth exited ({rc})"
            call(self._died, proc, conn, reason)

    # (event loop)
    def _forward(self, proc, conn, m):
        if proc is not self.proc or getattr(conn, "closing", False):
            return
        if m["state"] == "live" and getattr(proc, "live_at", None) is None:
            proc.live_at = self.clock()
        if m["state"] == "failed":
            return                          # (the exit that follows reports it, with its retry)
        conn.send(m)

    def _ended(self, proc):
        if proc is self.proc:
            self.proc, self.queue, self.owner = None, None, None
            log.info("weathersynth live-serve ended by itself (no Live line for a while)")

    def _died(self, proc, conn, reason):
        if proc is not self.proc:
            return
        self.proc, self.queue, self.owner = None, None, None
        live_at = getattr(proc, "live_at", None)
        if live_at is not None and self.clock() - live_at >= LIVE_RESET_S:
            self.fails.pop(conn, None)      # (it worked for a while: this failure gets its retries again)
        self.failed(conn, reason)
