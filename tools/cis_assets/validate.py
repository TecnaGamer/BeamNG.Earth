"""Strict file-level acceptance checks; invoke after each task: python .../validate.py A0."""
import sys,json,math,uuid,xml.etree.ElementTree as ET
sys.dont_write_bytecode=True
from collections import Counter,defaultdict
import numpy as np
from PIL import Image
from specs import *
NS={'c':'http://www.collada.org/2005/11/COLLADASchema'}
def need(ok,msg):
    if not ok:raise ValueError(msg)
def check_atlas(task):
    k=KITS[task];size=k.get('atlas',2048);name=P+k['kit'];d=json.loads((OUT/(name+'.cells.json')).read_text());cells=d['cells']
    need(d['atlas']==[size,size] and d['px_per_m']==96,'atlas size/scale')
    need(Counter(c['role'] for c in cells)==Counter(k['roles']),'required role/variant counts')
    expected={c['id']:c for c in cell_specs(task)};weights=defaultdict(float)
    for s in ('color','normal','data','lit'):
        im=Image.open(OUT/(name+'.'+s+'.png'));need(im.size==(size,size),name+' '+s+' dimensions');need(im.mode==('L' if s in ('data','lit') else 'RGB'),s+' channel format')
    for i,c in enumerate(cells):
        x,y,w,h=c['px'];need(x>=4 and y>=4 and x+w<=size-4 and y+h<=size-4,'4px margin '+c['id'])
        need(c['size_m']==expected[c['id']]['size_m'],'physical size '+c['id'])
        need(all(abs(a-b*96)<=1 for a,b in zip([w,h],c['size_m'])),'96 px/m '+c['id'])
        need(c['tags']==expected[c['id']]['tags'],'required set tags')
        weights[c['role']]+=c['weight']
        for q in cells[i+1:]:
            xx,yy,ww,hh=q['px'];need(x+w+4<=xx or xx+ww+4<=x or y+h+4<=yy or yy+hh+4<=y,'overlap/gutter '+c['id']+' '+q['id'])
    need(all(abs(v-1)<1e-6 for v in weights.values()),'weights must sum to 1 per role')
    # Verify actual two-pixel edge extrusion on each side (4px total intercell gutter).
    for s in ('color','normal','data','lit'):
        a=np.array(Image.open(OUT/(name+'.'+s+'.png')))
        for c in cells:
            x,y,w,h=c['px']
            need(np.array_equal(a[y:y+h,x-1],a[y:y+h,x]) and np.array_equal(a[y:y+h,x+w],a[y:y+h,x+w-1]),'horizontal bleed '+c['id'])
            need(np.array_equal(a[y-1,x:x+w],a[y,x:x+w]) and np.array_equal(a[y+h,x:x+w],a[y+h-1,x:x+w]),'vertical bleed '+c['id'])
    need(name in json.loads((OUT/'main.materials.json').read_text()),'missing atlas material')
    return f'{task}: {len(cells)} cells, fill {sum(c["px"][2]*c["px"][3] for c in cells)/size**2:.2%}'
def read_mesh(path):
    tree=ET.parse(path);root=tree.getroot();sources={}
    for s in root.findall('.//c:source',NS):
        a=s.find('c:float_array',NS);acc=s.find('c:technique_common/c:accessor',NS)
        sources[s.attrib['id']]=np.array(list(map(float,a.text.split()))).reshape(-1,int(acc.attrib['stride']))
    mesh=root.find('.//c:mesh',NS);pos=sources[mesh.find('c:vertices/c:input',NS).attrib['source'][1:]];faces=[]
    for t in mesh.findall('c:triangles',NS):
        ins=t.findall('c:input',NS);stride=max(int(i.attrib['offset']) for i in ins)+1
        data=np.array(list(map(int,t.find('c:p',NS).text.split()))).reshape(-1,3,stride)
        need(len(data)==int(t.attrib['count']),'triangle count XML')
        faces.extend([(t.attrib['material'],row,ins) for row in data])
    return root,pos,sources,faces

def check_module(m):
    root,pos,sources,faces=read_mesh(OUT/m['dae']);n=len(faces)
    need(root.attrib['version']=='1.4.1' and root.find('c:asset/c:up_axis',NS).text=='Z_UP','COLLADA version/up')
    need(float(root.find('c:asset/c:unit',NS).attrib['meter'])==1,'metres')
    task=m['task'];id=m['id']
    hard=260 if task=='M1' and '_top' in id else 220 if task=='M1' else 160 if task=='M2' else 400 if task=='M3' else (100 if id=='roof_exit' else 150) if task=='M4' else 140 if task=='M6' else {'roof_vent':40,'roof_antenna_old':120,'roof_dish':90,'wall_dish':90,'wall_ac':60,'wall_drain':30}[id]
    need(n==m['tris'] and n<=hard and m['budget']==hard,'triangle budget '+id)
    need(len(np.unique(np.round(pos,6),axis=0))==len(pos),'unwelded position vertices '+m['id'])
    need(np.isfinite(pos).all(),'nonfinite positions')
    nodes=root.findall('.//c:visual_scene/c:node',NS);need(len(nodes)==1 and nodes[0].find('c:matrix',NS) is None,'one identity scene node')
    need(not any('collision' in e.attrib.get('name','').lower() for e in root.iter()),'collision node')
    need(np.allclose(pos.min(0),m['bounds_min'],atol=1e-5) and np.allclose(pos.max(0),m['bounds_max'],atol=1e-5),'declared bounds')
    if m['mount']=='wall':need(pos[:,1].max()<=.020001,'behind wall '+m['id'])
    else:need(abs(pos[:,2].min())<1e-5,'roof origin '+m['id'])
    need(all(abs(a-b)<1e-4 for a,b in zip(pos.max(0)-pos.min(0),[m['width_m'],m['depth_m'],m['height_m']])),'declared dimensions '+m['id'])
    seen=set();mats=json.loads((OUT/'main.materials.json').read_text())
    common=json.loads((OUT/(P+'common.cells.json')).read_text())['cells'];rects=[c['px'] for c in common]
    for mat,row,ins in faces:
        need(mat in mats,'module material '+mat)
        ii={i.attrib['semantic']:i for i in ins};pidx=row[:,int(ii['VERTEX'].attrib['offset'])];tri=pos[pidx];norm=np.cross(tri[1]-tri[0],tri[2]-tri[0]);area=np.linalg.norm(norm)
        need(area>1e-9,'zero area '+m['id']);key=tuple(sorted(pidx));need(key not in seen,'duplicate face '+m['id']);seen.add(key)
        ni=ii['NORMAL'];normals=sources[ni.attrib['source'][1:]][row[:,int(ni.attrib['offset'])]]
        need(np.all(normals@(norm/area)>.999),'winding/normal '+m['id'])
        ui=ii['TEXCOORD'];uv=sources[ui.attrib['source'][1:]][row[:,int(ui.attrib['offset'])]]
        need(np.all((uv>=0)&(uv<=1)),'UV range '+m['id'])
        px=uv[:,0]*2048;py=(1-uv[:,1])*2048
        need(any(np.all((px>=x-.01)&(px<=x+w+.01)&(py>=y-.01)&(py<=y+h+.01)) for x,y,w,h in rects),'UV outside common cell '+m['id'])
    return f'{m["id"]}: {n}/{m["budget"]} tris'

def run(task):
    label=task;results=[]
    if task=='F1':task='M6'
    for path in OUT.glob('bne_cis_*.png'):
        mode='L' if path.name.endswith(('.data.png','.lit.png')) else 'RGB'
        need(Image.open(path).mode==mode,'8-bit single-channel/RGB check '+path.name)
    for t in TASKS[:TASKS.index(task)+1]:
        if t in KITS:results.append(check_atlas(t))
    mats=json.loads((OUT/'main.materials.json').read_text())
    for name,m in mats.items():
        need(m['class']=='Material' and m['version']==1.5 and m['mapTo']==name,'material format')
        need(m['persistentId']==str(uuid.uuid5(uuid.NAMESPACE_URL,'bne-cis/'+name)),'material UUID5')
        for key,suffix in [('baseColorMap','color'),('normalMap','normal'),('roughnessMap','data')]:
            ref=m['Stages'][0][key];need(ref.endswith('.'+suffix+'.dds'),'DDS reference')
            need((OUT/(Path(ref).name.removesuffix('.dds')+'.png')).exists(),'PNG source '+name)
    if label=='F1':
        baseline=json.loads((ROOT/'assets/cis_src/f1_baseline.json').read_text())
        for name,expected in baseline['cells'].items():need(json.loads((OUT/name).read_text())==expected,'F1 immutable cell layout '+name)
    mods=json.loads((OUT/'bne_cis_modules.json').read_text())['modules']
    for m in mods:results.append(check_module(m))
    if task.startswith('M'):
        from build_modules import expected_ids
        for t in [t for t in TASKS[6:TASKS.index(task)+1] if t.startswith('M')]:need({m['id'] for m in mods if m['task']==t}==set(expected_ids(t)),'required module variants '+t)
    for t in ('M1','M2'):
        for kit in ('khrush_panel','khrush_brick') if t=='M1' else ('panel9','panel16'):
            entries=[m for m in mods if m['task']==t and kit in m['fits'] and not m.get('top_only')]
            if entries:need(abs(sum(m['weight'] for m in entries)-1)<1e-6,'module weights '+kit)
    print('\n'.join(results));print('PASS '+label)
if __name__=='__main__':
    try:run(sys.argv[1])
    except Exception as e:print('FAIL:',e);sys.exit(1)
