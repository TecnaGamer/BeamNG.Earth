"""Whether a place is in Sweden, for the road code that has no country to hand: road_specs is asked from a dozen places,
and what a road is must not depend on which of them asked. A rough outline, good to some kilometres along the borders
(a road that near one is much like the road across it)."""
import numpy as np

# lon, lat: round the coast from Svinesund, then up the Finnish border and down the Norwegian one
OUTLINE = np.array([(11.25, 59.10), (10.95, 58.85), (11.00, 58.00), (11.70, 56.90), (12.25, 56.35), (12.66, 56.05),
                    (12.82, 55.60), (12.75, 55.25), (14.50, 55.20), (15.90, 55.80), (17.00, 56.10), (19.60, 57.00),
                    (19.60, 58.20), (19.20, 59.80), (18.00, 61.00), (18.00, 62.00), (20.00, 63.30), (21.70, 64.50),
                    (22.40, 65.40), (24.14, 65.75), (24.15, 65.90), (23.65, 66.30), (23.70, 67.20), (23.40, 67.50), (23.65, 67.95),
                    (23.00, 68.30), (21.00, 69.05), (20.55, 69.06), (20.10, 68.45), (18.10, 68.55), (17.90, 67.95),
                    (16.40, 67.55), (15.40, 66.50), (14.50, 66.10), (14.55, 65.50), (13.70, 64.60), (14.10, 64.40),
                    (13.20, 64.10), (12.20, 63.60), (12.10, 63.00), (12.25, 62.30), (12.30, 61.70), (12.85, 61.40),
                    (12.60, 61.00), (12.20, 60.80), (12.60, 60.40), (12.50, 60.00), (11.85, 59.85), (11.70, 59.60),
                    (11.80, 59.30)], np.float64)


def contains(lon, lat):
    """Bool array: inside OUTLINE (even-odd). Pure."""
    x, y = np.atleast_1d(np.asarray(lon, np.float64)), np.atleast_1d(np.asarray(lat, np.float64))
    a, b = OUTLINE, np.roll(OUTLINE, -1, axis=0)
    crosses = (a[:, 1, None] > y) != (b[:, 1, None] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        at = a[:, 0, None] + (y - a[:, 1, None]) * (b[:, 0, None] - a[:, 0, None]) / (b[:, 1, None] - a[:, 1, None])
    return (crosses & (x < at)).sum(0) % 2 == 1
