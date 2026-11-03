"""Layer B: Effort & Performance Tradeoff Controller (server/effort.py).

Strictly separates Layer B ("Effort & Performance Tradeoffs" — how hard the server and engine work
to synthesize, sample, and stream geometry/vegetation) from Layer A ("Ground Truth" in server/addons.py).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import threading
from typing import Any

CONCRETE_PROFILES: tuple[str, ...] = ("low", "balanced", "high", "ultra")
VALID_PROFILES: tuple[str, ...] = ("low", "balanced", "high", "ultra", "auto_fps")


@dataclass(frozen=True)
class EffortBudget:
    """Resolved Layer B effort & performance tradeoff budget."""

    profile: str
    effective_profile: str
    terrain_square_size: float
    crop_row_spacing_mult: float
    tree_spacing_mult: float
    aerial_blob_max_trees: int
    roof_ortho_sampling: bool
    building_col_rings: int
    far_geotiles: bool
    urgency_pause_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "low": {
        "terrain_square_size": 2.0,
        "crop_row_spacing_mult": 2.5,
        "tree_spacing_mult": 1.8,
        "aerial_blob_max_trees": 0,
        "roof_ortho_sampling": False,
        "building_col_rings": 1,
        "far_geotiles": False,
        "urgency_pause_s": 4.0,
    },
    "balanced": {
        "terrain_square_size": 1.0,
        "crop_row_spacing_mult": 1.4,
        "tree_spacing_mult": 1.2,
        "aerial_blob_max_trees": 120,
        "roof_ortho_sampling": True,
        "building_col_rings": 2,
        "far_geotiles": True,
        "urgency_pause_s": 3.0,
    },
    "high": {
        "terrain_square_size": 1.0,
        "crop_row_spacing_mult": 1.0,
        "tree_spacing_mult": 1.0,
        "aerial_blob_max_trees": 350,
        "roof_ortho_sampling": True,
        "building_col_rings": 2,
        "far_geotiles": True,
        "urgency_pause_s": 2.0,
    },
    "ultra": {
        "terrain_square_size": 1.0,
        "crop_row_spacing_mult": 0.85,
        "tree_spacing_mult": 0.85,
        "aerial_blob_max_trees": 600,
        "roof_ortho_sampling": True,
        "building_col_rings": 3,
        "far_geotiles": True,
        "urgency_pause_s": 1.5,
    },
}


def build_budget(profile: str, effective_profile: str | None = None) -> EffortBudget:
    """Construct an EffortBudget for `profile` and `effective_profile`."""
    prof = str(profile).strip().lower()
    if prof not in VALID_PROFILES:
        raise ValueError(f"Unsupported effort profile {profile!r}; expected one of {VALID_PROFILES}")
    eff = str(effective_profile or (prof if prof != "auto_fps" else "balanced")).strip().lower()
    if eff not in CONCRETE_PROFILES:
        raise ValueError(f"Unsupported effective profile {effective_profile!r}; expected one of {CONCRETE_PROFILES}")
    spec = _PROFILE_SPECS[eff]
    return EffortBudget(
        profile=prof,
        effective_profile=eff,
        terrain_square_size=float(spec["terrain_square_size"]),
        crop_row_spacing_mult=float(spec["crop_row_spacing_mult"]),
        tree_spacing_mult=float(spec["tree_spacing_mult"]),
        aerial_blob_max_trees=int(spec["aerial_blob_max_trees"]),
        roof_ortho_sampling=bool(spec["roof_ortho_sampling"]),
        building_col_rings=int(spec["building_col_rings"]),
        far_geotiles=bool(spec["far_geotiles"]),
        urgency_pause_s=float(spec["urgency_pause_s"]),
    )


class AutoFpsGovernor:
    """Adaptive effort tier governor with hysteresis (3 consecutive reports required to step tier)."""

    def __init__(
        self,
        initial_tier: str = "balanced",
        target_fps: float = 60.0,
        hysteresis_reports: int = 3,
    ):
        if initial_tier not in CONCRETE_PROFILES:
            raise ValueError(f"Invalid initial_tier {initial_tier!r}")
        self.current_tier: str = initial_tier
        self.target_fps: float = float(target_fps)
        self.hysteresis_reports: int = int(hysteresis_reports)
        self.down_streak: int = 0
        self.up_streak: int = 0
        self.last_telemetry: dict[str, float] = {}
        self.transitions: int = 0

    def reset(self, tier: str = "balanced", target_fps: float | None = None) -> None:
        if tier not in CONCRETE_PROFILES:
            raise ValueError(f"Invalid tier {tier!r}")
        self.current_tier = tier
        if target_fps is not None:
            self.target_fps = float(target_fps)
        self.down_streak = 0
        self.up_streak = 0
        self.last_telemetry = {}

    def update(
        self,
        fps_p50: float | None = None,
        fps_p95: float | None = None,
        frame_ms_p95: float | None = None,
        target_fps: float | None = None,
    ) -> str:
        """Process one client telemetry window and return the resolved concrete profile tier."""
        if target_fps is not None and float(target_fps) > 0:
            self.target_fps = float(target_fps)

        tgt = max(10.0, self.target_fps)
        target_ms = 1000.0 / tgt

        if fps_p50 is not None:
            self.last_telemetry["fps_p50"] = float(fps_p50)
        if fps_p95 is not None:
            self.last_telemetry["fps_p95"] = float(fps_p95)
        if frame_ms_p95 is not None:
            self.last_telemetry["frame_ms_p95"] = float(frame_ms_p95)
        self.last_telemetry["target_fps"] = tgt

        # Determine whether performance is struggling (under_target) or has ample headroom (above_target)
        under_target = False
        if fps_p50 is not None and float(fps_p50) < tgt * 0.85:
            under_target = True
        if fps_p95 is not None and float(fps_p95) < tgt * 0.70:
            under_target = True
        if frame_ms_p95 is not None and float(frame_ms_p95) > target_ms * 1.35:
            under_target = True

        above_target = False
        if not under_target:
            has_p50_headroom = fps_p50 is not None and float(fps_p50) >= tgt * 1.15
            has_ms_headroom = fps_p50 is None and frame_ms_p95 is not None and float(frame_ms_p95) <= target_ms * 0.80
            p95_ok = fps_p95 is None or float(fps_p95) >= tgt * 0.95
            ms95_ok = frame_ms_p95 is None or float(frame_ms_p95) <= target_ms * 1.05
            if (has_p50_headroom or has_ms_headroom) and p95_ok and ms95_ok:
                above_target = True

        idx = CONCRETE_PROFILES.index(self.current_tier)
        if under_target:
            self.down_streak += 1
            self.up_streak = 0
            if self.down_streak >= self.hysteresis_reports:
                self.down_streak = 0
                if idx > 0:
                    self.current_tier = CONCRETE_PROFILES[idx - 1]
                    self.transitions += 1
        elif above_target:
            self.up_streak += 1
            self.down_streak = 0
            if self.up_streak >= self.hysteresis_reports:
                self.up_streak = 0
                if idx < len(CONCRETE_PROFILES) - 1:
                    self.current_tier = CONCRETE_PROFILES[idx + 1]
                    self.transitions += 1
        else:
            self.down_streak = 0
            self.up_streak = 0

        return self.current_tier

    # Aliases for ergonomic calling from tests and HTTP endpoints
    report_telemetry = update
    observe = update

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_tier": self.current_tier,
            "target_fps": self.target_fps,
            "hysteresis_reports": self.hysteresis_reports,
            "down_streak": self.down_streak,
            "up_streak": self.up_streak,
            "transitions": self.transitions,
            "last_telemetry": dict(self.last_telemetry),
        }


class EffortController:
    """Thread-safe controller owning the active EffortBudget and AutoFpsGovernor."""

    def __init__(self, initial_profile: str | None = None, target_fps: float = 60.0):
        self._lock = threading.RLock()
        env_prof = os.environ.get("BNG_EARTH_EFFORT", "balanced").strip().lower()
        default_prof = initial_profile or (env_prof if env_prof in VALID_PROFILES else "balanced")
        if default_prof not in VALID_PROFILES:
            raise ValueError(f"Unsupported profile {default_prof!r}")
        self._profile: str = default_prof
        init_concrete = default_prof if default_prof in CONCRETE_PROFILES else "balanced"
        self.governor = AutoFpsGovernor(initial_tier=init_concrete, target_fps=target_fps)

    @property
    def profile(self) -> str:
        with self._lock:
            return self._profile

    @property
    def effective_profile(self) -> str:
        with self._lock:
            if self._profile == "auto_fps":
                return self.governor.current_tier
            return self._profile

    def set_profile(self, profile_name: str, initial_auto_tier: str | None = None) -> EffortBudget:
        prof = str(profile_name or "").strip().lower()
        if prof not in VALID_PROFILES:
            raise ValueError(f"Invalid effort profile {profile_name!r}; expected one of {VALID_PROFILES}")
        with self._lock:
            self._profile = prof
            if prof in CONCRETE_PROFILES:
                self.governor.reset(tier=prof)
            elif initial_auto_tier in CONCRETE_PROFILES:
                self.governor.reset(tier=str(initial_auto_tier))
            return self.get_budget()

    def report_telemetry(
        self,
        fps_p50: float | None = None,
        fps_p95: float | None = None,
        frame_ms_p95: float | None = None,
        target_fps: float | None = None,
    ) -> EffortBudget:
        with self._lock:
            self.governor.update(
                fps_p50=fps_p50,
                fps_p95=fps_p95,
                frame_ms_p95=frame_ms_p95,
                target_fps=target_fps,
            )
            return self.get_budget()

    def get_budget(self) -> EffortBudget:
        with self._lock:
            eff = self.governor.current_tier if self._profile == "auto_fps" else self._profile
            return build_budget(self._profile, eff)

    def cache_tag(self) -> str:
        """Deterministic cache tag encoding the effective effort budget parameters that affect tile synthesis."""
        b = self.get_budget()
        payload = {
            "effective_profile": b.effective_profile,
            "terrain_square_size": b.terrain_square_size,
            "crop_row_spacing_mult": b.crop_row_spacing_mult,
            "tree_spacing_mult": b.tree_spacing_mult,
            "aerial_blob_max_trees": b.aerial_blob_max_trees,
            "roof_ortho_sampling": b.roof_ortho_sampling,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return f"{b.effective_profile}_{digest[:8]}"

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            b = self.get_budget()
            return {
                "profile": b.profile,
                "effective_profile": b.effective_profile,
                "budget": b.to_dict(),
                "governor": self.governor.to_dict(),
                "cache_tag": self.cache_tag(),
            }

    def export_runtime_state(self) -> dict[str, Any]:
        """Export current effort profile and governor tier for cross-process worker synchronization."""
        with self._lock:
            return {
                "profile": self._profile,
                "effective_profile": self.effective_profile,
                "target_fps": self.governor.target_fps,
            }

    def import_runtime_state(self, state: dict[str, Any]) -> None:
        """Import runtime state exported by the parent server process."""
        if not isinstance(state, dict):
            return
        with self._lock:
            prof = str(state.get("profile") or "").strip().lower()
            eff = str(state.get("effective_profile") or "").strip().lower()
            if prof in VALID_PROFILES:
                self._profile = prof
            if eff in CONCRETE_PROFILES:
                self.governor.current_tier = eff


_GLOBAL_CONTROLLER: EffortController | None = None
_CONTROLLER_LOCK = threading.Lock()


def get_effort_controller() -> EffortController:
    """Return the singleton EffortController."""
    global _GLOBAL_CONTROLLER
    if _GLOBAL_CONTROLLER is None:
        with _CONTROLLER_LOCK:
            if _GLOBAL_CONTROLLER is None:
                _GLOBAL_CONTROLLER = EffortController()
    return _GLOBAL_CONTROLLER


def reset_effort_controller(initial_profile: str = "balanced", target_fps: float = 60.0) -> EffortController:
    """Reset the singleton EffortController (for isolated tests)."""
    global _GLOBAL_CONTROLLER
    with _CONTROLLER_LOCK:
        _GLOBAL_CONTROLLER = EffortController(initial_profile=initial_profile, target_fps=target_fps)
        return _GLOBAL_CONTROLLER


def get_budget() -> EffortBudget:
    """Return the active EffortBudget from the singleton EffortController."""
    return get_effort_controller().get_budget()


def set_profile(profile_name: str, initial_auto_tier: str | None = None) -> EffortBudget:
    """Set the active effort profile ('low' | 'balanced' | 'high' | 'ultra' | 'auto_fps') and return the new budget."""
    return get_effort_controller().set_profile(profile_name, initial_auto_tier=initial_auto_tier)


def report_telemetry(
    fps_p50: float | None = None,
    fps_p95: float | None = None,
    frame_ms_p95: float | None = None,
    target_fps: float | None = None,
) -> EffortBudget:
    """Feed client FPS / frame-time telemetry into AutoFpsGovernor and return the current EffortBudget."""
    return get_effort_controller().report_telemetry(
        fps_p50=fps_p50,
        fps_p95=fps_p95,
        frame_ms_p95=frame_ms_p95,
        target_fps=target_fps,
    )


def effort_cache_tag() -> str:
    """Return a deterministic short string tag representing the current EffortBudget for revisit/tile caching."""
    return get_effort_controller().cache_tag()
