"""The interchange pipeline's step outputs: one record type per step, the contract between the steps (server/ix/s*.py),
the cache (ix/cache.py) and the checks (server/ixcheck/, which import this module and nothing else of the pipeline).

Every artifact has a Meta: its step, its step's FORMAT, its key (the cache's: ix/cache.key_of), its parents' keys and the
digest of the laws it was made with. Arrays are float64 / int64 numpy arrays; everything else is plain Python (str, int,
float, bool, tuples, lists, dicts with str keys) so an artifact goes to one .npz (the arrays) plus one JSON (the rest)
and back unchanged: save(artifact, path) / load(path).

Frames: every coordinate is in the planner's frame (ENU metres, planner.frame), as gradeshapes' members are. A member
is referred to by its index in Paths.members; a position on a member by (member index, s), s metres along its line.

Steps (pipeline_steps.md rev 2, pipeline_skeleton.md):
  1 Paths      the members (roads and tracks, after one_road) and how they meet (joins) and cross (crossings in plan)
  2 Extent     the systems (interchanges): their members, their boundary (where they meet the roads on the ground)
  3 Order      per system: each crossing's upper and lower, the integer levels of the members
  4 Terrain    per system: the ground the solve reads at each sample, and its trust
  5 Datum      per system: who moves at each crossing, the dip windows, the decks held, the options weighed
  6 Heights    per system: the pieces, their samples and heights; the region's roads' profiles (the grader's input)
(7 Build is read from the grader's plans by the checks; it has no artifact of its own in the skeleton.)
"""
import io
import json
import zipfile
from dataclasses import dataclass, field, fields, is_dataclass

import numpy as np

SCHEMA = 1          # this module's record layout; a change bumps it (and every step's cache key with it)

KINDS = ("ground", "deck", "tunnel", "rail", "rail_deck")
STRUCTURES = ("deck", "tunnel", "rail_deck")


@dataclass(frozen=True)
class Meta:
    step: int                       # 1..6
    format: int                     # the step module's FORMAT
    key: str                        # cache key (hex): ix/cache.key_of(step, format, laws, parents, inputs)
    parents: tuple = ()             # keys of the artifacts it was made from
    laws: str = ""                  # digest of the law constants it read (ix/cache.laws_digest)
    scope: str = ""                 # "region:<home cell>" (steps 1, 2) or "system:<system key>" (steps 3-6)


@dataclass(frozen=True)
class Finding:
    """A named failure or note of a step (or of a check): never solved round, reported."""
    step: int
    level: str                      # "fail" | "note"
    code: str                       # short slug, e.g. "hanging_end", "order_cycle", "no_heights"
    text: str                       # one line, roads named
    where: tuple = ()               # ((member, s), ...) or ((x, y), ...)


# ---- step 1 ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    """gradeshapes.Member as data: key (its str form), line (k, 2), kind (KINDS), cls, kmh, level (the map's), half
    (m), held (a deck never raised), nodes (sorted connector ids, or None). The road's signed height limit is not
    data (a callable): Crossing.need carries what it asks at each crossing."""
    key: str
    line: np.ndarray
    kind: str
    cls: str
    kmh: object                     # float or None
    level: float
    half: float
    held: bool = False
    nodes: object = None            # tuple of str, or None


@dataclass(frozen=True)
class Join:
    """Two members meeting end to end (or an end on the other's line): member a at sa, member b at sb (gradeshapes'
    "join" relation)."""
    a: int
    sa: float
    b: int
    sb: float


@dataclass(frozen=True)
class PlanCrossing:
    """Two members crossing in plan away from their ends (gradeshapes' "cross" relation), before any order: a at sa, b
    at sb, at point q (2,)."""
    a: int
    sa: float
    b: int
    sb: float
    q: np.ndarray


@dataclass(frozen=True)
class Paths:
    meta: Meta
    members: list                   # [Member]
    joins: list                     # [Join]
    crossings: list                 # [PlanCrossing]
    findings: list = field(default_factory=list)


# ---- step 2 ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Boundary:
    """Where a system meets a road on the ground: member i (the structure's) at si, ground member j at sj (gradeshapes'
    system "ends")."""
    i: int
    si: float
    j: int
    sj: float


@dataclass(frozen=True)
class System:
    key: str                        # gradeshapes.system_key: the same from every cell
    seeds: tuple                    # member indices of its structures
    joins: tuple                    # (Join, ...) inside it
    boundary: tuple                 # (Boundary, ...)
    box: tuple                      # (x0, y0, x1, y1) of its seeds' lines


@dataclass(frozen=True)
class Extent:
    meta: Meta
    home: str                       # the region's home cell
    systems: list                   # [System] the region solves (those reaching its home box), in solve order
    findings: list = field(default_factory=list)


# ---- step 3 ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Crossing:
    """A crossing of the system with its order: upper member u at su over lower member d at sd, at q (2,); need: m of
    clearance asked between the two road surfaces there (gradeshapes.clearance_need: headroom, signed limit, deck
    depth); under: the carriageway pairs it holds over (gradeshapes.under_pairs: (k, 2) s on u, s on d)."""
    u: int
    su: float
    d: int
    sd: float
    q: np.ndarray
    need: float
    under: np.ndarray


@dataclass(frozen=True)
class Order:
    meta: Meta
    system: str                     # System.key
    crossings: list                 # [Crossing]
    sign: dict                      # {member index (str): -1 tunnel, 0 ground, +1 deck / rail deck} (3a)
    level: dict                     # {member index (str): int} global level order of the system's members (3b)
    findings: list = field(default_factory=list)


# ---- step 4 ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Terrain:
    """The ground the solve reads: per piece of the system (step 6's pieces, in order), the ground at each sample (m,)
    and its trust (m,) in [0, 1] (1: trusted; the skeleton's adapter writes 1 where the ground is finite)."""
    meta: Meta
    system: str
    ground: list                    # [(m,) float64 per piece]
    trust: list                     # [(m,) float64 per piece]
    findings: list = field(default_factory=list)


# ---- step 5 ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Option:
    """One choice weighed for the system: per crossing (index into Order.crossings) "up" (the upper leaves the ground),
    "down" (the lower is dug), "none" (nothing moves); its cost (m of earth run, None not costed); feasible (None not
    checked)."""
    moves: tuple
    cost: object = None
    feasible: object = None
    why: str = ""


@dataclass(frozen=True)
class Datum:
    meta: Meta
    system: str
    moves: tuple                    # the chosen per crossing ("up" | "down" | "none"), as gradeshapes' c["moves"]
    dips: tuple                     # ((member, a, b), ...) the windows that go down (gradeshapes' sysm["dips"])
    held: tuple                     # member indices of decks held at their ground
    options: list                   # [Option] weighed (the skeleton's adapter: the one chosen)
    chosen: int = 0
    findings: list = field(default_factory=list)


# ---- step 6 ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    """An unknown line of the shape (gradeshapes.pieces / sample): kind "deck" or "dip", main member; its samples xy
    (m, 2), d (m,) along it, mem (m,) member index, ms (m,) arc on that member; z (m,) its heights (NaN: no answer)."""
    kind: str
    main: int
    xy: np.ndarray
    d: np.ndarray
    mem: np.ndarray
    ms: np.ndarray
    z: np.ndarray


@dataclass(frozen=True)
class Heights:
    meta: Meta
    system: str
    pieces: list                    # [Piece]
    solved: bool                    # False: no answer (infeasible), the system left as it is
    diag: dict                      # the solver's diagnostics (plain values)
    findings: list = field(default_factory=list)


@dataclass(frozen=True)
class Limit:
    """A step that went past its time limit (ix/limits.py) and was stopped: the step has no artifact of its own, this is its
    record (ixcheck reports its finding). system: the interchange's key, or what the growth that never reached one started
    from; step: its name; limit_s / took_s: what it was given and what it had taken."""
    meta: Meta
    system: str
    step: str
    limit_s: float
    took_s: float
    findings: list = field(default_factory=list)


@dataclass(frozen=True)
class Profile:
    """One road's profile the grader takes (grading._apply_shapes' input): member key, xy (m, 2), z (m,), mode "deck",
    "fill", "cut" or "set"."""
    fid: str
    xy: np.ndarray
    z: np.ndarray
    mode: str


@dataclass(frozen=True)
class Region:
    """The region's output (step 6's embankments over every solved system): what _shape_region returned, as data."""
    meta: Meta
    home: str
    profiles: list                  # [Profile] in the order the grader gets them
    findings: list = field(default_factory=list)


RECORDS = {c.__name__: c for c in (Meta, Finding, Member, Join, PlanCrossing, Paths, Boundary, System, Extent,
                                   Crossing, Order, Terrain, Option, Datum, Piece, Heights, Limit, Profile, Region)}


def _enc(x, arrays):
    """x as plain JSON data; every ndarray met is appended to arrays and referred to by its index."""
    if isinstance(x, np.ndarray):
        arrays.append(np.ascontiguousarray(x))
        return {"__array__": len(arrays) - 1}
    if isinstance(x, np.generic):
        return x.item()
    if is_record(x):
        out = {"__rec__": type(x).__name__}
        for f in fields(x):
            out[f.name] = _enc(getattr(x, f.name), arrays)
        return out
    if isinstance(x, tuple):
        return {"__tuple__": [_enc(v, arrays) for v in x]}
    if isinstance(x, list):
        return [_enc(v, arrays) for v in x]
    if isinstance(x, dict):
        return {str(k): _enc(v, arrays) for k, v in x.items()}
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    raise TypeError("an artifact holds plain Python and numpy arrays, not %s" % type(x).__name__)


def _dec(x, arrays):
    """_enc's inverse (records rebuilt as their dataclasses)."""
    if isinstance(x, dict):
        if "__rec__" in x:
            cls = RECORDS[x["__rec__"]]
            return cls(**{k: _dec(v, arrays) for k, v in x.items() if k != "__rec__"})
        if "__tuple__" in x:
            return tuple(_dec(v, arrays) for v in x["__tuple__"])
        if set(x) == {"__array__"}:
            return arrays[x["__array__"]]
        return {k: _dec(v, arrays) for k, v in x.items()}
    if isinstance(x, list):
        return [_dec(v, arrays) for v in x]
    return x


def _zip_write(zf, name, data):
    zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))     # (a fixed date: the same artifact, the same bytes)
    zi.compress_type = zf.compression
    zi.create_system = 3
    zf.writestr(zi, data)


def _npz_bytes(doc, arrays):
    """The .npz bytes: the JSON entry, then the arrays' in order, no timestamps."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        _zip_write(zf, "json", json.dumps(doc, separators=(",", ":")).encode("utf-8"))
        for n, a in enumerate(arrays):
            b = io.BytesIO()
            np.lib.format.write_array(b, a, allow_pickle=False)
            _zip_write(zf, "a%d" % n, b.getvalue())
    return buf.getvalue()


def save(artifact, path):
    """Write artifact (a record above) to path (.npz): its arrays as entries "a<n>", the rest as JSON in entry "json",
    each array replaced there by {"__array__": n}; dataclasses as {"__rec__": name, fields...}; tuples as
    {"__tuple__": [...]}. Deterministic: the same artifact gives the same bytes (fixed entry order, no timestamps)."""
    with open(path, "wb") as fh:
        fh.write(to_bytes(artifact))


def load(path):
    """The artifact save wrote to path, records rebuilt as their dataclasses (arrays float64 / int64 as saved)."""
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        doc = json.loads(zf.read("json").decode("utf-8"))
        arrays, n = [], 0
        while "a%d" % n in names:
            arrays.append(np.lib.format.read_array(io.BytesIO(zf.read("a%d" % n)), allow_pickle=False))
            n += 1
    return _dec(doc, arrays)


def to_bytes(artifact):
    """save's bytes without a file (ix/cache.key_of digests inputs this way)."""
    arrays = []
    return _npz_bytes(_enc(artifact, arrays), arrays)


def is_record(x):
    return is_dataclass(x) and type(x).__name__ in RECORDS


__all__ = [n for n in RECORDS] + ["SCHEMA", "KINDS", "STRUCTURES", "save", "load", "to_bytes", "is_record", "fields",
                                   "io", "json", "zipfile"]
