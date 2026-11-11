"""Reading an Esri File Geodatabase's 3-D layers (swissBUILDINGS3D) for tools/build_lod2_roofs.py.

pyogrio refuses layers whose geometry type is a TIN or a multipatch, and GDAL's own Python bindings need a system
GDAL. This talks to the GDAL that pyogrio's wheel bundles through its C API (ctypes): a layer's features as
(attributes, ISO WKB), and the WKB as rings of points. Offline tool only (pip install pyogrio).
"""
import ctypes
import glob
import os
import struct

import numpy as np

_G = None


def _gdal():
    global _G
    if _G is not None:
        return _G
    import pyogrio      # (loads the bundled library's own dependencies first)
    libs = glob.glob(os.path.join(os.path.dirname(pyogrio.__file__), "..", "pyogrio.libs", "libgdal*")) + \
        glob.glob(os.path.join(os.path.dirname(pyogrio.__file__), "gdal*.dll"))
    if not libs:
        raise SystemExit("pyogrio's bundled GDAL not found (pip install pyogrio)")
    g = ctypes.CDLL(libs[0])
    v, p, i, s = None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    for name, res, args in [
            ("GDALAllRegister", v, []), ("GDALOpenEx", p, [s, ctypes.c_uint, p, p, p]), ("GDALClose", v, [p]),
            ("GDALDatasetGetLayerByName", p, [p, s]), ("OGR_L_GetNextFeature", p, [p]),
            ("OGR_L_ResetReading", v, [p]), ("OGR_L_GetLayerDefn", p, [p]), ("OGR_FD_GetFieldCount", i, [p]),
            ("OGR_FD_GetFieldDefn", p, [p, i]), ("OGR_Fld_GetNameRef", s, [p]),
            ("OGR_F_GetFieldAsString", s, [p, i]), ("OGR_F_IsFieldSetAndNotNull", i, [p, i]),
            ("OGR_F_GetGeometryRef", p, [p]), ("OGR_F_Destroy", v, [p]), ("OGR_G_WkbSize", i, [p]),
            ("OGR_G_ExportToIsoWkb", i, [p, i, s])]:
        f = getattr(g, name)
        f.restype, f.argtypes = res, args
    g.GDALAllRegister()
    _G = g
    return g


class Dataset:
    def __init__(self, path):
        self.g = _gdal()
        self.ds = self.g.GDALOpenEx(path.encode(), 0x04, None, None, None)     # (GDAL_OF_VECTOR)
        if not self.ds:
            raise IOError("cannot open " + path)

    def close(self):
        if self.ds:
            self.g.GDALClose(self.ds)
            self.ds = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def features(self, layer, fields):
        """(attributes {name: str or None}, ISO WKB bytes or None) of each feature of a layer (none: no layer)."""
        g = self.g
        lyr = g.GDALDatasetGetLayerByName(self.ds, layer.encode())
        if not lyr:
            return
        defn = g.OGR_L_GetLayerDefn(lyr)
        idx = [(i, g.OGR_Fld_GetNameRef(g.OGR_FD_GetFieldDefn(defn, i)).decode())
               for i in range(g.OGR_FD_GetFieldCount(defn))]
        idx = [(i, n) for i, n in idx if n in fields]
        g.OGR_L_ResetReading(lyr)
        while True:
            f = g.OGR_L_GetNextFeature(lyr)
            if not f:
                return
            try:
                at = {n: g.OGR_F_GetFieldAsString(f, i).decode("utf-8", "replace")
                      if g.OGR_F_IsFieldSetAndNotNull(f, i) else None for i, n in idx}
                geom = g.OGR_F_GetGeometryRef(f)
                wkb = None
                if geom:
                    buf = ctypes.create_string_buffer(g.OGR_G_WkbSize(geom))
                    g.OGR_G_ExportToIsoWkb(geom, 1, buf)
                    wkb = buf.raw
                yield at, wkb
            finally:
                g.OGR_F_Destroy(f)


def rings(wkb):
    """[(n, 3) points] of the outer ring of every polygon of a 3-D ISO WKB geometry: polygons, triangles, TINs,
    polyhedral surfaces, multipolygons, collections ((n, 2) for 2-D ones). Holes are left out (a hole runs the other
    way round: read as a face it would face down)."""
    out = []
    if wkb:
        _geom(memoryview(wkb), 0, out)
    return out


def _geom(b, o, out):
    e = "<" if b[o] == 1 else ">"
    t = struct.unpack_from(e + "I", b, o + 1)[0]
    o += 5
    base, dim = t % 1000, {0: 2, 1: 3, 2: 3, 3: 4}[t // 1000]
    if base in (3, 17):         # polygon, triangle
        n = struct.unpack_from(e + "I", b, o)[0]
        o += 4
        for k in range(n):
            m = struct.unpack_from(e + "I", b, o)[0]
            o += 4
            if k == 0:
                out.append(np.frombuffer(b, e + "f8", m * dim, o).reshape(m, dim)[:, :3])
            o += 8 * m * dim
        return o
    if base in (4, 5, 6, 7, 15, 16):     # multi*, collection, polyhedral surface, TIN
        n = struct.unpack_from(e + "I", b, o)[0]
        o += 4
        for _ in range(n):
            o = _geom(b, o, out)
        return o
    if base == 1:
        return o + 8 * dim
    if base == 2:
        return o + 4 + 8 * dim * struct.unpack_from(e + "I", b, o)[0]
    raise ValueError(f"WKB type {t}")
