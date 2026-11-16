"""Blender 4.5 preview: --background --python tools/cis_assets/preview.py -- A0."""
import sys,json,math,hashlib,xml.etree.ElementTree as ET
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
import bpy
from mathutils import Vector
from specs import *
CACHE={};CELLS={};MODS=[];ATLAS_SIZES={}
def material(name):
    if name in CACHE:return CACHE[name]
    m=bpy.data.materials.new(name);m.use_nodes=True;ns=m.node_tree.nodes;bs=ns.get('Principled BSDF');bs.inputs['Roughness'].default_value=.8
    for suffix,socket in [('color','Base Color'),('normal','Normal'),('data','Roughness')]:
        definition=json.loads((OUT/'main.materials.json').read_text())[name]['Stages'][0]
        key={'color':'baseColorMap','normal':'normalMap','data':'roughnessMap'}[suffix]
        path=OUT/(Path(definition[key]).name.removesuffix('.dds')+'.png')
        if not path.exists():continue
        tx=ns.new('ShaderNodeTexImage');tx.image=bpy.data.images.load(str(path),check_existing=True);tx.interpolation='Linear'
        if suffix!='color':tx.image.colorspace_settings.name='Non-Color'
        if suffix=='normal':
            nm=ns.new('ShaderNodeNormalMap');m.node_tree.links.new(tx.outputs['Color'],nm.inputs['Color']);m.node_tree.links.new(nm.outputs['Normal'],bs.inputs[socket])
        elif suffix=='color':
            mix=ns.new('ShaderNodeMixRGB');mix.blend_type='MULTIPLY';mix.inputs[0].default_value=1;mix.inputs[2].default_value=definition.get('baseColorFactor',[1,1,1,1]);m.node_tree.links.new(tx.outputs['Color'],mix.inputs[1]);m.node_tree.links.new(mix.outputs[0],bs.inputs[socket])
            if name=='bne_cis_lamp':bs.inputs['Emission Color'].default_value=(1,.85,.55,1);bs.inputs['Emission Strength'].default_value=2
        else:m.node_tree.links.new(tx.outputs['Color'],bs.inputs[socket])
    CACHE[name]=m;return m

def quad(name,points,cell=None):
    mesh=bpy.data.meshes.new('cell');mesh.from_pydata(points,[],[(0,1,2,3)]);mesh.update();o=bpy.data.objects.new('cell',mesh);bpy.context.collection.objects.link(o);mesh.materials.append(material(name))
    uv=mesh.uv_layers.new();x,y,w,h=cell['px'] if cell else (0,0,2048,2048)
    if name not in ATLAS_SIZES:
        meta=OUT/(name+'.cells.json');ATLAS_SIZES[name]=json.loads(meta.read_text())['atlas'] if meta.exists() else [2048,2048]
    aw,ah=ATLAS_SIZES[name]
    coords=[(x/aw,1-(y+h)/ah),((x+w)/aw,1-(y+h)/ah),((x+w)/aw,1-y/ah),(x/aw,1-y/ah)]
    for i in range(4):uv.data[i].uv=coords[i]
    return o

def select(kit,role,index,settag=None):
    if kit not in CELLS:CELLS[kit]=json.loads((OUT/(P+kit+'.cells.json')).read_text())['cells']
    options=[c for c in CELLS[kit] if c['role']==role and (not settag or settag in c['tags'])]
    if not options:options=[c for c in CELLS[kit] if c['role']==role]
    return options[int.from_bytes(hashlib.sha256(f'{kit}/{role}/{index}'.encode()).digest()[:4],'little')%len(options)]

def stamp(m,origin,tangent=(1,0,0),outward=(0,-1,0),scale=1):
    ns={'c':'http://www.collada.org/2005/11/COLLADASchema'};r=ET.parse(OUT/m['dae']).getroot();sources={}
    for s in r.findall('.//c:source',ns):
        vals=list(map(float,s.find('c:float_array',ns).text.split()));stride=int(s.find('c:technique_common/c:accessor',ns).attrib['stride']);sources[s.attrib['id']]=[vals[i:i+stride] for i in range(0,len(vals),stride)]
    me=r.find('.//c:mesh',ns);pos=sources[me.find('c:vertices/c:input',ns).attrib['source'][1:]]
    points=[(origin[0]+tangent[0]*p[0]*scale-outward[0]*p[1],origin[1]+tangent[1]*p[0]*scale-outward[1]*p[1],origin[2]+p[2]) for p in pos];faces=[];uvs=[];mids=[];materials=[]
    for t in me.findall('c:triangles',ns):
        name=t.attrib['material']
        if name not in materials:materials.append(name)
        inputs=t.findall('c:input',ns);stride=max(int(x.attrib['offset']) for x in inputs)+1;v=list(map(int,t.find('c:p',ns).text.split()));pi=next(int(x.attrib['offset']) for x in inputs if x.attrib['semantic']=='VERTEX');ui=next(x for x in inputs if x.attrib['semantic']=='TEXCOORD');uvo=int(ui.attrib['offset']);uvsrc=sources[ui.attrib['source'][1:]]
        for i in range(0,len(v),stride*3):
            faces.append(tuple(v[i+j*stride+pi] for j in range(3)));uvs.extend([uvsrc[v[i+j*stride+uvo]] for j in range(3)]);mids.append(materials.index(name))
    mesh=bpy.data.meshes.new(m['id']);mesh.from_pydata(points,[],faces);mesh.update();o=bpy.data.objects.new(m['id'],mesh);bpy.context.collection.objects.link(o)
    for n in materials:mesh.materials.append(material(n))
    for p,mi in zip(mesh.polygons,mids):p.material_index=mi
    uv=mesh.uv_layers.new()
    for i,p in enumerate(uvs):uv.data[i].uv=p

def building(task,far=False,tower=False):
    k=KITS[task];kit=k['kit'];modern=task=='A5' or k.get('modern',False);width,depth,floors=k['size'];plinth=0 if modern else 1.;gh=4.5 if task=='A5' else k['storey'];height=plinth+gh+(floors-1)*k['storey']+k.get('tech',0)+k.get('attic',0);par=k.get('cornice',k.get('parapet',1.2 if task=='A5' else .5 if task=='A1' else .6))
    entrances=k['entrances']
    if tower:
        width,depth,floors=k.get('secondary',[24,24,16]);entrances=k.get('secondary_entrances',[width/2])
        height=plinth+gh+(floors-1)*k['storey']+k.get('tech',0)+k.get('attic',0)
    # L footprint: two perpendicular 18m-deep wings, 40m and 30m outside spans.
    poly=[(-width/2,-depth/2),(width/2,-depth/2),(width/2,-depth/2+18),(-width/2+18,-depth/2+18),(-width/2+18,depth/2),(-width/2,depth/2)] if task=='A5' else [(-width/2,-depth/2),(width/2,-depth/2),(width/2,depth/2),(-width/2,depth/2)]
    for edge,(a,b) in enumerate(zip(poly,poly[1:]+poly[:1])):
        length=math.dist(a,b);tx,ty=(b[0]-a[0])/length,(b[1]-a[1])/length;out=(ty,-tx,0);tangent=(tx,ty,0);n=max(1,round(length/k['bay']));bw=length/n;stairs={min(n-1,int(e/length*n)) for e in (entrances if edge==0 else [])}
        def wall_cell(j,z,h,c,name=None):
            x,y=a[0]+j*bw*tx,a[1]+j*bw*ty
            quad(name or P+kit,[(x,y,z),(x+bw*tx,y+bw*ty,z),(x+bw*tx,y+bw*ty,z+h),(x,y,z+h)],c)
        for j in range(n):
            if plinth:wall_cell(j,0,plinth,select('common','plinth',0),P+'common')
            for f in range(floors):
                role='entrance' if j in stairs and f==0 else 'stair' if j in stairs else 'ground' if f==0 else ('bay_balcony_far' if far else 'bay_balcony') if edge==2 and (task in ('A3','A4','A5','A7','A8','A9') or j%2==0) else 'bay'
                if edge in (1,3) and not modern and j%3==0:role='end'
                z=plinth+(0 if f==0 else gh+(f-1)*k['storey']);hh=gh if f==0 else k['storey'];index=f*97+j*13+edge*17;tag=k['sets'][0]
                c=select(kit,role,j//2+(f//3)*17 if task=='A9' else 0 if modern else j*13+edge*17 if task=='A4' else index,tag);wall_cell(j,z,hh,c)
                if not far and role in ('bay_balcony','entrance'):
                    opts=[m for m in MODS if kit in m['fits'] and m.get('needs_role')==role and not m.get('top_only')]
                    if modern:opts=[m for m in opts if tag in m.get('sets',[]) and m['kind']=='loggia']
                    if opts:
                        pick=int.from_bytes(hashlib.sha256(f'module/{kit}/{index}'.encode()).digest()[:4],'little')/2**32
                        total=sum(q['weight'] for q in opts);cum=0;m=opts[-1]
                        for q in opts:
                            cum+=q['weight']/total
                            if pick<cum:m=q;break
                        if f==floors-1:
                            topid=m['id'].removesuffix('_brick')+'_top'+('_brick' if m['id'].endswith('_brick') else '')
                            top=next((q for q in MODS if q['id']==topid),None)
                            if top:m=top
                        origin=(a[0]+(j+.5)*bw*tx,a[1]+(j+.5)*bw*ty,z);stamp(m,origin,tangent,out,1.)
            if k.get('tech'):wall_cell(j,height-k.get('attic',0)-k['tech'],k['tech'],select(kit,'tech',j,k['sets'][0]))
            if k.get('attic'):wall_cell(j,height-k['attic'],k['attic'],select(kit,'attic',0,k['sets'][0]))
            own=modern or task=='A6';toprole='cornice' if task=='A6' else 'parapet'
            wall_cell(j,height,par,select(kit if own else 'common',toprole,j,k['sets'][0] if own else None),P+kit if own else P+'common')
    # Ear-clipped polygon roof via Blender tessellation, roof UV in metres/2.
    from mathutils.geometry import tessellate_polygon
    pts=[Vector((x,y,height)) for x,y in poly]
    for indices in tessellate_polygon([pts]):
        tri=[pts[i] for i in indices] if isinstance(indices[0],int) else indices
        me=bpy.data.meshes.new('roof');me.from_pydata(tri,[],[(0,1,2)]);o=bpy.data.objects.new('roof',me);bpy.context.collection.objects.link(o);me.materials.append(material(P+'roof'));uv=me.uv_layers.new()
        for i,p in enumerate(tri):uv.data[i].uv=(p.x/2,p.y/2)
    if not far:
        opts=[m for m in MODS if m['mount']=='roof' and kit in m['fits'] and m['kind']=='machine_room']
        for j,e in enumerate(entrances):
            if opts:stamp(opts[j%len(opts)],(e-width/2,0,height))
        details=[m for m in MODS if m['mount']=='roof' and kit in m['fits'] and m['kind'] not in ('machine_room','roof_exit')]
        for j,m in enumerate(details):stamp(m,(-width*.3+j*width*.15,depth*.15,height))
        ac=next((m for m in MODS if m['id']=='wall_ac'),None)
        dish=next((m for m in MODS if m['id']=='wall_dish'),None)
        drain=next((m for m in MODS if m['id']=='wall_drain'),None)
        if not modern:
            for f in range(1,floors):
                z=plinth+gh+(f-1)*k['storey']
                if ac:stamp(ac,(-width*.27,depth/2,z+1.25),(-1,0,0),(0,1,0))
                if dish and f%3==1:stamp(dish,(width*.26,depth/2,z+1.4),(-1,0,0),(0,1,0))
                if drain:stamp(drain,(width/2-.12,-depth/2,z))
    return width,depth,height+par

def common_scene():
    for j,role in enumerate(('plinth','parapet','door_steel','slab_edge','metal','roof')):
        cells=[c for c in json.loads((OUT/(P+'common.cells.json')).read_text())['cells'] if c['role']==role]
        for i,c in enumerate(cells):
            w,h=c['size_m'];x=i*3.6-5.4;z=j*2.6
            quad(P+'common',[(x,0,z),(x+w,0,z),(x+w,0,z+h),(x,0,z+h)],c)
    return 17,4,16

def gallery(task):
    opts=[m for m in MODS if m['task']==task];n=len(opts);cols=min(4,n)
    for i,m in enumerate(opts):stamp(m,((i%cols-(cols-1)/2)*6,0,(i//cols)*4+.65))
    return cols*6,5,math.ceil(n/cols)*4+1

def render(task,view,secondary=False):
    bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False);CACHE.clear()
    far=view=='far';width,depth,height=common_scene() if task=='A0' else gallery(task) if task.startswith('M') else building(task,far,tower=secondary)
    sc=bpy.context.scene;sc.render.engine='CYCLES';sc.cycles.samples=12;sc.cycles.use_denoising=True;sc.render.resolution_x=1600;sc.render.resolution_y=1000;sc.render.resolution_percentage=100
    sc.world.use_nodes=True;sc.world.node_tree.nodes['Background'].inputs[0].default_value=(.72,.75,.78,1);sc.world.node_tree.nodes['Background'].inputs[1].default_value=.8
    bpy.ops.mesh.primitive_plane_add(size=2000,location=(0,0,0 if task in KITS and task!='A0' else -.65));g=bpy.context.object;m=bpy.data.materials.new('ground');m.diffuse_color=(.24,.26,.23,1);g.data.materials.append(m)
    bpy.ops.object.camera_add();cam=bpy.context.object;sc.camera=cam
    target=Vector((0,0,height*.47));isbuild=task in KITS and task!='A0'
    if view=='front':
        cam.location=(0,depth/2+max(width,height)*1.5,height*.5) if isbuild else (0,-max(width,height)*1.7,height*.5);cam.data.type='ORTHO';cam.data.ortho_scale=max(width*1.12,height*1.7)
    elif view=='34':
        cam.location=(width/2+21,depth/2+21,1.7) if isbuild else (width*.6,-25,1.7);cam.data.type='PERSP';cam.data.lens=16 if isbuild else 28;cam.data.sensor_width=36
    else:
        cam.location=(70,depth/2+300,1.7) if isbuild else (70,-300,1.7);cam.data.type='PERSP';cam.data.lens=50;cam.data.sensor_width=36
    cam.rotation_euler=(target-Vector(cam.location)).to_track_quat('-Z','Y').to_euler();sc.view_settings.view_transform='AgX'
    if view=='34':fit_frame(sc,cam)
    kit=KITS[task]['kit'] if task in KITS else task.lower()
    if secondary:kit+='_slab' if task=='A7' else '_tower'
    sc.render.filepath=str(REND/(kit+'_'+view+'.png'));bpy.ops.render.render(write_still=True)
    if task in ('A4','A7') and not secondary:render(task,view,True)
def fit_frame(sc,cam):
    from bpy_extras.object_utils import world_to_camera_view
    bpy.context.view_layer.update();extent=1.
    for o in sc.objects:
        if o.type!='MESH' or o.name.startswith('Plane'):continue
        for corner in o.bound_box:
            p=world_to_camera_view(sc,cam,o.matrix_world@Vector(corner))
            if p.z>0:extent=max(extent,abs(p.x-.5)/.47,abs(p.y-.5)/.45)
    cam.data.lens/=extent

if __name__=='__main__':
    task=sys.argv[sys.argv.index('--')+1];MODS=json.loads((OUT/'bne_cis_modules.json').read_text())['modules']
    if task=='F1':
        import shutil
        for subtask in ('A0','A1','A2','A3','A4'):
            for view in ('front','34','far'):
                render(subtask,view)
                if subtask in ('A1','A3'):
                    kit=KITS[subtask]['kit'];shutil.copyfile(REND/(kit+'_'+view+'.png'),REND/(kit+'_after_'+view+'.png'))
    else:
        for view in ('front','34','far'):render(task,view)
