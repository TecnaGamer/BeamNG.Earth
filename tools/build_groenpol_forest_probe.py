"""Reproduce a small Forest-rendering probe from Sven's prepared signal assets.

The body is permanently dark; a separate tiny optical face carries one fixed
state. No per-frame material mutation or independent TSStatic is required.
BeamNG rendering still needs an in-game probe before adopting this approach.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import cdae

NS = {'c': 'http://www.collada.org/2005/11/COLLADASchema'}
ASSETS = ROOT / 'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/groenpol'
PREFIX = 'bne_groenpol_forest_probe_'


def prepared_geometry(path):
    tree = ET.parse(path)
    if tree.find('.//c:up_axis', NS).text != 'Z_UP':
        raise ValueError('expected prepared Z_UP geometry')
    # The prepared components have baked positions. Never silently ignore a
    # node transform if a future export changes that convention.
    for node in tree.findall('.//c:visual_scene//c:node', NS):
        if any(node.find('c:' + name, NS) is not None for name in ('matrix', 'translate', 'rotate', 'scale')):
            raise ValueError('prepared geometry must have baked transforms')
    geometries = {g.get('id'): g for g in tree.findall('.//c:library_geometries/c:geometry', NS)}
    out = []
    for instance in tree.findall('.//c:visual_scene//c:instance_geometry', NS):
        geom = geometries[instance.get('url').lstrip('#')]
        bindings = {m.get('symbol'): m.get('target').lstrip('#') for m in instance.findall('.//c:instance_material', NS)}
        mesh = geom.find('c:mesh', NS)
        sources = {}
        for source in mesh.findall('c:source', NS):
            accessor = source.find('c:technique_common/c:accessor', NS)
            data = np.fromstring(source.find('c:float_array', NS).text, sep=' ')
            sources[source.get('id')] = data.reshape(-1, int(accessor.get('stride')))
        vertices = {v.get('id'): v.find('c:input', NS).get('source').lstrip('#') for v in mesh.findall('c:vertices', NS)}
        if mesh.find('c:polylist', NS) is not None:
            raise ValueError('expected triangulated prepared geometry')
        for primitive in mesh.findall('c:triangles', NS):
            inputs = {i.get('semantic'): (int(i.get('offset')), i.get('source').lstrip('#')) for i in primitive.findall('c:input', NS)}
            if set(inputs) != {'VERTEX', 'NORMAL', 'TEXCOORD'}:
                raise ValueError('prepared vertex semantics')
            stride = max(i[0] for i in inputs.values()) + 1
            ix = np.fromstring(primitive.find('c:p', NS).text, sep=' ', dtype=np.int64).reshape(-1, stride)
            if len(ix) != int(primitive.get('count')) * 3:
                raise ValueError('prepared triangle count')
            pidx, psource = inputs['VERTEX']; nidx, nsource = inputs['NORMAL']; uidx, usource = inputs['TEXCOORD']
            unique, remap = np.unique(ix[:, [pidx, nidx, uidx]], axis=0, return_inverse=True)
            p = sources[vertices[psource]][unique[:, 0], :3]
            n = sources[nsource][unique[:, 1], :3]
            uv = sources[usource][unique[:, 2], :2]
            out.append((p, remap.reshape(-1, 3), n, uv, bindings[primitive.get('material')]))
    if not out:
        raise ValueError('empty prepared component')
    return out


def material(source, name, lit=False):
    out = copy.deepcopy(source)
    out['name'] = out['mapTo'] = name
    for stage in out.get('Stages', []):
        if not isinstance(stage, dict) or not stage:
            continue
        stage.pop('colorPaletteMap', None)
        stage.pop('instanceDiffuse', None)
        stage['instanceEmissive'] = False
        if not lit:
            stage.pop('emissiveMap', None)
            stage.pop('emissiveFactor', None)
            stage.pop('emissiveIntensityNits', None)
            stage['emissive'] = False
    return out


def write_mesh(path, pieces):
    positions, triangles, normals, uvs, mats, tri_mats = [], [], [], [], [], []
    offset = 0
    for p, t, n, uv, mat in pieces:
        if mat not in mats:
            mats.append(mat)
        positions.append(p);triangles.append(t + offset);normals.append(n);uvs.append(uv)
        tri_mats.extend([mats.index(mat)] * len(t))
        offset += len(p)
    data = cdae.encode(np.concatenate(positions), np.concatenate(triangles), normals=np.concatenate(normals),
                       uvs=np.concatenate(uvs), material=mats, tri_materials=np.array(tri_mats),
                       object_name='groenpol_forest_probe', close=False)
    path.write_bytes(data)
    return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), triangles=len(tri_mats))


def build(target, variant='vehicle', virtual_root='/temp/nl_forest_probe_assets_20261114'):
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError('use an empty probe output directory')
    catalog = json.loads((ROOT / 'docs/assets/groenpol/components.json').read_text())
    if variant not in catalog['heads'] or not variant.startswith('vehicle'):
        raise ValueError('vehicle head variant required')
    source = ROOT / 'mod/beamng_earth' / catalog['heads'][variant]['shape'].lstrip('/')
    original = json.loads((ASSETS / 'main.materials.json').read_text())
    pieces = prepared_geometry(source)
    target.mkdir(parents=True, exist_ok=True)
    materials, dark, optics = {}, [], {}
    for p, t, n, uv, mat in pieces:
        if '_lens_' in mat:
            channel = int(mat.rsplit('_lens_', 1)[1].split('_')[0])
            dark_source = mat.rsplit('_lens_', 1)[0] + '_dark_v4'
            dark_name = PREFIX + dark_source
            materials[dark_name] = material(original[dark_source], dark_name)
            dark.append((p, t, n, uv, dark_name))
            lit_name = PREFIX + mat
            materials[lit_name] = material(original[mat], lit_name, lit=True)
            # Avoid z-fighting with the permanent dark optical surface. The
            # component's authored +X faces the approaching driver.
            face = p.copy();face[:, 0] += .001
            optics[channel] = (face, t, n, uv, lit_name)
        else:
            name = PREFIX + mat
            materials[name] = material(original[mat], name)
            dark.append((p, t, n, uv, name))
    if set(optics) != {0, 1, 2}:
        raise ValueError('expected all three authored optical channels')
    files = {'head_dark.cdae': write_mesh(target / 'head_dark.cdae', dark)}
    for i, name in enumerate(('red', 'amber', 'green')):
        files[f'lens_{name}.cdae'] = write_mesh(target / f'lens_{name}.cdae', [optics[i]])
    for name, shape in (('beam_unit.cdae', catalog['supports']['cantilever_3']['construction']['beam_shape']),
                        ('mast.cdae', catalog['supports']['straight']['shape'])):
        source_part = ROOT / 'mod/beamng_earth' / shape.lstrip('/')
        mesh = []
        for p, t, n, uv, mat in prepared_geometry(source_part):
            target_name = PREFIX + mat
            materials[target_name] = material(original[mat], target_name)
            mesh.append((p, t, n, uv, target_name))
        files[name] = write_mesh(target / name, mesh)
    # A self-contained temp probe must also work without installing the PR mod.
    texture_names = set()
    for mat in materials.values():
        for stage in mat.get('Stages', []):
            for key, value in list(stage.items()):
                if isinstance(value, str) and value.startswith('/levels/beamng_earth/') and value.endswith('.dds'):
                    texture_names.add(Path(value).name)
                    stage[key] = virtual_root.rstrip('/') + '/' + Path(value).name
    for name in sorted(texture_names):
        raw = (ASSETS / name).read_bytes()
        (target / name).write_bytes(raw)
        files[name] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (target / 'main.materials.json').write_text(json.dumps(materials, indent=2) + '\n')
    (target / 'LICENSE.md').write_bytes((ASSETS / 'LICENSE.md').read_bytes())
    manifest = dict(format=1, variant=variant, virtual_root=virtual_root,
                    cap_z=catalog['supports']['straight']['mounts'][0]['position'][2],
                    beam_centre_z=sum(v[2] for v in catalog['supports']['cantilever_3']['construction']['beam_bounds_m']) / 2,
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    author='Sven / svenegaming-glitch', license='CC-BY-SA-4.0', files=files,
                    status='engine_probe_only_not_runtime_enabled')
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--variant', default='vehicle')
    parser.add_argument('--virtual-root', default='/temp/nl_forest_probe_assets_20261114')
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.variant, args.virtual_root), indent=2))
