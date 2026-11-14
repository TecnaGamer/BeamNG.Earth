"""build_osm_building_colours.py PLANET OUT_DIR: OpenStreetMap's mapped colours of buildings and building parts
(building:colour, roof:colour) as RGB, cut into one blob per zoom-12 tile and packed into PMTiles archive parts the
server reads a tile of with one range request (server/osmcolours.py), as build_osm_building_years.py does for the
construction years (the same parts, folders, manifest and checks: by.write_build).

    python3 tools/build_osm_building_colours.py planet-latest.osm.pbf out/osmcolours
    python3 tools/build_osm_building_colours.py https://.../planet-YYMMDD.osm.pbf out/osmcolours   (streamed by curl)
    python3 tools/build_osm_building_colours.py new-york-latest.osm.pbf out --bbox -74.03,40.69,-73.90,40.89 (tests)

Why (the Plaza investigation, 2026-11-14): Overture (2026-10-21.0) writes a mapped colour as its hex only for the
names its colour table has and leaves roof_color / facade_color empty for the rest: DarkGreen (its case), beige,
lightgray, darkgray, light_gray, lightgrey, gold, lightblue were all dropped. Over 9 Manhattan cells' 12,484 parts
matched to OSM by way id, 115 roof and 276 facade colours were lost (against ~3,600 / ~9,300 kept): the Plaza's green
copper mansard (roof:colour=DarkGreen on 22 parts), City Hall's and the Tweed Courthouse's light_gray roofs, the New
York Life and Met Life gold tops, 7 beige parts of the Empire State Building. Layercake's buildings file (osmyears'
source) has no building:part rows, so the colours are read from the planet itself. The server fills an Overture
colour only where Overture's is empty (a value Overture kept wins).

Read (COLOUR_KEYS): every way and relation tagged building=* or building:part=* (not "no") with a building:colour or
roof:colour this reads (parse_colour): a CSS colour name, any case, with its spaces, "_" and "-" left out (DarkGreen,
light_gray, dark-grey, "light grey"; CSS has the grey spellings too), a few other names the server draws
(EXTRA_COLOURS: cream, terracotta, charcoal ...), #rgb / #rgba / #rrggbb / #rrggbbaa (the alpha dropped), or six hex
digits without the "#"; of several values ("white;blue") the first. Anything else is left out (counted in the
manifest's "unread", its most common values logged). Only rows with a colour are kept.

Three passes (the planet's nodes come before its ways, its ways before its relations): 1. the ways and relations with
a colour (KeyFilter: only those reach Python), their node refs / their member ways (all roles but "inner"); 2. the
nodes of those ways, and the member ways' node refs, stopping at the first relation; 3. the member ways' nodes,
stopping at the first way. A row's bbox is that of all its nodes (a relation's: of its member ways'), float32; the row
is in every z12 tile the bbox touches (by.cut).

Output, in OUT_DIR, as the years' (build_osm_building_years.py's docstring: <key>/osmcolours-NNN.pmtiles parts,
manifest.json with "parts" pinned by head_sha256 / sha256 / bytes; publish the folder before the manifest, keep the
last KEEP_BUILDS folders); the manifest also has "facade", "roof" (rows with each) and "unread".

Blob (OBC1, earthfast buildcolours.rs), LEB128 varints: "OBC1", uv n, n x uv key deltas (ascending, one row a key: 2 x
the OSM id, + 1 for a relation), n flag bytes (FACADE 1: the row has a facade colour, ROOF 2: a roof colour; no other
bit, never 0: every row has a colour), uv m, the palette: m x 3 bytes r, g, b (the tile's colours, ascending as
0xRRGGBB, each once), then for every row with FACADE in key order uv its palette index, then likewise for ROOF. The
decoder's bounds (buildcolours.rs): 2 M rows a tile, 16 MiB inflated.

Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import os
import sys
import time
from array import array
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_osm_building_years as by   # noqa: E402  (the parts, folders and manifest: by.write_build; by.cut)

ots = by.ots
FORMAT = 1
SHARD_ZOOM = 12
PART_BYTES = ots.PART_BYTES
TILE_MAGIC = b"OBC1"
HEAD_BYTES = by.HEAD_BYTES      # (osmtagshards.HEAD_BYTES: a part's first read)
KEEP_BUILDS = by.KEEP_BUILDS
FACADE, ROOF = 1, 2
COLOUR_KEYS = ("building:colour", "roof:colour")
UNREAD_LOGGED = 40              # the most common values this cannot read, logged

# CSS Color Module 4's named colours (148, with the grey spellings), 0xRRGGBB
CSS_COLOURS = {
    "aliceblue": 0xf0f8ff, "antiquewhite": 0xfaebd7, "aqua": 0x00ffff, "aquamarine": 0x7fffd4, "azure": 0xf0ffff,
    "beige": 0xf5f5dc, "bisque": 0xffe4c4, "black": 0x000000, "blanchedalmond": 0xffebcd, "blue": 0x0000ff,
    "blueviolet": 0x8a2be2, "brown": 0xa52a2a, "burlywood": 0xdeb887, "cadetblue": 0x5f9ea0, "chartreuse": 0x7fff00,
    "chocolate": 0xd2691e, "coral": 0xff7f50, "cornflowerblue": 0x6495ed, "cornsilk": 0xfff8dc, "crimson": 0xdc143c,
    "cyan": 0x00ffff, "darkblue": 0x00008b, "darkcyan": 0x008b8b, "darkgoldenrod": 0xb8860b, "darkgray": 0xa9a9a9,
    "darkgreen": 0x006400, "darkgrey": 0xa9a9a9, "darkkhaki": 0xbdb76b, "darkmagenta": 0x8b008b,
    "darkolivegreen": 0x556b2f, "darkorange": 0xff8c00, "darkorchid": 0x9932cc, "darkred": 0x8b0000,
    "darksalmon": 0xe9967a, "darkseagreen": 0x8fbc8f, "darkslateblue": 0x483d8b, "darkslategray": 0x2f4f4f,
    "darkslategrey": 0x2f4f4f, "darkturquoise": 0x00ced1, "darkviolet": 0x9400d3, "deeppink": 0xff1493,
    "deepskyblue": 0x00bfff, "dimgray": 0x696969, "dimgrey": 0x696969, "dodgerblue": 0x1e90ff, "firebrick": 0xb22222,
    "floralwhite": 0xfffaf0, "forestgreen": 0x228b22, "fuchsia": 0xff00ff, "gainsboro": 0xdcdcdc,
    "ghostwhite": 0xf8f8ff, "gold": 0xffd700, "goldenrod": 0xdaa520, "gray": 0x808080, "green": 0x008000,
    "greenyellow": 0xadff2f, "grey": 0x808080, "honeydew": 0xf0fff0, "hotpink": 0xff69b4, "indianred": 0xcd5c5c,
    "indigo": 0x4b0082, "ivory": 0xfffff0, "khaki": 0xf0e68c, "lavender": 0xe6e6fa, "lavenderblush": 0xfff0f5,
    "lawngreen": 0x7cfc00, "lemonchiffon": 0xfffacd, "lightblue": 0xadd8e6, "lightcoral": 0xf08080,
    "lightcyan": 0xe0ffff, "lightgoldenrodyellow": 0xfafad2, "lightgray": 0xd3d3d3, "lightgreen": 0x90ee90,
    "lightgrey": 0xd3d3d3, "lightpink": 0xffb6c1, "lightsalmon": 0xffa07a, "lightseagreen": 0x20b2aa,
    "lightskyblue": 0x87cefa, "lightslategray": 0x778899, "lightslategrey": 0x778899, "lightsteelblue": 0xb0c4de,
    "lightyellow": 0xffffe0, "lime": 0x00ff00, "limegreen": 0x32cd32, "linen": 0xfaf0e6, "magenta": 0xff00ff,
    "maroon": 0x800000, "mediumaquamarine": 0x66cdaa, "mediumblue": 0x0000cd, "mediumorchid": 0xba55d3,
    "mediumpurple": 0x9370db, "mediumseagreen": 0x3cb371, "mediumslateblue": 0x7b68ee, "mediumspringgreen": 0x00fa9a,
    "mediumturquoise": 0x48d1cc, "mediumvioletred": 0xc71585, "midnightblue": 0x191970, "mintcream": 0xf5fffa,
    "mistyrose": 0xffe4e1, "moccasin": 0xffe4b5, "navajowhite": 0xffdead, "navy": 0x000080, "oldlace": 0xfdf5e6,
    "olive": 0x808000, "olivedrab": 0x6b8e23, "orange": 0xffa500, "orangered": 0xff4500, "orchid": 0xda70d6,
    "palegoldenrod": 0xeee8aa, "palegreen": 0x98fb98, "paleturquoise": 0xafeeee, "palevioletred": 0xdb7093,
    "papayawhip": 0xffefd5, "peachpuff": 0xffdab9, "peru": 0xcd853f, "pink": 0xffc0cb, "plum": 0xdda0dd,
    "powderblue": 0xb0e0e6, "purple": 0x800080, "rebeccapurple": 0x663399, "red": 0xff0000, "rosybrown": 0xbc8f8f,
    "royalblue": 0x4169e1, "saddlebrown": 0x8b4513, "salmon": 0xfa8072, "sandybrown": 0xf4a460, "seagreen": 0x2e8b57,
    "seashell": 0xfff5ee, "sienna": 0xa0522d, "silver": 0xc0c0c0, "skyblue": 0x87ceeb, "slateblue": 0x6a5acd,
    "slategray": 0x708090, "slategrey": 0x708090, "snow": 0xfffafa, "springgreen": 0x00ff7f, "steelblue": 0x4682b4,
    "tan": 0xd2b48c, "teal": 0x008080, "thistle": 0xd8bfd8, "tomato": 0xff6347, "turquoise": 0x40e0d0,
    "violet": 0xee82ee, "wheat": 0xf5deb3, "white": 0xffffff, "whitesmoke": 0xf5f5f5, "yellow": 0xffff00,
    "yellowgreen": 0x9acd32,
}
# the other names the server's buildings.COLOR_NAMES draws: the hex Overture writes for the name where it has one
# (buildings.OVERTURE_HEX: the server reads that hex as the name), else the server's own shade of it as a hex
# (tests/test_osm_building_colours.py keeps the two in step; this tool runs alone in the data repo, so it cannot
# import the server)
EXTRA_COLOURS = {
    "charcoal": 0x333338, "anthracite": 0x383b40, "lightred": 0xd95959, "burgundy": 0x731f33, "rose": 0xe68c99,
    "brick": 0xb22222, "mint": 0x99d9b2, "sage": 0x8ca68c, "moss": 0x597340, "cobalt": 0x264ca6, "steel": 0x73808c,
    "amber": 0xe0991f, "darkbrown": 0x4c331f, "lightbrown": 0x997352, "rust": 0x994026, "cream": 0xfffdd0,
    "terracotta": 0xe2725b, "ochre": 0xcc8c33, "sand": 0xe0cca6, "copper": 0x8cbfa6, "bronze": 0x8c6b40,
}
_HEX = set("0123456789abcdef")


def parse_colour(value):
    """A mapped colour value as 0xRRGGBB, or None (the module's docstring: what is read)."""
    if not isinstance(value, str):
        return None
    s = value.split(";", 1)[0].strip().lower()
    for c in " _-":
        s = s.replace(c, "")
    if s.startswith("#"):
        h = s[1:]
        if not h or not set(h) <= _HEX:
            return None
        if len(h) in (3, 4):
            return int(h[0] * 2 + h[1] * 2 + h[2] * 2, 16)
        if len(h) in (6, 8):
            return int(h[:6], 16)
        return None
    if len(s) == 6 and set(s) <= _HEX:
        return int(s, 16)
    got = CSS_COLOURS.get(s)
    return got if got is not None else EXTRA_COLOURS.get(s)


def is_building(tags):
    """A building or building part (building=* / building:part=*, not "no")."""
    return any(tags.get(k) not in (None, "no") for k in ("building", "building:part"))


def colours_of(tags, unread=None):
    """(facade, roof) of an object's tags: 0xRRGGBB or -1 each; None when neither is had (or it is no building).
    unread: a Counter of the values this could not read."""
    if not is_building(tags):
        return None
    out = []
    for k in COLOUR_KEYS:
        v = tags.get(k)
        c = parse_colour(v) if v is not None else None
        if c is None and v is not None and unread is not None:
            unread[v] += 1
        out.append(-1 if c is None else c)
    return None if out == [-1, -1] else tuple(out)


def key_of(osm_type, osm_id):
    return by.key_of(osm_type, osm_id)


def encode_tile(keys, facade, roof):
    """A tile's blob (before deflate) of its rows: keys ascending and unique; facade, roof 0xRRGGBB or -1 (none)."""
    keys = np.asarray(keys, np.uint64)
    fac = np.asarray(facade, np.int64)
    rf = np.asarray(roof, np.int64)
    flags = ((fac >= 0) * FACADE + (rf >= 0) * ROOF).astype(np.uint8)
    if not flags.all():
        raise ValueError("a row with neither colour (the decoder refuses the tile)")
    pal = np.unique(np.concatenate([fac[fac >= 0], rf[rf >= 0]]))
    rgb = np.stack([(pal >> 16) & 255, (pal >> 8) & 255, pal & 255], 1).astype(np.uint8)
    d = np.diff(keys, prepend=np.uint64(0)) if len(keys) else keys
    return (TILE_MAGIC + ots.uv1(len(keys)) + ots.uv(d) + flags.tobytes() + ots.uv1(len(pal)) + rgb.tobytes()
            + ots.uv(np.searchsorted(pal, fac[fac >= 0])) + ots.uv(np.searchsorted(pal, rf[rf >= 0])))


def _pack(fac, roof):
    """(facade, roof) as one int64 column for by.cut (each + 1, 0: none)."""
    return (np.asarray(fac, np.int64) + 1) | ((np.asarray(roof, np.int64) + 1) << 25)


def _encode_packed(keys, packed):
    p = np.asarray(packed, np.int64)
    return encode_tile(keys, (p & ((1 << 25) - 1)) - 1, (p >> 25) - 1)


# ------------------------------------------------------------------------------------------------ the planet
class _Stream:
    """A pass's input: a path (any format osmium reads by its name), '-' (stdin), or a URL streamed by curl (http(s),
    file: tests). A pass may stop() early (it has all it needs: the rest of the planet is other types); otherwise a
    curl that did not end cleanly fails the build instead of leaving it partial."""

    def __init__(self, src):
        self.src, self.proc, self.stopped = src, None, False

    def __enter__(self):
        import osmium
        if self.src.startswith(("http://", "https://", "file://")):
            import subprocess
            self.proc = subprocess.Popen(["curl", "-sfL", "--retry", "5", "-A", "BeamNG.Earth building colours build",
                                          self.src], stdout=subprocess.PIPE)
            return osmium.io.File(f"/proc/self/fd/{self.proc.stdout.fileno()}", "pbf")
        if self.src == "-":
            return osmium.io.File("", "pbf")
        return self.src

    def stop(self):
        self.stopped = True

    def __exit__(self, *exc):
        if self.proc is None:
            return
        if self.stopped:
            self.proc.kill()
        self.proc.stdout.close()
        rc = self.proc.wait()
        if not self.stopped and rc != 0 and exc[0] is None:
            raise SystemExit(f"curl {self.src}: exit {rc}")


def first_pass(src, log=print):
    """The colour ways and relations: (ways, rels, unread). ways: (ids, facade, roof, refs, offsets: refs[offsets[i]:
    offsets[i + 1]] way i's nodes); rels: (ids, facade, roof, member way ids, offsets); unread: a Counter."""
    import osmium
    w_id, w_f, w_r, refs, w_off = array("q"), array("q"), array("q"), array("q"), array("q", [0])
    r_id, r_f, r_r, mem, r_off = array("q"), array("q"), array("q"), array("q"), array("q", [0])
    unread, t0, k = Counter(), time.time(), 0
    with _Stream(src) as f:
        fp = osmium.FileProcessor(f, osmium.osm.WAY | osmium.osm.RELATION)
        for o in fp.with_filter(osmium.filter.KeyFilter(*COLOUR_KEYS)):
            k += 1
            if k % 500_000 == 0:
                log(f"  pass 1: {k:,} objects, {len(w_id):,} ways, {len(r_id):,} relations, {time.time() - t0:.0f} s")
            c = colours_of(o.tags, unread)
            if c is None:
                continue
            if o.is_way():
                n0 = len(refs)
                refs.extend(n.ref for n in o.nodes)
                if len(refs) == n0:
                    continue
                w_id.append(o.id)
                w_f.append(c[0])
                w_r.append(c[1])
                w_off.append(len(refs))
            elif o.is_relation():
                m0 = len(mem)
                mem.extend(m.ref for m in o.members if m.type == "w" and m.role != "inner")
                if len(mem) == m0:
                    continue
                r_id.append(o.id)
                r_f.append(c[0])
                r_r.append(c[1])
                r_off.append(len(mem))
    a = lambda v: np.frombuffer(v, np.int64).copy() if len(v) else np.zeros(0, np.int64)    # noqa: E731
    return (a(w_id), a(w_f), a(w_r), a(refs), a(w_off)), (a(r_id), a(r_f), a(r_r), a(mem), a(r_off)), unread


def _located(ids, lon, lat):
    i = np.asarray(ids, np.int64)
    o = np.argsort(i, kind="stable")
    return i[o], np.asarray(lon, np.float64)[o], np.asarray(lat, np.float64)[o]


def second_pass(src, node_ids, way_ids, log=print):
    """((node ids sorted, lon, lat) of node_ids, (member way ids, their refs, offsets)): the nodes of the colour ways
    and the member ways' node refs, stopping at the planet's first relation."""
    import osmium
    nid, nlon, nlat = array("q"), array("d"), array("d")
    mw, mrefs, moff = array("q"), array("q"), array("q", [0])
    t0 = time.time()
    st = _Stream(src)
    with st as f:
        fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION)
        fp = fp.with_filter(osmium.filter.IdFilter(node_ids.tolist()).enable_for(osmium.osm.NODE))
        fp = fp.with_filter(osmium.filter.IdFilter(way_ids.tolist()).enable_for(osmium.osm.WAY))
        for o in fp:
            if o.is_node():
                if o.location.valid():
                    nid.append(o.id)
                    nlon.append(o.location.lon)
                    nlat.append(o.location.lat)
                    if len(nid) % 5_000_000 == 0:
                        log(f"  pass 2: {len(nid):,} of {len(node_ids):,} nodes, {time.time() - t0:.0f} s")
            elif o.is_way():
                mw.append(o.id)
                mrefs.extend(n.ref for n in o.nodes)
                moff.append(len(mrefs))
            else:
                st.stop()           # (the relations: nothing more this pass needs)
                break
    return (_located(nid, nlon, nlat),
            (np.asarray(mw, np.int64), np.asarray(mrefs, np.int64), np.asarray(moff, np.int64)))


def nodes_pass(src, node_ids, log=print):
    """(node ids sorted, lon, lat) of node_ids, stopping at the planet's first way."""
    import osmium
    nid, nlon, nlat = array("q"), array("d"), array("d")
    st = _Stream(src)
    with st as f:
        fp = osmium.FileProcessor(f, osmium.osm.NODE | osmium.osm.WAY)
        for o in fp.with_filter(osmium.filter.IdFilter(node_ids.tolist()).enable_for(osmium.osm.NODE)):
            if not o.is_node():
                st.stop()           # (the ways: nothing more this pass needs)
                break
            if o.location.valid():
                nid.append(o.id)
                nlon.append(o.location.lon)
                nlat.append(o.location.lat)
    return _located(nid, nlon, nlat)


def boxes(refs, offs, nid, nlon, nlat):
    """(x0, y0, x1, y1) float64 per row of refs[offs[i]:offs[i + 1]] from the located nodes (nan: none located)."""
    n = len(offs) - 1
    out = np.full((n, 4), np.nan)
    if n <= 0 or not len(refs) or not len(nid):
        return out
    row = np.repeat(np.arange(n), np.diff(offs))
    j = np.minimum(np.searchsorted(nid, refs), len(nid) - 1)
    ok = nid[j] == refs
    row, j = row[ok], j[ok]
    x0, y0 = np.full(n, np.inf), np.full(n, np.inf)
    x1, y1 = np.full(n, -np.inf), np.full(n, -np.inf)
    np.minimum.at(x0, row, nlon[j])
    np.minimum.at(y0, row, nlat[j])
    np.maximum.at(x1, row, nlon[j])
    np.maximum.at(y1, row, nlat[j])
    had = np.isfinite(x0)
    out[had] = np.stack([x0, y0, x1, y1], 1)[had]
    return out


def read_planet(src, second=None, third=None, log=print):
    """(keys uint64, facade int64, roof int64, bbox float64 (n, 4), unread Counter) of every colour row of src (a
    path, '-', or a URL; second / third: the input for passes 2 / 3 when not src again: '-' for stdin). Rows whose
    nodes were not all missing; in key order."""
    t0 = time.time()
    (w_id, w_f, w_r, refs, w_off), (r_id, r_f, r_r, mem, r_off), unread = first_pass(src, log)
    log(f"pass 1: {len(w_id):,} ways, {len(r_id):,} relations with a colour, {sum(unread.values()):,} values not read, "
        f"{time.time() - t0:.0f} s")
    want_nodes = np.unique(refs)
    want_ways = np.unique(mem)
    (nid, nlon, nlat), (mw, mrefs, moff) = second_pass(second or src, want_nodes, want_ways, log)
    log(f"pass 2: {len(nid):,} nodes, {len(mw):,} member ways, {time.time() - t0:.0f} s")
    if len(mrefs):
        more = np.setdiff1d(np.unique(mrefs), nid, assume_unique=True)
        if len(more):
            n2 = nodes_pass(third or second or src, more, log)
            nid, nlon, nlat = _located(np.concatenate([nid, n2[0]]), np.concatenate([nlon, n2[1]]),
                                       np.concatenate([nlat, n2[2]]))
        log(f"pass 3: {len(more):,} member ways' nodes asked, {len(nid):,} nodes in all, {time.time() - t0:.0f} s")
    wb = boxes(refs, w_off, nid, nlon, nlat)
    # a relation's box: its member ways' boxes together
    mb = boxes(mrefs, moff, nid, nlon, nlat)
    o = np.argsort(mw, kind="stable")
    mw, mb = mw[o], mb[o]
    rb = np.full((len(r_id), 4), np.nan)
    if len(r_id) and len(mw):
        row = np.repeat(np.arange(len(r_id)), np.diff(r_off))
        j = np.minimum(np.searchsorted(mw, mem), len(mw) - 1)
        ok = (mw[j] == mem) & np.isfinite(mb[j, 0])
        row, j = row[ok], j[ok]
        lo = np.full((len(r_id), 2), np.inf)
        hi = np.full((len(r_id), 2), -np.inf)
        for c in (0, 1):
            np.minimum.at(lo[:, c], row, mb[j, c])
            np.maximum.at(hi[:, c], row, mb[j, c + 2])
        had = np.isfinite(lo[:, 0])
        rb[had] = np.concatenate([lo, hi], 1)[had]
    keys = np.concatenate([2 * w_id.astype(np.uint64), 2 * r_id.astype(np.uint64) + np.uint64(1)])
    fac = np.concatenate([w_f, r_f])
    roof = np.concatenate([w_r, r_r])
    bb = np.concatenate([wb, rb])
    ok = np.isfinite(bb[:, 0])
    log(f"{int(ok.sum()):,} rows placed ({int((~ok).sum()):,} without a node), {time.time() - t0:.0f} s")
    keys, fac, roof, bb = keys[ok], fac[ok], roof[ok], bb[ok]
    o = np.argsort(keys, kind="stable")
    return keys[o], fac[o], roof[o], bb[o], unread


def build(src, out, bbox=None, second=None, third=None, log=print, z=SHARD_ZOOM, part_bytes=PART_BYTES):
    """The archive's parts and the manifest in out from src (the planet or an extract: a path, '-', a URL; bbox: only
    that region's rows and tiles)."""
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    keys, fac, roof, bb, unread = read_planet(src, second, third, log)
    return build_rows(keys, fac, roof, bb, out, source=src if "://" in src else os.path.basename(src), bbox=bbox,
                      unread=unread, log=log, z=z, part_bytes=part_bytes, t0=t0)


def build_rows(keys, fac, roof, bb, out, source, bbox=None, unread=None, log=print, z=SHARD_ZOOM,
               part_bytes=PART_BYTES, t0=None):
    """The archive's parts and the manifest in out from rows (read_planet's: keys unique, facade, roof, bbox)."""
    t0 = time.time() if t0 is None else t0
    unread = unread or Counter()
    os.makedirs(out, exist_ok=True)
    keys, fac, roof = np.asarray(keys, np.uint64), np.asarray(fac, np.int64), np.asarray(roof, np.int64)
    bb = np.asarray(bb, np.float64).reshape(-1, 4)
    clip = None
    if bbox is not None:
        bx0, by0, bx1, by1 = bbox
        keep = (bb[:, 0] <= bx1) & (bb[:, 2] >= bx0) & (bb[:, 1] <= by1) & (bb[:, 3] >= by0)
        keys, fac, roof, bb = keys[keep], fac[keep], roof[keep], bb[keep]
        clip = tuple(int(v[0]) for v in ots.tile_ranges(*(np.array([v], np.float32) for v in bbox), z))
    tiles = by.cut(keys, _pack(fac, roof), bb.astype(np.float32), z, clip)
    log(f"{len(keys):,} rows ({int((fac >= 0).sum()):,} facade, {int((roof >= 0).sum()):,} roof colours) in "
        f"{len(tiles):,} tiles, {time.time() - t0:.0f} s")
    if unread:
        log("not read: " + ", ".join(f"{v!r} {n:,}" for v, n in sorted(unread.items(), key=lambda t: (-t[1], t[0]))
                                     [:UNREAD_LOGGED]))
    return by.write_build(tiles, _encode_packed, out, z=z, part_bytes=part_bytes, bbox=bbox, prefix="osmcolours",
                          title="BeamNG.Earth OSM building colours", fmt=f"obc{FORMAT}", fmt_num=FORMAT,
                          source=source, source_etag=None, log=log, t0=t0,
                          extra={"facade": int((fac >= 0).sum()), "roof": int((roof >= 0).sum()),
                                 "unread": int(sum(unread.values()))})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("planet", help="the planet (or an extract): a path, an http(s) URL (streamed by curl), '-'")
    ap.add_argument("out")
    ap.add_argument("--second-pass", help="the planet again for pass 2 (a path, or '-' stdin: streaming)")
    ap.add_argument("--third-pass", help="... and for pass 3")
    ap.add_argument("--bbox", help="lon0,lat0,lon1,lat1: only this region (tests)")
    ap.add_argument("--part-bytes", type=int, default=PART_BYTES, help="an archive part at most (bytes)")
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(",")) if a.bbox else None
    build(a.planet, a.out, bbox, a.second_pass, a.third_pass, log=lambda s: print(s, flush=True),
          part_bytes=a.part_bytes)


if __name__ == "__main__":
    sys.exit(main())
