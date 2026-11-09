"""Offline orthographic previews rasterized from the exported mesh and atlas.

This renders the same triangles, glyph UVs and material palette as signmesh,
not separately laid-out text. It does not simulate BeamNG lighting or mipmaps.
"""
import argparse
import html
import json
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import numpy as np
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import europeinfra as ei
import signmesh

ART=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/regional_signs'
LABEL_FONT=ROOT/'tools/fonts/town/DejaVuSans.ttf'
EXAMPLES={
 'AD':'Andorra la Vella','AL':'Elbasan','AM':'Երևան','AT':'Pöllau','AX':'Mariehamn',
 'AZ':'Şəki','BA':'Mostar','BE':'Schilde','BG':'ПЛОВДИВ','BY':'БРЭСТ',
 'CH':'Altdorf','CY':'ΛΕΥΚΩΣΙΑ','CZ':'ČESKÝ KRUMLOV','DE':'Giebelstadt',
 'DK':'Ribe','EE':'Paide','ES':'VILLANUEVA DE LA CAÑADA','FI':'Jyväskylä',
 'FO':'Klaksvík','FR':'MARCENAT','GB':'Chipping Norton','GE':'თბილისი',
 'GG':'St Peter Port','GI':'Gibraltar','GR':'ΘΕΣΣΑΛΟΝΙΚΗ','HR':'Samobor',
 'HU':'SZENTENDRE','IE':'WESTPORT','IM':'Ramsey','IS':'Húsavík','IT':'MONTEPULCIANO',
 'JE':'St Helier','LI':'Vaduz','LT':'TRAKAI','LU':'Vianden','LV':'SIGULDA',
 'MC':'MONACO','MD':'ORHEI','ME':'Kotor','MK':'Охрид','MT':'MDINA',
 'NL':'Wekerom','NO':'Lillehammer','PL':'Obiecanowo','PT':'Ponte de Lima',
 'RO':'Recea','RS':'Краљево','RU':'ЯРОСЛАВЛЬ','SE':'Nyköping','SI':'Bled',
 'SK':'BANSKÁ ŠTIAVNICA','SM':'SAN MARINO','TR':'ÇANAKKALE','UA':'ІВАНО-ФРАНКІВСЬК',
 'VA':'CITTÀ DEL VATICANO','XK':'Prizren',
}


def render(mesh,size=(560,240),folder=ART,atlas=None):
    """Front projection with depth and alpha testing; proportions stay in metres."""
    ss=2;ww,hh=size[0]*ss,size[1]*ss
    canvas=np.empty((hh,ww,3),np.float32);canvas[:]=[234,239,242]
    depth=np.full((hh,ww),np.inf)
    points=np.asarray(mesh.p,float);triangles=np.asarray(mesh.t,int);uv=np.asarray(mesh.uv,float)
    if len(points)==0:return Image.fromarray(canvas.astype('uint8')).resize(size)
    extent=points[:,[0,2]];lo=extent.min(0);hi=extent.max(0);mid=(lo+hi)/2
    factor=min((ww-64*ss)/max(hi[0]-lo[0],.01),(hh-30*ss)/max(hi[1]-lo[1],.01))
    screen=np.c_[(points[:,0]-mid[0])*factor+ww/2,(mid[1]-points[:,2])*factor+hh/2]
    opacity=np.asarray(Image.open(atlas or folder/'sign_font_opacity.data.png'),dtype=np.float32)/255
    materials=json.loads((folder/'main.materials.json').read_text())
    for indices,material in zip(triangles,mesh.tm):
        v=screen[indices];ab,ac=v[1]-v[0],v[2]-v[0];det=ab[0]*ac[1]-ab[1]*ac[0]
        if abs(det)<1e-9:continue
        # Front-facing mesh normals point toward -Y (clockwise in image XY).
        normal=np.cross(points[indices[1]]-points[indices[0]],points[indices[2]]-points[indices[0]])
        if normal[1]>=-1e-10:continue
        low=np.maximum(np.floor(v.min(0)).astype(int),0);high=np.minimum(np.ceil(v.max(0)).astype(int),[ww-1,hh-1])
        if (high<low).any():continue
        xx,yy=np.meshgrid(np.arange(low[0],high[0]+1)+.5,np.arange(low[1],high[1]+1)+.5)
        b=((xx-v[0,0])*(v[2,1]-v[0,1])-(yy-v[0,1])*(v[2,0]-v[0,0]))/det
        c=((v[1,0]-v[0,0])*(yy-v[0,1])-(v[1,1]-v[0,1])*(xx-v[0,0]))/det
        a=1-b-c;z=a*points[indices[0],1]+b*points[indices[1],1]+c*points[indices[2],1]
        dst=canvas[low[1]:high[1]+1,low[0]:high[0]+1];db=depth[low[1]:high[1]+1,low[0]:high[0]+1]
        visible=(a>=-1e-9)&(b>=-1e-9)&(c>=-1e-9)&(z<db-1e-10)
        alpha=np.ones(xx.shape)
        if material in (signmesh.GLYPH,signmesh.GLYPH_WHITE):
            u=a*uv[indices[0],0]+b*uv[indices[1],0]+c*uv[indices[2],0]
            vv=a*uv[indices[0],1]+b*uv[indices[1],1]+c*uv[indices[2],1]
            tx=np.clip((u*opacity.shape[1]).astype(int),0,opacity.shape[1]-1)
            ty=np.clip(((1-vv)*opacity.shape[0]).astype(int),0,opacity.shape[0]-1)
            alpha=opacity[ty,tx];visible&=alpha>=80/255
            rgb=np.array([255,255,255]if material==signmesh.GLYPH_WHITE else [0,0,0])
        else:
            linear=np.array(materials[signmesh.MATERIALS[material]]['Stages'][0]['baseColorFactor'][:3])
            rgb=np.where(linear<=.0031308,12.92*linear,1.055*linear**(1/2.4)-.055)*255
        dst[visible]=rgb*alpha[visible,None]+dst[visible]*(1-alpha[visible,None]);db[visible]=z[visible]
    return Image.fromarray(np.clip(canvas,0,255).astype('uint8')).resize(size,Image.Resampling.LANCZOS)


def card(code,name,size=(560,340),details=(),route='',kind='begin',style=None):
    style=style or ei.town_style(code);mesh=signmesh.Mesh()
    mesh.town((0,0,0),(0,1),name,route,kind,style,details)
    image=Image.new('RGB',size,'#f7f8fa');d=ImageDraw.Draw(image)
    title=ImageFont.truetype(str(LABEL_FONT),21);small=ImageFont.truetype(str(LABEL_FONT),14)
    label=f'{code} · {name}'
    if d.textlength(label,font=title)>size[0]-36:title=ImageFont.truetype(str(LABEL_FONT),max(11,int(21*(size[0]-36)/d.textlength(label,font=title))))
    d.text((18,12),label,font=title,fill='#132a39')
    image.paste(render(mesh,(size[0],size[1]-92)),(0,44))
    plan=signmesh.town_layout(name,style,details)
    d.text((18,size[1]-38),f'{plan.width:.2f} × {plan.height:.2f} m · {plan.profile.family}',font=small,fill='#314956')
    d.text((18,size[1]-19),f'Lettering: {plan.profile.font}'+(' · supplied admin lines'if details else ''),font=small,fill='#526774')
    return image


def comparison(out,ref):
    """Read a previous commit locally; never check out or modify the worktree."""
    if not re.fullmatch('[0-9a-fA-F]{7,40}',ref):raise ValueError('--compare requires a hexadecimal commit ID')
    def read(path):
        return subprocess.run(['git','-C',str(ROOT),'show',ref+':'+path],check=True,capture_output=True).stdout
    with tempfile.TemporaryDirectory()as tmp:
        before=Path(tmp);art=before/'art';art.mkdir();(before/'data').mkdir()
        for path,dest in [('server/signmesh.py',before/'signmesh.py'),('server/europeinfra.py',before/'europeinfra.py'),
                          ('server/data/sign_font.json',before/'data/sign_font.json')]:dest.write_bytes(read(path))
        for name in ('sign_font_opacity.data.png','main.materials.json'):
            (art/name).write_bytes(read(str(ART.relative_to(ROOT)).replace('\\','/')+'/'+name))
        def load(name,path):
            spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
            sys.modules[name]=module;spec.loader.exec_module(module);return module
        old=load('_town_preview_before_mesh',before/'signmesh.py');styles=load('_town_preview_before_styles',before/'europeinfra.py')
        old_layout=None
        if subprocess.run(['git','-C',str(ROOT),'cat-file','-e',ref+':server/townlayout.py'],capture_output=True).returncode==0:
            (before/'townlayout.py').write_bytes(read('server/townlayout.py'))
            old_layout=load('_town_preview_before_layout',before/'townlayout.py')
        countries=['FR','DE','AT','NL','BE','PL','RO','SE'];sheet=Image.new('RGB',(1500,80+len(countries)*190),'#f7f8fa')
        d=ImageDraw.Draw(sheet);title=ImageFont.truetype(str(LABEL_FONT),22);small=ImageFont.truetype(str(LABEL_FONT),15)
        d.text((16,12),'Avant / après · maillages réels, rendus hors jeu',font=title,fill='#132a39')
        d.text((16,45),'Cadrages individuels ; dimensions indiquées en mètres. Les versions en jeu utilisent les noms disponibles.',font=small,fill='#526774')
        for row,code in enumerate(countries):
            name=EXAMPLES[code];route='D679'if code=='FR'else'';y=80+row*190
            d.text((16,y+20),code,font=title,fill='#132a39')
            m=old.Mesh();oldstyle=None if code=='FR'else styles.town_style(code)
            current_layout=sys.modules.get('townlayout')
            try:
                if old_layout is not None:sys.modules['townlayout']=old_layout
                m.town((0,0,0),(0,1),name,route,'begin',oldstyle)
                oldw=old.layout(name,True if oldstyle is None else oldstyle.uppercase)[1]
                if old_layout is not None:
                    oldplan=old.town_layout(name,oldstyle);oldw=oldplan.width;oldh=oldplan.height;oldfont=oldplan.profile.font
                else:oldh=1.06 if oldstyle and oldstyle.skyline else .70;oldfont='alphabet commun'
            finally:
                if current_layout is not None:sys.modules['townlayout']=current_layout
                else:sys.modules.pop('townlayout',None)
            sheet.paste(render(m,(650,148),folder=art),(100,y))
            d.text((115,y+151),f'Avant : {oldw:.2f} × {oldh:.2f} m · {oldfont}',font=small,fill='#526774')
            m=signmesh.Mesh();style=ei.town_style(code);m.town((0,0,0),(0,1),name,route,'begin',style)
            sheet.paste(render(m,(650,148)),(820,y));p=signmesh.town_layout(name,style)
            d.text((835,y+151),f'Après : {p.width:.2f} × {p.height:.2f} m · {p.profile.font}',font=small,fill='#526774')
        sheet.save(out/'Avant-apres.png')


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('out',type=Path,nargs='?',default=ROOT/'build/town-previews')
    ap.add_argument('--compare',help='previous hexadecimal commit ID, read without changing branches')
    args=ap.parse_args();out=args.out;out.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for code in sorted(ei.COUNTRIES):
        name=EXAMPLES[code];plan=signmesh.town_layout(name,ei.town_style(code))
        card(code,name).save(out/(code+'.png'))
        manifest.append({'country':code,'name':name,'size_m':[plan.width,plan.height],
                         'font':plan.profile.font,'family':plan.profile.family})
    for page,start in enumerate(range(0,len(manifest),16),1):
        entries=manifest[start:start+16];sheet=Image.new('RGB',(2240,1360),'#e1e7eb')
        for i,item in enumerate(entries):sheet.paste(Image.open(out/(item['country']+'.png')),((i%4)*560,(i//4)*340))
        sheet.save(out/f'Europe-{page}.png')
    reference=[('FR','MARCENAT',(),'D679'),('DE','Giebelstadt',('Kreis Würzburg',),''),
               ('AT','Unterkirchen',('Gde. Leutasch',),''),('NL','Wekerom',('gem. Ede',),''),
               ('BE','Schilde',(),''),('PL','Obiecanowo',(),''),('RO','Recea',(),''),('SE','Nyköping',(),'')]
    sheet=Image.new('RGB',(1680,1020),'white')
    for i,(code,name,details,route)in enumerate(reference):
        sheet.paste(card(code,name,details=details,route=route),((i%3)*560,(i//3)*340))
    sheet.save(out/'Apercu-principal.png')
    lengths=[('FR','Y'),('FR','SAINT-REMY-EN-BOUZEMONT-SAINT-GENEST-ET-ISSON'),
             ('DE','Ulm'),('DE','Bad Homburg vor der Höhe'),('AT','Pöllau'),('AT','Bruck an der Großglocknerstraße'),
             ('PL','Łódź'),('PL','Nowe Miasto nad Pilicą'),('RU','САНКТ-ПЕТЕРБУРГ'),('UA','ІВАНО-ФРАНКІВСЬК'),
             ('GR','ΘΕΣΣΑΛΟΝΙΚΗ'),('TR','İZMİR')]
    sheet=Image.new('RGB',(1680,1360),'white')
    for i,(code,name)in enumerate(lengths):sheet.paste(card(code,name),((i%3)*560,(i//3)*340))
    sheet.save(out/'Noms-courts-longs.png')
    french=[('MARCENAT','D679'),('CHEYLADE','D62'),('GŒRSDORF',''),("L’HÔPITAL",''),('SAINT-ÉTIENNE',''),('SAINT-REMY-EN-BOUZEMONT-SAINT-GENEST-ET-ISSON','')]
    sheet=Image.new('RGB',(1920,780),'#f7f8fa');d=ImageDraw.Draw(sheet)
    d.text((18,8),'France · Caractères L1 — Typoroute 2002 L1 100',font=ImageFont.truetype(str(LABEL_FONT),23),fill='#132a39')
    d.text((18,37),'Rendus hors jeu des maillages du mod · lettres et crénage de la numérisation L1 · largeur adaptée au nom',font=ImageFont.truetype(str(LABEL_FONT),17),fill='#526774')
    for i,(name,route)in enumerate(french):sheet.paste(card('FR',name,size=(640,350),route=route),((i%3)*640,80+(i//3)*350))
    sheet.save(out/'France-L1.png')
    if args.compare:comparison(out,args.compare)
    (out/'catalogue.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    cards=''.join(f'<article><img src="{r["country"]}.png" alt="{html.escape(r["name"])}"><p>{r["country"]} · {html.escape(r["family"])}</p></article>'for r in manifest)
    (out/'index.html').write_text('''<!doctype html><html lang="fr"><meta charset="utf-8"><title>Panneaux européens — previews</title>
<style>body{font:16px system-ui;background:#e8edf0;color:#142c3a;margin:32px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}article{background:white;border-radius:10px;overflow:hidden}img{width:100%;display:block}p{padding:0 16px}h1{font-size:26px}</style>
<h1>Panneaux de localité · 56 pays et territoires</h1>
<p>Rendus hors jeu des vrais maillages et de leur atlas de lettres. Chaque carte est cadrée séparément : comparer les dimensions indiquées, pas la taille à l’écran.</p>
<p>Tratex : source suédoise officielle. Transport et CCRIGE : reconstructions des dessins nationaux. Trafikk : interprétation des spécifications norvégiennes. D-DIN et DejaVu : substituts, sans prétendre reproduire exactement les alphabets nationaux.</p>
<p>France et Monaco : Typoroute 2002 L1 100, numérisation L1 de Gwenaël Jouvin sous licence SIL OFL 1.1, avec crénage et trait d’union contextuel. Le TTF est conservé sans modification ; son auteur le présente comme une version de test.</p>
<p>Les petites lignes administratives visibles dans Apercu-principal.png proviennent des photos de référence. Elles ne seront affichées en jeu que si ces données sont fournies. Les autres cartes utilisent seulement le nom de localité disponible.</p>
<p>Les variantes bilingues, écussons locaux, informations administratives et silhouettes non vérifiées ne constituent pas une reproduction homologuée. Voir docs/TOWN_SIGNS.md et les sources dans le dépôt.</p><main>'''+cards+'</main></html>',encoding='utf-8')
    print(f'{len(manifest)} country meshes previewed in {out}')


if __name__=='__main__':main()
