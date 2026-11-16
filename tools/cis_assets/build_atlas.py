"""Deterministic, original procedural atlases. No fonts or external imagery."""
import sys,json,uuid,math
sys.dont_write_bytecode=True
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFilter
from specs import *

def dump(path,obj):
    path.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n',encoding='utf-8')

def material(name):
    path='/levels/beamng_earth/art/shapes/beamngEarth/cis/'+name
    return dict(name=name,mapTo=name,**{'class':'Material'},version=1.5,persistentId=str(uuid.uuid5(uuid.NAMESPACE_URL,'bne-cis/'+name)),Stages=[dict(baseColorFactor=[1,1,1,1],metallicFactor=0.,baseColorMap=path+'.color.dds',normalMap=path+'.normal.dds',roughnessMap=path+'.data.dds'),{},{},{}],doubleSided=False)

def register(name):
    f=OUT/'main.materials.json';m=json.loads(f.read_text()) if f.exists() else {};m[name]=material(name);dump(f,m)

class Paint:
    def __init__(self,w,h,base,seed):
        self.w,self.h=w,h; self.weathered=False;self.windows=[]; self.rng=np.random.default_rng(seed)
        noise=self.rng.normal(0,1.5,(h,w,1))
        self.im=Image.fromarray(np.clip(np.array(base)[None,None,:]+noise,0,255).astype('uint8'))
        self.he=Image.new('L',(w,h),180);self.li=Image.new('L',(w,h),0);self.ro=Image.new('L',(w,h),215)
        self.d=ImageDraw.Draw(self.im);self.hd=ImageDraw.Draw(self.he);self.ld=ImageDraw.Draw(self.li);self.rd=ImageDraw.Draw(self.ro)
    def rect(self,xy,c,height=None,lit=0,rough=215):
        xy=tuple(int(round(a)) for a in xy)
        if xy[2]<xy[0] or xy[3]<xy[1]:return
        self.d.rectangle(xy,fill=c)
        if height is not None:self.hd.rectangle(xy,fill=height)
        self.ld.rectangle(xy,fill=lit);self.rd.rectangle(xy,fill=rough)
    def line(self,xy,c,width=1,height=None):
        self.d.line(xy,fill=c,width=width)
        if height is not None:self.hd.line(xy,fill=height,width=width)
    def rust(self,x,y,length=30):
        for j in range(4):
            self.line((x+j,y,x+j,y+length-j*5),(119+j*12,94+j*12,72+j*14),1)
    def window(self,x,y,w,h,style=0,bars=False,modern=False):
        x,y,w,h=map(int,(x,y,w,h));self.windows.append((x,y,w,h,style));f=(76,71,61) if style==3 else (216,213,196) if style<3 else (225,224,213)
        if modern:f=(64,69,71) if style==0 else (222,223,217)
        self.rect((x-6,y-6,x+w+6,y+h+7),(111,112,106),112)
        self.rect((x-3,y-3,x+w+3,y+h+3),f,160)
        # No sky reflection: only a soft vertical interior gradient.
        for yy in range(y+5,y+h-4):
            v=round(26+13*(yy-y)/h);self.rect((x+5,yy,x+w-5,yy),(v+3,v+8,v+9),72,255,50)
        panes=3 if w>180 else 2
        for p in range(panes):
            xx=x+int(p*w/panes)
            if style%3==1:
                self.rect((xx+8,y+8,xx+int(w/panes*.45),y+h-8),(133,126,109),80,150,185)
                for cx in range(xx+10,xx+int(w/panes*.45),5):self.line((cx,y+8,cx,y+h-8),(119,114,101))
            if style%5==2:
                for yy in range(y+10,y+h-7,6):self.line((xx+7,yy,xx+w/panes-7,yy),(98,102,96))
            if p:self.rect((xx-2,y,xx+2,y+h),f,170)
        if not modern and style<4:
            self.rect((x,y+int(h*.33)-2,x+w,y+int(h*.33)+2),f,170)
            for _ in range(30):
                xx=int(self.rng.integers(x,x+w));yy=int(self.rng.choice([y+1,y+h-1]));self.line((xx,yy,xx+3,yy),(114,109,88))
        if style==7:
            for xx in range(x+7,x+int(w/2),4):self.line((xx,y+6,xx,y+h-6),(81,86,80))
        self.rect((x-6,y+h+4,x+w+7,y+h+8),(186,185,175),200)
        if bars:
            for xx in range(x+8,x+w,17):self.line((xx,y+2,xx,y+h),(48,53,49),2,190)
            for yy in (y+20,y+h-20):self.line((x,yy,x+w,yy),(48,53,49),2,190)
        if style%4==0 and not modern:
            self.rect((x+w-26,y+h-15,x+w-10,y+h-6),(120,83,61),100)
            self.d.ellipse((x+w-28,y+h-30,x+w-9,y+h-14),fill=(60,78,51))
        if self.weathered and not modern:
            if style<4:
                for _ in range(90):
                    side=int(self.rng.integers(4));u=float(self.rng.random());length=int(self.rng.integers(1,7))
                    if side<2:xx=x-1+side*(w+2);yy=y+int(u*h);box=(xx,yy,xx+1,yy+length)
                    else:xx=x+int(u*w);yy=y-1+(side-2)*(h+2);box=(xx,yy,xx+length,yy+1)
                    self.d.rectangle(box,fill=(117,113,97));self.rd.rectangle(box,fill=225)
            if style in (5,8):
                for yy in range(y+10,y+h-8,5):
                    self.d.line((x+8,yy,x+w-8,yy),fill=(112,108,94),width=2)
                    self.ld.line((x+8,yy,x+w-8,yy),fill=150,width=2)
            if style in (1,6,9):
                xx=x+w//3;yy=y+h//2
                self.d.polygon([(xx-10,yy+6),(xx-5,yy-9),(xx+7,yy-9),(xx+12,yy+6)],fill=(170,147,105))
                self.ld.polygon([(xx-10,yy+6),(xx-5,yy-9),(xx+7,yy-9),(xx+12,yy+6)],fill=175)
                self.d.line((xx+1,yy+6,xx+1,y+h-8),fill=(103,95,77),width=2)
    def door(self,x,y,w,h,variant):
        c=[(82,61,48),(49,68,56),(94,96,92)][variant%3]
        self.rect((x-5,y-5,x+w+5,y+h+2),(88,88,81),80)
        self.rect((x,y,x+w,y+h),c,145,0,140)
        self.rect((x+7,y+10,x+w-8,y+h-10),tuple(t+8 for t in c),151,0,145)
        self.rect((x+w-11,y+h*.48,x+w-8,y+h*.58),(177,176,160),178,0,100)
        self.rect((x+w+7,y+h*.45,x+w+15,y+h*.59),(55,56,51),170)
        for yy in range(int(y+h*.48),int(y+h*.57),4):self.rect((x+w+9,yy,x+w+10,yy+1),(148,148,133),175)
        self.line((x+8,y+8,x+w-16,y+15),(156,154,142),3,176)


def paint_cell(task,c):
    if task in ('A6','A7','A8','A9'):return paint_extended_cell(task,c)
    role=c['role'];i=c['variant'];tag=c['tags'][0];w,h=[round(v*96) for v in c['size_m']];modern=task=='A5'
    base={'tile_beige':(190,187,168),'tile_blue':(175,190,191),'silicate':(183,180,161),'red':(153,100,76),'aggregate':(180,177,162),'painted':(194,191,174),'panel':(200,201,192),'vent':(207,209,205),'plaster':(220,205,174),'common':(155,153,141)}[tag]
    p=Paint(w,h,base,1000+TASKS.index(task)*100+i+list(KITS[task]['roles']).index(role)*21)
    p.weathered=task in ('A1','A2','A3','A4','A6','A7')
    if task=='A0':
        if role=='plinth':
            colors=[(148,146,135),(81,84,79),(134,80,64),(165,140,82)];p=Paint(w,h,colors[i],40+i)
            p.rect((w*.37,h*.32,w*.63,h*.68),(46,48,45),60)
            for xx in range(int(w*.38),int(w*.63),5):p.line((xx,h*.34,xx,h*.66),(98,99,88),1,150)
            if i%2:
                for yy in range(int(h*.34),int(h*.66),5):p.line((w*.38,yy,w*.62,yy),(98,99,88),1,150)
            p.rect((w*.82,h*.65,w*.89,h*.72),(55,58,54),70)
        elif role=='parapet':
            if i==2:brick(p,(153,117,88))
            p.rect((0,0,w-1,8),(133,141,139),205,rough=130)
            for xx in (70,180,260):p.rust(xx,10,35)
        elif role=='roof':
            p=Paint(w,h,(57,59,54),43)
            for xx in range(0,w,64):p.line((xx,0,xx,h),(43,45,41),2,165)
            p.rect((30,50,85,105),(64,66,61),186)
            p.d.ellipse((105,112,156,133),fill=(49,52,49))
        elif role=='door_steel':p.door(8,3,w-25,h-7,i)
        elif role=='slab_edge':
            p.rect((0,h-6,w-1,h-1),(91,91,83),140)
            for xx in range(15,w,41):p.rust(xx,2,17)
        elif role=='metal':
            p=Paint(w,h,(161,169,164) if i==0 else (134,151,130),60+i)
            for xx in range(0,w,12):p.line((xx,0,xx,h-1),(148,157,149),1,176)
        if role in ('plinth','parapet'):weathering(p,role,c,base)
        return finish(p,role,base,task)
    if tag in ('silicate','red'):brick(p,base)
    elif tag.startswith('tile') and 'bare' not in c['tags']:
        for yy in range(0,h,5):p.line((0,yy,w-1,yy),tuple(v-9 for v in base),1,176)
        for xx in range(0,w,5):p.line((xx,0,xx,h-1),tuple(v-9 for v in base),1,176)
        for _ in range(60):
            xx=int(p.rng.integers(3,w//5-2))*5; yy=int(p.rng.integers(3,h//5-2))*5
            p.rect((xx,yy,xx+3,yy+3),(153,154,143),169)
    elif tag=='aggregate':
        for _ in range(w*h//8):
            xx=int(p.rng.integers(1,w-1));yy=int(p.rng.integers(1,h-1));v=int(p.rng.integers(-25,24));p.d.point((xx,yy),fill=tuple(max(0,min(255,t+v)) for t in base))
    elif tag=='vent':
        for yy in range(0,h,58):p.line((0,yy,w,yy),(132,139,138),1,165)
        for xx in range(0,w,115):p.line((xx,0,xx,h),(132,139,138),1,165)
    if modern:
        if tag=='plaster':
            accent=[(179,125,99),(215,145,66),(192,178,98),(177,156,128)][i%4]
            p.rect((w-45,4,w-5,h-5),accent,180)
        elif i%4==3:
            p.rect((4,4,w-5,h-5),(118,127,129),180)
    if not modern:
        # Weathering stays away from shared boundaries.
        wash=Image.new('RGBA',(w,h));d=ImageDraw.Draw(wash)
        for _ in range(22):
            xx=int(p.rng.integers(15,w-15));yy=int(p.rng.integers(20,max(21,h-25)));d.ellipse((xx-7,yy-15,xx+7,yy+23),fill=(80,77,62,12))
        p.im=Image.alpha_composite(p.im.convert('RGBA'),wash.filter(ImageFilter.GaussianBlur(5))).convert('RGB');p.d=ImageDraw.Draw(p.im)
    if task=='A4' or tag=='painted':
        p.rect((w-40,4,w-10,h-7),[(158,176,180),(175,128,107),(196,182,125)][i%3],180)
    if p.weathered and ('insulated' in c['tags'] or (role in ('bay','end','ground') and i%4==1)):
        pastel=[(205,176,169),(176,199,178),(216,206,158),(176,194,203),(195,170,119)][int(p.rng.integers(5))]
        patch=(int(w*.16),int(h*.08),int(w*.84),int(h*.9))
        p.rect(patch,(115,115,102),185,rough=195)
        p.rect((patch[0]+2,patch[1]+2,patch[2]-2,patch[3]-2),pastel,193,rough=165)
        p.line((patch[0]+3,patch[1]+4,patch[2]-3,patch[1]+4),tuple(min(255,v+9) for v in pastel),1,194)
    if role in ('bay_balcony','bay_balcony_far') and task in ('A3','A4'):
        p.rect((w*.07,5,w*.93,h-7),tuple(round(v*.86) for v in base),145)
        p.rect((w*.07,5,w*.93,14),tuple(round(v*.72) for v in base),130)
    if role in ('bay','ground','bay_balcony','bay_balcony_far'):
        ww,hh=(230,192) if modern else (round(1.35*96),round(1.45*96)) if task=='A2' else (139,144)
        y=h-round(.85*96)-hh if role!='ground' or not modern else 35
        if modern and role=='ground':ww,hh=w-30,h-65
        if not modern and role=='ground' and i==2:ww,hh=w-45,h-50;y=20
        p.window((w-ww)//2,max(15,y),ww,hh,(0 if tag=='vent' else 5) if modern else i%10,'bars' in c['tags'],modern)
        if task=='A3' and role=='bay' and i in (6,7):
            p.rect((w*.78,h*.63,w*.96,h*.82),(143,143,130),175)
        if task=='A2':p.rect(((w-ww)/2-7,max(5,y-12),(w+ww)/2+7,max(10,y-6)),(158,158,143),184)
        if role.startswith('bay_balcony'):
            p.window(w*.56,22,w*.21,h-35,5 if modern else i%4,modern=modern)
            if role=='bay_balcony_far':
                # Painted front only for distant skin; near cells remain unobstructed.
                p.rect((w*.08,h*.64,w*.92,h-9),(172,175,161) if i%3 else (132,151,135),195)
                if i%5 in (1,2,3) or modern:
                    p.window(w*.08,14,w*.84,h*.59,5 if i%2 else 2,modern=modern)
                else:
                    for xx in range(int(w*.1),int(w*.91),13):p.line((xx,h*.6,xx,h-9),(82,93,83),2,199)
                    p.line((w*.08,h*.6,w*.92,h*.6),(92,101,91),3,200)
                    if i==0:
                        p.line((w*.15,h*.48,w*.8,h*.48),(74,78,69),1)
                        for xx in (w*.3,w*.5):p.rect((xx,h*.48,xx+17,h*.57),(175,174,152),170)
                if modern:p.rect((w*.08,h*.64,w*.92,h-11),(175,185,181),168,0,115)
                p.rect((w*.05,h-10,w*.95,h-4),(122,124,112),200)
                if not modern:
                    for xx in (w*.2,w*.7):p.rust(int(xx),h-15,10)
    elif role=='stair':
        if task=='A4':p.window(w*.32,4,w*.36,h-10,5)
        elif task=='A3':
            for j in range(3):p.window(w*.16+j*w*.24,12,w*.19,68,2)
        else:p.window(w*.35,10,w*.3,78,5 if modern else i,modern=modern)
    elif role=='entrance':
        if modern:p.window(w*.15,15,w*.7,h-22,0,modern=True)
        else:
            common=json.loads((OUT/(P+'common.cells.json')).read_text());door=[x for x in common['cells'] if x['role']=='door_steel'][i%3];x,y,cw,ch=door['px'];xx=(w-cw)//2
            for suffix,dest in [('color',p.im),('lit',p.li),('data',p.ro)]:
                src=Image.open(OUT/(P+'common.'+suffix+'.png')).crop((x,y,x+cw,y+ch));dest.paste(src.convert(dest.mode),(xx,h-ch))
            p.hd.rectangle((xx,h-ch,xx+cw,h-1),fill=145)
            p.window(w*.36,12,w*.28,25,2)
    elif role=='tech':
        for xx in (w*.22,w*.66):
            p.rect((xx,45,xx+38,83),(69,74,68),80)
            for yy in range(48,82,5):p.line((xx,yy,xx+38,yy),(124,129,119),2,157)
    elif role=='end':
        if i==1 and not modern:p.rect((w*.2,16,w*.75,h-16),(193,192,166),195)
        if i==2 and task=='A2':
            p.line((w*.4,5,w*.45,h*.4,w*.42,h*.6,w*.5,h-6),(127,125,111),4,167)
            p.line((w*.4+2,5,w*.45+2,h*.4,w*.42+2,h*.6,w*.5+2,h-6),(173,172,153),2,177)
        if i==2 and not modern and task!='A2':
            p.rect((w-20,0,w-12,h-1),(111,117,108),200)
            for yy in range(20,h,75):p.rect((w-23,yy,w-9,yy+4),(76,82,75),202)
        if task=='A3' and i==3:
            for j,col in enumerate([(155,150,115),(146,161,161),(172,143,123)]):p.rect((40+j*40,20,68+j*40,h-20),col,182)
    if p.weathered:weathering(p,role,c,base)
    return finish(p,role,base,task)

def brick(p,base):
    # 7.7 cm nominal mortar-inclusive courses, running bond.
    for row,yy in enumerate(round(r*.077*96) for r in range(math.ceil(p.h/(.077*96)))):
        p.line((0,yy,p.w-1,yy),(128,125,112),1,156)
        for xx in range(-12+(row%2)*12,p.w,24):
            p.line((xx,yy,xx,yy+6),(128,125,112),1,156)
            if xx>2 and xx+21<p.w-3 and yy>2 and yy+5<p.h-3:
                v=int(p.rng.integers(-9,10));p.rect((xx+2,yy+2,xx+21,yy+5),tuple(max(0,t+v) for t in base),180+v//3)


def weathering(p,role,c,base):
    """Seeded non-directional wear; masks protect glass and canonical seams."""
    w,h=p.w,p.h;rng=p.rng
    layer=Image.new('RGBA',(w,h));d=ImageDraw.Draw(layer)
    # Damp and soot form broad, soft irregular patches, not a baked sunlight direction.
    for _ in range(15):
        x=int(rng.integers(8,w-8));y=int(rng.integers(6,h-6));rx=int(rng.integers(6,30));ry=int(rng.integers(12,55))
        d.ellipse((x-rx,y-ry,x+rx,y+ry),fill=(70,73,56,int(rng.integers(18,45))))
    if role in ('plinth','ground','parapet'):
        for x in range(7,w-7,14):
            yy=h-4 if role!='parapet' else 8;length=int(rng.integers(12,min(50,h-5)))
            d.ellipse((x-10,yy-length,x+12,yy+length),fill=(57,67,49,55))
    layer=layer.filter(ImageFilter.GaussianBlur(7));d=ImageDraw.Draw(layer)
    def streak(x,y,length):
        length=max(3,min(length,h-y-5))
        for step in range(length):
            alpha=round(95*(1-step/length)**1.3);ww=max(1,round(4*(1-step/length)))
            d.line((x-ww,y+step,x+ww,y+step),fill=(133,77,39,alpha))
    for x,y,ww,hh,style in p.windows:
        for xx in (x+5,x+ww-6):streak(xx,y+hh+9,int(rng.integers(29,116)))
    if role in ('bay_balcony','bay_balcony_far'):
        for x in (int(w*.18),int(w*.34),int(w*.75)):streak(x,7,int(rng.integers(29,116)))
    layer=layer.filter(ImageFilter.GaussianBlur(1.8));a=np.array(layer);glass=np.array(p.li)>0;a[glass,3]=0
    a[:4,:,3]=0;a[-4:,:,3]=0;a[:,:4,3]=0;a[:,-4:,3]=0
    p.im=Image.alpha_composite(p.im.convert('RGBA'),Image.fromarray(a)).convert('RGB');p.d=ImageDraw.Draw(p.im)
    # Local repair mortar stays inside the canonical joint edge.
    if role not in ('roof','metal','door_steel','slab_edge'):
        if c.get('variant',0)%2:
            yy=int(h*.22);p.rect((3,yy,6,min(h-5,yy+int(h*.28))),(155,155,140),169,rough=230)
            p.rect((int(w*.56),h-7,int(w*.82),h-4),(154,153,139),168,rough=230)
    if role=='bay' and c.get('variant',0)%5 in (1,3):
        # Ambient contact footprints only: AC enclosure and small dish bracket.
        xx=int(w*.78);yy=int(h*.54)
        contact=Image.new('RGBA',(w,h));dd=ImageDraw.Draw(contact)
        dd.rectangle((xx,yy,xx+int(w*.16),yy+int(h*.16)),fill=(47,50,44,55))
        if c.get('variant',0)%5==3:dd.ellipse((12,55,34,77),fill=(48,51,44,65))
        contact=contact.filter(ImageFilter.GaussianBlur(2));arr=np.array(contact);arr[glass,3]=0
        p.im=Image.alpha_composite(p.im.convert('RGBA'),Image.fromarray(arr)).convert('RGB');p.d=ImageDraw.Draw(p.im)
    if role=='bay_balcony_far' and c.get('variant',0)%5 in (0,4):
        y=int(h*.43);p.line((w*.13,y,w*.86,y),(83,80,67),1)
        for j,col in enumerate(((177,171,151),(133,150,159),(192,178,161))):
            x=int(w*(.23+j*.19));p.rect((x,y+1,x+14,y+25+j*3),col,164,rough=220)

def finish(p,role,base,task):
    # Canonical per-set edges: arbitrary variant adjacency does not create colour steps.
    if role not in ('door_steel','metal','slab_edge','roof'):
        p.rect((0,0,2,p.h-1),base,180);p.rect((p.w-3,0,p.w-1,p.h-1),base,180)
        p.rect((0,0,p.w-1,2),base,180)
        joint=(46,49,45) if p.weathered else (81,83,75) if task!='A5' else (150,154,148)
        if task=='A6':joint=tuple(round(v*.78) for v in base)
        if task in ('A8','A9'):joint=(153,157,151)
        p.rect((0,p.h-(1 if task in ('A8','A9') else 3),p.w-1,p.h-1),joint,140)
        if task not in ('A0','A2','A5','A6','A7','A8'):
            p.rect((0,0,1,p.h-1),joint,140);p.rect((p.w-2,0,p.w-1,p.h-1),joint,140)
    elif role=='roof':
        for im in (p.im,p.he,p.ro,p.li):
            im.paste(im.crop((0,0,1,p.h)),(p.w-1,0));im.paste(im.crop((0,0,p.w,1)),(0,p.h-1))
    hh=np.asarray(p.he.filter(ImageFilter.GaussianBlur(.65)),float)/255
    dx=(np.roll(hh,-1,1)-np.roll(hh,1,1))*2.6;dy=(np.roll(hh,-1,0)-np.roll(hh,1,0))*2.6
    n=np.dstack((-dx,dy,np.ones_like(hh)));n/=np.linalg.norm(n,axis=2,keepdims=True)
    normal=Image.fromarray(np.round((n*.5+.5)*255).astype('uint8'))
    return dict(color=p.im,normal=normal,data=p.ro,lit=p.li)

def pack(cells,size=2048,region=None):
    # Best short-side fit, no rotation; rectangular free-space partition.
    free=[region or (4,4,size-8,size-8)]
    for c in sorted(cells,key=lambda c:-round(c['size_m'][1]*96)*round(c['size_m'][0]*96)):
        w,h=[round(v*96) for v in c['size_m']];fw,fh=w+4,h+4
        fits=[(min(r[2]-fw,r[3]-fh),r[2]*r[3],i,r) for i,r in enumerate(free) if r[2]>=fw and r[3]>=fh]
        if not fits:raise ValueError('Atlas packing exhausted: '+c['id'])
        _,_,idx,(x,y,rw,rh)=min(fits);free.pop(idx);c['px']=[x,y,w,h]
        # Guillotine split along the greater leftover axis.
        if rw-fw>rh-fh:free.extend([(x+fw,y,rw-fw,rh),(x,y+fh,fw,rh-fh)])
        else:free.extend([(x+fw,y,rw-fw,fh),(x,y+fh,rw,rh-fh)])
        free=[r for r in free if r[2]>0 and r[3]>0]

def build(task):
    k=KITS[task];cells=cell_specs(task);size=k.get('atlas',2048)
    if k.get('per_set'):
        for j,tag in enumerate(k['sets']):pack([c for c in cells if tag in c['tags']],size,(4+j*(size//2),4,size//2-8,size-8))
    else:pack(cells,size)
    atl={s:Image.new('L' if s in ('data','lit') else 'RGB',(size,size),0 if s=='lit' else 215 if s=='data' else (128,128,255) if s=='normal' else (120,120,110)) for s in ('color','normal','data','lit')}
    for c in cells:
        maps=paint_cell(task,c);x,y,w,h=c['px']
        for s,im in maps.items():
            a=np.array(im);pad=((2,2),(2,2)) if a.ndim==2 else ((2,2),(2,2),(0,0));atl[s].paste(Image.fromarray(np.pad(a,pad,mode='edge')),(x-2,y-2))
        c.pop('variant')
    name=P+k['kit']
    for s,im in atl.items():im.save(OUT/(name+'.'+s+'.png'))
    dump(OUT/(name+'.cells.json'),dict(kit=k['kit'],version=1,px_per_m=96,atlas=[size,size],bay_m=k['bay'],storey_m=k['storey'],cells=cells))
    register(name)
    if task=='A0':
        c=next(c for c in cells if c['role']=='roof');x,y,w,h=c['px']
        for s,im in atl.items():im.crop((x,y,x+w,y+h)).resize((1024,1024),Image.Resampling.BICUBIC).save(OUT/(P+'roof.'+s+'.png'))
        register(P+'roof')
        if not (OUT/'bne_cis_modules.json').exists():dump(OUT/'bne_cis_modules.json',dict(version=1,modules=[]))
    print(task,k['kit'],'atlas fill',round(sum(c['px'][2]*c['px'][3] for c in cells)/size**2*100,2),'%')


def arched_window(p,x,y,w,h,style=0,bars=False):
    """Keep the rectangular reveal/curtain painter, clip its upper corners to an arch."""
    x,y,w,h=map(int,(x,y,w,h));before={k:getattr(p,k).copy() for k in ('im','he','li','ro')}
    p.window(x,y,w,h,style,bars)
    mask=Image.new('L',(p.w,p.h),0);d=ImageDraw.Draw(mask)
    d.ellipse((x-7,y-7,x+w+7,y+w+7),fill=255);d.rectangle((x-7,y+w//2,x+w+7,y+h+9),fill=255)
    region=Image.new('L',(p.w,p.h),0);ImageDraw.Draw(region).rectangle((x-8,y-8,x+w+8,y+h+10),fill=255)
    outside=np.maximum(0,np.array(region,dtype='int16')-np.array(mask,dtype='int16')).astype('uint8');outside=Image.fromarray(outside)
    for key in before:setattr(p,key,Image.composite(before[key],getattr(p,key),outside))
    p.d=ImageDraw.Draw(p.im);p.hd=ImageDraw.Draw(p.he);p.ld=ImageDraw.Draw(p.li);p.rd=ImageDraw.Draw(p.ro)
    p.d.arc((x-5,y-5,x+w+5,y+w+5),180,360,fill=(219,214,194),width=4)
    p.hd.arc((x-5,y-5,x+w+5,y+w+5),180,360,fill=196,width=4)

def paint_extended_cell(task,c):
    k=KITS[task];role=c['role'];tag=c['tags'][0];i=c['variant'];local=i%(k['roles'][role]//2)
    w,h=[round(v*96) for v in c['size_m']];modern=k.get('modern',False)
    palettes={'ochre':(195,178,136),'grey':(183,189,177),'silicate':(187,185,170),'red':(154,99,75),'clinker':(218,214,196),'plaster':(219,215,200),'mosaic':(213,218,213),'pastel':(201,215,208)}
    base=palettes[tag];p=Paint(w,h,base,8100+TASKS.index(task)*500+i+list(k['roles']).index(role)*41);p.weathered=not modern
    trim=(224,217,196) if tag=='ochre' else (224,226,214)
    if task=='A7' or (task=='A8' and tag=='clinker' and role=='ground'):brick(p,(149,91,69) if tag=='clinker' else base)
    if task=='A6':
        # Faded render and hairline repaired cracks; never directional lighting.
        for j in range(3):
            xx=int(p.rng.integers(18,w-18));yy=int(p.rng.integers(12,h-18))
            p.line((xx,yy,xx+5,min(h-6,yy+22),xx+2,min(h-5,yy+38)),tuple(v-22 for v in base),1,173)
        if role=='ground':
            for yy in range(0,h,48):
                p.rect((4,yy,w-5,yy+2),tuple(v-36 for v in base),154)
                p.line((4,yy+3,w-5,yy+3),tuple(min(255,v+12) for v in base),1,188)
        if role=='cornice':
            for z,hh,col in ((5,18,trim),(29,11,tuple(v-16 for v in trim)),(48,12,trim),(h-20,15,trim)):
                p.rect((3,z,w-4,z+hh),col,205,rough=205)
            if local==0:
                for xx in range(10,w-12,19):p.rect((xx,66,xx+9,91),trim,218)
            weathering(p,role,c,base);return finish(p,role,base,task)
        if role=='end':
            for xx in (13,w-33):
                p.rect((xx,5,xx+20,h-6),trim,198)
                for dx in (4,14):p.line((xx+dx,7,xx+dx,h-7),tuple(v-20 for v in trim),1,188)
        if role in ('bay','bay_balcony','bay_balcony_far','ground') and local%4==1:
            pastel=[(207,181,169),(179,201,181),(213,206,163),(178,196,207),(197,177,133)][i%5]
            p.rect((w*.15,h*.07,w*.85,h*.94),tuple(v-25 for v in pastel),184,rough=190)
            p.rect((w*.15+2,h*.07+2,w*.85-2,h*.94-2),pastel,193,rough=164)
    if task=='A9':
        accents=[(161,193,210),(215,151,99),(163,185,165),(218,222,213)] if tag=='mosaic' else [(197,210,184),(221,203,181)]
        accent=accents[local%len(accents)]
        p.rect((5,5,w-6,h-6),accent,182,rough=205)
        if tag=='mosaic':p.rect((w*.72,5,w-6,h-6),base,182)
        else:p.rect((5,h*.72,w-6,h-6),base,182)
    elif task=='A8' and role!='ground':
        accent=(184,177,157) if tag=='plaster' else (202,198,179)
        p.rect((w*.84,4,w-5,h-5),accent,181)
    if role in ('bay','bay_balcony','bay_balcony_far','ground','attic'):
        if task=='A6':ww,hh=125,182
        elif task=='A7':ww,hh=130,144
        else:ww,hh=(190,164) if task=='A8' else (155,153)
        y=h-82-hh
        if role=='attic':ww,hh=125,125;y=45
        if role=='ground' and (modern and local>=1 or not modern and local==2):ww,hh=w-44,h-70;y=24
        x=(w-ww)//2;y=max(14,y)
        if task=='A6':
            p.rect((x-13,y-12,x+ww+13,y+hh+13),tuple(v-35 for v in trim),190)
            p.rect((x-10,y-10,x+ww+10,y+hh+10),trim,203)
        if task=='A7' and role.startswith('bay_balcony'):
            p.rect((w*.07,5,w*.93,h-8),tuple(round(v*.77) for v in base),128)
            p.rect((w*.07,5,w*.93,14),tuple(round(v*.62) for v in base),110)
        style=(0 if tag in ('clinker','mosaic') else 5) if modern else local%8
        if task=='A6' and role=='ground' and local%2==0:arched_window(p,x,y,ww,hh,style,bars=local==0)
        else:p.window(x,y,ww,hh,style,bars=role=='ground' and not modern and local in (0,1),modern=modern)
        if task=='A6':
            p.rect((x-17,y-17,x+ww+17,y-11),trim,210)
            if local%3==0:
                for xx in range(x+4,x+ww-4,17):p.rect((xx,y+hh+15,xx+7,min(h-7,y+hh+48)),trim,200)
                p.rect((x-8,min(h-10,y+hh+48),x+ww+8,min(h-5,y+hh+53)),trim,207)
        if task=='A7':
            p.rect((x-8,y-12,x+ww+8,y-5),(163,165,151),196)
            # Efflorescence softened into the mortar field below the lintel.
            salt=Image.new('RGBA',(w,h));d=ImageDraw.Draw(salt)
            for j in range(8):
                xx=int(p.rng.integers(9,w-9));yy=int(p.rng.integers(18,h-8));d.ellipse((xx-7,yy-12,xx+8,yy+16),fill=(228,225,208,32))
            p.im=Image.alpha_composite(p.im.convert('RGBA'),salt.filter(ImageFilter.GaussianBlur(4))).convert('RGB');p.d=ImageDraw.Draw(p.im)
        if role.startswith('bay_balcony'):
            p.window(w*.6,20,w*.2,h-35,style,modern=modern)
            if role=='bay_balcony_far':
                if modern or local%3:
                    p.window(w*.08,13,w*.84,h*.58,style,modern=modern)
                    p.rect((w*.08,h*.66,w*.92,h-8),(176,182,174) if modern else (173,169,148),192,rough=180)
                else:
                    for xx in range(int(w*.1),int(w*.91),15):p.line((xx,h*.62,xx,h-8),(112,116,102),2,195)
                    p.line((w*.08,h*.62,w*.92,h*.62),(112,116,102),3,199)
                p.rect((w*.05,h-10,w*.95,h-5),(146,146,129),205)
        if role=='attic':
            p.d.line((x-14,y-6,x+ww/2,y-24,x+ww+14,y-6),fill=(102,109,105),width=4)
    elif role=='entrance':
        if modern:
            p.rect((w*.16,10,w*.84,h-6),(144,148,138),120)
            p.window(w*.22,23,w*.56,h-35,0,modern=True)
            p.rect((w*.48,24,w*.5,h-14),(78,84,81),170)
        else:
            dw,dh=115,221;x=(w-dw)//2;y=h-dh
            if task=='A6':
                p.rect((x-14,y-25,x+dw+14,h-3),trim,203)
                p.d.polygon([(x-22,y-26),(w/2,y-54),(x+dw+22,y-26)],fill=trim)
            p.door(x,y,dw,dh,local)
            if task=='A6' and local==0:
                p.d.arc((x-9,y-45,x+dw+9,y+dw-27),180,360,fill=trim,width=7)
            else:p.window(w*.37,14,w*.26,35,1)
    elif role=='stair':
        if task=='A6':arched_window(p,w*.32,24,w*.36,h-45,0)
        else:
            for xx in (w*.28,w*.53):p.window(xx,6,w*.19,h-16,5 if modern else 2,modern=modern)
    elif role=='tech':
        for xx in (w*.22,w*.65):
            p.rect((xx,40,xx+38,78),(65,72,66),78)
            for yy in range(44,77,5):p.line((xx,yy,xx+38,yy),(125,130,118),2,151)
    elif role=='parapet':
        p.rect((3,3,w-4,12),(148,155,151),202,rough=140)
        if local:p.rect((w*.7,13,w-4,h-5),tuple(v-18 for v in base),182)
    elif role=='end' and task=='A7' and local==1:
        p.line((w*.4,7,w*.46,h*.35,w*.43,h*.6,w*.49,h-7),(137,137,122),3,164)
    if task=='A7':p.rect((3,h-12,w-4,h-4),(159,162,148),197)
    if not modern:weathering(p,role,c,base)
    return finish(p,role,base,task)

if __name__=='__main__':build(sys.argv[1])
