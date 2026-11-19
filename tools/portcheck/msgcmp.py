#!/usr/bin/env python3
"""msgcmp.py A B [--places a,b]: two recorded sessions' messages, all of them: each message's last copy by what it
is about, its fields and the files it names by content, timing and send-order fields left out, progress-only
messages skipped, the far field's swap bookkeeping counted (session.py's OUT/sessions; msgs.py). The phase-1 host must
leave this identical. Exit 0: identical; 1: something differs; 2: messages on one side only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import msgs  # noqa: E402

if __name__ == "__main__":
    sys.exit(msgs.main(None, __doc__))
