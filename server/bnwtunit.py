"""The parquet path's cells of a whole block of cells at once (tools/build_bnwt.py): one query for the block instead of
one per cell, then each cell's layer made from the block's rows exactly as overture.Reader.query makes it.

overture.Reader.query for a cell: the STAC files and row groups whose boxes touch the cell (_plan), the rows whose
float32 bbox touches it (the SQL bbox test), one row per id (the highest version, then the smallest WKB), in id order;
the geometry from WKB, a clip layer's polygons clipped to the cell (rows clipped away dropped), the columns built by
mapgeom's column builders from the kept rows, the sources' counts over all the cell's rows. Here: the block's rows
once (every version kept, numbered by id, version descending, WKB), each row's geometry parsed once; per cell the
rows its bbox test keeps (earthfast bnwt_assign: in the columns' own type, as DuckDB compares) of the files and row
groups whose boxes and statistics touch the cell (the cell's own query reads only those), the first row of each id,
then the same steps as Reader._run with the same functions, so the cell is the same arrays bit for bit
(tests/test_bnwt.py and the builder's --check compare it with Reader.query itself). Layers: those without a
`where`, `simplify` or point geometry (the base theme)."""
import os

import numpy as np

import fast
import mapcells
import mapgeom as mg
import overture

LAYERS = ("water", "land_use", "infrastructure")


class UnitRows:
    """A block's rows of one layer (in the block query's order: id, version descending, WKB)."""

    def __init__(self, layer, rows, at, srcs, grp, gtest):
        self.layer, self.rows, self.at = layer, rows, at
        self.srcs = srcs                # row -> [(dataset, licence)] of its sources
        self.grp = grp                  # row -> its row group (index into gtest)
        self.gtest = gtest              # row group -> (its file's STAC box, its bbox statistics) for the cell tests
        self.n = len(rows)
        i = at
        self.box = [np.array([r[i[c]] for r in rows], np.float64) for c in ("bx0", "bx1", "by0", "by1")]
        self.box32 = False              # (the bbox columns are FLOAT: the test in float32, as DuckDB makes it)
        self._geom = {}

    def geom(self, i):
        g = self._geom.get(i)
        if g is None:
            g = self._geom[i] = mg.parse_wkb(self.rows[i][self.at["wkb"]])
        return g


def unit_rows(rd, layer, bounds, kind="demand"):
    """UnitRows of layer for the block bounds (lon0, lat0, lon1, lat1), or None when no row touches it."""
    spec = overture.LAYERS[layer]
    if layer not in LAYERS or spec.get("where") or spec.get("simplify") or spec.get("geometry") is False:
        raise ValueError(f"{layer}: not a layer bnwtunit makes")
    hits = rd.index.files(spec["theme"], spec["type"], bounds, kind)
    if not hits:
        return None
    files = {f"{spec['theme']}/{spec['type']}/{name}": rd._remote(spec["theme"], spec["type"], name, urls, size)
             for name, urls, size in hits}
    con, proto, errors = rd._connect(files, kind)
    try:
        paths = sorted(files)
        rows = rd._plan(con, proto, paths, files, spec, bounds, kind)
        if not rows:
            return None
        spec = overture.tolerant(spec, getattr(overture._PLAN, "present", None), layer)
        local = rd._local_rows(con, layer, spec, rows, files, kind)
        # (a cell's own query keeps only the rows of the files whose STAC box touches it (index.files) and of the row
        # groups whose bbox statistics do (_plan's candidates): both compared in double, not the rows' float32)
        c = rd.index.collection(spec["theme"], spec["type"], kind)
        fbox = {r[0]: r[1] for r in c["rows"]} if "rows" in c else \
            {h.rstrip("/").rsplit("/", 1)[-1]: bb for h, bb in zip(c["items"], c["bboxes"])}
        gtest, gi = [], {}
        for (uri, first, _last), lf in zip(rows, local):
            path = uri.split("://", 1)[-1]
            g = next(g for g in rd._meta[path].values() if g["first"] == first)
            name = path.rsplit("/", 1)[-1]
            bb = fbox.get(name) or fbox.get(name.rsplit(".", 1)[0])
            if bb is None:
                bb = next(v for k, v in fbox.items() if name.startswith(k) or k.startswith(name))

            def stat(leaf, i, default, g=g):
                try:
                    return float(g["bbox"][leaf][i])
                except (KeyError, TypeError, ValueError):
                    return default
            gi[os.path.abspath(lf)] = len(gtest)
            gtest.append((tuple(bb), (stat("xmin", 0, -1e9), stat("xmax", 1, 1e9), stat("ymin", 0, -1e9),
                                      stat("ymax", 1, 1e9))))
        lon0, lat0, lon1, lat1 = bounds
        bbox = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND "
                f"bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}")
        con.execute(f"""CREATE TEMP TABLE s AS SELECT * EXCLUDE (bbox, filename), bbox.xmin AS bx0, bbox.xmax AS bx1,
              bbox.ymin AS by0, bbox.ymax AS by1, filename AS lf
              FROM read_parquet([{", ".join(f"'{overture._q(f)}'" for f in local)}], filename = true) WHERE {bbox}""")
        con.execute("""CREATE TEMP TABLE t AS
            SELECT row_number() OVER (ORDER BY id, version DESC, wkb) - 1 AS ui, * FROM s""")
        names = ["id", "wkb", "cx", "cy", "bx0", "bx1", "by0", "by1", "lf"] + \
            [c for k in ("text", "float", "bool") for c in spec.get(k, ())]
        got = con.execute(f"SELECT {', '.join(names)} FROM t ORDER BY ui").fetchall()
        srcs = [[] for _ in got]
        for ui, ds, lic in con.execute("""SELECT ui, s.dataset, s.license FROM
                (SELECT ui, unnest(sources) AS s FROM t)""").fetchall():
            srcs[ui].append((ds, lic))
        at = {c: i for i, c in enumerate(names)}
        grp = [gi[os.path.abspath(r[at["lf"]])] for r in got]
        ur = UnitRows(layer, got, at, srcs, grp, gtest)
        ur.box32 = con.execute("SELECT typeof(bx0) FROM s LIMIT 1").fetchone() == ("FLOAT",)
        return ur
    finally:
        con.close()


def assign(ur, x0, y0, nx, ny):
    """(offsets, rows) of the cells x0..x0+nx-1, y0..y0+ny-1 (z14): cell (ix, iy) at index ix * ny + iy."""
    n = 1 << 14
    lon = [mapcells.CellKey(14, x0 + i, y0).bounds()[0] for i in range(nx)] + \
        [mapcells.CellKey(14, x0 + nx - 1, y0).bounds()[2]]
    lat = [mapcells.CellKey(14, x0, y0 + j).bounds()[3] for j in range(ny)] + \
        [mapcells.CellKey(14, x0, y0 + ny - 1).bounds()[1]]
    assert x0 + nx <= n and y0 + ny <= n
    return fast._ef.bnwt_assign(*ur.box, lon, lat, ur.box32)


def cell_layer(ur, idx, cell):
    """(Features, sources) of the cell from the block's rows idx (ascending: assign's), as Reader.query gives it."""
    layer = ur.layer
    spec = overture.LAYERS[layer]
    if ur is None or not len(idx):
        return overture.empty(layer), {}
    at, R = ur.at, ur.rows
    bounds = cell.bounds()
    lon0, lat0, lon1, lat1 = bounds
    gok = [overture.intersects(bb, bounds) and x0 <= lon1 and x1 >= lon0 and y0 <= lat1 and y1 >= lat0
           for bb, (x0, x1, y0, y1) in ur.gtest]
    idx = [i for i in idx if gok[ur.grp[i]]]
    if not idx:
        return overture.empty(layer), {}
    t, seen = [], set()
    for i in idx:                       # (one row per id: the block's order puts its highest version first)
        fid = R[i][at["id"]]
        if fid not in seen:
            seen.add(fid)
            t.append(int(i))
    ids = mg.text_column([R[i][at["id"]] for i in t])
    geoms = [ur.geom(i) for i in t]
    clipped = np.zeros(len(t), bool)
    if spec["clip"]:
        keep = []
        for j, (kind, parts) in enumerate(geoms):
            if kind == mg.POLYGON:
                parts, changed = mg.clip_polygon_parts(parts, bounds)
                geoms[j] = (kind, parts)
                clipped[j] = changed
            keep.append(bool(parts))
        keep = np.array(keep, bool)
    else:
        keep = np.ones(len(t), bool)
    feats = mg.build(layer, ids[keep], [g for g, k in zip(geoms, keep) if k])
    feats.clipped = clipped[keep]
    sel = [R[i] for i, k in zip(t, keep) if k]
    feats.attrs["cx"] = np.array([r[at["cx"]] for r in sel], np.float64)
    feats.attrs["cy"] = np.array([r[at["cy"]] for r in sel], np.float64)
    for c in spec.get("text", ()):
        feats.attrs[c] = mg.text_column([r[at[c]] for r in sel])
    for c in spec.get("float", ()):
        feats.attrs[c] = mg.float_column([r[at[c]] for r in sel])
    for c in spec.get("bool", ()):
        feats.attrs[c] = mg.bool_column([r[at[c]] for r in sel])
    # (the SQL's GROUP BY ... ORDER BY 1, 2: NULLs last; then (ds or "", lic or "") as Reader._run keys them)
    counts = {}
    for i in t:
        for s in ur.srcs[i]:
            counts[s] = counts.get(s, 0) + 1
    sources = {}
    for (ds, lic), n in sorted(counts.items(), key=lambda kv: (kv[0][0] is None, kv[0][0] or "",
                                                               kv[0][1] is None, kv[0][1] or "")):
        sources[(ds or "", lic or "")] = int(n)
    return feats, sources
