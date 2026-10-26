"""Small, cached state-marker silhouettes for white highway guide-sign insets.

Own vector drawings referenced against the user's state-marker chart and AARoads.
These are guide-panel variants, not standalone sign assemblies. Coordinates are
in a unit box: x=-.5..+.5, z=0..1; number/legend layout follows the available face.
"""
from functools import lru_cache
import math
from typing import NamedTuple

import numpy as np


class Marker(NamedTuple):
    contour: np.ndarray
    triangles: np.ndarray
    number: tuple  # centre x, baseline, cap height, maximum width
    legend: tuple  # text, centre x, baseline, cap height, maximum width


# Concave state borders retain their recognizable features at sign viewing sizes.
# MI/CA and federal markers remain in exitsigns so their existing meshes stay exact.
OUTLINES = {
    'IN': [(-.5,0.),(.5,0.),(.5,1.),(-.5,1.)],
    'OH': [(-.5,.90),(-.10,.88),(.06,.82),(.25,.89),(.5,1.),(.49,.61),(.47,.44),
           (.38,.39),(.40,.31),(.29,.28),(.31,.20),(.21,.15),(.15,.06),(.04,.11),
           (-.04,.03),(-.14,.08),(-.22,0.),(-.33,.08),(-.47,.06)],
    'TN': [(-.5,.08),(-.40,.08),(-.35,0.),(.40,0.),(.5,.14),(.5,1.),(-.5,1.)],
    'AZ': [(-.5,1.),(.5,1.),(.5,.03),(.35,0.),(-.48,.33),(-.44,.49),
           (-.50,.55),(-.45,.64),(-.49,.73)],
    'OK': [(-.5,1.),(.48,1.),(.48,.68),(.5,.52),(.48,.09),(.36,.12),(.29,.07),
           (.21,.10),(.16,.03),(.07,.09),(-.02,.05),(-.10,.13),(-.17,.13),
           (-.22,.22),(-.22,.84),(-.5,.84)],
    'PA': [(-.5,.83),(-.32,.83),(-.32,1.),(.32,1.),(.32,.83),(.5,.83),(.34,0.),(-.34,0.)],
    'TX': [(-.5,0.),(.5,0.),(.5,1.),(-.5,1.)],
    'AR': [(-.5,1.),(.35,1.),(.40,.94),(.50,.94),(.44,.80),(.50,.72),
           (.42,.58),(.45,.48),(.36,.35),(.40,.26),(.30,0.),(-.40,0.),(-.40,.20),(-.48,.22)],
    'MO': [(-.45,1.),(.20,1.),(.25,.88),(.38,.80),(.34,.72),(.48,.62),
           (.43,.48),(.50,.37),(.42,.18),(.45,.08),(.30,0.),(.21,0.),(.26,.13),
           (-.38,.13),(-.38,.68),(-.50,.80),(-.40,.88)],
    'IL': [(-.33,1.),(.23,1.),(.36,.92),(.25,.80),(.38,.71),(.43,.54),(.37,.40),
           (.5,.17),(.35,.12),(.31,0.),(.18,.06),(.02,.03),(-.08,.12),(-.18,.18),
           (-.29,.18),(-.40,.36),(-.43,.51),(-.5,.62),(-.40,.76),(-.36,.89)],
    'NV': [(-.5,1.),(.5,1.),(.5,.22),(.43,.16),(.40,.04),(.31,0.),(-.5,.71)],
}
SUPPORTED_STATES = frozenset(OUTLINES) | {'KS','KY'}

# Shape-specific placement keeps digits away from the Oklahoma panhandle,
# Nevada diagonal, Pennsylvania shoulders and the Arizona sloping southwest edge.
NUMBERS = {
    'IN': (0.,.19,.48,.78), 'OH': (-.03,.32,.40,.62),
    'TN': (0.,.31,.46,.78), 'AZ': (.05,.35,.36,.70),
    'OK': (.11,.28,.43,.65), 'PA': (0.,.28,.41,.61),
    'TX': (0.,.34,.49,.78), 'AR': (-.04,.29,.44,.67),
    'MO': (-.02,.32,.43,.66), 'IL': (-.01,.33,.36,.60),
    'NV': (.13,.46,.29,.59), 'KS': (0.,.29,.43,.66),
    'KY': (0.,.30,.43,.72),
}
LEGENDS = {
    'IN': ('INDIANA',0.,.80,.115,.78),
    'TN': ('TENNESSEE',.015,.09,.10,.76),
    'AZ': ('ARIZONA',0.,.82,.11,.78),
    'TX': ('TEXAS',0.,.105,.13,.76),
    'IL': ('ILLINOIS',-.015,.79,.09,.53),
    'NV': ('NEVADA',0.,.82,.11,.76),
}


@lru_cache(maxsize=16)
def marker(state):
    """Return a triangulated reusable profile; no per-sign files or image generation."""
    if state not in SUPPORTED_STATES:
        return None
    if state in ('KS','KY'):
        angle=np.linspace(0.,2*math.pi,97 if state=='KS' else 49)[:-1]
        radius=.465+.025*np.cos(16*angle) if state=='KS' else np.full(len(angle),.49)
        contour=np.c_[radius*np.cos(angle),.5+radius*np.sin(angle)]
    else:
        contour=np.asarray(OUTLINES[state],float)
    area=np.sum(contour[:,0]*np.roll(contour[:,1],-1)-contour[:,1]*np.roll(contour[:,0],-1))
    if area<0:contour=contour[::-1].copy()
    # Existing project ear clipping handles the Oklahoma panhandle and other
    # non-star-shaped outlines without the overlapping triangles of a centre fan.
    from buildings import triangulate
    triangles=triangulate(contour)
    if len(triangles)!=len(contour)-2:
        raise ValueError('Invalid state route-marker outline: '+state)
    contour.setflags(write=False);triangles.setflags(write=False)
    return Marker(contour,triangles,NUMBERS[state],LEGENDS.get(state,()))
