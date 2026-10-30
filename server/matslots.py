"""Far-tile imagery material names from a reusable pool (2026-10-16).

The game's per-frame cost grows with every Material it ever created, deleted ones included (in-game
2026-10-16: after 8 teleports the same drive ran 137 -> 130 fps until a restart; 4000 Materials made
and deleted cost 6-14 % fps, 4000 colorMap switches on 100 Materials ~0). A per-tile name
(bngEarthSat_<anchor>_<key>) made a new Material for every tile of every teleport. Pooled names
(bngEarthFar_<session>_<n>) are handed out again once no game can show the tile that had one, and the
mod keeps their Materials and points them at the new tile's texture (satmat.lua): the number of
Materials stops at the most tiles shown at once.

A name is taken when a tile is built (its shape file names it). It goes back to the pool when its tile lets go
of it (the tile's files are deleted, or reclaim() takes it from an idle tile while the pool runs short, least
recently used first) AND no game holds it: every far_tile with a pooled name carries a serial, the mod reports
[slot, serial] when the object (or the never-spawned instance) of that message is gone, and a slot is held by a
connection until its latest send is reported gone (Connection.slot_holds; a reconnecting mod lists what it
still holds in hello). No timing guess: a name is never reused while a game may still draw it.
A reclaimed tile sent again gets a new name and a copy of its shape with that name (Server.reslot).
The event loop releases names, checks them at send time and records holds; tile workers only take names.

Shape files are pooled the same way (in-game 2026-10-16: the frame cost grows ~0.3 ms per 1000 distinct shape
files the engine ever loaded, shadows and reflections most; loading a path again costs nothing, and a path no
object holds is read afresh): slot n's tile is sent as slot_<n>.cdae, a hard link to (or a copy of) the tile's
own file, replaced atomically when the slot goes to another tile. The mod frees a pooled path still held by a
spare or a doomed object before loading it again (farfield.lua freeSlot).
"""
import os
import re
import shutil
import threading
import staging

PREFIX = "bngEarthFar"
DIGITS = 5
LOW = 32            # reclaim when fewer free names than this ...
TARGET = 128        # ... until this many are free (or nothing idle is left)


class MatSlots:
    def __init__(self, session, held=lambda n: False):
        self.prefix = f"{PREFIX}_{session}_"
        self.held = held        # n -> does some game still hold slot n (Server.slot_held)
        self.lock = threading.Lock()
        self.made = 0           # names ever handed out (= the mod's Materials for this session)
        self.free = []          # numbers free to hand out again (a stack: recently freed first)
        self.slot = {}          # ck -> number
        self.owner = {}         # number -> ck
        self.waiting = set()    # numbers no tile owns that a game still holds (free once it lets go)

    def name(self, n):
        return f"{self.prefix}{n:0{DIGITS}d}"

    @staticmethod
    def number(name):
        return int(name.rsplit("_", 1)[1])

    def name_in(self, data):
        """The pooled name a shape file's bytes carry (this pool's), or None."""
        m = re.search(re.escape(self.prefix.encode()) + rb"\d{%d}" % DIGITS, data)
        return m.group(0).decode() if m else None

    @staticmethod
    def file(name, ext=".cdae"):
        """The pooled shape file of a slot name: slot_<n><ext>."""
        return f"slot_{name.rsplit('_', 1)[1]}{ext}"

    def take(self, ck):
        """The tile's name: the one it holds, else a free one, else a new one."""
        with self.lock:
            n = self.slot.get(ck)
            if n is None:
                if self.free:
                    n = self.free.pop()
                else:
                    n = self.made
                    self.made += 1
                self.slot[ck] = n
                self.owner[n] = ck
            return self.name(n)

    def owns(self, ck, name):
        """Does the tile still hold this name?"""
        with self.lock:
            n = self.slot.get(ck)
            return n is not None and self.name(n) == name

    def release(self, ck):
        """The tile lets go of its name: free now, or once no game holds it (unheld)."""
        with self.lock:
            n = self.slot.pop(ck, None)
            if n is not None:
                del self.owner[n]
                if self.held(n):
                    self.waiting.add(n)
                else:
                    self.free.append(n)

    def unheld(self, n):
        """No game holds slot n any more (event loop): free if no tile owns it."""
        with self.lock:
            if n in self.waiting:
                self.waiting.discard(n)
                self.free.append(n)

    def short(self):
        return len(self.free) < LOW

    def reclaim(self, idle):
        """Free names of idle tiles (an iterable of ck, least recently used first) until TARGET are
        free. Returns how many were freed."""
        freed = 0
        for ck in idle:
            if len(self.free) >= TARGET:
                break
            with self.lock:
                n = self.slot.get(ck)
            if n is None or self.held(n):
                continue
            self.release(ck)
            freed += 1
        return freed

    def holders(self):
        """Every tile holding a name (a copy)."""
        with self.lock:
            return list(self.slot)

    def stats(self):
        with self.lock:
            return {"made": self.made, "owned": len(self.slot), "free": len(self.free), "waiting": len(self.waiting)}


def restamp(data, old, new):
    """A shape file's bytes with its material name `old` replaced by `new` (the same length: the
    .cdae string table keeps its layout). Raises ValueError if the name is not in it."""
    old_b, new_b = old.encode(), new.encode()
    if len(old_b) != len(new_b):
        raise ValueError("names differ in length")
    if old_b not in data:
        raise ValueError(f"{old} not in the shape")
    return data.replace(old_b, new_b)


def place(src, dst):
    """dst becomes a file with src's content, atomically (a hard link; a copy where links fail): a game loading
    dst sees either the old file or the new one, never a partial one."""
    tmp = staging.tmp_for(dst)
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copyfile(src, tmp)
    staging.replace(tmp, dst)
