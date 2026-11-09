"""A track laid beside a road, without a step between them (plan 028, 2026-11-08: prospekt Heroiv in Nemiia, a track whose
bed stood 2.1 m under the road 3 m past its edge, 1.2 m under the ground beside it: a vertical cut between the two).

The bed line (railways.bed_heights) is the smoothed ground under the track, laid to the DEM alone, so beside a road it
knows nothing of the road's height. The grader lays the ground to every plan's flat band and slopes it between two bands at
SLOPE_RUN : 1; where the room between a road's band and the bed's is less than that asks for, the ground stood as a wall.

couple() takes the bed to where the room is enough: per vertex, every road within REACH_M gives a band of heights,
its own +- (room / SEAM_SLOPE + SEAM_TOL_M), the room being the distance between the road's band and the bed's; the bed is
moved into the strictest. The moves are then spread along the track at the track's own grade, so no step is made
further on, and more where the road runs on beside it. A track crossing a road at grade has no room at all there: its bed
is the road's, which is the flush crossing (railways._flush) already, kept. Pure given its inputs."""
import numpy as np

SEAM_SLOPE = 3.0        # m of room a metre of height difference needs between two flat bands (the grader's SLOPE_RUN is 2)
SEAM_TOL_M = 0.10       # m of difference allowed with no room at all
REACH_M = 40.0          # m from the bed line a road is looked at
MAX_MOVE_M = 6.0        # m the bed is moved at most (bad data: rather a step than a mountain)
SPREAD_GRADE = 0.02     # the track's own grade, at which a move is run out along it (railways.GRADE)


def couple(xy, z, half, roads):
    """The bed heights z (n,) of a track on xy (n, 2) taken to where the roads beside it leave room for the ground between
    (module doc); z itself where no road is near. half: the bed's flat band's half width; roads: [(xy (k, 2), z (k,),
    half width)]: the roads' bands. Native (railseam.rs, 2026-11-09: in numpy per bed vertex per road plan it was ~5 s
    of a Manhattan cell's 35 s of planning)."""
    import fast
    xy = np.asarray(xy, np.float64)[:, :2]
    z = np.asarray(z, np.float64)
    if len(z) < 2 or not roads:
        return z
    pts, zs, off, hw = [], [], [0], []
    for rxy, rz, rh in roads:
        rxy = np.asarray(rxy, np.float64)[:, :2]
        if len(rxy) < 2:
            continue
        pts.append(rxy)
        zs.append(np.asarray(rz, np.float64)[:len(rxy)])
        off.append(off[-1] + len(rxy))
        hw.append(float(rh))
    if not hw:
        return z
    out = fast.rail_seam_couple(xy, z, half, np.concatenate(pts), np.concatenate(zs), np.asarray(off, np.int64),
                                np.asarray(hw, np.float64), REACH_M, SEAM_SLOPE, SEAM_TOL_M, MAX_MOVE_M, SPREAD_GRADE)
    return z if out is None else out
