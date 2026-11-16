"""Small welded COLLADA modules, all sharing A0 atlas cells. Run one M task at a time."""
import sys,json,math,xml.etree.ElementTree as ET
sys.dont_write_bytecode=True
import numpy as np
from specs import *
from build_atlas import dump,material
NS='http://www.collada.org/2005/11/COLLADASchema'
ET.register_namespace('',NS)
def el(parent,name,attrs=None,text=None):
    e=ET.SubElement(parent,'{'+NS+'}'+name,attrs or {})
    if text is not None:e.text=str(text)
    return e

def aliases():
    f=OUT/'main.materials.json';m=json.loads(f.read_text())
    colors=dict(concrete=[1,1,1,1],pvc=[1.42,1.4,1.4,1],wood=[1.25,1.24,1.16,1],silicate=[1.25,1.6,2.1,1],glass=[.19,.25,.26,1],frosted=[1.05,1.1,1.09,1],graphite=[.3,.34,.35,1],sheet_brown=[.62,.43,.3,1],brick=[1,1,1,1],lamp=[1.6,1.5,1.25,1])
    for short,tint in colors.items():
        n=P+short;d=material(n);st=d['Stages'][0];st['baseColorFactor']=tint
        for key in ('baseColorMap','normalMap','roughnessMap'):st[key]=st[key].replace(n+'.',P+'common.')
        if short=='lamp':st['emissiveFactor']=[1,.85,.55];st['emissive']=True;st['glow']=True
        m[n]=d
    dump(f,m)

def uvcell(role,variant=0,crop=None):
    cells=json.loads((OUT/(P+'common.cells.json')).read_text())['cells'];c=[c for c in cells if c['role']==role][variant];x,y,w,h=c['px']
    if crop:
        a,b,cc,d=crop;x,y,w,h=x+a*w,y+b*h,(cc-a)*w,(d-b)*h
    return [(x/2048,1-(y+h)/2048),((x+w)/2048,1-(y+h)/2048),((x+w)/2048,1-y/2048),(x/2048,1-y/2048)]
STYLE={
 'cement_sheet':(P+'concrete','metal',0,None), 'slab':(P+'concrete','slab_edge',0,None), 'concrete':(P+'concrete','plinth',0,(.03,.03,.3,.95)),
 'metal':(P+'common','metal',1,None),'galv':(P+'common','metal',0,None),
 'pvc':(P+'pvc','metal',0,None),'wood':(P+'wood','slab_edge',0,None),
 'glass':(P+'glass','metal',0,None),'frosted':(P+'frosted','metal',0,None),
 'graphite':(P+'graphite','metal',0,None),'brown':(P+'sheet_brown','metal',0,None),
 'roof':(P+'common','roof',0,None),'brick':(P+'brick','parapet',2,(.02,.22,.98,.95)),
 'silicate':(P+'silicate','parapet',2,(.02,.22,.98,.95)), 'door':(P+'common','door_steel',1,None),
 'lamp':(P+'lamp','metal',0,None)}
class Mesh:
    def __init__(self):self.pos=[];self.pmap={};self.faces=[]
    def polygon(self,points,style):
        mat,role,v,crop=STYLE[style];uv=uvcell(role,v,crop);ids=[]
        for p in points:
            key=tuple(round(float(a),7) for a in p)
            if key not in self.pmap:self.pmap[key]=len(self.pos);self.pos.append(key)
            ids.append(self.pmap[key])
        n=np.cross(np.array(points[1])-points[0],np.array(points[2])-points[0]);n/=np.linalg.norm(n)
        for j in range(1,len(ids)-1):self.faces.append((mat,[ids[0],ids[j],ids[j+1]],n.tolist(),[uv[0],uv[j],uv[j+1]]))
    def box(self,style,x,y,z,w,d,h,top=None):
        a,b=x-w/2,x+w/2;c,e=y-d/2,y+d/2;f,g=z-h/2,z+h/2
        for pts in [[(a,c,f),(b,c,f),(b,c,g),(a,c,g)],[(b,e,f),(a,e,f),(a,e,g),(b,e,g)],[(a,e,f),(a,c,f),(a,c,g),(a,e,g)],[(b,c,f),(b,e,f),(b,e,g),(b,c,g)],[(a,e,f),(b,e,f),(b,c,f),(a,c,f)]]:self.polygon(pts,style)
        self.polygon([(a,c,g),(b,c,g),(b,e,g),(a,e,g)],top or style)
    def front(self,style,x,y,z,w,h):self.polygon([(x-w/2,y,z),(x+w/2,y,z),(x+w/2,y,z+h),(x-w/2,y,z+h)],style)
    def tube(self,style,x,y,z,h,r=.045,sides=6):
        low=[(x+r*math.cos(j*2*math.pi/sides),y+r*math.sin(j*2*math.pi/sides),z) for j in range(sides)];high=[(a,b,z+h) for a,b,_ in low]
        for j in range(sides):self.polygon([low[j],low[(j+1)%sides],high[(j+1)%sides],high[j]],style)
        # Triangle caps avoid polygon UVs having more than four corners.
        for j in range(1,sides-1):self.polygon([low[0],low[j+1],low[j]],style);self.polygon([high[0],high[j],high[j+1]],style)
    def write(self,name):
        root=ET.Element('{'+NS+'}COLLADA',version='1.4.1');asset=el(root,'asset');el(asset,'contributor');el(asset,'unit',dict(name='meter',meter='1'));el(asset,'up_axis',text='Z_UP')
        mats=sorted({f[0] for f in self.faces});effects=el(root,'library_effects');materials=el(root,'library_materials')
        for mat in mats:
            fx=el(effects,'effect',dict(id=mat+'-fx'));profile=el(fx,'profile_COMMON');tech=el(profile,'technique',dict(sid='common'));lam=el(tech,'lambert');diff=el(lam,'diffuse');el(diff,'color',text='.8 .8 .8 1');ma=el(materials,'material',dict(id=mat+'-mat',name=mat));el(ma,'instance_effect',dict(url='#'+mat+'-fx'))
        geoms=el(root,'library_geometries');geom=el(geoms,'geometry',dict(id=name+'-g'));mesh=el(geom,'mesh')
        normals=[f[2] for f in self.faces];uvs=[uv for f in self.faces for uv in f[3]]
        for short,values,params in [('p',self.pos,['X','Y','Z']),('n',normals,['X','Y','Z']),('uv',uvs,['S','T'])]:
            sid=name+'-'+short;src=el(mesh,'source',dict(id=sid));el(src,'float_array',dict(id=sid+'-a',count=str(len(values)*len(params))),text=' '.join(f'{v:.8g}' for row in values for v in row));acc=el(el(src,'technique_common'),'accessor',dict(source='#'+sid+'-a',count=str(len(values)),stride=str(len(params))))
            for p in params:el(acc,'param',dict(name=p,type='float'))
        vertices=el(mesh,'vertices',dict(id=name+'-v'));el(vertices,'input',dict(semantic='POSITION',source='#'+name+'-p'))
        for mat in mats:
            indices=[i for i,f in enumerate(self.faces) if f[0]==mat];t=el(mesh,'triangles',dict(material=mat,count=str(len(indices))))
            for semantic,source,offset in [('VERTEX','v',0),('NORMAL','n',1),('TEXCOORD','uv',2)]:
                attrs=dict(semantic=semantic,source='#'+name+'-'+source,offset=str(offset))
                if semantic=='TEXCOORD':attrs['set']='0'
                el(t,'input',attrs)
            el(t,'p',text=' '.join(str(v) for i in indices for j in range(3) for v in [self.faces[i][1][j],i,i*3+j]))
        scenes=el(root,'library_visual_scenes');scene=el(scenes,'visual_scene',dict(id='Scene'));node=el(scene,'node',dict(id=name,name=name,type='NODE'));inst=el(node,'instance_geometry',dict(url='#'+name+'-g'));bind=el(el(inst,'bind_material'),'technique_common')
        for mat in mats:
            im=el(bind,'instance_material',dict(symbol=mat,target='#'+mat+'-mat'));el(im,'bind_vertex_input',dict(semantic='UVMap',input_semantic='TEXCOORD',input_set='0'))
        el(el(root,'scene'),'instance_visual_scene',dict(url='#Scene'));ET.indent(root);ET.ElementTree(root).write(OUT/(name+'.dae'),encoding='utf-8',xml_declaration=True)

BALCS=['balc_kh_open_1','balc_kh_open_2','balc_kh_glazed_wood','balc_kh_glazed_pvc','balc_kh_glazed_pvc_top','balc_kh_glazed_mix','balc_kh_box']
LOGGIAS=['lg_open_screen','lg_open_metal','lg_glazed_alu','lg_glazed_pvc','lg_glazed_wood','lg_glazed_mix']
MACHINES=['mr_concrete','mr_brick_silicate','mr_brick_red']
def expected_ids(task):
    return {'M1':BALCS+[n+'_brick' for n in BALCS],'M2':LOGGIAS+[n+'_p16' for n in LOGGIAS],'M3':['entr_bracket','entr_columns','entr_vestibule'],'M4':MACHINES+[n+'_large' for n in MACHINES]+['roof_exit'],'M5':['roof_vent','roof_antenna_old','roof_dish','wall_ac','wall_dish','wall_drain'],'M6':['lg_new_vent','lg_new_plaster','balc_new_glass']}[task]

def railing(me,w,d,style='metal',bars=7):
    me.box(style,0,-d+.025,1.025,w,.04,.05)
    for i in range(bars):me.box(style,-w/2+.035+i*(w-.07)/(bars-1),-d+.025,.52,.025,.035,1.0)
    for x in (-w/2+.025,w/2-.025):me.box(style,x,-d/2,1.025,.04,d,.05)

def glazed(me,w,d,frame,lower='concrete',height=2.45,modern=False):
    low=height/3 if modern else 1.05
    me.box(lower,0,-d+.025,low/2,w,.05,low)
    me.front('glass',0,-d+.018,low,w,height-low)
    # Return glazing, with correctly outward side normals.
    for x,sign in [(-w/2,-1),(w/2,1)]:
        pts=[(x,-d+.04,low),(x,-.01,low),(x,-.01,height),(x,-d+.04,height)]
        if sign<0:pts.reverse()
        me.polygon(pts,'glass');me.box(lower,x-sign*.025,-d/2,low/2,.05,d,low)
    for z in (low,height):me.box(frame,0,-d+.014,z,w,.028,.04)
    for x in (-w/2+.02,0,w/2-.02):me.box(frame,x,-d+.012,(low+height)/2,.035,.025,height-low)


def build_one(task,id):
    me=Mesh();meta=dict(id=id,task=task,dae=P+id+'.dae',mount='wall',kind='detail',fits=['khrush_panel','khrush_brick','panel9','panel16'],weight=1.,stack='once',budget=220,origin=[0,0,0])
    if task=='M1':
        brick=id.endswith('_brick');base=id.removesuffix('_brick');w,d=(2.4,.8) if brick else (2.6,.9);meta.update(kind='balcony',fits=['khrush_brick'] if brick else ['khrush_panel'],needs_role='bay_balcony',stack='storey',budget=260 if '_top' in base else 220,weight={'balc_kh_open_1':.18,'balc_kh_open_2':.17,'balc_kh_glazed_wood':.15,'balc_kh_glazed_pvc':.30,'balc_kh_glazed_mix':.12,'balc_kh_box':.08,'balc_kh_glazed_pvc_top':0}[base],top_only=base.endswith('_top'))
        me.box('slab',0,-d/2,-.06,w,d,.12)
        if base=='balc_kh_open_1':railing(me,w,d)
        elif base=='balc_kh_open_2':
            me.box('cement_sheet',0,-d+.03,.53,w,.06,1.04)
            for x in (-w/2+.025,w/2-.025):me.box('metal',x,-d/2,.53,.04,d,1.04)
            me.box('metal',0,-d+.02,1.05,w,.04,.04)
        else:
            frame='wood' if 'wood' in base else 'galv' if 'mix' in base else 'pvc';lower='brown' if 'mix' in base else 'pvc' if 'pvc' in base else 'concrete'
            if base=='balc_kh_box':
                w+=.3;frame='pvc';lower='brown';d+=.15
                me.box('slab',0,-d/2,.0,w,d,.08)
            glazed(me,w,d,frame,lower)
            if base.endswith('_top') or base=='balc_kh_box':me.box('galv',0,-d/2,2.49,w+.1,d,.06,top='roof')
    elif task=='M2':
        small=id.endswith('_p16');base=id.removesuffix('_p16');w=2.8 if small else 3.4;d=.15
        meta.update(kind='loggia',fits=['panel16'] if small else ['panel9'],needs_role='bay_balcony',stack='storey',budget=160,weight=dict(zip(LOGGIAS,[.2,.1,.25,.3,.1,.05]))[base])
        if base=='lg_open_metal':railing(me,w,d,bars=7)
        elif base=='lg_open_screen':
            me.box('concrete',0,-d+.025,.525,w,.05,1.05)
            for x in (-w/2+.025,w/2-.025):me.box('concrete',x,-d/2,.525,.05,d,1.05)
            # Ribs are real shallow returns, 5 x 12 tris + base36 = 96.
            for j in range(5):me.box('concrete',-w*.4+j*w*.2,-d-.008,.525,.06,.016,.96)
        else:glazed(me,w,d,'pvc' if 'pvc' in base else 'wood' if 'wood' in base else 'galv','brown' if 'mix' in base else 'concrete',2.6)
    elif task=='M3':
        meta.update(kind='entrance',needs_role='entrance',budget=400,weight=1/3)
        me.box('slab',0,-.7,2.475,2.4,1.4,.15,top='roof')
        for j in range(3):me.box('concrete',0,-.45-j*.3,-.1-j*.2,2.2,.9+j*.6,.2)
        if id=='entr_columns':
            for x in (-1.05,1.05):me.box('metal',x,-1.25,1.2,.07,.07,2.4)
        elif id=='entr_bracket':
            for x in (-.9,.9):
                # Thin triangular steel plates, physically attached to wall and canopy.
                me.polygon([(x,-.02,1.95),(x,-1.2,2.39),(x,-.02,2.39)],'metal');me.polygon([(x+.025,-.02,2.39),(x+.025,-1.2,2.39),(x+.025,-.02,1.95)],'metal')
        else:
            for x in (-1.17,1.17):me.box('brick',x,-.9,1.2,.06,1.8,2.4)
            me.box('galv',0,-1.77,1.2,2.4,.06,2.4)
            me.front('door',0,-1.806,0,1.2,2.3)
        for y,z in [(-.3,.72),(-1.45,.25)]:me.box('metal',1.02,y,z,.035,.035,1.)
        me.box('metal',1.02,-.875,1.02,.035,1.2,.035)
        me.box('lamp',0,-.7,2.37,.2,.12,.06)
    elif task=='M4':
        large=id.endswith('_large');base=id.removesuffix('_large');w,d,h=(2,2.5,2.2) if base=='roof_exit' else (5,4,3.2) if large else (4,3,2.6)
        meta.update(kind='roof_exit' if base=='roof_exit' else 'machine_room',mount='roof',fits=['panel16','tower'] if large else ['panel9','panel16','brick_mid','tower'] if base=='roof_exit' else ['panel9','brick_mid'],budget=100 if base=='roof_exit' else 150,weight=1. if base=='roof_exit' else 1/3)
        style='silicate' if 'silicate' in base else 'brick' if 'red' in base else 'concrete';me.box(style,0,0,(h-.12)/2,w,d,h-.12,top='roof');me.box('slab',0,0,h-.06,w,d,.12,top='roof');me.front('door',-.6,-d/2-.001,0,1.0,2.0)
        for x in (w*.16,w*.34):
            me.front('graphite',x,-d/2-.002,h*.49,.45,.58)
            for j in range(2 if base=='roof_exit' else 4):me.box('galv',x,-d/2-.016,h*.51+j*.12,.44,.025,.025)
    elif task=='M5':
        meta.update(kind=id,mount='roof' if id.startswith('roof') else 'wall',fits=['khrush_panel','khrush_brick','panel9','panel16','new_mono'],budget={'roof_vent':40,'roof_antenna_old':120,'roof_dish':90,'wall_dish':90,'wall_ac':60,'wall_drain':30}[id])
        if id=='roof_vent':
            me.box('brick',0,0,.5,.6,.6,1.0);me.box('slab',0,0,1.16,.6,.6,.08);me.tube('graphite',0,0,1,.12,.2,4)
        elif id=='roof_antenna_old':
            me.tube('galv',0,0,0,3,.025,4)
            for j in range(5):me.box('galv',0,0,1.7+j*.25,1.3-j*.12,.02,.02)
            me.box('concrete',0,0,.05,.4,.4,.1)
        elif id in ('roof_dish','wall_dish'):
            roof=id=='roof_dish';zc=.8 if roof else 0;yc=0 if roof else -.15
            if roof:me.box('galv',0,0,.3,.04,.04,.6)
            else:me.box('galv',0,-.09,-.16,.04,.18,.035)
            ring=[(.3*math.cos(j*math.tau/12),yc-.12,zc+.3*math.sin(j*math.tau/12)) for j in range(12)]
            for j in range(12):
                me.polygon([(0,yc-.04,zc),ring[j],ring[(j+1)%12]],'pvc');me.polygon([(0,yc-.025,zc),ring[(j+1)%12],ring[j]],'galv')
            me.box('galv',0,yc-.19,zc-.1,.02,.18,.02);me.box('graphite',0,yc-.28,zc-.08,.035,.025,.045)
        elif id=='wall_ac':
            meta['origin_convention']='wall-plane centre, as explicitly required by M5'
            me.box('pvc',0,-.15,0,.8,.26,.55)
            # Fan is a six-segment recessed grille, not a costly cylinder.
            for j in range(6):me.front('graphite',-.3+j*.075,-.281,-.19,.012,.38)
            for x in (-.3,.3):me.box('galv',x,-.15,-.25,.04,.3,.04)
        else:
            me.tube('galv',0,-.06,0,2.8,.05,6)
            # Tube 20 tris; 2 single-sided clamp plates, 4 tris; total24.
            for z in (.2,2.5):me.front('graphite',0,-.112,z,.14,.025)
    elif task=='M6':
        meta.update(kind='balcony' if id=='balc_new_glass' else 'loggia',fits=['new_mono'],needs_role='bay_balcony',stack='storey',budget=140,weight=1.,sets=['vent'] if id=='lg_new_vent' else ['plaster'] if id=='lg_new_plaster' else ['vent','plaster'])
        if id=='balc_new_glass':
            me.box('slab',0,-.6,-.06,3.3,1.2,.12);me.front('frosted',0,-1.19,0,3.3,1.05)
            for x in (-1.65,1.65):
                points=[(x,-1.19,0),(x,-.01,0),(x,-.01,1.05),(x,-1.19,1.05)]
                me.polygon(points if x>0 else list(reversed(points)),'frosted')
            me.box('galv',0,-1.19,1.05,3.3,.02,.025)
        else:glazed(me,3.3,.15,'graphite' if id=='lg_new_vent' else 'pvc','frosted',2.95,True)
    a=np.array(me.pos);meta.update(tris=len(me.faces),width_m=round(float(np.ptp(a[:,0])),7),depth_m=round(float(np.ptp(a[:,1])),7),height_m=round(float(np.ptp(a[:,2])),7),bounds_min=a.min(0).tolist(),bounds_max=a.max(0).tolist())
    if meta['tris']>meta['budget']:raise ValueError(f'{id}: {meta["tris"]} > {meta["budget"]}')
    me.write(P+id);return meta

def build(task):
    aliases();path=OUT/'bne_cis_modules.json';data=json.loads(path.read_text());data['modules']=[m for m in data['modules'] if m['task']!=task]
    for id in expected_ids(task):
        m=build_one(task,id);data['modules'].append(m);print(id,m['tris'],'/',m['budget'])
    dump(path,data)
if __name__=='__main__':build(sys.argv[1])
