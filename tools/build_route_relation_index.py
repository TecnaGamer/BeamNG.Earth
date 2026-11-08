"""Build the offline nationwide way -> road-route relation join (no geometry).

Input: fixed Overpass `out body` JSON or .json.gz. Output: gzip-compressed,
indexed SQLite plus a provenance manifest. No network requests in this tool.
"""
import argparse
from collections import Counter
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile


def build(source, output):
    source,output=Path(source),Path(output)
    raw=gzip.decompress(source.read_bytes()) if source.suffix=='.gz' else source.read_bytes()
    data=json.loads(raw)
    if data.get('remark'):
        raise ValueError('Incomplete OSM query: '+data['remark'])
    networks=Counter()
    keys=('type','route','network','ref','modifier','direction','destination:ref')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        database=Path(tmp)/'routes.sqlite'
        with closing(sqlite3.connect(database)) as con, con:
            con.execute('PRAGMA page_size=4096')
            con.execute('CREATE TABLE routes(id INTEGER PRIMARY KEY,tags TEXT NOT NULL) WITHOUT ROWID')
            con.execute('CREATE TABLE members(way_id INTEGER NOT NULL,route_id INTEGER NOT NULL,role TEXT NOT NULL,'
                        'PRIMARY KEY(way_id,route_id,role)) WITHOUT ROWID')
            for rel in sorted(data['elements'],key=lambda r:r['id']):
                tags=rel.get('tags',{})
                if rel['type']!='relation' or tags.get('type')!='route' or tags.get('route')!='road':
                    continue
                if not str(tags.get('network','')).startswith('US:') or not tags.get('ref'):
                    continue
                slim={key:tags[key] for key in keys if key in tags}
                con.execute('INSERT INTO routes VALUES (?,?)',(rel['id'],json.dumps(slim,separators=(',',':'),sort_keys=True)))
                con.executemany('INSERT OR IGNORE INTO members VALUES (?,?,?)',
                    ((m['ref'],rel['id'],m.get('role','')) for m in rel.get('members',()) if m['type']=='way'))
                networks[tags['network']]+=1
            relations=con.execute('SELECT COUNT(*) FROM routes').fetchone()[0]
            members=con.execute('SELECT COUNT(*) FROM members').fetchone()[0]
            ways=con.execute('SELECT COUNT(DISTINCT way_id) FROM members').fetchone()[0]
        body=database.read_bytes()
        output.write_bytes(gzip.compress(body,compresslevel=9,mtime=0))
    manifest=dict(format=1,license='ODbL-1.0',attribution='© OpenStreetMap contributors',
        osm_timestamp=data.get('osm3s',{}).get('timestamp_osm_base'),
        source_sha256=hashlib.sha256(raw).hexdigest(),database_sha256=hashlib.sha256(body).hexdigest(),
        database_bytes=len(body),relations=relations,members=members,ways=ways,networks=dict(sorted(networks.items())))
    output.with_suffix('').with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source',type=Path)
    ap.add_argument('output',type=Path)
    args=ap.parse_args()
    result=build(args.source,args.output)
    print(json.dumps({k:v for k,v in result.items() if k!='networks'},indent=2))
