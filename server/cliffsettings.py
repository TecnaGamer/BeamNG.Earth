"""Independent, opt-in cliff categories, captured once when a worker starts."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class CliffSettings:
    natural: bool = False
    road_cuts: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        def enabled(key):
            return env.get(key, "off").strip().lower() in {"1", "on", "true"}
        return cls(enabled("BNE_CLIFFS"), enabled("BNE_CLIFF_ROAD_CUTS"))

    @property
    def enabled(self):
        return self.natural or self.road_cuts

    @property
    def cache_key(self):
        # Version the cliff payload independently of upstream's road cache.
        return ("dynamic-cliffs-v1", self.natural, self.road_cuts)


SETTINGS = CliffSettings.from_env()
