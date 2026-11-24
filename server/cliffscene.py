"""Cliff tile protocol and serialization through the existing shape pipeline.

Keep imports of generation modules inside the writer: enumerating session files
must not load heightmaps or prepare assets when cliffs are disabled.

Reuse cdae.write(collision_only=True), the road-geotile content protocol, and
roads.lua's existing collider scheduling/rebuilds, as buildings do. Only the
surface and its spatial partition belong here. buildings.collision_mesh consumes
building specifications and constructs simplified walls/roof lids; build_parts
groups whole buildings. Neither accepts an arbitrary relief surface. Converting
cliffs to fake building specifications would lose their collision contours.

The shared writer currently rejects multiple collision-only meshes in one shape.
If batching those becomes necessary, extend that upstream API rather than adding
a cliff-specific binary encoder or a second collision loader/scheduler here.
"""
import os

FACE_PARTS = 64
CONTACT_PARTS = 24
COLLISION_PARTS = 256
# Exact triangles, in bounded pieces: fewer imports than 2K pieces without making
# a whole tile one expensive collision cook. Drawn meshes/LODs are independent.
COLLISION_TRIANGLES = 4096


def shape_files(aid, tx, ty):
    stem = f"{aid}_{tx}_{ty}"
    faces = {"cliff" if k == 1 else f"cliff{k}":
             f"cliff_{stem}{'' if k == 1 else '_' + str(k)}.cdae"
             for k in range(1, FACE_PARTS + 1)}
    faces.update({f"cliffStrip{k}": f"cliff_strip_{stem}_{k}.cdae"
                  for k in range(1, CONTACT_PARTS + 1)})
    faces.update({f"cliffCol{k}": f"cliff_col_{stem}_{k}.cdae"
                  for k in range(1, COLLISION_PARTS + 1)})
    return faces


def is_face(what):
    return what == "cliff" or (what.startswith("cliff") and what[5:].isdigit())


def is_collision(what):
    return what.startswith("cliffCol") and what[8:].isdigit()


def skin_files(key, aid):
    return {f"cliffStrip{i}": f"rs_{aid}_{key}_cliffStrip{i}.cdae" for i in range(1, CONTACT_PARTS + 1)}


def write_contacts(data, out_dir, vfs_dir, names, patches, tile, frame, drawn, quantum):
    """Project saved receivers onto this anchor's actual terrain lattice."""
    if not patches:
        return
    import numpy as np
    import cliffstrips
    patches = cliffstrips.map_patches(patches, tile.frame, frame, np.array((*tile.box[:2], 0.0)))
    surface = cliffstrips.TerrainSurface(frame, drawn, quantum)
    for i, part in enumerate(cliffstrips.project_parts(patches, surface), 1):
        what = f"cliffStrip{i}"
        cliffstrips.write(os.path.join(out_dir, names[what]), part)
        p = part[0].positions
        data[what] = {"shape": f"{vfs_dir}/{names[what]}", "pos": [0, 0, 0],
                      "bounds": p.min(axis=0).tolist() + p.max(axis=0).tolist()}


def write(data, out_dir, vfs_dir, aid, tx, ty, origin, rock):
    if rock is None:
        return
    import numpy as np
    import cdae
    import cliffrocks
    import cliffstrips
    import fast

    from cliffsettings import DEBUG
    if DEBUG:
        data.update(debugCliffs=rock.debug_coverage, debugCliffCuts=rock.debug_cuts,
                    debugCliffRocks=rock.debug_rocks)
    files = shape_files(aid, tx, ty)

    def entry(what, positions):
        return {"shape": f"{vfs_dir}/{files[what]}", "pos": list(origin),
                "bounds": positions.min(axis=0).tolist() + positions.max(axis=0).tolist()}

    # Drawn detail stays independent of the nearby collision island.
    for k, part in enumerate(cliffrocks.stream_parts(rock), 1):
        what = "cliff" if k == 1 else f"cliff{k}"
        cliffrocks.write(os.path.join(out_dir, files[what]), part)
        data[what] = entry(what, part.positions)
    # Exact face geometry, partitioned before the engine builds collision acceleration.
    # Large single meshes make first-use collision cooking disproportionately expensive.
    groups = fast._ef.cliff_stream_parts(np.ascontiguousarray(rock.positions, float),
        np.ascontiguousarray(rock.tris, np.int64), COLLISION_TRIANGLES, COLLISION_PARTS)
    for k, faces in enumerate(groups, 1):
        vertices, triangles = np.unique(rock.tris[np.asarray(faces, np.int64)], return_inverse=True)
        positions = rock.positions[vertices]
        what = f"cliffCol{k}"
        cdae.write(os.path.join(out_dir, files[what]), positions, triangles.reshape(-1, 3),
                   material="bne_cliff_strata", collision_only=True)
        data[what] = entry(what, positions)
        data.setdefault("colliders", {})[what] = [data[what]["bounds"]]
    for k, part in enumerate(rock.strips, 1):
        what = f"cliffStrip{k}"
        cliffstrips.write(os.path.join(out_dir, files[what]), part)
        data[what] = entry(what, part[0].positions)
    if rock.talus:
        groups = sorted({row[0] for row in rock.talus})
        indices = {name: i for i, name in enumerate(groups)}
        data["talus"] = {"groups": groups, "items": [[indices[row[0]], *row[1:]] for row in rock.talus]}
