"""Actual-game Dutch signals ABBA comparison at a parked, already loaded city.

Default prints the plan without contacting the game. Run later with
  python tools/benchmark_nl_signals.py --run --pid GAME_PID --out result.json
Uses the existing explicit BeamNG MCP endpoint, never searches other games.
Does not teleport, move the camera, change weather or start traffic. Foreground
focus must be retained by the user. No offline timing is reported as gameplay.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import statistics
import time

from benchmark import Game, check_info, readiness


def working_set(pid):
    if os.name != 'nt':
        fields=(Path('/proc')/str(pid)/'statm').read_text().split()
        return int(fields[1])*os.sysconf('SC_PAGE_SIZE')
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[
            (name,ctypes.c_size_t)for name in ('peak','working','peakPaged','paged',
                'peakNonPaged','nonPaged','pagefile','peakPagefile')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Memory),wintypes.DWORD]
    handle=kernel.OpenProcess(0x1000|0x10,False,pid)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    try:
        data=Memory();data.cb=ctypes.sizeof(data)
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(data),data.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return data.working
    finally:kernel.CloseHandle(handle)


def run(args):
    game=Game()
    if game.lua(Path(__file__).with_name('benchmark.lua').read_text())!='ok':
        raise RuntimeError('Benchmark helper did not load')
    def call(fn):return json.loads(game.lua('return BNGE_BENCH.'+fn+'()'))
    def audit():
        return json.loads(game.lua("local p=extensions.beamngEarth_perfmon;if p then p.tagHarness()end;"
            "return jsonEncode(require('beamngEarth/navgraph').signalAudit())"))
    def setting(on):
        game.lua("require('beamngEarth/settings').set('dutchSignals',"+str(on).lower()+")")
    info=call('info');bad=check_info(info)
    if bad:raise RuntimeError('; '.join(bad))
    player=info['player']
    if not (51.85<player['lat']<52.45 and 4.2<player['lon']<5.2):
        raise RuntimeError('Park in Rotterdam or Amsterdam before running this comparison')
    original=json.loads(game.lua("return jsonEncode(require('beamngEarth/settings').get('dutchSignals'))"))
    report=dict(format=1,kind='actual_game_ABBA',pid=args.pid,location=player,
                warmup_s=args.warmup,sample_s=args.seconds,phases=[])
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    def save():out.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    def stationary():
        current=call('info')
        pos=current.get('player')or{}
        if (current.get('speed')or 0)>1 or abs(pos.get('lat',0)-player['lat'])>.0001 \
                or abs(pos.get('lon',0)-player['lon'])>.0001:
            raise RuntimeError('Vehicle moved; comparison aborted without moving it back')
    try:
        for on in (False,True,True,False):
            setting(on);print('Signals',on,'warming up',flush=True)
            until=time.monotonic()+args.warmup
            while time.monotonic()<until:
                stationary();time.sleep(min(5,max(0,until-time.monotonic())))
            state=audit()
            if on and state.get('liveOwners',0)<1:
                raise RuntimeError('No native assemblies accepted; an on/off claim would be invalid')
            progress=call('progress')
            problems=readiness(progress,need_anchor=False)
            if problems:raise RuntimeError('Scene is not settled: '+'; '.join(problems))
            call('perfStart');samples=[];until=time.monotonic()+args.seconds
            while time.monotonic()<until:
                samples.append(working_set(args.pid));time.sleep(1)
            perf=call('perfRead');stationary()
            if not perf.get('focused'):raise RuntimeError('Game lost focus; frame timings invalid')
            report['phases'].append(dict(enabled=on,perf=perf,native=audit(),
                working_set_mean_bytes=statistics.mean(samples),working_set_peak_bytes=max(samples),
                progress=progress))
            save()
        report['complete']=True
    finally:
        try:setting(original)
        finally:
            if not info.get('perfmon'):game.lua("extensions.unload('beamngEarth_perfmon')")
            save()
    print('Saved',out)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true')
    p.add_argument('--pid',type=int)
    p.add_argument('--warmup',type=int,default=60)
    p.add_argument('--seconds',type=int,default=30)
    p.add_argument('--out',default='build/nl-signals-city-abba.json')
    a=p.parse_args()
    if not a.run:print('Plan: parked city; off/on/on/off; 60 s warm-up + 30 s capture each; restore setting. No game contacted.')
    elif not a.pid or a.warmup<30 or a.seconds<15:p.error('--pid and at least 30 s warm-up / 15 s capture required')
    else:run(a)
