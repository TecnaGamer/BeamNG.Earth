"""Build one shared font atlas for dynamic sign labels, independent of town count.

DejaVu Sans Bold (see tools/fonts/DejaVu-LICENSE.txt), condensed at layout time.
The typeface is a prototype substitute, not the official French L1 alphabet.
"""
import json
from pathlib import Path
import uuid
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/regional_signs'
FONT=Path(__file__).with_name('fonts')/'DejaVuSans-Bold.ttf'
VFS='/levels/beamng_earth/art/shapes/beamngEarth/regional_signs'

def build():
    OUT.mkdir(parents=True,exist_ok=True)
    font=ImageFont.truetype(str(FONT),44)
    # Preserve case, Greek and Cyrillic, rather than replacing local names by '?'.
    # DejaVu also includes Armenian/Georgian lower-case lettering.
    cmap=TTFont(FONT).getBestCmap()
    chars=sorted(set(chr(i) for a,b in ((32,127),(160,592),(880,1328),(1328,1424),(4256,4352),(7680,8192))
                     for i in range(a,b) if i in cmap))
    cell,cols=64,32;rows=(len(chars)+cols-1)//cols
    image=Image.new('RGBA',(1<<(cols*cell-1).bit_length(),1<<(rows*cell-1).bit_length()),(0,0,0,0));draw=ImageDraw.Draw(image);glyphs={}
    cap=font.getbbox('H')[3]-font.getbbox('H')[1]
    for i,ch in enumerate(chars):
        x,y=(i%cols)*cell+5,(i//cols)*cell+5
        l,t,r,b=font.getbbox(ch,anchor='ls');w,h=r-l,b-t
        draw.text((x-l,y-t),ch,font=font,fill=(0,0,0,255),anchor='ls')
        glyphs[ch]={'advance':font.getlength(ch)/cap,'bounds':[l/cap,-b/cap,r/cap,-t/cap],
                    'uv':[x/image.width,1-(y+h)/image.height,(x+w)/image.width,1-y/image.height]}
    image.save(OUT/'sign_font.png')
    # PBR v1.5 reads opacity from its own map; do not rely on base-colour alpha.
    image.getchannel('A').save(OUT/'sign_font_opacity.data.png')
    white=Image.new('RGBA',image.size,(255,255,255,255))
    white.putalpha(image.getchannel('A'))
    white.save(OUT/'sign_font_white.png')
    meta={'font':'DejaVu Sans Bold','license':'Bitstream Vera / DejaVu public-domain changes',
          'glyphs':glyphs,'horizontal_scale':.78,'letter_spacing':.08}
    (ROOT/'server/data/sign_font.json').write_bytes((json.dumps(meta,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode())
    colours={'white':[.93,.93,.89,1],'red':[.76,.015,.02,1],'black':[.01,.012,.013,1],
             'metal':[.46,.49,.5,1],'yellow':[.95,.69,.05,1],'glyph':[1,1,1,1],
             'blue':[.015,.10,.40,1],'green':[.025,.23,.08,1],'glyph_white':[1,1,1,1]}
    materials={}
    for key,colour in colours.items():
        name='bne_sign_'+key
        stage={'baseColorFactor':colour,'metallicFactor':.65 if key=='metal' else 0.,'roughnessFactor':.45}
        m={'name':name,'mapTo':name,'class':'Material','version':1.5,'Stages':[stage],
           'persistentId':str(uuid.uuid5(uuid.NAMESPACE_URL,'beamng-earth:regional-signs:'+name)),
           'doubleSided':False,'translucent':False}
        if key in ('white','red','yellow'):stage['retroreflectivity']=.8
        if key in ('glyph','glyph_white'):
            stage['baseColorMap']=VFS+('/sign_font_white.png' if key=='glyph_white' else '/sign_font.png')
            stage['opacityMap']=VFS+'/sign_font_opacity.data.png'
            m.update(alphaTest=True,alphaRef=80,castShadows=False)
        materials[name]=m
    (OUT/'main.materials.json').write_bytes((json.dumps(materials,indent=2)+'\n').encode())
    (OUT/'FONT-LICENSE.txt').write_bytes(FONT.with_name('DejaVu-LICENSE.txt').read_bytes())
    print(len(glyphs),'glyphs',image.size)

if __name__=='__main__':build()
