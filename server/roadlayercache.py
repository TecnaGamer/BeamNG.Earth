"""The roads layer as mapdata.MapData._decoded_layer leaves it, kept on disk for the server's other processes and the
next run: a cell's roads with OpenStreetMap's rail and lane tags joined, the level ways promoted, the merged highways'
lane sections split out and the bends rounded (roadsmooth.py). Every process made them again for each cell it read
(~20 ms of thread CPU a city cell, the lane sections and the rounding 0.9 of it: 752 decodes, 17 CPU-s of a first visit
to York PA across its processes, 4.7 s of them in the process whose first graded block everything waited for;
2026-11-12), and none of it was shared.

One folder per cell beside the layer's mirrored arrays (<cache>/mapdata-mirror/.../<y>.dec: the arrays in one zstd
frame and an "ok" marker; counted and cut back with the mirror, mapdata.prune_mirror). The marker's first line is the key: this
format, the code (revisit.code_key: any change of the server's modules or its native module makes them again), the
layer file's stamp (size and time), the tag data's versions and the modules' switches. Its JSON holds the arrays'
table, the attribute and rule names in the layer's own order, the object columns (the joined tags: None, strings,
numbers, lists, tuples, dicts, numpy scalars and arrays, each given back as its own type), the state kept on the layer
(the tag nodes, the lane sections' joints), and the neighbour
cells the lane sections read with their files' stamps (checked again at each read).

Only a complete layer is kept: nothing it read failed (revisit.thread_faults unchanged), every neighbour cell was read,
and it holds nothing this file cannot give back exactly (an unknown value type, an attribute on the layer this file
does not know: not kept, made again as before). A read gives arrays of its own (copies, writable: as a decode's)."""
import json
import logging
import os
import shutil
import threading

import numpy as np

import mapgeom

log = logging.getLogger("earthd.roadlayercache")

ON = os.environ.get("BNE_ROADS_DEC", "on") not in ("off", "0", "false")
FORMAT = 1
# kept on the layer beside its arrays (_lane_section_joints: lanesections' joints as the split made them, before
# the bends were rounded: read from the rounded lines instead, a slip road's mouth stood elsewhere and one building of
# an I-83 tile was cut differently, 2026-11-12)
STATE = ("_lanes_built", "_rails_built", "_rail_nodes", "_sign_nodes", "_road_revision", "_lane_section_joints")
MEMOS = ("_xy_in", "_centres", "_boxes", "_boxes_in", "_ll_boxes", "_lonlat_boxes", "_feature_boxes", "_ecef",
         "_id_pos", "_ids_at")      # pure in the arrays, made again when asked for: not kept
FIELDS = ("layer", "ids", "kind", "part_off", "ring_off", "coord_off", "lonlat", "en", "own", "clipped", "attrs", "rules")
_T = "\x00t"                # the marks in the JSON: a tuple,
_A = "\x00a"                # a numpy array [dtype, shape, its bytes in hex],
_S = "\x00s"                # a numpy scalar [dtype, its bytes in hex],
_D = "\x00d"                # a dict with keys that are not strings [[key, value], ...]
_MARKS = (_T, _A, _S, _D)


class Unkept(Exception):
    """The layer holds something that cannot be given back exactly."""


def _enc(v):
    t = type(v)
    if v is None or t in (str, int, float, bool):
        return v
    if t is list:
        return [_enc(x) for x in v]
    if t is tuple:
        return {_T: [_enc(x) for x in v]}
    if t is dict:
        if any(type(k) is not str or k in _MARKS for k in v):
            return {_D: [[_enc(k), _enc(x)] for k, x in v.items()]}
        return {k: _enc(x) for k, x in v.items()}
    if t is np.ndarray and not v.dtype.hasobject:
        return {_A: [np.lib.format.dtype_to_descr(v.dtype), list(v.shape), np.ascontiguousarray(v).tobytes().hex()]}
    if isinstance(v, np.generic) and not v.dtype.hasobject:
        return {_S: [np.lib.format.dtype_to_descr(v.dtype), v.tobytes().hex()]}
    raise Unkept(t.__name__)


def _dec(v):
    if type(v) is list:
        return [_dec(x) for x in v]
    if type(v) is dict:
        if len(v) == 1:
            if _T in v:
                return tuple(_dec(x) for x in v[_T])
            if _D in v:
                return {_dec(k): _dec(x) for k, x in v[_D]}
            if _A in v:
                descr, shape, hx = v[_A]
                return np.frombuffer(bytes.fromhex(hx), _dtype(descr)).reshape(shape).copy()
            if _S in v:
                descr, hx = v[_S]
                return np.frombuffer(bytes.fromhex(hx), _dtype(descr))[0]
        return {k: _dec(x) for k, x in v.items()}
    return v


def _dtype(descr):
    return np.lib.format.descr_to_dtype(descr if isinstance(descr, str) else [tuple(x) for x in descr])


def key(code, stamp, rails, lanes, switches):
    return json.dumps([FORMAT, code, stamp, rails, lanes, switches], sort_keys=True)


def encode(f):
    """(arrays {name: ndarray}, meta dict) of Features f, or raises Unkept."""
    extra = set(f.__dict__) - set(FIELDS) - set(STATE) - set(MEMOS) - {"_bnge_gradesep", "_bnge_neighbours", "_bnge_cell"}
    if extra:
        raise Unkept("state " + ",".join(sorted(extra)))
    arrays, objs = {}, {}
    for name in ("ids", "kind", "part_off", "ring_off", "coord_off", "lonlat", "en", "own", "clipped"):
        v = getattr(f, name)
        if v is not None:
            if not isinstance(v, np.ndarray):
                raise Unkept(name)
            arrays["f." + name] = v
    for k, v in f.attrs.items():
        if not isinstance(v, np.ndarray):
            raise Unkept("attr " + k)
        if v.dtype.hasobject:
            if v.ndim != 1:
                raise Unkept("attr " + k)
            # (its values that are not None, at their rows: most joined tag columns are None all the way down, and
            # a value per row was 7 of a big cell's 16 ms to read back)
            vals = v.tolist()
            at = [i for i, x in enumerate(vals) if x is not None]
            objs[k] = {"n": len(vals), "at": at, "v": [_enc(vals[i]) for i in at]}
        else:
            arrays["a." + k] = v
    for k, r in f.rules.items():
        for col in ("feature", "value", "start", "end"):
            v = getattr(r, col)
            if not isinstance(v, np.ndarray) or v.dtype.hasobject:
                raise Unkept(f"rule {k}.{col}")
            arrays[f"r.{k}.{col}"] = v
    state = {}
    for k in STATE:
        if k in f.__dict__:
            state[k] = _enc(f.__dict__[k])
    meta = {"layer": f.layer, "attrs": list(f.attrs), "rules": list(f.rules), "objs": objs, "state": state}
    return arrays, meta


def decode(arrays, meta):
    """The Features of encode's (arrays, meta): arrays of its own."""
    def own(name):
        a = arrays.get(name)
        return None if a is None else np.array(a)       # (a copy: writable, not the file's mapping)
    f = mapgeom.Features(meta["layer"], own("f.ids"), own("f.kind"), own("f.part_off"), own("f.ring_off"),
                         own("f.coord_off"), own("f.lonlat"), own("f.en"), own("f.own"), own("f.clipped"))
    for k in meta["attrs"]:
        if k in meta["objs"]:
            o = meta["objs"][k]
            col = np.full(int(o["n"]), None, object)
            for i, v in zip(o["at"], o["v"]):
                col[i] = _dec(v)
            f.attrs[k] = col
        else:
            f.attrs[k] = own("a." + k)
    for k in meta["rules"]:
        f.rules[k] = mapgeom.RuleTable(*(own(f"r.{k}.{col}") for col in ("feature", "value", "start", "end")))
    for k, v in meta["state"].items():
        f.__dict__[k] = _dec(v)
    return f


BLOB = "roads.zst"          # the arrays' bytes one after another, one zstd frame (level 1: a city cell's 2.6 MB of
LEVEL = 1                   # coordinates and fixed-width strings in ~0.4 MB; mapped files would be the layer again on disk)


def _ef():
    import fast
    fast.require(171, "zstd_pack")
    return fast._ef


def _blob(arrays):
    """(table [[name, dtype, shape, offset]], bytes) of arrays."""
    table, parts, off = [], [], 0
    for n, a in arrays.items():
        a = np.ascontiguousarray(a) if a.ndim else a
        table.append([n, np.lib.format.dtype_to_descr(a.dtype), list(a.shape), off])
        parts.append(a.tobytes())
        off += a.nbytes
    return table, b"".join(parts)


def _unblob(table, raw):
    out = {}
    for n, descr, shape, off in table:
        dt = np.lib.format.descr_to_dtype(descr if isinstance(descr, str) else [tuple(x) for x in descr])
        count = int(np.prod(shape)) if shape else 1
        if off + count * dt.itemsize > len(raw):
            raise ValueError(f"{n} past the end")
        out[n] = np.frombuffer(raw, dt, count, off).reshape(shape)
    return out


def load(d, want, stamp_of):
    """The Features kept in folder d under key want, or None. stamp_of(cell string) -> the stamp now of a neighbour
    cell's layer file (None: not there)."""
    try:
        with open(os.path.join(d, "ok"), encoding="utf-8") as fh:
            got, head = fh.readline().rstrip("\n"), fh.read()
        if got != want:
            return None
        head = json.loads(head)
        for cell, stamp in head["meta"]["ctx"]:
            if stamp_of(cell) != stamp:
                return None
        with open(os.path.join(d, BLOB), "rb") as fh:
            raw = _ef().zstd_unpack(fh.read(), int(head["size"]))
        if len(raw) != int(head["size"]):
            return None
        f = decode(_unblob(head["arrays"], raw), head["meta"])
        os.utime(os.path.join(d, "ok"))             # (read: the last to go, as the mirror's)
        return f
    except FileNotFoundError:
        return None
    except Exception as e:          # (damaged, or of another build's shape: made again)
        log.debug("road layer %s: not read: %s: %s", d, type(e).__name__, e)
        return None


def has(d, want):
    """Is a layer kept in folder d under key want (its marker's first line; nothing else read)?"""
    try:
        with open(os.path.join(d, "ok"), encoding="utf-8") as fh:
            return fh.readline().rstrip("\n") == want
    except OSError:
        return False


def store(d, want, f, ctx):
    """Features f written to folder d under key want (ctx: [(neighbour cell string, its file's stamp)]): the bytes
    written, or 0 when it is not kept."""
    try:
        arrays, meta = encode(f)
    except Unkept as e:
        log.debug("road layer %s: not kept: %s", d, e)
        return 0
    meta["ctx"] = [[c, s] for c, s in ctx]
    tmp = f"{d}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        table, raw = _blob(arrays)
        z = bytes(_ef().zstd_pack(raw, LEVEL))
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, BLOB), "wb") as fh:
            fh.write(z)
        with open(os.path.join(tmp, "ok"), "w", encoding="utf-8") as fh:
            fh.write(want + "\n" + json.dumps({"arrays": table, "size": len(raw), "meta": meta}))
        try:
            if os.path.isdir(d):            # (another key's, or another process's of the same)
                shutil.rmtree(d, ignore_errors=True)
            os.rename(tmp, d)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            return 0
        return len(z)
    except Exception as e:
        log.debug("road layer %s: not written: %s: %s", d, type(e).__name__, e)
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
