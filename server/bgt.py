"""The Netherlands' road surfaces from the BGT (Basisregistratie Grootschalige Topografie), drawn as they are mapped
instead of widened from centre lines (--nl-bgt on; the user, Rotterdam 2026-10-21: "veel inaccurate neppe gokwegen,
nergens parkeerplaatsen, hoe kruisingen elkaar connecten is totaal verkeerd").

The BGT is the government's large-scale base map: every carriageway, parking bay, cycle path, footway, tram lane,
traffic island and verge as its own polygon (cm-level, kept by the municipalities; PDOK, CC0). Measured at Westzeedijk x
's-Gravendijkwal (~300 x 250 m): 451 road polygons (18 parking bays, 18 tram lanes), 22 traffic islands, 32 polygons at
level -1 (the sunken approach to the Maastunnel) and 7 at +1 (the deck over it); its walls, street lights, tram tracks
and trees too.

    b = Bgt(cache_dir, fetcher)
    polys = b.polygons(frame, tx, ty)       # [Poly] of road tile (tx, ty): the road surfaces
    mesh(frame, box, polys, elev, raw, origin, lift)   # (positions, tris, uvs, materials, tri material index)

Read from PDOK's vector tiles (Web Mercator z17, ~190 m in Rotterdam, ~60 KB each, all layers in one; mvt.py), kept
gzipped under <cache>/regional/bgt/v<VERSION>/17/x/y.mvt.gz. (The OGC API's GeoJSON of the same features was 21 MB for
300 x 250 m: 46 KB a polygon.) A tile's features reach past its square (a wide buffer), so each tile draws only what
lies in its own square (Poly.own). The polygons tile the ground without overlapping, so one lift draws them all; the
road mesh keeps its markings over them and drops its guessed surfaces (blockworker.roads_tile). Level +1 polygons (a
deck over a sunken road) lie on the ungraded ground (the lidar has no deck: its hole filled from the street around it,
dem.fill_holes), the others on the graded ground."""
import gzip
import logging
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

import roads as roadsmod

log = logging.getLogger("earthd.bgt")

VERSION = 1
TILES_URL = "https://api.pdok.nl/kadaster/bgt/ogc/v1/tiles/WebMercatorQuad/{z}/{y}/{x}?f=mvt"
TILE_ZOOM = 17                  # the only zoom PDOK serves the BGT's vector tiles at
SURFACES = ("wegdeel", "ondersteunendwegdeel")     # areas: the road surfaces (mesh)
WALLS = "scheiding"                                 # areas and lines: walls, quay walls, fences (walls)
POLES = "paal"                                      # points: street lights, gantries (lamps)
RAILS = "spoor"                                     # lines: tram and rail tracks (rails_mesh)
TREES = "vegetatieobject"                           # points: trees
PASSAGE = "tunneldeel"                               # areas: the ground under a deck a road goes through (passages)
FLOOR_STEP_M = 2.0              # m between the points of a passage's outline its floor is read at
FLOOR_LOW_M = 1.0               # ... those within this of its lowest are its open ends (the rest: under the deck)
DECK_NEAR_M = 6.0               # m round a passage a street's points tell whether it is a deck over it (mesh)
DECK_SIDE_M = 6.0               # m beside a passage's sides its deck's top is read (Passage.prepare)
FASCIA_M = 1.2                  # m: a deck's edge over a passage's mouth (walls)
FLOOR_REACH_M = 10.0            # m outside its outline the ground is read (the DEM's blur of the deck's edge) and dug
NL = (3.2, 50.7, 7.3, 53.6)     # lon0, lat0, lon1, lat1: where the BGT is asked at all
MEM_TILES = 64
ENV = "BNGE_NL_BGT"             # "1": on (earthd --nl-bgt on sets it before the worker processes start)
MARGIN_M = 8.0                  # m past a road tile's edge whose BGT tiles are read (polygons crossing its edge)

SURFACE_LIFT_M = 0.05           # m the BGT's surfaces (and the markings on them) lie over the terrain: between their
                                # STEP_M vertices the 1 m terrain showed through them by 5-15 cm (tools/road_check.py)
STEP_M = 2.0                    # m: the longest edge after subdivision (follows the graded ground; plazas.STEP_M)
MIN_AREA_M2 = 0.5
KERB_M = 0.12                   # a footway's or traffic island's kerb height over the road
KERB_STEP_M = 2.0               # m per kerb face
KERB = "curbs"                  # West Coast USA's kerb stone (sidewalks.MATERIALS)
KERB_REPEAT_M = 2.0
CUT_KEEP_M = 1.0                # a level 0 polygon stays on the ground where the grading cut deeper than this
KEEP_P = 12                     # road mesh chunks below this render priority are kept over the BGT (markings);
                                # the surfaces (asphalt 20, sidewalks 16, gutters 14, drains 12) are the BGT's now

ASPHALT = ("road_asphalt_2lane", 11.0)
STONE = ("italy_cobblestone", 2.5)
TILES = ("sidewalk_concrete", 6.0)
RED = ("bne_sidewalk_brick", 1.2)       # cycle paths: red (stock has no red asphalt yet; design 13d)
DECK_VERGE = ("bne_tunnel_soil", 1.0)   # a verge on a deck (mesh: the terrain under a deck is the road under it)
GRAVEL = ("m_dirt_road", 88.0)


def enabled():
    return os.environ.get(ENV) == "1"


def covers(lat, lon):
    return NL[0] <= lon <= NL[2] and NL[1] <= lat <= NL[3]


def look(functie, voorkomen):
    """(material, m per texture repeat) of a BGT road polygon, or None where the terrain's photo stays (verges,
    unpaved). Pure."""
    f = (functie or "").lower()
    v = (voorkomen or "").lower()
    if "berm" in f or v == "onverhard" or "groen" in v:
        return None
    if "verkeerseiland" in f:
        return TILES
    if "fietspad" in f:
        return RED
    if "voetpad" in f or "voetgangersgebied" in f:
        return TILES if v != "gesloten verharding" else ASPHALT
    if v == "half verhard":
        return GRAVEL
    if v == "open verharding":
        return STONE
    return ASPHALT


@dataclass
class Poly:
    id: str
    layer: str
    functie: str
    voorkomen: str
    level: int
    rings: list                 # [(k, 2) lon/lat]: an area's outer ring then its holes; a line's points; a point
    own: tuple = None           # (lon0, lat0, lon1, lat1): the square of the BGT tile it came from (drawn there only)


def _functie(layer, p):
    """The kind of a BGT feature: its function, else its type (a wall's "muur"), else its plus type (a pole's
    "lichtmast", a tree's "boom")."""
    f = p.get("functie") or p.get("type") or p.get("bgt_type") or ""
    if not f or f == "niet-bgt":
        f = p.get("plus_type") or ""
    return str(f)


def _lonlat(z, x, y, extent, pts):
    """Tile coordinates (y down) of tile (z, x, y) as lon/lat (k, 2)."""
    n = 1 << z
    a = np.asarray(pts, np.float64)
    lon = (x + a[:, 0] / extent) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * (y + a[:, 1] / extent) / n))))
    return np.stack([lon, lat], 1)


def tile_polys(data, z, x, y, layers):
    """[Poly] of one decoded-from-bytes BGT vector tile, the given layers only. Pure."""
    import mvt
    n = 1 << z
    own = (x / n * 360.0 - 180.0, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n)))),
           (x + 1) / n * 360.0 - 180.0, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))))
    out = []
    for L in mvt.decode(data):
        if L["name"] not in layers:
            continue
        ext = L["extent"]
        for k, f in enumerate(L["features"]):
            p = f["props"]
            pid = f"{p.get('lokaal_id') or k}@{z}/{x}/{y}"
            fun, v, lv = _functie(L["name"], p), str(p.get("fysiek_voorkomen") or ""), int(p.get("relatieve_hoogteligging") or 0)
            if f["type"] == 3:
                # MVT: an outer ring winds one way, its holes the other (y down); a polygon per outer ring
                rings = []
                for ring in f["geom"]:
                    if len(ring) < 4:
                        continue
                    a = np.asarray(ring, np.float64)
                    area = 0.5 * np.sum(a[:-1, 0] * a[1:, 1] - a[1:, 0] * a[:-1, 1])
                    if area > 0 or not rings:
                        if rings:
                            out.append(Poly(pid, L["name"], fun, v, lv, rings, own))
                        rings = [_lonlat(z, x, y, ext, ring)]
                    else:
                        rings.append(_lonlat(z, x, y, ext, ring))
                if rings:
                    out.append(Poly(pid, L["name"], fun, v, lv, rings, own))
            elif f["type"] == 2:
                for line in f["geom"]:
                    if len(line) >= 2:
                        out.append(Poly(pid, L["name"], fun, v, lv, [_lonlat(z, x, y, ext, line)], own))
            elif f["type"] == 1:
                for pt in f["geom"]:
                    out.append(Poly(pid, L["name"], fun, v, lv, [_lonlat(z, x, y, ext, [pt])], own))
    return out


def in_own(lon, lat, own):
    """Which points (lon, lat arrays) lie in a tile's own square (west and south edges in)."""
    lon0, lat0, lon1, lat1 = own
    return (lon >= lon0) & (lon < lon1) & (lat >= lat0) & (lat < lat1)


class Bgt:
    def __init__(self, cache_dir, fetcher):
        self.dir = os.path.join(cache_dir, "regional", "bgt", f"v{VERSION}")
        self.fetcher = fetcher
        self.mem = OrderedDict()
        self.lock = threading.Lock()

    def _path(self, x, y):
        return os.path.join(self.dir, str(TILE_ZOOM), str(x), f"{y}.mvt.gz")

    def tile(self, x, y):
        """A BGT vector tile's bytes (from memory, disk or PDOK; b"" where it has none)."""
        with self.lock:
            if (x, y) in self.mem:
                self.mem.move_to_end((x, y))
                return self.mem[(x, y)]
        path = self._path(x, y)
        try:
            with gzip.open(path, "rb") as f:
                data = f.read()
        except OSError:
            import netfetch
            try:
                data = self.fetcher.get(TILES_URL.format(z=TILE_ZOOM, x=x, y=y)).body
            except netfetch.NotFound:
                data = b""
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with gzip.open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        with self.lock:
            self.mem[(x, y)] = data
            while len(self.mem) > MEM_TILES:
                self.mem.popitem(last=False)
        return data

    def tiles_of(self, frame, tx, ty):
        """The BGT tiles (x, y) over road tile (tx, ty) and MARGIN_M round it, inside the Netherlands."""
        return self.tiles_box(frame, roadsmod.tile_bbox(tx, ty))

    def tiles_box(self, frame, box):
        """The BGT tiles (x, y) over the box (x0, y0, x1, y1) in the frame and MARGIN_M round it, inside the
        Netherlands."""
        x0, y0, x1, y1 = box
        la, lo, _ = frame.enu_to_geodetic(np.array([x0 - MARGIN_M, x1 + MARGIN_M]),
                                          np.array([y0 - MARGIN_M, y1 + MARGIN_M]), np.zeros(2))
        if not (covers(float(la[0]), float(lo[0])) or covers(float(la[1]), float(lo[1]))):
            return []
        n = 1 << TILE_ZOOM

        def xy(lat, lon):
            return (int((lon + 180.0) / 360.0 * n),
                    int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))
        ax, ay = xy(float(la.max()), float(lo.min()))
        bx, by = xy(float(la.min()), float(lo.max()))
        return [(x, y) for x in range(ax, bx + 1) for y in range(ay, by + 1)]

    def features(self, frame, tx, ty, layers):
        """[Poly] of road tile (tx, ty) in these layers, each with its BGT tile's own square."""
        out = []
        for x, y in self.tiles_of(frame, tx, ty):
            data = self.tile(x, y)
            if data:
                out += tile_polys(data, TILE_ZOOM, x, y, layers)
        return out

    def polygons(self, frame, tx, ty):
        """The road surface polygons (SURFACES) of road tile (tx, ty)."""
        return self.features(frame, tx, ty, SURFACES)

    def surfaces(self, frame, box):
        """The road surface polygons (SURFACES) over the box (x0, y0, x1, y1) in the frame."""
        out = []
        for x, y in self.tiles_box(frame, box):
            data = self.tile(x, y)
            if data:
                out += tile_polys(data, TILE_ZOOM, x, y, SURFACES)
        return out

    def passages(self, frame, box):
        """The underpasses (passages()) over the box (x0, y0, x1, y1) in the frame."""
        out = []
        for x, y in self.tiles_box(frame, box):
            data = self.tile(x, y)
            if data:
                out += tile_polys(data, TILE_ZOOM, x, y, (PASSAGE,))
        return passages(frame, out)


class _Mesh:
    """Triangles collected by material: (positions (k, 3) in the frame, tris, uvs, materials, tri material index)."""

    def __init__(self):
        self.P, self.T, self.UV, self.M, self.mats, self.n = [], [], [], [], [], 0

    def add(self, P, T, UV, mat):
        if mat not in self.mats:
            self.mats.append(mat)
        self.P.append(P)
        self.T.append(np.asarray(T, np.int64) + self.n)
        self.UV.append(UV)
        self.M.append(np.full(len(T), self.mats.index(mat), np.int64))
        self.n += len(P)

    def done(self, origin):
        if not self.P:
            return None
        return (np.concatenate(self.P) - np.asarray(origin, np.float64), np.concatenate(self.T),
                np.concatenate(self.UV), list(self.mats), np.concatenate(self.M))


def raised(functie):
    """Is a BGT polygon of this function a kerbed one (footways, traffic islands, pedestrian areas)?"""
    f = (functie or "").lower()
    return "voetpad" in f or "verkeerseiland" in f or "voetgangersgebied" in f


def _ground(frame, xy, level, elev, raw):
    """(heights in the frame (k,), on the ungraded ground anywhere) of points xy (k, 2) of a polygon at this BGT
    level: the graded ground, but the ground as it is where the grading cut a sunken road under a polygon at level
    >= 0 (a deck, +1, or the street crossing over the trench at grade, 0: the junction over 's-Gravendijkwal sank
    into its trench)."""
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    h = np.asarray(elev(la, lo), np.float64)
    over = np.zeros(len(xy), bool)
    if level >= 0:
        r = np.asarray(raw(la, lo), np.float64)
        over = (level > 0) | (r - h > CUT_KEEP_M)
        h = np.where(over, np.maximum(r, h), h)
    _, _, z = frame.geodetic_to_enu(la, lo, h)
    return np.asarray(z, np.float64), np.broadcast_to(over, (len(xy),)).copy()


class Passage:
    """An underpass: a BGT tunneldeel (the ground under a deck, level -1) as rings in the frame and its box. Once its
    ground is known (prepare): its open ends (the points of its outline whose ground within FLOOR_REACH_M outside is
    within FLOOR_LOW_M of the lowest), the floor there (that lowest ground: the DEM blurs the deck's edge over a few
    of its pixels into the trench) and the strips FLOOR_REACH_M out of them that are dug too."""

    def __init__(self, id, rings):
        self.id, self.rings = id, rings
        self.lo, self.hi = rings[0].min(0), rings[0].max(0)
        self.fx = self.fz = self.top = None
        self.areas = [rings]

    def prepare(self, frame, ground):
        if self.fx is not None:
            return
        import grading
        ring = self.rings[0]
        pts, _ = grading.resample(np.r_[ring, ring[:1]], FLOOR_STEP_M)
        pts = pts[:-1] if len(pts) > 1 and np.hypot(*(pts[0] - pts[-1])) < 1e-6 else pts
        d = np.roll(pts, -1, 0) - np.roll(pts, 1, 0)
        nrm = np.stack([d[:, 1], -d[:, 0]], 1) / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nrm = np.where(_inside_poly(self.rings, pts + nrm * 0.5)[:, None], -nrm, nrm)     # outward
        t = np.arange(0.0, FLOOR_REACH_M + 1e-6, FLOOR_STEP_M)
        q = (pts[:, None, :] + nrm[:, None, :] * t[None, :, None]).reshape(-1, 2)
        la, lo, _ = frame.enu_to_geodetic(q[:, 0], q[:, 1], np.zeros(len(q)))
        z = np.asarray(ground(la, lo), np.float64).reshape(len(pts), len(t)).min(1)
        low = z <= z.min() + FLOOR_LOW_M
        # the deck's top: the ground in its middle (the DEM there is the deck, its edges blurred into the trench)
        g0, g1 = self.lo, self.hi
        gx, gy = np.meshgrid(np.arange(g0[0], g1[0], 2.0), np.arange(g0[1], g1[1], 2.0))
        inner = np.c_[gx.ravel(), gy.ravel()]
        inner = inner[_inside_poly(self.rings, inner)] if len(inner) else inner
        if len(inner):
            ila, ilo, _ = frame.enu_to_geodetic(inner[:, 0], inner[:, 1], np.zeros(len(inner)))
            self.top = float(np.percentile(np.asarray(ground(ila, ilo), np.float64), 90))
        else:
            self.top = None
        if not low.all():               # ... and beside it (a narrow passage's middle blurred too: 2.0 m for 3.3)
            side = pts[~low] + nrm[~low] * DECK_SIDE_M
            sla, slo, _ = frame.enu_to_geodetic(side[:, 0], side[:, 1], np.zeros(len(side)))
            st = float(np.median(np.asarray(ground(sla, slo), np.float64)))
            self.top = st if self.top is None else max(self.top, st)
        self.fx, self.fz = np.r_[pts[low], (pts + nrm * FLOOR_REACH_M)[low]], np.r_[z[low], z[low]]
        # the strips out of the open ends: each run of low points with its copy FLOOR_REACH_M out
        k = np.flatnonzero(low)
        if len(k) and not low.all():
            start = int(np.flatnonzero(low & ~np.roll(low, 1))[0])
            order = np.roll(np.arange(len(pts)), -start)
            run = []
            for i in list(order) + [None]:
                if i is not None and low[i]:
                    run.append(i)
                    continue
                if len(run) >= 2:       # (and the next point on either side: the blur reaches past the run)
                    run = [(run[0] - 1) % len(pts)] + run + [(run[-1] + 1) % len(pts)]
                    a = pts[run]
                    self.areas.append([np.r_[a, (a + nrm[run] * FLOOR_REACH_M)[::-1]]])
                run = []
        lo_ = np.min([r[0].min(0) for r in self.areas], 0)
        hi_ = np.max([r[0].max(0) for r in self.areas], 0)
        self.lo, self.hi = lo_, hi_


def passages(frame, polys):
    """[Passage] of the BGT tunneldeel polygons under a deck (level < 0) among polys: where a road goes under a
    junction ('s-Gravendijkwal under the Droogleever Fortuynplein, 2026-10-21). Their ground is the deck's in the
    DEM; the terrain is cut to the road's floor in them (cut) and the streets over them are the deck (mesh)."""
    out = []
    for p in polys:
        if p.layer != PASSAGE or p.level >= 0 or not p.rings or len(p.rings[0]) < 4:
            continue
        rings = []
        for ring in p.rings:
            e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
            xy = np.stack([np.asarray(e), np.asarray(n)], 1)
            if len(xy) > 3 and np.hypot(*(xy[0] - xy[-1])) < 1e-6:
                xy = xy[:-1]
            rings.append(xy)
        pid = p.id.split("@")[0]        # (the same feature in each BGT tile it reaches)
        if len(rings[0]) >= 3 and not any(q.id == pid for q in out):
            out.append(Passage(pid, rings))
    return out


def skips_tunnel(bgt, frame, ground):
    """For roads.TunnelProfiles.skip: is a tunnel part (its centre line xy (k, 2) in the frame) mostly in a BGT
    passage (or past its ends)? Then the BGT's deck and the dug floor are the underpass, not the map's tunnel: its
    stone arch, portal faces and the soil raised over it stood across 's-Gravendijkwal (2026-10-21)."""
    def skip(xy):
        if len(xy) < 2:
            return False
        lo, hi = xy.min(0), xy.max(0)
        try:
            passes = bgt.passages(frame, (lo[0] - 20, lo[1] - 20, hi[0] + 20, hi[1] + 20))
        except Exception as e:
            log.warning("bgt passages for a tunnel: %s: %s", type(e).__name__, e)
            return False
        if not passes:
            return False
        for ps in passes:
            ps.prepare(frame, ground)
        import grading
        pts = grading.resample(xy, 2.0)[0]
        return bool((_in_passages(passes, pts, ends=True) >= 0).mean() > 0.5)
    return skip


def _in_passages(passes, xy, ends=False):
    """Which points (k, 2) lie in any passage (ends: or the strips out of its open ends), in which (index, -1 none)."""
    which = np.full(len(xy), -1, np.int64)
    for i, ps in enumerate(passes):
        m = ((which < 0) & (xy[:, 0] >= ps.lo[0]) & (xy[:, 0] <= ps.hi[0]) & (xy[:, 1] >= ps.lo[1])
             & (xy[:, 1] <= ps.hi[1]))
        for rings in (ps.areas if ends else ps.areas[:1]):
            if m.any():
                k = np.flatnonzero(m)
                k = k[_inside_poly(rings, xy[k])]
                which[k] = i
                m[k] = False
    return which


def _idw(px, x, z):
    """Inverse-distance weighted heights z at points x (m, 2) at the points px (k, 2). Pure."""
    d2 = ((px[:, None, :] - x[None, :, :]) ** 2).sum(2) + 1.0
    w = 1.0 / d2
    return (w * z[None, :]).sum(1) / w.sum(1)


def cut(frame, passes, ground):
    """ground(lat, lon) -> heights, lowered in the passages to their floors (interpolated from their open ends): the
    terrain under a deck dug out so the road under it goes through."""
    if not passes:
        return ground

    def cut_ground(la, lo):
        z = np.asarray(ground(la, lo), np.float64)
        shape = z.shape
        e, n, _ = frame.geodetic_to_enu(np.ravel(la), np.ravel(lo), 0.0)
        xy = np.stack([np.ravel(np.asarray(e, np.float64)), np.ravel(np.asarray(n, np.float64))], 1)
        for ps in passes:
            ps.prepare(frame, ground)
        which = _in_passages(passes, xy, ends=True)
        if not (which >= 0).any():
            return z
        z = np.array(z.reshape(-1), np.float64)
        for i in np.unique(which[which >= 0]):
            k = np.flatnonzero(which == i)
            fx, fz = passes[i].fx, passes[i].fz
            for a in range(0, len(k), 4096):
                b = k[a:a + 4096]
                z[b] = np.minimum(z[b], _idw(xy[b], fx, fz))
        return z.reshape(shape)
    return cut_ground


def banks(frame, passes, ground, raw):
    """ground(lat, lon) -> heights, kept up to the ground as it is (raw: the lidar) where the grading cut more than
    CUT_KEEP_M under it, outside the passages: a sunken road's trench is walled (its BGT walls), the grading's had wide
    slopes cut into the banks, under the bike paths along its top (they hung 1.5 m in the air) and the grass beside
    them (pits), 2026-10-21. In the trench itself the lidar is as low as the grading."""
    def banked(la, lo):
        z = np.asarray(ground(la, lo), np.float64)
        shape = z.shape
        z = np.array(z.reshape(-1), np.float64)
        r = np.asarray(raw(np.ravel(la), np.ravel(lo)), np.float64).reshape(-1)
        cand = r - z > CUT_KEEP_M
        if passes and cand.any():
            e, n, _ = frame.geodetic_to_enu(np.ravel(la), np.ravel(lo), 0.0)
            xy = np.stack([np.ravel(np.asarray(e, np.float64)), np.ravel(np.asarray(n, np.float64))], 1)
            k = np.flatnonzero(cand)
            cand[k[_in_passages(passes, xy[k], ends=True) >= 0]] = False
        z[cand] = r[cand] - CUT_KEEP_M
        return z.reshape(shape)
    return banked


def _inside_poly(rings, pts):
    """Which points lie in the polygon (outer ring, holes; (k, 2) each). Pure."""
    import plazas
    m = plazas._inside_many(rings[0], pts)
    for h in rings[1:]:
        if m.any() and len(h) >= 3:
            m &= ~plazas._inside_many(h, pts)
    return m


def mesh(frame, box, polys, elev, raw, origin, lift, solid=None, passes=(), decks=None, step=STEP_M, deck_top=None,
         skip=None):
    """The BGT polygons in tile box (x0, y0, x1, y1) as meshes ((positions relative to origin (k, 3), tris, uvs,
    [material], tri material index) or None): the flat road surfaces render only (the car drives on the graded
    ground under them), returned; the kerbed ones (footways, traffic islands: KERB_M up, with kerb faces where they
    meet anything lower) and the surfaces over a cut (a deck, the street over a trench) colliding, appended to the list
    `solid` when given (blockworker puts them in the tile's colliding tunnel mesh). elev(lat, lon): the graded
    ground; raw(lat, lon): the ground without grading; lift: the road mesh's base lift (the BGT lies just over it, its
    markings over that); passes: the underpasses (passages()), elev cut to their floors: the streets over them a deck,
    level between the street's ground round it (the DEM's blur of the deck's edge into the trench is not drawn)."""
    import buildings
    import grading
    import plazas
    x0, y0, x1, y1 = box
    flat, hard, over_m = _Mesh(), _Mesh(), _Mesh()
    shapes = []
    for poly in polys:
        lk = look(poly.functie, poly.voorkomen)
        if lk is None:
            if deck_top is None or poly.level < 0 or poly.layer not in SURFACES:
                continue
            lk = DECK_VERGE         # (a verge on a deck: drawn where it is on it, the terrain under it is the road under)
        rings = []
        for ring in poly.rings:
            e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
            xy = np.stack([np.asarray(e), np.asarray(n)], 1)
            if len(xy) > 3 and np.hypot(*(xy[0] - xy[-1])) < 1e-6:
                xy = xy[:-1]
            rings.append(xy)
        xy = rings[0]
        if (len(xy) < 3 or xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0
                or xy[:, 1].min() > y1):
            continue
        area2 = buildings._area2(xy)
        if abs(area2) / 2 < MIN_AREA_M2:
            continue
        if area2 < 0:
            rings[0] = xy = xy[::-1]
        shapes.append((poly, lk, rings))
    kerbed = [(rings, rings[0].min(0), rings[0].max(0)) for poly, _, rings in shapes if raised(poly.functie)]

    def next_to_kerbed(pts):
        """Which points lie in any kerbed polygon (the kerb face between two of them is not drawn)."""
        out = np.zeros(len(pts), bool)
        for rings, lo_, hi_ in kerbed:
            m = ~out & (pts[:, 0] >= lo_[0]) & (pts[:, 0] <= hi_[0]) & (pts[:, 1] >= lo_[1]) & (pts[:, 1] <= hi_[1])
            if m.any():
                k = np.flatnonzero(m)
                out[k[_inside_poly(rings, pts[k])]] = True
        return out

    for poly, (mat, rep_m), rings in shapes:
        tris = np.asarray(buildings.triangulate(rings[0]), np.int64).reshape(-1, 3)
        if not len(tris):
            continue
        P, T = plazas._subdivide(rings[0], tris, step)
        Pall = P                        # (all of it: whether it is the trench's road or a deck is the whole polygon's)
        c = P[T].mean(1)
        keep = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
        if skip is not None and keep.any():               # (a kunstwerk draws its streets there: kunstwerken.covered)
            k = np.flatnonzero(keep)
            sla, slo, _ = frame.enu_to_geodetic(c[k, 0], c[k, 1], np.zeros(len(k)))
            keep[k[np.asarray(skip(np.asarray(sla), np.asarray(slo)), bool)]] = False
        if (mat, rep_m) == DECK_VERGE and keep.any():      # a verge: only its part on a deck
            k = np.flatnonzero(keep)
            cla, clo, _ = frame.enu_to_geodetic(c[k, 0], c[k, 1], np.zeros(len(k)))
            keep[k[~np.isfinite(np.asarray(deck_top(cla, clo), np.float64))]] = False
        if poly.own is not None and keep.any():     # (its BGT tile's square: the neighbours draw the rest)
            cla, clo, _ = frame.enu_to_geodetic(c[:, 0], c[:, 1], np.zeros(len(c)))
            keep &= in_own(np.asarray(clo), np.asarray(cla), poly.own)
        for h in rings[1:]:             # holes (a tree pit, a mast's footing): their triangles out
            if keep.any() and len(h) >= 3:
                k = np.flatnonzero(keep)
                keep[k[plazas._inside_many(h, c[k])]] = False
        if not keep.any():
            continue
        T = T[keep]
        used = np.unique(T)
        remap = np.full(len(P), -1, np.int64)
        remap[used] = np.arange(len(used))
        P, T = P[used], remap[T]
        z, om = _ground(frame, P, poly.level, elev, raw)
        deck = False
        dv = np.zeros(len(P), bool)     # the vertices of its deck part
        if passes and poly.level >= 0 and (_in_passages(passes, Pall, ends=True) >= 0).any():
            # in the open trench (its road, BGT level 0) or at grade (a street over the passage, a deck)? The polygon's
            # ground FLOOR_REACH_M .. 2.5 FLOOR_REACH_M from the passage tells: past the DEM's blur of the deck's edge,
            # short of where a trench's road climbs its ramp; all of the polygon, not the piece in this BGT tile (a
            # bike path in the trench read as a deck from its piece by the passage: a red slab 1.5 m over it,
            # 2026-10-21)
            nearA = _in_passages(passes, Pall, ends=True) >= 0
            floorA = _ground(frame, Pall[nearA], -1, elev, raw)[0]
            rzA = _ground(frame, Pall, 0, raw, raw)[0]
            nk = Pall[nearA][::max(1, int(nearA.sum()) // 64)]

            def dist_near(Q):
                return np.sqrt(((Q[:, None, :] - nk[None, :, :]) ** 2).sum(2).min(1))
            refA = np.flatnonzero(~nearA)
            if len(refA):
                dA = dist_near(Pall[refA])
                band = refA[(dA >= FLOOR_REACH_M) & (dA <= 2.5 * FLOOR_REACH_M)]
                refA = band if len(band) else refA[np.argsort(dA)[:max(1, len(refA) // 4)]]
            trench = not len(refA) or np.median(rzA[refA]) < np.median(floorA) + FLOOR_LOW_M
            on = _in_passages(passes, P) >= 0
            past = ~on & (_in_passages(passes, P, ends=True) >= 0)
            near = on | past
            if trench:                  # the trench's road: on the floor through the passage and past its ends
                if near.any():
                    z[near] = _ground(frame, P[near], -1, elev, raw)[0]
                    om[near] = False
            else:                       # a street at grade: over the passage a deck at its top; elsewhere on the
                deck = bool(on.any())   # ground (bgt.banks keeps that at the lidar: no blur to level out; the blur's
                dv |= on                # band levelled from 10-25 m off reached into the trench, decks at 0 m by the
                om[past] = False        # passages with the ground 3 m over them, 2026-10-21, tools/road_check.py)
                if on.any():
                    w = _in_passages(passes, P)
                    for i in np.unique(w[w >= 0]):
                        k = np.flatnonzero(w == i)
                        if passes[i].top is not None:
                            la_, lo_, _ = frame.enu_to_geodetic(P[k, 0], P[k, 1], np.zeros(len(k)))
                            z[k] = np.asarray(frame.geodetic_to_enu(la_, lo_, np.full(len(k), passes[i].top))[2])
                        elif len(refA):
                            z[k] = _idw(P[k], Pall[refA], _ground(frame, Pall[refA], poly.level, elev, raw)[0])
        if deck_top is not None and poly.level >= 0:     # on a deck the lidar's surface has (ahn.Ahn.deck_top): the
            dla, dlo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))     # street over a sunken road
            dt = np.asarray(deck_top(dla, dlo), np.float64)     # where the ground under it is the road's (its ground
            on_deck = np.isfinite(dt)                           # has no deck)
            if on_deck.any():
                z[on_deck] = np.asarray(frame.geodetic_to_enu(dla[on_deck], dlo[on_deck], dt[on_deck])[2])
                if (mat, rep_m) == DECK_VERGE and (~on_deck).any():     # (its edge vertices off the deck: level)
                    z[~on_deck] = _idw(P[~on_deck], P[on_deck], z[on_deck])
                    on_deck[:] = True
                dv |= on_deck
                deck = True
        dv |= om
        kerb = raised(poly.functie)
        top = z + lift + 0.005 + (KERB_M if kerb else 0.0)
        # (the streets over a cut into `decks` when given: the engine draws an object's first 65535 vertices only, and
        # the plein's decks with its kerbs and walls were 72424 in one tile: slabs across the junction, 2026-10-21).
        # Only its triangles over or by the passage (or over a cut): the whole ring road round the junction was a
        # deck (2026-10-21, tools/road_check.py)
        if kerb or not dv.any():
            (hard if kerb else flat).add(np.c_[P, top], T, P / rep_m, mat)
        else:
            td = dv[T].any(1)
            for part, sel in ((over_m if decks is not None else hard, td), (flat, ~td)):
                if sel.any():
                    Ts = T[sel]
                    u = np.unique(Ts)
                    rm = np.full(len(P), -1, np.int64)
                    rm[u] = np.arange(len(u))
                    part.add(np.c_[P[u], top[u]], rm[Ts], P[u] / rep_m, mat)
        if not kerb:
            continue
        # the kerb faces: along every ring edge whose outside is not another kerbed polygon, from the ground up
        for ring in rings:
            pts, _ = grading.resample(np.r_[ring, ring[:1]], KERB_STEP_M)
            if len(pts) < 2:
                continue
            a, b = pts[:-1], pts[1:]
            mid = (a + b) / 2
            d = b - a
            nrm = np.stack([d[:, 1], -d[:, 0]], 1) / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
            out_pt = mid + nrm * 0.3
            flip = _inside_poly(rings, out_pt)              # the normal pointed in: the other side is out
            out_pt = np.where(flip[:, None], mid - nrm * 0.3, out_pt)
            show = ~next_to_kerbed(out_pt)
            show &= (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
            if poly.own is not None and show.any():
                mla, mlo, _ = frame.enu_to_geodetic(mid[:, 0], mid[:, 1], np.zeros(len(mid)))
                show &= in_own(np.asarray(mlo), np.asarray(mla), poly.own)
            if not show.any():
                continue
            zp, _ = _ground(frame, pts, poly.level, elev, raw)
            k = np.flatnonzero(show)
            Q = np.concatenate([np.c_[a[k], zp[k] + lift], np.c_[b[k], zp[k + 1] + lift],
                                np.c_[b[k], zp[k + 1] + lift + 0.005 + KERB_M],
                                np.c_[a[k], zp[k] + lift + 0.005 + KERB_M]])
            m = len(k)
            i = np.arange(m)
            quads = np.concatenate([np.stack([i, i + m, i + 2 * m], 1), np.stack([i, i + 2 * m, i + 3 * m], 1)])
            both = np.concatenate([quads, quads[:, ::-1]])  # two-sided (the faces are seen from the road)
            s = np.hypot(*(Q[:, :2] - Q[0, :2]).T)
            hard.add(Q, both, np.c_[s / KERB_REPEAT_M, Q[:, 2] - Q[:, 2].min()], KERB)
    if solid is not None:
        sm = hard.done(origin)
        if sm is not None:
            solid.append(sm)
    if decks is not None:
        dm = over_m.done(origin)
        if dm is not None:
            decks.append(dm)
    return flat.done(origin)


WALL_STEP_M = 2.0               # m between a wall's centre line points
WALL_PROBE_M = 3.0              # m to either side of a wall where the ground is read
WALL_MIN_M = 0.6                # a wall lower than this over the ground on its low side is left to the terrain
WALL_TOP_M = 0.15               # m its top stands over the ground behind it (a coping)


def _centre_line(xy, step=WALL_STEP_M):
    """The centre line of a thin polygon xy (k, 2): its points binned along its main axis, each bin the middle of the
    ring's points across. Pure."""
    c = xy.mean(0)
    u, _, vt = np.linalg.svd(xy - c, full_matrices=False)
    ax, nx = vt[0], vt[1]
    s = (xy - c) @ ax
    t = (xy - c) @ nx
    n = max(2, int(np.ceil((s.max() - s.min()) / step)) + 1)
    edges = np.linspace(s.min(), s.max(), n)
    out = []
    for a in edges:
        m = np.abs(s - a) <= step
        if m.any():
            out.append(c + ax * a + nx * (t[m].min() + t[m].max()) / 2)
    return np.array(out)


def walls(frame, box, polys, elev, raw, passes=()):
    """BGT walls (muur, kademuur) as roads.retaining_walls' dicts (base face line (k, 2), top (k,), foot (k,),
    toward (k, 2) unit vectors to the low side; frame heights) for roadmesh.tunnel: a wall standing between the
    graded ground on its low side and the ground behind it (the sunken approach to the Maastunnel between its walls),
    in the tile box. elev: the graded ground, raw: the ground as it is. passes: the underpasses (passages()): over
    their mouths a wall is the deck's edge, FASCIA_M deep, not a wall down to the dug floor that shut them. Pure given
    elev and raw."""
    x0, y0, x1, y1 = box
    out = []
    for poly in polys:
        if "muur" not in poly.functie.lower():
            continue
        ring = poly.rings[0]
        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        xy = np.stack([np.asarray(e), np.asarray(n)], 1)
        if (len(xy) < 2 or xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0
                or xy[:, 1].min() > y1):
            continue
        closed = len(xy) >= 4 and np.hypot(*(xy[0] - xy[-1])) < 1e-6
        if closed:                      # an area: its centre line
            line = _centre_line(xy)
        else:                           # a line (a wall drawn as one)
            import grading
            line = grading.resample(xy, WALL_STEP_M)[0]
        if len(line) < 2:
            continue
        d = np.gradient(line, axis=0)
        d /= np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nl = np.stack([-d[:, 1], d[:, 0]], axis=1)
        pts = np.concatenate([line + nl * WALL_PROBE_M, line - nl * WALL_PROBE_M, line])
        la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        k = len(line)
        g = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
        r = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(raw(la, lo), np.float64))[2], np.float64)
        low_left = g[:k].mean() < g[k:2 * k].mean()
        side = 1.0 if low_left else -1.0
        foot = np.minimum(g[:k], g[k:2 * k])
        top = np.maximum.reduce([r[:k], r[k:2 * k], r[2 * k:]]) + WALL_TOP_M
        if (top - foot).max() < WALL_MIN_M:
            continue
        inside = (line[:, 0] >= x0) & (line[:, 0] < x1) & (line[:, 1] >= y0) & (line[:, 1] < y1)
        if poly.own is not None:
            inside &= in_own(np.asarray(lo[2 * k:]), np.asarray(la[2 * k:]), poly.own)
        if inside.sum() < 2:
            continue
        foot = foot - 0.3
        if passes:
            mouth = _in_passages(passes, line + nl * side * WALL_PROBE_M, ends=True) >= 0
            foot = np.where(mouth, np.maximum(foot, top - FASCIA_M), foot)
        sl = inside
        out.append({"base": line[sl], "top": top[sl], "foot": foot[sl], "toward": nl[sl] * side, "concrete": True})
    return out


LAMP_HEIGHT_M = 9.0             # furniture.G_LAMP's (the stock lamppost's) height


def lamps(frame, box, poles, surfaces, elev):
    """furniture rows ([group, x, y, z, yaw, scale, height, rank]) of the BGT street lights (paal "lichtmast") in the
    tile box, each turned to the nearest carriageway's polygon (surfaces: the tile's BGT road polygons). elev: the
    graded ground. Pure given elev."""
    import furniture
    x0, y0, x1, y1 = box
    pts = []
    for p in poles:
        if p.functie.lower() == "lichtmast":
            if p.own is not None and not in_own(p.rings[0][:1, 0], p.rings[0][:1, 1], p.own)[0]:
                continue
            e, n, _ = frame.geodetic_to_enu(p.rings[0][:1, 1], p.rings[0][:1, 0], 0.0)
            x, y = float(np.asarray(e)[0]), float(np.asarray(n)[0])
            if x0 <= x < x1 and y0 <= y < y1:
                pts.append((x, y, p.rings[0][0, 1], p.rings[0][0, 0]))
    if not pts:
        return []
    road = []
    for s in surfaces:
        if "rijbaan" in s.functie.lower():
            e, n, _ = frame.geodetic_to_enu(s.rings[0][:, 1], s.rings[0][:, 0], 0.0)
            road.append(np.stack([np.asarray(e), np.asarray(n)], 1))
    road = np.concatenate(road) if road else np.zeros((0, 2))
    P = np.array(pts)
    h = np.asarray(elev(P[:, 2], P[:, 3]), np.float64)
    _, _, z = frame.geodetic_to_enu(P[:, 2], P[:, 3], h)
    rows = []
    for (x, y, _, _), zz in zip(pts, np.atleast_1d(z)):
        yaw = 0.0
        if len(road):
            j = int(np.argmin(np.hypot(road[:, 0] - x, road[:, 1] - y)))
            yaw = furniture._yaw(road[j, 0] - x, road[j, 1] - y)
        rows.append([furniture.G_LAMP, round(x, 2), round(y, 2), round(float(zz), 2), round(yaw, 1), 1.0,
                     LAMP_HEIGHT_M, 0.0])
    return rows


GAUGE_M = 1.435                 # standard gauge (RET Rotterdam's trams)
RAIL_W_M = 0.07                 # a grooved rail's head
RAIL_LIFT_M = 0.008             # m over the base lift: over the BGT surface (+0.005), under the markings (+0.01)
RAIL_STEP_M = 1.0
RAIL = ("bne_truss_steel", 2.0)     # painted steel (tunnel.materials.json): grey, close to worn rail heads


def rails_mesh(frame, box, lines, elev, origin, lift):
    """The tram tracks (BGT spoor, "tram") as two rail-head strips each, GAUGE_M apart, on the graded ground in the
    tile box: (positions relative to origin, tris, uvs, [material], tri material index) or None. Pure given elev."""
    import grading
    x0, y0, x1, y1 = box
    Ps, Ts, n0 = [], [], 0
    for ln in lines:
        if "tram" not in ln.functie.lower():
            continue
        pts = ln.rings[0]
        e, n, _ = frame.geodetic_to_enu(pts[:, 1], pts[:, 0], 0.0)
        xy = np.stack([np.asarray(e), np.asarray(n)], 1)
        if len(xy) < 2 or xy[:, 0].max() < x0 - 2 or xy[:, 0].min() > x1 + 2 or xy[:, 1].max() < y0 - 2 \
                or xy[:, 1].min() > y1 + 2:
            continue
        xy, _ = grading.resample(xy, RAIL_STEP_M)
        if len(xy) < 2:
            continue
        d = np.gradient(xy, axis=0)
        d /= np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nl = np.stack([-d[:, 1], d[:, 0]], axis=1)
        for off in (-GAUGE_M / 2, GAUGE_M / 2):
            a = xy + nl * (off - RAIL_W_M / 2)
            b = xy + nl * (off + RAIL_W_M / 2)
            k = len(xy)
            P = np.empty((2 * k, 2))
            P[0::2], P[1::2] = a, b
            q = np.arange(k - 1) * 2
            T = np.concatenate([np.stack([q, q + 1, q + 2], 1), np.stack([q + 1, q + 3, q + 2], 1)])
            c = P[T].mean(1)
            keep = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
            if ln.own is not None and keep.any():
                cla, clo, _ = frame.enu_to_geodetic(c[:, 0], c[:, 1], np.zeros(len(c)))
                keep &= in_own(np.asarray(clo), np.asarray(cla), ln.own)
            if not keep.any():
                continue
            Ps.append(P)
            Ts.append(T[keep] + n0)
            n0 += len(P)
    if not Ps:
        return None
    P = np.concatenate(Ps)
    T = np.concatenate(Ts)
    la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))
    _, _, z = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    V = np.c_[P, np.asarray(z) + lift + RAIL_LIFT_M] - np.asarray(origin, np.float64)
    return V, T, P / RAIL[1], [RAIL[0]], np.zeros(len(T), np.int64)


def keep_markings(chunks):
    """The road mesh's chunks drawn with the BGT: markings (render priority under KEEP_P), and the road surfaces on
    bridge decks and in tunnels (the BGT polygons there are left out: off_structures)."""
    return [c for c in chunks if c.get("p", 20) < KEEP_P or c.get("deck") or c.get("tunnel")]


STRUCTURE_PAD_M = 1.5           # m past a deck's or tunnel's half width a BGT polygon's centre counts as on it


def off_structures(frame, polys, chunks):
    """The polygons not on a bridge deck (level +1) or in a tunnel (level -1) the road mesh draws itself."""
    lines = {1: [], -1: []}
    for c in chunks:
        lv = 1 if c.get("deck") else -1 if c.get("tunnel") else 0
        if lv and len(c.get("n") or ()) >= 2:
            lines[lv].append((np.asarray(c["n"], np.float64)[:, :2], c.get("w", 0.0) / 2 + STRUCTURE_PAD_M))
    if not (lines[1] or lines[-1]):
        return polys
    out = []
    for p in polys:
        ls = lines.get(int(np.sign(p.level)))
        if not ls:
            out.append(p)
            continue
        ring = p.rings[0]
        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        q = np.array([np.mean(e), np.mean(n)])
        on = False
        for xy, r in ls:
            a, b = xy[:-1], xy[1:]
            ab = b - a
            t = np.clip(((q - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-9), 0.0, 1.0)
            if (np.hypot(*(a + t[:, None] * ab - q).T) <= r).any():
                on = True
                break
        if not on:
            out.append(p)
    return out
