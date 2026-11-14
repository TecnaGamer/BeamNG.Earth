"""Immutable, opt-in road-plan bundles. No network, pickle, or fallback generation.

The input digest is an operator declaration, not a captured raw-DEM snapshot. Bundles pin
road grading plans only; they do not establish complete-world or collision-mesh identity.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import zipfile

import numpy as np

MAX_MANIFEST_BYTES = 1 << 20
MAX_ARTIFACT_BYTES = 32 << 20
MAX_TOTAL_BYTES = 256 << 20
MAX_ARTIFACTS = 256
MAX_ROWS = 1_000_000
ENV = "BNGE_WORLD_BUNDLE"
HEX = re.compile(r"[0-9a-f]{64}\Z")
SIGNATURE = re.compile(r"w[01]s[01]\Z")
GROUPS = ("p", "z", "c", "b", "w", "k", "v", "o")   # plancache.store's array groups
DECK_GROUPS = ("g", "r")    # ... and the corrected decks' (grading.DECK_GROUND_*: lines (k, 2), stations (m, 6)), optional


class BundleError(ValueError):
    """Incompatible or invalid pinned data; callers must not substitute local geometry."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise BundleError("Invalid JSON value") from exc


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BundleError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(BundleError("Nonfinite JSON")))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise BundleError("Invalid JSON") from exc


def manifest_id(manifest):
    return hashlib.sha256(canonical({k: v for k, v in manifest.items() if k != "id"})).hexdigest()


def generator_identity():
    import fast
    import peerwork
    import revisit
    import roads
    fast.require(fast.MIN_VERSION, "world bundle")
    # Optional generator switches must not silently change the application of pinned plans.
    # Only their digest is retained; environment values never enter distributed metadata.
    settings = {key: value for key, value in os.environ.items()
                if key.startswith("BNE_") and key not in revisit.NOT_SWITCHES}
    # earthd writes the CLI default into its environment, whereas a separate exporter does not.
    # Hash the mode actually used by the road generator, including its default/case normalization.
    settings["BNE_BRIDGE_PROFILE"] = roads.bridge_profile_mode()
    env_id = hashlib.sha256(canonical(settings)).hexdigest()[:16]
    return {"code": peerwork.code_id() + "-" + env_id, "native": int(fast._ef.VERSION)}


def cached_source_build(path):
    """Read an already cached shard manifest without fetching or refreshing it. Unknown stays unknown."""
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BundleError("Cannot read cached source manifest") from exc
    if len(data) > MAX_MANIFEST_BYTES:
        raise BundleError("Cached source manifest exceeds size limit")
    manifest = _json(data)
    if not isinstance(manifest, dict) or not _text(manifest.get("built"), 256):
        raise BundleError("Cached source manifest has no valid build identity")
    return manifest["built"]


def completion_record(key, signature, sources, digest):
    """Generation-time provenance, stored beside exactly the NPZ bytes it describes."""
    return {"schema": 2, "sha256": digest, "key": list(_key(key)), "frames": "r10",
            "signature": signature, "generator": generator_identity(), "sources": sources}


def _read_completion(path, key, signature, digest, sources, generator):
    with open(path, "rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise BundleError("Plan provenance exceeds size limit")
    record = _json(data)
    expected = {"schema": 2, "sha256": digest, "key": list(key), "frames": "r10",
                "signature": signature, "generator": generator, "sources": sources}
    if canonical(record) != canonical(expected):
        raise BundleError("Plan provenance differs: regenerate with matching code, settings and source versions")


def _key(key):
    if not isinstance(key, (list, tuple)) or len(key) != 3 or any(type(v) is not int for v in key):
        raise BundleError("Invalid geographic cell key")
    z, x, y = key
    if not 0 <= z <= 22 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
        raise BundleError("Geographic cell key outside bounds")
    return tuple(key)


def _text(value, limit=1024):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def validate_manifest(manifest, expected_id=None):
    keys = {"schema", "kind", "frames", "generator", "signature", "sources", "input_digest",
            "attribution", "artifacts", "id"}
    if not isinstance(manifest, dict) or set(manifest) != keys:
        raise BundleError("Unexpected manifest structure")
    if len(canonical(manifest)) > MAX_MANIFEST_BYTES:
        raise BundleError("Manifest exceeds size limit")
    if type(manifest["schema"]) is not int or manifest["schema"] != 1 or manifest["kind"] != "road-plans" or manifest["frames"] != "r10":
        raise BundleError("Unsupported manifest format")
    gen = manifest["generator"]
    if (not isinstance(gen, dict) or set(gen) != {"code", "native"} or not _text(gen["code"], 100)
            or type(gen["native"]) is not int or gen["native"] < 1):
        raise BundleError("Invalid generator identity")
    if not isinstance(manifest["signature"], str) or not SIGNATURE.fullmatch(manifest["signature"]):
        raise BundleError("Invalid plan signature")
    if not isinstance(manifest["sources"], dict):
        raise BundleError("Source versions must be an object")
    if not isinstance(manifest["input_digest"], str) or not HEX.fullmatch(manifest["input_digest"]):
        raise BundleError("Input snapshot digest must be SHA256")
    attrs = manifest["attribution"]
    if not isinstance(attrs, list) or not 1 <= len(attrs) <= 64 or not all(_text(v, 2048) for v in attrs):
        raise BundleError("Attribution required")
    entries = manifest["artifacts"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ARTIFACTS:
        raise BundleError("Invalid artifact count")
    seen, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"key", "sha256", "size"}:
            raise BundleError("Invalid artifact record")
        key = _key(entry["key"])
        if key in seen:
            raise BundleError("Duplicate cell")
        seen.add(key)
        if not isinstance(entry["sha256"], str) or not HEX.fullmatch(entry["sha256"]):
            raise BundleError("Invalid artifact hash")
        if type(entry["size"]) is not int or not 0 < entry["size"] <= MAX_ARTIFACT_BYTES:
            raise BundleError("Invalid artifact size")
        total += entry["size"]
    if total > MAX_TOTAL_BYTES:
        raise BundleError("Bundle exceeds size limit")
    digest = manifest_id(manifest)
    if manifest["id"] != digest or expected_id is not None and digest != expected_id:
        raise BundleError("Manifest identity mismatch")
    return manifest


def read_manifest(path, expected_id=None):
    with open(path, "rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    return parse_manifest(data, expected_id)


def parse_manifest(data, expected_id=None):
    if not isinstance(data, bytes) or len(data) > MAX_MANIFEST_BYTES:
        raise BundleError("Manifest exceeds size limit")
    return validate_manifest(_json(data), expected_id)


def _number(value, lo, hi):
    # Range comparison first also rejects arbitrarily large JSON integers without converting them to float.
    return type(value) in (int, float) and lo <= value <= hi and math.isfinite(value)


def validate_artifact(data, artifact=None):
    """Validate lengths, NPY headers and every field before constructing grading/native inputs."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ARTIFACT_BYTES:
        raise BundleError("Artifact exceeds size limit")
    if artifact is not None and (len(data) != artifact["size"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]):
        raise BundleError("Artifact integrity mismatch")
    allowed = {"meta"} | set(GROUPS + DECK_GROUPS) | {g + "_n" for g in GROUPS + DECK_GROUPS}
    arrays = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 1 + len(GROUPS) <= len(entries) <= len(allowed) or sum(e.file_size for e in entries) > MAX_ARTIFACT_BYTES:
                raise BundleError("Invalid NPZ member count/size")
            for entry in entries:
                name = entry.filename.removesuffix(".npy")
                if name not in allowed or entry.filename != name + ".npy" or name in arrays:
                    raise BundleError("Unexpected or duplicate NPZ member")
                if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise BundleError("Unsupported NPZ compression")
                raw = archive.read(entry)
                stream = io.BytesIO(raw)
                version = np.lib.format.read_magic(stream)
                if version == (1, 0):
                    shape, order, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
                elif version == (2, 0):
                    shape, order, dtype = np.lib.format.read_array_header_2_0(stream, max_header_size=4096)
                else:
                    raise BundleError("Unsupported NPY version")
                if (not 1 <= len(shape) <= 2 or any(type(n) is not int or not 0 <= n <= MAX_ROWS for n in shape)
                        or dtype.hasobject or dtype.fields is not None or dtype.subdtype is not None):
                    raise BundleError("Invalid NPY shape/dtype")
                count = math.prod(shape)
                if count * dtype.itemsize != len(raw) - stream.tell():
                    raise BundleError("NPY payload size differs from header")
                if name == "meta":
                    valid = dtype == np.dtype("uint8") and len(shape) == 1 and shape[0] <= MAX_MANIFEST_BYTES
                elif name.endswith("_n"):
                    valid = dtype.kind in "iu" and dtype.itemsize <= 8 and len(shape) == 1
                else:
                    valid = dtype.kind == "f" and dtype.itemsize in (4, 8)
                if not valid:
                    raise BundleError("Unexpected array dtype")
                arrays[name] = np.lib.format.read_array(io.BytesIO(raw), allow_pickle=False, max_header_size=4096)
    except (OSError, ValueError, EOFError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise BundleError("Invalid plan archive") from exc
    if "meta" not in arrays or any(g + "_n" not in arrays for g in GROUPS):
        raise BundleError("Missing plan arrays")
    meta = _json(arrays["meta"].tobytes())
    if not isinstance(meta, dict) or set(meta) - {"decks"} != {"plans", "keeps", "covers", "opens"}:
        raise BundleError("Invalid plan metadata")
    has_decks = "decks" in meta
    if has_decks != all(g + "_n" in arrays for g in DECK_GROUPS) or (
            not has_decks and any(g in arrays or g + "_n" in arrays for g in DECK_GROUPS)):
        raise BundleError("Missing plan arrays")
    if any(not isinstance(v, list) or len(v) > MAX_ROWS for v in meta.values()):
        raise BundleError("Invalid plan metadata counts")
    groups = {}
    for group in GROUPS + (DECK_GROUPS if has_decks else ()):
        lengths = arrays[group + "_n"]
        if (lengths < 0).any() or any(int(v) > MAX_ROWS for v in lengths):
            raise BundleError("Invalid array lengths")
        total = sum(int(v) for v in lengths)
        a = arrays.get(group)
        if len(lengths) == 0:
            if a is not None and len(a):
                raise BundleError("Unreferenced array data")
            groups[group] = []
            continue
        dims = {"p": 2, "k": 2, "v": 2, "o": 2, "c": 6, "g": 2, "r": 6}
        if (a is None or total != len(a) or total > MAX_ROWS or
                (group in dims and (a.ndim != 2 or a.shape[1] != dims[group])) or
                (group in ("z", "b", "w") and a.ndim != 1)):
            raise BundleError("Array shape/count mismatch")
        if group == "c":
            # Cuttings use all-NaN triples to indicate that a side has no wall.
            triples = a.reshape(-1, 3)
            if not (np.isfinite(triples).all(axis=1) | np.isnan(triples).all(axis=1)).all():
                raise BundleError("Invalid cutting sentinel")
        elif not np.isfinite(a).all():
            raise BundleError("Nonfinite geometry")
        if np.any(np.abs(a[np.isfinite(a)]) > 1e8):
            raise BundleError("Geometry outside numeric bounds")
        groups[group] = list(np.split(a, np.cumsum(lengths, dtype=np.int64)[:-1]))
    plans_meta = meta["plans"]
    if len(plans_meta) != len(groups["p"]) or len(plans_meta) != len(groups["z"]):
        raise BundleError("Plan metadata cardinality mismatch")
    for group, name in (("k", "keeps"), ("v", "covers"), ("o", "opens")) + ((("g", "decks"),) if has_decks else ()):
        if len(groups[group]) != len(meta[name]) or any(len(a) < 2 for a in groups[group]):
            raise BundleError("Polyline metadata cardinality mismatch")
    from grading import Plan
    from roads import Spec, spec_from_json
    from sidewalks import EU_SLAB
    from grading import set_widen
    plans, ci, bi, wi = [], 0, 0, 0
    fields = {"fid", "half", "abut", "road_half", "rid", "rank", "max_wall", "cut", "bank", "steep", "widen"}
    for item, xy, heights in zip(plans_meta, groups["p"], groups["z"]):
        if (not isinstance(item, dict) or set(item) != fields or len(xy) < 2 or len(xy) != len(heights)
                or not _text(item["fid"])
                or type(item["rid"]) is not int or not -1 <= item["rid"] <= 2 ** 31 - 1
                or type(item["rank"]) is not int or not -1 <= item["rank"] <= 10000
                or not all(_number(item[k], 0, 10000) for k in ("half", "road_half", "max_wall"))
                or not isinstance(item["abut"], list) or len(item["abut"]) != 2
                or any(type(v) is not bool for v in item["abut"])
                or any(type(item[k]) is not bool for k in ("cut", "bank", "steep", "widen"))):
            raise BundleError("Invalid plan fields")
        cut = bank = None
        if item["cut"]:
            if ci >= len(groups["c"]) or len(groups["c"][ci]) != len(xy) - 1:
                raise BundleError("Cutting length mismatch")
            cut = groups["c"][ci]
            ci += 1
        if item["bank"]:
            if bi >= len(groups["b"]) or len(groups["b"][bi]) != len(xy):
                raise BundleError("Banking length mismatch")
            bank = groups["b"][bi]
            bi += 1
        widen = None
        if item["widen"]:
            if wi >= len(groups["w"]) or len(groups["w"][wi]) != len(xy):
                raise BundleError("Widening length mismatch")
            widen = groups["w"][wi]
            wi += 1
        p = Plan(xy, heights, item["half"], tuple(item["abut"]), cut, item["road_half"])
        p.rid, p.max_wall, p.bank, p.steep = item["rid"], item["max_wall"], bank, item["steep"]
        p.rank = item["rank"]
        set_widen(p, widen)
        plans.append((item["fid"], p))
    if ci != len(groups["c"]) or bi != len(groups["b"]) or wi != len(groups["w"]):
        raise BundleError("Unreferenced cutting/banking arrays")
    for name in ("keeps", "covers"):
        if not all(_number(v, 0, 10000) for v in meta[name]):
            raise BundleError("Invalid width")
    opens = []
    for xy, spec in zip(groups["o"], meta["opens"]):
        if not isinstance(spec, dict) or set(spec) - {"street"} != set(Spec._fields) - {"street"}:
            raise BundleError("Invalid road specification")
        for name, value in spec.items():
            if name in ("kind", "cls"):
                valid = _text(value, 128)
            elif name == "surf":
                valid = value is None or _text(value, 128)
            elif name in ("urban", "oneway", "link", "mountain", "france"):
                valid = type(value) is bool
            elif name == "street":
                valid = value is None or (
                    isinstance(value, list) and len(value) == 4 and _number(value[2], 0, 5)
                    and type(value[3]) is bool and all(
                        w is None or (isinstance(w, list) and len(w) == 4 and _number(w[0], .5, 7)
                                      and _number(w[1], 0, 20) and _number(w[2], 0, 1)
                                      and isinstance(w[3], str) and w[3] in EU_SLAB)
                        for w in value[:2]))
            elif name == "sides":
                valid = value is None or isinstance(value, list) and len(value) <= 2 and all(type(v) is int and v in (-1, 1) for v in value)
            elif name == "dir":
                valid = type(value) is int and value in (-1, 0, 1)
            else:
                valid = value is None and name in ("kmh", "gap", "median", "lanes") or _number(value, 0, 10000)
            if not valid:
                raise BundleError("Invalid road specification field")
        opens.append((xy, spec_from_json(spec)))
    decks = None
    if has_decks:
        if len(groups["r"]) != len(meta["decks"]) or any(len(r) < 1 for r in groups["r"]) \
                or not all(_number(v, 0, 10000) and v > 0 for v in meta["decks"]):
            raise BundleError("Invalid corrected decks")
        from grading import deck_key
        decks = [(deck_key(line, half), line, float(half), rows)
                 for line, rows, half in zip(groups["g"], groups["r"], meta["decks"])]
    return plans, list(zip(groups["k"], meta["keeps"])), list(zip(groups["v"], meta["covers"])), opens, decks


def artifact_bytes(root, entry):
    path = Path(root) / "blobs" / (entry["sha256"] + ".npz")
    with path.open("rb") as stream:
        data = stream.read(MAX_ARTIFACT_BYTES + 1)
    validate_artifact(data, entry)
    return data


def validate_bundle(root, expected_id=None):
    manifest = read_manifest(Path(root) / "manifest.json", expected_id)
    for entry in manifest["artifacts"]:
        artifact_bytes(root, entry)
    return manifest


def build_bundle(plan_dir, output_root, sources, input_digest, attribution):
    """Export complete run-cache plans. Caller chooses and attests input snapshot provenance.
    output_root must not already exist; publish its manifest only after every artifact validates."""
    plan_dir, output_root = Path(plan_dir), Path(output_root)
    if output_root.exists():
        raise BundleError("Bundle output already exists; immutable bundles cannot be overwritten")
    signature = plan_dir.name
    generator = generator_identity()
    entries, payloads = [], {}
    for path in sorted(plan_dir.glob("*.npz")):
        try:
            key = _key(tuple(int(v) for v in path.stem.split("_")))
            with path.open("rb") as stream:
                data = stream.read(MAX_ARTIFACT_BYTES + 1)
            digest = hashlib.sha256(data).hexdigest()
            _read_completion(path.with_suffix(".npz.complete"), key, signature, digest, sources, generator)
        except (OSError, ValueError) as exc:
            raise BundleError("Export requires matching generation-time provenance for every complete plan") from exc
        validate_artifact(data)
        entries.append({"key": list(key), "sha256": digest, "size": len(data)})
        payloads[digest] = data
        if len(entries) > MAX_ARTIFACTS or sum(e["size"] for e in entries) > MAX_TOTAL_BYTES:
            raise BundleError("Bundle exceeds configured limits")
    manifest = {"schema": 1, "kind": "road-plans", "frames": "r10", "generator": generator,
                "signature": signature, "sources": sources, "input_digest": input_digest,
                "attribution": attribution, "artifacts": entries}
    manifest["id"] = manifest_id(manifest)
    validate_manifest(manifest)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".worldbundle-", dir=output_root.parent) as temp:
        stage = Path(temp) / "bundle"
        (stage / "blobs").mkdir(parents=True)
        for digest, data in payloads.items():
            (stage / "blobs" / (digest + ".npz")).write_bytes(data)
        (stage / "manifest.json").write_bytes(canonical(manifest))
        os.rename(stage, output_root)
    return manifest


class PinnedPlanCache:
    """Read-only bundle snapshot for one process lifetime. No fallback on missing/corrupt data."""
    def __init__(self, root, signature, sources):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise BundleError("BNGE_WORLD_BUNDLE must be an absolute path")
        self.manifest = read_manifest(self.root / "manifest.json")
        if self.manifest["generator"] != generator_identity():
            raise BundleError("Pinned plan generator differs from this companion")
        if self.manifest["signature"] != signature or canonical(self.manifest["sources"]) != canonical(sources):
            raise BundleError("Pinned plan settings/source versions differ from this companion")
        self.entries = {_key(e["key"]): e for e in self.manifest["artifacts"]}
        self.signature = signature
        self.persist = None
        self.stats = {"read": 0, "written": 0, "waited": 0, "wait_s": 0.0}
        self._lock = threading.Lock()

    def load(self, key):
        entry = self.entries.get(_key(tuple(int(v) for v in key)))
        if entry is None:
            raise BundleError("Requested road cell is outside the pinned bundle")
        try:
            path = self.root / "blobs" / (entry["sha256"] + ".npz")
            with path.open("rb") as stream:
                result = validate_artifact(stream.read(MAX_ARTIFACT_BYTES + 1), entry)
        except OSError as exc:
            raise BundleError("Pinned road artifact is unavailable") from exc
        with self._lock:
            self.stats["read"] += 1
        return result

    def claim(self, key):
        return self.load(key), False

    def store(self, *args, **kwargs):
        raise BundleError("Pinned plan bundles are immutable")

    def release(self, key):
        pass
