"""swissTLM3D's kinds of road structure (KUNSTBAUTE) as roadz levels (>0 on a deck, 0 on the ground, <0 underground):
the "tlm" source (roadz.py) and its cutting tool (tools/build_swisstlm.py) read them from here.

The codes are the release's own coded value domain, read from its FileGDB (swissTLM3D 2026-02, GDB_Items:
TLM_STRASSE_KUNSTBAUTE_CD, 2026-11-22). Only roads (TLM_STRASSE) are cut: no code reads railway heights from roadz,
and a railway's line beside a road could only be matched to it. (The railways number their structures otherwise:
their 800 is a tunnel, a road's 800 a dam wall.)
"""

STRASSE = {
    100: 0,         # Keine
    200: 1,         # Bruecke
    300: 1,         # Bruecke mit Galerie
    400: 1,         # Gedeckte Bruecke
    450: 1,         # Bruecke mit Treppe
    500: 0,         # Staudamm (an earth dam: the road on its crest, on the ground)
    600: 1,         # Steg (a footbridge)
    700: 0,         # Galerie (a road along a slope under a roof: on the ground)
    800: 1,         # Staumauer, Wehr (on a dam wall or weir, over the valley or the water)
    900: 0,         # Treppe
    1000: -1,       # Tunnel
    1100: -1,       # Unterfuehrung
    1200: -1,       # Unterfuehrung mit Treppe
    1300: 0,        # Furt
    1400: None,     # in/auf Gebaeude: through a building or on its roof, which is not said: not cut
}


def tlm_level(code):
    """The roadz level of a swissTLM3D road with KUNSTBAUTE `code`; None for a line not to be cut (in or on a
    building); 0, the ground, for an unknown code, 999997 ("ub", unknown) and 999998 ("k_W", none) or none."""
    try:
        return STRASSE.get(int(code), 0)
    except (TypeError, ValueError):
        return 0
