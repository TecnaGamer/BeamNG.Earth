"""wholesys.py: the whole machine during a run — CPU, memory, disk and GPU per server role and the game.

  wholesys.py record OUT.jsonl [--period 5] [--seconds 3600]   (until killed or --seconds)
  wholesys.py summary OUT.jsonl [--from S] [--to S]

Roles come from the server workers' process names (pools.proc_name: bnge:roadtile, bnge:block, ...); the server's
main process is "server", other children (helpers' threads are not processes) "server:other". Per role: CPU seconds
used (utime + stime, all its processes), PSS (MB, the shared mappings split fairly; the game: RSS only, its smaps
are big and reading them takes its mmap lock), bytes read / written to storage (/proc/<pid>/io: read_bytes /
write_bytes, what reached the block layer). System: MemAvailable, swap used, GPU busy % and VRAM (nvidia-smi or
amdgpu sysfs), the disks' sectors written.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import autodrive_perf  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def proc(pid, pss=True):
    st = _read(f"/proc/{pid}/stat")
    if not st:
        return None
    fields = st.rsplit(")", 1)[1].split()
    out = {"cpu": (int(fields[11]) + int(fields[12])) / TICK, "comm": _read(f"/proc/{pid}/comm").strip()}
    io = dict(line.split(": ") for line in _read(f"/proc/{pid}/io").splitlines() if ": " in line)
    out["rd"], out["wr"] = int(io.get("read_bytes", 0)), int(io.get("write_bytes", 0))
    if pss:
        for line in _read(f"/proc/{pid}/smaps_rollup").splitlines():
            k, _, v = line.partition(":")
            if k == "Pss":
                out["mem"] = int(v.split()[0]) / 1024
    else:
        statm = _read(f"/proc/{pid}/statm").split()
        out["mem"] = int(statm[1]) * os.sysconf("SC_PAGE_SIZE") / 2 ** 20 if statm else 0
    return out


def gpu():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        busy, vram = r.stdout.strip().splitlines()[0].split(",")
        return {"busy": float(busy), "vramMB": float(vram)}
    except Exception:
        pass
    for card in sorted(os.listdir("/sys/class/drm")):
        busy = _read(f"/sys/class/drm/{card}/device/gpu_busy_percent").strip()
        if busy:
            vram = _read(f"/sys/class/drm/{card}/device/mem_info_vram_used").strip()
            return {"busy": float(busy), "vramMB": int(vram or 0) / 2 ** 20}
    return None


def system():
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0]) / 1024
    sectors = 0
    for line in _read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) > 9 and (f[2].startswith("nvme") and "p" not in f[2][4:] or f[2] in ("sda", "sdb", "vda")):
            sectors += int(f[9])
    return {"availMB": mem.get("MemAvailable", 0), "swapMB": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            "diskWrMB": sectors * 512 / 2 ** 20, "gpu": gpu()}


def role(pid, comm, main):
    if pid in main:
        return "server"
    return comm.split(":", 1)[1] if comm.startswith("bnge:") else "server:other"


def record(out, period, seconds):
    sampler = autodrive_perf.ProcSampler()
    t0 = time.time()
    with open(out, "a") as f:
        while time.time() - t0 < seconds:
            groups = sampler.groups()
            procs = autodrive_perf._procs()
            server = set(groups.get("server", []))
            main = {p for p in server if procs.get(p, (None,))[0] not in server}
            row = {"t": round(time.time() - t0, 1), "sys": system(), "roles": {}}
            for name, pids in (("server", sorted(server)), ("game", groups.get("game", []))):
                for p in pids:
                    s = proc(p, pss=name == "server")
                    if s is None:
                        continue
                    r = "game" if name == "game" else role(p, s["comm"], main)
                    agg = row["roles"].setdefault(r, {"n": 0, "mem": 0.0, "p": {}})
                    agg["n"] += 1
                    agg["mem"] += s.get("mem", 0)
                    agg["p"][str(p)] = [round(s["cpu"], 2), s["rd"], s["wr"]]
            f.write(json.dumps(row) + "\n")
            f.flush()
            time.sleep(period)


def summary(path, t_from, t_to):
    rows = [json.loads(line) for line in open(path) if line.strip()]
    rows = [r for r in rows if t_from <= r["t"] <= t_to]
    if len(rows) < 2:
        sys.exit("fewer than 2 samples")
    a, b = rows[0], rows[-1]
    dt = b["t"] - a["t"]
    print(f"{len(rows)} samples over {dt:.0f} s")
    print(f"{'role':14s} {'procs':>5s} {'cpu s':>8s} {'cores':>6s} {'mem MB':>8s} {'peak':>8s} {'read MB':>8s} {'wr MB':>8s}")
    names = sorted({k for r in rows for k in r["roles"]}, key=lambda k: -b["roles"].get(k, {}).get("cpu", 0))
    tot = [0.0, 0.0, 0.0, 0.0]
    for k in names:
        # per process: its last counters less its first (0 for one started inside the window), so a process that
        # started, ended or was restarted inside it counts what it used there
        first, lastv = {}, {}
        for i, r in enumerate(rows):
            for pid, v in r["roles"].get(k, {}).get("p", {}).items():
                first.setdefault(pid, v if i == 0 else [0, 0, 0])
                lastv[pid] = v
        cpu, rd, wr = (sum(lastv[pid][j] - first[pid][j] for pid in lastv) for j in range(3))
        gb = b["roles"].get(k)
        peak = max(r["roles"].get(k, {}).get("mem", 0) for r in rows)
        mem = (gb or {}).get("mem", 0)
        n = (gb or {}).get("n", 0)
        print(f"{k:14s} {n:5d} {cpu:8.1f} {cpu / dt:6.2f} {mem:8.0f} {peak:8.0f} {rd / 2**20:8.1f} {wr / 2**20:8.1f}")
        if k != "game":
            tot[0] += cpu
            tot[1] += mem
            tot[2] += rd
            tot[3] += wr
    print(f"{'server total':14s} {'':5s} {tot[0]:8.1f} {tot[0] / dt:6.2f} {tot[1]:8.0f} {'':8s} "
          f"{tot[2] / 2**20:8.1f} {tot[3] / 2**20:8.1f}")
    sa, sb = a["sys"], b["sys"]
    av = [r["sys"]["availMB"] for r in rows]
    sw = [r["sys"]["swapMB"] for r in rows]
    g = [r["sys"]["gpu"] for r in rows if r["sys"].get("gpu")]
    print(f"system: avail min {min(av):.0f} MB (start {av[0]:.0f}), swap max {max(sw):.0f} MB, "
          f"disk written {sb['diskWrMB'] - sa['diskWrMB']:.0f} MB ({(sb['diskWrMB'] - sa['diskWrMB']) / dt * 3600 / 1024:.1f} GB/h)")
    if g:
        busy = sorted(x["busy"] for x in g)
        print(f"gpu: busy median {busy[len(busy) // 2]:.0f} % p90 {busy[int(len(busy) * 0.9)]:.0f} %, "
              f"vram max {max(x['vramMB'] for x in g):.0f} MB")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("out")
    r.add_argument("--period", type=float, default=5.0)
    r.add_argument("--seconds", type=float, default=3600.0)
    s = sub.add_parser("summary")
    s.add_argument("path")
    s.add_argument("--from", dest="t_from", type=float, default=0.0)
    s.add_argument("--to", dest="t_to", type=float, default=1e12)
    a = ap.parse_args()
    if a.cmd == "record":
        record(a.out, a.period, a.seconds)
    else:
        summary(a.path, a.t_from, a.t_to)


if __name__ == "__main__":
    main()
