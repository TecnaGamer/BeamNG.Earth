"""Separate simplified cliff collision; render geometry and LODs are never changed."""
import os
import numpy as np
import clifflod
import fast

# Opt-in while the driving experiment is evaluated; old caches remain valid.
ENABLED = os.environ.get("BNE_CLIFF_COLLISION_SIMPLIFY") == "1"
SHARP_COS = 0.5  # protect 60-degree fractures and the faces touching them
ERROR_M = 0.08  # meshoptimizer absolute quadric metric, not a Hausdorff guarantee
TARGET_RATIO = 0.10
MIN_REDUCTION = 0.25
MIN_TRIANGLES = 128


def fracture_locks(positions, tris):
    """Keep sharp creases and their adjacent faces exact, including narrow slivers."""
    edges, inverse, counts = clifflod._edge_groups(tris, len(positions))
    faces = positions[tris]
    normals = np.cross(faces[:, 1]-faces[:, 0], faces[:, 2]-faces[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-20)[:, None]
    sums = np.zeros((len(edges), 3))
    np.add.at(sums, inverse, np.tile(normals, (3, 1)))
    sharp = (counts == 2) & (np.sum(sums*sums, axis=1) < 2+2*SHARP_COS)
    locked = np.zeros(len(positions), np.uint8)
    locked[edges[sharp].ravel()] = 1
    locked[tris[np.any(locked[tris], axis=1)].ravel()] = 1
    return locked


def build(rock):
    """Return collision arrays, or None when the exact surface is already small.

    Simplify each material separately, locking all topological borders (including
    holes, stream-piece edges and material seams). No position welding, component
    pruning or vertex movement. Existing f64 positions survive export unchanged.
    """
    if len(rock.tris) < MIN_TRIANGLES:
        return None
    fast.require(152, "simplified cliff collision")
    positions = np.ascontiguousarray(rock.positions, np.float64)
    tris = np.asarray(rock.tris, np.int64)
    materials = (np.zeros(len(tris), np.int32) if rock.tri_materials is None
                 else np.asarray(rock.tri_materials, np.int32))
    locks = fracture_locks(positions, tris)
    groups, groups_materials = [], []
    for material in np.unique(materials):
        faces = np.ascontiguousarray(tris[materials == material])
        indices, error = fast._ef.cliff_collision_indices(
            positions, faces, locks, max(1, int(len(faces)*TARGET_RATIO)), ERROR_M)
        simplified = np.asarray(indices, np.int64).reshape(-1, 3)
        # Never accept an empty surface or invalid native result as a collider.
        if (not len(simplified) or not np.isfinite(error) or error > ERROR_M*1.001
                or simplified.min() < 0 or simplified.max() >= len(positions)):
            return None
        groups.append(simplified)
        groups_materials.append(np.full(len(simplified), material, np.int32))
    triangles = np.concatenate(groups)
    if len(triangles) > len(tris)*(1-MIN_REDUCTION):
        return None
    vertices, mapped = np.unique(triangles, return_inverse=True)
    return dict(positions=positions[vertices], tris=mapped.reshape(-1, 3),
                tri_materials=np.concatenate(groups_materials))
