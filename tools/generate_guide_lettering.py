"""Bake the advance guide-sign atlas from the bundled MIT-licensed Roadgeek font.

Runtime uses the shared atlas, never font lookup or per-sign image generation.
Legacy exit/mile-marker lettering remains separate.
"""
from pathlib import Path
import json
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside'

def main():
    font=ImageFont.truetype(str(ROOT/'tools/fonts/RG2014EEM.ttf'),128)
    image=Image.new('L',(2048,2048),0);draw=ImageDraw.Draw(image);glyphs={}
    for i,char in enumerate(map(chr,range(32,127))):
        l,t,r,b=font.getbbox(char,anchor='ls')
        if r-l>116 or b-t>116:raise ValueError(f'Glyph exceeds its cell: {char}')
        x,y=(i%16)*128+6,(i//16)*128+6
        draw.text((x-l,y-t),char,font=font,fill=255,anchor='ls')
        glyphs[char]=dict(advance=round(font.getlength(char),3),bbox=[l,t,r,b],
            uv=[x/2048,1-(y+b-t)/2048,(x+r-l)/2048,1-y/2048])
    OUT.mkdir(parents=True,exist_ok=True)
    image.save(OUT/'guide_letters_o.data.png',optimize=True)
    metrics=dict(font='Roadgeek 2014 Series EEM',version='3.1',license='MIT',
        source='https://github.com/sammdot/roadgeek-fonts/releases/tag/3.1',
        capHeight=-font.getbbox('H',anchor='ls')[1],glyphs=glyphs)
    (ROOT/'server/guide_glyphs.json').write_text(json.dumps(metrics,separators=(',',':'))+'\n',encoding='utf-8')
    path=OUT/'exit.materials.json';materials=json.loads(path.read_text())
    prefix='/levels/beamng_earth/art/shapes/beamngEarth/roadside/'
    for name,color,letter in (
        ('bne_guide_red',[.65,.018,.03,1],False),
        ('bne_guide_black',[.008,.009,.01,1],False),
        ('bne_guide_letters',[.97,.98,.94,1],True),
        ('bne_guide_letters_black',[.008,.009,.01,1],True)):
        stage=dict(baseColorFactor=color,roughnessFactor=.65,metallicFactor=0.)
        extra={}
        if letter:
            stage.update(opacityMap=prefix+'guide_letters_o.data.png',opacityFactor=1.)
            extra=dict(alphaTest=True,alphaRef=64,translucent=False)
        materials[name]=dict(name=name,mapTo=name,**{'class':'Material'},version=1.5,
            Stages=[stage,{},{},{}],castShadows=False,**extra)
    path.write_text(json.dumps(materials,indent=2)+'\n',encoding='utf-8')
    print('Baked',len(glyphs),'guide glyphs; cap height',metrics['capHeight'])

if __name__=='__main__':main()
