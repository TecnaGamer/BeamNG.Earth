"""Batch S-1 dimensions and independently checked deliverable counts."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cis'
REND=ROOT/'assets/cis_renders'
P='bne_cis_'
TASKS=['A0','A1','A2','A3','A4','A5','M1','M2','M3','M4','M5','M6','F1','A6','A7','A8','A9','M7','M8','T1','T2','T3','T4','T5','T6']
KITS={
'A0':dict(kit='common',bay=3.2,storey=2.7,roles=dict(plinth=4,parapet=3,roof=1,door_steel=3,slab_edge=1,metal=2),sets=['common']),
'A1':dict(kit='khrush_panel',bay=3.2,storey=2.7,roles=dict(bay=10,bay_balcony=3,bay_balcony_far=5,stair=2,entrance=2,ground=4,end=4),sets=['tile_beige','tile_blue'],size=[64,11.5,5],entrances=[8,24,40,56]),
'A2':dict(kit='khrush_brick',bay=3.,storey=2.7,roles=dict(bay=8,bay_balcony=2,bay_balcony_far=4,stair=2,entrance=2,ground=3,end=3),sets=['silicate','red'],size=[56,11,5],entrances=[10,28,46]),
'A3':dict(kit='panel9',bay=3.6,storey=2.8,roles=dict(bay=10,bay_balcony=3,bay_balcony_far=6,stair=2,entrance=2,ground=4,tech=3,end=4),sets=['aggregate','painted'],size=[96,12.6,9],entrances=[12,36,60,84],tech=1.8),
'A4':dict(kit='panel16',bay=3.,storey=2.8,roles=dict(bay=10,bay_balcony=3,bay_balcony_far=6,stair=2,entrance=2,ground=4,tech=2,end=3),sets=['panel'],size=[54,13.2,16],entrances=[14,40],tech=1.6),
'A5':dict(kit='new_mono',bay=3.3,storey=3.,roles=dict(bay=8,bay_balcony=4,bay_balcony_far=4,stair=2,entrance=2,ground=6,parapet=2,end=4),sets=['vent','plaster'],size=[40,30,22],entrances=[10,30])}

# Counts are totals across the two independently selectable colour sets.
KITS.update({
'A6':dict(kit='stalinka',bay=3.4,storey=3.4,atlas=4096,per_set=True,roles=dict(bay=12,bay_balcony=4,bay_balcony_far=4,ground=8,entrance=4,stair=2,cornice=4,end=4),sets=['ochre','grey'],size=[70,15,5],entrances=[17,53],cornice=1.2),
'A7':dict(kit='brick_tower',bay=3.,storey=2.8,atlas=4096,per_set=True,roles=dict(bay=16,bay_balcony=6,bay_balcony_far=8,stair=4,entrance=4,ground=6,tech=4,end=6),sets=['silicate','red'],size=[22,22,12],entrances=[11],tech=1.6,secondary=[48,13,9],secondary_entrances=[8,24,40]),
'A8':dict(kit='new_low',bay=3.3,storey=3.,per_set=True,modern=True,roles=dict(bay=8,bay_balcony=4,bay_balcony_far=4,stair=2,entrance=2,ground=6,parapet=4,attic=4,end=4),sets=['clinker','plaster'],size=[36,16,5],entrances=[9,27],parapet=.9,attic=2.4),
'A9':dict(kit='new_panel',bay=3.,storey=2.8,per_set=True,modern=True,roles=dict(bay=8,bay_balcony=6,bay_balcony_far=6,stair=4,entrance=2,ground=6,tech=2,parapet=2,end=6),sets=['mosaic','pastel'],size=[60,14,20],entrances=[10,30,50],tech=1.6,parapet=1.)})
TILES={
'T1':dict(name='roof_slate',tile_m=[1.75,1.0]),
'T2':dict(name='roof_metaltile',tile_m=[1.19,1.1]),
'T3':dict(name='roof_profiled',tile_m=[1.,1.]),
'T4':dict(name='wall_silicate',tile_m=[1.,1.]),
'T5':dict(name='wall_redbrick',tile_m=[1.,1.]),
'T6':dict(name='wall_koroied',tile_m=[1.,1.])}

def cell_specs(task):
    k=KITS[task]; out=[]
    for role,n in k['roles'].items():
        for i in range(n):
            w,h=k['bay'],k['storey']
            if task=='A0':
                w,h={'plinth':(3.2,1.),'parapet':(3.2,.6),'roof':(2.,2.),'door_steel':(1.2,2.3),'slab_edge':(3.2,.25),'metal':(1.,1.)}[role]
            elif role=='tech':h=k['tech']
            elif task=='A5' and role in ('entrance','ground'):h=4.5
            elif role=='parapet':h=k.get('parapet',1.2)
            elif role=='cornice':h=1.2
            elif role=='attic':h=k['attic']
            si=(i//(n//2)) if task=='A5' or k.get('per_set') else i%len(k['sets'])
            tags=[k['sets'][si]]
            local=i%(n//2) if k.get('per_set') else i
            if role in ('bay','ground'):
                tags.append('pvc' if task=='A5' or k.get('modern') or local%10 in (4,5,6,7) else 'old')
                if task!='A5' and not k.get('modern') and local%10>=8:tags.append('insulated')
                if role=='ground' and i<2:tags.append('bars')
            if task=='A1' and role in ('bay','end') and i in (3,7):tags.append('bare')
            if role=='bay_balcony_far':tags.append('far')
            out.append(dict(id=f'{role}_{i+1:02}',role=role,weight=1/n,size_m=[w,h],lit=role in ('bay','bay_balcony','bay_balcony_far','stair','ground','entrance','door_steel'),tags=tags,variant=i))
    return out
