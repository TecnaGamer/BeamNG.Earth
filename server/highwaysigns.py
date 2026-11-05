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
        x0, y0, x1, y1 = box
        lat, lon, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.)
        return self.tags.query(float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat)))


def tile_objects(source, frame, tx, ty):
    """Guide-sign and mile-reference sources that may place a sign in this tile (an exit's signs stand up to
    exitsigns.APPROACH_M before its junction)."""
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    m = exitsigns.QUERY_MARGIN
    return source.objects(frame, (x0 - m, y0 - m, x1 + m, y1 + m))


def build_tile(md, frame, tx, ty, elev, chunks=(), left_hand=False, mph=True, urban=None, widths=None, french=False):
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
        exits, miles, overheads = [], [], []
        for obj in {o['id']: o for o in objects if o.get('exit')}.values():
            pavements, mains = exitsigns.source_pavements(frame, obj, context, left_hand)
            # Decide replacement in the full source context, even if the roadside
            # panel and the gantry centre belong to different neighbouring tiles.
            roadside = exitsigns.place(frame, [obj], (), box, elev, chunks, left_hand, mph,
                                       pavements=pavements, ramp_mains=mains)
            distance = '1/2 MILE' if mph else '500 m'
            replaces = any(s['kind'] == 'advance' and s['distance'] == distance for s in roadside)
            paired = throughsigns.place(frame, [obj], pavements, box, elev, chunks, mph,
                                        allow_outside=replaces)
            exits.extend(s for s in roadside if not (paired and s['kind'] == 'advance' and
                                                     s['distance'] == distance))
            overheads.extend(s for s in paired if box[0] <= s['centre'][0] < box[2]
                             and box[1] <= s['centre'][1] < box[3])
        for obj in {o['id']: o for o in objects if o.get('through')}.values():
            pavements = throughsigns.source_pavements(frame, obj, context, left_hand)
            overheads.extend(throughsigns.place(frame, [obj], pavements, box, elev, chunks, mph))
        for obj in milemarkers.unique_objects(objects):
            pavements = milemarkers.source_pavements(frame, obj, context, left_hand)
            miles.extend(milemarkers.place(frame, [obj], pavements, box, elev, chunks))
        origin = (box[0], box[1], 0.)
        if french:
            import frroadmesh
            import euguardrails
            exits=[s for s in exits if euguardrails.country_at(frame,s['centre'])=='FR']
            overheads=[s for s in overheads if euguardrails.country_at(frame,s['centre'])=='FR']
            return frroadmesh.build_highways(exits,overheads,origin),None
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
