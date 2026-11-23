"""A second global Sentinel-2 layer for the tests ("s2other": the chain's fallback order between two layers of one host,
the host's shared breaker), never in the server's chain. Until 2026-11-23 the tests used EOX's 2023 mosaic for this;
it left the chain (CC BY-NC-SA 4.0). Importing this module registers it in imagery.SOURCES (this process only)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import imagery  # noqa: E402

ALT = "s2other"
imagery.SOURCES.setdefault(ALT, imagery.Source(
    ALT, "https://tiles.maps.eox.at/wmts/1.0.0/s2other_3857/default/g/{z}/{y}/{x}.jpg", 14,
    "Test stand-in Sentinel-2 layer (never in the server's chain)"))
