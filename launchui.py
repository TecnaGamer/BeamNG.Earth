"""The launch screen's look: colours, a banner and timed steps for the scripts the player runs
(start.py, install.py; the packaged app's console could adopt them too).

ANSI colours when the console supports them (Windows 10+'s virtual-terminal mode, enabled for the
console this process owns; Windows Terminal and most Linux terminals already have them) and plain
text everywhere else: a redirected or captured output, NO_COLOR, a console that refuses the mode.
The step glyphs fall back to ASCII ("> ", "ok", "x") when the console's encoding cannot show them
(a Windows console at its code page), so nothing ever raises UnicodeEncodeError.

    ui = launchui.Screen()
    ui.banner("BeamNG.Earth 1.4.35-beta", "world server",
              rows=[("Game folder", user_dir), ("Telemetry", "on")])
    with ui.step("Preparing the mod") as st:
        ...                                  # its own output shows below the step's line
    st.done("up to date")                    # or, in the with's body, st.note("...")

Every step prints

    > Preparing the mod
      ok up to date (0.34 s)

and the times add up to the total the summary shows (Screen.summary). Standard library only.
"""
import ctypes
import os
import shutil
import sys
import time

# --- colours -------------------------------------------------------------------------------------

ENABLED = False


def enable():
    """Turn colours on for the console this process writes to, when it can show them. False when
    not (a redirected or captured output, NO_COLOR, a terminal without support)."""
    global ENABLED
    ENABLED = False                       # (a second call starts from a clean state)
    if os.environ.get("NO_COLOR"):
        return False
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return False
    if os.name == "nt":
        ENABLED = _windows_virtual_terminal()
    else:
        ENABLED = os.environ.get("TERM", "") not in ("", "dumb")
    return ENABLED


def _windows_virtual_terminal():
    """Windows: ENABLE_VIRTUAL_TERMINAL_PROCESSING on the console's output buffers (the classic
    conhost needs it; Windows Terminal has it on already and the call is harmless there)."""
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:                # (not Windows after all: Wine reported os.name "nt"? no)
        return False
    out = -11                             # STD_OUTPUT_HANDLE
    err = -12
    ok = False
    for handle in (out, err):
        h = kernel32.GetStdHandle(handle)
        if h in (None, 0, -1):
            continue
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            continue
        if kernel32.SetConsoleMode(h, mode.value | 0x0004):     # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            ok = True
    return ok


def _encodes(text):
    """The console takes this character (its code page does not drop it to '?', a Windows console
    at cp1252 with a box character)."""
    enc = getattr(sys.stdout, "encoding", None)
    if not enc:
        return False
    try:
        text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# The glyphs, chosen when the console's encoding takes them (a Windows console at its code page
# cannot show these: their ASCII fallbacks).
GLYPHS = {"sep": "\u2500", "bullet": "\u00b7", "step": "\u25b8", "ok": "\u2713", "bad": "\u2717",
          "ascii": {"sep": "-", "bullet": "*", "step": ">", "ok": "ok", "bad": "x"}}


def _glyph(name):
    g = GLYPHS[name]
    return g if _encodes(g) else GLYPHS["ascii"][name]


class Color:
    title = "\x1b[1;36m"       # bright cyan
    dim = "\x1b[2m"
    ok = "\x1b[32m"            # green
    warn = "\x1b[33m"          # yellow
    bad = "\x1b[31m"           # red
    reset = "\x1b[0m"


def paint(text, color):
    """text in color when the console shows colours, else as it is."""
    return f"{color}{text}{Color.reset}" if ENABLED else text


# --- the screen ----------------------------------------------------------------------------------

_WIDTH = shutil.get_terminal_size((78, 24)).columns if sys.stdout.isatty() else 78


def _say(msg=""):
    print(msg, flush=True)


class Screen:
    """The launch screen: a banner, steps with their times and a total."""

    def __init__(self, out=_say):
        enable()
        self.out = out
        self.t0 = time.time()
        self.spent = []               # every step's seconds (the summary's total)

    # banner

    def banner(self, title, subtitle="", rows=(), tips=()):
        """The header block:

           BeamNG.Earth 1.4.35-beta ─ world server
             Game folder  C:\\...\\current
             Telemetry    on (anonymous; BNE_TELEMETRY=off turns it off)
           ...
        """
        sep = _glyph("sep")
        head = f"{title} {sep} {subtitle}" if subtitle else title
        self.out("")
        self.out("  " + paint(head, Color.title))
        self.out("  " + paint(sep * min(len(head), _WIDTH - 4), Color.dim))
        for label, value in rows:
            if value is None:
                continue
            self.out(f"  {paint(label.ljust(13), Color.dim)} {value}")
        for tip in tips:
            self.out(f"  {paint(_glyph('bullet'), Color.dim)} {paint(tip, Color.dim)}")
        self.out("")

    # steps

    def step(self, title):
        """with ui.step("Preparing the mod") as st: ... ; st.done(status) ends it:

           > Preparing the mod
             ok up to date (0.34 s)
        """
        return Step(self, title)

    def _step_line(self, title):
        glyph = "> " if not _encodes("\u25b8") else "\u25b8 "
        self.out(f"{paint(glyph, Color.dim)} {title}")

    def _step_result(self, ok, status, secs):
        glyph = "x " if (not ok and not _encodes("\u2717")) else ("\u2717 " if not ok else "")
        if ok:
            glyph = "ok " if not _encodes("\u2713") else "\u2713 "
        color = Color.ok if ok else Color.bad
        self.out(f"  {paint(glyph + status, color)}{paint(f' ({secs:.1f} s)', Color.dim)}")
        self.spent.append(secs)

    def warn(self, text):
        self.out("  " + paint(text, Color.warn))

    def hint(self, text):
        self.out(f"  {paint(_glyph('bullet'), Color.dim)} {text}")

    def note(self, text=""):
        self.out(text)

    def summary(self, what="ready"):
        """The launch's total, after the last step."""
        total = sum(self.spent)
        if self.spent:
            self.out("")
            self.out(f"  {paint(what, Color.dim)}{paint(f' in {total:.1f} s', Color.dim)}")


class Step:
    """One step of the launch, its line printed when the with begins, its result when it ends.
    st.done(status) / st.skip(status) inside the body end it there (an exception prints a red
    result and is raised again)."""

    def __init__(self, screen, title):
        self.screen = screen
        self.title = title
        self.t0 = time.time()
        self._finished = False

    def __enter__(self):
        self.screen._step_line(self.title)
        return self

    def __exit__(self, et, ev, tb):
        if not self._finished:
            self.screen._step_result(et is None, "failed" if et is not None else "done",
                                     time.time() - self.t0)
        return False

    def done(self, status):
        """The step went well: a green result."""
        self._end(True, status)

    def skip(self, status):
        """Nothing to do (already there, switched off): the same green, the status says why."""
        self._end(True, status)

    def _end(self, ok, status):
        if self._finished:
            return
        self._finished = True
        self.screen._step_result(ok, status, time.time() - self.t0)
