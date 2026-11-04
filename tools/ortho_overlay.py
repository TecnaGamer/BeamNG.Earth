"""Our road / sidewalk geometry drawn over a NAIP orthophoto of the same square (tools/ortho_naip.py):
carriageway edges cyan, curb lines yellow, sidewalk outer edges green, crosswalks magenta, stop lines
white, corner asphalt orange. The user asked (2026-10-11) for bird's-eye comparisons with real imagery.
    python3 tools/ortho_naip.py LAT LON HALF_M docs/research/ortho/x.jpg
    python3 tools/ortho_overlay.py LAT LON HALF_M docs/research/ortho/x.jpg docs/research/ortho/x_overlay.jpg
Uses the server's caches (map data, DEM) like the server does."""
import os, sys, math
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server'))
import numpy as np
from PIL import Image, ImageDraw
import geodesy, roads, grading, mapdata, dem, netfetch, surface, sidewalks, roadwidth
lat, lon, half, img, out = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], sys.argv[5]
cache = os.path.expanduser('~/.cache/beamng-earth')
net = netfetch.Fetcher(stats_dir=os.path.join(cache, 'netstats'))
md = mapdata.MapData(cache, fetcher=net); dm = dem.Dem(os.path.join(cache, 'dem'), fetcher=net)
frame = geodesy.EnuFrame(lat, lon)
g = grading.Grader(frame, md, lambda la, lo: dm.sample(la, lo, grading.PROFILE_ZOOM), None)
elev = lambda la, lo: g.apply(la, lo, dm.sample(la, lo, 15), grading.ROAD_LIFT)
sf = surface.Surface(md)
rw = roadwidth.RoadWidths(lambda k: md.cell(k, layers=("buildings",), kind="demand").layers.get("buildings")).get
dlat = half / 111320.0; dlon = half / (111320.0 * math.cos(math.radians(lat)))
im = Image.open(img).convert('RGB'); W, H = im.size
dr = ImageDraw.Draw(im, 'RGBA')
def px(xy):
    la, lo, _ = frame.enu_to_geodetic(np.asarray(xy)[:, 0], np.asarray(xy)[:, 1], 0.0)
    return list(zip(((lo - (lon - dlon)) / (2 * dlon) * W).tolist(), (((lat + dlat) - la) / (2 * dlat) * H).tolist()))
e, n, _ = frame.geodetic_to_enu(np.array([lat - dlat, lat + dlat]), np.array([lon - dlon, lon + dlon]), 0.0)
tiles = {(int(math.floor(x / 256)), int(math.floor(y / 256))) for x in np.linspace(e[0], e[1], 4) for y in np.linspace(n[0], n[1], 4)}
for tx, ty in sorted(tiles):
    cells = [md.cell(k, layers=("roads",), kind="demand").layers.get("roads") for k in roads.cells_of_tile(frame, tx, ty)]
    walks = []
    chunks = roads.build_tile(frame, tx, ty, cells, elev, sf.urban, walks=walks, widths=rw)
    for c in chunks:
        if not c['n'] and not c.get('poly'):
            continue
        if c.get('poly'):
            dr.polygon(px(np.array(c['poly'])[:, :2]), outline=(255, 160, 0, 255))
            continue
        pts = np.array(c['n'])[:, :2]
        if c['m'] in ('road_asphalt_2lane', 'italy_cobblestone') or str(c['m']).startswith(('m_dirt_', 'bne_dirt_')):
            for s in (1, -1):               # carriageway edges in cyan
                dr.line(px(roads.offset_polyline(np.c_[pts, np.zeros(len(pts))], s * c['w'] / 2)[:, :2]), fill=(0, 255, 255, 255), width=2)
        elif c['m'] == 'crossing_white':
            dr.line(px(pts), fill=(255, 0, 255, 160), width=int(c['w'] / (2 * half) * W))
        elif c['w'] == 0.45:
            dr.line(px(pts), fill=(255, 255, 255, 255), width=3)
    for wk in walks:                        # sidewalks: curb line yellow, outer edge green
        if 'corner' in wk:
            dr.line(px(wk['corner']['arc'][:, :2]), fill=(255, 255, 0, 255), width=3)
            continue
        ww = wk.get('w', roads.SIDEWALK['w'])
        curb, o = sidewalks.run_geometry(wk['pts'], wk['side'], ww)
        dr.line(px(curb[:, :2]), fill=(255, 255, 0, 255), width=3)
        dr.line(px(curb[:, :2] + o * ww), fill=(0, 255, 0, 200), width=2)
im.resize((W, W)).save(out)
print('tiles', sorted(tiles))
