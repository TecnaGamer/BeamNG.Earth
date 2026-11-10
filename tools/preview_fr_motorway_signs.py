"""Render actual French motorway plans, triangles, UVs and materials offline.

Uses a representative straight test road and the production placement pipeline.
These are not BeamNG screenshots or surveyed reproductions of road geometry.
"""
from pathlib import Path
import argparse
import copy
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'tools')]
import frhighwaysigns as fr
import frhighway_refs
import geodesy
import highwaysigns
import signmesh as sm
import signroads
from preview_fr_road_details import render, caption


def example(reference):
    lon, lat = reference['junction']
    frame = geodesy.EnuFrame(lat, lon)
    def ll(x, y):
        la, lo, _ = frame.enu_to_geodetic(x, y, 0.)
        return [float(lo), float(la)]
    width = reference['lanes']*3.5+5.
    main = np.array([[-2000., 0., 0.], [600., 0., 0.]])
    ramp = np.array([[0., 0., 0.], [100., -120., 0.], [200., -200., 0.]])
    roads = [('osm:w100:0', signroads.Spec(width, 'motorway', True, 1, False), main, width),
             ('osm:w200:0', signroads.Spec(6., 'motorway', True, 1, True), ramp, 6.)]
    exit_label = dict(names=[n for b in reference['exit'].get('fr_blocks', ()) for n in b.get('names', ())],
                      ref=reference['exit_ref'], **reference['exit'])
    obj = dict(id=-200, exit=dict(label=exit_label,
        through_label=reference['main'], main_tags=dict(highway='motorway', ref=reference['route'],
            lanes=str(reference['lanes'])), approach=[ll(-1900., 0.), ll(0., 0.)],
        ramp=[ll(*p[:2]) for p in ramp], main_ids=[100], ramp_id=200, side=1,
        fr_support=reference.get('support', 'gantry')))
    context = SimpleNamespace(pavements=lambda *a: roads)
    sides, overheads = highwaysigns.guide_layout(frame, [obj], context,
        (-2100., -300., 700., 300.), lambda la, lo: np.zeros_like(la), mph=False, french=True)
    return frame, ll, context, sides, overheads


def mesh_of(sides, overheads):
    p, t, uv, mats, mi = fr.build(sides, overheads, (0, 0, 0))
    mesh = sm.Mesh()
    # The fixture travels +X: rotate its exported coordinates to the -Y camera.
    mesh.p = np.c_[-p[:, 1], p[:, 0], p[:, 2]]
    mesh.t, mesh.uv, mesh.tm, mesh.materials = t, uv, mi, mats
    return mesh


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    references = frhighway_refs.catalogue()
    for reference, name in zip(references, ('portique-a6', 'potence-a4', 'portique-a355')):
        _, _, _, sides, overheads = example(reference)
        assert len(overheads) == 1
        mesh = mesh_of([], overheads)
        caption(render(mesh, (1600, 1000), -8, 0),
            reference['route']+' · sortie '+reference['exit_ref']+' · maillage généré',
            'Route de test · mêmes modèles et placement qu’en jeu · composition issue des références fournies').save(output/(name+'.png'))
        if name == 'portique-a6':
            # Rear view checks the clamps/rails all the way to the common beam.
            caption(render(mesh, (1600, 900), 162, 9), 'Fixations arrière du portique',
                'Maillage réel · rails et brides rejoignent chaque registre, les cartouches et la traverse').save(output/'fixations-arriere.png')
            side = copy.deepcopy(next(s for s in sides if s['kind'] == 'advance' and s['distance'] == '1000 m'))
            caption(render(mesh_of([side], []), (1000, 1000), -6, 0), 'Sortie sur le bas-côté · annonce à 1000 m',
                'Registres et numéro de sortie séparés · maillage réel hors jeu').save(output/'sortie-laterale.png')
    frame, ll, context, _, _ = example(references[0])
    obj = dict(id=-300, through=dict(main_ids=[100], point=ll(-800., 0.),
        approach=[ll(-900., 0.), ll(-700., 0.)], label=dict(ref='',
            names=['PARIS','BESANÇON','DIJON','BEAUNE'], routes=['E15','E21','A6'],
            distances=['340','135','70','35'])))
    sides, overheads = highwaysigns.guide_layout(frame, [obj], context, (-2100,-300,700,300),
        lambda la, lo: np.zeros_like(la), mph=False, french=True)
    assert len(sides) == 1 and not overheads
    caption(render(mesh_of(sides, []), (1100, 800), -6, 0), 'Confirmation des distances sur le bas-côté',
        'Maillage réel · distances de l’exemple fourni ; en jeu, uniquement si elles sont renseignées').save(output/'confirmation-distances.png')
    sheet = Image.new('RGB', (1800, 1540), '#eaeff2')
    for name, size, at in [('portique-a6', (1100, 744), (0, 0)),
                           ('sortie-laterale', (700, 744), (1100, 0)),
                           ('potence-a4', (1000, 744), (0, 780)),
                           ('confirmation-distances', (800, 744), (1000, 780))]:
        image = Image.open(output/(name+'.png'))
        image.thumbnail(size, Image.Resampling.LANCZOS)
        sheet.paste(image, at)
    sheet.save(output/'apercu-panneaux-autoroute.png')
    print('7 previews in', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, nargs='?', default=ROOT/'build/fr-motorway-previews')
    main(parser.parse_args().output)
