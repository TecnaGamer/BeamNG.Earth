"""Show national service-sign build progress without touching the game/server."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    work = ROOT/'build/service-signs-us'
    path = work/'status.json'
    data = json.loads(path.read_text()) if path.exists() else dict(phase='not_started')
    temporary = work/'us-latest.osm.pbf.part'
    if temporary.exists(): data['downloaded_bytes'] = temporary.stat().st_size
    source = work/'us-latest.osm.pbf'
    if source.exists(): data['source_bytes'] = source.stat().st_size
    manifest = ROOT/'server/data/service_signs_us/manifest.json'
    if manifest.exists():
        coverage = json.loads(manifest.read_text())
        data.update(completed_cells=coverage['completed_cells'],total_cells=coverage['total_cells'],
                    coverage_complete=coverage['complete'],
                    directed_exits=sum(e['objects'] for e in coverage['cells'].values()))
    data['log'] = str(work/'build.log')
    data['download_log'] = str(work/'build-error.log')
    print(json.dumps(data,indent=2))


if __name__=='__main__': main()
