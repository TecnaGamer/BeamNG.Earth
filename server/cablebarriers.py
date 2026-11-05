"""Galvanized median posts and four steel cables, in the colliding road stream."""
import numpy as np
import roadmesh
import roads

HEIGHT = 1.18
POST_SPACING = 3.2
EDGE_OFFSET = .55
CABLE_HEIGHTS = (.43, .63, .83, 1.03)
CABLE_RADIUS = .012
METAL = 'bne_median_cable_steel'
REFLECTOR = 'bne_median_cable_reflector'


def runs(frame, rows, blockers, box, elev):
    import medianbarriers as medians
    out = []
    for fid, spec, line, width, route, north in rows:
        if route != 65:
            continue
        # Orient in the travel direction before sampling: reversing the source
        # geometry cannot change post spacing, shoulder selection or tile seams.
        line = line if (spec.dir or 1) > 0 else line[::-1]
        xy, station, _ = roadmesh._resample(line, 2.)
        if len(xy) < 2:
            continue
        normals = roadmesh._normals(xy)
        centres = xy + normals * (width/2. + EDGE_OFFSET)
        la, lo, _ = frame.enu_to_geodetic(centres[:, 0], centres[:, 1], 0.)
        valid = medians.treatment(route, la, lo) == (medians.CABLE_NORTH if north else medians.CABLE_SOUTH)
        if not valid.any():
            continue
        # Preserve mapped median crossovers and ramp openings. Never put cable
        # inside the opposing carriageway when a very tight median cannot fit it.
        for points, blocker_width in blockers:
            valid &= medians.project(centres, points)[2] > blocker_width/2. + medians.OPENING_CLEARANCE
        for other_id, other, points, other_width, other_route, other_north in rows:
            if other_route == route and other_north != north:
                valid &= medians.project(centres, points)[2] > other_width/2. + .10
        z = medians._heights(frame, elev, centres)
        valid &= np.isfinite(z)
        indices = np.flatnonzero(valid)
        for group in np.split(indices, np.flatnonzero(np.diff(indices) != 1)+1):
            if len(group) < 2:
                continue
            source = np.c_[centres[group], z[group], normals[group], station[group]]
            for piece in roads.clip_polyline(source, *box):
                piece = piece[np.r_[True, np.linalg.norm(np.diff(piece[:, :2], axis=0), axis=1) > 1e-6]]
                if len(piece) < 2:
                    continue
                ticks = np.arange(np.ceil(piece[0, 5]/POST_SPACING)*POST_SPACING,
                                  piece[-1, 5]+1e-7, POST_SPACING)
                posts = np.column_stack([np.interp(ticks, piece[:, 5], piece[:, d]) for d in range(5)])
                # One owner for posts at a shared tile edge; wires meet exactly.
                posts = posts[(posts[:, 0] >= box[0]) & (posts[:, 0] < box[2]) &
                              (posts[:, 1] >= box[1]) & (posts[:, 1] < box[3])]
                out.append(dict(route=route, side='N' if north else 'S', barrier=True, barrier_type='cable',
                                n=piece[:, :3].tolist(), cross=piece[:, 3:5].tolist(), posts=posts.tolist(),
                                top=(piece[:, 2]+HEIGHT).tolist(), w=.14, m=METAL))
    return out


def build(chunks, origin):
    """Closed metal geometry; no opaque sheet between the visible cables."""
    vertices, triangles, materials = [], [], []
    origin = np.asarray(origin, float)

    def quad(points, material=0):
        index = len(vertices)
        vertices.extend(np.asarray(points)-origin)
        triangles.extend([(index, index+1, index+2), (index, index+2, index+3)])
        materials.extend([material, material])

    def box(centre, normal, lo, hi, material=0):
        tangent = np.array([normal[1], -normal[0]])
        x, y, z = lo; X, Y, Z = hi
        for face in ([(x,y,z),(x,y,Z),(x,Y,Z),(x,Y,z)], [(X,y,z),(X,Y,z),(X,Y,Z),(X,y,Z)],
                     [(x,y,z),(X,y,z),(X,y,Z),(x,y,Z)], [(x,Y,z),(x,Y,Z),(X,Y,Z),(X,Y,z)],
                     [(x,y,z),(x,Y,z),(X,Y,z),(X,y,z)], [(x,y,Z),(X,y,Z),(X,Y,Z),(x,Y,Z)]):
            p = np.asarray(face, float)
            xy = p[:, :1]*tangent+p[:, 1:2]*normal
            quad(centre+np.c_[xy, p[:, 2]], material)

    for chunk in chunks:
        if chunk.get('barrier_type') != 'cable':
            continue
        line = np.asarray(chunk['n'], float)
        normals = np.asarray(chunk['cross'], float)
        normals /= np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-9)
        for height in CABLE_HEIGHTS:
            angles = np.arange(6)*np.pi/3.
            ring = np.repeat(line[:, None, :], 6, axis=1)
            ring[:, :, :2] += normals[:, None, :]*np.cos(angles)[None, :, None]*CABLE_RADIUS
            ring[:, :, 2] += height+np.sin(angles)*CABLE_RADIUS
            for k in range(len(line)-1):
                for j in range(6):
                    nxt = (j+1)%6
                    quad([ring[k,j],ring[k,nxt],ring[k+1,nxt],ring[k+1,j]])
            for k, reverse in ((0, True), (-1, False)):
                index = len(vertices)
                vertices.extend(ring[k]-origin)
                for j in range(1, 5):
                    tri = (index,index+j,index+j+1)
                    triangles.append(tri[::-1] if reverse else tri)
                    materials.append(0)
        for post in np.asarray(chunk['posts'], float).reshape(-1, 5):
            normal = post[3:5]/max(np.linalg.norm(post[3:5]), 1e-9)
            # Slim I-section steel post and amber reflector cap, as in the reference.
            box(post[:3], normal, (-.035,-.006,-.15), (.035,.006,HEIGHT))
            for x in (-.035,.027):
                box(post[:3], normal, (x,-.035,-.15), (x+.008,.035,HEIGHT))
            box(post[:3], normal, (-.039,-.037,HEIGHT-.07), (.039,.037,HEIGHT-.01), 1)
    if not triangles:
        return None
    return (np.asarray(vertices), np.asarray(triangles, np.int32), np.zeros((len(vertices), 2)),
            [METAL, REFLECTOR], np.asarray(materials, np.int32))
