"""Blue service boards, batched into the existing streamed exit mesh.

Index is built offline with tools/build_service_signs.py. Text uses the shared
Roadgeek atlas: deterministic geometry, no per-frame image generation or logo
downloads. Business panels are text unless an independently licensed asset
pipeline is added; OSM brands never imply participation in a paid program.
"""
import hashlib
import json
from pathlib import Path
import threading
import logging
import time

import numpy as np

import exitsigns
import signlayout
import serviceobstacles as signplacement
from servicesigns_index import policy, POLICY
import servicecoverage

DATA = Path(__file__).with_name('data') / 'service_signs.json'


class Source:
    def __init__(self, path=DATA, coverage_dir=None):
        self.path = Path(path)
        self.stamp = None
        self.digest = None
        self.objects = []
        self.buckets = {}
        self.lock = threading.RLock()
        self.checked = -float('inf')
        self.coverage = servicecoverage.Source(coverage_dir or DATA.parent/'service_signs_us') if coverage_dir is not None or self.path == DATA else None
        self.revision = None

    def known(self):
        with self.lock:
            now = time.monotonic()
            if now-self.checked > 5:
                self.version()
                self.checked = now
            return self.revision

    def version(self):
        try:
            base = self._version()
        except (OSError, ValueError, TypeError, KeyError):
            logging.getLogger('earthd.servicesigns').warning('Service index unavailable; existing signs continue')
            with self.lock:
                self.objects, self.buckets, self.digest = [], {}, None
            base = None
        national = None
        if self.coverage is not None:
            try: national = self.coverage.version()
            except (OSError, ValueError, TypeError, KeyError):
                logging.getLogger('earthd.servicesigns').warning('National service manifest unavailable; bundled signs continue')
        self.revision = hashlib.sha256(str((base,national)).encode()).hexdigest() if national else base
        return self.revision

    def _version(self):
        with self.lock:
            try:
                stat = self.path.stat()
                stamp = stat.st_mtime_ns, stat.st_size, POLICY.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp != self.stamp:
                raw = self.path.read_bytes() if stamp else b''
                try:
                    data = json.loads(raw) if raw else {'objects': []}
                    if data.get('format', 1) != 1 or not isinstance(data.get('objects'), list):
                        raise ValueError('Unsupported service-sign index')
                    for o in data['objects']:
                        if not all(k in o for k in ('id', 'lon', 'lat', 'exit')):
                            raise ValueError('Incomplete service-sign record')
                except (ValueError, TypeError):
                    logging.getLogger('earthd.servicesigns').warning('Invalid service index; service signs omitted')
                    data = {'objects': []}
                self.objects = data['objects']
                self.buckets = {}
                import signshards
                for o in self.objects:
                    self.buckets.setdefault(signshards.tile(o['lat'], o['lon']), []).append(o)
                self.digest = hashlib.sha256(raw+POLICY.read_bytes()).hexdigest() if raw else None
                self.stamp = stamp
            return self.digest

    def query(self, lon0, lat0, lon1, lat1):
        with self.lock:
            self.version()
            import signshards
            x0, y0 = signshards.tile(lat1, lon0)
            x1, y1 = signshards.tile(lat0, lon1)
            objects = [o for x in range(x0, x1+1) for y in range(y0, y1+1)
                       for o in self.buckets.get((x, y), ())]
            national, covered = [],set()
            if self.coverage is not None:
                try: national,covered = self.coverage.query(lon0,lat0,lon1,lat1)
                except (OSError, ValueError, TypeError, KeyError):
                    logging.getLogger('earthd.servicesigns').warning('National service cell unavailable; bundled signs continue')
            objects = [o for o in objects if servicecoverage.key(*servicecoverage.cell(o['lon'],o['lat'])) not in covered]
            objects = list({o['id']:o for o in objects+national}.values())
            return [dict(id=o['id'], lon=o['lon'], lat=o['lat'], tags=o.get('tags', {}),
                         service_exit=o['exit'], provenance=o.get('provenance', 'inferred'))
                    for o in objects if lon0 <= o['lon'] <= lon1 and lat0 <= o['lat'] <= lat1]


def boards(extra, rules):
    services = [b for b in extra.get('services', [])
                if b['category'] in rules['distance_m'] and b['distance_m'] <= rules['distance_m'][b['category']]]
    categories = [c for c in rules['distance_m'] if any(b['category'] == c for b in services)]
    if not categories: return []
    result = [dict(category=None, lines=categories, businesses=[], width=rules['width_m'],
                   height=max(1.8, .62+.45*len(categories)), advance=rules['advance_m'])]
    for category in rules['specific_categories']:
        if len(result)-1 >= max(0, min(4, rules['max_specific_signs'])): break
        candidates = [b for b in services if b['category'] == category and b['name']
                      and name_lines(b['name'], (rules['width_m']-.6)/2-.25)]
        # Verified participation, when supplied by an audited importer, wins.
        candidates.sort(key=lambda b: (b.get('provenance') != 'verified', b['distance_m'], b['id']))
        unique, seen = [], set()
        for b in candidates:
            key = b['name'].strip().casefold()
            if key not in seen:
                seen.add(key); unique.append(b)
        if not unique: continue
        count = min(6, max(1, int(rules['max_panels'])))
        selected = unique[:count]
        columns = 2
        if len(selected) > 4:
            narrow = [b for b in unique if name_lines(b['name'], (rules['width_m']-.6)/3-.25)][:count]
            if len(narrow) > 4:
                selected, columns = narrow, 3
            else:
                selected = selected[:4]
        result.append(dict(category=category, lines=[], businesses=selected, columns=columns,
                           width=rules['width_m'], height=rules['height_m'],
                           advance=rules['advance_m']-(len(result))*rules['spacing_m']))
    return result


def name_lines(name, width):
    """At most three full-name lines at a legible minimum cap height."""
    lines = ['']
    for word in str(name).split():
        candidate = (lines[-1]+' '+word).strip()
        if exitsigns.guide_width(candidate, .20) <= width:
            lines[-1] = candidate
        elif lines[-1] and len(lines) < 3 and exitsigns.guide_width(word, .20) <= width:
            lines.append(word)
        else:
            return []
    return [line for line in lines if line]


def place(frame, objects, context, box, elev, chunks=(), existing=(), obstacle_factory=None):
    """Canonical source stations first, half-open tile ownership last.

    Failed stations are omitted; moving a sign in each tile creates duplicates.
    Full panel discs clear generated pavement, buildings and bridge footprints.
    """
    placed = []
    for obj in sorted({o['id']: o for o in objects if o.get('service_exit')}.values(), key=lambda o: o['id']):
        extra = obj['service_exit']
        rules = policy(extra['service_state'])
        proxy = dict(obj, exit=extra)
        pavements, _ = exitsigns.source_pavements(frame, proxy, context)
        approach = exitsigns._xy(frame, extra['approach'])
        end = exitsigns._station(approach, 1e12)
        if end is None: continue
        for index, board in enumerate(boards(extra, rules)):
            advance, width, height = board['advance'], board['width'], board['height']
            if advance < 200 or end[2] < advance+20: continue
            q, axis, _ = exitsigns._station(approach, end[2]-advance)
            candidates = []
            for row in pavements:
                if row[1].cls not in ('motorway','trunk') or row[1].link: continue
                wid = exitsigns._way_id(row[0])
                if wid is not None and wid not in extra['main_ids']: continue
                near = exitsigns._nearest(q, [row])
                if near is None or near[0] > 30: continue
                travel = near[7]*(near[2].dir or 1) if near[2].oneway else exitsigns._travel(near,axis)
                if float(travel@axis) > .8: candidates.append((near[0], str(row[0]), near, row[3], travel))
            if not candidates: continue
            _, _, near, paved_width, travel = min(candidates, key=lambda c: c[:2])
            _, _, spec, line, k, t, _, _ = near
            at = line[k]*(1-t)+line[k+1]*t
            right = np.array([travel[1], -travel[0]])
            # Service boards use the right shoulder, even at a left-hand exit.
            centre = at[:2]+right*(paved_width/2+rules['clearance_m']+width/2)
            radius = width/2+.5
            if (not exitsigns._clear(centre, radius, exitsigns._outlines(pavements))
                    or exitsigns._on_deck(centre, chunks, radius+1)
                    or exitsigns._on_deck(at[:2], chunks, 2)):
                continue
            if any(np.linalg.norm(centre-np.asarray(s['centre'])[:2]) < rules['existing_sign_separation_m']
                   for s in list(existing)+placed): continue
            obstructed = False
            for barrier in extra.get('service_barriers', []):
                line = exitsigns._xy(frame, barrier)
                if len(line) < 2: continue
                delta = np.diff(line, axis=0)
                fraction = np.clip(((centre-line[:-1])*delta).sum(1)/np.maximum((delta*delta).sum(1),1e-12),0,1)
                if np.min(np.linalg.norm(centre-line[:-1]-fraction[:,None]*delta,axis=1)) < radius:
                    obstructed = True; break
            if obstructed: continue
            if obstacle_factory is not None and not obstacle_factory(centre).clear(centre, radius): continue
            sign = exitsigns._make_sign(proxy, 'service', centre, travel, at, frame, elev, 1,
                                        width, height, state=extra['service_state'])
            if not np.isfinite(np.r_[sign['centre'], sign['bases'], sign['bottom']]).all(): continue
            if max(sign['bases'])-min(sign['bases']) > 1.5: continue
            sign.update(board=board, id=f"{obj['id']}:{index}", provenance=obj.get('provenance', 'inferred'))
            placed.append(sign)
    return [s for s in placed if signlayout.owns(box, s)]


def build(signs, origin):
    mesh = exitsigns.Mesh(origin)
    for sign in signs:
        mesh.centre = np.asarray(sign['centre'])
        travel = np.asarray(sign['travel'])
        mesh.face = np.r_[-travel, 0.]
        mesh.right = np.r_[travel[1], -travel[0], 0.]
        width, height = sign['width'], sign['height']
        bottom = sign['bottom']-mesh.centre[2]
        for off, base in zip(sign['post_offsets'], sign['bases']):
            y = width*off
            mesh.box((-.12, y-.07, base-mesh.centre[2]-.2), (.02, y+.07, bottom+height-.1), 2)
        mesh.rounded_panel(width, bottom, height, 4)
        board = sign['board']
        header = ('%s - EXIT %s' % (board['category'], sign['label']['ref'])) if board['category'] else 'EXIT '+sign['label']['ref']
        mesh.guide_text(header, 0, bottom+height-.55, .32, width-.5)
        if board['category']:
            # At most three columns and two rows; sparse boards retain fixed slots.
            columns = board['columns']
            cell_w, cell_h = (width-.6)/columns, (height-1.05)/2
            for i, business in enumerate(board['businesses']):
                y = (i%columns-(columns-1)/2)*cell_w
                z = bottom+height-.85-(i//columns+1)*cell_h+.08
                mesh.rectangle(cell_w-.12, z, cell_h-.12, .061, 1, y)
                mesh.rectangle(cell_w-.20, z+.04, cell_h-.20, .065, 4, y)
                # Wrap actual names; never substitute a different brand or invent a logo.
                lines = name_lines(business['name'], cell_w-.25)
                for j, value in enumerate(lines):
                    mesh.guide_text(value, y, z+cell_h/2+(len(lines)-1)*.13-j*.26-.12, .24, cell_w-.25)
        else:
            for i, value in enumerate(board['lines']):
                mesh.guide_text(value, 0, bottom+height-1.05-i*.45, .32, width-.5)
    return mesh.result()


def build_tile(md, frame, objects, context, box, elev, chunks, existing):
    obstacles = {}
    def obstacle(centre):
        import roads
        key = tuple(np.floor(centre/roads.TILE).astype(int))
        if key not in obstacles:
            obstacles[key] = signplacement.for_tile(frame, *key, md, ())
        return obstacles[key]
    return build(place(frame, objects, context, box, elev, chunks, existing, obstacle), (box[0], box[1], 0.))
