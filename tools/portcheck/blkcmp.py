#!/usr/bin/env python3
"""blkcmp.py A B [--places a,b]: two recorded sessions' terrain blocks: every terrain_ready / terrain_refresh /
block_base by its centre, its heightmap, layer masks, .ter and ground photo by content (PNGs compared by pixel when
their bytes differ), the anchor's ground height and the collision patches (session.py's OUT/sessions; msgs.py).
Exit 0: identical; 1: something differs; 2: blocks on one side only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msgs  # noqa: E402

if __name__ == "__main__":
    sys.exit(msgs.main('blocks', __doc__))
