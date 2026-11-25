"""Cliff-only startup choices; the environment opt-in always wins over saved settings."""
from dataclasses import dataclass
import json
import os
from functools import lru_cache
from pathlib import Path
import hashlib

# Minimum native interface required by this feature, independent of upstream releases.
CLIFF_KERNEL = 616  # (the native module 616: lib.rs; 608-615 kept for other branches)
DEBUG = os.environ.get("BNE_CLIFFS_DEBUG", "off").strip().lower() in {"1", "on", "true"}

KEYS = ("naturalCliffs", "cliffRoadCuts", "cliffDetail", "cliffTextures")
# Near/canyon grid subdivisions, then projected-size multiplier for earlier LODs.
# High/Extreme extend detail range without increasing the maximum mesh density.
DETAILS = {"low": (4, 2, 2.0), "medium": (6, 3, 1.5), "normal": (8, 4, 1.0),
           "high": (8, 4, .75), "extreme": (8, 4, .5)}
PRESET_DETAIL = {"low": "low", "medium": "medium", "high": "normal", "ultra": "high", "extreme": "extreme"}


def geometry_identity(directory=None):
    """Digest cliff-only references without altering the shared source fingerprint."""
    root = Path(__file__).parent if directory is None else Path(directory)
    digest = hashlib.sha256()
    for name in ('cliff_height.png', 'cliff_cut_height.png', 'cliff_arid_cut_height.png',
                 'cliff_canyon_height.png', 'cliff_transition_means.json'):
        path = root / name
        if path.is_file():
            digest.update(name.encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _geometry_identity():
    return geometry_identity()


def choices(settings):
    """Normalize saved controls, including presets from before these controls existed."""
    detail = settings.get("cliffDetail", PRESET_DETAIL.get(str(settings.get("quality")), "normal"))
    return dict(naturalCliffs=settings.get("naturalCliffs") is not False,
                cliffRoadCuts=settings.get("cliffRoadCuts") is not False,
                cliffDetail=detail if isinstance(detail, str) and detail in DETAILS else "normal",
                cliffTextures="4k" if settings.get("cliffTextures") == "4k" else "bundled")


def available(env=None):
    env = os.environ if env is None else env
    return env.get("BNE_CLIFFS", "off").strip().lower() in {"1", "on", "true"}


@dataclass(frozen=True)
class CliffSettings:
    natural: bool = False
    road_cuts: bool = False
    detail: str = "normal"
    textures: str = "bundled"

    @classmethod
    def from_env(cls, env=None, settings=None):
        env = os.environ if env is None else env
        if not available(env):
            return cls()
        if settings is None:
            # Reuse the supervisor's startup snapshot so all workers see identical
            # choices even if the player edits settings while this server runs.
            try:
                settings = json.loads(env.get("BNGEARTH_START_SETTINGS", "{}"))["values"]
            except (ValueError, KeyError, TypeError):
                from experimental import read_settings
                settings = read_settings(env.get("BNGE_USER_DIR"))
        values = choices(settings if isinstance(settings, dict) else {})
        return cls(values["naturalCliffs"], values["cliffRoadCuts"], values["cliffDetail"], values["cliffTextures"])

    @property
    def enabled(self):
        return self.natural or self.road_cuts

    @property
    def cache_key(self):
        return ("dynamic-cliffs-v27", self.natural, self.road_cuts, self.detail, DEBUG, _geometry_identity() if self.enabled else None)

    @property
    def cache_parts(self):
        """No placeholder arguments in upstream keys when the feature is off."""
        return (self.cache_key,) if self.enabled else ()

    @property
    def cache_options(self):
        return {"cliffs": self.cache_key} if self.enabled else {}

    @property
    def profile(self):
        return DETAILS[self.detail]


def capture_startup():
    """Freeze direct server launches too, before any worker processes start."""
    global SETTINGS
    SETTINGS = CliffSettings.from_env()
    if available():
        try:
            snapshot = json.loads(os.environ.get("BNGEARTH_START_SETTINGS", "{}"))
        except ValueError:
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        values = snapshot.get("values")
        if not isinstance(values, dict):
            values = {}
        values.update(naturalCliffs=SETTINGS.natural, cliffRoadCuts=SETTINGS.road_cuts,
                      cliffDetail=SETTINGS.detail, cliffTextures=SETTINGS.textures)
        snapshot["values"] = values
        os.environ["BNGEARTH_START_SETTINGS"] = json.dumps(snapshot)


SETTINGS = CliffSettings.from_env()
