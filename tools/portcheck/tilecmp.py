#!/usr/bin/env python3
"""tilecmp.py A B [--places a,b]: two recorded sessions' road and content geotiles: road tiles, skins and exact
variants, vegetation, traffic, turbines, far buildings / skyline / far bridges / packs / horizon ring, the AI road
graph, deck spans, the water near the player (session.py's OUT/sessions; msgs.py says how). Exit 0: identical;
1: something differs; 2: tiles sent on one side only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msgs  # noqa: E402

if __name__ == "__main__":
    sys.exit(msgs.main('tiles', __doc__))
