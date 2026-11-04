#!/usr/bin/env python3
"""Live performance monitor for a BeamNG.Earth session.

Every INTERVAL s: frame stats from beamngEarth_perfmon, mod status, server CPU/RSS, new
server log lines. Every sample goes to a JSONL file; stdout (one line = one event) only
carries things worth a look: hitches, terrain-follow/rebase commits, backlogs, errors,
plus a summary every SUMMARY_S.

  python3 tools/perfmon.py --log <earthd.log> --out <samples.jsonl>
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INTERVAL = 2.0
TERRAIN_SLOW_MS = 1500


def _med(v):
    return sorted(v)[len(v) // 2] if v else 0


def far_q(st):
    return (st.get('far') or {}).get('queued')
SUMMARY_S = 60.0
HITCH_MS = 50.0

LUA_SAMPLE = r'''
if not beamngEarth_perfmon then extensions.load('beamngEarth_perfmon') end
local p = beamngEarth_perfmon and beamngEarth_perfmon.snapshot() or {}
local s = beamngEarth_core and loadstring('return ' .. beamngEarth_core.status())() or {}
local v = getPlayerVehicle(0)
-- P1b: ground heights at fixed world points around the player; after a terrain move the same
-- points are re-read (the v1.2 lattice should make them bit-identical).
local tb = scenetree.findObject('bngEarthTerrain')
local p1b = nil
if tb and v and s.block then
  local bk = tostring(s.block.cx) .. ',' .. tostring(s.block.cy)
  if BNGE_P1B and BNGE_P1B.bk ~= bk and BNGE_P1B.anchor == s.anchorId then
    local m, n = 0, 0
    for _, q in ipairs(BNGE_P1B.pts) do
      local h = tb:getHeight(vec3(q[1], q[2], 0))
      if h and q[3] then n = n + 1; m = math.max(m, math.abs(h - q[3])) end
    end
    p1b = { maxAbsDelta = m, n = n }
  end
  if not BNGE_P1B or BNGE_P1B.bk ~= bk or BNGE_P1B.anchor ~= s.anchorId or (BNGE_P1B.t or 0) + 4 < os.clock() then
    local c = v:getPosition(); local pts = {}
    for i = 1, 400 do
      local a, d = i * 2.39996, 20 + (i % 20) * 19
      local x, y = math.floor(c.x + d * math.cos(a)) + 0.37, math.floor(c.y + d * math.sin(a)) + 0.61
      pts[i] = { x, y, tb:getHeight(vec3(x, y, 0)) }
    end
    BNGE_P1B = { bk = bk, anchor = s.anchorId, pts = pts, t = os.clock() }
  end
end
local lim = Engine.getCurrentFpsLimitReason and Engine.getCurrentFpsLimitReason() or {}
return jsonEncode({ p1b = p1b, perf = p, focused = lim.windowFocused ~= false, st = { anchor = s.anchorId, block = s.block, net = s.net, status = s.status,
  far = s.far, followMs = s.lastFollowMs, rebaseMs = s.lastRebaseMs, pending = s.pending,
  player = s.player, rebaseDistance = s.rebaseDistance,
  guard = (function() local g = beamngEarth_core._state and (beamngEarth_core._state() or {}).guard
    return g and { mode = g.mode, paused = g.pausedByUs and true or false, healthy = g.healthy and true or false } or nil end)() },
  speed = v and v:getVelocity():length() or 0,
  damage = v and map and map.objects and map.objects[v:getId()] and map.objects[v:getId()].damage or 0 })
'''


def mcp_lua(code):
    r = subprocess.run([sys.executable, os.path.join(HERE, 'mcp.py'), 'lua', code],
                       capture_output=True, text=True, timeout=20)
    return json.loads(r.stdout.strip())


def find_server_pid():
    r = subprocess.run(['pgrep', '-f', 'server/earthd.py'], capture_output=True, text=True)
    pids = [int(x) for x in r.stdout.split()]
    if not pids:                    # (the launcher's own server process: launcher.py --server)
        r = subprocess.run(['pgrep', '-f', '[l]auncher.py --server'], capture_output=True, text=True)
        pids = [int(x) for x in r.stdout.split()]
    return pids[-1] if pids else None


def proc_stat(pid):
    """(cpu seconds, rss MB) from /proc."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            parts = f.read().rsplit(')', 1)[1].split()
        ticks = os.sysconf('SC_CLK_TCK')
        cpu = (int(parts[11]) + int(parts[12])) / ticks
        with open(f'/proc/{pid}/status') as f:
            rss = next(int(l.split()[1]) for l in f if l.startswith('VmRSS')) / 1024
        return cpu, rss
    except (OSError, StopIteration, IndexError, ValueError):
        return None, None


def emit(msg):
    print(time.strftime('%H:%M:%S ') + msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--quiet', action='store_true',
                    help='only unusual events: no routine terrain-follow lines, summaries every 5 min')
    a = ap.parse_args()
    global SUMMARY_S
    if a.quiet:
        SUMMARY_S = 300.0

    logf = open(a.log)
    logf.seek(0, 2)
    out = open(a.out, 'a')
    last = {}
    win = {'frames': 0, 'ms': 0.0, 'hitches': 0, 'max': 0.0, 'follow': 0, 'rebase': 0, 'tilesAdded': 0,
           'serverCpu': 0.0, 'errors': 0, 't0': time.time(), 'speedSum': 0.0, 'n': 0, 'dist': 0.0}
    pid = find_server_pid()
    cpu_prev = proc_stat(pid)[0] if pid else None
    t_prev = time.time()
    emit(f'monitor started (server pid {pid})')

    while True:
        time.sleep(INTERVAL)
        now = time.time()
        try:
            d = mcp_lua(LUA_SAMPLE)
        except Exception as e:  # game busy/loading/closed: report once per state change
            if last.get('mcp_ok', True):
                emit(f'game not responding to MCP ({type(e).__name__})')
            last['mcp_ok'] = False
            continue
        if not last.get('mcp_ok', True):
            emit('game responding again')
        last['mcp_ok'] = True

        p, st = d.get('perf', {}), d.get('st', {})
        if (d.get('speed') or 0) > 200:  # a rebase moves the car 10 km in one frame; not a real speed
            d['speed'] = last.get('speed', 0)
        d['speedDrop'] = max(0.0, last.get('speed', 0) - (d.get('speed') or 0))
        last['speed'] = d.get('speed') or 0
        dmg = d.get('damage') or 0
        d['damageDelta'] = max(0, dmg - last.get('damage', dmg))
        last['damage'] = dmg
        if not pid or not os.path.exists(f'/proc/{pid}'):
            was, pid = pid, find_server_pid()
            cpu_prev = None
            if pid != was:
                emit(f'server pid now {pid}')
        cpu, rss = proc_stat(pid) if pid else (None, None)
        cpu_pct = None
        if cpu is not None and cpu_prev is not None:
            cpu_pct = 100 * (cpu - cpu_prev) / max(1e-6, now - t_prev)
        cpu_prev, t_prev = cpu, now

        new_lines = logf.readlines()
        rec = {'t': now, 'perf': p, 'st': st, 'speed': d.get('speed'), 'serverCpu': cpu_pct, 'serverRssMB': rss,
               'serverLog': [l.rstrip() for l in new_lines if ' DEBUG ' not in l]}
        out.write(json.dumps(rec) + '\n')
        out.flush()

        # ---- events ----
        # (world gen speed: the terrain blocks the game waited on for TERRAIN_SLOW_MS or more)
        for l in new_lines:
            m_ = re.search(r'terrain_ready \(([^)]*)\) after (\d+) ms', l)
            if m_:
                win['ready'] = win.get('ready', []) + [int(m_.group(2))]
                if int(m_.group(2)) >= TERRAIN_SLOW_MS:
                    emit(f"slow terrain block {m_.group(1)}: ready after {int(m_.group(2))} ms "
                         f"(speed {d.get('speed', 0) * 2.237:.0f} mph)")
        # (the edge guard: warned near the loaded ground's edge, held there, or a rescue)
        gd = st.get('guard') or {}
        gmode = gd.get('mode')
        if gmode != last.get('gmode') and (gmode in ('warn', 'hold', 'stop', 'rescue') or gd.get('paused')
                                            or last.get('gmode') in ('warn', 'hold', 'stop', 'rescue')):
            emit(f"EDGE GUARD {last.get('gmode')} -> {gmode}{' (paused)' if gd.get('paused') else ''} at "
                 f"{d.get('speed', 0) * 2.237:.0f} mph | far q {far_q(st)} | {st.get('status')}")
            win['edge'] = win.get('edge', 0) + (1 if gmode in ('warn', 'hold', 'stop') else 0)
        last['gmode'] = gmode
        for l in new_lines:
            if re.search(r'WARNING|ERROR|Traceback|Exception', l):
                win['errors'] += 1
                emit('SERVER: ' + l.strip()[:220])
        far = st.get('far') or {}
        block = st.get('block') or {}
        bkey = (block.get('cx'), block.get('cy'))
        if last.get('block') not in (None, bkey) and st.get('anchor') == last.get('anchor'):
            win['follow'] += 1
            followed_now = True
            if d.get('p1b') and 0.002 < d['p1b']['maxAbsDelta'] < 20.0:   # (larger: points past the moved block)
                pb = d['p1b']
                emit(f"ground stability across terrain move: max height change {pb['maxAbsDelta'] * 1000:.2f} mm over {pb['n']} points"
                     + ("  OK" if pb['maxAbsDelta'] <= 0.002 else "  CHANGED"))
            if not a.quiet or (st.get('followMs') or 0) > 100:
              emit(f"terrain-follow -> {bkey}  commit {st.get('followMs') or 0:.0f} ms  "
                 f"(speed {d.get('speed', 0) * 2.237:.0f} mph)")
        if last.get('anchor') not in (None, st.get('anchor')):
            win['rebase'] += 1
            emit(f"REBASE anchor {last.get('anchor')} -> {st.get('anchor')}  commit {st.get('rebaseMs') or 0:.0f} ms, "
                 f"far live {far.get('live')} queued {far.get('queued')} old {far.get('old')}")
        if st.get('net') != last.get('net') and last.get('net') is not None:
            emit(f"connection: {last.get('net')} -> {st.get('net')}")
        if (far.get('queued') or 0) > 150 and not last.get('backlog'):
            emit(f"far-tile backlog: {far.get('queued')} queued (live {far.get('live')}, old {far.get('old')})")
            last['backlog'] = True
        elif (far.get('queued') or 0) < 20 and last.get('backlog'):
            emit('far-tile backlog cleared')
            last['backlog'] = False
        if p.get('slowSimFrames') and d.get('focused') and (p.get('simRatio') or 1) < 0.97:
            emit(f"TIME WARP: sim ran at {p['simRatio'] * 100:.0f}% of real time over the last {INTERVAL:.0f} s "
                 f"({p['slowSimFrames']} slow frames, worst {p.get('minSimRatio', 1) * 100:.0f}%) | fps {p.get('fps', 0):.0f} "
                 f"| worst frame {p.get('maxMs', 0):.0f} ms | far q {far.get('queued')}")
        routine = a.quiet and locals().get('followed_now') and (p.get('maxMs') or 0) < 110 and (p.get('over50') or 0) <= 2
        followed_now = False
        if (p.get('over50') or 0) > 0 and d.get('focused') and not routine:
            worst = ', '.join(f"{w[0]:.0f}" for w in (p.get('worst') or [])[:3])
            crash = (f" | likely CRASH (speed -{d['speedDrop'] * 2.237:.0f} mph)"
                     if d.get('speedDrop', 0) > 12 else '')
            emit(f"hitch: {p['over50']} frame(s) >50 ms (worst {worst} ms){crash} | fps {p.get('fps', 0):.0f} "
                 f"| far q {far.get('queued')} | {st.get('status')}")
        if d.get('focused') != last.get('focused', True):
            emit('game window ' + ('focused again' if d.get('focused') else
                 'lost focus (BeamNG caps at 30 fps in the background; fps alerts paused)'))
        last['focused'] = d.get('focused')
        if p.get('frames') and p.get('fps', 99) < 30 and d.get('focused'):
            emit(f"low fps {p['fps']:.0f} (p95 {p.get('p95', 0):.0f} ms) | far live {far.get('live')} q {far.get('queued')}")

        last['block'], last['anchor'], last['net'] = bkey, st.get('anchor'), st.get('net')
        win['frames'] += p.get('frames') or 0
        win['ms'] += (p.get('avgMs') or 0) * (p.get('frames') or 0)
        win['hitches'] += p.get('over50') or 0
        win['max'] = max(win['max'], p.get('maxMs') or 0)
        win['serverCpu'] += cpu_pct or 0
        win['speedSum'] += d.get('speed') or 0
        win['n'] += 1
        win['dist'] += (d.get('speed') or 0) * INTERVAL

        idle = win['dist'] < 1 and not win['follow'] and not win['rebase'] and not win['errors'] and not d.get('focused')
        if now - win['t0'] >= SUMMARY_S and idle:
            win.update({'ready': [], 'edge': 0})
            win.update({'frames': 0, 'ms': 0.0, 'hitches': 0, 'max': 0.0, 'follow': 0, 'rebase': 0,
                        'serverCpu': 0.0, 'errors': 0, 't0': now, 'speedSum': 0.0, 'n': 0, 'dist': 0.0})
        if now - win['t0'] >= SUMMARY_S:
            fps = win['frames'] / (win['ms'] / 1000) if win['ms'] else 0
            emit(f"SUMMARY {now - win['t0']:.0f}s: avg {fps:.0f} fps, worst frame {win['max']:.0f} ms, "
                 f"{win['hitches']} hitch(es) >50 ms, {win['follow']} terrain-follow, {win['rebase']} rebase, "
                 f"~{win['dist'] / 1609.3:.1f} mi at avg {win['speedSum'] / max(1, win['n']) * 2.237:.0f} mph, "
                 f"terrain ready median/max {_med(win.get('ready'))}/{max(win.get('ready') or [0])} ms "
                 f"({len(win.get('ready') or [])} blocks), {win.get('edge', 0)} edge warning(s), "
                 f"server cpu {win['serverCpu'] / max(1, win['n']):.0f}% rss {rss or 0:.0f} MB, "
                 f"far live {far.get('live')}, errors {win['errors']}")
            win.update({'ready': [], 'edge': 0})
            win.update({'frames': 0, 'ms': 0.0, 'hitches': 0, 'max': 0.0, 'follow': 0, 'rebase': 0,
                        'serverCpu': 0.0, 'errors': 0, 't0': now, 'speedSum': 0.0, 'n': 0, 'dist': 0.0})


if __name__ == '__main__':
    main()
