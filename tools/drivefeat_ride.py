"""drivefeat_ride.py: a quarter car over a road profile, the mapped traffic calming's drivability check (the sheets in
~/bnge-tests/drivefeat, tests/test_drivefeat.py). The IRI's golden car (per unit sprung mass: tyre k1 653, spring k2
63.3, damper c 6.0, unsprung mass mu 0.15), stepped at 0.5 ms; the tyre pushes only (a wheel off the road carries
nothing).

    from drivefeat_ride import ride
    peak_g, least_load = ride(z, ds, v)

peak_g: the body's largest vertical acceleration in g; least_load: the tyre's least load over its static one (below
0: the wheel left the road, a launch)."""
import math

K1, K2, C, MU, G = 653.0, 63.3, 6.0, 0.15, 9.81


def ride(z, ds, v, dt=0.0005):
    """(peak body acceleration in g, least tyre load / static load) of a quarter car at v m/s over heights z every ds
    m (the car settled on the first height)."""
    z = [float(q) for q in z]
    if len(z) < 2 or v <= 0:
        return 0.0, 1.0
    zs = zu = z[0]
    vs = vu = 0.0
    static = G * (1.0 + MU)             # the tyre's static load per unit sprung mass
    t, total = 0.0, (len(z) - 1) * ds / v
    peak, least = 0.0, 1.0
    while t < total:
        x = t * v / ds
        i = min(int(x), len(z) - 2)
        r = z[i] + (z[i + 1] - z[i]) * (x - i)
        fs = K2 * (zu - zs) + C * (vu - vs)             # on the body, up
        ft = max(-static, K1 * (r - zu))                 # the tyre's change from static: it cannot pull
        a_s = fs
        a_u = (ft - fs) / MU
        vs += a_s * dt
        vu += a_u * dt
        zs += vs * dt
        zu += vu * dt
        peak = max(peak, abs(a_s) / G)
        least = min(least, (static + K1 * (r - zu)) / static)
        t += dt
    return peak, least


if __name__ == "__main__":
    import sys
    h, ramp, top, mph = (float(a) for a in sys.argv[1:5])
    ds = 0.05
    n = int((2 * ramp + top + 20) / ds)
    zz = []
    for k in range(n):
        d = abs(k * ds - (10 + ramp + top / 2))
        rr = d - top / 2
        zz.append(h if rr <= 0 else (h * 0.5 * (1 + math.cos(math.pi * rr / ramp)) if rr < ramp else 0.0))
    print(ride(zz, ds, mph * 0.44704))
