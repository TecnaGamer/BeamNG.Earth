#!/usr/bin/env python3
"""hostreplay.py: the game's side of a recorded session played again against a world server (the Rust port's 1c: a
server run with BNGE_HOST=on BNGE_HOST_RECORD=DIR writes DIR/<start>-<connection>.in.zst, every frame the game sent).

    python3 tools/hostreplay.py RECORDING.in.zst PORT OUT.jsonl [--speed 1.0 | --fast] [--linger 5]

Connects to 127.0.0.1:PORT, sends each recorded frame at its recorded time (--speed 2: twice as fast; --fast: as fast
as the socket takes them), reads everything the server sends meanwhile and writes it to OUT.jsonl, one line per
frame: {"t": seconds since connecting, "m": the message}. It stops --linger seconds after the last frame was sent.
--dump prints the recording's frames instead (no server). Poses sent faster than they came meet in one batch and a
BNGE_HOST=on server merges them (1b): run it with BNGE_HOST_MERGE=off to have each one handled.

Recordings hold the player's positions: keep them private (never in a report, never uploaded)."""
import argparse
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import fast  # noqa: E402
import protocol  # noqa: E402

RECORD_VERSION = 371            # the earthfast that reads recordings (host_read_recording)


def records(path):
    fast.require(RECORD_VERSION, "hostreplay (host_read_recording)")
    import earthfast
    return earthfast.host_read_recording(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("recording")
    ap.add_argument("port", nargs="?", type=int)
    ap.add_argument("out", nargs="?")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--linger", type=float, default=5.0)
    ap.add_argument("--dump", action="store_true")
    a = ap.parse_args(argv)
    recs = records(a.recording)
    if a.dump:
        for ns, data in recs:
            print(f"{ns / 1e9:10.3f} {data[:200].decode('utf-8', 'replace')}")
        return 0
    if a.port is None or a.out is None:
        ap.error("PORT and OUT are needed (or --dump)")
    s = socket.create_connection(("127.0.0.1", a.port), timeout=30)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    t0 = time.monotonic()
    got = []
    done = threading.Event()

    def read():
        dec = protocol.Decoder()
        s.settimeout(0.5)
        while not done.is_set():
            try:
                b = s.recv(1 << 20)
            except socket.timeout:
                continue
            except OSError:
                break
            if not b:
                break
            for m in dec.feed(b):
                got.append((time.monotonic() - t0, m))
    rd = threading.Thread(target=read, daemon=True)
    rd.start()
    for ns, data in recs:
        if not a.fast:
            wait = ns / 1e9 / a.speed - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)
        s.sendall(protocol.HEADER.pack(len(data)) + data)
    time.sleep(a.linger)
    done.set()
    rd.join(2)
    s.close()
    with open(a.out, "w", encoding="utf-8") as f:
        for t, m in got:
            f.write(json.dumps({"t": round(t, 4), "m": m}, ensure_ascii=False) + "\n")
    print(f"{len(recs)} frames sent, {len(got)} received -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
