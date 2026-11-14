#!/usr/bin/env python3
"""Join one-place runs (DIR/<label>/{net.log,places.jsonl}) into DIR/net.log and DIR/places.jsonl, in the order given,
so the ab_*.py scripts read them as one session.

    merge.py DIR label [label ...]
"""
import json
import os
import sys


def main():
    root, labels = sys.argv[1], sys.argv[2:]
    offset = 0
    with open(os.path.join(root, "net.log"), "w") as log, open(os.path.join(root, "places.jsonl"), "w") as places:
        for label in labels:
            with open(os.path.join(root, label, "net.log")) as f:
                lines = f.readlines()
            log.writelines(lines)
            with open(os.path.join(root, label, "places.jsonl")) as f:
                rec = json.loads(f.readline())
            rec["start_line"], rec["end_line"] = rec["start_line"] + offset, rec["end_line"] + offset
            places.write(json.dumps(rec) + "\n")
            offset += len(lines)


if __name__ == "__main__":
    main()
