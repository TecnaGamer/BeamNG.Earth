"""Render the actual runtime faces as review sheets, without BeamNG or a server."""
import argparse
import json
from pathlib import Path
import sys

from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import highwayroutes
import routefaces

SAMPLES={'AL':'17','AK':'98','AZ':'87','AR':'367','CA':'49','CO':'135','CT':'15','DE':'1',
 'FL':'A1A','GA':'100','HI':'61','ID':'43','IL':'390','IN':'165','IA':'150','KS':'6',
 'KY':'3487','LA':'1245','ME':'100','MD':'45','MA':'140','MI':'185','MN':'61','MS':'347',
 'MO':'174','MT':'1','NE':'92','NV':'318','NH':'3A','NJ':'133','NM':'65','NY':'77',
 'NC':'55','ND':'13','OH':'2','OK':'3','OR':'140','PA':'39','RI':'12','SC':'462',
 'SD':'63','TN':'125','TX':'273','UT':'210','VT':'100','VA':'32','WA':'20','WV':'93','WI':'22','WY':'210'}


def sheet(routes,path,columns=5):
    result=Image.new('RGB',(columns*300,((len(routes)+columns-1)//columns)*290),'#c8c8c8')
    draw=ImageDraw.Draw(result)
    for i,(network,number,modifier) in enumerate(routes):
        key=highwayroutes.template_for(network,number,modifier)
        template=highwayroutes.registry()['templates'].get(key,{})
        picture=Image.frombytes('RGB',(512,512),routefaces.raster(network,number,modifier)).convert('RGBA')
        alpha=Image.new('L',(512,512));pen=ImageDraw.Draw(alpha)
        profile=routefaces.contour(key)
        if profile is not None:
            pen.polygon([((x+.5)*512,(1-z)*512) for x,z in profile[0]],fill=255)
        else:pen.rounded_rectangle((0,0,511,511),radius=30,fill=255)
        picture.putalpha(alpha)
        aspect=template.get('aspect',1.)
        picture=picture.resize((round(220*aspect),220))
        picture.thumbnail((280,245),Image.Resampling.LANCZOS)
        x,y=(i%columns)*300,(i//columns)*290
        result.paste(picture,(x+(300-picture.width)//2,y+30+(245-picture.height)//2),picture)
        draw.text((x+12,y+8),network+' '+number+(' / '+modifier if modifier else ''),fill='black')
    result.save(path)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('output',type=Path)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    routes=[('US:TN:PRIMARY' if state=='TN' else 'US:'+state,ref,'') for state,ref in SAMPLES.items()]
    sheet(routes,args.output/'all-50-states.png')
    for page,start in enumerate(range(0,len(routes),20),1):sheet(routes[start:start+20],args.output/f'states-{page}.png')
    variants=[('US:TX:FM','1234',''),('US:TX:RM','2381',''),('US:TX:LOOP','12',''),('US:TX:SPUR','408',''),
              ('US:TX:BELTWAY','8',''),('US:TN:SECONDARY','125',''),('US:MT:SECONDARY','243',''),
              ('US:MO:SUPPLEMENTAL','JJ',''),('US:NE:LINK','26A',''),('US:NE:SPUR','26D',''),
              ('US:I','80','BUSINESS LOOP'),('US:I','196','BUSINESS SPUR')]
    sheet(variants,args.output/'special-networks.png',columns=4)
    art=[]
    registry=highwayroutes.registry()
    for key,template in sorted(registry['templates'].items()):
        if not template.get('image'):continue
        net='ART:'+key.upper()
        registry['networks'][net]=dict(template=key,kind='state')
        modifier=next(iter(template.get('modifiers',[])),'')
        number='210' if key.endswith('_wide') else '42'
        if key=='mo_supplemental':number='JJ'
        art.append((net,number,modifier))
    for page,start in enumerate(range(0,len(art),20),1):sheet(art[start:start+20],args.output/f'templates-{page}.png')
