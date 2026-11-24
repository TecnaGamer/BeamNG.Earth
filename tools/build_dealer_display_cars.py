"""Build unbranded parked-car scenery. Metres, nose along +X, wheel contact at Z=0."""
import json
import math
from pathlib import Path

import npyfirst  # Apply the build tools' NumPy guard before importing NumPy.
import numpy as np

from build_rail_crossing_props import Mesh, write_dae

OUT = Path(__file__).resolve().parents[1] / "mod/beamng_earth/art/shapes/beamngearth/dealer"
# Body half length, half width, belt line, roof height, cabin stations (rear to front).
PROFILES = {
    "sedan": (2.28, .89, .91, 1.45, (-1.55, -.93, .25, 1.10), "silver"),
    "hatchback": (1.98, .85, .92, 1.48, (-1.78, -1.50, .20, .98), "red"),
    "suv": (2.32, .94, 1.11, 1.74, (-2.11, -1.76, .24, 1.19), "blue"),
    "pickup": (2.59, .98, 1.15, 1.79, (-.61, -.54, .51, 1.18), "white"),
}
COLORS = {"silver": [.55,.58,.60], "red": [.48,.055,.04], "blue": [.065,.18,.27],
          "white": [.81,.80,.76], "glass": [.085,.15,.19], "rubber": [.035,.036,.039],
          "rim": [.52,.54,.55], "tail": [.68,.055,.032], "lamp": [.84,.87,.82]}


def shell(mesh, rings, material):
    mesh.face(material, rings[0][::-1])
    mesh.face(material, rings[-1])
    for left, right in zip(rings, rings[1:]):
        for i in range(len(left)):
            j = (i+1) % len(left)
            mesh.face(material, [left[i],left[j],right[j],right[i]])


def car(profile, detail):
    length, width, belt, roof, cab, paint = profile
    mesh = Mesh()
    paint = "bne_inventory_" + paint
    def trim(kind):
        return "bne_inventory_" + kind
    # The bevel at each shoulder catches daylight even at the far detail level.
    rings = []
    for x, w, h in [(-length,.89,belt-.1),(-length+.25,1,belt),
                     (length-.39,1,belt-.05),(length,.89,belt-.22)]:
        w *= width
        rings.append([(x,-w*.93,.33),(x,w*.93,.33),(x,w,h-.13),
                      (x,w*.94,h),(x,-w*.94,h),(x,-w,h-.13)])
    shell(mesh,rings,paint)
    lower = belt-.065
    cap = [(-width*.89,lower),(width*.89,lower),(width*.76,roof),(-width*.76,roof)]
    # Glass wraps around the cabin; the roof and pillars retain the body colour.
    back, rear_top, front_top, front = cab
    lr, rr = (back,cap[0][0],lower),(back,cap[1][0],lower)
    lf, rf = (front,cap[0][0],lower),(front,cap[1][0],lower)
    a,b = (rear_top,cap[3][0],roof),(rear_top,cap[2][0],roof)
    c,d = (front_top,cap[3][0],roof),(front_top,cap[2][0],roof)
    for face in ([lr,a,b,rr],[lf,rf,d,c],[lr,lf,c,a],[rr,b,d,rf]):
        mesh.face(trim("glass"),face)
    mesh.face(paint,[a,c,d,b])
    if detail:
        for side in (-1,1):
            x = (rear_top+front_top)*.5
            y0,y1 = side*(width*.89+.006),side*(width*.76+.006)
            pillar = [(x-.04,y0,lower),(x+.04,y0,lower),(x+.04,y1,roof),(x-.04,y1,roof)]
            mesh.face(trim("rubber"),pillar if side<0 else pillar[::-1])
        if cab[0] > -1:  # Open pickup bed behind the cab.
            mesh.box(trim("rubber"),((-length+back)/2,0,belt+.005),((length+back)/2-.12,width*.8,.025))
    radius = .35 if roof > 1.6 else .30
    for x in (-length*.65,length*.65):
        for side in (-1,1):
            segs = 12 if detail else 8
            angle = np.arange(segs)*2*math.pi/segs
            def wheel_ring(y,r):
                return [(x+r*math.cos(t),y,radius+r*math.sin(t)) for t in angle]
            inner, outer = wheel_ring(side*(width-.19),radius), wheel_ring(side*(width+.017),radius)
            for i in range(segs):
                j = (i+1)%segs
                quad = [inner[i],outer[i],outer[j],inner[j]]
                mesh.face(trim("rubber"),quad if side>0 else quad[::-1])
            mesh.face(trim("rubber"),outer[::-1] if side>0 else outer)
            if detail:
                hub = wheel_ring(side*(width+.021),radius*.57)
                mesh.face(trim("rim"),hub[::-1] if side>0 else hub)
    for x,kind,z in [(length,"lamp",belt-.29),(-length,"tail",belt-.19)]:
        for side in (-1,1):
            mesh.box(trim(kind),(x,side*width*.65,z),(.009,width*.17,.045))
    return mesh


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    materials = {}
    for kind,rgb in COLORS.items():
        name = "bne_inventory_"+kind
        materials[name] = {"name":name,"mapTo":name,"class":"Material","version":1.5,
                           "Stages":[{"baseColorFactor":rgb+[1],"roughnessFactor":.32 if kind=="glass" else .54}]}
    (OUT/"main.materials.json").write_text(json.dumps(materials,indent=2)+"\n",encoding="utf8")
    for name,profile in PROFILES.items():
        lods = [(48,car(profile,True)),(3,car(profile,False))]
        write_dae(OUT/(name+".dae"),"dealer_"+name,lods,Mesh())
        print(name,[sum(map(len,m.tri.values())) for _,m in lods])


if __name__ == "__main__":
    main()
