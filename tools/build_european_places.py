"""Tile the developer's locality extraction into a small lazy offline ZIP.

One-degree cells own points using floor(lon), floor(lat), with no clipping of
geometry. Runtime reads only nearby cells. The extraction contains names and
settlement points, NOT surveyed board locations.
"""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import zipfile

def build(source,output):
    raw=Path(source).read_bytes();data=json.loads(gzip.decompress(raw));cells=defaultdict(list)
    for p in data['places']:cells[(math.floor(p['lon']),math.floor(p['lat']))].append(p)
    metadata={k:v for k,v in data.items() if k not in ('signs','places')}
    metadata.update(format=1,source_sha256=hashlib.sha256(raw).hexdigest(),cells=[])
    def put(z,name,value):
        info=zipfile.ZipInfo(name,(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
        z.writestr(info,json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),compresslevel=9)
    with zipfile.ZipFile(output,'w') as z:
        for (x,y),rows in sorted(cells.items()):
            name=f'{x}_{y}.json';put(z,name,sorted(rows,key=lambda r:r['id']));metadata['cells'].append([x,y])
        put(z,'index.json',metadata)
    print(len(data['places']),'places',len(cells),'cells','SHA256',hashlib.sha256(Path(output).read_bytes()).hexdigest())

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args();build(a.source,a.output)
