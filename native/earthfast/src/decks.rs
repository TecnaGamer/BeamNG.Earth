//! Bridge decks side by side (roads._deck_beside): which deck parts come within touching distance of each other in
//! plan, the pair test that was a numpy pass per pair (2.4 s of 14 for 8 of Bratislava's road tiles, 2026-10-28).

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Pairs (i, j), i < j, of parts whose samples q (part i's rows qoff[i]..qoff[i + 1]: its line every few m) come within
/// (widths[i] + widths[j]) / 2 + touch of part j's line (pts rows poff[j]..poff[j + 1]) at 2 or more samples. Parts
/// with fewer than 2 vertices are left out, as are pairs whose boxes are farther apart. The numpy version's sums.
#[pyfunction]
pub fn deck_beside<'py>(py: Python<'py>, q: PyReadonlyArray2<'py, f64>, qoff: PyReadonlyArray1<'py, i64>,
                        pts: PyReadonlyArray2<'py, f64>, poff: PyReadonlyArray1<'py, i64>,
                        widths: PyReadonlyArray1<'py, f64>, touch: f64) -> Bound<'py, PyArray2<i64>> {
    let (q, qoff, pts, poff, widths) = (q.as_array(), qoff.as_array(), pts.as_array(), poff.as_array(),
                                        widths.as_array());
    let n = poff.len().saturating_sub(1);
    let out = py.allow_threads(|| {
        let span = |k: usize| (poff[k] as usize, poff[k + 1] as usize);
        // each part's box
        let mut bb = vec![[0.0f64; 4]; n];
        for k in 0..n {
            let (a, b) = span(k);
            if b > a {
                let mut r = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
                for v in a..b {
                    r[0] = r[0].min(pts[[v, 0]]);
                    r[1] = r[1].min(pts[[v, 1]]);
                    r[2] = r[2].max(pts[[v, 0]]);
                    r[3] = r[3].max(pts[[v, 1]]);
                }
                bb[k] = r;
            }
        }
        let mut out: Vec<i64> = Vec::new();
        for i in 0..n {
            let (ia, ib) = span(i);
            if ib < ia + 2 {
                continue;
            }
            let (qa, qb) = (qoff[i] as usize, qoff[i + 1] as usize);
            for j in i + 1..n {
                let (ja, jb) = span(j);
                if jb < ja + 2 {
                    continue;
                }
                let reach = (widths[i] + widths[j]) / 2.0 + touch;
                let (bi, bj) = (bb[i], bb[j]);
                if bi[2] + reach < bj[0] || bi[3] + reach < bj[1] || bj[2] + reach < bi[0] || bj[3] + reach < bi[1] {
                    continue;
                }
                let mut hits = 0;
                for s in qa..qb {
                    let (qx, qy) = (q[[s, 0]], q[[s, 1]]);
                    let mut d = f64::INFINITY;
                    for v in ja..jb - 1 {
                        let (px, py_) = (pts[[v, 0]], pts[[v, 1]]);
                        let (abx, aby) = (pts[[v + 1, 0]] - px, pts[[v + 1, 1]] - py_);
                        let l2 = (abx * abx + aby * aby).max(1e-12);
                        let t = (((qx - px) * abx + (qy - py_) * aby) / l2).clamp(0.0, 1.0);
                        d = d.min((qx - (px + t * abx)).hypot(qy - (py_ + t * aby)));
                    }
                    if d < reach {
                        hits += 1;
                        if hits >= 2 {
                            break;
                        }
                    }
                }
                if hits >= 2 {
                    out.push(i as i64);
                    out.push(j as i64);
                }
            }
        }
        out
    });
    let k = out.len() / 2;
    numpy::ndarray::Array2::from_shape_vec((k, 2), out).expect("pairs").into_pyarray(py)
}

/// The unit direction of a polyline (rows a..b of pts) leaving its start (at_end false) or its end (true), into the
/// line: from that end to the point `look` m along it (or its other end, if shorter).
fn end_dir(pts: &numpy::ndarray::ArrayView2<f64>, a: usize, b: usize, at_end: bool, look: f64) -> [f64; 2] {
    let idx = |k: usize| if at_end { b - 1 - k } else { a + k };
    let (x0, y0) = (pts[[idx(0), 0]], pts[[idx(0), 1]]);
    let (mut tx, mut ty) = (pts[[idx(b - a - 1), 0]], pts[[idx(b - a - 1), 1]]);
    let mut run = 0.0;
    for k in 1..b - a {
        let (px, py) = (pts[[idx(k - 1), 0]], pts[[idx(k - 1), 1]]);
        let (qx, qy) = (pts[[idx(k), 0]], pts[[idx(k), 1]]);
        let seg = (qx - px).hypot(qy - py);
        if run + seg >= look && seg > 0.0 {
            let t = (look - run) / seg;
            tx = px + t * (qx - px);
            ty = py + t * (qy - py);
            break;
        }
        run += seg;
    }
    let (dx, dy) = (tx - x0, ty - y0);
    let l = dx.hypot(dy);
    if l > 1e-9 { [dx / l, dy / l] } else { [0.0, 0.0] }
}

/// Where the free ends of deck parts join another part of their road (roads.BridgeProfiles: a carriageway's split or
/// merge the map draws a few metres off the joint, Overture's lines not meeting: the branch's end was taken for an
/// abutment and held at the ground under the deck, a cliff of 7-14 m across a motorway's lanes, I-83 in Baltimore and
/// the Springfield Interchange, 2026-10-29). Parts: pts rows poff[k]..poff[k + 1] (m, the anchor frame), half[k] their
/// half widths; ground (m, 2): the ends of the ground roads near them. End e = 2k (part k's start) or 2k + 1 (its
/// end) is free when no other part's end lies within join_m (roads._EndJoin's tolerance) and no ground road's end
/// within ground_m (a road going on on the ground: an abutment). A free end of a part at least min_len long joins
/// the nearest end of another part within min(snap_m, the larger half width) whose direction into its part runs
/// along this one's (|cos| >= min_cos: a split, a merge or the road going on, not a road crossing). Ends are taken in
/// order: a free end joins only non-free ends or free ends before it (at their joined place), so two free ends facing
/// each other meet at the first one's. params: join_m, snap_m, min_cos, ground_m, min_len. (2 * parts, 2): each
/// end's joined place, NaN where it stays.
#[pyfunction]
pub fn deck_end_joins<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, poff: PyReadonlyArray1<'py, i64>,
                           half: PyReadonlyArray1<'py, f64>, ground: PyReadonlyArray2<'py, f64>,
                           params: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyArray2<f64>> {
    let (pts, poff, half, ground, params) = (pts.as_array(), poff.as_array(), half.as_array(), ground.as_array(),
                                             params.as_array());
    let (join_m, snap_m, min_cos, ground_m, min_len) = (params[0], params[1], params[2], params[3], params[4]);
    let n = poff.len().saturating_sub(1);
    let out = py.allow_threads(|| {
        let ne = 2 * n;
        let mut pos = vec![[f64::NAN; 2]; ne];
        let mut dir = vec![[0.0f64; 2]; ne];
        let mut len = vec![0.0f64; n];
        let mut ok = vec![false; n];
        for k in 0..n {
            let (a, b) = (poff[k] as usize, poff[k + 1] as usize);
            if b < a + 2 {
                continue;
            }
            ok[k] = true;
            let mut l = 0.0;
            for v in a..b - 1 {
                l += (pts[[v + 1, 0]] - pts[[v, 0]]).hypot(pts[[v + 1, 1]] - pts[[v, 1]]);
            }
            len[k] = l;
            pos[2 * k] = [pts[[a, 0]], pts[[a, 1]]];
            pos[2 * k + 1] = [pts[[b - 1, 0]], pts[[b - 1, 1]]];
            let look = (0.5 * l).min(5.0);
            dir[2 * k] = end_dir(&pts, a, b, false, look);
            dir[2 * k + 1] = end_dir(&pts, a, b, true, look);
        }
        let d = |p: [f64; 2], q: [f64; 2]| (p[0] - q[0]).hypot(p[1] - q[1]);
        // a grid of the ends and of the ground roads' ends (cells of the widest reach): the near ones only
        // (twice the reach: a free end before this one may have moved up to snap_m from where it is filed)
        let cell = 2.0 * join_m.max(ground_m).max(snap_m).max(1.0);
        let key = |p: [f64; 2]| ((p[0] / cell).floor() as i64, (p[1] / cell).floor() as i64);
        let mut grid: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
        for e in 0..ne {
            if ok[e / 2] {
                grid.entry(key(pos[e])).or_default().push(e);
            }
        }
        let mut ggrid: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
        for g in 0..ground.nrows() {
            ggrid.entry(key([ground[[g, 0]], ground[[g, 1]]])).or_default().push(g);
        }
        let near = |grid: &std::collections::HashMap<(i64, i64), Vec<usize>>, p: [f64; 2]| -> Vec<usize> {
            let (gx, gy) = key(p);
            let mut v = Vec::new();
            for dx in -1..=1 {
                for dy in -1..=1 {
                    if let Some(c) = grid.get(&(gx + dx, gy + dy)) {
                        v.extend_from_slice(c);
                    }
                }
            }
            v.sort_unstable();
            v
        };
        let mut free = vec![false; ne];
        for e in 0..ne {
            if !ok[e / 2] {
                continue;
            }
            let joined = near(&grid, pos[e]).into_iter().any(|f| f != e && d(pos[e], pos[f]) < join_m);
            let grounded = near(&ggrid, pos[e]).into_iter()
                .any(|g| d(pos[e], [ground[[g, 0]], ground[[g, 1]]]) < ground_m);
            free[e] = !joined && !grounded;
        }
        let mut at = pos.clone();           // (where each end is once joined)
        let mut out = vec![f64::NAN; 2 * ne];
        for e in 0..ne {
            let k = e / 2;
            if !free[e] || len[k] < min_len {
                continue;
            }
            let mut best: Option<(f64, usize)> = None;
            for f in near(&grid, pos[e]) {
                let j = f / 2;
                if j == k || (free[f] && f > e) {
                    continue;
                }
                let r = snap_m.min(half[k].max(half[j]));
                let dd = d(pos[e], at[f]);
                if dd > r {
                    continue;
                }
                let c = (dir[e][0] * dir[f][0] + dir[e][1] * dir[f][1]).abs();
                if c < min_cos {
                    continue;
                }
                if best.map_or(true, |(bd, bf)| dd < bd || (dd == bd && f < bf)) {
                    best = Some((dd, f));
                }
            }
            if let Some((_, f)) = best {
                at[e] = at[f];
                out[2 * e] = at[f][0];
                out[2 * e + 1] = at[f][1];
            }
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((2 * n, 2), out).expect("joins").into_pyarray(py)
}

/// The ground gaps of one road between decks made deck (roads.level_ranges: a few metres of a viaduct the map leaves
/// untagged between two bridges are drawn on the ground, each deck eased down to them and up again). line (k, 2): the
/// road's line in m; bridges (r, 2): its bridge ranges, m along it (ascending, apart); end_deck[0 / 1]: a deck goes on
/// from its start / end (another road's bridge ends there and nothing else meets it); others (m, 2): the ends of the
/// other roads near it (junctions). A ground piece shorter than gap_m with a deck on both sides (a bridge range of its
/// own, or end_deck at a road end) becomes bridge unless another road's end lies within junction_m of it (a junction
/// at grade between two bridges: kept on the ground). Pure. (r', 2): the bridge ranges after, in m, ascending.
pub fn fill_gaps(line: &[[f64; 2]], bridges: &[[f64; 2]], end_deck: [bool; 2], others: &[[f64; 2]], gap_m: f64,
                 junction_m: f64) -> Vec<[f64; 2]> {
    let n = line.len();
    let mut s = vec![0.0f64; n];
    for v in 1..n {
        s[v] = s[v - 1] + (line[v][0] - line[v - 1][0]).hypot(line[v][1] - line[v - 1][1]);
    }
    let total = if n > 0 { s[n - 1] } else { 0.0 };
    if n < 2 || total <= 0.0 {
        return bridges.to_vec();
    }
    // the distance from point p to the line between arcs a and b
    let near = |p: [f64; 2], a: f64, b: f64| -> f64 {
        let mut best = f64::INFINITY;
        let at = |t: f64| -> [f64; 2] {
            let v = s.partition_point(|&x| x < t).clamp(1, n - 1);
            let seg = (s[v] - s[v - 1]).max(1e-12);
            let u = ((t - s[v - 1]) / seg).clamp(0.0, 1.0);
            [line[v - 1][0] + u * (line[v][0] - line[v - 1][0]), line[v - 1][1] + u * (line[v][1] - line[v - 1][1])]
        };
        let mut pts = vec![at(a)];
        for v in 0..n {
            if s[v] > a && s[v] < b {
                pts.push(line[v]);
            }
        }
        pts.push(at(b));
        for w in pts.windows(2) {
            let (px, py) = (w[0][0], w[0][1]);
            let (abx, aby) = (w[1][0] - px, w[1][1] - py);
            let l2 = (abx * abx + aby * aby).max(1e-12);
            let t = (((p[0] - px) * abx + (p[1] - py) * aby) / l2).clamp(0.0, 1.0);
            best = best.min((p[0] - (px + t * abx)).hypot(p[1] - (py + t * aby)));
        }
        best
    };
    // the ground pieces (a, b, deck before, deck after)
    let mut pieces: Vec<(f64, f64, bool, bool)> = Vec::new();
    let mut at = 0.0;
    let mut deck_before = end_deck[0];
    for r in bridges {
        if r[0] > at {
            pieces.push((at, r[0], deck_before, true));
        }
        at = at.max(r[1]);
        deck_before = true;
    }
    if total > at {
        pieces.push((at, total, deck_before, end_deck[1]));
    }
    let mut out: Vec<[f64; 2]> = bridges.to_vec();
    for (a, b, d0, d1) in pieces {
        if !(d0 && d1) || b - a >= gap_m {
            continue;
        }
        // (a junction at either end of the road was ruled out by end_deck; inside it, any road ending on the piece)
        let (ia, ib) = (if a <= 0.0 { junction_m } else { 0.0 }, if b >= total { junction_m } else { 0.0 });
        let lo = a + ia;
        let hi = (b - ib).max(lo);
        if others.iter().any(|&p| near(p, lo, hi) < junction_m) {
            continue;
        }
        out.push([a, b]);
    }
    out.sort_by(|x, y| x[0].partial_cmp(&y[0]).unwrap_or(std::cmp::Ordering::Equal));
    let mut merged: Vec<[f64; 2]> = Vec::new();
    for r in out {
        if let Some(last) = merged.last_mut() {
            if r[0] <= last[1] + 1e-9 {
                last[1] = last[1].max(r[1]);
                continue;
            }
        }
        merged.push(r);
    }
    merged
}

/// fill_gaps for Python: line (k, 2) m, bridges (r, 2) m along it, end_deck (2,) 0 / 1, others (m, 2), gap_m,
/// junction_m. (r', 2) the bridge ranges after, m.
#[pyfunction]
pub fn deck_gaps<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, bridges: PyReadonlyArray2<'py, f64>,
                      end_deck: PyReadonlyArray1<'py, i64>, others: PyReadonlyArray2<'py, f64>, gap_m: f64,
                      junction_m: f64) -> Bound<'py, PyArray2<f64>> {
    let rows = |a: &numpy::ndarray::ArrayView2<f64>| -> Vec<[f64; 2]> {
        (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect()
    };
    let (l, b, o) = (rows(&line.as_array()), rows(&bridges.as_array()), rows(&others.as_array()));
    let ed = end_deck.as_array();
    let got = fill_gaps(&l, &b, [ed.len() > 0 && ed[0] != 0, ed.len() > 1 && ed[1] != 0], &o, gap_m, junction_m);
    let flat: Vec<f64> = got.iter().flat_map(|r| [r[0], r[1]]).collect();
    numpy::ndarray::Array2::from_shape_vec((got.len(), 2), flat).expect("ranges").into_pyarray(py)
}

/// The free deck ends that stand on another deck's carriageway (a ramp's end on the cross street's bridge, a junction
/// the map draws a few metres off the other road's line; held at the ground as an abutment, the ramp stood as a wall
/// at the joint, the Springfield Interchange, 2026-10-29). Parts as deck_end_joins' (pts rows poff[k]..poff[k + 1],
/// half widths half[k]), level[k] their levels (Overture's: a deck over another has a higher one), ground (m, 2) the
/// ground roads' ends. An end is free as there (join_m, ground_m); it stands on part j (another, of its level) when
/// its distance to j's line is within half[j] + margin_m (the nearest such j). (2 * parts, 2): per end the part j it
/// stands on and the arc along j's line there (m), -1 / NaN where none. params: join_m, ground_m, margin_m.
#[pyfunction]
pub fn deck_end_on<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, poff: PyReadonlyArray1<'py, i64>,
                        half: PyReadonlyArray1<'py, f64>, level: PyReadonlyArray1<'py, f64>,
                        ground: PyReadonlyArray2<'py, f64>, params: PyReadonlyArray1<'py, f64>)
                        -> Bound<'py, PyArray2<f64>> {
    let (pts, poff, half, level, ground, params) = (pts.as_array(), poff.as_array(), half.as_array(),
                                                    level.as_array(), ground.as_array(), params.as_array());
    let (join_m, ground_m, margin_m) = (params[0], params[1], params[2]);
    let n = poff.len().saturating_sub(1);
    let out = py.allow_threads(|| {
        let span = |k: usize| (poff[k] as usize, poff[k + 1] as usize);
        let p = |v: usize| [pts[[v, 0]], pts[[v, 1]]];
        let d = |a: [f64; 2], b: [f64; 2]| (a[0] - b[0]).hypot(a[1] - b[1]);
        let mut ends: Vec<Option<[f64; 2]>> = vec![None; 2 * n];
        for k in 0..n {
            let (a, b) = span(k);
            if b >= a + 2 {
                ends[2 * k] = Some(p(a));
                ends[2 * k + 1] = Some(p(b - 1));
            }
        }
        // grids of the ends and the ground roads' ends (a pool of a few thousand parts: the pairs alone were seconds)
        let cell = join_m.max(ground_m).max(1.0);
        let key = |q: [f64; 2]| ((q[0] / cell).floor() as i64, (q[1] / cell).floor() as i64);
        let mut eg: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
        for (e, q) in ends.iter().enumerate() {
            if let Some(q) = q {
                eg.entry(key(*q)).or_default().push(e);
            }
        }
        let mut gg: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
        for g in 0..ground.nrows() {
            gg.entry(key([ground[[g, 0]], ground[[g, 1]]])).or_default().push(g);
        }
        let near = |grid: &std::collections::HashMap<(i64, i64), Vec<usize>>, q: [f64; 2]| -> Vec<usize> {
            let (gx, gy) = key(q);
            let mut v = Vec::new();
            for dx in -1..=1 {
                for dy in -1..=1 {
                    if let Some(c) = grid.get(&(gx + dx, gy + dy)) {
                        v.extend_from_slice(c);
                    }
                }
            }
            v
        };
        // each part's box, grown by its half width and the margin
        let mut bb = vec![[f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY]; n];
        for k in 0..n {
            let (a, b) = span(k);
            for v in a..b {
                let q = p(v);
                bb[k] = [bb[k][0].min(q[0]), bb[k][1].min(q[1]), bb[k][2].max(q[0]), bb[k][3].max(q[1])];
            }
            let r = half[k] + margin_m;
            bb[k] = [bb[k][0] - r, bb[k][1] - r, bb[k][2] + r, bb[k][3] + r];
        }
        let mut out = vec![-1.0f64; 4 * n];
        for e in 0..2 * n {
            out[2 * e + 1] = f64::NAN;
            let Some(q) = ends[e] else { continue };
            let k = e / 2;
            let joined = near(&eg, q).into_iter().any(|f| f != e && ends[f].map_or(false, |r| d(q, r) < join_m));
            let grounded = near(&gg, q).into_iter().any(|g| d(q, [ground[[g, 0]], ground[[g, 1]]]) < ground_m);
            if joined || grounded {
                continue;
            }
            let mut best: Option<(f64, usize, f64)> = None;
            for j in 0..n {
                let (a, b) = span(j);
                if j == k || b < a + 2 || (level[j] - level[k]).abs() > 1e-6 || q[0] < bb[j][0] || q[1] < bb[j][1]
                    || q[0] > bb[j][2] || q[1] > bb[j][3] {
                    continue;
                }
                let mut run = 0.0;
                for v in a..b - 1 {
                    let (s0, s1) = (p(v), p(v + 1));
                    let (abx, aby) = (s1[0] - s0[0], s1[1] - s0[1]);
                    let l2 = abx * abx + aby * aby;
                    let t = if l2 > 1e-12 { (((q[0] - s0[0]) * abx + (q[1] - s0[1]) * aby) / l2).clamp(0.0, 1.0) }
                            else { 0.0 };
                    let dd = d(q, [s0[0] + t * abx, s0[1] + t * aby]);
                    if dd <= half[j] + margin_m && best.map_or(true, |(bd, _, _)| dd < bd) {
                        best = Some((dd, j, run + t * l2.sqrt()));
                    }
                    run += l2.sqrt();
                }
            }
            if let Some((_, j, s)) = best {
                out[2 * e] = j as f64;
                out[2 * e + 1] = s;
            }
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((2 * n, 2), out).expect("ends").into_pyarray(py)
}

/// The paved pad round a junction on a bridge (roads.deck_pads): the convex hull of the carriageways' footprints
/// within `reach` of the joint, each part a line (rows a..b of pts with heights z, starting at the joint) of half
/// width half[k], sampled every `step` m (its two edges). Between the parts the map leaves open ground under the
/// joint: a right turn's corner, the wedge where a ramp leaves, a narrower street's edge meeting a wider deck (Charles
/// Street over I-83 in Baltimore: the AI cut the corner to the station's road over the cut, 2026-10-29). The hull's
/// vertices (k, 3) counter-clockwise, each at the height of the edge point it is; empty when degenerate.
pub fn pad_hull(parts: &[(Vec<[f64; 2]>, Vec<f64>, f64)], reach: f64, step: f64) -> Vec<[f64; 3]> {
    let mut pts: Vec<[f64; 3]> = Vec::new();
    for (line, z, half) in parts {
        let n = line.len();
        if n < 2 {
            continue;
        }
        let mut s = vec![0.0f64; n];
        for v in 1..n {
            s[v] = s[v - 1] + (line[v][0] - line[v - 1][0]).hypot(line[v][1] - line[v - 1][1]);
        }
        let end = s[n - 1].min(reach);
        let mut t = 0.0;
        loop {
            let v = s.partition_point(|&x| x < t).clamp(1, n - 1);
            let seg = (s[v] - s[v - 1]).max(1e-9);
            let u = ((t - s[v - 1]) / seg).clamp(0.0, 1.0);
            let (dx, dy) = ((line[v][0] - line[v - 1][0]) / seg, (line[v][1] - line[v - 1][1]) / seg);
            let p = [line[v - 1][0] + u * (line[v][0] - line[v - 1][0]), line[v - 1][1] + u * (line[v][1] - line[v - 1][1])];
            let zz = z[v - 1] + u * (z[v] - z[v - 1]);
            pts.push([p[0] - dy * half, p[1] + dx * half, zz]);
            pts.push([p[0] + dy * half, p[1] - dx * half, zz]);
            if t >= end {
                break;
            }
            t = (t + step).min(end);
        }
    }
    if pts.len() < 3 {
        return Vec::new();
    }
    // Andrew's monotone chain (x, then y; ties: the lower index first: deterministic)
    let mut idx: Vec<usize> = (0..pts.len()).collect();
    idx.sort_by(|&a, &b| pts[a][0].partial_cmp(&pts[b][0]).unwrap_or(std::cmp::Ordering::Equal)
        .then(pts[a][1].partial_cmp(&pts[b][1]).unwrap_or(std::cmp::Ordering::Equal)).then(a.cmp(&b)));
    let cross = |o: [f64; 3], a: [f64; 3], b: [f64; 3]| (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    let mut hull: Vec<[f64; 3]> = Vec::new();
    for pass in 0..2 {
        let start = hull.len();
        let order: Vec<usize> = if pass == 0 { idx.clone() } else { idx.iter().rev().cloned().collect() };
        for &i in &order {
            let p = pts[i];
            while hull.len() >= start + 2 && cross(hull[hull.len() - 2], hull[hull.len() - 1], p) <= 1e-9 {
                hull.pop();
            }
            hull.push(p);
        }
        hull.pop();
    }
    if hull.len() < 3 { Vec::new() } else { hull }
}

/// pad_hull for Python: pts (N, 2), z (N,), off (n + 1,) the parts' rows (each starting at the joint), half (n,),
/// reach, step. (k, 3) the hull, counter-clockwise.
#[pyfunction]
pub fn deck_pad<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                     off: PyReadonlyArray1<'py, i64>, half: PyReadonlyArray1<'py, f64>, reach: f64, step: f64)
                     -> Bound<'py, PyArray2<f64>> {
    let (pts, z, off, half) = (pts.as_array(), z.as_array(), off.as_array(), half.as_array());
    let mut parts = Vec::new();
    for k in 0..off.len().saturating_sub(1) {
        let (a, b) = (off[k] as usize, off[k + 1] as usize);
        parts.push(((a..b).map(|v| [pts[[v, 0]], pts[[v, 1]]]).collect::<Vec<_>>(), (a..b).map(|v| z[v]).collect::<Vec<_>>(),
                    half[k]));
    }
    let hull = pad_hull(&parts, reach, step.max(0.5));
    let flat: Vec<f64> = hull.iter().flat_map(|p| [p[0], p[1], p[2]]).collect();
    numpy::ndarray::Array2::from_shape_vec((hull.len(), 3), flat).expect("hull").into_pyarray(py)
}

/// A junction pad's rim (ring (k, 3), counter-clockwise) sampled every `step` m: per sample x, y, z, the outward unit
/// normal, and whether a carriageway leaves the pad there (open: within its half width + open_m of one of the lines,
/// rows off[j]..off[j + 1] of pts, half widths half[j]): its railing goes round the rest (roads.deck_pads). Pure.
pub fn rim_samples(ring: &[[f64; 3]], lines: &[(Vec<[f64; 2]>, f64)], step: f64, open_m: f64) -> Vec<[f64; 6]> {
    let k = ring.len();
    let mut out = Vec::new();
    if k < 3 {
        return out;
    }
    for i in 0..k {
        let (a, b) = (ring[i], ring[(i + 1) % k]);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let len = dx.hypot(dy);
        if len < 1e-9 {
            continue;
        }
        let (nx, ny) = (dy / len, -dx / len);      // (counter-clockwise: the right of the edge is outside)
        let n = (len / step).ceil().max(1.0) as usize;
        for j in 0..n {
            let u = j as f64 / n as f64;
            let p = [a[0] + u * dx, a[1] + u * dy, a[2] + u * (b[2] - a[2])];
            let open = lines.iter().any(|(line, half)| {
                line.windows(2).any(|w| {
                    let (sx, sy) = (w[1][0] - w[0][0], w[1][1] - w[0][1]);
                    let l2 = (sx * sx + sy * sy).max(1e-12);
                    let t = (((p[0] - w[0][0]) * sx + (p[1] - w[0][1]) * sy) / l2).clamp(0.0, 1.0);
                    (p[0] - (w[0][0] + t * sx)).hypot(p[1] - (w[0][1] + t * sy)) <= half + open_m
                })
            });
            out.push([p[0], p[1], p[2], nx, ny, if open { 1.0 } else { 0.0 }]);
        }
    }
    out
}

/// rim_samples for Python: ring (k, 3), pts (N, 2), off (n + 1,), half (n,), step, open_m. (m, 6).
#[pyfunction]
pub fn deck_pad_rim<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>,
                         off: PyReadonlyArray1<'py, i64>, half: PyReadonlyArray1<'py, f64>, step: f64, open_m: f64)
                         -> Bound<'py, PyArray2<f64>> {
    let (ring, pts, off, half) = (ring.as_array(), pts.as_array(), off.as_array(), half.as_array());
    let r: Vec<[f64; 3]> = (0..ring.nrows()).map(|i| [ring[[i, 0]], ring[[i, 1]], ring[[i, 2]]]).collect();
    let lines: Vec<(Vec<[f64; 2]>, f64)> = (0..off.len().saturating_sub(1)).map(|j| {
        ((off[j] as usize..off[j + 1] as usize).map(|v| [pts[[v, 0]], pts[[v, 1]]]).collect(), half[j])
    }).collect();
    let got = rim_samples(&r, &lines, step.max(0.25), open_m);
    let flat: Vec<f64> = got.iter().flat_map(|s| s.iter().cloned()).collect();
    numpy::ndarray::Array2::from_shape_vec((got.len(), 6), flat).expect("rim").into_pyarray(py)
}

#[cfg(test)]
mod tests {
    use super::{fill_gaps, pad_hull};

    #[test]
    fn pad_covers_the_corner() {
        // a street north (half 5) and a ramp leaving north-east (half 1.75) from a joint at 10 m
        let north = (vec![[0.0, 0.0], [0.0, 20.0]], vec![10.0, 10.0], 5.0);
        let ne = (vec![[0.0, 0.0], [14.0, 14.0]], vec![10.0, 10.0], 1.75);
        let south = (vec![[0.0, 0.0], [0.0, -20.0]], vec![10.0, 10.0], 4.0);
        let hull = pad_hull(&[north, ne, south], 12.0, 2.0);
        assert!(hull.len() >= 4);
        let inside = |x: f64, y: f64| (0..hull.len()).all(|i| {
            let (a, b) = (hull[i], hull[(i + 1) % hull.len()]);
            (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]) >= -1e-9
        });
        assert!(inside(6.0, 3.0), "the corner between the street's edge and the ramp");
        assert!(!inside(20.0, 0.0));
        assert!(hull.iter().all(|p| (p[2] - 10.0).abs() < 1e-9));
    }

    #[test]
    fn a_short_gap_between_decks_is_deck() {
        let line = [[0.0, 0.0], [300.0, 0.0]];
        let got = fill_gaps(&line, &[[0.0, 100.0], [140.0, 300.0]], [false, false], &[], 60.0, 1.5);
        assert_eq!(got, vec![[0.0, 300.0]]);
    }

    #[test]
    fn a_long_gap_or_a_junction_stays_ground() {
        let line = [[0.0, 0.0], [300.0, 0.0]];
        let b = [[0.0, 100.0], [180.0, 300.0]];
        assert_eq!(fill_gaps(&line, &b, [false, false], &[], 60.0, 1.5), b.to_vec());
        let b2 = [[0.0, 100.0], [140.0, 300.0]];
        assert_eq!(fill_gaps(&line, &b2, [false, false], &[[120.0, 1.0]], 60.0, 1.5), b2.to_vec());
    }

    #[test]
    fn an_end_piece_needs_a_deck_going_on() {
        let line = [[0.0, 0.0], [100.0, 0.0]];
        let b = [[30.0, 100.0]];
        assert_eq!(fill_gaps(&line, &b, [false, false], &[], 60.0, 1.5), b.to_vec());
        assert_eq!(fill_gaps(&line, &b, [true, false], &[], 60.0, 1.5), vec![[0.0, 100.0]]);
        // (the other road at the joint itself is the deck going on: not a junction)
        assert_eq!(fill_gaps(&line, &b, [true, false], &[[0.0, 0.5]], 60.0, 1.5), vec![[0.0, 100.0]]);
        // a whole road between two decks
        assert_eq!(fill_gaps(&line, &[], [true, true], &[], 120.0, 1.5), vec![[0.0, 100.0]]);
    }
}
