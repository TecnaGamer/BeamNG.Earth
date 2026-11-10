"""Production motorway legends/shoulder plans on a synthetic sloping test road."""
from pathlib import Path
import argparse
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'tools')]
import frhighwaysigns as fr
import geodesy
import highwaysigns
import signmesh as sm
import signroads
from preview_fr_road_details import caption, render


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    label = dict(ref='36', names=['Clermont-Fᵈ','Bourges','Désertines'], routes=[])
    plan = fr.compose(label, True)
    mesh = sm.Mesh()
    fr.draw(mesh, plan)
    mesh.materials = fr.MATERIALS
    caption(render(mesh, (1000, 820)), 'Sortie 36 · légende et registres corrigés',
            'Maillage et atlas réels hors jeu · petit D réutilisant le glyphe D existant').save(out/'clermont-superscript.png')

    frame = geodesy.EnuFrame(46.3801, 2.6229)
    def ll(x, y):
        la, lo, _ = frame.enu_to_geodetic(x, y, 0.)
        return [float(lo), float(la)]
    mainline = np.array([[-1900.,0.,0.],[600.,0.,0.]])
    ramp = np.array([[0.,0.,0.],[100.,120.,0.],[200.,200.,0.]])
    rows = [('osm:w100:0', signroads.Spec(12.,'motorway',True,1,False), mainline, 12.),
            ('osm:w200:0', signroads.Spec(6.,'motorway',True,1,True), ramp, 6.)]
    obj = dict(id=-200, exit=dict(side=-1, main_ids=[100], ramp_id=200,
        main_tags=dict(highway='motorway',ref='A714'),
        approach=[ll(-1800.,0.),ll(0.,0.)], ramp=[ll(*p[:2]) for p in ramp],
        label=dict(ref='36',names=['Bourges','Montluçon','Désertines'],routes=[])))
    def slope(lat, lon):
        _, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
        return np.maximum(0., -np.asarray(y)-6.)*.65
    sides, _ = highwaysigns.guide_layout(frame, [obj], SimpleNamespace(pavements=lambda *a: rows),
        (-2000,-100,500,100), slope, mph=False, french=True)
    sign = next(s for s in sides if s['kind']=='advance' and s['distance']=='500 m')
    p, t, uv, mats, mi = fr.build([sign], [], (0,0,0))
    mesh = sm.Mesh()
    mesh.p, mesh.t, mesh.uv, mesh.tm = list(np.c_[-p[:,1],p[:,0],p[:,2]]),list(t),list(uv),list(mi)
    mesh.materials = mats
    depth = sign['centre'][0]-.25
    # A cross-section of the actual test slope occludes the buried post ends.
    mesh.face(sm.WHITE, [(-1,depth,-.45),(16,depth,-.45),(16,depth,0),(-1,depth,0)])
    mesh.face(sm.WHITE, [(6,depth,0),(16,depth,0),(16,depth,6.5)])
    mesh.face(sm.METAL, [(-1,depth-.01,-.06),(6,depth-.01,-.06),
                         (6,depth-.01,.015),(-1,depth-.01,.015)])
    caption(render(mesh,(1500,850)), 'Annonce à droite · pieds adaptés au talus',
            'Coupe de route de test : chaussée grise à gauche, talus à droite · pas une capture BeamNG').save(out/'annonce-talus-droite.png')
    sheet = Image.new('RGB',(1900,1000),'#eaeff2')
    for name, bounds, pos in [('clermont-superscript',(850,980),(0,0)),
                              ('annonce-talus-droite',(1050,980),(850,0))]:
        with Image.open(out/(name+'.png')) as image:
            image.thumbnail(bounds,Image.Resampling.LANCZOS)
            sheet.paste(image,pos)
    sheet.save(out/'apercu-sorties-france-v3.png')
    print('3 previews in',out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, nargs='?', default=ROOT/'build/fr-exit-corrections')
    main(parser.parse_args().output)
