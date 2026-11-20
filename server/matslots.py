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
game lease until its latest send is reported gone (Connection.slot_holds aliases the current lease). TCP EOF
retains that lease; the reconnecting mod names its server-issued leaseId and lists its complete held snapshot
in hello. An older client without a resumable identity is retained conservatively until server shutdown.
No timing guess: a name is never reused while a game may still draw it.
A reclaimed tile sent again gets a new name and a copy of its shape with that name (Server.reslot).
The event loop releases names, checks them at send time and records holds; tile workers only take names.

Shape files are pooled the same way (in-game 2026-10-16: the frame cost grows ~0.3 ms per 1000 distinct shape
files the engine ever loaded, shadows and reflections most; loading a path again costs nothing, and a path no
object holds is read afresh): slot n's tile is sent as slot_<n>.cdae, a hard link to (or a copy of) the tile's
own file, replaced atomically when the slot goes to another tile. The mod frees a pooled path still held by a
spare or a doomed object before loading it again (farfield.lua freeSlot).
"""

import fast
import staging

_native = fast.native(557, "pooled materials and shape paths")
MatSlots = _native.MatSlots
TARGET = _native.SLOT_TARGET
restamp = _native.slot_restamp
start_renewal = _native.start_renewal
finish_renewal = _native.finish_renewal
rebuild_renewal = _native.rebuild_renewal
write_renewal = _native.write_renewal
reslot_sync = _native.reslot_sync


def place(src, dst):
    """dst becomes a file with src's content, atomically (a hard link; a copy where links fail): a game loading
    dst sees either the old file or the new one, never a partial one."""
    tmp = staging.tmp_for(dst)
    _native.staging_link(src, tmp)
    staging.replace(tmp, dst)
