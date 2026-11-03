"""Native BeamNG Addon Registry, Compatibility Gate & Ground Truth Layer A.

Separates Layer A ("Ground Truth" — regional data sources, parcel crop mappings, structured crop row
specifications, Mediterranean/regional biome species, and building roof palettes) from Layer B
("Effort & Performance Tradeoffs" in server/effort.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any
import zipfile

log = logging.getLogger("earthd.addons")

SCHEMA_VERSION = 1
DEFAULT_ENGINE_VERSION = "1.7.0-beta"
DEFAULT_EARTHFAST_ABI = 142
DEFAULT_REVISIT_SCHEMA = 2
DEFAULT_PLANS_FORMAT = 1

VALID_POLICIES = ("strict", "warn", "dev")

# Pre-registered official/approved addon digests and publisher keys.
# The canonical digest of the reference 'provence' addon is populated at import time
# (and pre-listed here so strict mode verifies it out of the box).
APPROVED_ADDONS: dict[str, dict[str, Any]] = {
    "provence": {
        "publisher": "beamng-earth",
        "channel": "official",
        "public_key_id": "ed25519:beamng-earth-official-2026",
        "digests": {
            "4d82074a150105fcf02a55178d9fbe7050e33b2dfa853af384466f15d96e3a37",
            "5bb2eaef2125fd69b0b95ad10c10247fca5736c6f7898db94f373d8598667664",
        },
    }
}


class CompatResult(dict):
    """Compatibility check result supporting both dict access, attribute access, and truthiness."""

    def __bool__(self) -> bool:
        return bool(self.get("compatible", False))

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class ApprovalResult(dict):
    """Approval verification result supporting both dict access, attribute access, and truthiness."""

    def __bool__(self) -> bool:
        return bool(self.get("allowed", False))

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_SEMVER_RE = re.compile(
    r"^\s*v?(?P<major>\d+|x|\*)"
    r"(?:\.(?P<minor>\d+|x|\*)"
    r"(?:\.(?P<patch>\d+|x|\*))?)?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.\-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.\-]+))?\s*$"
)


def parse_semver(version_str: str) -> tuple[int, int, int]:
    """Parse 'major.minor.patch' from a version string (stripping '-beta', '-dev-experimental', etc.)."""
    if not isinstance(version_str, str) or not version_str.strip():
        raise ValueError(f"Invalid version string: {version_str!r}")
    m = _SEMVER_RE.match(version_str)
    if not m:
        raise ValueError(f"Invalid semantic version: {version_str!r}")
    maj_s, min_s, pat_s = m.group("major"), m.group("minor") or "0", m.group("patch") or "0"
    if maj_s in ("x", "*") or min_s in ("x", "*") or pat_s in ("x", "*"):
        raise ValueError(f"Concrete version cannot contain wildcard: {version_str!r}")
    return int(maj_s), int(min_s), int(pat_s)


def _parse_bound_version(bound_str: str, is_max: bool = False) -> tuple[int, int, int]:
    """Parse a min_version or max_version bound, supporting wildcards like '1.7.x' or '1.8.*'."""
    if not isinstance(bound_str, str) or not bound_str.strip():
        raise ValueError(f"Invalid version bound: {bound_str!r}")
    m = _SEMVER_RE.match(bound_str)
    if not m:
        raise ValueError(f"Invalid version bound syntax: {bound_str!r}")
    maj_s = m.group("major")
    min_s = m.group("minor") if m.group("minor") is not None else ("x" if is_max else "0")
    pat_s = m.group("patch") if m.group("patch") is not None else ("x" if is_max else "0")
    wildcard_val = 999_999 if is_max else 0
    major = wildcard_val if maj_s in ("x", "*") else int(maj_s)
    minor = wildcard_val if min_s in ("x", "*") else int(min_s)
    patch = wildcard_val if pat_s in ("x", "*") else int(pat_s)
    return major, minor, patch


def _current_engine_version() -> str:
    env_ver = os.environ.get("BNG_EARTH_VERSION")
    if env_ver:
        return env_ver
    try:
        import sys
        earthd_mod = sys.modules.get("earthd")
        if earthd_mod and hasattr(earthd_mod, "VERSION"):
            return str(earthd_mod.VERSION)
    except Exception:
        pass
    return DEFAULT_ENGINE_VERSION


def _current_earthfast_abi() -> int:
    try:
        import fast
        if getattr(fast, "_ef", None) is not None and hasattr(fast._ef, "VERSION"):
            return int(fast._ef.VERSION)
        return int(getattr(fast, "MIN_VERSION", DEFAULT_EARTHFAST_ABI))
    except Exception:
        return DEFAULT_EARTHFAST_ABI


def _current_revisit_schema() -> int:
    try:
        import revisit
        return int(getattr(revisit, "SCHEMA", DEFAULT_REVISIT_SCHEMA))
    except Exception:
        return DEFAULT_REVISIT_SCHEMA


def normalize_addon_payload(addon_payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize either a nested sync payload {'id', 'manifest', 'data_sources', 'biomes'} or a flat manifest dict."""
    if not isinstance(addon_payload, dict):
        raise ValueError("Addon payload must be a dict")

    if isinstance(addon_payload.get("manifest"), dict):
        manifest = dict(addon_payload["manifest"])
    else:
        manifest = {
            k: v
            for k, v in addon_payload.items()
            if k not in ("data_sources", "biomes", "vfs_root", "enabled", "user_toggles", "status", "approval_status")
        }

    raw_ds = addon_payload.get("data_sources")
    if raw_ds is None:
        raw_ds = manifest.get("data_sources", [])
    if isinstance(raw_ds, dict):
        data_sources = list(raw_ds.get("data_sources", []))
    elif isinstance(raw_ds, list):
        data_sources = list(raw_ds)
    else:
        data_sources = []

    raw_biomes = addon_payload.get("biomes")
    if raw_biomes is None:
        raw_biomes = manifest.get("biomes", [])
    if isinstance(raw_biomes, dict):
        biomes = list(raw_biomes.get("biomes", []))
    elif isinstance(raw_biomes, list):
        biomes = list(raw_biomes)
    else:
        biomes = []

    manifest_clean = {
        k: v
        for k, v in manifest.items()
        if k not in ("data_sources", "biomes", "vfs_root", "enabled", "user_toggles", "status", "approval_status")
    }
    addon_id = str(addon_payload.get("id") or manifest_clean.get("id") or "").strip()
    if addon_id and "id" not in manifest_clean:
        manifest_clean["id"] = addon_id

    return {
        "id": addon_id,
        "manifest": manifest_clean,
        "data_sources": data_sources,
        "biomes": biomes,
        "vfs_root": addon_payload.get("vfs_root") or f"/beamng_earth_addons/{addon_id}",
        "enabled": bool(addon_payload.get("enabled", True)),
        "user_toggles": dict(addon_payload.get("user_toggles") or {}),
    }


def validate_addon_manifest(addon_payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate addon manifest and structure against schema_version == 1."""
    errors: list[str] = []
    try:
        norm = normalize_addon_payload(addon_payload)
    except Exception as exc:
        return False, [str(exc)]

    m = norm["manifest"]
    schema_ver = m.get("schema_version")
    if schema_ver != SCHEMA_VERSION:
        errors.append(f"Unsupported schema_version: {schema_ver!r} (expected {SCHEMA_VERSION})")

    addon_id = m.get("id")
    if not isinstance(addon_id, str) or not re.match(r"^[a-z0-9_.\-]+$", addon_id):
        errors.append(f"Invalid or missing addon id: {addon_id!r}")

    name = m.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("Missing or empty addon 'name'")

    version = m.get("version")
    try:
        parse_semver(str(version or ""))
    except ValueError as exc:
        errors.append(f"Invalid addon version {version!r}: {exc}")

    priority = m.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        errors.append(f"Invalid priority {priority!r}: must be an integer")

    compat = m.get("engine_compat")
    if not isinstance(compat, dict):
        errors.append("Missing or invalid 'engine_compat' object")

    approval = m.get("approval", {})
    if approval is not None and not isinstance(approval, dict):
        errors.append("'approval' must be a dict if provided")

    capabilities = m.get("capabilities", [])
    if not isinstance(capabilities, list):
        errors.append("'capabilities' must be a list")

    for idx, ds in enumerate(norm["data_sources"]):
        if not isinstance(ds, dict) or not ds.get("id"):
            errors.append(f"data_sources[{idx}] is missing required 'id'")

    for idx, biome in enumerate(norm["biomes"]):
        if not isinstance(biome, dict) or not biome.get("id"):
            errors.append(f"biomes[{idx}] is missing required 'id'")
        bbox = biome.get("bbox") if isinstance(biome, dict) else None
        if bbox is not None:
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                errors.append(f"biomes[{idx}].bbox must be [lat_min, lon_min, lat_max, lon_max]")

    return len(errors) == 0, errors


def compute_addon_digest(addon_payload: dict[str, Any]) -> str:
    """Compute canonical SHA-256 hex digest of an addon's manifest, data_sources, and biomes."""
    norm = normalize_addon_payload(addon_payload)
    manifest_copy = dict(norm["manifest"])
    if isinstance(manifest_copy.get("approval"), dict):
        app_copy = {
            k: v
            for k, v in manifest_copy["approval"].items()
            if k not in ("digest", "sha256", "manifest_sha256")
        }
        manifest_copy["approval"] = app_copy

    canonical = {
        "manifest": manifest_copy,
        "data_sources": norm["data_sources"],
        "biomes": norm["biomes"],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def check_addon_compat(
    addon_payload: dict[str, Any],
    engine_version: str | None = None,
    earthfast_abi: int | None = None,
    revisit_schema: int | None = None,
    plans_format: int | None = None,
) -> CompatResult:
    """Strictly verify engine semver range, earthfast ABI range, revisit_schema, and plans_format."""
    eng_ver_str = engine_version if engine_version is not None else _current_engine_version()
    abi_val = int(earthfast_abi if earthfast_abi is not None else _current_earthfast_abi())
    rev_val = int(revisit_schema if revisit_schema is not None else _current_revisit_schema())
    plans_val = int(plans_format if plans_format is not None else DEFAULT_PLANS_FORMAT)

    reasons: list[str] = []
    valid, val_errors = validate_addon_manifest(addon_payload)
    if not valid:
        reasons.extend(val_errors)
        return CompatResult(
            compatible=False,
            ok=False,
            status="rejected_invalid_manifest",
            reasons=reasons,
            errors=reasons,
            engine_version=eng_ver_str,
            earthfast_abi=abi_val,
            revisit_schema=rev_val,
            plans_format=plans_val,
        )

    norm = normalize_addon_payload(addon_payload)
    compat = norm["manifest"].get("engine_compat") or {}

    try:
        cur_ver = parse_semver(eng_ver_str)
    except ValueError as exc:
        reasons.append(f"Invalid server engine_version {eng_ver_str!r}: {exc}")
        cur_ver = (0, 0, 0)

    min_ver_s = compat.get("min_version")
    if min_ver_s is not None:
        try:
            min_ver = _parse_bound_version(str(min_ver_s), is_max=False)
            if cur_ver < min_ver:
                reasons.append(
                    f"Engine version {eng_ver_str} is older than required min_version {min_ver_s}"
                )
        except ValueError as exc:
            reasons.append(str(exc))

    max_ver_s = compat.get("max_version")
    if max_ver_s is not None:
        try:
            max_ver = _parse_bound_version(str(max_ver_s), is_max=True)
            if cur_ver > max_ver:
                reasons.append(
                    f"Engine version {eng_ver_str} is newer than supported max_version {max_ver_s}"
                )
        except ValueError as exc:
            reasons.append(str(exc))

    abi_spec = compat.get("earthfast_abi")
    if isinstance(abi_spec, dict):
        abi_min = abi_spec.get("min")
        abi_max = abi_spec.get("max")
        if abi_min is not None and abi_val < int(abi_min):
            reasons.append(f"earthfast ABI {abi_val} is below addon min {abi_min}")
        if abi_max is not None and abi_val > int(abi_max):
            reasons.append(f"earthfast ABI {abi_val} exceeds addon max {abi_max}")
    elif isinstance(abi_spec, int):
        if abi_val != abi_spec:
            reasons.append(f"earthfast ABI {abi_val} does not match required {abi_spec}")

    req_revisit = compat.get("revisit_schema")
    if req_revisit is not None and int(req_revisit) != rev_val:
        reasons.append(f"revisit_schema {rev_val} does not match required {req_revisit}")

    req_plans = compat.get("plans_format")
    if req_plans is not None and int(req_plans) != plans_val:
        reasons.append(f"plans_format {plans_val} does not match required {req_plans}")

    is_ok = len(reasons) == 0
    return CompatResult(
        compatible=is_ok,
        ok=is_ok,
        status="compatible" if is_ok else "rejected_incompatible",
        reasons=reasons,
        errors=reasons,
        engine_version=eng_ver_str,
        earthfast_abi=abi_val,
        revisit_schema=rev_val,
        plans_format=plans_val,
    )


def get_default_approval_policy() -> str:
    pol = os.environ.get("BNG_EARTH_ADDON_POLICY", "warn").strip().lower()
    return pol if pol in VALID_POLICIES else "warn"


def verify_addon_approval(
    addon_payload: dict[str, Any],
    policy: str | None = None,
    approved_registry: dict[str, dict[str, Any]] | None = None,
) -> ApprovalResult:
    """Verify addon cryptographic digest / approval status under 'strict', 'warn', or 'dev' policy."""
    pol = (policy or get_default_approval_policy()).strip().lower()
    if pol not in VALID_POLICIES:
        raise ValueError(f"Unsupported approval policy {pol!r}; expected one of {VALID_POLICIES}")

    reg = approved_registry if approved_registry is not None else APPROVED_ADDONS
    norm = normalize_addon_payload(addon_payload)
    addon_id = norm["id"]
    digest = compute_addon_digest(norm)

    entry = reg.get(addon_id)
    approved_digests = set(entry.get("digests", ())) if isinstance(entry, dict) else set()
    is_approved = bool(entry is not None and digest in approved_digests)

    warnings: list[str] = []
    reason: str | None = None

    if is_approved:
        allowed = True
        status = "active"
        approval_status = "approved"
    elif pol == "strict":
        allowed = False
        status = "rejected_unapproved"
        approval_status = "rejected_unapproved"
        reason = f"Addon {addon_id!r} (digest {digest[:12]}) is not in APPROVED_ADDONS under strict policy"
    elif pol == "warn":
        allowed = True
        status = "active"
        approval_status = "unverified_warn"
        msg = f"Addon {addon_id!r} (digest {digest[:12]}) is unverified; activating with warning under 'warn' policy"
        warnings.append(msg)
        reason = msg
    else:  # pol == "dev"
        allowed = True
        status = "active"
        approval_status = "dev_override"
        reason = f"Addon {addon_id!r} activated via 'dev' policy override"

    return ApprovalResult(
        allowed=allowed,
        ok=allowed,
        approved=is_approved,
        status=status,
        approval_status=approval_status,
        policy=pol,
        digest=digest,
        warnings=warnings,
        reason=reason,
    )


def load_addon_from_dir(addon_dir: str | Path) -> dict[str, Any]:
    """Load an addon payload from a directory containing addon.json, data_sources.json, and biomes.json."""
    p = Path(addon_dir)
    if (p / "addon.json").is_file():
        root = p
    else:
        candidates = list(p.glob("beamng_earth_addons/*/addon.json"))
        if not candidates:
            raise FileNotFoundError(f"No addon.json found in {p}")
        root = candidates[0].parent

    manifest = json.loads((root / "addon.json").read_text(encoding="utf-8"))
    ds_file = manifest.get("data_sources_file", "data_sources.json")
    biomes_file = manifest.get("biomes_file", "biomes.json")

    ds_path = root / ds_file
    biomes_path = root / biomes_file

    data_sources = []
    if ds_path.is_file():
        ds_raw = json.loads(ds_path.read_text(encoding="utf-8"))
        data_sources = ds_raw.get("data_sources", ds_raw) if isinstance(ds_raw, dict) else ds_raw

    biomes = []
    if biomes_path.is_file():
        b_raw = json.loads(biomes_path.read_text(encoding="utf-8"))
        biomes = b_raw.get("biomes", b_raw) if isinstance(b_raw, dict) else b_raw

    return normalize_addon_payload(
        {
            "id": manifest.get("id", root.name),
            "manifest": manifest,
            "data_sources": data_sources,
            "biomes": biomes,
            "vfs_root": f"/beamng_earth_addons/{manifest.get('id', root.name)}",
        }
    )


def load_addons_from_zip(zip_path: str | Path) -> list[dict[str, Any]]:
    """Inspect a BeamNG mod .zip for beamng_earth_addons/*/addon.json and load all contained addons."""
    out: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        for name in sorted(names):
            norm_name = name.replace("\\", "/")
            if norm_name.startswith("beamng_earth_addons/") and norm_name.endswith("/addon.json"):
                prefix = norm_name[: -len("addon.json")]
                manifest = json.loads(zf.read(name).decode("utf-8"))
                ds_file = manifest.get("data_sources_file", "data_sources.json")
                biomes_file = manifest.get("biomes_file", "biomes.json")
                data_sources = []
                if (prefix + ds_file) in names:
                    ds_raw = json.loads(zf.read(prefix + ds_file).decode("utf-8"))
                    data_sources = ds_raw.get("data_sources", ds_raw) if isinstance(ds_raw, dict) else ds_raw
                biomes = []
                if (prefix + biomes_file) in names:
                    b_raw = json.loads(zf.read(prefix + biomes_file).decode("utf-8"))
                    biomes = b_raw.get("biomes", b_raw) if isinstance(b_raw, dict) else b_raw
                addon_id = manifest.get("id") or prefix.strip("/").split("/")[-1]
                out.append(
                    normalize_addon_payload(
                        {
                            "id": addon_id,
                            "manifest": manifest,
                            "data_sources": data_sources,
                            "biomes": biomes,
                            "vfs_root": "/" + prefix.strip("/"),
                        }
                    )
                )
    return out


def _bbox_contains(bbox: list[ float ] | tuple[float, ...] | None, lat: float | None, lon: float | None) -> bool:
    if bbox is None or lat is None or lon is None:
        return True
    if len(bbox) != 4:
        return True
    lat_min, lon_min, lat_max, lon_max = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    return lat_min <= float(lat) <= lat_max and lon_min <= float(lon) <= lon_max


@dataclass
class RegisteredAddon:
    id: str
    name: str
    version: str
    priority: int
    manifest: dict[str, Any]
    data_sources: list[dict[str, Any]]
    biomes: list[dict[str, Any]]
    vfs_root: str
    digest: str
    status: str
    approval_status: str
    compatible: bool
    enabled: bool = True
    source_toggles: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def is_active(self) -> bool:
        return self.compatible and self.status == "active" and self.enabled

    def to_status_dict(self) -> dict[str, Any]:
        sources_status = {}
        for ds in self.data_sources:
            sid = ds.get("id")
            if sid:
                default_en = bool(ds.get("default_enabled", True))
                sources_status[sid] = bool(self.source_toggles.get(sid, default_en))
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "priority": self.priority,
            "vfs_root": self.vfs_root,
            "digest": self.digest,
            "status": self.status if self.enabled else "disabled_by_user",
            "approval_status": self.approval_status,
            "compatible": self.compatible,
            "enabled": self.enabled,
            "active": self.is_active(),
            "sources": sources_status,
            "capabilities": list(self.manifest.get("capabilities", [])),
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
        }


class AddonRegistry:
    """Layer A — Ground Truth Provider & Active Addon Registry."""

    def __init__(
        self,
        approval_policy: str | None = None,
        engine_version: str | None = None,
        earthfast_abi: int | None = None,
        auto_discover_reference: bool = False,
    ):
        self._lock = threading.RLock()
        self.approval_policy = approval_policy or get_default_approval_policy()
        self.engine_version = engine_version
        self.earthfast_abi = earthfast_abi
        self._addons: dict[str, RegisteredAddon] = {}
        self._global_source_toggles: dict[str, bool] = {}
        if auto_discover_reference:
            self.load_bundled_addons()

    def load_bundled_addons(self) -> None:
        """Load bundled reference addons from repo/addons/*/beamng_earth_addons/*/addon.json if present."""
        import sys
        repo_root = Path(__file__).resolve().parents[1]
        addons_root = repo_root / "addons"
        if not addons_root.is_dir():
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                addons_root = Path(meipass) / "addons"
        if not addons_root.is_dir():
            return
        for addon_json in sorted(addons_root.glob("*/beamng_earth_addons/*/addon.json")):
            try:
                payload = load_addon_from_dir(addon_json.parent)
                self.register_addon(payload)
            except Exception as exc:
                log.warning("Failed to load bundled addon %s: %s", addon_json, exc)

    def clear(self) -> None:
        with self._lock:
            self._addons.clear()
            self._global_source_toggles.clear()

    def set_approval_policy(self, policy: str) -> None:
        pol = policy.strip().lower()
        if pol not in VALID_POLICIES:
            raise ValueError(f"Invalid approval policy {policy!r}; expected one of {VALID_POLICIES}")
        with self._lock:
            self.approval_policy = pol
            # Re-evaluate all registered addons under the new policy
            current_payloads = [
                {
                    "id": a.id,
                    "manifest": a.manifest,
                    "data_sources": a.data_sources,
                    "biomes": a.biomes,
                    "vfs_root": a.vfs_root,
                    "enabled": a.enabled,
                    "user_toggles": dict(a.source_toggles),
                }
                for a in self._addons.values()
            ]
            self._addons.clear()
            for p in current_payloads:
                self.register_addon(p)

    def register_addon(
        self,
        addon_payload: dict[str, Any],
        user_toggles: dict[str, Any] | None = None,
        policy: str | None = None,
    ) -> RegisteredAddon:
        """Validate, check compatibility & approval, and register an addon into the registry."""
        norm = normalize_addon_payload(addon_payload)
        addon_id = norm["id"]
        manifest = norm["manifest"]
        compat = check_addon_compat(
            norm,
            engine_version=self.engine_version,
            earthfast_abi=self.earthfast_abi,
        )
        pol = policy or self.approval_policy
        if compat.compatible:
            approval = verify_addon_approval(norm, policy=pol)
            status = approval.status
            approval_status = approval.approval_status
            digest = approval.digest
            warnings = list(approval.warnings)
            reasons = [approval.reason] if approval.reason and not approval.allowed else []
        else:
            digest = compute_addon_digest(norm) if manifest.get("id") else ""
            status = compat.status
            approval_status = "unverified"
            warnings = []
            reasons = list(compat.reasons)

        disabled_env = {x.strip() for x in os.environ.get("BNG_EARTH_DISABLED_ADDONS", "").split(",") if x.strip()}
        if addon_id in disabled_env:
            enabled = False
        elif "enabled" in addon_payload and isinstance(addon_payload["enabled"], bool):
            enabled = addon_payload["enabled"]
        else:
            enabled = True
        source_toggles: dict[str, bool] = {}
        for ds in norm["data_sources"]:
            sid = ds.get("id")
            if sid:
                source_toggles[sid] = bool(ds.get("default_enabled", True))

        # Apply per-addon user_toggles from payload
        for k, v in (norm.get("user_toggles") or {}).items():
            if isinstance(v, bool):
                source_toggles[k] = v

        # Apply external user_toggles if provided
        if isinstance(user_toggles, dict):
            addon_ut = user_toggles.get(addon_id, user_toggles)
            if isinstance(addon_ut, dict):
                if "enabled" in addon_ut and isinstance(addon_ut["enabled"], bool):
                    enabled = addon_ut["enabled"]
                sources_ut = addon_ut.get("sources", addon_ut)
                if isinstance(sources_ut, dict):
                    for sid, val in sources_ut.items():
                        if sid != "enabled" and isinstance(val, bool):
                            source_toggles[sid] = val

        # Apply any global source toggle overrides
        with self._lock:
            for sid, val in self._global_source_toggles.items():
                if sid in source_toggles:
                    source_toggles[sid] = val

            reg_addon = RegisteredAddon(
                id=addon_id,
                name=str(manifest.get("name") or addon_id),
                version=str(manifest.get("version") or "0.0.0"),
                priority=int(manifest.get("priority", 100)) if isinstance(manifest.get("priority", 100), int) else 100,
                manifest=manifest,
                data_sources=norm["data_sources"],
                biomes=norm["biomes"],
                vfs_root=norm["vfs_root"],
                digest=digest,
                status=status,
                approval_status=approval_status,
                compatible=compat.compatible,
                enabled=enabled,
                source_toggles=source_toggles,
                warnings=warnings,
                reasons=reasons,
            )
            self._addons[addon_id] = reg_addon
            return reg_addon

    def sync_addons(
        self,
        addons_list: list[dict[str, Any]],
        user_toggles: dict[str, Any] | None = None,
        policy: str | None = None,
    ) -> dict[str, Any]:
        """Synchronize discovered client addons & user toggles, returning active/rejected status & cache_fingerprint."""
        with self._lock:
            prev_fp = self.cache_fingerprint()
            if policy is not None:
                self.approval_policy = policy.strip().lower()
            self._addons.clear()
            for item in addons_list or []:
                self.register_addon(item, user_toggles=user_toggles, policy=self.approval_policy)
            new_fp = self.cache_fingerprint()
            status_payload = self.get_status()
            status_payload["cache_invalidated"] = prev_fp != new_fp
            status_payload["previous_fingerprint"] = prev_fp
            return status_payload

    def active_addons(self) -> list[RegisteredAddon]:
        """Return active addons ordered deterministically by (-priority, id) so higher priority wins."""
        with self._lock:
            active = [a for a in self._addons.values() if a.is_active()]
            active.sort(key=lambda a: (-a.priority, a.id))
            return active

    def all_addons(self) -> list[RegisteredAddon]:
        """Return all registered addons ordered by (-priority, id)."""
        with self._lock:
            items = list(self._addons.values())
            items.sort(key=lambda a: (-a.priority, a.id))
            return items

    def set_addon_enabled(self, addon_id: str, enabled: bool) -> bool:
        with self._lock:
            addon = self._addons.get(addon_id)
            if addon is None:
                return False
            addon.enabled = bool(enabled)
            return True

    def set_source_enabled(
        self,
        addon_or_source_id: str,
        source_id_or_enabled: str | bool,
        enabled: bool | None = None,
    ) -> bool:
        """Set a data source's enabled state.

        Supports both:
          - set_source_enabled(addon_id, source_id, enabled)
          - set_source_enabled(source_id, enabled)
        """
        with self._lock:
            if enabled is not None or isinstance(source_id_or_enabled, str):
                addon_id = addon_or_source_id
                source_id = str(source_id_or_enabled)
                flag = bool(enabled)
                addon = self._addons.get(addon_id)
                if addon is None:
                    return False
                addon.source_toggles[source_id] = flag
                return True
            else:
                source_id = addon_or_source_id
                flag = bool(source_id_or_enabled)
                self._global_source_toggles[source_id] = flag
                matched = False
                for addon in self._addons.values():
                    for ds in addon.data_sources:
                        if ds.get("id") == source_id:
                            addon.source_toggles[source_id] = flag
                            matched = True
                return matched

    def cache_fingerprint(self) -> str:
        """Compute deterministic 12-hex-char fingerprint over active addon IDs, versions, digests, and enabled sources."""
        with self._lock:
            active = sorted(
                (a for a in self._addons.values() if a.is_active()),
                key=lambda a: (a.priority, a.id),
            )
            entries = []
            for a in active:
                enabled_sources = sorted(
                    sid
                    for ds in a.data_sources
                    if (sid := ds.get("id")) and a.source_toggles.get(sid, bool(ds.get("default_enabled", True)))
                )
                entries.append(
                    {
                        "id": a.id,
                        "version": a.version,
                        "priority": a.priority,
                        "digest": a.digest,
                        "enabled_sources": enabled_sources,
                    }
                )
            raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(raw).hexdigest()[:12]

    def is_source_enabled(
        self,
        source_id: str,
        lat: float | None = None,
        lon: float | None = None,
    ) -> bool:
        """Check whether data source `source_id` is enabled in any active addon covering `(lat, lon)`.

        If no addon declares `source_id` (e.g., before any addon is synced), falls back to `_global_source_toggles`
        (defaulting to True for backward compatibility unless explicitly disabled).
        """
        with self._lock:
            if source_id in self._global_source_toggles and not self._global_source_toggles[source_id]:
                return False

            declared_anywhere = False
            for addon in self.active_addons():
                for ds in addon.data_sources:
                    if ds.get("id") == source_id:
                        declared_anywhere = True
                        if not addon.source_toggles.get(source_id, bool(ds.get("default_enabled", True))):
                            continue
                        if _bbox_contains(ds.get("bbox"), lat, lon):
                            return True

            if declared_anywhere:
                return False

            # Check if an addon that declares source_id was explicitly disabled by user or rejected
            for addon in self._addons.values():
                for ds in addon.data_sources:
                    if ds.get("id") == source_id:
                        return False

            return bool(self._global_source_toggles.get(source_id, True))

    def get_matching_biomes(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """Return all biomes from active addons whose bbox contains (lat, lon), ordered by (-priority, addon_id)."""
        matches: list[dict[str, Any]] = []
        for addon in self.active_addons():
            for biome in addon.biomes:
                if _bbox_contains(biome.get("bbox"), lat, lon):
                    b_copy = dict(biome)
                    b_copy.setdefault("addon_id", addon.id)
                    b_copy.setdefault("priority", addon.priority)
                    matches.append(b_copy)
        return matches

    def get_crop_row_spec(
        self,
        crop_name: str,
        lat: float | None = None,
        lon: float | None = None,
    ) -> dict[str, Any] | None:
        """Return structured crop row spec for `crop_name` from the highest-priority matching addon biome."""
        key = str(crop_name or "").strip().lower()
        if not key:
            return None
        if lat is not None and lon is not None:
            biomes = self.get_matching_biomes(lat, lon)
        else:
            biomes = []
            for addon in self.active_addons():
                for b in addon.biomes:
                    b_copy = dict(b)
                    b_copy.setdefault("addon_id", addon.id)
                    b_copy.setdefault("priority", addon.priority)
                    biomes.append(b_copy)
        for biome in biomes:
            crops = biome.get("structured_crops")
            if isinstance(crops, dict) and key in crops and isinstance(crops[key], dict):
                spec = dict(crops[key])
                spec.setdefault("addon_id", biome.get("addon_id"))
                spec.setdefault("biome_id", biome.get("id"))
                return spec
        return None

    def get_roof_style(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Return building roof palette/style constraints for (lat, lon) from the highest-priority matching biome."""
        for biome in self.get_matching_biomes(lat, lon):
            palettes = biome.get("building_palettes") or {}
            roof_style = palettes.get("roof_style") or biome.get("roof_style")
            if isinstance(roof_style, dict):
                out = dict(roof_style)
                out.setdefault("addon_id", biome.get("addon_id"))
                out.setdefault("biome_id", biome.get("id"))
                return out
        return None

    def get_status(self) -> dict[str, Any]:
        """Return full status dictionary for GET /api/addons and POST /api/addons/sync."""
        with self._lock:
            all_items = [a.to_status_dict() for a in self.all_addons()]
            active_items = [x for x in all_items if x["active"]]
            rejected_items = [x for x in all_items if not x["compatible"] or x["status"].startswith("rejected")]
            warnings: list[str] = []
            for x in all_items:
                warnings.extend(x.get("warnings", []))
            return {
                "schema_version": SCHEMA_VERSION,
                "approval_policy": self.approval_policy,
                "engine_version": self.engine_version or _current_engine_version(),
                "earthfast_abi": self.earthfast_abi or _current_earthfast_abi(),
                "cache_fingerprint": self.cache_fingerprint(),
                "addons": all_items,
                "active": [x["id"] for x in active_items],
                "rejected": [x["id"] for x in rejected_items],
                "warnings": warnings,
            }

    def export_runtime_state(self) -> dict[str, Any]:
        """Export full runtime state of registered addons and toggles for cross-process worker synchronization."""
        with self._lock:
            return {
                "approval_policy": self.approval_policy,
                "global_source_toggles": dict(self._global_source_toggles),
                "addons": [
                    {
                        "id": a.id,
                        "manifest": a.manifest,
                        "data_sources": a.data_sources,
                        "biomes": a.biomes,
                        "vfs_root": a.vfs_root,
                        "enabled": a.enabled,
                        "user_toggles": dict(a.source_toggles),
                    }
                    for a in self._addons.values()
                ],
            }

    def import_runtime_state(self, state: dict[str, Any]) -> None:
        """Import runtime state exported by the parent server process."""
        if not isinstance(state, dict):
            return
        with self._lock:
            pol = state.get("approval_policy")
            if isinstance(pol, str) and pol.strip().lower() in VALID_POLICIES:
                self.approval_policy = pol.strip().lower()
            self._global_source_toggles = {
                str(k): bool(v)
                for k, v in (state.get("global_source_toggles") or {}).items()
                if isinstance(v, bool)
            }
            self._addons.clear()
            for item in state.get("addons") or []:
                if isinstance(item, dict):
                    self.register_addon(item, policy=self.approval_policy)


def _register_reference_provence_digest() -> None:
    """Compute and pre-register the canonical digest of the bundled reference 'provence' addon."""
    try:
        repo_root = Path(__file__).resolve().parents[1]
        prov_dir = repo_root / "addons" / "beamng_earth_provence" / "beamng_earth_addons" / "provence"
        if prov_dir.is_dir():
            payload = load_addon_from_dir(prov_dir)
            digest = compute_addon_digest(payload)
            APPROVED_ADDONS.setdefault("provence", {"publisher": "beamng-earth", "channel": "official", "digests": set()})
            APPROVED_ADDONS["provence"]["digests"].add(digest)
    except Exception as exc:
        log.debug("Reference provence digest registration skipped: %s", exc)


_register_reference_provence_digest()

_GLOBAL_REGISTRY: AddonRegistry | None = None
_GLOBAL_LOCK = threading.Lock()


def get_registry() -> AddonRegistry:
    """Return the singleton server AddonRegistry (initializing with bundled reference addons on first access)."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        with _GLOBAL_LOCK:
            if _GLOBAL_REGISTRY is None:
                _GLOBAL_REGISTRY = AddonRegistry(auto_discover_reference=True)
    return _GLOBAL_REGISTRY


def reset_registry(auto_discover_reference: bool = True) -> AddonRegistry:
    """Reset the singleton AddonRegistry (useful for isolated unit tests)."""
    global _GLOBAL_REGISTRY
    with _GLOBAL_LOCK:
        _GLOBAL_REGISTRY = AddonRegistry(auto_discover_reference=auto_discover_reference)
        return _GLOBAL_REGISTRY
