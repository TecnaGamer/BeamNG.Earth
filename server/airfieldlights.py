"""Airfield lights: a lit runway's edge, threshold and end lights and an airport's blue taxiway edge lights, as points
of light for the mod's glow items (the street lamps' lampglow mechanism: one forest item per light, a glowing ball,
night only; GROUPS names the group of each colour). Pure: lat / lon points, no heights (tile_rows takes the ground).

runway_lights(rw) -> Lights: only a runway with lighted set and not closed (nor on water). In the runway's frame
(runwaygeom: s along it from the low-numbered end, t across): white edge lights every EDGE_SPACING_M (a whole number of
equal steps, a light at each end) EDGE_OUT_M outside the edge both sides, the last min(CAUTION_M, a third) of each end
yellow (ICAO Annex 14's caution zone); a green threshold bar across the runway at each threshold (the
displaced one, when the data has it) and a red end bar BAR_OUT_M beyond each end, a light every BAR_SPACING_M across.
Approach lights and PAPI (kinds "approach", "papi", in runway_lights): before each end of a lit paved runway at a large
airport or over APPROACH_MIN_RUNWAY_M, a white centreline row from APPROACH_STEP_M out every APPROACH_STEP_M to
APPROACH_LEN_M (a 900 m line; below: barrettes) with crossbars of APPROACH_BAR_W_M at APPROACH_BAR2_AT_M and APPROACH_BAR_AT_M; at every lit paved runway of
PAPI_MIN_RUNWAY_M or more a PAPI at each landing end: four lights PAPI_STEP_M apart, the first PAPI_OFF_M left of the
runway edge (as the landing aircraft sees it) PAPI_AT_M past the threshold, the two nearer the runway red and the two
outer white (the on-slope look). The lights stand on the ground (the extended centreline is not checked for water,
roads or buildings by runway_lights: tile_rows leaves out what stands on them, below).
ils_masts(rw) -> [Mast]: the ILS aerials of a paved open runway over ILS_MIN_RUNWAY_M, forest rows of the mod's static
groups ils_loc (a localizer array, a 30 m wide, 3 m high row of dipoles on a frame) and ils_gs (a 15 m glide-slope lattice
mast): the localizer of each approach LOC_AT_M past the far end on the centreline (between the approach lights' rows)
facing down the runway, the glide-slope mast GS_OFF_M off the centreline GS_AT_M past the threshold, on the side opposite
the PAPI, facing the approach. Shapes: tools/build_ils.py, trees.lua CATALOG (origin on the ground, front local +Y).
Approach barrettes, red side rows, touchdown zone lights and wing bars (the user, 2026-11-17: the lights read thin beside the
photographs, fork/refs/lights l1, l2; ICAO Annex 14 5.3 and FAA AC 150/5340-30 for the layout): the approach line is a white
barrette (APPROACH_BARRETTE_N lights APPROACH_BARRETTE_STEP_M apart across the centreline) every APPROACH_STEP_M to
APPROACH_LEN_M with crossbars at APPROACH_BAR2_AT_M and APPROACH_BAR_AT_M. On a CAT II / III runway (over CAT_MIN_RUNWAY_M or at a
large airport) red side-row barrettes (SIDE_ROW_N lights, SIDE_ROW_OFF_M off the centreline) over the last SIDE_ROW_LEN_M before
the threshold, and white touchdown zone barrettes (kind "tdz": TDZ_N lights, TDZ_OFF_M off the centreline) every TDZ_STEP_M over
the first min(TDZ_LEN_M, half the runway) of each end. A green wing bar (kind "wing": WING_N lights WING_STEP_M apart outward from
the edge lights' line) each side of every threshold of a lit runway. runway_lights(rw, far=True): each barrette its middle light
(the far tiles).
taxiway_lights(feats, runways) -> Lights: blue lights TAXI_SPACING_M apart along both edges (TAXI_OUT_M outside) of the
paved taxiways (infrastructure Features: subtype "airport", class "taxiway"; the width, the unpaved test and the
defaults are aeroways') at airports whose nearest open runway (within TAXI_REACH_M) is lighted; none inside an apron,
inside a runway's half width + RUNWAY_CLEAR_M, nor within another taxiway's or taxilane's pavement (a junction's, the
inner side of a tight bend) nor on a way tagged lit=no.
tile_rows(frame, box, infra_fn, runways_fn, elev, keys=None, roads_fn=None, buildings_fn=None, masts=True, skip=(),
far=False) -> forest rows
[group, x, y, z, yaw, scale, height, rank] of the lights a road tile owns (the group's shape is 1 m high: height 1), as
areaprops' rows, and of the ILS masts (their own groups, the catalogue's height, the yaw the server's, as windsocks').
The tile's own clearance (_cleared, from the data of the cells round the equipment: aeroways.pads without the runways'
strips, the buildings' footprints, windsocks.road_lines): an approach light, a PAPI unit or an ILS aerial that falls on
an apron or taxiway pad, inside a building (an aerial's own half width round it too) or within CLEAR_ROAD_M of a road
line is left out, and the approach row of that end with it from that light out (a localizer array stood on Brisbane's
domestic apron, 315 m past a runway end). No approach lights nor ILS aerials for a runway of a closed airport.

far_points(frame, box, infra_fn, runways_fn, elev) -> [x, y, z, c] of the same lights for the far and skyline tiles (no ILS
aerials, one light a barrette, none of the FAR_SKIP kinds: the counts), which carry them as glow points beside their street lamps
(blockworker._lamps_by_geotile; the mod's roads.lua draws them in these groups, to the street lamps' distance).
lights_of_cells(keys, la, lo, infra_fn, runways_fn, ...) -> (Lights, [Mast]): tile_rows' lights before they are rows (tools/airport_map.py
--lights draws them).

taxiway_centre_lights(feats_list, runways) -> Lights: the lights of the taxiways' centrelines as PAINTED (taxiwaymarks._mapped_runs:
smoothed, cut at the runways) at the airports the blue edge lights are at (a taxiway, not a taxilane, not lit=no, its nearest open
runway lit; the user, 2026-11-17: "taxiway centreline lights"). Green centre lights (kind "taxicl"), 30 m apart on the straights
and TAXI_CURVE_STEP_M where the line turns (ICAO Annex 14 5.3: at most 30 m, closer on curves); none on a stand's lead-in (those
are standpaths', not mapped ways) nor on a runway but the lead-on / lead-off part: where a line meets a runway, from the runway's
end of it back to the way's runway holding position (LEAD_DEFAULT_M without one) lights LEAD_SPACING_M apart, alternately green
and yellow, the first (at the runway) green (kind "lead"). At each holding position (taxiwaymarks' sites, where the marking is
painted) a red stop bar (kind "stopbar": a light every STOPBAR_STEP_M across the taxiway) and runway guard lights (kind "guard": a
yellow pair GUARD_PAIR_M apart each side, GUARD_OUT_M outside the edge, GUARD_LIFT_M higher than the rest as the elevated units are;
steady: the glow balls do not flash). The lights of a list of Features are kept on its taxiwaymarks Scene. A tile takes them from
the whole airport's Scene (airportscene: every cell of the airport, made once and cut to the tile; the user, 2026-11-17: "build each
airport's scene once"; airport_centre_lights), from its own cells' only where the airport could not be read whole.

Every light is a pure function of its runway's data or its Features, kept: a runway's by its data's signature (a bounded
module cache), a Features' on the Features by the runways it was asked with (so every tile of a cell shares them). A
cell with no airport costs one membership test; a Features of anywhere else gives nothing.
"""
import logging
import math

import numpy as np

import aeroways
import dmath
import dnp
import geotiles
import polygon
import runwaygeom

log = logging.getLogger(__name__)

WHITE, GREEN, RED, BLUE, YELLOW = "white", "green", "red", "blue", "yellow"
COLOURS = (WHITE, GREEN, RED, BLUE, YELLOW)
EDGE, THRESHOLD, END, TAXI, APPROACH, PAPI, CENTRE = "edge", "threshold", "end", "taxi", "approach", "papi", "centre"
TAXICL, LEAD, STOPBAR, GUARD, TDZ, WING = "taxicl", "lead", "stopbar", "guard", "tdz", "wing"
KINDS = (EDGE, THRESHOLD, END, TAXI, APPROACH, PAPI, CENTRE, TAXICL, LEAD, STOPBAR, GUARD, TDZ, WING)   # (new kinds last:
                                                                                  # the rows' kind numbers stay)
# The mod's group per colour: one glow ball shape + tinted night materials each (tools/build_lamp_glow.py's balls in the
# colour: nightlights.lua MATERIALS / trees.lua CATALOG, GROUP_MAX_M, groupOff, setGroupEnabled at dusk).
GROUPS = {c: "afl_" + c for c in COLOURS}
ILS_GROUPS = {"loc": "ils_loc", "gs": "ils_gs"}      # the ILS aerials' groups (tools/build_ils.py, trees.lua CATALOG)
ILS_H = {"ils_loc": 3.0, "ils_gs": 15.0}             # m: their catalogue heights (the shapes' tops)
GROUP_H = 1.0                   # m: the catalog height of the glow shapes (a row's height over it is its scale)
LIFT_M = 0.3                    # m: a light's centre over the ground

EDGE_SPACING_M = 60.0           # runway edge lights (FAA AC 150/5340-30 / ICAO: at most 60 m)
EDGE_OUT_M = 1.0                # m outside the runway edge
CAUTION_M = 600.0               # the yellow end zone of the edge lights (or a third of the runway: the shorter)
APPROACH_MIN_RUNWAY_M = 1800.0  # approach lights on a runway longer than this (or at a large airport)
CENTRE_SPACING_M = 30.0         # runway centreline lights (ICAO 5.3.12: 15 m on the precision runways, 30 m on the others;
                                # 15 m read too dense in game, the user, 2026-11-17: "twice as spaced out"), on the runways
                                # with approach lights, white threshold to threshold (the user, 2026-11-17: "no center
                                # runway line"; the balls shine every way, so not the ICAO red / white end coding, which
                                # would show red at the end a landing aircraft touches down at)
CAT_MIN_RUNWAY_M = 2400.0       # the CAT II / III extras (red side rows, touchdown zone lights): a runway longer than this or at a
                                # large airport (the user, 2026-11-17)
APPROACH_LEN_M = 900.0          # m: the approach lights' line (from the runway's end out)
APPROACH_STEP_M = 30.0          # m between the barrettes (ICAO Annex 14: one every 30 m, the CAT I to III centreline)
APPROACH_BARRETTE_N = 5         # a barrette: this many white lights across the centreline, 1 m apart (the photographs, l1 / l2)
APPROACH_BARRETTE_STEP_M = 1.0
APPROACH_BAR_AT_M = 300.0       # the crossbar's distance from the end
APPROACH_BAR2_AT_M = 150.0      # the second crossbar (CAT II / III: crossbars at 150 m and 300 m)
APPROACH_BAR_W_M = 30.0         # their width, a light every APPROACH_BAR_STEP_M
APPROACH_BAR_STEP_M = 3.0
SIDE_ROW_LEN_M = 270.0          # the red side-row barrettes: over the last 270 m before the threshold (ICAO: 270 m of the CAT II / III
SIDE_ROW_N = 3                  # ... system), SIDE_ROW_N lights across (1 m apart), their middle SIDE_ROW_OFF_M either side of the centre
SIDE_ROW_OFF_M = 9.0            # ... (in line with the touchdown zone barrettes); none where the 150 m crossbar stands
TDZ_LEN_M = 900.0               # touchdown zone lights: over this far past each threshold or half the runway, the shorter (Annex 14)
TDZ_STEP_M = 30.0               # ... a barrette pair every 30 m (the first at one step), TDZ_N lights each, their middle
TDZ_N = 3                       # ... TDZ_OFF_M either side of the centreline
TDZ_OFF_M = 9.0
WING_N = 5                      # threshold wing bars (Annex 14: at least five lights, 10 m, outward from the edge lights' line):
WING_STEP_M = 2.5               # ... green, each side of the threshold, the innermost WING_IN_M outside the edge lights' line (the
WING_IN_M = 1.0                 # ... edge light that stands at the threshold is not one of them: lights are one a half metre)
PAPI_MIN_RUNWAY_M = 900.0       # a PAPI on a lit paved runway this long (300 m past each threshold: the units clear)
PAPI_AT_M = 300.0               # m past the threshold
PAPI_OFF_M = 15.0               # the first unit this far outside the runway's edge, on the landing aircraft's left
PAPI_STEP_M = 9.0               # m between the four units
ILS_MIN_RUNWAY_M = 1800.0       # the ILS aerials on a runway longer than this
LOC_AT_M = 315.0                # m past the far end: the localizer (between the approach lights' 300 m and 330 m rows)
GS_AT_M = 300.0                 # m past the threshold: the glide-slope mast
GS_OFF_M = 120.0                # m off the runway's centreline
FACE_M = 10.0                   # m: a mast's second point (the way its front faces)
CLEAR_ROAD_M = 5.0              # m: approach lights, PAPIs and ILS aerials stand at least this far from a road line ...
MAST_CLEAR_M = {"ils_loc": 15.0, "ils_gs": 4.0}     # ... an aerial's own half width clear of pavement and buildings
CLEAR_CELL_PAD_DEG = 0.0004     # (about 45 m) the data's reach round the equipment, in map cells
BAR_SPACING_M = 4.0             # across the threshold and end bars
BAR_OUT_M = 1.0                 # the end bar this far beyond the runway end
DISPLACED_MAX = 1.0 / 3.0       # a displaced threshold past this share of the runway is a data error: none
TAXI_SPACING_M = 30.0           # taxiway edge lights (straight parts; ICAO 5.3.18: at most 60 m, 7.5 m on curves)
TAXI_OUT_M = 1.0                # m outside the taxiway's edge
TAXI_CURVE_STEP_M = 15.0        # taxiway centre lights where the painted line turns (ICAO 7.5 to 15 m on curves; 15 m reads well)
TAXI_CURVE_TURN_RAD = 0.1       # ... the line turns at least this over TAXI_CURVE_WIN_M (a radius under about 150 m)
TAXI_CURVE_WIN_M = 15.0
LEAD_SPACING_M = 15.0           # the lead-on / lead-off lights (ICAO: at most 15 m), alternately green and yellow from the runway
LEAD_DEFAULT_M = 60.0           # ... as far from the runway's end of the line as its holding position (no mapped one: this far)
LEAD_NEAR_M = 2.5               # ... the end of a painted run within this of a runway's edge meets it (taxiwaymarks cuts a way
                                # ... RUNWAY_GAP_M = 1 m outside the edge, or runs it on to the centreline)
LEAD_HOLD_NEAR_M = 2.0          # ... a holding position within this of the run is on it
LEAD_HOLD_MAX_M = 160.0         # ... and within this of the runway's end of it (taxiwaymarks.HOLD_MAX_M and the run's end)
STOPBAR_STEP_M = 3.0            # stop bar: red lights across the taxiway at most this far apart (ICAO Annex 14: 3 m)
GUARD_OUT_M = 3.0               # runway guard lights: a pair each side, this far outside the taxiway's edge ...
GUARD_PAIR_M = 1.5              # ... the pair this far apart along the taxiway (the wig-wag's two lamps)
GUARD_LIFT_M = 1.0              # ... on their posts: this far over the other lights (LIFT_KIND_M)
CENTRE_REACH_M = 280.0           # the cells a road tile's centre lights are made from: those its painted markings read (taxiwaymarks.REACH_M)
DEDUP_M = 6.0                   # two centre lights this near each other (two pieces at a junction) are one
LIFT_KIND_M = {GUARD: GUARD_LIFT_M}         # m a kind stands over the others' LIFT_M
TAXI_REACH_M = 5000.0           # the nearest open runway is further than this: no airport here
RUNWAY_CLEAR_M = 3.0            # m beyond a runway's half width / ends where a taxiway's lights are left out
PAVEMENT_CLEAR_M = 0.5          # m beyond another way's half width
CACHE_MAX = 512                 # runway cache entries
_KEY = "_airfield_lights"
_CELL_M = 150.0                 # m: the squares the pavement test groups candidates in
_RUNWAYS = {}


class Lights:
    """Light points: lat, lon (float64 (n,)), colour and kind (int8 (n,): indexes of COLOURS and KINDS)."""
    __slots__ = ("lat", "lon", "colour", "kind")

    def __init__(self, lat=(), lon=(), colour=(), kind=()):
        self.lat = np.asarray(lat, np.float64).reshape(-1)
        self.lon = np.asarray(lon, np.float64).reshape(-1)
        self.colour = np.asarray(colour, np.int8).reshape(-1)
        self.kind = np.asarray(kind, np.int8).reshape(-1)

    def __len__(self):
        return len(self.lat)

    def rows(self):
        """[(lat, lon, colour name, kind name)]."""
        return [(float(a), float(o), COLOURS[c], KINDS[k]) for a, o, c, k in
                zip(self.lat, self.lon, self.colour.tolist(), self.kind.tolist())]

    def select(self, mask):
        """The lights where the boolean mask is true."""
        return Lights(self.lat[mask], self.lon[mask], self.colour[mask], self.kind[mask])

    @staticmethod
    def join(parts):
        parts = [p for p in parts if len(p)]
        if not parts:
            return Lights()
        return Lights(*(np.concatenate([getattr(p, f) for p in parts]) for f in Lights.__slots__))


# ------------------------------------------------------------------------------------------------- the runways
def _signature(rw):
    return (rw.key, bool(rw.lighted), bool(rw.closed), rw.le.lat, rw.le.lon, rw.he.lat, rw.he.lon, float(rw.width_m),
            rw.le.displaced_m, rw.he.displaced_m, bool(rw.paved), rw.airport_type, rw.surface)


def _on(rw):
    """Is the runway one with lights (lighted, open, on land)?"""
    return bool(rw.lighted) and not rw.closed and rw.surface != "water"


def _bar(s, width, colour, kind):
    """Lights across the runway at s: BAR_SPACING_M apart from edge to edge, as (s, t, colour, kind) lists."""
    t = np.linspace(-width / 2.0, width / 2.0, max(1, math.ceil(width / BAR_SPACING_M)) + 1)
    return np.full(len(t), s), t, np.full(len(t), COLOURS.index(colour)), np.full(len(t), KINDS.index(kind))


def _displaced(end, L):
    """An end's displaced threshold length (0 when the data has none or a doubtful one)."""
    d = float(end.displaced_m or 0.0)
    return d if 0.0 < d <= L * DISPLACED_MAX else 0.0


def _row(s, t, colour, kind):
    """Lights at (s, t) (arrays or numbers) of one colour and kind, as (s, t, colour, kind) lists."""
    s, t = np.atleast_1d(np.asarray(s, np.float64)), np.atleast_1d(np.asarray(t, np.float64))
    s, t = np.broadcast_arrays(s, t)
    return s.copy(), t.copy(), np.full(len(s), COLOURS.index(colour)), np.full(len(s), KINDS.index(kind))


def _has_approach(rw, L):
    """Does the runway get approach lights and centreline lights: paved, open, over APPROACH_MIN_RUNWAY_M or at a large airport?"""
    return bool(rw.paved and rw.airport_type != "closed" and (L > APPROACH_MIN_RUNWAY_M or rw.airport_type == "large_airport"))


def _is_cat(rw, L):
    """Does it get the CAT II / III extras (red side rows, touchdown zone lights): an approach runway over CAT_MIN_RUNWAY_M or at a
    large airport (the user, 2026-11-17)?"""
    return _has_approach(rw, L) and (L > CAT_MIN_RUNWAY_M or rw.airport_type == "large_airport")


def _across(n, step):
    """n offsets step apart about 0."""
    return (np.arange(n, dtype=np.float64) - (n - 1) / 2.0) * step


def _approach_parts(rw, L, far=False):
    """The approach lights before each end, the PAPIs at each landing end, as (s, t, colour, kind) lists. The approach: a white barrette
    (APPROACH_BARRETTE_N lights across, the middle one on the centreline: so the old single row's light is still there) every
    APPROACH_STEP_M to APPROACH_LEN_M, crossbars at APPROACH_BAR2_AT_M and APPROACH_BAR_AT_M (ICAO Annex 14 / FAA AC 150/5340-30: the
    CAT II / III pattern of the photographs); on a CAT runway red side-row barrettes either side over the last SIDE_ROW_LEN_M."""
    parts = []
    if _has_approach(rw, L):
        k = np.arange(1, int(round(APPROACH_LEN_M / APPROACH_STEP_M)) + 1) * APPROACH_STEP_M
        barrette = _across(1 if far else APPROACH_BARRETTE_N, APPROACH_BARRETTE_STEP_M)
        n_bar = int(round(APPROACH_BAR_W_M / APPROACH_BAR_STEP_M))
        bar = np.linspace(-APPROACH_BAR_W_M / 2.0, APPROACH_BAR_W_M / 2.0, n_bar + 1)
        bar = bar[np.abs(bar) > float(np.abs(barrette).max()) + 1e-6]      # (the barrette at the middle of the bar stands there)
        rows = k[(k <= SIDE_ROW_LEN_M + 1e-6) & (np.abs(k - APPROACH_BAR2_AT_M) > 1e-6)] if _is_cat(rw, L) else k[:0]
        side = np.concatenate([sgn * SIDE_ROW_OFF_M + _across(1 if far else SIDE_ROW_N, APPROACH_BARRETTE_STEP_M)
                               for sgn in (-1.0, 1.0)])
        for sign, base in ((-1.0, 0.0), (1.0, L)):
            parts.append(_row(np.repeat(base + sign * k, len(barrette)), np.tile(barrette, len(k)), WHITE, APPROACH))
            for at in (APPROACH_BAR2_AT_M, APPROACH_BAR_AT_M):
                parts.append(_row(base + sign * at, bar, WHITE, APPROACH))
            if len(rows):
                parts.append(_row(np.repeat(base + sign * rows, len(side)), np.tile(side, len(rows)), RED, APPROACH))
    if rw.paved and L >= PAPI_MIN_RUNWAY_M:
        # (the landing aircraft's left: +t flying toward the high end, -t flying back)
        for at, side in ((_displaced(rw.le, L) + PAPI_AT_M, 1.0), (L - _displaced(rw.he, L) - PAPI_AT_M, -1.0)):
            for i in range(4):
                parts.append(_row(at, side * (rw.width_m / 2.0 + PAPI_OFF_M + i * PAPI_STEP_M), RED if i < 2 else WHITE, PAPI))
    return parts


def _tdz_parts(rw, L, far=False):
    """The touchdown zone lights of a CAT runway: white barrettes (TDZ_N lights, the middle TDZ_OFF_M either side of the centreline)
    every TDZ_STEP_M from the (displaced) threshold over min(TDZ_LEN_M, half the runway) of each end (Annex 14: not past the middle
    of a runway under 1800 m, so the two ends' do not overlap), as (s, t, colour, kind) lists."""
    if not _is_cat(rw, L):
        return []
    parts = []
    side = np.concatenate([sgn * TDZ_OFF_M + _across(1 if far else TDZ_N, APPROACH_BARRETTE_STEP_M) for sgn in (-1.0, 1.0)])
    for at_le, end in ((True, rw.le), (False, rw.he)):
        d = _displaced(end, L)
        reach = min(TDZ_LEN_M, L / 2.0 - d)
        k = np.arange(1, int(math.floor(reach / TDZ_STEP_M + 1e-9)) + 1) * TDZ_STEP_M
        if len(k):
            s = d + k if at_le else L - d - k
            parts.append(_row(np.repeat(s, len(side)), np.tile(side, len(k)), WHITE, TDZ))
    return parts


def _wing_parts(rw, L):
    """The threshold wing bars: WING_N green lights WING_STEP_M apart outward from the runway edge lights' line (the first WING_IN_M
    outside it), each side of each (displaced) threshold, as (s, t, colour, kind) lists."""
    out = np.arange(WING_N) * WING_STEP_M + rw.width_m / 2.0 + EDGE_OUT_M + WING_IN_M
    t = np.r_[-out, out]
    return [_row(np.repeat([_displaced(rw.le, L), L - _displaced(rw.he, L)], len(t)), np.tile(t, 2), GREEN, WING)]


def _centre_parts(rw, L):
    """The runway centreline lights, every CENTRE_SPACING_M between the (displaced) thresholds on a paved runway with
    approach lights, as (s, t, colour, kind) lists."""
    if not _has_approach(rw, L):
        return []
    a, b = _displaced(rw.le, L), L - _displaced(rw.he, L)
    n = max(1, int(math.floor((b - a) / CENTRE_SPACING_M + 1e-9)))
    s = a + (b - a - n * CENTRE_SPACING_M) / 2.0 + np.arange(n + 1) * CENTRE_SPACING_M     # (centred: even at both ends)
    return [_row(s, 0.0, WHITE, CENTRE)]


def _lights_of(rw, far=False):
    g = runwaygeom.geometry(rw)
    L, W = g.length, g.width
    n = max(1, math.ceil(L / EDGE_SPACING_M))
    s = np.linspace(0.0, L, n + 1)
    zone = min(CAUTION_M, L / 3.0)
    colour = np.where((s < zone - 1e-6) | (s > L - zone + 1e-6), COLOURS.index(YELLOW), COLOURS.index(WHITE))
    parts = [(np.r_[s, s], np.r_[np.full(len(s), -(W / 2.0 + EDGE_OUT_M)), np.full(len(s), W / 2.0 + EDGE_OUT_M)],
              np.r_[colour, colour], np.full(2 * len(s), KINDS.index(EDGE)))]
    for at_le, end in ((True, rw.le), (False, rw.he)):
        d = _displaced(end, L)
        sign = 1.0 if at_le else -1.0
        base = 0.0 if at_le else L
        parts.append(_bar(base + sign * d, W, GREEN, THRESHOLD))
        parts.append(_bar(base - sign * BAR_OUT_M, W, RED, END))
    parts += _approach_parts(rw, L, far)
    parts += _centre_parts(rw, L)
    parts += _tdz_parts(rw, L, far)
    parts += _wing_parts(rw, L)
    ss, tt, cc, kk = (np.concatenate(p) for p in zip(*parts))
    lat, lon = g.latlon(ss, tt)
    return Lights(lat, lon, cc, kk)


def runway_lights(rw, far=False):
    """The Lights of a runway (airports.Runway), none unless lighted and open; memoised by its data. far: for the far tiles, each
    barrette (approach, side row, touchdown zone) one light, its middle one (the far tiles carry every light as a point, and a
    barrette's lights are a metre apart: from kilometres off it is one)."""
    if not _on(rw):
        return Lights()
    key = _signature(rw) + (bool(far),)
    got = _RUNWAYS.get(key)
    if got is None:
        if len(_RUNWAYS) >= CACHE_MAX:
            _RUNWAYS.clear()
        got = _RUNWAYS[key] = _lights_of(rw, far)
    return got


# ------------------------------------------------------------------------------------------------- the ILS aerials
class Mast:
    """An ILS aerial: its group, its point, and a second point 10 m in front of it (lat_to, lon_to: the way it faces)."""
    __slots__ = ("group", "lat", "lon", "lat_to", "lon_to")

    def __init__(self, group, lat, lon, lat_to, lon_to):
        self.group, self.lat, self.lon, self.lat_to, self.lon_to = group, float(lat), float(lon), float(lat_to), float(lon_to)


_MASTS = {}


def _masts_of(rw):
    g = runwaygeom.geometry(rw)
    L = g.length
    out = []
    # (the localizer past the far end faces down the runway; the glide-slope mast faces the approach, off the side the
    # PAPI is not on: landing toward the high end the PAPI is at +t, back the other way at -t)
    for at, face, t, group in (
            (L + LOC_AT_M, -1.0, 0.0, ILS_GROUPS["loc"]),
            (-LOC_AT_M, 1.0, 0.0, ILS_GROUPS["loc"]),
            (_displaced(rw.le, L) + GS_AT_M, -1.0, -GS_OFF_M, ILS_GROUPS["gs"]),
            (L - _displaced(rw.he, L) - GS_AT_M, 1.0, GS_OFF_M, ILS_GROUPS["gs"])):
        la, lo = (float(a[0]) for a in g.latlon([at], [t]))
        la2, lo2 = (float(a[0]) for a in g.latlon([at + face * FACE_M], [t]))
        out.append(Mast(group, la, lo, la2, lo2))
    return out


def ils_masts(rw):
    """The ILS aerials [Mast] of a runway (airports.Runway): none unless paved, open, on land and over ILS_MIN_RUNWAY_M;
    memoised by its data."""
    if not rw.paved or rw.closed or rw.airport_type == "closed" or rw.surface == "water" \
            or runwaygeom.geometry(rw).length <= ILS_MIN_RUNWAY_M:
        return []
    key = _signature(rw)
    got = _MASTS.get(key)
    if got is None:
        if len(_MASTS) >= CACHE_MAX:
            _MASTS.clear()
        got = _MASTS[key] = _masts_of(rw)
    return got


# ------------------------------------------------------------------------------------------------- the taxiways
def _width(tags, cls, intl):
    """A taxiway's or taxilane's width: the tag's, else aeroways' default by class and airport."""
    w = aeroways.width_tag(tags.get("width"))
    if w is not None:
        return w
    if cls == "taxiway":
        return aeroways.TAXIWAY_INTL_M if intl else aeroways.TAXIWAY_M
    return aeroways.TAXILANE_INTL_M if intl else aeroways.TAXILANE_M


def _seg_dist_min(p, a, b, margin):
    """The smallest (distance to a segment a->b (S, 2) - its margin (S,)) of each point p (m, 2): negative inside. Points
    in _CELL_M squares, each against the segments whose box reaches the square (a big airport is thousands of both)."""
    out = np.full(len(p), np.inf)
    lo, hi = np.minimum(a, b) - margin[:, None], np.maximum(a, b) + margin[:, None]
    cell = np.floor(p / _CELL_M).astype(np.int64)
    _, inv = np.unique(cell, axis=0, return_inverse=True)
    for c in range(int(inv.max()) + 1 if len(p) else 0):
        idx = np.flatnonzero(inv == c)
        q = p[idx]
        near = np.flatnonzero(((hi >= q.min(axis=0)) & (lo <= q.max(axis=0))).all(axis=1))
        if not len(near):
            continue
        sa, sb, sm = a[near], b[near], margin[near]
        ab = sb - sa
        ab2 = np.maximum(dnp.vecdot(ab, ab), 1e-12)
        qq = q[:, None, :]
        t = np.clip(dnp.vecdot(qq - sa, ab) / ab2, 0.0, 1.0)
        d = dnp.hypot(*(sa + t[..., None] * ab - qq).transpose(2, 0, 1))
        out[idx] = (d - sm).min(axis=1)
    return out


def _sig(runways):
    return tuple(sorted(_signature(rw) for rw in runways))


def _taxiway_lights(feats, runways):
    sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
    if sub is None or cls is None:
        return Lights()
    air = np.asarray(sub).astype(str) == aeroways.SUBTYPE
    if not air.any():
        return Lights()
    cls = np.asarray(cls).astype(str)
    intl = bool((air & (cls == aeroways.AERODROME)).any())
    land = [rw for rw in runways if rw.surface != "water"]
    open_ = [rw for rw in land if not rw.closed]
    if not open_:
        return Lights()
    lat0, lon0 = open_[0].le.lat, open_[0].le.lon
    kx = aeroways.M_PER_DEG * dmath.cos(math.radians(lat0))

    def local(ll):                          # (n, 2) lon / lat -> metres about the first runway's end
        return np.c_[(ll[:, 0] - lon0) * kx, (ll[:, 1] - lat0) * aeroways.M_PER_DEG]
    rend = [(local(np.array([[rw.le.lon, rw.le.lat], [rw.he.lon, rw.he.lat]])), rw) for rw in open_]
    ways = []                               # (xy (n, 2), half width, lit) of the paved taxiways and taxilanes
    for i in np.flatnonzero(air & np.isin(cls, aeroways.WAYS)):
        if int(feats.kind[i]) != 2:
            continue
        tags = aeroways._tags(feats, i)
        if aeroways._unpaved(feats, i, tags):
            continue
        half = _width(tags, cls[i], intl) / 2.0
        no_lit = str(tags.get("lit", "")).strip().lower() in ("no", "false", "0")
        for part in feats.parts(i):
            for line in part:
                if len(line) < 2:
                    continue
                xy = local(np.asarray(line, np.float64))
                xy = xy[np.r_[True, dnp.hypot(*np.diff(xy, axis=0).T) > 1e-3]]
                if len(xy) < 2:
                    continue
                lit = cls[i] == "taxiway" and not no_lit and _lit_at(xy, rend)
                ways.append((xy, half, bool(lit)))
    if not any(w[2] for w in ways):
        return Lights()
    cand, owner = [], []
    for xy, half, lit in ways:
        if not lit:
            continue
        d = np.diff(xy, axis=0)
        seg = dnp.hypot(d[:, 0], d[:, 1])
        cum = np.r_[0.0, np.cumsum(seg)]
        n = max(1, math.ceil(cum[-1] / TAXI_SPACING_M))
        st = np.linspace(0.0, cum[-1], n + 1)
        k = np.clip(np.searchsorted(cum, st, side="right") - 1, 0, len(seg) - 1)
        p = xy[k] + ((st - cum[k]) / seg[k])[:, None] * d[k]
        nrm = np.c_[-d[k, 1], d[k, 0]] / seg[k][:, None]
        off = nrm * (half + TAXI_OUT_M)
        cand += [p + off, p - off]
    pts = np.concatenate(cand)
    # not within any paved way's pavement (its own other segments at a tight bend, the ways it joins)
    a = np.concatenate([w[0][:-1] for w in ways])
    b = np.concatenate([w[0][1:] for w in ways])
    margin = np.concatenate([np.full(len(w[0]) - 1, w[1] + PAVEMENT_CLEAR_M) for w in ways])
    keep = _seg_dist_min(pts, a, b, margin) >= 0.0
    # nor on a runway (half width + RUNWAY_CLEAR_M, its length + the same at each end)
    for (e, rw) in ((local(np.array([[r.le.lon, r.le.lat], [r.he.lon, r.he.lat]])), r) for r in land):
        v = e[1] - e[0]
        length = float(dnp.hypot(*v))
        if length < 1.0:
            continue
        u = v / length
        q = pts - e[0]
        s, t = dnp.matmul(q, u), q[:, 0] * -u[1] + q[:, 1] * u[0]
        keep &= ~((s > -RUNWAY_CLEAR_M) & (s < length + RUNWAY_CLEAR_M) & (np.abs(t) < rw.width_m / 2.0 + RUNWAY_CLEAR_M))
    pts = pts[keep]
    lat, lon = lat0 + pts[:, 1] / aeroways.M_PER_DEG, lon0 + pts[:, 0] / kx
    # nor inside an apron
    for i in np.flatnonzero(air & (cls == aeroways.APRON)):
        if int(feats.kind[i]) != 3 or not len(lat):
            continue
        for part in feats.parts(i):
            if not part or len(part[0]) < 3:
                continue
            ring = np.asarray(part[0], np.float64)
            inside = ((lon >= ring[:, 0].min()) & (lon <= ring[:, 0].max()) & (lat >= ring[:, 1].min())
                      & (lat <= ring[:, 1].max()))
            if inside.any():
                idx = np.flatnonzero(inside)
                hit = polygon.contains_points(ring[:, :2], np.c_[lon[idx], lat[idx]])
                inside[idx] = hit
                lat, lon = lat[~inside], lon[~inside]
    return Lights(lat, lon, np.full(len(lat), COLOURS.index(BLUE)), np.full(len(lat), KINDS.index(TAXI)))


def _lit_at(xy, rend):
    """Is the way's middle within TAXI_REACH_M of an open runway, the nearest one lighted?"""
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    k = min(int(np.searchsorted(cum, cum[-1] / 2.0, side="right")) - 1, len(seg) - 1)
    m = xy[k] + (cum[-1] / 2.0 - cum[k]) / max(seg[k], 1e-9) * (xy[k + 1] - xy[k])
    best, lit = np.inf, False
    for e, rw in rend:
        v = e[1] - e[0]
        t = float(np.clip(dnp.matmul(m - e[0], v) / max(float(dnp.matmul(v, v)), 1e-9), 0.0, 1.0))
        d = float(dnp.hypot(*(e[0] + t * v - m)))
        if d < best:
            best, lit = d, bool(rw.lighted)
    return lit and best <= TAXI_REACH_M


def taxiway_lights(feats, runways):
    """The Lights of the taxiways in Features feats (see the module docstring); runways: the airports.Runways around
    (their lighting and distance decide, closed ones are no airport's). Kept on the Features by the runways' data."""
    runways = list(runways or ())
    if feats is None or len(feats) == 0 or not runways:
        return Lights()
    cache = feats.__dict__.setdefault(_KEY, {})
    key = _sig(runways)
    got = cache.get(key)
    if got is None:
        if len(cache) >= 8:
            cache.clear()
        got = cache[key] = _taxiway_lights(feats, runways)
    return got


# ------------------------------------------------------------------------------------------------- the taxiways' centre lights
_CENTRE_KEY = "_airfield_centre_lights"        # on the taxiwaymarks Scene's memo
_NOLIT_KEY = "_airfield_unlit_ways"            # on a Features: the ids of its ways tagged lit=no


def _unlit_ids(feats):
    """The ids of the taxiways and taxilanes of Features feats tagged lit=no (kept on it)."""
    got = feats.__dict__.get(_NOLIT_KEY)
    if got is None:
        got = set()
        sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
        if sub is not None and cls is not None and len(feats):
            air = np.asarray(sub).astype(str) == aeroways.SUBTYPE
            cls = np.asarray(cls).astype(str)
            for i in np.flatnonzero(air & np.isin(cls, aeroways.WAYS)):
                if str(aeroways._tags(feats, i).get("lit", "")).strip().lower() in ("no", "false", "0"):
                    got.add(str(feats.ids[i]))
        feats.__dict__[_NOLIT_KEY] = got
    return got


def _way_ids(feats):
    """The ids of the ways of the taxiwaymarks Scene of the Features list feats, in its order (taxiwaymarks._scene: each
    Features' ways whose id is not already read)."""
    import taxiwaymarks
    seen, ids = set(), []
    for f in feats:
        w, r, h, _intl = taxiwaymarks._raw(f)
        ids += [x[0] for x in w if x[0] not in seen]
        seen.update(x[0] for x in w + r + h)
    return ids


def _turn_profile(xy):
    """(cum (k,), the turn (n,) in radians at each metre of the polyline xy (k, 2): the angle between its direction
    TAXI_CURVE_WIN_M / 2 before and after)."""
    d = np.diff(xy, axis=0)
    cum = np.r_[0.0, np.cumsum(dnp.hypot(d[:, 0], d[:, 1]))]
    n = max(int(math.floor(cum[-1])) + 1, 2)
    sf = np.minimum(np.arange(n, dtype=np.float64), cum[-1])
    p = np.c_[np.interp(sf, cum, xy[:, 0]), np.interp(sf, cum, xy[:, 1])]
    v = np.diff(p, axis=0)
    v = v / np.maximum(dnp.hypot(v[:, 0], v[:, 1]), 1e-9)[:, None]
    w = max(1, int(round(TAXI_CURVE_WIN_M / 2.0)))
    j = np.arange(n)
    a, b = v[np.clip(j - w, 0, len(v) - 1)], v[np.clip(j + w, 0, len(v) - 1)]
    cross = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    return cum, dnp.arctan2(np.abs(cross), a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])


def _step_at(turn, s):
    j = min(max(int(s), 0), len(turn) - 1)
    return TAXI_CURVE_STEP_M if turn[j] >= TAXI_CURVE_TURN_RAD else TAXI_SPACING_M


def _stations(turn, lo, hi, first, cap):
    """The distances along a run, from lo to hi: a light at lo when `first`, then each step on (the turn at that metre decides
    the step) and, when `cap`, one at hi if the last is more than half a step short of it."""
    out, s = [], lo
    if first:
        out.append(s)
    while True:
        s += _step_at(turn, s)
        if s > hi + 1e-6:
            break
        out.append(s)
    if cap and hi > lo and (not out or hi - out[-1] > 0.5 * _step_at(turn, hi)):
        out.append(hi)
    return out


def _project(xy, cum, p):
    """(distance, distance along the polyline) of the point p to the nearest point of the polyline xy."""
    a, ab = xy[:-1], xy[1:] - xy[:-1]
    t = np.clip(dnp.vecdot(p - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
    q = a + t[:, None] * ab
    d = dnp.hypot(q[:, 0] - p[0], q[:, 1] - p[1])
    j = int(np.argmin(d))
    return float(d[j]), float(cum[j] + t[j] * (cum[j + 1] - cum[j]))


def _runway_edge_dist(sc, p):
    """The distance from the point p to the nearest runway's edge of the Scene sc (negative inside), inf without a runway."""
    best = np.inf
    for rxy, half, _ref in sc.runways:
        if len(rxy) >= 2:
            a, ab = rxy[:-1], rxy[1:] - rxy[:-1]
            t = np.clip(dnp.vecdot(p - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
            q = a + t[:, None] * ab
            best = min(best, float(dnp.hypot(q[:, 0] - p[0], q[:, 1] - p[1]).min()) - half)
    return best


def _lead_length(sc, wi, xy, cum, at_end):
    """How far from its runway end the lead-on / lead-off lights of a run reach: to the way's runway holding position when
    one lies on the run (taxiwaymarks' sites), else LEAD_DEFAULT_M; at most half the run."""
    total = float(cum[-1])
    best = None
    for _ri, swi, c, _u in sc.sites:
        if swi != wi:
            continue
        d, along = _project(xy, cum, c)
        along = total - along if at_end else along
        if d <= LEAD_HOLD_NEAR_M and 0.0 <= along <= LEAD_HOLD_MAX_M and (best is None or along < best):
            best = along
    return min(LEAD_DEFAULT_M if best is None else best, total / 2.0)


def _centre_points(sc, lit):
    """([(x, y, colour index, kind index)] of the centre and lead-on / lead-off lights, the same of the stop bars and guard
    lights) of the Scene sc's lit ways (a flag per way), in its plane."""
    import taxiwaymarks
    runs = [r for r in taxiwaymarks._mapped_runs(sc) if lit[r[2]] and len(r[0]) >= 2]
    first = np.array([r[0][0] for r in runs], np.float64).reshape(-1, 2)
    la, lo, _ = sc.frame.enu_to_geodetic(first[:, 0], first[:, 1], np.zeros(len(first)))
    # (in the order of the runs' first points, not the order the Features were read in: a junction keeps the same light whichever
    # cells a tile read)
    order = sorted(range(len(runs)), key=lambda i: (round(float(la[i]) * 1e6), round(float(lo[i]) * 1e6), len(runs[i][0])))
    leads, regular = [], []
    green, yellow = COLOURS.index(GREEN), COLOURS.index(YELLOW)
    for i in order:
        xy, _cls, wi, _f, _l = runs[i]
        cum, turn = _turn_profile(xy)
        total = float(cum[-1])
        keep = [0.0, total]                             # (the part of the run the ordinary lights cover: lo, hi)
        for at_end in (False, True):
            end = xy[-1] if at_end else xy[0]
            # (a run's end at a runway: cut a metre outside its edge, or running on to its centreline: lead-on / lead-off)
            if _runway_edge_dist(sc, end) > LEAD_NEAR_M:
                continue
            reach = _lead_length(sc, wi, xy, cum, at_end)
            n = int(math.floor(reach / LEAD_SPACING_M + 1e-9))
            for k in range(n + 1):
                d = k * LEAD_SPACING_M
                leads.append((total - d if at_end else d, xy, cum, green if k % 2 == 0 else yellow, LEAD))
            keep[1 if at_end else 0] = total - n * LEAD_SPACING_M if at_end else n * LEAD_SPACING_M
        hi = keep[1] if keep[1] >= total else keep[1] - 0.5 * _step_at(turn, keep[1])     # (half a step clear of the lead's last light)
        for st in _stations(turn, keep[0], hi, keep[0] <= 0.0, keep[1] >= total):
            regular.append((st, xy, cum, green, TAXICL))
    out, grid = [], {}

    def free(x, y):
        gx, gy = int(math.floor(x / DEDUP_M)), int(math.floor(y / DEDUP_M))
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for px, py in grid.get((gx + a, gy + b), ()):
                    if float(dnp.hypot(x - px, y - py)) < DEDUP_M:
                        return False
        return True
    for s, xy, cum, col, kind in leads + regular:
        x, y = float(np.interp(s, cum, xy[:, 0])), float(np.interp(s, cum, xy[:, 1]))
        if free(x, y):
            grid.setdefault((int(math.floor(x / DEDUP_M)), int(math.floor(y / DEDUP_M))), []).append((x, y))
            out.append((x, y, col, KINDS.index(kind)))
    bars = []
    red, amber = COLOURS.index(RED), COLOURS.index(YELLOW)
    for _ri, wi, c, u in sc.sites:                       # (a runway holding position: where taxiwaymarks paints its marking)
        if not lit[wi]:
            continue
        width = sc.ways[wi][1]
        nv = np.array([-u[1], u[0]])
        for t in np.linspace(-width / 2.0, width / 2.0, int(math.ceil(width / STOPBAR_STEP_M)) + 1):
            q = c + nv * t
            bars.append((float(q[0]), float(q[1]), red, KINDS.index(STOPBAR)))
        for side in (-1.0, 1.0):
            for along in (-GUARD_PAIR_M / 2.0, GUARD_PAIR_M / 2.0):
                q = c + u * along + nv * (side * (width / 2.0 + GUARD_OUT_M))
                bars.append((float(q[0]), float(q[1]), amber, KINDS.index(GUARD)))
    return out, bars


def _taxi_centre(feats, runways, sc=None):
    import taxiwaymarks
    sc = taxiwaymarks.scene(feats) if sc is None else sc
    open_ = [rw for rw in runways if rw.surface != "water" and not rw.closed]
    if sc is None or not open_:
        return Lights()
    ends = np.array([[rw.le.lat, rw.le.lon, rw.he.lat, rw.he.lon] for rw in open_], np.float64)
    e, n, _ = sc.frame.geodetic_to_enu(np.r_[ends[:, 0], ends[:, 2]], np.r_[ends[:, 1], ends[:, 3]], 0.0)
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    m = len(open_)
    rend = [(np.array([[e[i], n[i]], [e[m + i], n[m + i]]]), rw) for i, rw in enumerate(open_)]
    ids = _way_ids(feats)
    unlit = set().union(*(_unlit_ids(f) for f in feats)) if len(ids) == len(sc.ways) else set()
    lit = [cls == "taxiway" and (not unlit or ids[wi] not in unlit) and _lit_at(xy, rend)
           for wi, (xy, _w, _ref, cls) in enumerate(sc.ways)]
    if not any(lit):
        return Lights()
    centre, bars = _centre_points(sc, lit)
    # (no ordinary centre light on a runway, though the mapped line of a taxiway runs over a runway the data does not draw: the
    # lead-on and lead-off lights, the runway's own, are the ones on it)
    taxicl = KINDS.index(TAXICL)
    for e0, rw in rend:
        v = e0[1] - e0[0]
        length = float(dnp.hypot(v[0], v[1]))
        if length < 1.0:
            continue
        u = v / length
        centre = [p for p in centre if p[3] != taxicl or not (
            -LEAD_NEAR_M < float((p[0] - e0[0][0]) * u[0] + (p[1] - e0[0][1]) * u[1]) < length + LEAD_NEAR_M
            and abs(float(-(p[0] - e0[0][0]) * u[1] + (p[1] - e0[0][1]) * u[0])) < rw.width_m / 2.0 + 0.5)]
    pts = centre + bars
    if not pts:
        return Lights()
    arr = np.array([[p[0], p[1]] for p in pts], np.float64)
    la, lo, _ = sc.frame.enu_to_geodetic(arr[:, 0], arr[:, 1], np.zeros(len(arr)))
    return Lights(la, lo, [p[2] for p in pts], [p[3] for p in pts])


def taxiway_centre_lights(feats_list, runways, sc=None):
    """The Lights of the taxiways' centrelines, stop bars and runway guard lights (see the module docstring) of the Features
    list feats_list (the infrastructure Features of the cells round a tile, or of a whole airport: airportscene); runways: the
    airports.Runways around (their lighting and distance decide, as for taxiway_lights). Kept on the Features' taxiwaymarks
    Scene by the runways' data. sc: that Scene where the caller holds it (an Airport's)."""
    runways = list(runways or ())
    feats = [f for f in (feats_list or ()) if f is not None and len(f)]
    if not feats or not runways:
        return Lights()
    import taxiwaymarks
    sc = taxiwaymarks.scene(feats) if sc is None else sc
    if sc is None:
        return Lights()
    cache = sc.memo.setdefault(_CENTRE_KEY, {})
    key = _sig(runways)
    got = cache.get(key)
    if got is None:
        if len(cache) >= 8:
            cache.clear()
        got = cache[key] = _taxi_centre(feats, runways, sc)
    return got


def airport_centre_lights(ap, runways_fn):
    """The centre lights of the whole Airport ap (airportscene): from the runways round any of its cells, sorted, so the answer
    is the airport's and the same for every tile that asks (kept on its Scene by them)."""
    rws = {}
    for k in ap.cells:
        for rw in runways_fn(k) or ():
            rws.setdefault(rw.key, rw)
    return taxiway_centre_lights(ap.feats, [rws[k] for k in sorted(rws)], ap.scene())


# ------------------------------------------------------------------------------------------------- the tile's rows
def _keys_of(frame, box):
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        return mapcells.cells_for_points(la, lo, pad_lat=0.0005, pad_lon=0.0005, max_cells=64), (la, lo)
    except (ValueError, OverflowError):
        return [], (la, lo)


def _reach_keys(frame, box, margin_m):
    """The cells under the box and margin_m round it (roads.cells_of_tile's: the cells a road tile's painted markings read)."""
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - margin_m, x1 + margin_m, x0 - margin_m, x1 + margin_m]),
                                      np.array([y0 - margin_m, y0 - margin_m, y1 + margin_m, y1 + margin_m]), 0.0)
    try:
        return mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    except (ValueError, OverflowError):
        return []


CENTRE_CROP_DEG = 0.001         # (about 110 m) the centre lights of a whole airport kept round the area they are asked for


def _centre_lights(cells, all_cells, infra_fn, runways_fn, la, lo):
    """The Lights of the taxiways' centrelines near the corners la, lo (arrays: the area asked for) from the airports the cells
    touch, each whole (airportscene: made once, the same for every tile). cells: those the area reads (all_cells: every one is
    read; else those with a runway in reach); the cells of an airport that cannot be built whole (airportscene.of_cells:
    None) are read together, as a tile's own scene (taxiway_centre_lights of their Features)."""
    import airportscene
    seeds = [k for k in cells if runways_fn(k)]
    if not seeds:
        return Lights()
    aps = airportscene.of_cells(infra_fn, seeds)
    if aps is None:
        infra, rws, near = [], [], set()
        for k in cells:
            runways = list(runways_fn(k) or ())
            if runways or all_cells:
                infra.append(infra_fn(k))
            for rw in runways:
                if rw.key not in near:
                    near.add(rw.key)
                    rws.append(rw)
        return taxiway_centre_lights(infra, rws)
    out = []
    for ap in aps:
        got = airport_centre_lights(ap, runways_fn)
        if len(got):
            near = ((got.lat >= la.min() - CENTRE_CROP_DEG) & (got.lat <= la.max() + CENTRE_CROP_DEG)
                    & (got.lon >= lo.min() - CENTRE_CROP_DEG) & (got.lon <= lo.max() + CENTRE_CROP_DEG))
            out.append(got.select(near))
    return Lights.join(out)


def lights_of_cells(keys, la, lo, infra_fn, runways_fn, roads_fn=None, buildings_fn=None, masts=True, skip=(), far=False,
                    centre_keys=None):
    """(Lights, [Mast]) of the cells keys (tile_rows' arguments; la, lo: the corners' lat / lon of the area they are for, which the
    clearance of the equipment is looked at in): the runways' lights (cleared), the taxiways' edge and centre lights, stop bars
    and guard lights. skip: the kinds (names of KINDS) left out. far: the far tiles' (runway_lights). centre_keys: the cells whose
    Features the centre lights are made from together (a list of cells with every Features of them, as the painted markings read
    them: taxiwaymarks shares the Scene); default: keys, those of them with a runway in reach."""
    parts, aerials, seen = [], [], {}
    for k in keys:
        runways = list(runways_fn(k) or ())
        if not runways:
            continue
        for rw in runways:
            if rw.key not in seen:
                seen[rw.key] = True
                lights, ms = _cleared(rw, runway_lights(rw, far), ils_masts(rw) if masts else [], la, lo, infra_fn, roads_fn,
                                      buildings_fn)
                parts.append(lights)
                aerials += ms
        parts.append(taxiway_lights(infra_fn(k), runways))
    try:                    # (the painted centrelines' lights: the edge lights are worth more than them)
        parts.append(_centre_lights(keys if centre_keys is None else centre_keys, centre_keys is not None, infra_fn, runways_fn,
                                    la, lo))
    except Exception as ex:
        log.warning("taxiway centre lights: %s: %s", type(ex).__name__, ex)
    lights = Lights.join(parts)
    if skip and len(lights):
        lights = lights.select(~np.isin(lights.kind, [KINDS.index(k) for k in skip]))
    return lights, aerials


def tile_rows(frame, box, infra_fn, runways_fn, elev, keys=None, roads_fn=None, buildings_fn=None, masts=True, skip=(), far=False,
              reach_m=0.0):
    """Forest rows [group, x, y, z, yaw, 1, GROUP_H, 0] of the airfield lights a tile (box: x0, y0, x1, y1 in frame, or
    a geotile's OwnedBox) owns (each light in one tile). infra_fn(key) -> the cell's infrastructure Features (or None),
    runways_fn(key) -> the airports.Runways to light it by (AirportDB.in_cell(key, margin_m=TAXI_REACH_M)), elev(lat,
    lon) -> the (graded) ground in metres (arrays). keys: the box's cells (default: those under it). roads_fn(key) ->
    the cell's roads Features, buildings_fn(key) -> its buildings Features, or a list of them (or None; None for the
    function: no road, no building is kept clear of: the clearance of the equipment is the pavement of infra_fn's).
    masts=False: the lights alone (far_points: no ILS aerial is seen from kilometres off). skip: kinds left out. far: see runway_lights.
    The taxiways' centre lights are made from the Features of all the cells under the box (and reach_m round it, CENTRE_REACH_M for a
    road tile: the cells its painted markings read, whose Scene they share) together: a junction's fillet is the paint's."""
    got = _keys_of(frame, box)
    if keys is None:
        keys = got[0]
    la, lo = got[1]
    centre = _reach_keys(frame, box, reach_m) if reach_m > 0.0 else None
    lights, aerials = lights_of_cells(keys, la, lo, infra_fn, runways_fn, roads_fn, buildings_fn, masts, skip, far, centre)
    return _light_rows(frame, box, lights, elev, la, lo) + _mast_rows(frame, box, aerials, elev, la, lo)


# The airfield lights in the far and skyline tiles (the user, 2026-11-17, at night: "what lets me see city lights all
# the way in the distance, but not all the taxiways or to the end of the runway": the street lamps' far glow came with
# the far tiles, these only with the road tiles' props): [x, y, z, c] points of the same lights (tile_rows' own, without
# the ILS aerials), c the glow group's number in roads.lua's FAR_LAMP_GROUPS (1 + the colour's index in COLOURS; 0 is the
# lamps' lampglow), as buildings.far_lamps' [x, y, z] beside them (blockworker._lamps_by_geotile).
FAR_FORMAT = 3                  # the rows' format, in the key of a far tile that carries them (blockworker.far_geo)
FAR_GROUPS = tuple(GROUPS[c] for c in COLOURS)      # (roads.lua FAR_LAMP_GROUPS[1:], in this order)
FAR_SKIP = (GUARD, TDZ)         # the far tiles' lights without these kinds (their count: a guard light pair and the touchdown zone
                                # barrettes are lost in the runway's own edge and centre lights from kilometres off)


def far_points(frame, box, infra_fn, runways_fn, elev):
    """tile_rows' lights of a far tile (box in frame, or a geotile's OwnedBox: each light in one tile) as [x, y, z, c]
    (see above; z over the ground by LIFT_M, the arguments tile_rows'): no road or building is kept clear of (far
    away, the equipment's few metres are lost in the glow of its row) but the aprons' and taxiway pads' are."""
    num = {g: i + 1 for i, g in enumerate(FAR_GROUPS)}
    return [[r[1], r[2], r[3], num[r[0]]] for r in tile_rows(frame, box, infra_fn, runways_fn, elev, masts=False, skip=FAR_SKIP, far=True)]


_BUILDINGS_KEY = "_airfield_building_outlines"
CLEARED_MAX = 64                # _cleared's answers kept
_CLEARED = {}


def _building_outlines(feats):
    """The footprints of a buildings Features (or a list of them) as [(lat (k,), lon (k,))] (above ground), kept on it."""
    out = []
    for f in (feats if isinstance(feats, (list, tuple)) else [feats]):
        if f is None or len(f) == 0:
            continue
        got = f.__dict__.get(_BUILDINGS_KEY)
        if got is None:
            got = []
            under = f.attrs.get("is_underground")
            for i in np.flatnonzero(np.asarray(f.kind) == 3).tolist():
                if under is not None and bool(under[i]):
                    continue
                for part in f.parts(i):
                    if part and len(part[0]) >= 3:
                        ring = np.asarray(part[0], np.float64)
                        got.append((ring[:, 1], ring[:, 0]))
            f.__dict__[_BUILDINGS_KEY] = got
        out += got
    return out


def _cleared(rw, lights, masts, la, lo, infra_fn, roads_fn, buildings_fn):
    """(lights, masts) of the runway rw without the equipment (approach lights, PAPI units, ILS aerials) that falls on an
    apron or taxiway pad, inside a building or within CLEAR_ROAD_M of a road line, and without the approach row of an end
    from its first centreline light that does (the crossbar's lights too, in line with them): the data of the cells round
    the equipment, so every tile decides alike. Unchanged where none of the equipment is in the tile (la, lo: its
    corners' lat / lon)."""
    import windsocks                        # (here: windsocks imports this module)
    app = KINDS.index(APPROACH)
    eq = np.flatnonzero((lights.kind == app) | (lights.kind == KINDS.index(PAPI)))
    if not len(eq) and not masts:
        return lights, masts
    elat = np.r_[lights.lat[eq], [m.lat for m in masts]]
    elon = np.r_[lights.lon[eq], [m.lon for m in masts]]
    pad = 0.0005
    if not ((elat >= la.min() - pad) & (elat <= la.max() + pad) & (elon >= lo.min() - pad) & (elon <= lo.max() + pad)).any():
        return lights, masts
    import mapcells
    cells = list(mapcells.cells_near_points(elat, elon, CLEAR_CELL_PAD_DEG, CLEAR_CELL_PAD_DEG))
    infra = [f for f in (infra_fn(k) for k in cells) if f is not None]
    outlines, roads, kept, stamps = list(aeroways.pads(infra, runways=False)), [], list(infra), [tuple(aeroways.stamp(f) for f in infra)]
    for k in cells:
        if roads_fn is not None:
            try:                            # (a cell without its roads or buildings loaded: not kept clear of there)
                rf = roads_fn(k)
                roads += windsocks.road_lines(rf)
                kept.append(rf)
                stamps.append(aeroways.stamp(rf))
            except Exception:
                pass
        if buildings_fn is not None:
            try:
                bf = buildings_fn(k)
                outlines += _building_outlines(bf)
                kept.append(bf)
                stamps.append(tuple(aeroways.stamp(f) for f in (bf if isinstance(bf, (list, tuple)) else [bf])))
            except Exception:
                pass
    if not (outlines or roads):
        return lights, masts
    # (the answer is the runway's and the data's, not the tile's: every tile with some of the equipment in it asked for all of it
    # again, ~30 ms each at Atlanta; kept by the runway's lights and the data round the equipment; the user, 2026-11-17: "build each
    # airport's scene once")
    memo = (id(lights), id(masts) if masts else 0, tuple(stamps))
    got = _CLEARED.get(memo)
    if got is None:
        if len(_CLEARED) >= CLEARED_MAX:
            _CLEARED.clear()
        got = _CLEARED[memo] = (_clear(rw, lights, masts, eq, elat, elon, app, outlines, roads), lights, masts, kept)
    return got[0]


def _clear(rw, lights, masts, eq, elat, elon, app, outlines, roads):
    """_cleared's lights and masts without what falls on the outlines or the road lines."""
    import windsocks
    bad = windsocks._blocked(elat[:len(eq)], elon[:len(eq)], outlines, roads, 0.0, CLEAR_ROAD_M)
    keep = ~bad
    isapp = lights.kind[eq] == app
    # (a road drops only the light on it, the row goes on over it as at Heathrow's 27L; an apron or a building ends it)
    hard = windsocks._blocked(elat[:len(eq)], elon[:len(eq)], outlines, [], 0.0, CLEAR_ROAD_M) if roads else bad
    if (isapp & hard).any():
        a = np.flatnonzero(isapp)
        g = runwaygeom.geometry(rw)
        s, t = g.st(lights.lat[eq[a]], lights.lon[eq[a]])
        far = s > g.length / 2.0
        d = np.where(far, s - g.length, -s)             # (from the end it is before)
        centre = np.abs(t) < 0.5
        for end in (False, True):
            on = far == end
            cut = d[on & centre & hard[a]]
            if len(cut):
                keep[a[on & (d >= cut.min() - 0.5)]] = False
    keep_all = np.ones(len(lights), bool)
    keep_all[eq] = keep
    mlat, mlon = elat[len(eq):], elon[len(eq):]
    ok = np.ones(len(masts), bool)
    for group, r in MAST_CLEAR_M.items():
        idx = [i for i, m in enumerate(masts) if m.group == group]
        if idx:
            ok[idx] = ~windsocks._blocked(mlat[idx], mlon[idx], outlines, roads, r, CLEAR_ROAD_M)
    if keep_all.all() and ok.all():
        return lights, masts
    return Lights(lights.lat[keep_all], lights.lon[keep_all], lights.colour[keep_all], lights.kind[keep_all]), \
        [m for m, o in zip(masts, ok) if o]


def _light_rows(frame, box, lights, elev, la, lo):
    """tile_rows' glow rows of the lights the box owns."""
    if not len(lights):
        return []
    pad = 0.0005                            # (degrees: the box's corners' lat / lon, a little wider)
    near = np.flatnonzero((lights.lat >= la.min() - pad) & (lights.lat <= la.max() + pad)
                          & (lights.lon >= lo.min() - pad) & (lights.lon <= lo.max() + pad))
    if not len(near):
        return []
    lat, lon = lights.lat[near], lights.lon[near]
    e, n, _ = frame.geodetic_to_enu(lat, lon, 0.0)
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    own = np.flatnonzero(np.asarray(geotiles.box_owns(box, e, n), bool))
    if not len(own):
        return []
    # (a Features cut at a cell's edge or listed in two cells gives a light twice: one per half metre)
    _, first = np.unique(np.round(np.c_[e[own], n[own]] * 2.0).astype(np.int64), axis=0, return_index=True)
    own = own[np.sort(first)]
    lat, lon, e, n, near = lat[own], lon[own], e[own], n[own], near[own]
    h = np.asarray(elev(lat, lon), np.float64)
    kind = lights.kind[near]
    lift = np.full(len(kind), LIFT_M)
    for k, m in LIFT_KIND_M.items():
        lift[kind == KINDS.index(k)] += m
    z = np.asarray(frame.geodetic_to_enu(lat, lon, h)[2], np.float64) + lift
    groups = [GROUPS[COLOURS[c]] for c in lights.colour[near].tolist()]
    return [[g, round(float(x), 2), round(float(y), 2), round(float(h_), 2), 0.0, 1.0, GROUP_H, 0.0]
            for g, x, y, h_ in zip(groups, e, n, z)]


def _yaw_along(east, north):
    """The row yaw of a shape whose front (local +Y) faces (east, north): the server's, counter-clockwise from +x, less 90."""
    return (math.degrees(dmath.atan2(north, east)) - 90.0) % 360.0


def _mast_rows(frame, box, masts, elev, la, lo):
    """tile_rows' rows [group, x, y, z, yaw, 1, catalogue height, 0] of the ILS masts the box owns (on the ground)."""
    pad = 0.0005
    masts = [m for m in masts if la.min() - pad <= m.lat <= la.max() + pad and lo.min() - pad <= m.lon <= lo.max() + pad]
    if not masts:
        return []
    lat, lon, lat2, lon2 = (np.array([getattr(m, f) for m in masts], np.float64) for f in ("lat", "lon", "lat_to", "lon_to"))
    e, n, _ = frame.geodetic_to_enu(lat, lon, 0.0)
    e2, n2, _ = frame.geodetic_to_enu(lat2, lon2, 0.0)
    e, n, e2, n2 = (np.asarray(a, np.float64) for a in (e, n, e2, n2))
    own = np.flatnonzero(np.asarray(geotiles.box_owns(box, e, n), bool))
    if not len(own):
        return []
    h = np.asarray(elev(lat[own], lon[own]), np.float64)
    z = np.asarray(frame.geodetic_to_enu(lat[own], lon[own], h)[2], np.float64)
    rows, seen = [], set()
    for j, zz in zip(own.tolist(), z):
        key = (masts[j].group, round(float(e[j]) * 2.0), round(float(n[j]) * 2.0))
        if key in seen:                     # (the same runway from two cells: one mast)
            continue
        seen.add(key)
        rows.append([masts[j].group, round(float(e[j]), 2), round(float(n[j]), 2), round(float(zz), 2),
                     round(_yaw_along(float(e2[j] - e[j]), float(n2[j] - n[j])), 1), 1.0, ILS_H[masts[j].group], 0.0])
    return rows
