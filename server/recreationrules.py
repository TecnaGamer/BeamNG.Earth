"""Posted recreation destinations and agency-aware US entrance-board labels."""
import re

PARK_DESTINATION = re.compile(
    r"\b(?:state\s+park|national\s+park|nat(?:ional|[’']?l|l)?\.?\s+park|"
    r"national\s+(?:forest|grassland|monument|recreation\s+area|seashore|lakeshore)|"
    r"state\s+recreation\s+area)\b", re.I)


def destination(value):
    """A posted park destination, never a town merely containing the word Park."""
    value=' '.join(str(value).split())
    if re.search(r'\b(?:road|rd|drive|dr|street|st|avenue|ave|highway|hwy)\.?$',value,re.I):return False
    match=PARK_DESTINATION.search(value)
    return bool(match and value[:match.start()].strip())


def entrance(name, tags, state=''):
    """Return a display family only when the mapped identity supports it."""
    if not state:
        return None
    name = ' '.join(str(name).split())
    operator = str(tags.get('operator', ''))
    designation = str(tags.get('designation', ''))
    evidence = re.sub(r'[_-]+',' ',' '.join((name, designation)).lower())
    usfs = bool(re.search(r'forest service|\busfs\b', operator, re.I))
    nps = bool(re.search(r'national park service|\bnps\b', operator, re.I))
    if 'national grassland' in evidence:
        category, agency, emblem = 'NATIONAL GRASSLAND', 'US DEPARTMENT OF AGRICULTURE', 'usfs'
    elif 'national forest' in evidence:
        category, agency, emblem = 'NATIONAL FOREST', 'US DEPARTMENT OF AGRICULTURE', 'usfs'
    elif 'national park' in evidence or (tags.get('boundary') == 'national_park' and nps):
        category, agency, emblem = 'NATIONAL PARK', 'US DEPARTMENT OF THE INTERIOR', 'nps'
    elif 'state park' in evidence:
        category, agency, emblem = 'STATE PARK', operator or f'{state} STATE PARKS', None
    elif usfs:
        category, agency, emblem = 'RECREATION AREA', 'US DEPARTMENT OF AGRICULTURE', 'usfs'
    elif nps:
        category, agency, emblem = 'RECREATION AREA', 'US DEPARTMENT OF THE INTERIOR', 'nps'
    else:
        return None
    short = re.sub(r'\s+(?:National\s+(?:Park|Forest|Grassland)|State\s+Park|Recreation\s+Area)\s*$', '', name, flags=re.I)
    return dict(name=short or name, category=category,
                forest='FOREST SERVICE' if emblem == 'usfs' else ('NATIONAL PARK SERVICE' if emblem == 'nps' else f'{state} STATE PARKS'),
                designation='Welcome', agency=agency, emblem=emblem,
                width_m=4.8, height_m=3.1)
