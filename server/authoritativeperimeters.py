"""Exclusive, user-confirmed OSM relation outer linework. No polygon inference or geographic repair.

The data file is refreshed explicitly from live OSM; Overture remains unchanged.
Unavailable or disconnected authoritative geometry never falls back to land-use edges.
"""
from pathlib import Path
from collections import defaultdict
import hashlib,json,logging,threading,os
import numpy as np
import shapely
from shapely.geometry import LineString,Polygon,shape

LABEL='Boundary Francis E. Warren Air Force Base (border_zone)'
BASE_NAME='Francis E. Warren Air Force Base'
SOURCE_ID='relation/252043'
TARGETS=frozenset(('relation/252043','relation/21918541','way/841575049','explicit-scope:warren'))
DATA=Path(os.environ.get('BNE_WARREN_BOUNDARY_FILE',str(Path(__file__).with_name('warren_outer_boundary.json'))))
log=logging.getLogger('earthd.authoritativeperimeters')
_lock=threading.RLock()
_snapshot={}


def match_tags(tags):
    """The user's clarified relation identity, verified against the live OSM API."""
    expected=dict(type='boundary',boundary='border_zone',name=BASE_NAME)
    return list(expected.items()) if all(tags.get(k)==v for k,v in expected.items()) else []


def policy_for(members,metadata):
    if any(fid in TARGETS or metadata[fid].get('name')==LABEL or match_tags(metadata[fid].get('tags',{})) or
            metadata[fid].get('tags',{}).get('wikidata')=='Q1956149' for fid in members):
        return dict(key='warren',label=LABEL,source_id=SOURCE_ID,mode='exclusive_relation_outer_ways',fallback=False)
    return None


def signature():
    document()
    with _lock:return _snapshot['signature']


def document():
    try:
        stat=DATA.stat();token=(str(DATA),stat.st_mtime_ns,stat.st_size,stat.st_ino,stat.st_ctime_ns)
    except FileNotFoundError:token=(str(DATA),None,None)
    with _lock:
        if _snapshot.get('token')==token:return _snapshot['document']
        content=DATA.read_bytes() if token[1] is not None else b''
        doc=json.loads(content) if content else dict(elements=[],status='missing_authoritative_relation',reason='run tools/refresh_warren_boundary.py')
        _snapshot.update(token=token,document=doc,signature=hashlib.sha256(LABEL.encode()+b'\0'+content).hexdigest())
        return doc


def xml_document(content):
    import xml.etree.ElementTree as ET
    root=ET.fromstring(content);nodes={int(n.get('id')):(float(n.get('lon')),float(n.get('lat'))) for n in root.iter('node') if n.get('lon')}
    elements=[]
    for e in root.iter():
        if e.tag not in ('node','way','relation'):continue
        entry=dict(type=e.tag,id=int(e.get('id')),tags={t.get('k'):t.get('v') for t in e.findall('tag')})
        for key in ('version','changeset','timestamp','visible'):
            if e.get(key) is not None:entry[key]=e.get(key)
        if e.tag=='node' and e.get('lon'):entry.update(lon=float(e.get('lon')),lat=float(e.get('lat')))
        elif e.tag=='way':
            entry['nodes']=[int(n.get('ref')) for n in e.findall('nd')]
            if entry['nodes'] and all(n in nodes for n in entry['nodes']):entry['geometry']=[dict(lon=nodes[n][0],lat=nodes[n][1]) for n in entry['nodes']]
        elif e.tag=='relation':entry['members']=[dict(type=m.get('type'),ref=int(m.get('ref')),role=m.get('role','')) for m in e.findall('member')]
        elements.append(entry)
    return dict(elements=elements)


def extract(doc):
    elements={(e['type'],int(e['id'])):e for e in doc.get('elements',[])}
    found={};issues=[];roots=[]
    def walk(entry,root,visited):
        key=(entry['type'],int(entry['id']))
        if key in visited:issues.append(dict(reason='cyclic relation',id=str(key)));return
        if entry['type']=='way':
            geometry=entry.get('geometry',[])
            if len(geometry)<2:issues.append(dict(reason='missing way geometry',source='way/'+str(entry['id'])));return
            fid='way/'+str(entry['id']);record=found.setdefault(fid,dict(id=fid,coordinates=tuple((float(p['lon']),float(p['lat'])) for p in geometry),
                nodes=tuple(entry.get('nodes',())),tags=entry.get('tags',{}),version=entry.get('version'),timestamp=entry.get('timestamp'),changeset=entry.get('changeset'),roots=set()))
            record['roots'].add(root)
        elif entry['type']=='relation':
            for member in entry.get('members',[]):
                if member.get('role') in ('inner','label','admin_centre','subarea'):continue
                child=elements.get((member['type'],int(member['ref'])))
                if child is None and member['type']=='way' and member.get('geometry'):
                    child=dict(type='way',id=member['ref'],geometry=member['geometry'])
                if child is None:issues.append(dict(reason='missing relation member',source=member['type']+'/'+str(member['ref']),parent=root));continue
                walk(child,root,visited|{key})
        else:issues.append(dict(reason='matching point is not fence linework',source=root))
    for key,e in sorted(elements.items()):
        if e['type']=='relation' and int(e['id'])==252043 and match_tags(e.get('tags',{})):
            root=e['type']+'/'+str(e['id']);roots.append(dict(id=root,tags=e.get('tags',{}),matches=match_tags(e.get('tags',{})),version=e.get('version'),timestamp=e.get('timestamp'),changeset=e.get('changeset')))
            walk(e,root,set())
    for r in found.values():r['roots']=tuple(sorted(r['roots']))
    return list(found.values()),roots,issues


def sources(features=()):
    doc=document();records,roots,issues=extract(doc)
    # Never use an older Overture polygon as an implicit replacement for this
    # explicitly refreshed, versioned OSM relation.
    return records,dict(label=LABEL,source_id=SOURCE_ID,policy='exclusive_relation_outer_ways',fallback=False,source_status='available' if records else 'missing_authoritative_relation',
        roots=roots,features=[dict(id=r['id'],version=r.get('version'),timestamp=r.get('timestamp')) for r in records],issues=issues,provider=doc.get('provider'),checked_at=doc.get('checked_at'),osm_timestamp=doc.get('osm3s',{}).get('timestamp_osm_base') or (roots[0].get('timestamp') if roots else None),
        source_signature=signature(),diagnostic=doc.get('diagnostic'))


def openings():
    result=[]
    for e in document().get('elements',()):
        tags=e.get('tags',{})
        if e['type']=='node' and 'lon' in e and (tags.get('barrier') in ('gate','lift_gate','swing_gate','entrance') or tags.get('entrance')):
            fid='node/'+str(e['id'])
            result.append(dict(geometry=shapely.Point(e['lon'],e['lat']),properties=dict(id=fid,role='mapped_opening',facility_sources=[SOURCE_ID],
                source='https://www.openstreetmap.org/'+fid,reason='gate on current authoritative OSM linework',tags=tags)))
    return result


def bounds():
    doc=document()
    if doc.get('scope_bounds'):return tuple(doc['scope_bounds'])
    records,_,_=extract(doc)
    boxes=[tuple(doc['scope_bounds'])] if doc.get('scope_bounds') else []
    if records:boxes.append(shapely.union_all([LineString(r['coordinates']) for r in records]).bounds)
    if not boxes:return None
    return (min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))


def cache_token(tile_bounds):
    scope=bounds()
    if not scope:return None
    w,s,e,n=scope;tw,ts,te,tn=tile_bounds
    return signature() if tw<=e+.001 and te>=w-.001 and ts<=n+.001 and tn>=s-.001 else None


def frame_token(frame,box):
    x0,y0,x1,y1=box
    la,lo,_=frame.enu_to_geodetic(np.array([x0,x1,x0,x1]),np.array([y0,y0,y1,y1]),np.zeros(4))
    return cache_token((float(np.min(lo)),float(np.min(la)),float(np.max(lo)),float(np.max(la))))


def chains(records):
    """Exact source topology and segment ownership. No snapping, offsets or chords."""
    edges={};positions={};node_coords=defaultdict(set);adj=defaultdict(list);issues=[]
    for record in sorted(records,key=lambda r:(r['id'],r.get('part',0))):
        coords=record['coordinates'];node_ids=record.get('nodes',())
        def token(i):
            c=coords[i]
            if len(node_ids)==len(coords):
                node_coords[node_ids[i]].add(c)
                return ('node',node_ids[i],c)
            return ('coordinate',c)
        for i,(a,b) in enumerate(zip(coords,coords[1:])):
            if a==b:continue
            key=tuple(sorted((a,b)))
            if key in edges:edges[key]['sources'].add(record['id']);edges[key]['sources'].update(record['roots']);continue
            u,v=token(i),token(i+1);positions[u]=a;positions[v]=b
            edge=dict(a=u,b=v,sources={record['id'],*record['roots']});edges[key]=edge
            adj[u].append(key);adj[v].append(key)
    for nid,coords in node_coords.items():
        if len(coords)>1:issues.append(dict(reason='conflicting coordinates for shared OSM node; not joined',node=nid,coordinates=sorted(coords)))
    remaining=set(edges);paths=[]
    def walk(start,key):
        coords=[positions[start]];sources=set();at=start
        while key in remaining:
            remaining.remove(key);edge=edges[key];sources.update(edge['sources']);at=edge['b'] if edge['a']==at else edge['a'];coords.append(positions[at])
            available=[k for k in adj[at] if k in remaining]
            if len(adj[at])!=2 or not available:break
            key=min(available)
        return dict(coordinates=tuple(coords),source_ids=tuple(sorted(sources)))
    for node in sorted(adj,key=lambda n:(positions[n],repr(n))):
        if len(adj[node])!=2:
            for key in sorted(adj[node]):
                if key in remaining:paths.append(walk(node,key))
    while remaining:
        key=min(remaining);start=min((edges[key]['a'],edges[key]['b']),key=lambda n:(positions[n],repr(n)))
        paths.append(walk(start,key))
    endpoints=[dict(coordinates=positions[n],degree=len(adj[n]),node=str(n),reason='disconnected source endpoint' if len(adj[n])==1 else 'source junction') for n in adj if len(adj[n])!=2]
    for path in paths:path['id']='authoritative:warren/'+hashlib.sha1(repr(path).encode()).hexdigest()[:16]
    return paths,dict(paths=len(paths),source_segments=len(edges),endpoints=endpoints,issues=issues,repair='none; exact shared endpoints only')


def seed_area(frame,areas,metadata,infrastructure):
    if any(policy_for([fid],metadata) for fid,_,_ in areas):return areas
    if areas:return areas
    records,_=sources(infrastructure)
    if not records:return areas
    bounds=shapely.union_all([LineString(r['coordinates']) for r in records]).bounds
    if not shapely.box(*bounds).buffer(.02).covers(shapely.Point(frame.lon0,frame.lat0)):return areas
    # Context envelope only. It never supplies fence coordinates or inferred edges.
    envelope=shapely.box(*bounds).buffer(1e-7);geo=np.asarray(envelope.exterior.coords)
    e,n,_=frame.geodetic_to_enu(geo[:,1],geo[:,0],0.)
    fid='explicit-scope:warren';metadata[fid]=dict(name=BASE_NAME,tags={},polygon_id=fid,geometry_lonlat=envelope,category='military',interior_status='context envelope; not inferred security geometry')
    return list(areas)+[(fid,'military',[(geo,np.c_[e,n])])]
