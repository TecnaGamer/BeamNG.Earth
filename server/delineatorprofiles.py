"""Roadside delineator deployment, separate from the country's model family.

See docs/DELINEATOR_RESEARCH.md for sources, dates and modelling assumptions.
Spacing on equipped roads is not a national coverage percentage. Road class,
width and mapped speed are proxies where traffic counts are unavailable.
Unverified countries explicitly retain the v3 trial, rather than inheriting
Germany's widespread deployment. France keeps its existing sparse J6 trial.
"""
from dataclasses import dataclass

VERSION = 'delineator-deployment-v4'
MAIN = frozenset(('primary', 'secondary'))
REGIONAL = MAIN | {'tertiary'}
RURAL = REGIONAL | {'unclassified'}


@dataclass(frozen=True)
class Policy:
    mode: str = 'regular'              # regular / bends / legacy
    spacing: float = 50.
    classes: frozenset = REGIONAL
    min_width: float = 4.
    min_speed: float = 0.              # only applied to a known speed
    unlit: bool = False
    gravel: bool = False
    curves: tuple = ((100., 10.), (300., 25.))
    source: str = ''
    evidence: str = 'spacing; coverage inferred'


LEGACY = Policy(mode='legacy', spacing=90., classes=MAIN,
                source='v3 trial', evidence='provisional; not a measured prevalence')
BENDS = Policy(mode='bends', classes=RURAL, source='UK-TSM4-15')
PROFILES = {
    'FR': Policy(mode='legacy', spacing=90., classes=MAIN, source='user preference',
                 evidence='8% scenery trial, unchanged; not a national statistic'),
    'DE': Policy(classes=RURAL, min_width=3.5, source='DE-StVO-620'),
    'AT': Policy(spacing=33., curves=((100., 11.), (300., 16.5)), source='AT-Kaernten-2019'),
    'CH': Policy(source='CH-Luzern-653.301'),
    'CZ': Policy(classes=RURAL, min_width=3.5,
                 curves=((50., 5.), (250., 10.), (450., 20.), (850., 30.), (1250., 40.)),
                 source='CZ-BESIP / SK-TP105', evidence='CZ spacing; SK curve table used as approximation'),
    'SK': Policy(classes=RURAL, min_width=3.5,
                 curves=((50., 5.), (250., 10.), (450., 20.), (850., 30.), (1250., 40.)),
                 source='SK-TP105-2022'),
    'DK': Policy(spacing=100., curves=((250., 25.), (2000., 50.)), source='DK-N41'),
    'PL': Policy(spacing=100., curves=((100., 25.), (300., 50.)), source='PL-GDDKiA-2022'),
    'ES': Policy(min_speed=60., curves=((100., 10.), (150., 12.5), (200., 16.67),
                 (300., 20.), (500., 25.), (700., 33.33)), source='ES-OC309 / Bizkaia',
                 evidence='spacing on equipped roads; regional road-class approximation'),
    'IT': Policy(min_speed=60., curves=((30., 6.), (50., 8.), (100., 12.),
                 (200., 20.), (400., 30.)), source='IT-DPR495-art173',
                 evidence='spacing; speed/geometry/climate coverage approximated by road class'),
    'SE': Policy(classes=MAIN, min_width=5.5, min_speed=80., unlit=True,
                 curves=((300., 25.),), source='SE-VGU2015 / NVDB2013',
                 evidence='historical guidance; traffic volume unavailable'),
    'NO': Policy(classes=MAIN, min_width=6.5, min_speed=80., unlit=True,
                 curves=((50., 10.), (300., 25.)), source='NO-N300-2012',
                 evidence='historical guidance; traffic volume / winter salting unavailable'),
    'IS': Policy(classes=RURAL, min_width=3., unlit=True, gravel=True,
                 curves=((300., 25.),), source='IS-Vegagerdin-2.1',
                 evidence='road class substitutes for unavailable traffic volume'),
    'GB': BENDS,
    'IE': Policy(mode='bends', classes=RURAL, source='IE-TSM6-6.22'),
    # These territories share a representative family, not a verified national rule.
    **{c: Policy(mode='bends', classes=RURAL, source='GB analogy', evidence='provisional')
       for c in ('GG', 'GI', 'IM', 'JE')},
    'LI': Policy(source='CH analogy', evidence='provisional'),
}


def policy(country):
    return PROFILES.get(country, LEGACY)


def eligible(country, spec, lit=None):
    p = policy(country)
    return (p.mode == 'regular' and spec.cls in p.classes and not spec.urban and not spec.link
            and spec.width >= p.min_width
            and (spec.kind == 'paved' or (p.gravel and spec.kind == 'gravel'))
            and (spec.kmh is None or spec.kmh >= p.min_speed)
            and not (p.unlit and str(lit).lower() in {'yes', 'true', '1', '24/7'}))


def spacing(country, spec, radius=float('inf')):
    p = policy(country)
    # Iceland's lower-traffic roads: one side, 100 m (class proxy, not a count).
    base = 100. if country == 'IS' and spec.cls in {'tertiary', 'unclassified'} else p.spacing
    for limit, distance in p.curves:
        if radius < limit:
            return distance * (2 if base == 100. and country == 'IS' else 1)
    return base


def verge(country, spec, radius, turn, side):
    if country == 'IS' and spec.cls in {'tertiary', 'unclassified'}:
        return side == -1
    # N300: only the outer verge in very tight bends.
    return not (country == 'NO' and radius < 50. and side * turn > 0)


def legacy_share(country):
    if policy(country).mode != 'legacy':
        return 0.
    return .08 if country == 'FR' else .35
