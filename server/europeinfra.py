"""Country-specific scenery presets, selected at each object's coordinates.

These are representative variants, not a survey of every national road asset.
The supplied comparison photographs guide poles/delineators. Shared families
are intentional; a country's equipment is not identified from its appearance.
Town-name boards and built-up-area boards are distinguished in TownStyle.
No profile changes junction priorities, speed limits, or STOP placement.
"""
from dataclasses import dataclass,replace

VERSION = 'europe-infrastructure-v3'


@dataclass(frozen=True)
class TownStyle:
    background: str = 'white'
    border: str = 'black'
    ink: str = 'black'
    skyline: bool = False
    uppercase: bool = True
    exit: str = 'slash'
    # Name-only boards are not converted into regulatory built-up signs.
    meaning: str = 'built-up'
    footer: str = ''
    header_speed: int | None = None


@dataclass(frozen=True)
class Kit:
    chevron: str                 # foreground_background
    marker: str                 # model family, front/back reflectors
    poles: tuple                # stable per-road representative alternatives
    town: str


TOWNS = {
    'fr': TownStyle('white', 'red'),
    'red': TownStyle('white', 'red'),
    'plain': TownStyle(),
    'mixed': TownStyle(uppercase=False),
    'yellow': TownStyle('yellow', 'black', uppercase=False),
    'blue_border': TownStyle('white', 'blue', uppercase=False),
    'blue': TownStyle('blue', 'white', 'white', exit='none', meaning='place-name'),
    'blue_exit': TownStyle('blue', 'white', 'white'),
    'dutch': TownStyle('blue', 'white', 'white', uppercase=False),
    'green': TownStyle('green', 'white', 'white', uppercase=False, meaning='place-name'),
    'skyline': TownStyle(skyline=True, uppercase=False),
    'skyline_yellow': TownStyle('yellow', 'black', skyline=True, uppercase=False),
    'uk': TownStyle(uppercase=False, exit='none', meaning='place-name'),
    'ie': TownStyle(exit='none', meaning='place-name'),
    'gr': TownStyle(footer='blue'),
    'ro': TownStyle(uppercase=False),
}

# B = bare/concrete; W = timber. These weights are scenery choices, not
# measured national percentages. The France preference is kept in poleprofiles.
KITS = {
    'AD': Kit('red_white', 'ad', ('concrete_round',), 'red'),
    'AL': Kit('red_white', 'red_black', ('concrete_round', 'concrete_three'), 'blue'),
    'AT': Kit('red_white', 'at', ('concrete_round', 'wood_a'), 'blue_border'),
    'AX': Kit('yellow_black', 'nordic', ('wood_side', 'wood_h'), 'blue'),
    'BA': Kit('red_white', 'red_black', ('concrete_round',), 'yellow'),
    'BE': Kit('red_white', 'be', ('concrete_open', 'concrete_round'), 'skyline'),
    'BG': Kit('red_white', 'red_black', ('concrete_open', 'concrete_three'), 'blue_border'),
    'BY': Kit('white_red', 'red_black', ('concrete_three',), 'plain'),
    'CH': Kit('white_black', 'ch', ('wood_side', 'concrete_round'), 'plain'),
    'CY': Kit('red_white', 'red_black', ('wood_t',), 'plain'),
    'CZ': Kit('red_white', 'cz', ('concrete_round', 'wood_a'), 'plain'),
    'DE': Kit('red_white', 'de', ('concrete_round', 'wood_side'), 'yellow'),
    'DK': Kit('red_white', 'dk', ('wood_side',), 'skyline'),
    'EE': Kit('white_red', 'nordic', ('wood_side', 'wood_a'), 'skyline'),
    'ES': Kit('white_blue', 'es', ('concrete_open', 'concrete_round'), 'red'),
    'FI': Kit('yellow_black', 'nordic', ('wood_side', 'wood_h'), 'blue'),
    'FO': Kit('red_white', 'fo', ('concrete_round',), 'skyline'),
    'FR': Kit('white_blue', 'fr', ('fr_wood', 'fr_concrete'), 'fr'),
    'GB': Kit('white_black', 'gb', ('wood_side',), 'uk'),
    'GG': Kit('white_black', 'gb', ('wood_side',), 'uk'),
    'GI': Kit('white_black', 'gb', ('concrete_round',), 'uk'),
    'GR': Kit('white_black', 'red_black', ('wood_t', 'concrete_round'), 'gr'),
    'HR': Kit('red_white', 'red_black', ('concrete_round',), 'yellow'),
    'HU': Kit('white_red', 'red_black', ('concrete_open', 'wood_a'), 'red'),
    'IE': Kit('yellow_black', 'ie', ('wood_side', 'wood_a'), 'ie'),
    'IM': Kit('white_black', 'gb', ('wood_side',), 'uk'),
    'IS': Kit('yellow_black', 'is', ('wood_h',), 'skyline_yellow'),
    'IT': Kit('white_black', 'red_black', ('concrete_round', 'concrete_pair'), 'plain'),
    'JE': Kit('white_black', 'gb', ('wood_side',), 'uk'),
    'LI': Kit('white_black', 'ch', ('wood_side', 'concrete_round'), 'plain'),
    'LT': Kit('red_white', 'red_black', ('concrete_three', 'wood_a'), 'blue'),
    'LU': Kit('red_white', 'de', ('concrete_round', 'wood_t'), 'yellow'),
    'LV': Kit('red_white', 'nordic', ('wood_side', 'wood_a'), 'blue'),
    'MC': Kit('white_blue', 'fr', ('fr_concrete',), 'fr'),
    'MD': Kit('white_red', 'red_black', ('concrete_three',), 'plain'),
    'ME': Kit('yellow_black', 'red_black', ('concrete_round',), 'yellow'),
    'MK': Kit('red_white', 'red_black', ('concrete_open', 'wood_t'), 'yellow'),
    'MT': Kit('white_black', 'gb', ('concrete_round',), 'plain'),
    'NL': Kit('red_white', 'nl', ('concrete_round',), 'dutch'),
    'NO': Kit('yellow_black', 'de', ('wood_side', 'wood_h'), 'blue'),
    'PL': Kit('red_white', 'pl', ('concrete_open', 'wood_a'), 'green'),
    'PT': Kit('yellow_black', 'de', ('concrete_open', 'concrete_round'), 'mixed'),
    'RO': Kit('red_white', 'nl', ('concrete_open', 'concrete_round'), 'ro'),
    'RS': Kit('black_white', 'rs', ('concrete_round', 'steel_lattice'), 'yellow'),
    'RU': Kit('white_red', 'red_black', ('concrete_three', 'wood_a'), 'plain'),
    'SE': Kit('yellow_blue', 'de', ('wood_side', 'wood_h'), 'blue'),
    'SI': Kit('red_white', 'si', ('wood_a', 'concrete_round'), 'yellow'),
    'SK': Kit('red_white', 'cz', ('concrete_round', 'wood_a'), 'blue_border'),
    'SM': Kit('white_black', 'red_black', ('concrete_round', 'steel_lattice'), 'plain'),
    'TR': Kit('red_white', 'tr', ('concrete_open', 'concrete_three'), 'blue_exit'),
    'UA': Kit('white_red', 'red_black', ('concrete_three', 'concrete_round'), 'plain'),
    'VA': Kit('white_black', 'red_black', ('concrete_round',), 'plain'),
    'XK': Kit('red_white', 'red_black', ('concrete_round',), 'yellow'),
    # Caucasus: representative shared families; limited photographic reference.
    'GE': Kit('white_red', 'red_black', ('concrete_three', 'wood_side'), 'mixed'),
    'AM': Kit('white_red', 'red_black', ('concrete_three',), 'mixed'),
    'AZ': Kit('white_red', 'red_black', ('concrete_three',), 'blue'),
}
COUNTRIES = frozenset(KITS)
LEFT_HAND = frozenset('GB IE CY MT IM JE GG'.split())

# Metres; the asset builder and the wire generator use the same attachment data.
POLE_LAYOUTS = {
    'wood_side': (8.5, ((.27,0,7.86),(.27,0,7.20),(.27,0,6.54))),
    'wood_t': (8.5, ((-.9,0,8.08),(0,0,8.08),(.9,0,8.08))),
    'wood_a': (8.5, ((-.9,0,8.08),(0,0,8.08),(.9,0,8.08))),
    'wood_h': (8.5, ((-1.1,0,8.08),(0,0,8.08),(1.1,0,8.08))),
    'concrete_round': (8.5, ((.27,0,7.86),(.27,0,7.20),(.27,0,6.54))),
    'concrete_open': (8.5, ((-.9,0,8.08),(0,0,8.08),(.9,0,8.08))),
    'concrete_pair': (8.5, ((-.9,0,8.08),(0,0,8.08),(.9,0,8.08))),
    'concrete_three': (8.5, ((-.55,0,8.08),(.55,0,7.45),(-.55,0,6.82))),
    'steel_lattice': (8.5, ((-.9,0,8.08),(0,0,8.08),(.9,0,8.08))),
}


def town_style(country, road_class=None, speed=None):
    kit = KITS.get(country)
    if kit is None:
        return None
    # Switzerland/Liechtenstein have blue principal-road name boards and white
    # secondary-road variants. Do not invent a single nationwide board colour.
    if country in {'CH', 'LI'} and road_class in {'primary', 'secondary'}:
        return TOWNS['blue_exit']
    if country=='RO' and speed is not None and 10<=speed<=90:
        # Only display a numeric speed when the mapped road supplies it; never
        # invent a 50 km/h override for the navigation or speed-sign systems.
        return replace(TOWNS['ro'],header_speed=int(speed))
    return TOWNS[kit.town]
