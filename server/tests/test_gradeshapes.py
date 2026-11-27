"""gradeshapes: each interchange solved as one shape, placed on the ground with its embankments."""
import numpy as np

import gradeshapes as ix
from gradeshapes import Member


def _line(x0, y0, x1, y1, n=40):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _run(members, ground=lambda xy: np.zeros(len(xy))):
    net, systems = ix.find_systems(members, lambda net_: (ground, lambda i, s: _fixed(net_, ground, i, s)))
    shapes = []
    for sysm in systems:
        pcs = ix.pieces(net, sysm)
        smp = ix.sample(net, pcs)
        zs = ix.solve_shape(net, sysm, pcs, smp, [ground(x["xy"]) for x in smp], lambda i, s: _fixed(net, ground, i, s))
        shapes.append((pcs, smp, zs))
    emb = ix.embankments(net, systems, shapes,
                         lambda i, sv: ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]),
                                                        np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)))
    return net, systems, shapes, emb


def _fixed(net, ground, i, s):
    """The ground of member i at arc length s (a float), or at an array of them (an array)."""
    sv = np.atleast_1d(np.asarray(s, np.float64))
    z = ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])],
                        axis=1))
    return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])


def _shape_z(net, shapes, key, q):
    best = None
    for pcs, smp, zs in shapes:
        for x, z in zip(smp, zs):
            for k in range(len(z)):
                if net.m[int(x["mem"][k])].key != key:
                    continue
                d = float(np.hypot(*(x["xy"][k] - q)))
                if best is None or d < best[0]:
                    best = (d, z[k])
    return best[1]


def _key_index(net, key):
    return [n for n, m in enumerate(net.m) if m.key == key][0]


def _joined(net, sysm, a, b):
    return any({net.m[i].key, net.m[j].key} == {a, b} for i, _si, j, _sj in sysm["joins"])


def _ray(p, deg, s0, s1, n=40):
    u = np.array([np.cos(np.radians(deg)), np.sin(np.radians(deg))])
    return np.asarray(p, np.float64) + np.linspace(s0, s1, n)[:, None] * u


def _arc(c, r, a0, a1, n=48):
    a = np.radians(np.linspace(a0, a1, n))
    return np.asarray(c, np.float64) + r * np.stack([np.cos(a), np.sin(a)], axis=1)


def _spiral(r0, pitch, a0, a1, n):
    a = np.linspace(a0, a1, n)
    r = r0 + pitch * a / (2 * np.pi)
    return np.stack([r * np.cos(a), r * np.sin(a)], axis=1)


def test_a_road_cut_in_lane_sections_in_one_cell_and_whole_in_its_neighbour_is_one_road():
    """Vienna's A23 viaduct: its sections ("<road>~lanes<a>-<b>") from the cell with lane tags and the unsplit copy from the
    neighbour without them were both members, the deck and the tunnel it goes into twice, and the deck rose 2.3 m."""
    sec = Member("r1~lanes000000000-811393678", _line(0, 0, 53, 0, 12), "deck", "motorway", 100, 1, 8.0, None)
    whole = Member("r1", _line(3, 1, 49, 1, 10), "deck", "motorway", 100, 1, 8.0, None)
    other = Member("r2", _line(3, 1, 49, 1, 10), "deck", "motorway", 100, 1, 8.0, None)
    far = Member("r1", _line(0, 200, 40, 200, 10), "deck", "motorway", 100, 1, 8.0, None)
    tun = Member("r1", _line(3, 1, 49, 1, 10), "tunnel", "motorway", 100, -1, 8.0, None)
    got = {id(m) for m in ix.one_road([sec, whole, other, far, tun])}
    assert id(whole) not in got and id(sec) in got, "(the copy along a section of its own road)"
    assert id(other) in got, "(another road lying there is another road)"
    assert id(far) in got, "(a copy of the road away from its sections is not theirs)"
    assert id(tun) in got, "(a tunnel is not a deck's copy)"
    both = [whole, other]
    assert ix.one_road(both) is both


def test_a_deck_read_from_two_cells_with_its_line_cut_differently_is_one_deck():
    """A part's key is its road's and the middle of its line: the copies a neighbouring cell reads of a deck (a vertex more,
    its line a few metres longer, its width measured differently) were two decks, each the other's continuation at both ends,
    so neither was an end of the structure on the road it lands on."""
    a = Member(("r1", "deck", 100, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    b = Member(("r1", "deck", 99, 0), _line(0, 0.5, 198, 0.5, 38), "deck", "motorway", 100, 1, 9.0, None)
    same = Member(("r1", "deck", 100, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    next_part = Member(("r1", "deck", 300, 0), _line(200, 0, 400, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    tun = Member(("r1", "tunnel", 100, 0), _line(0, 0, 200, 0, 40), "tunnel", "motorway", 100, -1, 7.0, None)
    other = Member(("r2", "deck", 100, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    got = {id(m) for m in ix.one_road([b, a, next_part, tun, other])}
    assert id(a) in got and id(b) not in got, "(the longer copy stays)"
    assert id(next_part) in got, "(the next part of the road is another part)"
    assert id(tun) in got and id(other) in got, "(a tunnel, another road's deck, are not copies)"
    assert len(ix.one_road([same, a])) == 1, "(copies as long as each other keep one)"
    c = Member(("r1", "deck", 101, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    assert ix.one_road([c, a])[0].key == ix.one_road([a, c])[0].key == a.key, "(the first by key, whatever their order)"


def test_the_maps_levels_order_two_structures_of_one_kind_and_a_track_as_a_road():
    def mem(kind, level, cls="rail"):
        return Member(f"{kind}{level}", _line(0, 0, 10, 0, 3), kind, cls, None, level, 2.0, None)
    assert ix._over(mem("rail_deck", 3), mem("rail_deck", 1)) == "i", "(a viaduct over another)"
    assert ix._over(mem("rail_deck", 1), mem("deck", 2, "primary")) == "j"
    assert ix._over(mem("tunnel", -1, "primary"), mem("tunnel", -2, "primary")) == "i", "(tunnels on two levels)"
    assert ix._over(mem("ground", 0, "primary"), mem("rail", -2)) == "i", "(a street over a track in a tunnel)"
    assert ix._over(mem("ground", 0, "primary"), mem("rail", 0)) is None, "(a level crossing)"
    assert ix._over(mem("deck", 1, "primary"), mem("deck", 1, "primary")) is None
    assert ix._over(mem("deck", -1, "primary"), mem("ground", 0, "primary")) == "i", "(a bridge is over the ground)"


def test_a_projection_asked_for_the_near_pairs_alone_keeps_them_to_the_last_bit():
    # (_proj_many's near: the ramp's junctions ask it of every road in the ramp's box and keep a pair by its distance;
    # nothing was within JOIN_M in 39 % of the asks, 70 % of its seconds. A point against the segments whose box grown
    # by near holds it: a bound, so every pair within near is kept, with the full projection's arc and distance, and a
    # point with no such segment is as far as one none is near)
    import dnp
    rng = np.random.default_rng(7)

    def full(line, s, P):
        a, ab = line[:-1], line[1:] - line[:-1]
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((P[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        v = a[None] + t[..., None] * ab[None] - P[:, None, :]
        dist = dnp.hypot(v[..., 0], v[..., 1])
        j = np.argmin(dist, axis=1)
        r = np.arange(len(P))
        return s[j] + t[r, j] * (s[j + 1] - s[j]), dist[r, j]

    for trial in range(120):
        nseg, k = int(rng.integers(3, 300)), int(rng.integers(1, 1600))
        if trial % 3 == 0:       # a wandering line, the points scattered wide
            line = np.cumsum(rng.normal(size=(nseg + 1, 2)) * 30.0, axis=0)
            P = line[0] + rng.uniform(-2000, 2000, size=(k, 2))
        elif trial % 3 == 1:     # the points along the line (a ramp beside the road)
            line = np.cumsum(rng.normal(size=(nseg + 1, 2)) * 5.0, axis=0)
            idx = rng.integers(0, nseg, k)
            P = line[idx] + rng.uniform(size=(k, 1)) * (line[idx + 1] - line[idx]) + rng.normal(scale=ix.JOIN_M * 1.5, size=(k, 2))
        else:                    # a winding line, the points in a thin box across it
            t = np.linspace(0.0, 6.0 * np.pi, nseg + 1)
            line = np.stack([t * 50.0, 400.0 * np.sin(t)], axis=1)
            P = np.stack([rng.uniform(line[:, 0].min(), line[:, 0].max(), k), rng.uniform(-400.0, 400.0, k)], axis=1)
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(line, axis=0).T))]
        sf, df = full(line, s, P)
        sn, dn = ix._proj_many(line, s, P, near=ix.JOIN_M)
        kept = df <= ix.JOIN_M
        assert np.array_equal(kept, dn <= ix.JOIN_M), trial
        assert np.array_equal(sf[kept], sn[kept]), trial
        assert np.array_equal(ix._proj_many(line, s, P)[0], sf) and np.array_equal(ix._proj_many(line, s, P)[1], df)


def test_a_members_laws_are_made_once_and_follow_the_laws_set_anew():
    # (max_grade, curve_radius, deck_grade and deck_depth by the member's law key and the laws' values: asked 160,000
    # times a region for a thousand members. The same floats as unmemoized; a law set anew (grading sets them from
    # roads.py; a test eases one) is made anew, never a stale answer)
    m = Member("x", _line(0, 0, 100, 0), "deck", "secondary", 60, 1.0, 5.0, None)
    for fn in (ix.max_grade, ix.curve_radius, ix.deck_grade):
        assert fn(m) == fn._unmemoized(m)
    assert ix.deck_depth(120.0) == ix._deck_depth_unmemoized(120.0) and ix.deck_depth(120) == ix.deck_depth(120.0)
    was = ix.deck_grade(m)
    ix.DECK_MAX_GRADE = was - 0.01
    try:
        assert ix.deck_grade(m) == was - 0.01 and ix.deck_grade(m) == ix.deck_grade._unmemoized(m)
    finally:
        ix.DECK_MAX_GRADE = was
    assert ix.deck_grade(m) == was
    t = Member("r", _line(0, 0, 100, 0), "rail", "rail", None, 0.0, 2.0, None)
    assert ix.max_grade(t) == ix.railways.GRADE and ix.curve_radius(t) == max(ix.R_MIN_RAIL_M, (100.0 / 3.6) ** 2 / ix.A_VERT_RAIL)


def _scan_rounds(members, rounds, ground):
    """find_interchanges over the members of each round in turn, one Scan kept (a growth's rounds: owner.grow): the
    last round's (net, found)."""
    scan = ix.Scan()
    net = found = None
    for part in rounds:
        net, found = ix.find_interchanges([members[k] for k in part], lambda n_: (ground, lambda i, s: _fixed(n_, ground, i, s)),
                                          scan=scan, reach=True)
    return net, found


def _indexed(members, reach=False):
    """find_interchanges of the members on the process's line network (a growth's round, as owner.grow makes it): (net,
    found, the round's scan)."""
    scan = ix.Scan()
    net, found = ix.find_interchanges(members, lambda n_: (lambda xy: np.zeros(len(xy)), lambda i, s: _fixed(n_, lambda xy: np.zeros(len(xy)), i, s)),
                                      scan=scan, reach=reach)
    return net, found, scan


def _assert_network_exact(net):
    """Every member's relations from the line network against the Python made with no index (every other member of two
    points or more through _pair_relations)."""
    for i in range(len(net.m)):
        if len(net.lines[i]) < 2:
            continue
        whole = [(j,) + r_ for j in range(len(net.m)) if j != i and len(net.lines[j]) >= 2
                 for r_ in ix._pair_relations(net, i, j)]
        got = ix._relations(net, i)
        assert len(got) == len(whole), net.m[i].key
        for a, b in zip(got, whole):
            assert a[:4] == b[:4] and np.asarray(a[4]).tobytes() == np.asarray(b[4]).tobytes(), (net.m[i].key, a, b)


def _chain_members():
    # (a road of 12 parts end to end with forks and strays at the joints, ground roads of one feature and others)
    rng = np.random.default_rng(7)
    m = []
    for k in range(12):
        m.append(Member(("r", k // 3, k), _line(k * 60.0, 0, k * 60.0 + 60.0, 0, 20), "ground", "secondary", 60, 0, 5.0, None))
    for k in range(6):
        y = float(rng.uniform(-3, 3))
        m.append(Member(("f", k), _line(k * 120.0, y, k * 120.0 + 60.0, 25 * (k % 2 - 0.5), 20), "ground", "secondary", 60, 0, 5.0,
                        None))
    return m


def test_next_member_asked_twice_of_a_net_is_what_it_is_computed_afresh():
    # (a dip's road is walked on from both its ends for every crossing under it: next_member keeps its answer per end of
    # the net, and the answer is the one _next_member computes)
    net = ix.Net(_chain_members())
    for i in range(len(net.m)):
        for at_end in (False, True):
            first = ix.next_member(net, i, at_end)
            assert first == ix._next_member(net, i, at_end), (i, at_end)
            assert ix.next_member(net, i, at_end) == first
    whole = ix.walk(net, 0, True, 400.0)
    assert [j for j, _a, _b in whole][:3] == [1, 2, 3], whole
    assert ix.walk(net, 0, True, 400.0) == whole


def test_a_ramp_lands_on_a_line_the_road_can_drive_where_the_ground_rises_off_its_start():
    # a deck's end in a trench: the ground 0.2 m under the deck at its end, 6 m over it a few metres on and level from
    # there (a cut: mirrored). The ramp meets the ground at once, and its landing curve was laid onto a line of the
    # ground's steepest grade over those samples, 0.6 and more, 0.5 rad |g2 - g1| long: a floor 40 m over level ground
    # (A61's motorways, 235-247 m on ground of 199). An embankment stays under the ground it lands on, a cut over it
    rad, g = 934.0, 0.065
    d = np.arange(0.0, 400.0, ix.STEP_M)
    for sign, z0, start, level in ((1.0, 191.2, 191.0, 197.0), (-1.0, 202.8, 203.0, 197.0)):
        ground = np.where(d < 8.0, start, level)
        z, k = ix.ramp(z0, 0.0, sign, g, rad, d, ground)
        assert k > 1
        if sign > 0:
            assert (z[:k] <= np.maximum(ground[:k], z0) + 0.01).all(), (sign, z[:k], ground[:k])
        else:
            assert (z[:k] >= np.minimum(ground[:k], z0) - 0.01).all(), (sign, z[:k], ground[:k])


def test_deck_paths_are_the_shortest_run_over_the_joints_where_it_is_within_the_limit_and_inf_where_it_is_not():
    # (against every joint to every joint of the decks, all pairs: the answer the bounded search stands for)
    rng = np.random.default_rng(7)
    for trial in range(30):
        n = int(rng.integers(3, 9))
        net = type("N", (), {})()
        net.m = [type("M", (), {"kind": "deck" if k < n - 1 else "ramp"})() for k in range(n)]
        joins = []
        for _ in range(int(rng.integers(2, 14))):
            i, j = (int(v) for v in rng.integers(0, n, 2))
            if i != j:
                joins.append((i, float(rng.uniform(0, 300)), j, float(rng.uniform(0, 300))))
        along = ix._deck_paths(net, dict(joins=joins))
        at, k = {}, 0
        for i, si, j, sj in joins:
            if net.m[i].kind == net.m[j].kind == "deck":
                at.setdefault(i, []).append((si, k))
                at.setdefault(j, []).append((sj, k))
                k += 1
        w = np.full((k, k), np.inf)
        np.fill_diagonal(w, 0.0)
        for lst in at.values():
            for sa, a in lst:
                for sb, b in lst:
                    w[a, b] = min(w[a, b], abs(sa - sb))
        for m in range(k):
            w = np.minimum(w, w[:, m:m + 1] + w[m:m + 1, :])
        for _ in range(40):
            i, j = (int(v) for v in rng.integers(0, n, 2))
            si, sj, limit = (float(v) for v in rng.uniform(0, 300, 3))
            best = abs(si - sj) if i == j else np.inf
            for sa, a in at.get(i, ()):
                for sb, b in at.get(j, ()):
                    best = min(best, abs(si - sa) + w[a, b] + abs(sb - sj))
            want = best if best < limit else np.inf
            got = along(i, si, j, sj, limit)
            assert got == want or abs(got - want) < 1e-9 or (got >= limit and want == np.inf), (trial, i, j, got, want)
            full = along(i, si, j, sj)
            assert full == best or abs(full - best) < 1e-9, (trial, i, j, full, best)
