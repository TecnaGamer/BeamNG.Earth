r"""Read-only NVDB geometry probe for the Norway tunnel rebuild.

Place next to the existing `norway_tunnels_nvdb_v022` folder and run from the
BeamNG.Earth root: py .\norway_nvdb_wkt_probe.py
This script never alters game files, cache files or NVDB records.
"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
HELPER_DIR = ROOT / "norway_tunnels_nvdb_v022" / "server"
if not (HELPER_DIR / "norway_tunnel_nvdb.py").is_file():
    sys.exit("Missing norway_tunnels_nvdb_v022/server/norway_tunnel_nvdb.py; "
             "extract the v0.2.2 ZIP into the same folder first.")
sys.path.insert(0, str(HELPER_DIR))
import norway_tunnel_nvdb as nvdb  # noqa: E402


def geometry_preview(geometry):
    """Show a tiny public coordinate sample, never an entire WKT or response."""
    wkt = geometry.get("wkt")
    if not isinstance(wkt, str):
        return "no WKT text"
    # The preview is intentionally short and escaped to keep PowerShell output
    # safe to copy here even when the returned geometry has thousands of points.
    return repr(wkt[:125]) + (" ..." if len(wkt) > 125 else "")


def show(kind, lat, lon):
    print(f"\nNVDB {kind} at {lat:.7f}, {lon:.7f}")
    try:
        objects = nvdb.pages(kind, lat, lon, radius_m=1250)
    except nvdb.NVDBError as exc:
        print("  API error:", exc)
        return
    print("  objects:", len(objects))
    for obj in objects[:2]:
        print("  object id:", obj.get("id"))
        candidates = nvdb._geometry_candidates(obj)
        print("  geometry candidates:", len(candidates))
        for label, geometry in candidates[:5]:
            srid = geometry.get("srid")
            coords = nvdb._wkt_coords(geometry.get("wkt"), srid)
            print(f"    source={label} srid={srid!r} parsed_points={len(coords)}")
            print("      WKT start:", geometry_preview(geometry))
            if coords:
                print("      first parsed lat/lon:", coords[0])
                print("      distance to test site (m):",
                      round(nvdb._distance_m(lat, lon, *coords[0]), 1))


if __name__ == "__main__":
    print("NVDB geometry diagnostic — public road data only; no files modified")
    show(69, 61.5490040, 10.0062711)
    show(67, 61.5490040, 10.0062711)
    print("\nCopy this output back into ChatGPT. No map/cache files were changed.")
