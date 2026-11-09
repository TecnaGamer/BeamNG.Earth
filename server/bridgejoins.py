"""Imported bridge entrances: shared C1 road profiles for terrain and retained procedural approaches.

Plans are made in the model's geographic frame from the unmodified road grader. Never feed the resulting
ramps back into bridge placement; that would move the height target each time another tile is built.
"""
import logging
import math
import threading

import numpy as np

log = logging.getLogger("earthd.bridgejoins")
STEP = 2.0
MAX_RUN = 180.0
MAX_CHANGE = 8.0


def nearest(line, xy):
    """Nearest polyline segment, fraction, distance and arc length for each point."""
    line, xy = np.asarray(line, float), np.asarray(xy, float).reshape(-1, 2)
    ab = np.diff(line, axis=0)
    length = np.linalg.norm(ab, axis=1)
    lengths = np.r_[0., np.cumsum(length)]
    den = np.maximum(length ** 2, 1e-12)
    t = np.clip(np.einsum('nsi,si->ns', xy[:, None] - line[:-1], ab) / den, 0, 1)
    d = np.linalg.norm(xy[:, None] - line[:-1] - t[..., None] * ab, axis=2)
    i = d.argmin(1)
    k = np.arange(len(xy))
    return i, t[k, i], d[k, i], lengths[i] + t[k, i] * length[i]


def surface_height(surf, xy):
    xy = np.asarray(xy, float).reshape(-1, 2)
    u, v = xy[:, surf['ax']], xy[:, 1 - surf['ax']]
    i = np.rint((u - surf['u0']) / surf['du']).astype(int)
    j = np.rint((v - surf['v0']) / surf['dv']).astype(int)
    out = np.full(len(xy), np.nan)
    z = surf['z']
    ok = (i >= 0) & (i < z.shape[0]) & (j >= 0) & (j < z.shape[1])
    out[ok] = z[i[ok], j[ok]]
    return out


class Ramp:
    def __init__(self, xy, z0, slope0, base_z, half, terrain_start=0., protected=(), reference=None):
        import grading
        self.xy, s = grading.resample(np.asarray(xy, float), STEP)
        self.s = s
        self.half = float(half)
        self.protected = list(protected)
        self.reference = reference
        base = np.asarray(base_z(self.xy), float)
        h, L = base[-1], s[-1]
        tail = s >= s[-1]-12.
        slope1 = float(np.clip(np.polyfit(s[tail], base[tail], 1)[0], -.065, .065))
        t = s / L
        self.z = ((2*t**3 - 3*t**2 + 1)*z0 + (t**3 - 2*t**2 + t)*L*slope0
                  + (-2*t**3 + 3*t**2)*h + (t**3 - t**2)*L*slope1)
        self.terrain_start = max(0., float(terrain_start))
        # An abutment's embankment blends into the last few metres beneath the retained connector.
        # Starting a several-metre fill at a hard plane excavated a cliff across Narrows' lower road.
        start = min(max(0., self.terrain_start-12.), L)
        ss = np.r_[start, s[s > start]]
        q = np.c_[np.interp(ss, s, self.xy[:, 0]), np.interp(ss, s, self.xy[:, 1])]
        self.plan = grading.Plan(q, np.interp(ss, s, self.z), self.half + 1., road_half=self.half)
        self.pack = grading.PlanPack([self.plan])

    def fit(self, xy, z, terrain=False, lift=0., core_only=False, native=None):
        import grading
        xy, z = np.asarray(xy, float).reshape(-1, 2), np.asarray(z, float)
        pad = self.half + grading.T_MAX + 2.
        near = ((xy >= self.xy.min(0)-pad) & (xy <= self.xy.max(0)+pad)).all(1)
        if not near.any():
            return z
        if not near.all():
            out = z.copy()
            out[near] = self.fit(xy[near], z[near], terrain, lift, core_only,
                                 None if native is None else np.asarray(native)[near])
            return out
        if terrain and getattr(self, "elevated", False):
            return z.copy()
        if terrain:
            out = z.copy() if core_only else grading.grade_points(xy[:, 0], xy[:, 1], z, self.pack, lift)
            _, _, d, s = nearest(self.xy, xy)
            core = d <= self.half
            out[core] = np.interp(s[core], self.s, self.z) + lift
            d0 = self.xy[1]-self.xy[0]
            d1 = self.xy[-1]-self.xy[-2]
            start = (xy-self.xy[0]) @ (d0/np.linalg.norm(d0))
            end = (xy-self.xy[-1]) @ (d1/np.linalg.norm(d1))
            fade_start=max(0.,self.terrain_start-12.)
            if self.terrain_start>fade_start:
                t=np.clip((start-fade_start)/(self.terrain_start-fade_start),0.,1.)
                weight=t*t*(3.-2.*t)
                out=z+(out-z)*weight
            out[(start < fade_start) | (end > 0.)] = z[(start < fade_start) | (end > 0.)]
            if core_only:
                out[start < self.terrain_start]=z[start < self.terrain_start]
        else:
            _, _, d, s = nearest(self.xy, xy)
            # Never drag a far tile onto a clamped endpoint of this short polyline.
            along = np.einsum('ij,j->i', xy - self.xy[0], (self.xy[1]-self.xy[0]) / np.linalg.norm(self.xy[1]-self.xy[0]))
            weight = np.clip((self.half + 2. - d) / 2., 0., 1.)
            weight *= (along >= -4.) & (s < self.s[-1] - .01)
            reference = z
            if self.reference is not None:
                line, levels = self.reference
                i, t, _, _ = nearest(line, xy)
                reference = levels[i]*(1.-t) + levels[i+1]*t
            out = z + weight * (np.interp(s, self.s, self.z) + lift - reference)
        # Preserve lower roads with a C1 transition back to the native surface. A hard restore at the
        # carriageway edge cut a vertical wall into Narrows' underpass when the fill was several metres high.
        for p in self.protected if terrain else ():
            i, t, d, _ = nearest(p.xy, xy)
            # A mapped crossing profile can carry its upper level. Preserve the already graded physical
            # road, not that profile's nominal elevation, and always use the original pre-bridge surface.
            protected_base=z if native is None else np.asarray(native)
            difference = np.abs(out-protected_base)
            width = np.maximum(8., difference*3.)
            t = np.clip((d-p.road_half-2.)/width,0.,1.)
            weight = t*t*(3.-2.*t)
            # Avoid a second discontinuity at the old 1.5 m height-difference threshold.
            strength = np.clip((difference-.5)/1.,0.,1.)
            strength = strength*strength*(3.-2.*strength)
            out = protected_base + (out-protected_base)*(1.-strength*(1.-weight))
        return out


def approach_crossing_mode(ramp, crossings, base_z, mapped_z):
    """Avoid placing a new deck over a crossing without conservative headroom.

    Uncertain cases leave the approach untouched; this test samples the crossing
    centerline and is deliberately not a guarantee of vehicle clearance.
    """
    import grading
    if not np.isfinite(mapped_z) or abs(ramp.z[0] - mapped_z) <= 1.5:
        return "ground"
    gaps = []
    for crossing in crossings:
        dense, _ = grading.resample(np.asarray(crossing.xy, float), 4.)
        _, _, distance, station = nearest(ramp.xy, dense)
        near = distance <= ramp.half + crossing.road_half + 1.
        if near.any():
            heights = np.asarray(base_z(dense[near]), float)
            clearance = np.interp(station[near], ramp.s, ramp.z) - heights
            if not np.isfinite(clearance).all():
                return "unsafe"
            gaps.extend(clearance.tolist())
    if not gaps or max(gaps) <= 2.5:
        return "ground"
    # The rendered deck is 0.35 m deep; leave at least six metres above
    # sampled ground. A mixed-level overlap is ambiguous, so skip the ramp.
    return "elevated" if min(gaps) >= 6.35 else "unsafe"


def make_ramps(surf, parts, plans, base_z):
    """Match each motor carriageway to its actual model edge and connected ground-road plan."""
    import threedmr
    import grading
    rows = np.flatnonzero(np.isfinite(surf['z']).any(1))
    bounds = surf['u0'] + surf['du'] * rows[[0, -1]]
    ax = surf['ax']
    ramps = []
    for _fid, spec, xy, _z, _lon in parts:
        if getattr(spec,'cls','') in {'footway','cycleway','pedestrian','path','steps','bridleway'}:
            continue
        xy = np.asarray(xy, float)
        part_z = np.asarray(_z, float)
        if len(xy) < 2 or not threedmr.under_footprint(threedmr.bridge_footprint_of(surf), (0., 0.), xy).any():
            continue
        if xy[0, ax] > xy[-1, ax]:
            xy = xy[::-1]
            part_z = part_z[::-1]
        us = np.arange(bounds[0], bounds[1]+.01, surf['du'])
        trace = np.c_[np.interp(us, xy[:, ax], xy[:, 0]), np.interp(us, xy[:, ax], xy[:, 1])]
        trace[:, ax] = us
        for side, outside in ((0, us < xy[0, ax]), (-1, us > xy[-1, ax])):
            a, b = (xy[0], xy[1]) if side == 0 else (xy[-1], xy[-2])
            if abs(a[ax]-b[ax]) > .01:
                trace[outside, 1-ax] = a[1-ax] + (us[outside]-a[ax])*(a[1-ax]-b[1-ax])/(a[ax]-b[ax])
        heights = surface_height(surf, trace)
        valid = np.flatnonzero(np.isfinite(heights))
        if len(valid) < 8:
            continue
        for end, edge_index in enumerate((valid[0], valid[-1])):
            p, q = (xy[0], xy[1]) if end == 0 else (xy[-1], xy[-2])
            direction = p-q
            if abs(direction[ax]) < .2:
                continue
            direction /= np.linalg.norm(direction)
            edge = trace[edge_index]
            # Use the traced cross-section at this carriageway, not the median of both road decks/railway.
            h0 = surface_height(surf, [edge])[0]
            mapped_z = float(part_z[0 if end == 0 else -1])
            overhang = float((edge - p) @ direction)
            # If the model already reaches beyond the mapped abutment at essentially
            # the same elevation, keep the native ground-road grade. A long model-led
            # terrain ramp here can create a bump on nearby lower carriageways.
            if overhang >= 1. and np.isfinite(h0) and abs(h0 - mapped_z) <= .35:
                continue
            ds = np.arange(6., 39., 4.)
            hh = surface_height(surf, edge - ds[:, None]*direction)
            ok = np.isfinite(hh)
            if not np.isfinite(h0) or ok.sum() < 4:
                log.debug('no interior road surface at %s', edge)
                continue
            slope0 = float(np.clip(-np.polyfit(ds[ok], hh[ok], 2)[1], -.065, .065))
            matches = []
            for pl in plans:
                for k in (0, -1):
                    if not pl.abut[0 if k == 0 else 1]:
                        continue
                    if pl.road_half < max(1.5, spec.width*.25):
                        continue
                    a = np.asarray(pl.xy if k == 0 else pl.xy[::-1], float)
                    delta = a[min(2, len(a)-1)]-a[0]
                    norm = np.linalg.norm(delta)
                    if norm < .01:
                        continue
                    alignment = float(delta @ direction / norm)
                    dist = float(np.linalg.norm(a[0]-p))
                    if alignment > .75 and dist < max(12., spec.width):
                        matches.append((dist + 20.*(1.-alignment), pl, a))
            if not matches:
                log.debug('no connected ground road at mapped end %s (edge %s, plans %s)', p, edge, len(plans))
                continue
            _, selected, road = min(matches, key=lambda x:x[0])
            allowed = {selected.rid}
            # A ground road may be split at a junction or cell edge a few metres from the abutment.
            # Follow its connected continuation before choosing the outer join; never cram the fill into that stub.
            for _ in range(16):
                if np.linalg.norm(np.diff(road, axis=0), axis=1).sum() >= MAX_RUN:
                    break
                tail = road[-1]
                heading = (road[-1]-road[-2]) / max(np.linalg.norm(road[-1]-road[-2]), .01)
                nexts = []
                for pl in plans:
                    if pl.rid in allowed:
                        continue
                    for k in (0, -1):
                        a = np.asarray(pl.xy if k == 0 else pl.xy[::-1], float)
                        delta = a[min(2, len(a)-1)]-a[0]
                        length = np.linalg.norm(delta)
                        dist = np.linalg.norm(a[0]-tail)
                        if length > .01 and dist < 4. and delta @ heading / length > .8:
                            nexts.append((dist + abs(pl.road_half-selected.road_half), pl, a))
                if not nexts:
                    break
                _, pl, a = min(nexts, key=lambda item:item[0])
                allowed.add(pl.rid)
                road = np.vstack([road, a[1:]])
            road = road[((road-edge) @ direction) > 1.]
            if not len(road):
                continue
            route, s = grading.resample(np.vstack([edge, road]), STEP)
            # The model can finish over water before the mapped abutment. Its sea floor is not the height
            # of the road we're joining; use the connected ground-road plan's abutment height instead.
            base0 = float(selected.z[np.linalg.norm(selected.xy-p, axis=1).argmin()])
            if abs(h0-base0) > MAX_CHANGE:
                log.warning('model entrance %.1f m off ground; refusing an extreme fill', h0-base0)
                continue
            mapped_start = max(0., float((p-edge) @ direction))
            run = min(MAX_RUN, mapped_start + max(50., abs(h0-base0)/.025, abs(slope0)*800.))
            run = min(run, s[-1])
            if run < 15.:
                continue
            ss = np.r_[s[s < run], run]
            route = np.c_[np.interp(ss, s, route[:, 0]), np.interp(ss, s, route[:, 1])]
            protected = []
            for pl in plans:
                if pl.rid in allowed or len(pl.xy) < 2:
                    continue
                pad = spec.width/2. + grading.T_MAX + 2.
                if not ((pl.xy.max(0) >= route.min(0)-pad) & (pl.xy.min(0) <= route.max(0)+pad)).all():
                    continue
                delta = pl.xy[-1]-pl.xy[0]
                cosine = abs(float(delta @ direction)) / max(np.linalg.norm(delta), .01)
                if cosine < .8 or pl.road_half < spec.width*.25:
                    protected.append(pl)
            ramp = Ramp(route, h0, slope0, base_z, spec.width/2., mapped_start, protected, (xy, part_z))
            mode = approach_crossing_mode(ramp, protected, base_z, mapped_z)
            if mode == "unsafe":
                log.warning('model approach overlaps a crossing without verified clearance; leaving it unchanged')
                continue
            ramp.elevated = mode == "elevated"
            ramp.spec, ramp.fid, ramp.lon = spec, _fid, _lon
            ramps.append(ramp)
    return ramps


class Context:
    def __init__(self, grader, cache_dir, raw):
        self.grader, self.cache_dir, self.raw = grader, cache_dir, raw
        self.models, self.regions = {}, {}
        self.lock = threading.RLock()

    def _load(self, mid, lat, lon):
        import buildings
        import geodesy
        import grading
        import roads
        import threedmr
        frame = geodesy.EnuFrame(lat, lon)
        c = {'id': mid, 'xy': (0., 0.), 'latlon': (lat, lon)}
        pos, _uv, tris, _n, textures, tm = buildings._model_mesh(c, self.cache_dir)
        if not buildings._is_bridge(pos, buildings._bridge_info(c, self.cache_dir)):
            return None
        pos = buildings._centred(pos)
        asphalt = [i for i,t in enumerate(textures) if len(t) > 6 and str(t[6]).strip().casefold() == 'asphalt']
        if not asphalt:
            # A generic exposed floor may be a plaza, pier cap or an authored ramp. Only grade terrain
            # against an explicit road surface; this preserves existing imports such as Dragon Bridge.
            return None
        surf = threedmr.bridge_deck_surface(pos, tris)
        if surf is None:
            return None
        view = grading.Grader(frame, self.grader.md, self.grader.elev, self.grader.widths, store=self.grader.store)

        def base(la, lo):
            return view.apply_base(la, lo, self.raw(la, lo), grading.ROAD_LIFT)

        def base_z(xy):
            xy = np.asarray(xy, float).reshape(-1, 2)
            la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
            return base(la, lo)
        cell_fn = lambda k: view.md.cell(k, layers=('roads',), kind='demand').layers.get('roads')
        profiles = roads.BridgeProfiles(frame, cell_fn, base)
        profiles.wet = getattr(self.grader, 'wet', None)
        profiles.infra_fn = lambda k: view.md.cell(k, layers=('infrastructure',), kind='demand').layers.get('infrastructure')
        reach = float(np.abs(pos[:, :2]).max()) + MAX_RUN + 50.
        keys = roads.cells_of_tile(frame, 0, 0, margin=reach)
        cells = [cell_fn(key) for key in keys]
        parts = list(profiles.parts(cells, (-reach, -reach, reach, reach)))
        fp = dict(threedmr.bridge_footprint_of(surf), bridge=True)
        ends = buildings.bridge_road_ends([(fp, (0., 0.), mid)], profiles, cells).get(mid)
        if not ends:
            return None  # a footbridge has no motor-road profile to reshape
        gz = float(base_z([[0., 0.]])[0])
        surf = {**surf, 'z': surf['z'] + gz - .2 - float(pos[:, 2].min())}
        tilt = threedmr.bridge_tilt(surf, lambda x,y: float(base_z([[x,y]])[0]), mid, ends)
        if asphalt:
            # Separate carriageways can differ in elevation (Mount Henry by 0.7 m). With asphalt-only
            # triangles there are no low pier caps/plinths to reject at such a cross-section change.
            road_surf = threedmr.bridge_deck_surface(pos, tris[np.isin(tm, asphalt)], step_m=1.5,
                                                    tier_below=2., tier_above=2.)
            if road_surf is not None:
                surf = {**road_surf, 'z': road_surf['z'] + gz - .2 - float(pos[:, 2].min())}
        u = surf['u0'] + surf['du']*np.arange(len(surf['z']))
        surf['z'] = surf['z'] + (tilt[2] + tilt[3]*(u-tilt[1]))[:, None]
        plans, seen = [], set()
        for key in keys:
            for fid, pl in view._cell_plans(key):
                k = (str(fid), tuple(np.round(pl.xy[0], 2)), tuple(np.round(pl.xy[-1], 2)))
                if k not in seen:
                    seen.add(k);plans.append(pl)
        ramps = make_ramps(surf, parts, plans, base_z)
        log.info('3dmr model %s: %d smooth entrance profile(s)', mid, len(ramps))
        return frame, ramps

    def near(self, lat, lon):
        import threedmr
        la, lo = np.asarray(lat, float), np.asarray(lon, float)
        if not la.size:
            return []
        centre = (float(np.mean(la)), float(np.mean(lo)))
        region = (math.floor(centre[0]/threedmr.REGION_DEG), math.floor(centre[1]/threedmr.REGION_DEG))
        with self.lock:
            if region not in self.regions:
                import revisit
                faults = revisit.thread_faults()
                ids = threedmr.region_ids(*centre, self.cache_dir)
                if revisit.thread_faults() != faults:
                    return []
                self.regions[region] = ids
            out = []
            reach = 2400. + float(np.ptp(la))*111320.
            for mid in self.regions[region]:
                mid = int(mid)
                try:
                    mlat, mlon, _ = threedmr.model_position(mid, self.cache_dir)
                    distance = math.hypot((centre[0]-mlat)*111320., (centre[1]-mlon)*111320.*math.cos(math.radians(mlat)))
                    if distance > reach:
                        continue
                    if mid not in self.models:
                        import revisit
                        faults = revisit.thread_faults()
                        loaded = self._load(mid, mlat, mlon)
                        if revisit.thread_faults() != faults:
                            continue  # retry incomplete map/model data rather than caching a missing entrance
                        self.models[mid] = loaded
                    if self.models[mid] is not None:
                        out.append(self.models[mid])
                except Exception:
                    import revisit
                    revisit.fault()
                    log.exception('3dmr model %s: entrance grading failed', mid)
            return out

    def apply(self, lat, lon, base, terrain=True, height_offset=0.):
        import threedmr
        if not threedmr.enabled():
            return base
        shape = np.shape(base)
        out = np.asarray(base, float).ravel().copy()
        native=out.copy()
        for frame, ramps in self.near(lat, lon):
            e, n, _ = frame.geodetic_to_enu(np.asarray(lat).ravel(), np.asarray(lon).ravel(), 0.)
            xy = np.c_[e, n]
            for ramp in ramps:
                out = ramp.fit(xy, out, terrain=terrain, lift=height_offset,native=native if terrain else None)
            if terrain:
                # A neighbouring carriageway's embankment must not tilt the other road's core.
                for ramp in ramps:
                    out = ramp.fit(xy, out, terrain=True, lift=height_offset, core_only=True,native=native)
        return out.reshape(shape)

    def approach_chunks(self, frame, box, left_hand=False, country=None):
        """Create deck chunks for approaches that must cross above another road."""
        import roads, roadmarkings
        cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
        lat, lon, _ = frame.enu_to_geodetic(cx, cy, 0.)
        chunks = []
        for model_frame, ramps in self.near(np.array([lat]), np.array([lon])):
            for ramp in ramps:
                if not ramp.elevated:
                    continue
                la, lo, _ = model_frame.enu_to_geodetic(ramp.xy[:,0], ramp.xy[:,1], np.zeros(len(ramp.xy)))
                e, n, _ = frame.geodetic_to_enu(la, lo, np.zeros(len(la)))
                z = ramp.z.copy()
                # Sink the distant end slightly into the native pavement.
                z -= .15*np.clip((ramp.s-(ramp.s[-1]-12.))/12., 0., 1.)
                pts = np.c_[e,n,z]
                lines = [({**roads._deck_look(ramp.spec), 'deck': True}, ramp.spec.width, pts)]
                lines.extend((look,w,line) for look,w,line in roads.extras(ramp.spec,pts,ramp.lon,left_hand=left_hand,country=country)
                             if look['m'] not in (roads.SIDEWALK['m'],roads.GUTTER['m'],roads.EDGE_DECAL['m']))
                for look,w,line in lines:
                    for piece in roads.clip_polyline(line,*box):
                        if len(piece)<2: continue
                        c=roads._chunk(look,w,piece); c['fixed']=True; c['fid']=str(ramp.fid)+'_model_approach'
                        if look.get('deck'): c['deck']=True; c['dd']=.35
                        chunks.append(c)
        return roadmarkings.paint(chunks)

    def nodes(self, frame, nodes):
        a = np.asarray(nodes, float).copy()
        la, lo, _ = frame.enu_to_geodetic(a[:, 0], a[:, 1], np.zeros(len(a)))
        a[:, 2] = self.apply(la, lo, a[:, 2], terrain=False)
        return a


def configure(grader, cache_dir, raw):
    import threedmr
    if grader is not None and cache_dir and threedmr.enabled() and not hasattr(grader, 'model_joins'):
        # Every anchor/geotile view shares one PlanStore. Decode/trace a bridge once per process, not per tile.
        with grader.store.lock:
            contexts = grader.store.__dict__.setdefault('_model_join_contexts', {})
            if cache_dir not in contexts:
                contexts[cache_dir] = Context(grader, cache_dir, raw)
            grader.model_joins = contexts[cache_dir]
