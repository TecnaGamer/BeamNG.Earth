"""Refresh only Warren's approved OSM relation snapshot; never restart or clear caches."""
from pathlib import Path
import argparse,datetime,json,os,sys,urllib.request
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import authoritativeperimeters as source
import shapely
from shapely.geometry import LineString

def refresh(output=source.DATA,fetch=None):
    url='https://api.openstreetmap.org/api/0.6/relation/252043/full'
    def download(url):
        request=urllib.request.Request(url,headers={'User-Agent':'BeamNG-Earth-authoritative-boundary/1.0'})
        return urllib.request.urlopen(request,timeout=30).read()
    fetch=fetch or download
    document=source.xml_document(fetch(url));seen={252043};pending=[e for e in document['elements'] if e['type']=='relation']
    while pending:
        relation=pending.pop()
        for member in relation.get('members',[]):
            if member['type']!='relation' or member.get('role')=='inner' or member['ref'] in seen:continue
            if len(seen)>64:raise ValueError('relation recursion exceeds bounded scope')
            seen.add(member['ref']);child=source.xml_document(fetch('https://api.openstreetmap.org/api/0.6/relation/'+str(member['ref'])+'/full'))
            document['elements'].extend(child['elements']);pending.extend(e for e in child['elements'] if e['type']=='relation' and e['id']==member['ref'])
    unique={}
    for element in document['elements']:
        key=(element['type'],element['id']);previous=unique.get(key)
        if previous is None or int(element.get('version',0))>=int(previous.get('version',0)):unique[key]=element
    document['elements']=[unique[key] for key in sorted(unique)]
    records,roots,issues=source.extract(document)
    if not records or not roots:raise ValueError('current relation does not match the confirmed type/name/border_zone selector; snapshot unchanged')
    if issues:raise ValueError('incomplete relation download: '+json.dumps(issues))
    geometry=shapely.union_all([LineString(r['coordinates']) for r in records]);scope=geometry.bounds
    output=Path(output)
    if output.exists():
        old=json.loads(output.read_text(encoding='utf-8')).get('scope_bounds')
        if old:scope=(min(old[0],scope[0]),min(old[1],scope[1]),max(old[2],scope[2]),max(old[3],scope[3]))
    document.update(provider=url,checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),scope_bounds=list(scope),
        diagnostic='User-confirmed relation 252043: type=boundary, boundary=border_zone, name=Francis E. Warren Air Force Base. Direct current OSM API; no Overture fallback.')
    output.parent.mkdir(parents=True,exist_ok=True);temporary=output.with_suffix(output.suffix+'.tmp')
    temporary.write_text(json.dumps(document,indent=2)+'\n',encoding='utf-8');os.replace(temporary,output)
    paths,topology=source.chains(records)
    return dict(source=source.SOURCE_ID,version=roots[0]['version'],changeset=roots[0].get('changeset'),ways=[r['id'] for r in records],topology=topology,output=str(output),restart_performed=False)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=source.DATA);args=parser.parse_args()
    print(json.dumps(refresh(args.output),indent=2))
    print('Only the approved snapshot was updated. Restart/reconnect when ready; no cache purge is required.')
