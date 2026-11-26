"""The level select's picture of each saved place (the mod's levelinfo.lua: a spawn point per place): SIZE_M of the
server's imagery round it (Imagery.render, as the terrain's photo), cropped to 16:9, written once to
<user dir>/levels/beamng_earth/places/<key>.jpg, where the level's info.json in the user folder points.

FORMAT (the folder's .format file): 2 since 2026-11-23: older pictures may hold EOX 2023 pixels (CC BY-NC-SA, out of
the imagery chain since): deleted once (sweep), and made again when the level select asks.

    write(imagery, user_dir, [(key, lat, lon), ...]) -> [keys written or already there]
"""
import logging
import os
import re

import numpy as np
import fast

import geodesy

log = logging.getLogger("earthd.placepreview")

SIZE_M = 1500.0             # m of ground across the picture
PX = 640                    # its width (px); 16:9 high
FOLDER = ("levels", "beamng_earth", "places")
KEY_RE = re.compile(r"^[0-9mp_]{3,40}$")     # levelinfo.previewKey: digits, 'm' (minus), 'p' (point), '_'
FORMAT = 2
FORMAT_FILE = ".format"
_swept = set()


def path_of(user_dir, key):
    return os.path.join(user_dir, *FOLDER, key + ".jpg")


def sweep(user_dir):
    """The pictures of an older FORMAT deleted (once per folder and process; a picture file per key only, nothing
    linked: a folder that is a link is left alone); the number deleted."""
    folder = os.path.join(user_dir, *FOLDER)
    if folder in _swept:
        return 0
    _swept.add(folder)
    marker = os.path.join(folder, FORMAT_FILE)
    try:
        with open(marker) as f:
            if int(f.read().strip() or 0) >= FORMAT:
                return 0
    except (OSError, ValueError):
        pass
    if not os.path.isdir(folder) or os.path.islink(folder):
        return 0
    n = 0
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        if name.endswith(".jpg") and KEY_RE.match(name[:-4]) and os.path.isfile(p) and not os.path.islink(p):
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
    try:
        with open(marker + ".tmp", "w") as f:
            f.write(str(FORMAT))
        os.replace(marker + ".tmp", marker)
    except OSError as e:
        log.debug("place preview format file: %s", e)
    if n:
        log.info("place previews: %d older pictures deleted (made again)", n)
    return n


def write(imagery, user_dir, items):
    """Each (key, lat, lon)'s picture when it is not there yet; the keys that have one now. A key that is not a
    levelinfo.previewKey (a file name from the mod's message) is skipped."""
    sweep(user_dir)
    done = []
    for key, lat, lon in items:
        if not isinstance(key, str) or not KEY_RE.match(key):
            log.debug("place preview: bad key %r", key)
            continue
        out = path_of(user_dir, key)
        if os.path.exists(out):
            done.append(key)
            continue
        try:
            lat, lon = float(lat), float(lon)
            frame = geodesy.EnuFrame(lat, lon)
            rgb, _ = imagery.render(frame, -SIZE_M / 2, -SIZE_M / 2, SIZE_M, PX)
        except Exception as e:      # offline, no imagery there: the Earth's picture stands in
            log.info("place preview %s: %s: %s", key, type(e).__name__, e)
            continue
        h = PX * 9 // 16
        y0 = (PX - h) // 2
        pic = fast.photo_jpeg(np.asarray(rgb)[y0:y0 + h, :, :3], 85, False)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(pic)
        os.replace(tmp, out)
        done.append(key)
    return done
