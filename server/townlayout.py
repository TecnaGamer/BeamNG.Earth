"""Measured town-board composition in metres, not a stretched generic rectangle.

Dimensions are representative scenery presets; documented national drawings
anchor the families. No municipal name, translation, crest or speed is invented.
"""
from dataclasses import dataclass,replace
import re

VERSION='town-layout-2026-11-v3-de-frame'

@dataclass(frozen=True)
class Profile:
    font:str='din'
    min_width:float=1.05
    max_width:float=3.6
    height:float=.60
    cap:float=.23
    pad_x:float=.13
    pad_y:float=.10
    outer:float=.025
    border:float=.018
    radius:float=.055
    max_ratio:float=4.5
    letter_scale:float=1.
    tracking:float|None=None
    bottom:float=1.45
    admin_scale:float=.53
    admin_gap:float=.085
    family:str='continental-name'

FR=Profile('l1',.80,4.6,.43,.21,.12,.095,.018,.032,.045,8.,family='FR-EB10')
DE_LEGACY=Profile('din',1.05,2.8,.75,.235,.14,.11,.022,.018,.065,2.50,family='DE-310')
DE=Profile('din',.90,.90,.60,.16,.065,.065,.012,.010,.035,1.50,family='DE-310')
AT=Profile('din',.96,2.8,.63,.205,.14,.12,.018,.025,.025,3.25,family='AT-17a')
NL=Profile('transport',1.15,3.8,.62,.235,.13,.11,.018,.018,.045,5.,family='NL-H1')
BE=Profile('condensed',1.00,2.7,1.05,.265,.11,.09,.020,.020,.065,3.,family='BE-F1')
PL=Profile('wide',1.,4.3,.53,.21,.12,.09,.020,.018,.025,8.,family='PL-E17a')
RO=Profile('condensed',.88,3.8,.60,.30,.11,.11,.017,.017,.040,5.,family='RO-locality')
CZ=Profile('din',1.,4.0,.55,.25,.12,.10,.017,.017,.025,7.,family='CZ-IS12a')
BALKAN=Profile('din',1.10,4.0,.64,.26,.13,.12,.020,.018,.045,6.,family='Balkan-yellow-name')
NORDIC=Profile('din',1.00,3.8,.56,.225,.13,.11,.020,.018,.025,7.,family='Nordic-blue-name')
SKYLINE=Profile('din',1.10,3.1,1.02,.24,.12,.09,.020,.018,.035,4.,family='name-and-built-up-symbol')
UK=Profile('transport',.9,4.2,.52,.22,.11,.09,.018,.017,.035,7.,family='UK-boundary-name')
IE=Profile('transport',1.,4.2,.60,.23,.12,.10,.020,.018,.035,7.,family='IE-name')
# Every existing geographic selector has an explicit layout entry. Small states
# share their documented/inherited visual family, not a fabricated unique font.
PROFILES={}
for codes,p in [
 ('FR MC',FR),('DE',DE),('AT',AT),('NL',NL),('BE',BE),('PL',PL),('RO',RO),
 ('CZ SK',CZ),('BA HR ME MK RS SI XK',BALKAN),('AX FI',NORDIC),
 ('NO',replace(NORDIC,font='trafikk',family='NO-727.1')),
 ('SE',replace(NORDIC,font='tratex_white',family='SE-F10')),
 ('DK EE FO',SKYLINE),('IS',replace(SKYLINE,font='transport',family='IS-built-up')),
 ('GB GG GI IM JE',UK),('IE',IE),
 ('CH LI',replace(CZ,font='din',cap=.235,family='CH-4.27/4.29')),
 ('IT SM VA',replace(CZ,font='din',cap=.24,family='IT-centro-abitato')),
 ('ES',replace(CZ,font='ccrige',cap=.215,family='ES-S500')),
 ('PT',replace(UK,font='transport',family='PT-N1a')),
 ('HU',replace(CZ,cap=.235,family='HU-lakott-terulet')),
 ('LU',replace(DE_LEGACY,height=.66,max_width=3.8,max_ratio=5.,family='LU-E9')),
 ('AD',replace(FR,font='din',tracking=.05,family='AD-locality')),
 ('AL AZ',replace(NORDIC,family='blue-locality')),
 ('BG',replace(CZ,family='BG-locality')),
 ('BY MD RU UA',replace(CZ,font='fallback',cap=.235,family='Cyrillic-locality')),
 ('LT LV',replace(NORDIC,font='din',family='Baltic-blue-place-name')),
 ('GR',replace(CZ,font='fallback',family='GR-locality')),
 ('CY MT',replace(UK,font='transport',family='Mediterranean-name')),
 ('TR',replace(NORDIC,font='din',cap=.24,family='TR-B9')),
 ('GE AM',replace(RO,font='fallback',cap=.245,family='Caucasus-locality')),
]:
 for code in codes.split():PROFILES[code]=p

@dataclass(frozen=True)
class Line:
    text:str
    font:str
    cap:float
    centre:float
    scale:float=1.
    tracking:float|None=None
    role:str='name'

@dataclass(frozen=True)
class Layout:
    country:str
    profile:Profile
    width:float
    height:float
    centre:float
    lines:tuple
    skyline:bool=False
    header:float=0.
    @property
    def top(self):return self.centre+self.height/2
    @property
    def bottom(self):return self.centre-self.height/2


def _wrap(text,width,measure):
    """Preserve every character; prefer real spaces/hyphens to splitting words."""
    if measure(text)<=width:return [text]
    parts=re.findall(r'[^ -]+(?:[- ]+|$)',text);lines=[];line=''
    for part in parts:
        trial=line+part
        if line and measure(trial.rstrip())>width:lines.append(line.rstrip());line=''
        # Extremely long unbroken place names get a bounded multi-line board.
        for ch in part:
            if line and measure(line+ch)>width:lines.append(line.rstrip());line=''
            line+=ch
    if line:lines.append(line.rstrip())
    return lines or ['']


def compose(name,style,measure,details=()):
    country=getattr(style,'country','')or'FR'
    p=PROFILES.get(country,FR)
    if country=='DE':
        return _compose_de(name,p,measure,details)
    skyline=bool(getattr(style,'skyline',False))
    def width(s,key=p.font):return measure(s,key,p.letter_scale,p.tracking)[0]
    cap=p.cap
    # Natural glyph widths first; modest overall size reduction precedes wrapping.
    natural=width(name)*cap+2*p.pad_x
    if natural>p.max_width:
        cap=max(p.cap*.78,(p.max_width-2*p.pad_x)/max(width(name),.01))
    rows=_wrap(name,(p.max_width-2*p.pad_x)/cap,width)
    secondary=[str(x).strip()for x in details if isinstance(x,str) and x.strip()][:2]
    name_w=max((width(x)*cap for x in rows),default=0)
    secondary_w=max((measure(x,p.font,p.letter_scale,None)[0]*cap*p.admin_scale for x in secondary),default=0)
    w=min(p.max_width,max(p.min_width,name_w+2*p.pad_x,secondary_w+2*p.pad_x))
    name_height=sum(max(cap,measure(x,p.font,p.letter_scale,p.tracking)[1]*cap) for x in rows)
    name_height+=max(0,len(rows)-1)*cap*.32
    admin_h=cap*p.admin_scale
    text_height=name_height+(len(secondary)*(admin_h+p.admin_gap)if secondary else 0)
    reserve=.46 if skyline else 0.
    header=.34 if getattr(style,'header_speed',None) is not None else 0.
    h=max(p.height+header,text_height+2*p.pad_y+reserve+header,w/p.max_ratio)
    # Keep the board's lower edge above the ground while adapting its height.
    centre=p.bottom+h/2
    area_centre=centre+reserve/2-header/2
    cursor=area_centre+text_height/2
    lines=[]
    for row in rows:
        rh=max(cap,measure(row,p.font,p.letter_scale,p.tracking)[1]*cap)
        lines.append(Line(row,p.font,cap,cursor-rh/2,p.letter_scale,p.tracking))
        cursor-=rh+cap*.32
    if secondary:
        cursor+=cap*.32
        for row in secondary:
            cursor-=p.admin_gap
            # Size a supplied administrative line to fit, without squeezing it.
            ah=min(admin_h,(w-2*p.pad_x)/max(measure(row,p.font,p.letter_scale,None)[0],.01))
            lines.append(Line(row,p.font,ah,cursor-ah/2,p.letter_scale,None,'admin'))
            cursor-=admin_h
    return Layout(country,p,w,h,centre,tuple(lines),skyline,header)


def _compose_de(name,p,measure,details):
    """900 x 600 mm face: fit lettering, never stretch the plate or glyphs.

    Administrative labels are displayed only when explicitly supplied. This
    is a scenery layout, not an implementation of the full DIN lettering rules.
    """
    secondary=[s.strip() for s in details if isinstance(s,str) and s.strip()][:2]
    available=p.max_width-2*p.pad_x
    # Keep ordinary names on one line before wrapping genuinely long names.
    natural=measure(name,p.font,1.,None)[0]
    cap=max(.085,min(p.cap,available*.995/max(natural,.01)))
    for _ in range(100):
        rows=_wrap(name,available/cap,lambda s:measure(s,p.font,1.,None)[0])
        main_h=[max(1.,measure(s,p.font,1.,None)[1])*cap for s in rows]
        admin_caps=[min(cap*.53,available/max(measure(s,p.font,1.,None)[0],.01)) for s in secondary]
        admin_h=[max(1.,measure(s,p.font,1.,None)[1])*c for s,c in zip(secondary,admin_caps)]
        total=sum(main_h)+max(0,len(rows)-1)*cap*.22+sum(admin_h)+len(secondary)*.025
        if total<=p.height-2*p.pad_y:break
        cap*=.94
    centre=p.bottom+p.height/2;cursor=centre+total/2;lines=[]
    for row,rh in zip(rows,main_h):
        lines.append(Line(row,p.font,cap,cursor-rh/2));cursor-=rh+cap*.22
    cursor+=cap*.22
    for row,ah,rh in zip(secondary,admin_caps,admin_h):
        cursor-=.025
        lines.append(Line(row,p.font,ah,cursor-rh/2,role='admin'));cursor-=rh
    return Layout('DE',p,.90,.60,centre,tuple(lines))
