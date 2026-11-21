"""Measure stock foliage/furniture for path clearance; no game assets are copied.

python tools/survey_path_footprints.py PATH/TO/BeamNG.drive
Prints CATALOG for server/pathfootprints.py. Bounds include all visual LODs and
node transforms, measured around the item's origin (not its bounding-box centre).
"""
import math
import re
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

GROUPS = ('shrub', 'desert_shrub', 'creosote', 'rock', 'desert_rock', 'vine',
          'tuft', 'fern', 'hydrant_eu', 'postbox_eu', 'postbox_us', 'bikestand',
          'recyclebin', 'picnictable', 'picnicgrill', 'bench_metal')
NS = {'c': 'http://www.collada.org/2005/11/COLLADASchema'}


def catalog(text, group):
    text = re.sub(r'--[^\n]*', '', text)
    match = re.search(r'(?:M\.CATALOG\.|\b)' + group + r'\s*=\s*\{', text)
    if match is None:
        return []
    start, end, depth = match.end(), match.end(), 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    aliases = dict(re.findall(r"local (\w+) = '([^']+)'", text))
    return [(aliases.get(base, '') + path, float(height)) for base, path, height in
            re.findall(r"\{\s*(?:(\w+)\s*\.\.\s*)?'([^']+)'\s*,\s*([\d.]+)\s*\}", text[start:end])]


def radius(data):
    root = ET.fromstring(data)
    geometries = {}
    for g in root.findall('.//c:geometry', NS):
        inp = g.find('c:mesh/c:vertices/c:input[@semantic="POSITION"]', NS)
        if inp is None:
            continue
        source = g.find('c:mesh/c:source[@id="' + inp.attrib['source'][1:] + '"]', NS)
        arr = np.fromstring(source.find('c:float_array', NS).text, sep=' ')
        stride = int(source.find('c:technique_common/c:accessor', NS).attrib.get('stride', 3))
        geometries[g.attrib['id']] = arr.reshape(-1, stride)[:, :3]
    points = []

    def node(n, parent):
        local = np.eye(4)
        for child in n:
            tag = child.tag.rsplit('}', 1)[-1]
            if tag not in ('matrix', 'translate', 'scale', 'rotate'):
                continue
            v = np.fromstring(child.text, sep=' ')
            t = np.eye(4)
            if tag == 'matrix':
                t = v.reshape(4, 4)
            elif tag == 'translate':
                t[:3, 3] = v
            elif tag == 'scale':
                t[np.arange(3), np.arange(3)] = v
            else:
                u = v[:3] / np.linalg.norm(v[:3])
                c, s = math.cos(math.radians(v[3])), math.sin(math.radians(v[3]))
                x, y, z = u
                k = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
                t[:3, :3] = c * np.eye(3) + (1 - c) * np.outer(u, u) + s * k
            local = local @ t
        transform = parent @ local
        for inst in n.findall('c:instance_geometry', NS):
            p = geometries[inst.attrib['url'][1:]]
            points.append((np.c_[p, np.ones(len(p))] @ transform.T)[:, :3])
        for child in n.findall('c:node', NS):
            node(child, transform)

    for n in root.findall('c:library_visual_scenes/c:visual_scene/c:node', NS):
        node(n, np.eye(4))
    p = np.concatenate(points)
    unit = root.find('c:asset/c:unit', NS)
    p *= float(unit.attrib.get('meter', 1)) if unit is not None else 1
    if root.findtext('c:asset/c:up_axis', default='Y_UP', namespaces=NS) == 'Y_UP':
        p = p[:, [0, 2, 1]]
    return math.ceil(float(np.linalg.norm(p[:, :2], axis=1).max()) * 1000) / 1000


def survey(game):
    text = (Path(__file__).resolve().parents[1] /
            'mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua').read_text(encoding='utf-8')
    archives = {}
    result = {}
    try:
        for group in GROUPS:
            rows = []
            for shape, height in catalog(text, group):
                level = shape.split('/')[2]
                if level not in archives:
                    archives[level] = zipfile.ZipFile(game / 'content/levels' / (level + '.zip'))
                z = archives[level]
                name = shape.lstrip('/') + '.dae'
                name = next(n for n in z.namelist() if n.lower() == name.lower())
                rows.append((height, radius(z.read(name))))
            if rows:
                result[group] = tuple(rows)
    finally:
        for z in archives.values():
            z.close()
    return result


if __name__ == '__main__':
    print('CATALOG = {')
    for group, rows in survey(Path(sys.argv[1])).items():
        print(f'    {group!r}: {rows!r},')
    print('}')
