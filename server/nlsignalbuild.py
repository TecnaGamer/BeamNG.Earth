"""Native junction planning over immutable server input snapshots."""
import json
from pathlib import Path

import numpy as np
import fast
import nlsignalassembly
import nlsignalcontext
import nlsignalinputs
import staging


def dumps(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':'))


def build(root, vfs, aid, frame, terrain, centre, tiles, graph_path, source, catalog, previous=None, name_key=''):
    graph_file = nlsignalinputs.owned(root, vfs, graph_path)
    if graph_file.stat().st_size > 32_000_000:
        raise ValueError('signal graph file too large')
    graph = json.loads(graph_file.read_text())
    half = 650.
    la, lo, _ = frame.enu_to_geodetic(np.array([centre[0]-half, centre[0]+half]),
                                     np.array([centre[1]-half, centre[1]+half]), 0.)
    context = source.snapshot((float(lo.min()), float(la.min()), float(lo.max()), float(la.max())))
    if context['built'] is None:
        return dict(pending=True, reason='context_unpublished_or_pending')
    records = context['records']
    points = [row['geometry'][0] for row in records if row['kind'] == 'signal']
    xy = []
    if points:
        ll = np.asarray(points)
        x, y, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
        xy = np.column_stack((x, y)).tolist()
    plan = json.loads(fast.native(300, 'native signal graph integration').nl_signal_graph_requests(dumps(graph), dumps(xy)))
    if not context['complete'] and not plan['groups']:
        return dict(pending=True, reason='context_tiles_pending')
    scene = nlsignalinputs.session_scene(root, vfs, aid, frame, terrain, centre, tiles)
    previous = previous or {}
    ready = {} if context['complete'] else dict(previous)
    parts, owners, status = [], [], []
    for group in plan['groups']:
        rows = group['approaches']
        req = [r['request'] for r in rows]
        queries = [[*r['stop'][:2], *r['travel'], r['lanes']] for r in req]
        ctx = nlsignalcontext.match(records, frame, queries, complete_wkd=context['complete'])
        zone = [dict(bibeko=1, bubeko=0, unknown=-1)[r['zone']] for r in ctx]
        exits = [r.get('road_class') == 'motorway_link' for r in ctx]
        if any(exits):
            zone.append(0)  # one compatible lens size for a shared junction bundle
        turns = [r.get('turns', ['through'] * q['lanes']) for r, q in zip(ctx, req)]
        names = [r['owner'] for r in rows]
        layout = [[r['lanes'],r['opposite']] for r in req]
        neighbours = [p for j,g in {**previous,**ready}.items() if j != group['junction'] for p in g['parts']]
        joint = nlsignalassembly.junction(scene, req, catalog, turns, [zone] * len(req), exits, names, neighbours)
        if not joint['ready']:
            status.append(dict(junction=group['junction'], pending=joint.get('retryable',False), reasons=[joint['reason']]))
            old = previous.get(group['junction'])
            if joint.get('retryable') and old and old['owners'] == names and old['layout'] == layout:
                ready[group['junction']] = old
            else:
                ready.pop(group['junction'],None)
            continue
        ready[group['junction']] = dict(parts=joint['assembly']['parts'],owners=names,
                                       layout=layout,stops=joint['assembly']['stops'])
    for j,group in sorted(ready.items()):
        parts.extend(group['parts']);owners.extend(group['owners'])
        signal = next((s for s in plan['signals'] if s['j'] == j),None)
        if signal is not None:
            for app, stop in zip(signal['app'], group['stops']):
                app[1:4] = stop
    graph['signals'] = plan['signals']
    graph['nlNative'] = True
    # A separate graph file never races a stock connection's shared navgraph.
    name = f'nl_{name_key}_{Path(graph_path).name}'
    path = Path(root) / name
    tmp = staging.tmp_for(str(path))
    with open(tmp, 'w') as f:
        f.write(dumps(graph))
    staging.replace(tmp, str(path))
    return dict(parts=parts, owners=owners, statuses=status, context=context['built'],
                graphPath=f'{vfs}/{name}', signalSignature=dumps(plan['signals']),groups=ready)
