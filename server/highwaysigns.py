"""Exit, overhead destination and mile-marker signs of a road tile (from cody196's PR #363), from the weekly sign
shards (railtags.SignTags: tools/build_sign_tags.py over the OSM planet) and the tile's generated road geometry."""
import logging
import threading
import time
from concurrent.futures import CancelledError

import numpy as np

import exitsigns
import milemarkers
import railtags
import roads
import signroads
import signlayout
import throughsigns

log = logging.getLogger('earthd.highwaysigns')


class Source:
    """The sign records (SignTags) and the stable road neighbourhoods signs are placed in (signroads.Context)."""

    def __init__(self, md, tags=None):
        self.tags = tags if tags is not None else railtags.SignTags(md.cache_dir, md.fetcher)
        self.md = md
        self.contexts = {}
        self.lock = threading.Lock()
        self.warned = -float('inf')

    def close(self):
        pass

    def version(self):
        """The sign data's build in use, or None (none yet)."""
        return self.tags.version()

    def context(self, urban, widths):
        key = (urban, widths)
        with self.lock:
            if key not in self.contexts:
                self.contexts[key] = signroads.Context(
                    lambda cell: self.md.cell(cell, layers=('roads',), kind='demand').layers.get('roads'),
                    urban, widths)
            return self.contexts[key]

    def objects(self, frame, box):
        """The sign records in box (x0, y0, x1, y1 of frame), in the sign modules' form."""
        # Bundled references remain available when the optional weekly index is
        # unpublished, empty here, or temporarily unavailable.
        objects = milemarkers.query(frame, box)
        x0, y0, x1, y1 = box
        lat, lon, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.)
        try:
            objects += self.tags.query(float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat)))
        except CancelledError:
            raise
        except Exception as error:
            now = time.monotonic()
            if now - self.warned >= 60.:
                self.warned = now
                log.warning('OSM signs unavailable; bundled mile markers remain: %s: %s', type(error).__name__, error)
        return objects


def tile_objects(source, frame, tx, ty):
    """Guide-sign and mile-reference sources that may place a sign in this tile (an exit's signs stand up to
    exitsigns.APPROACH_M before its junction)."""
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    m = exitsigns.QUERY_MARGIN + signlayout.MARGIN
    return source.objects(frame, (x0 - m, y0 - m, x1 + m, y1 + m))


def guide_layout(frame, objects, context, box, elev, chunks=(), left_hand=False, mph=True):
    """Plan neighbouring panels together, then give each final assembly one owner."""
    pad = signlayout.MARGIN
    area = (box[0]-pad, box[1]-pad, box[2]+pad, box[3]+pad)
    exits, overheads, contexts, by_id = [], [], {}, {}
    for group in signlayout.groups(frame, objects):
        roadside, paired = [], []
        for obj in group:
            by_id[obj['id']] = obj
            pavements, mains = exitsigns.source_pavements(frame, obj, context, left_hand)
            contexts[obj['id']] = pavements
            placed = exitsigns.place(frame, [obj], (), area, elev, chunks, left_hand, mph,
                                      pavements=pavements, ramp_mains=mains)
            roadside.extend(placed)
            distance = '1/2 MILE' if mph else '500 m'
            replaces = any(s['kind'] == 'advance' and s['distance'] == distance for s in placed)
            paired.extend(throughsigns.place(frame, [obj], pavements, area, elev, chunks, mph,
                                             allow_outside=replaces))
        replaced = set()
        stations = sorted({(s['kind'], s['distance']) for s in roadside if s['kind'] != 'ramp' and s['style'] == 'exit'})
        for kind, distance in stations:
            panels = [s for s in roadside if s['kind'] == kind and s['distance'] == distance and s['style'] == 'exit']
            if len(panels) < 2:
                continue
            first = min(panels, key=lambda s: (s['side'], signlayout.label_key(s['label']), s['id']))
            assembly = throughsigns.branches(frame, by_id, panels, contexts[first['id']], area, elev, chunks)
            if assembly is not None:
                overheads.append(assembly)
                replaced.update((s['id'], kind, distance) for s in panels)
        for assembly in paired:
            key = assembly['id'], 'advance', assembly['distance']
            if key not in replaced:
                overheads.append(assembly)
                replaced.add(key)
        exits.extend(s for s in roadside if (s['id'], s['kind'], s['distance']) not in replaced)
    for obj in sorted({o['id']: o for o in objects if o.get('through')}.values(), key=lambda o: o['id']):
        pavements = throughsigns.source_pavements(frame, obj, context, left_hand)
        contexts[obj['id']], by_id[obj['id']] = pavements, obj
        overheads.extend(throughsigns.place(frame, [obj], pavements, area, elev, chunks, mph))
    exits, overheads = signlayout.arrange(frame, exits, overheads, contexts, by_id, elev, chunks)
    return ([s for s in exits if signlayout.owns(box, s)], [s for s in overheads if signlayout.owns(box, s)])


def build_tile(md, frame, tx, ty, elev, chunks=(), left_hand=False, mph=True, urban=None, widths=None):
    """Return (exit mesh, mile-marker mesh); optional scenery cannot stop road generation."""
    source = getattr(md, 'highway_signs', None)
    if source is None:
        return None, None
    try:
        box = roads.tile_bbox(tx, ty)
        objects = tile_objects(source, frame, tx, ty)
        if not objects:
            return None, None
        context = source.context(urban, widths)
        exits, overheads = guide_layout(frame, objects, context, box, elev, chunks, left_hand, mph)
        miles = []
        for obj in milemarkers.unique_objects(objects):
            pavements = milemarkers.source_pavements(frame, obj, context, left_hand)
            miles.extend(milemarkers.place(frame, [obj], pavements, box, elev, chunks))
        origin = (box[0], box[1], 0.)
        return throughsigns.build(exits, overheads, origin), milemarkers.build(miles, origin)
    except CancelledError:
        raise
    except Exception as error:
        # Keep provider outages, incomplete optional data or missing glyphs from blanking roads.
        now = time.monotonic()
        if now - source.warned >= 60.:
            source.warned = now
            log.warning('Highway signs unavailable; roads continue: %s: %s', type(error).__name__, error)
        return None, None
