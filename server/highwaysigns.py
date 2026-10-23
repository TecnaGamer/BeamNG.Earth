"""Shared sign generation for both the road process pool and the server fallback."""
import logging
import os
import threading
import time
import weakref

import numpy as np

import exitsigns
import milemarkers
import osmregions
import osmsigns
import roads
import signroads

log = logging.getLogger('earthd.highwaysigns')


class Source:
    def __init__(self, md):
        self.objects = osmsigns.ObjectSource(osmregions.RegionSource(md.raw_dir, md.fetcher))
        self.md = md
        self.contexts = {}
        self.lock = threading.Lock()
        self.warned = -float('inf')
        self.probes = weakref.WeakKeyDictionary()

    def close(self):
        self.objects.close()

    def context(self, urban, widths):
        key = (urban, widths)
        with self.lock:
            if key not in self.contexts:
                self.contexts[key] = signroads.Context(
                    lambda cell: self.md.cell(cell, layers=('roads',), kind='demand').layers.get('roads'),
                    urban, widths)
            return self.contexts[key]

    def refresh(self, tracker, frame, x, y):
        """Notice an index completed by any worker; do not leave already loaded tiles signless."""
        now = time.monotonic()
        previous = self.probes.get(tracker)
        if previous and now - previous[0] < 5.:
            return
        # Cover the road lookahead plus the exit-sign approach. Region lookup and stats are local;
        # downloads and imports stay on the background thread, under the shared process lock.
        delta = np.array([-3000., -3000., 0., 3000., 3000.])
        lat, lon, _ = frame.enu_to_geodetic(x + delta, y + np.array([-3000., 3000., 0., -3000., 3000.]), 0.)
        names = {self.objects._region(float(la), float(lo)) for la, lo in zip(lat, lon)} - {None}
        stamps = {}
        for name in names:
            found = self.objects._readable(name, True)
            if found:
                try:
                    stamps[name] = os.stat(found[0]).st_mtime_ns
                except OSError:
                    pass
        if previous:
            # Leaving a region does not invalidate anything; newly ready or updated data does.
            if any(previous[1].get(name) != stamp for name, stamp in stamps.items()):
                tracker.refresh()
            stamps = {**previous[1], **stamps}
        self.probes[tracker] = now, stamps


def build_tile(md, frame, tx, ty, elev, chunks=(), left_hand=False, mph=True, urban=None, widths=None):
    """Return (exit mesh, mile-marker mesh); optional scenery cannot stop road generation."""
    source = getattr(md, 'highway_signs', None)
    if source is None:
        return None, None
    try:
        box = roads.tile_bbox(tx, ty)
        objects = osmsigns.tile_exit_objects(source, frame, tx, ty)
        context = source.context(urban, widths)
        exits, miles = [], []
        for obj in {o['id']: o for o in objects if o.get('exit')}.values():
            pavements, mains = exitsigns.source_pavements(frame, obj, context, left_hand)
            exits.extend(exitsigns.place(frame, [obj], (), box, elev, chunks, left_hand, mph,
                                        pavements=pavements, ramp_mains=mains))
        for obj in milemarkers.unique_objects(objects):
            pavements = milemarkers.source_pavements(frame, obj, context, left_hand)
            miles.extend(milemarkers.place(frame, [obj], pavements, box, elev, chunks))
        origin = (box[0], box[1], 0.)
        return exitsigns.build(exits, origin), milemarkers.build(miles, origin)
    except Exception as error:
        # Keep provider outages, incomplete optional data or missing glyphs from blanking roads.
        now = time.monotonic()
        if now - source.warned >= 60.:
            source.warned = now
            log.warning('Highway signs unavailable; roads continue: %s: %s', type(error).__name__, error)
        return None, None
