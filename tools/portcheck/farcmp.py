#!/usr/bin/env python3
"""farcmp.py A B [--places a,b]: two recorded sessions' far tiles by key: each far_tile's fields (level, position,
error, skirt, material, frame / footprint) and its shape and DDS texture by content (session.py's OUT/sessions;
msgs.py). Exit 0: identical; 1: something differs; 2: tiles on one side only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msgs  # noqa: E402

if __name__ == "__main__":
    sys.exit(msgs.main('far', __doc__))
