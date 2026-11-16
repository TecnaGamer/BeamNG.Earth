"""Developer-only extraction of French road details from a local Geofabrik PBF.

Requires pyosmium (pip install osmium), not a player/runtime dependency. No
network requests. A completed national input is required; partial reads fail
before either output is replaced. OSM contributors' ODbL attribution retained.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import countrybounds


def save(path,doc):
    raw=gzip.compress(json.dumps(doc,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode(),mtime=0)
    path=Path(path);temporary=path.with_suffix('.partial');temporary.write_bytes(raw);temporary.replace(path)
    print(path.name,len(raw),'bytes; SHA256',hashlib.sha256(raw).hexdigest(),flush=True)


def build(source,phones_output,directions_output=None):
    import osmium
    source=Path(source);idx=countrybounds.bundled()
    digest=hashlib.sha256()
    with source.open('rb')as f:
        while chunk:=f.read(8*1024*1024):digest.update(chunk)
    fp=osmium.FileProcessor(str(source),osmium.osm.NODE).with_filter(osmium.filter.TagFilter(('emergency','phone')))
    timestamp=fp.header.get('osmosis_replication_timestamp')
    metadata={'format':1,'source':'OpenStreetMap contributors via Geofabrik France extract',
              'source_url':'https://download.geofabrik.de/europe/france.html','license':'ODbL-1.0',
              'attribution':'© OpenStreetMap contributors — https://www.openstreetmap.org/copyright',
              'timestamp':timestamp,'source_sha256':digest.hexdigest(),
              'coverage':'Metropolitan France and Corsica, FR polygon filter; mapped features only'}
    phones=[]
    for node in fp:
        if not node.location.valid():continue
        lat,lon=node.lat,node.lon
        if idx.at(lat,lon)!='FR' or node.tags.get('access')in {'no','private'}:continue
        row={'id':node.id,'lat':lat,'lon':lon}
        for key in ('ref','operator'):
            if node.tags.get(key):row[key]=node.tags[key]
        phones.append(row)
    if not phones:raise ValueError('Empty emergency-phone extraction; existing output retained')
    save(phones_output,dict(metadata,selection='public emergency=phone nodes, placed only beside compatible ground highways',
                            phones=sorted(phones,key=lambda p:p['id'])))
    print(len(phones),'mapped phones',flush=True)
    if directions_output is None:return
    relations=[];node_ids=set();way_ids=set()
    for r in osmium.FileProcessor(str(source),osmium.osm.RELATION).with_filter(osmium.filter.TagFilter(('type','destination_sign'))):
        members=[{'type':{'n':'node','w':'way','r':'relation'}[m.type],'ref':m.ref,'role':m.role}for m in r.members]
        relations.append({'type':'relation','id':r.id,'tags':dict(r.tags),'members':members})
        node_ids.update(m['ref']for m in members if m['type']=='node')
        way_ids.update(m['ref']for m in members if m['type']=='way')
    print(len(relations),'destination relations',flush=True)
    ways=[]
    for w in osmium.FileProcessor(str(source),osmium.osm.WAY).with_filter(osmium.filter.IdFilter(way_ids)):
        ids=[n.ref for n in w.nodes];node_ids.update(ids)
        ways.append({'type':'way','id':w.id,'tags':dict(w.tags),'nodes':ids})
    nodes={}
    for n in osmium.FileProcessor(str(source),osmium.osm.NODE).with_filter(osmium.filter.IdFilter(node_ids)):
        if n.location.valid():nodes[n.id]={'type':'node','id':n.id,'lat':n.lat,'lon':n.lon,'tags':dict(n.tags)}
    for w in ways:w['geometry']=[{'lat':nodes[n]['lat'],'lon':nodes[n]['lon']}if n in nodes else None for n in w['nodes']]
    doc={'version':.6,'osm3s':{'timestamp_osm_base':timestamp},'elements':list(nodes.values())+ways+relations}
    # Reuse the reviewed relation-to-arrow compiler, with explicit colour support.
    import build_fr_direction_data
    output=Path(directions_output);temporary=output.with_name('.fr_road_relations.json.gz')
    save(temporary,doc)
    try:build_fr_direction_data.build(temporary,output,all_colours=True,metadata=metadata)
    finally:temporary.unlink(missing_ok=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path)
    p.add_argument('phones_output',type=Path);p.add_argument('--directions-output',type=Path)
    a=p.parse_args();build(a.source,a.phones_output,a.directions_output)
