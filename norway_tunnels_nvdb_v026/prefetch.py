"""Fetch official NVDB tunnel data for a test site; no changes to game files."""
import argparse
import json
from pathlib import Path
import sys


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
CACHE_DIR = ROOT / "server" / "data" / "norway_tunnel_nvdb_v025"
sys.path.insert(0, str(PACKAGE / "server"))
import norway_tunnel_nvdb as nvdb


SITES = (("road portal", 61.549004, 10.0062711),
         ("Aursjøvegen", 62.4145045, 8.3222186))


def main():
    p = argparse.ArgumentParser(description="Read-only Norway NVDB 581/67/69 test")
    p.add_argument("--lat", type=float, help="Latitude of a custom Norwegian test point")
    p.add_argument("--lon", type=float, help="Longitude of a custom Norwegian test point")
    p.add_argument("--radius-m", type=int, default=1250, help="Search radius 100-2500m")
    p.add_argument("--force", action="store_true", help="Refresh an existing cached site")
    p.add_argument("--dry-run", action="store_true", help="Print exact request URLs, no network or writes")
    args = p.parse_args()
    if (args.lat is None) != (args.lon is None):
        p.error("Supply both --lat and --lon")
    if not (100 <= args.radius_m <= 2500):
        p.error("--radius-m must be 100..2500")
    points = (("custom", args.lat, args.lon),) if args.lat is not None else SITES
    failures = False
    print("NVDB V4 read-only prefetch | X-Client:", nvdb.X_CLIENT)
    print("Attribution: Contains NLOD data made available by Statens vegvesen.")
    for name, lat, lon in points:
        if not nvdb.country_bounds(lat, lon):
            print("SKIPPED outside Norway:", name, lat, lon)
            failures = True
            continue
        print(f"\n{name}: {lat:.7f}, {lon:.7f}, radius {args.radius_m}m")
        if args.dry_run:
            for kind in nvdb.TYPES:
                print(kind, nvdb.query_url(kind, lat, lon, args.radius_m))
            continue
        cache_file = nvdb._file(nvdb.cell_key(lat, lon), CACHE_DIR)
        if cache_file.is_file() and not args.force:
            report = json.loads(cache_file.read_text(encoding="utf-8"))
            if report.get("schema") != nvdb.SCHEMA or report.get("errors"):
                if report.get("errors"):
                    print("Retrying previously failed API pages...")
                report = nvdb.fetch_cell(lat, lon, args.radius_m)
                nvdb.save_cell(report, cache_dir=CACHE_DIR)
            else:
                print("Loaded previously cached results (use --force to refresh).")
        else:
            report = nvdb.fetch_cell(lat, lon, args.radius_m)
            nvdb.save_cell(report, cache_dir=CACHE_DIR)
        for type_id in nvdb.TYPES:
            t = str(type_id)
            if t in report["errors"]:
                print(f"{type_id} ERROR: {report['errors'][t]}")
                failures = True
            else:
                print(f"{type_id}: {report['counts'][t]} objects, "
                      f"{len(report['records'][t])} with usable WGS84 geometry")
                if type_id == 67:
                    use_3d = sum(len(record.get("line3d", [])) >= 3 for record in report['records'][t])
                    print(f"  {use_3d} with 3D NVDB tunnel centreline available for alignment")
                if report['counts'][t] and not report['records'][t]:
                    print("  Geometry structure:", json.dumps(
                        report.get('geometry_hints', {}).get(t, {}), ensure_ascii=False))
                for warning in report.get('detail_warnings', {}).get(t, []):
                    print("  Detail warning:", warning)
        print("Cache:", cache_file)
    print("\nNo map files were modified. Restart server to ensure an already-generated tile is rebuilt.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
