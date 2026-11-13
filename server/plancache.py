"""Road plans shared between the server's processes (2026-10-23).

A cell's road plans (grading._Planner._plan_cell: the profiles, the kept carriageways, the tunnels' covers and open
sections) are a pure function of the cell, the map data and elevation, and the grading settings (in the frame of the
cell's planning region, geotiles.PLAN_Z: no anchor in them since 2026-10-29, so every anchor reads them), and
every process that grades needs them: both terrain-block processes (each warmed the cells ahead of the player for
itself) and the road-tile processes. On a Steam Deck a fresh place's teleport spent ~300 CPU-s planning the same
cells up to four times over, of ~1000 CPU-s for its whole world on four cores. Now the first process to plan a cell
writes it here; the others read it, or wait while one is planning it (a lock file) instead of planning it again.

The files: <cache>/plans/<run>/<signature>/<z>_<x>_<y>.npz, the arrays as npz (no pickle) and the
rest as JSON (Spec as its fields). One server run's own (RUN_ENV, set by earthd before its workers start): a plan made
while some elevation was missing stays out of the next run. A stale lock (xproc.STALE_S: a process that died planning)
is taken over; a wait longer than WAIT_S plans the cell here as before; a process waits only for a planner at least as
favoured as itself (xproc.once). Old runs' folders go at server start (prune).
"""
import logging
import json
import hashlib
import io
import os
import shutil
import threading
import time
import zipfile

import numpy as np

import xproc

log = logging.getLogger("earthd.plancache")

WAIT_S = 90.0
LONG_WAIT_S = 20.0          # s: a wait for another process's plans this long is logged (claim)
KEEP_S = 6 * 3600.0         # run folders untouched this long are removed (prune)
VERSIONS_RECHECK_S = 2.0    # s between reads of the map data's versions for the revisit keys (PlanCache.persist)
RUN_ENV = "BNGE_PLAN_RUN"


STAGE_ON = os.environ.get("BNE_STAGE_SHARE", "on") != "off"   # (road stages shared by the run's processes; off: each its own)
STAGE_WAIT_S = 45.0         # s a process waits for another making a cell's road stage before it makes it too
STAGE_SLICE_MS = 200        # ms of that wait between two looks at the waiting job's cancel check


def _stage_ef():
    """The native module's stage functions (earthfast 290), or None when the sharing is off."""
    if not STAGE_ON:
        return None
    import fast
    return fast.native(290, "shared road stages")


def _pack(arrays, g, items):
    """items (arrays of one dtype, shape (n_i, ...)) as one array g (concatenated along the first axis) and g_n
    (the n_i); a dtype per group, as the plans make them (exact round trip)."""
    items = [np.asarray(x) for x in items]
    arrays[g + "_n"] = np.array([len(x) for x in items], np.int64)
    if items:
        if len({(x.dtype.str, x.shape[1:]) for x in items}) != 1:
            raise ValueError(f"plancache: mixed arrays in {g}")
        arrays[g] = np.concatenate(items)


def _split(f, g):
    n = f[g + "_n"]
    if not len(n):
        return []
    return np.split(f[g], np.cumsum(n)[:-1])


def start_run(cache_dir):
    """earthd, before its worker processes start: this run's id in the environment they inherit; old runs pruned."""
    root = os.path.join(cache_dir, "plans")
    prune(root)
    if os.environ.get("BNGE_PLANCACHE") == "off":       # (debugging: each process plans its own, as before)
        os.environ.pop(RUN_ENV, None)
        return
    os.environ[RUN_ENV] = f"{os.getpid()}_{int(time.time())}"


# The plans' output format: their key in the revisit cache (kept across updates) and in baked packs (bakepack.py), in
# place of the code hash. Bump it whenever a change makes other plans (grading, its inputs' rules), or players keep the
# old ones; tests/test_bake_formats.py fails until it is bumped (its fixture's digest is stored with the number).
PLANS_FORMAT = 65     # 65: a rail bed coupled to the roads beside it keeps each vertex within its own roads' band after the run-out along the track (earthfast railseam: a move down to a bus lane the track lies in no longer carries 2 m onto a street it crosses 30 m on), and a lower road crossing a tunnel part the elevation shows open all along eases onto its carriageway whatever the class gap (gradebins OPEN_TUNNEL, Plan.open_tunnel): Kennedybruecke in Vienna stepped 2.65 m and 1.2 m (GitHub #1169), 2026-11-13; 64: a street closed to cars (a cycleway with its sidewalks mapped on it) is a road of the plans (roads.STYLE "cycleway": London's filtered Warren Street, the user's report 2026-11-12), FACTS_FORMAT 26; 63: a track running down a street inside its carriageway (railways.street_along, STREET_ALONG_M) cuts the 3D track there and is flush with the street, like a crossing road (Fells Point's Thames Street trolley tracks, the user's report 16), and roads.FACTS_FORMAT 25 (the widths of laned town streets between buildings), 2026-11-12; 61: deterministic maths (GitHub #1169: the kernels' and the server's sin / atan2 / hypot / exp / pow / dot / norm from the libm crate and exact formulations, dm.rs / dnp.py / dmath.py: Windows, Linux and every glibc / CPU plan the same roads), FACTS_FORMAT 23, 2026-11-12; 60: a short rail bridge over a road (10-12 m) dips the road under it (grading RAIL_MIN_CROSS_M), to the height limit the road signs (max_height_m, RAIL_SIGNED_ON) at most 2.5 m, and the rail bridges of a long segment are kept (railways MIN_PART_M, were dropped under 0.1 % of it) and on their place (pmtiles anchors, pmtiles.FORMAT 2; Robitnik's PR #1193), FACTS_FORMAT 22; 59: a motorway's or trunk road's built-up test 90 m out (roads.URBAN_HIGHWAY_BESIDE_M, wyo-backroads' PR #1117: its urban flag and widths); 58: plans independent of the order cells are planned in (a road width per copy of the road, roadwidth._memo_key; railway ground per centimetre at its own point; pin heights per candidate copy and context, _stronger_zs; the decks round a point past the cell being planned, _near_pack, each deck its home cell's copy, _decks_in), FACTS_FORMAT 20, 2026-11-12; 57 (56 not used): one source per tunnel network (the grading takes a tunnel's floor and sections from the tunnel networks' solve, their portals graded by the roads' plans alone), tunnels under flat ground buried by default (not in a trench the ground shows, not beside a slip road joining), only a street lower than the bare ground kept off a tube's cover, 2026-11-12; 55: tunnels as networks: a tunnel's joints at the floor of the whole tunnel (earthfast tunnel_chain_nodes, GitHub #1160 #1161 #490, Tromsø), the tube fitted under a road over a short section, opt-in burial under flat ground (BNE_TUNNEL_BURY), 2026-11-12; 54: FACTS_FORMAT 18 (town streets narrowed to the walls along them, roads._wall_capped: the graded corridors with them); 53: two carriageways overlapping in plan at other levels: the point is the road's it lies deeper in (earthfast gradebins, the S1's east lanes under Rautenweg's bank ramp, GitHub #1083), 2026-11-12; 52: lid junctions (GitHub #1083: Vienna's S1 under Rautenweg and Suedtiroler Platz, Porte Maillot) and slip roads at raised junctions (#960): a ground road that ends at a deck joint is no road under the deck (grading.JOINT_ROADS: the junction's own arms asked 8 m of clearance over themselves); an abutment's lift from the bridge whose end is nearest (NEAREST_END); an approach fill is a surface, the road's own climb taken off it and smoothed (APPROACH_FILL, approach.rs FILL_SMOOTH_M: no sag where a ramp meets a road climbing away, the A6 at Heilbronn #1176); a dipped road no longer holds its whole length beside other roads (DIP_HOLDS off: an M1 slip road stepped 1.4 m off the raised road it forks from); FACTS_FORMAT 17 (a junction over a tunnel in the open is an underpass, gradesep JOINED_ENDS); and each joint of a deck chain at one height (roads.DECK_WELD); 51: a raised junction is a height, not an offset (grading JR_FILL: an arm end above the junction rises only to its height, an arm filled to the ramp's surface, the ramp's remainder less what the stub climbs out of its abutment's cut; Wesley Chapel's SR 56 decks 13 m over I-75 for the real 7 m, GitHub #1043), and the dips under a deck see it climb from its ends and the junction raise carry its abutments (UNDER_CLIMB, JR_CARRY: I-75 dipped 1.6 m under them, now 0.4 m); 50: roads side by side agree in height (grading.share_corridors: a fork's branches, a slip road beside the road it joins, a road dipped under a deck and the ramp beside it; 0.4-0.8 m ramps across the lanes at Wesley Chapel #1043 and Findlay #1045; dirt roads without synthetic damage, smoothed as their class, and not eased flat onto a paved shoulder they leave; tunnel mouth plans without grade rank; a rail line from its longest copy); 49: FACTS_FORMAT 16 (lane sections of merged highways with one-sided auxiliary-lane tapers graded, PR #880; measured default widths, narrow winding rural roads); 48: both 47s merged; 47 (bumps): a rail bed's plan cut out of the carriageway of a road it crosses at grade (grading.rail_yield_pieces: a 0.3-0.7 m dip across the road at level crossings, Fells Point's streets with their tracks), and the deck-ground correction faded in from a deck end on the ground (grading.deck_ground_abut_taper: I-405's overpass ends read 5 m low off their fill); 47 (curves): the road centrelines rounded and curvature continuous by default (roadsmooth.py ON, G2 spirals, raceways included, interior connectors pinned: the user 2026-11-12, banked ovals ridden over their chords); 46: a side road trimmed at its mouth has its bank and widen cut with its line (they were left as long as the untrimmed line, and every later plan in the set read shifted bank values: 289 of 905 banked plans in 87 captured sets); 45: both of the 2026-11-11 night's 44s at once (the small and big PR batches each bumped to 44): one road planned in pieces meets itself at one height (grading.weld_continuations), a side road's mouth ends at the painted edge with a 20 m Hermite ramp (grading.trim_side_mouths), a rural minor road's flat verge is 1 m, and unclassified side roads get the gentle rank side ease (RANK_SIDE_MAX_DIFF 6) (PR #1107); and railway beds from one lattice of node profiles (railprofile.py: the same bed in every tile and grading cell), long smoothing of ground beds and rounded bends, switches brought to the lines they join, a road crossing a track at grade flush with it, a short rail bridge over a road on level ground a level crossing, a road under a rail bridge on lidar lowered at most 1 m (BNE_RAIL_UNDER_LIDAR_DROP) (PR #1106); 43: the plans' elevation has the bridges it holds under mapped decks taken out (grading.DECK_GROUND_*: the Pont de Normandie's 60-150 m ridge under its deck, GitHub #1098); 42: an overpass's abutment lift reads the road under beside the deck, as that road's own plan reads it (grading.ground_under_deck, LIFT_UNDER_BESIDE: an elevation holding the deck over a motorway cut asked 6.7-8.5 m at Albox, GitHub #1086, PR #1090); 41: a short road tunnel grade separation made ground still holds the railway over it at underpass clearance (railways.road_underpasses reads the map's tunnel ranges too: Nashville, PR #1094); 40: no junction raise at a freeway's own deck, nor for a lift only an elevation model holding the deck over the road under asks (grading JR_OWN_CLASS / JR_TRUSTED: Andalusia's roundabouts and slip roads stood 5-8 m over the ground, GitHub #1077); 39: road grading ranks: a built (paved / cobble) road keeps its own ground where a dirt road runs beside it, the dirt road adapting; a side road eases onto the higher road's ground (RANK_SIDE_FADE_M, RANK_HEIGHT_FADE_M) and the blend starts at the painted asphalt edge; a road with no equal-ranked road at a point holds its own carriageway, and a junction pad takes only the highest-ranked branch (GitHub #771, PR #1050); 38: a road under a measured deck needs only real headroom under it, the deck as measured over it (grading UNDER_MEASURED_ON: I-75 dipped 1.5 m under an overpass at Findlay); 37: the ramp envelope only where the geometry breaks (a step of 0.3 m or more where roads meet, a raised road steeper than 12 % and than before), else nothing changes; 36: the roads round a raised junction and a freeway overpass's approaches on ramps no steeper than 6 % (grading.ramp_envelope: steps of 45-58 % at Findlay's diverging diamond), freeway links rise with their junction; 35: an approach's junction rises with it (grading JUNCTION_RAISE_ON: the junction's roads no longer cap its ramp; GitHub #1043, #1045); 34: a ground track takes a distant deck's rise over its own bed, not its height (railways.profile: a mountain line's deck carried down at 2 % stood Andermatt's station 14 m up, GitHub #1051); 33: a small roundabout's ring wider by its fitted radius (roads.ring_width, PR #1048); 32: a measured deck on the ground where clearance needs a lift is stale (grading OBS_STALE_LIFT_M, roads OBS_STALE_M: GitHub #1045), and 31's tunnel end rule taken back (it pushed portals out of approach cuts at Caldecott, Tom Lantos, the Gotthard); 31: a tunnel's short open end run over a drop is tube (roads.portal_cover: Fort Pitt's east portal onto its bridge); 30: FACTS_FORMAT 13 (skew crossings' grade-separation bridges long enough for the road under them, GitHub #1020); 29: FACTS_FORMAT 12 (a long road's copies far from its home agree on its bridges over underpasses, GitHub #1020); 28: a rail bed beside a road that crosses under it (a bridge, SEAM_CROSS_M of height at a
                      # crossing angle) keeps its own level: the approach fill is not taken down to the road under
                      # the deck and the two sides meet the deck; 27: FACTS_FORMAT 11 (an underpass in a cutting bridged by the roads over it, GitHub #1009); PR #1014's one-way carriageways drawn and graded where OpenStreetMap's placement puts their lanes (PR #1014); 26: CA-1's Tom Lantos tunnels: a bridge starts at a tunnel's portal over a short ground gap (roads._portal_decks, FACTS_FORMAT 10), no portal low arch shorter than SECTION_MIN_M before a full tube (roads.portal_cover), and a tunnel's own sections (a low arch at its portal, the full tube) and a part going on from another's end never twin tunnels (roads.twin_tunnels: grading._twin_trimmed cut them, CA-1's Tom Lantos bore lost its tube); 25: underpasses: no open-road trench through the roads over them, a tunnel's portal kept where its own approach road joins it (roads._tunnel_reach) and a road's bridge over one found in any cell it crosses (roads._gradesep_elsewhere; FACTS_FORMAT 9); 24: a segment's tunnel ranges joined only where one source way spans the gap (PR #927); a rail bed's band past its ballast gives way to an approach's carriageway, no claim past an abutment end (PR #920, #909); bridge pieces keep their mapped local levels across merged interchange roads (PR #873, #866); 23: bridge seams without steps: a deck end meets its road, a street off an abutment held level, a single span shared between its approaches, a stream crossed at the road's level, a track beside a road with room for the ground between (grading._pin_cw_ends, _hold_mouth, _grade_match, railroadseam.py, roads.water_clear); 22: overpasses mapped in parts are one span and road ends within 2 m one dip node (#973, #983; still the same release as 21); 21: one bump for the 2026-11-08 PR batch: abandoned railways not graded (#953, #706), dirt roads on the coarse global elevation smoothed over DIRT_SIGMA_COARSE_M (#951, #835), rail viaduct decks' heights capped (#949), worn railway lines cut at motorways (#946, #918); 18: cutting probes located through grading.enu_to_ll_dense (heights moved < 0.1 mm); 17: PR #843 (restored highway and ramp shoulder widths); 16: walkway bridge spans cap the road under them (PR #842) and OSM busways graded (PR #836); 15: continuous subsea tunnel profiles and their approaches (PR #822, #792); 14: past an abutment end the ground falls away under a still-climbing deck (ABUT_UNDER_GRADE)
                      # 13: centimetre-scale dirt wear and bounded edge displacement from random banking
                      # 12: dirt roads toned down (#783, #784; dev); PR #811's own 12: bounded, local dirt damage, independent of real
                      #     grades and DEM extensions
                      # 11: pass roads with two lanes graded as mountain roads again (the Stelvio); 10: worn dirt roads (PR #677: damage, lean, 2 m vertices, 2.5 m smoothing of minor dirt); 9: roundabout rings level across (RING_LEVEL_ON); 8: PR #753 (a deck end's approaches rise with it, slip roads tied to their overpass road); 7: PR #471 and #686 lanes (wider / narrower roads graded); 6: the dip under a deck takes its ends on their embankments (#684's fill); 5: one half width a bridge (each was appended twice: under-deck ground and carriageways read the wrong bridge's); 4: junction pads, steep side-road ramps, rail bridges the DEM predates (PR #482/#671); 3:
                      # bridge ends on embankments (#694), the ground under decks (#698), junction corridors (#682)


def plans_format():
    """The plans' revisit key format: PLANS_FORMAT and the road facts' (roads.FACTS_FORMAT: the plans are made from
    them), "<plans>.<facts>"."""
    import roads
    import grading
    # (the grade guard, off by default, makes other plans: its own key, so that the two never mix and nobody else's
    # plans are thrown away)
    return f"{PLANS_FORMAT}.{roads.FACTS_FORMAT}" + (".gg1" if grading.GUARD_ON else "")


FRAMES = "r10"          # the plans' frames: their planning regions' (geotiles.PLAN_Z), not an anchor's


def for_grader(cache_dir, grader, widths, wet):
    """The PlanCache of a grading.Grader (any anchor's: the plans are its store's, in the planning regions' frames;
    settings that change plans in the signature), or None outside a server run. Complete plans are kept in the revisit
    cache too (persist): a place's next visit (a game started where it was left, or anchored anywhere near) reads them
    instead of planning again."""
    md = getattr(grader, "md", None)
    signature = f"w{int(widths is not None)}s{int(wet is not None)}"
    bundle = os.environ.get("BNGE_WORLD_BUNDLE")
    if bundle:
        from worldbundle import PinnedPlanCache
        versions = getattr(md, "bundle_versions", None) or getattr(md, "versions", None)
        return PinnedPlanCache(bundle, signature, versions() if versions is not None else {})
    run = os.environ.get(RUN_ENV)
    if not run:
        return None
    pc = PlanCache(os.path.join(cache_dir, "plans", run), signature)
    # Resolve after generation: loading source layers can discover their actual shard releases.
    pc.source_versions = getattr(md, "bundle_versions", None) or getattr(md, "versions", None)
    pc.persist = (cache_dir, (FRAMES, pc.signature,
                              md.versions() if md is not None and hasattr(md, "versions") else None))
    if md is not None and hasattr(md, "versions"):
        # (the versions followed, not taken once: a first run's weekly manifests come after its first anchor, and a
        # pinned week-old build is refreshed during a session; taken once, every plan of the session was keyed
        # without them / with the old build, and never found again, nor in a shared archive)
        pc._versions_of = md.versions
    return pc


class PlanCache:
    def __init__(self, root, signature):
        self.dir = os.path.join(root, FRAMES, signature)
        self.signature = signature
        self._persist = None    # (cache dir, key parts) of the revisit cache's copy (for_grader), or None: persist
        self._versions_of = None    # (for_grader: the map data's versions(), read again at most every VERSIONS_RECHECK_S)
        self._persist_at = time.monotonic()
        self.source_versions = None  # callable for the versions actually known after a complete cell was built
        self.stats = {"read": 0, "written": 0, "waited": 0, "wait_s": 0.0}
        self._lock = threading.Lock()

    def _path(self, key):
        return os.path.join(self.dir, f"{int(key[0])}_{int(key[1])}_{int(key[2])}.npz")

    def load(self, key):
        """(plans, keeps, covers, opens, corrected decks or None) of cell key from the cache, or None."""
        from grading import Plan, set_widen
        from roads import spec_from_json
        try:
            src = self._path(key)
            final = False
            if not os.path.exists(src):
                src = self._kept(key)
                if src is None:
                    src = self._baked(key)
                if src is None:
                    return None
            elif os.path.exists(src + ".short"):
                # (made short in this run, and complete since in the revisit cache (latework.py: planned again once
                # its lidar was in, in a process of the late pool): that copy, read as complete, is_short)
                got = self._kept(key)
                if got is not None:
                    src, final = got, True
            self._note_final(key, final)
            with np.load(src, allow_pickle=False) as f:
                meta = json.loads(bytes(f["meta"]).decode("utf-8"))
                lists = {g: _split(f, g) for g in ("p", "z", "c", "k", "v", "o")}
                lists["b"] = _split(f, "b") if "b_n" in f.files else []
                lists["w"] = _split(f, "w") if "w_n" in f.files else []
                # (grading.DECK_GROUND_*: the decks over a bridge the elevation holds, their lines and stations)
                lists["g"] = _split(f, "g") if "g_n" in f.files else None
                lists["r"] = _split(f, "r") if "r_n" in f.files else None
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):   # (damaged: planned again)
            return None
        plans, cuts, banks, widens = [], iter(lists["c"]), iter(lists["b"]), iter(lists["w"])
        for m, xy, z in zip(meta["plans"], lists["p"], lists["z"]):
            pl = Plan(xy, z, m["half"], tuple(m["abut"]), next(cuts) if m["cut"] else None, m["road_half"])
            pl.rid, pl.max_wall = m["rid"], m["max_wall"]
            pl.rank = int(m.get("rank", -1))
            pl.steep = bool(m.get("steep", False))
            pl.open_tunnel = bool(m.get("open_tunnel", False))
            pl.bank = next(banks) if m.get("bank") else None
            if m.get("widen"):
                set_widen(pl, next(widens))
            plans.append((m["fid"], pl))
        keeps = list(zip(lists["k"], meta["keeps"]))
        covers = list(zip(lists["v"], meta["covers"]))
        try:
            opens = [(xy, spec_from_json(sp)) for xy, sp in zip(lists["o"], meta["opens"])]
        except (TypeError, ValueError, KeyError):
            return None
        decks = None
        if lists["g"] is not None and lists["r"] is not None and "decks" in meta:
            from grading import deck_key
            decks = [(deck_key(line, half), line, float(half), rows)
                     for line, rows, half in zip(lists["g"], lists["r"], meta["decks"])]
        with self._lock:
            self.stats["read"] += 1
        return plans, keeps, covers, opens, decks

    def known_complete(self, key):
        """Were cell key's plans as load() reads them made complete? This run's file only with its ".complete" marker
        (store: written after the file, and only for complete plans); the revisit cache's and the baked packs' copies
        always (only complete plans are kept there). Unsure: False (grading._PLANS_COMPLETE, the disk block cache)."""
        p = self._path(key)
        if self._final(key):
            return True
        if os.path.exists(p):
            return os.path.exists(p + ".complete")
        return True

    def _note_final(self, key, final):
        """load read cell key's run file (final False) or the revisit cache's complete copy in place of the run's short
        one (True): what is_short / known_complete answer for it in this thread (the one that read it: another
        thread's read of the short file before the copy came stays short)."""
        tl = self.__dict__.get("_tl")
        if tl is None:
            with self._lock:
                tl = self.__dict__.setdefault("_tl", threading.local())
        got = getattr(tl, "finals", None)
        if got is None:
            got = tl.finals = set()
        k = (int(key[0]), int(key[1]), int(key[2]))
        if final:
            got.add(k)
        else:
            got.discard(k)

    def _final(self, key):
        tl = self.__dict__.get("_tl")
        got = getattr(tl, "finals", None) if tl is not None else None
        return bool(got) and (int(key[0]), int(key[1]), int(key[2])) in got

    def _revisit_key(self, key):
        import revisit
        import roadz
        # (by PLANS_FORMAT, not the code: kept across updates, the user, 2026-11-02; a cell with Hong Kong's measured
        # heights or terrain its own: the plans made there before, or with BNE_ROADZ_HK / BNE_HKDTM off, are not those)
        import roadsurf
        return revisit.format_key("plans", plans_format(), *self.persist[1],
                                  (int(key[0]), int(key[1]), int(key[2])), *roadz.hk_tags(key),
                                  *roadsurf.cell_tags(key))     # (the photo's road surfaces round it: other kinds)

    def _where(self, key):
        """revisit.where of cell key's plans (the map's cache view), or None."""
        import mapcells
        import revisit
        try:
            w, s, e, n = mapcells.CellKey(int(key[0]), int(key[1]), int(key[2])).bounds()
            _frames, sig, versions = self.persist[1]
            # (the sidecar's place: the cell's own centre, no anchor)
            return revisit.where((s + n) / 2, (w + e) / 2, s, w, n, e, revisit.sig(plans=sig, md=versions))
        except Exception:
            return None

    @property
    def persist(self):
        """(cache dir, (frames, signature, the map data's versions now)) or None: the revisit cache's key parts."""
        p = self._persist
        if p is None or self._versions_of is None:
            return p
        now = time.monotonic()
        if now - self._persist_at >= VERSIONS_RECHECK_S:
            self._persist_at = now
            try:
                v = self._versions_of()
            except Exception:
                v = None
            if v is not None and v != p[1][2]:
                p = self._persist = (p[0], (p[1][0], p[1][1], v))
        return p

    @persist.setter
    def persist(self, value):
        self._persist = value
        self._versions_of = None

    def has(self, key):
        """Are cell key's plans on disk (this run's, or the revisit cache's copy)? A stat or two, nothing read."""
        if os.path.exists(self._path(key)):
            return True
        if self.persist is None:
            return False
        import revisit
        if revisit.has(self.persist[0], "plans", self._revisit_key(key)):
            return True
        import bakepack
        import roadsurf
        if roadsurf.cell_tags(key):
            return False
        import roadz
        if roadz.hk_cell(key):
            return False
        k = bakepack.plans_key(self.signature, key)
        if any(k in p.items and bakepack._matches(p, self._bake_expect()) for p in bakepack.packs(self.persist[0])):
            return True
        import decisions        # (a shared archive's block already downloaded: nothing fetched or unpacked here)
        return decisions.enabled() and decisions.has_plans(self.persist[0], self.signature, key, self.persist[1][2])

    def _bake_expect(self):
        import bakepack
        return {"bake_format": bakepack.BAKE_FORMAT, "plans_format": PLANS_FORMAT, "frames": FRAMES,
                "signature": self.signature,
                "versions": json.loads(json.dumps(self.persist[1][2], sort_keys=True))}

    def _baked(self, key):
        """A baked pack's plans of cell key (bakepack.py: made offline for this map data and signature), or None."""
        import roadsurf
        import roadz
        if self.persist is None or roadz.hk_cell(key) or roadsurf.cell_tags(key):
            return None             # (packs are made without Hong Kong's data and without the photo's road surfaces)
        import io
        import bakepack
        got = bakepack.find(self.persist[0], bakepack.plans_key(self.signature, key), self._bake_expect())
        if got is None:
            got = self._decided(key)
            if got is None:
                return None
        with self._lock:
            self.stats["baked"] = self.stats.get("baked", 0) + 1
        return io.BytesIO(got)

    def _decided(self, key):
        """The shared decision archives' plans of cell key for this version (decisions.py: built by the project's
        builder, signed, checked when downloaded), kept in the revisit cache under this server's own key; or None."""
        import decisions
        if not decisions.enabled():
            return None
        got = decisions.plans(self.persist[0], self.signature, key, self.persist[1][2])
        if got is not None:
            import revisit
            revisit.store(self.persist[0], "plans", self._revisit_key(key), {"plans.npz": got},
                          where=self._where(key), code=revisit.format_marker("plans", plans_format()))
            with self._lock:
                self.stats["decided"] = self.stats.get("decided", 0) + 1
        return got

    def _kept(self, key):
        """The revisit cache's copy of cell key's plans (a file-like npz), or None."""
        if self.persist is None:
            return None
        import io
        import revisit
        got = revisit.load(self.persist[0], "plans", self._revisit_key(key))
        if got is None or "plans.npz" not in got[0]:
            return None
        return io.BytesIO(got[0]["plans.npz"])

    def store(self, key, plans, keeps, covers, opens, decks=None, persist=False):
        """Write cell key's plans for the run's other processes; persist: also into the revisit cache (complete plans:
        nothing they read failed or was missing, Grader._plan_cell)."""
        from roads import _spec_json

        meta = {"plans": [{"fid": fid, "half": p.half, "abut": list(p.abut), "road_half": p.road_half,
                           "rid": int(p.rid), "rank": int(getattr(p, "rank", -1)),
                           "max_wall": float(p.max_wall), "cut": p.cut is not None,
                           "bank": getattr(p, "bank", None) is not None, "steep": bool(getattr(p, "steep", False)),
                           "open_tunnel": bool(getattr(p, "open_tunnel", False)),
                           "widen": getattr(p, "widen", None) is not None}
                          for fid, p in plans],
                "keeps": [float(hw) for _xy, hw in keeps], "covers": [float(w) for _xy, w in covers],
                "opens": [_spec_json(sp) for _xy, sp in opens],
                "decks": [float(half) for _k, _line, half, _rows in (decks or ())]}
        arrays = {"meta": np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8)}
        try:
            _pack(arrays, "p", [p.xy for _f, p in plans])
            _pack(arrays, "z", [p.z for _f, p in plans])
            _pack(arrays, "c", [p.cut for _f, p in plans if p.cut is not None])
            _pack(arrays, "b", [p.bank for _f, p in plans if getattr(p, "bank", None) is not None])
            _pack(arrays, "w", [p.widen for _f, p in plans if getattr(p, "widen", None) is not None])
            _pack(arrays, "k", [xy for xy, _hw in keeps])
            _pack(arrays, "v", [xy for xy, _w in covers])
            _pack(arrays, "o", [xy for xy, _sp in opens])
            _pack(arrays, "g", [np.asarray(line, np.float64) for _k, line, _h, _r in (decks or ())])
            _pack(arrays, "r", [np.asarray(rows, np.float64) for _k, _l, _h, rows in (decks or ())])
            p = self._path(key)
            os.makedirs(self.dir, exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            buf = io.BytesIO()          # (the bytes kept in hand: hashed and put in the revisit cache, not read back)
            np.savez(buf, **arrays)
            data = buf.getvalue()
            with open(tmp, "wb") as fh:
                fh.write(data)
            provenance = None
            import roadz
            shared = not roadz.hk_cell(key)         # (Hong Kong's heights stay on this computer: roadz.hk_cell)
            if persist and shared:
                from worldbundle import canonical, completion_record
                digest = hashlib.sha256(data).hexdigest()
                try:
                    sources = self.source_versions() if self.source_versions is not None else {}
                    provenance = canonical(completion_record(key, self.signature, sources, digest))
                except (OSError, ValueError, TypeError):
                    # Optional export provenance must not disable normal local caching. Without a
                    # valid marker this cell remains unexportable, including any stale earlier marker.
                    pass
            # (made short of something, this run's other processes read it so: is_short; before the plans)
            if persist:
                try:
                    os.remove(p + ".short")
                except FileNotFoundError:
                    pass
            else:
                with open(p + ".short", "wb"):
                    pass
            os.replace(tmp, p)
            # Export only plans whose complete-input condition the grader actually observed.
            # The digest binds the marker to this version; an interrupted replacement fails closed.
            marker = p + ".complete"
            if provenance is not None:
                with open(tmp, "wb") as fh:
                    fh.write(provenance)
                os.replace(tmp, marker)
            else:
                try:
                    os.remove(marker)
                except FileNotFoundError:
                    pass
            if persist and self.persist is not None:
                import revisit
                revisit.store(self.persist[0], "plans", self._revisit_key(key), {"plans.npz": data},
                              where=self._where(key), code=revisit.format_marker("plans", plans_format()))
                if shared:
                    import mpshare      # (a BeamMP session shares them: the hub sends them to the server)
                    mpshare.note_built(self.persist[0], self.signature, key)
        except (OSError, ValueError, TypeError):
            return False
        with self._lock:
            self.stats["written"] += 1
        return True

    def is_short(self, key):
        """Were cell key's plans in this run's folder made short of something (store's persist false)? Read so, the
        reader's builds are not kept either (grading.Grader._plan_cell). The revisit cache's and baked packs' never are:
        nor the run's short plans once load read their complete copy instead (latework.py)."""
        if self._final(key):
            return False
        return os.path.exists(self._path(key) + ".short")

    def claim(self, key):
        """Before planning cell key: (state, False) when it is in the cache (or another process planned it while we
        waited); (None, True) when this process plans it (call release after store); (None, False) when it plans it
        without the lock (xproc.once)."""
        t0 = time.monotonic()
        if self.persist is not None:
            import mpshare      # (a BeamMP session: the server is sending this cell's plans; waited for a little)
            if mpshare.coming(self.persist[0], self.signature, key):
                while time.monotonic() - t0 < mpshare.COMING_WAIT_S:
                    if self.has(key):
                        got = self.load(key)
                        if got is not None:
                            with self._lock:
                                self.stats["shared"] = self.stats.get("shared", 0) + 1
                            return got, False
                    if not mpshare.coming(self.persist[0], self.signature, key):
                        break   # (missed: planned here)
                    time.sleep(0.1)

        holder = []

        def waiting():
            with self._lock:
                self.stats["waited"] += 1
            h = xproc._holder(self._path(key) + ".lock")
            holder.append(h[2] if h else None)
        got, mine = xproc.once(self._path(key) + ".lock", lambda: self.load(key), WAIT_S, on_wait=waiting)
        waited = time.monotonic() - t0
        if got is not None and waited > xproc.POLL_S:
            with self._lock:
                self.stats["wait_s"] += waited
        if waited > LONG_WAIT_S:            # (a road tile "may be stuck": whose plan it waited for)
            log.warning("cell %s's plans: waited %.0f s for process %s making them (%s)", key, waited,
                        holder[0] if holder else "?", "they came" if got is not None else "made here after all")
        return got, mine

    def release(self, key):
        xproc.release(self._path(key) + ".lock")

    def busy(self, key):
        """Is a process planning cell key now (its claim's lock is there)? A stat, nothing opened (a lock open for
        reading cannot be released on Windows: xproc.once)."""
        return os.path.exists(self._path(key) + ".lock")


    # ---- road stages (grading._Planner._road_stage): made by one process of the run, read by the others ----
    # A cell's road stage (its roads' plans and kept strips, before its tunnels) is what the tunnels of the cells round
    # it are graded by, so every process that planned a cell made its neighbours' stages too: four processes planning a
    # first block's four cells each made all four stages (13.5 CPU-s a cell where 6.5 were its own, Fells Point,
    # 2026-11-13). The files, the claims and the wait are earthfast's (stageshare.rs): this is the plans' form of them.

    def _stage_path(self, key):
        return os.path.join(self.dir, "stage", f"{int(key[0])}_{int(key[1])}_{int(key[2])}.stage")

    def stage_claim(self, key, wait_s=None, hold=True):
        """Before making cell key's road stage: ((plans, keeps, short), False) when a process of this run made it (now,
        or while we waited for it); (None, True) when this process makes it (stage_store, then stage_release);
        (None, False) when it makes it without the claim (a less urgent holder, a wait past wait_s, a damaged file).
        hold false: the claim is never taken (the caller makes several stages at once on one interpreter lock: a
        process waiting for one of them would wait several times as long as making it itself); another process's
        stage is still read, and waited for while it is being made."""
        ef = _stage_ef()
        if ef is None:
            return None, False
        path = self._stage_path(key)
        if not hold:
            got = self.stage_load(key)
            if got is not None or not ef.stage_busy(path):
                return got, False
        rank, prio = xproc._rank("demand")
        pid = os.getpid()
        t0 = time.monotonic()
        st = ef.stage_claim(path, rank, prio, pid)
        waited = False
        while st == ef.STAGE_HELD:
            if time.monotonic() - t0 > (STAGE_WAIT_S if wait_s is None else wait_s):
                return None, False
            if not waited:
                waited = True
                with self._lock:
                    self.stats["stage_waited"] = self.stats.get("stage_waited", 0) + 1
            xproc.check_cancel()        # (a block / road tile of a place a teleport left)
            st = ef.stage_wait(path, rank, prio, pid, STAGE_SLICE_MS)
        if waited:
            with self._lock:
                self.stats["stage_wait_s"] = self.stats.get("stage_wait_s", 0.0) + time.monotonic() - t0
        if st == ef.STAGE_READY:
            return self.stage_load(key), False
        if st == ef.STAGE_MINE and not hold:
            ef.stage_release(path)
            return None, False
        return None, st == ef.STAGE_MINE

    def stage_release(self, key):
        ef = _stage_ef()
        if ef is not None:
            ef.stage_release(self._stage_path(key))

    def stage_store(self, key, plans, keeps, short):
        """Cell key's road stage for the run's other processes (short: made short of something, read so). False when
        it could not be written (they make it themselves)."""
        ef = _stage_ef()
        if ef is None:
            return False
        meta = {"plans": [{"fid": fid, "half": p.half, "abut": list(p.abut), "road_half": p.road_half,
                           "rid": int(p.rid), "rank": int(getattr(p, "rank", -1)),
                           "max_wall": float(p.max_wall), "cut": p.cut is not None,
                           "bank": getattr(p, "bank", None) is not None, "steep": bool(getattr(p, "steep", False)),
                           "widen": getattr(p, "widen", None) is not None}
                          for fid, p in plans],
                "keeps": [float(hw) for _xy, hw in keeps], "short": bool(short)}
        arrays = {}
        try:
            _pack(arrays, "p", [p.xy for _f, p in plans])
            _pack(arrays, "z", [p.z for _f, p in plans])
            _pack(arrays, "c", [p.cut for _f, p in plans if p.cut is not None])
            _pack(arrays, "b", [p.bank for _f, p in plans if getattr(p, "bank", None) is not None])
            _pack(arrays, "w", [p.widen for _f, p in plans if getattr(p, "widen", None) is not None])
            _pack(arrays, "k", [xy for xy, _hw in keeps])
            rows = [(n, a.dtype.str, [int(d) for d in a.shape], np.ascontiguousarray(a).tobytes())
                    for n, a in arrays.items()]
            ok = bool(ef.stage_put(self._stage_path(key), json.dumps(meta).encode("utf-8"), rows, os.getpid()))
        except (OSError, ValueError, TypeError):    # (plans of mixed arrays, a full disk: each process makes its own)
            return False
        if ok:
            with self._lock:
                self.stats["stage_written"] = self.stats.get("stage_written", 0) + 1
        return ok

    def stage_load(self, key):
        """(plans, keeps, short) of cell key's road stage as a process of this run made it, or None."""
        from grading import Plan, set_widen
        ef = _stage_ef()
        if ef is None:
            return None
        try:
            got = ef.stage_get(self._stage_path(key))
            if got is None:
                return None
            meta = json.loads(bytes(got[0]).decode("utf-8"))
            f = {n: np.frombuffer(data, np.dtype(dt)).reshape([int(d) for d in shape])
                 for n, dt, shape, data in got[1]}
            lists = {g: (_split(f, g) if g + "_n" in f else []) for g in ("p", "z", "c", "b", "w", "k")}
            plans, cuts, banks, widens = [], iter(lists["c"]), iter(lists["b"]), iter(lists["w"])
            for m, xy, z in zip(meta["plans"], lists["p"], lists["z"]):
                pl = Plan(xy, z, m["half"], tuple(m["abut"]), next(cuts) if m["cut"] else None, m["road_half"])
                pl.rid, pl.max_wall = m["rid"], m["max_wall"]
                pl.rank = int(m.get("rank", -1))
                pl.steep = bool(m.get("steep", False))
                pl.bank = next(banks) if m.get("bank") else None
                if m.get("widen"):
                    set_widen(pl, next(widens))
                plans.append((m["fid"], pl))
            if len(plans) != len(meta["plans"]) or len(lists["k"]) != len(meta["keeps"]):
                return None
            keeps = list(zip(lists["k"], meta["keeps"]))
        except (OSError, ValueError, KeyError, TypeError, StopIteration):   # (damaged: made here)
            return None
        with self._lock:
            self.stats["stage_read"] = self.stats.get("stage_read", 0) + 1
        return plans, keeps, bool(meta.get("short"))


    # ---- carriageway regions (grading._Planner._cw_region): solved once a run, read by every process ----

    def _region_path(self, tag, home):
        return os.path.join(self.dir, "cw", tag, f"{int(home[0])}_{int(home[1])}_{int(home[2])}.npz")

    def region_load(self, tag, home):
        """{"parts": [(key, line, is_bridge, cls, fid)], "prof": {key: (xy, z)}} of region home (planner frame tag) or
        None."""
        try:
            with np.load(self._region_path(tag, home), allow_pickle=False) as f:
                meta = json.loads(bytes(f["meta"]).decode("utf-8"))
                lines, cx, cz = _split(f, "l"), _split(f, "x"), _split(f, "q")
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):   # (damaged: planned again)
            return None
        parts, prof = [], {}
        for m, ln in zip(meta["parts"], lines):
            key = tuple([m["key"][0]] + [float(v) for v in m["key"][1:]])
            parts.append((key, ln, bool(m["br"]), m["cls"], m["fid"], float(m.get("lv", 1.0 if m["br"] else 0.0)),
                          float(m.get("half", 5.0))))
            if m["chain"] >= 0:
                prof[key] = (cx[m["chain"]], cz[m["chain"]])
        return {"parts": parts, "prof": prof}

    def region_store(self, tag, home, got):
        chains, idx = [], {}
        for key, (xy, z) in got["prof"].items():
            if id(xy) not in idx:
                idx[id(xy)] = len(chains)
                chains.append((xy, z))
        meta = {"parts": [{"key": list(q[0]), "br": bool(q[2]), "cls": q[3],
                           "fid": q[4] if isinstance(q[4], (str, int, type(None))) else str(q[4]),
                           "lv": float(q[5]) if len(q) > 5 else (1.0 if q[2] else 0.0),
                           "half": float(q[6]) if len(q) > 6 else 5.0,
                           "chain": idx[id(got["prof"][q[0]][0])] if q[0] in got["prof"] else -1}
                          for q in got["parts"]]}
        arrays = {"meta": np.frombuffer(json.dumps(meta).encode("utf-8"), np.uint8)}
        try:
            _pack(arrays, "l", [np.asarray(q[1], np.float64) for q in got["parts"]])
            _pack(arrays, "x", [np.asarray(xy, np.float64) for xy, _z in chains])
            _pack(arrays, "q", [np.asarray(z, np.float64) for _xy, z in chains])
            p = self._region_path(tag, home)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez(fh, **arrays)
            os.replace(tmp, p)
        except (OSError, ValueError, TypeError):
            return False
        return True

    def region_claim(self, tag, home):
        """As claim, for a carriageway region."""
        return xproc.once(self._region_path(tag, home) + ".lock", lambda: self.region_load(tag, home), WAIT_S)

    def region_release(self, tag, home):
        xproc.release(self._region_path(tag, home) + ".lock")


def _newest(d, depth=2):
    """The newest mtime of folder d and the folders up to depth levels below it (a run's anchor and signature
    folders: new cells change only their own folder's time)."""
    t = os.path.getmtime(d)
    if depth:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        t = max(t, _newest(e.path, depth - 1))
        except OSError:
            pass
    return t


def prune(root, keep_s=KEEP_S, now=None):
    """Remove the run folders under root not written to for keep_s (server start)."""
    now = time.time() if now is None else now
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    n = 0
    for name in names:
        d = os.path.join(root, name)
        try:
            if now - _newest(d) > keep_s:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n
