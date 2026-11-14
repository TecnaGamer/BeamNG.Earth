"""Reproducible shared town lettering atlas; no runtime font/Pillow dependency.

New letterforms retain natural proportions. Missing glyphs use the bundled
Unicode fallback, without transliteration or silently losing accents. Legacy
labels retain their original metrics within the repacked shared atlas.
"""
from pathlib import Path
import hashlib
import json
from PIL import Image,ImageDraw,ImageFont
from fontTools.ttLib import TTFont
ROOT=Path(__file__).resolve().parents[1]
SOURCES=ROOT/'tools/fonts/town'
OUT=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/regional_signs'
VFS='/levels/beamng_earth/art/shapes/beamngEarth/regional_signs'
RANGES=((32,127),(160,592),(880,1328),(1328,1424),(4256,4352),(7680,8192))
SPECS={
 'legacy':(ROOT/'tools/fonts/DejaVuSans-Bold.ttf',44,.78,.08,'DejaVu Sans Bold (legacy)'),
 'fallback':(SOURCES/'DejaVuSans.ttf',48,1.,.025,'DejaVu Sans (Unicode fallback)'),
 'din':(SOURCES/'D-DIN.ttf',56,1.,.035,'D-DIN (DIN-inspired substitute)'),
 'condensed':(SOURCES/'D-DINCondensed.ttf',56,1.,.04,'D-DIN Condensed (substitute)'),
 'wide':(SOURCES/'D-DINExp.ttf',56,1.,.03,'D-DIN Expanded (substitute)'),
 'transport':(SOURCES/'AG-Transport-Heavy.ttf',56,1.,0.,'AG Transport Heavy (DfT drawing reconstruction)'),
 'irish':(SOURCES/'AG-EITP-Heavy.ttf',56,1.,0.,'AG EI-TP Heavy (drawing reconstruction)'),
 'tratex_black':(SOURCES/'Tratex-Black.ttf',56,1.,.02,'Tratex Svart (Transportstyrelsen)'),
 'tratex_white':(SOURCES/'Tratex-White.ttf',56,1.,.02,'Tratex Vit (Transportstyrelsen)'),
 'ccrige':(SOURCES/'CCRIGE.otf',56,1.,0.,'CCRIGE (Javi Casas, Spanish drawing reconstruction)'),
 'trafikk':(SOURCES/'Trafikk-regular.ttf',56,1.,.015,'Trafikk Regular (Aitor, Norwegian specification interpretation)'),
 'l1':(SOURCES/'Typoroute2002-L1_100.ttf',96,1.,0.,'Typoroute 2002 L1 100 (Gwenaël Jouvin, French L1 digitization)'),
}

def l1_positioning(tt,chars,cap,size):
 """Export this pinned font's GPOS pairs and one GSUB hyphen context offline."""
 cmap=tt.getBestCmap();names={ch:cmap[ord(ch)]for ch in chars};factor=size/(tt['head'].unitsPerEm*cap)
 gpos=tt['GPOS'].table
 indices=next(f.Feature.LookupListIndex for f in gpos.FeatureList.FeatureRecord if f.FeatureTag=='kern')
 pairs={}
 for a,ga in names.items():
  for b,gb in names.items():
   amount=0
   for index in indices:
    lookup=gpos.LookupList.Lookup[index]
    assert lookup.LookupType==2
    for sub in lookup.SubTable:
     assert sub.Format==2 and sub.ValueFormat1==4 and sub.ValueFormat2==0
     if ga not in sub.Coverage.glyphs:continue
     value=sub.Class1Record[sub.ClassDef1.classDefs.get(ga,0)].Class2Record[sub.ClassDef2.classDefs.get(gb,0)].Value1
     amount+=getattr(value,'XAdvance',0) if value is not None else 0
     break
   if amount:pairs[a+b]=amount*factor
 gsub=tt['GSUB'].table
 context_index=next(f.Feature.LookupListIndex[0]for f in gsub.FeatureList.FeatureRecord if f.FeatureTag=='calt')
 context=gsub.LookupList.Lookup[context_index].SubTable[0]
 assert context.Format==2 and context.Coverage.glyphs==['hyphen']
 rule=context.ChainSubClassSet[1].ChainSubClassRule[0]
 assert rule.Backtrack==[1] and rule.LookAhead==[1] and rule.InputGlyphCount==1
 subst=gsub.LookupList.Lookup[rule.SubstLookupRecord[0].LookupListIndex].SubTable[0].mapping
 alt=next(ch for ch,gn in names.items()if gn==subst['hyphen'])
 return {'kerning':pairs,'aliases':{"'":'’'},'hyphen_context':{'glyph':alt,
  'left':''.join(ch for ch,gn in names.items()if context.BacktrackClassDef.classDefs.get(gn)==1),
  'right':''.join(ch for ch,gn in names.items()if context.LookAheadClassDef.classDefs.get(gn)==1)}}


def build():
 OUT.mkdir(parents=True,exist_ok=True)
 fonts={};images=[];allchars=set()
 for a,b in RANGES:allchars.update(chr(i)for i in range(a,b))
 for key,(path,size,scale,spacing,label)in SPECS.items():
  font=ImageFont.truetype(str(path),size);tt=TTFont(path);cmap=tt.getBestCmap();cap=font.getbbox('H',anchor='ls')[3]-font.getbbox('H',anchor='ls')[1]
  chars=sorted(chr(cp)for cp in cmap)if key=='l1'else sorted(c for c in allchars if ord(c)in cmap and (key in ('legacy','fallback')or ord(c)<592))
  glyphs={};fonts[key]={'font':label,'horizontal_scale':scale,'letter_spacing':spacing,'glyphs':glyphs,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
  if key=='l1':fonts[key].update(l1_positioning(tt,chars,cap,size))
  for ch in chars:
   l,t,r,b=font.getbbox(ch,anchor='ls');w,h=r-l,b-t
   g={'advance':font.getlength(ch)/cap,'bounds':[l/cap,-b/cap,r/cap,-t/cap],'uv':[0,0,0,0]};glyphs[ch]=g
   if w>0 and h>0:
    im=Image.new('L',(w,h));ImageDraw.Draw(im).text((-l,-t),ch,font=font,fill=255,anchor='ls')
    if key=='l1':
     # Typoroute has deliberate side bearings. Pillow's getbbox includes the
     # advance box; trim to visible ink while retaining the original advance.
     bounds=im.getbbox()
     if bounds is not None:
      a,bb,c,d=bounds;im=im.crop(bounds)
      g['bounds']=[(l+a)/cap,-(t+d)/cap,(l+c)/cap,-(t+bb)/cap]
    images.append((key,ch,im))
 # One texture pair shared by every typeface, independent of settlement count.
 width=4096;x=y=rowh=0;positions=[];pad=3
 for key,ch,im in sorted(images,key=lambda v:(-v[2].height,v[0],v[1])):
  w,h=im.size
  if x+w+pad*2>width:x=0;y+=rowh;rowh=0
  positions.append((key,ch,im,x+pad,y+pad));x+=w+pad*2;rowh=max(rowh,h+pad*2)
 height=1<<(y+rowh-1).bit_length()
 if height>4096:raise ValueError('Font atlas exceeds the 4096x4096 budget')
 alpha=Image.new('L',(width,height))
 for key,ch,im,x,y in positions:
  alpha.paste(im,(x,y));fonts[key]['glyphs'][ch]['uv']=[x/width,1-(y+im.height)/height,(x+im.width)/width,1-y/height]
 # (the letters' colour is the material's baseColorFactor, black or white: the shader takes only a colour map's rgb, never
 # its alpha, so the two 4096 px colour layers of uniform black / white were 21 MB of DDS for nothing, 2026-11-13)
 alpha.save(OUT/'sign_font_opacity.data.png')
 meta={**fonts['legacy'],'format':2,'fonts':fonts,'atlas_size':[width,height],'generator':'tools/build_town_fonts.py'}
 (ROOT/'server/data/sign_font.json').write_text(json.dumps(meta,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')
 # Keep existing material names/indices so unrelated signs retain their contract.
 materials=json.loads((OUT/'main.materials.json').read_text())
 for key in ('glyph','glyph_white'):
  m=materials['bne_sign_'+key];m['Stages'][0].pop('baseColorMap',None)
  m['Stages'][0]['baseColorFactor']=[1,1,1,1]if key=='glyph_white'else[0,0,0,1]
  m['Stages'][0]['opacityMap']=VFS+'/sign_font_opacity.data.png'
 (OUT/'main.materials.json').write_text(json.dumps(materials,indent=2)+'\n')
 (OUT/'TOWN-FONTS-NOTICE.md').write_text((SOURCES/'NOTICE.md').read_text(),encoding='utf-8')
 (OUT/'Typoroute-LICENSE.md').write_bytes((SOURCES/'Typoroute-LICENSE.md').read_bytes())
 print(len(fonts),'typefaces;',len(images),'glyphs;',alpha.size,'used height',y+rowh)

if __name__=='__main__':build()
