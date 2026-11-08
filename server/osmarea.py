"""Complete OSM XML outer-ring reconstruction for development fixtures and diagnostics.

Production geometry arrives as complete GeoParquet polygons through mapdata.
"""
import math
import xml.etree.ElementTree as ET


def outer_rings(source, kind, identifier):
    """Reconstruct complete outer rings by node identity; fail on missing/open members."""
    root = ET.parse(source).getroot()
    nodes = {n.attrib['id']: (float(n.attrib['lon']), float(n.attrib['lat'])) for n in root.findall('node')}
    ways = {w.attrib['id']: [n.attrib['ref'] for n in w.findall('nd')] for w in root.findall('way')}
    if kind == 'way':
        parts = [ways[str(identifier)][:]]
    elif kind == 'relation':
        relation = next(r for r in root.findall('relation') if r.attrib['id'] == str(identifier))
        parts = []
        for member in relation.findall('member'):
            if member.attrib.get('role', '') not in ('', 'outer'):
                continue
            if member.attrib['type'] != 'way':
                raise ValueError('unsupported outer member')
            parts.append(ways[member.attrib['ref']][:])
    else:
        raise ValueError('expected way or relation')
    rings = []
    while parts:
        ring = parts.pop(0)
        while ring[-1] != ring[0]:
            for i, part in enumerate(parts):
                if part[0] == ring[-1] or part[-1] == ring[-1]:
                    if part[-1] == ring[-1]:
                        part = part[::-1]
                    ring.extend(part[1:])
                    parts.pop(i)
                    break
            else:
                raise ValueError('incomplete outer ring')
        if len(set(ring)) < 3:
            raise ValueError('degenerate ring')
        rings.append([nodes[n] for n in ring])
    if not rings:
        raise ValueError('no outer rings')
    return rings


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742000 * math.asin(min(1., math.sqrt(h)))

