"""Live weather sounds: process plumbing between the mod and native/weathersynth (the user, 2026-11-25: "You can
have a setting switch between live and pre-recorded").

The mod's 'weather_audio' (liveaudio.lua) goes, one JSON line each, to a `weathersynth live-serve` child that plays the
rain and the thunder out of the default audio device. Its state lines ({"state": "live", "latencyMs": ...} every
0.5 s, or {"state": "failed", "reason": ...}) come back to the mod as 'audio_state'. All synthesis, scheduling and
fading is in Rust; this file only starts, feeds and stops the process (glue, per the Rust-first rule).

- One child per server, owned by the connection that last asked for 'live'. 'prerecorded', the owner's disconnect
  or the server's end stop it.
- A missing binary is 'off' with a reason. A child that dies is 'failed' with its last stderr line. Neither is
  retried until the mod switches to Pre-recorded and back, or a new connection asks: no respawn loop.
- The child fades itself out when the lines stop for 1 s, so a hung game never leaves the rain playing.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading

log = logging.getLogger("liveaudio")

ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = "weathersynth.exe" if os.name == "nt" else "weathersynth"
# the fields passed on to the synth (protocol_table.json weather_audio; anything else is dropped)
FIELDS = ("mode", "rainMmH", "windMs", "shelter", "tunnel", "cabin", "volume", "thunderVolume", "muted", "duck", "t",
          "thunders")
THUNDER_FIELDS = ("id", "at", "d", "kind", "gain", "seed", "final")
STATE_FIELDS = ("state", "reason", "latencyMs")
MAX_THUNDERS = 32            # per message (a hint burst is a few)


def find_binary(env=os.environ):
    """The weathersynth executable: BNE_WEATHERSYNTH, next to the app (packaged), the source tree's release build,
    or PATH; None when there is none."""
    p = env.get("BNE_WEATHERSYNTH")
    if p:
        return p if os.path.isfile(p) else None
    for cand in (os.path.join(ROOT, EXE), os.path.join(ROOT, "bin", EXE),
                 os.path.join(ROOT, "native", "weathersynth", "target", "release", EXE)):
        if os.path.isfile(cand):
            return cand
    return shutil.which("weathersynth")


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
    return out


class LiveAudio:
    """call(fn, *args): run fn on the event loop (default: the owning connection's loop.call_soon_threadsafe);
    popen / binary: injectable for tests."""

    def __init__(self, call=None, binary=find_binary, popen=subprocess.Popen):
        self.call = call
        self.binary = binary
        self.popen = popen
        self.proc = None
        self.owner = None
        self.blocked = set()        # connections whose start failed (or found no binary): until they say prerecorded
        self.lock = threading.Lock()

    # (event loop) a connection's weather_audio
    def on_message(self, conn, msg):
        if msg.get("mode") != "live":
            self.blocked.discard(conn)
            if self.owner is conn or self.owner is None:
                self.stop()
                if msg.get("mode") == "prerecorded":
                    conn.send({"type": "audio_state", "state": "off"})
            return
        if conn in self.blocked:
            return
        if self.owner is not conn:
            self.stop()
        if self.proc is None and not self.start(conn):
            return
        self.write(clean(msg))

    # (event loop) a connection closed
    def on_close(self, conn):
        self.blocked.discard(conn)
        if self.owner is conn:
            self.stop()

    def start(self, conn):
        exe = self.binary() if callable(self.binary) else self.binary
        if not exe:
            self.blocked.add(conn)
            conn.send({"type": "audio_state", "state": "off", "reason": "no weathersynth binary in this build"})
            return False
        try:
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            proc = self.popen([exe, "live-serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, bufsize=0, env=child_env(), **kw)
        except OSError as e:
            self.blocked.add(conn)
            conn.send({"type": "audio_state", "state": "failed", "reason": f"could not start weathersynth: {e}"})
            return False
        self.proc, self.owner = proc, conn
        call = self.call or conn.loop.call_soon_threadsafe
        stderr_tail = []
        threading.Thread(target=self._stderr, args=(proc, stderr_tail), daemon=True, name="weathersynth-err").start()
        threading.Thread(target=self._stdout, args=(proc, conn, stderr_tail, call), daemon=True,
                         name="weathersynth-out").start()
        log.info("weathersynth live-serve started (%s)", exe)
        return True

    def write(self, obj):
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except (OSError, ValueError):
            pass                    # (it died: the stdout reader reports it)

    def stop(self):
        proc, self.proc, self.owner = self.proc, None, None
        if proc is None:
            return
        proc.stopping = True
        try:
            proc.stdin.close()      # (EOF: the synth fades out and exits)
        except (OSError, ValueError, AttributeError):
            pass
        def reap():
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        threading.Thread(target=reap, daemon=True, name="weathersynth-reap").start()

    def _stderr(self, proc, tail):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                log.info("weathersynth: %s", line)
                tail[:] = [line]
        proc.stderr.close()

    def _stdout(self, proc, conn, tail, call):
        for raw in iter(proc.stdout.readline, b""):
            m = state_message(raw.decode("utf-8", "replace"))
            if m is not None:
                call(self._forward, proc, conn, m)
        rc = proc.wait()
        proc.stdout.close()
        if not getattr(proc, "stopping", False):
            reason = tail[0] if tail else f"weathersynth exited ({rc})"
            call(self._died, proc, conn, reason)

    # (event loop)
    def _forward(self, proc, conn, m):
        if proc is self.proc and not getattr(conn, "closing", False):
            if m["state"] == "failed":
                self.blocked.add(conn)
            conn.send(m)

    def _died(self, proc, conn, reason):
        if proc is not self.proc:
            return
        self.proc, self.owner = None, None
        self.blocked.add(conn)
        if not getattr(conn, "closing", False):
            conn.send({"type": "audio_state", "state": "failed", "reason": reason[:200]})
