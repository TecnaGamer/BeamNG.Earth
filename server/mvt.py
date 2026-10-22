"""A small Mapbox Vector Tile (MVT 2.1) decoder: the layers of a tile as features with their properties and geometry
in tile coordinates (0 .. extent, y down). No dependency (the protobuf wire format read by hand); bgt.py reads PDOK's
BGT tiles with it (59 KB a z17 tile where the GeoJSON of the same area was 21 MB).

    for layer in decode(data):              # [{"name", "extent", "features": [{"type", "props", "geom"}]}]
        ...                                  # type 1 point, 2 line, 3 polygon; geom: [rings / lines / points]
"""

POINT, LINE, POLYGON = 1, 2, 3


def _varint(b, i):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if c < 0x80:
            return v, i
        s += 7


def _fields(b):
    """(field number, wire type, value) of a message: varints as ints, length-delimited as bytes."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        elif w == 1:
            v, i = b[i:i + 8], i + 8
        elif w == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {w}")
        yield f, w, v


def _packed(b):
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i)
        out.append(v)
    return out


def _zz(v):
    return (v >> 1) ^ -(v & 1)


def _value(b):
    import struct
    for f, w, v in _fields(b):
        if f == 1:
            return v.decode("utf-8", "replace")
        if f == 2:
            return struct.unpack("<f", v)[0]
        if f == 3:
            return struct.unpack("<d", v)[0]
        if f in (4, 5):             # (an int64 written as unsigned: PDOK's BGT has its -1 levels so)
            return v - (1 << 64) if v >= 1 << 63 else v
        if f == 6:
            return _zz(v)
        if f == 7:
            return bool(v)
    return None


def _geometry(cmds, gtype):
    """Parts of a feature's geometry: polygons' rings, lines, or points, each [(x, y)] in tile units."""
    parts, cur = [], []
    x = y = 0
    i = 0
    while i < len(cmds):
        c = cmds[i]
        i += 1
        op, count = c & 7, c >> 3
        if op in (1, 2):
            if op == 1 and cur:
                parts.append(cur)
                cur = []
            for _ in range(count):
                x += _zz(cmds[i])
                y += _zz(cmds[i + 1])
                i += 2
                cur.append((x, y))
        elif op == 7:
            if cur:
                cur.append(cur[0])
                parts.append(cur)
                cur = []
    if cur:
        parts.append(cur)
    if gtype == POINT:
        return [p for part in parts for p in part]
    return parts


def decode(data):
    """The layers of an MVT tile (bytes, not gzipped)."""
    layers = []
    for f, _, v in _fields(data):
        if f != 3:
            continue
        name, extent, keys, values, raw = "", 4096, [], [], []
        for lf, _, lv in _fields(v):
            if lf == 1:
                name = lv.decode("utf-8", "replace")
            elif lf == 5:
                extent = lv
            elif lf == 3:
                keys.append(lv.decode("utf-8", "replace"))
            elif lf == 4:
                values.append(_value(lv))
            elif lf == 2:
                raw.append(lv)
        feats = []
        for fb in raw:
            tags, gtype, geom = [], 0, []
            for ff, _, fv in _fields(fb):
                if ff == 2:
                    tags = _packed(fv)
                elif ff == 3:
                    gtype = fv
                elif ff == 4:
                    geom = _packed(fv)
            props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)
                     if tags[k] < len(keys) and tags[k + 1] < len(values)}
            feats.append({"type": gtype, "props": props, "geom": _geometry(geom, gtype)})
        layers.append({"name": name, "extent": extent, "features": feats})
    return layers
