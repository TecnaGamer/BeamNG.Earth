//! gradesep.py: the grade separations the map has no bridge for (GitHub #387: a railway over a trunk road drawn as a
//! hump; a flyover's untagged middle stretch; a road over a railway's short tunnel). Two calls: the crossings of the
//! ground lines with where to read the ground on each side (gradesep_candidates), then, with those heights, the
//! bridge spans on the upper lines (gradesep_spans). Pure and deterministic for a given input order.

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

const ROAD: i64 = 0;
const RAIL: i64 = 1;
const UNDER: i64 = 2; // a railway's short tunnel: under the road crossing it
const UNDER_ROAD: i64 = 3; // a road's underpass mapped as a short tunnel: under the road or railway crossing it

fn under(k: i64) -> bool {
    k == UNDER || k == UNDER_ROAD
}

struct Lines<'a> {
    xy: numpy::ndarray::ArrayView2<'a, f64>,
    st: Vec<usize>,
    arc: Vec<f64>,                               // per point: m along its line
    ends: std::cell::OnceCell<HashMap<(i64, i64), Vec<(usize, bool)>>>, // (line, its last point) by ENDS_G grid
                                                                         // square, made when first asked (next())
}

const ENDS_G: f64 = 4.0; // m: the end points' grid (next(): tol at most this)

impl<'a> Lines<'a> {
    fn new(xy: numpy::ndarray::ArrayView2<'a, f64>, starts: numpy::ndarray::ArrayView1<'a, i64>) -> Self {
        let st: Vec<usize> = starts.iter().map(|&v| v as usize).collect();
        let mut arc = vec![0.0; xy.nrows()];
        for l in 0..st.len().saturating_sub(1) {
            for k in st[l] + 1..st[l + 1] {
                arc[k] = arc[k - 1] + (xy[[k, 0]] - xy[[k - 1, 0]]).hypot(xy[[k, 1]] - xy[[k - 1, 1]]);
            }
        }
        Lines { xy, st, arc, ends: std::cell::OnceCell::new() }
    }
    fn ends(&self) -> &HashMap<(i64, i64), Vec<(usize, bool)>> {
        self.ends.get_or_init(|| {
            let (xy, st) = (&self.xy, &self.st);
            let mut ends: HashMap<(i64, i64), Vec<(usize, bool)>> = HashMap::new();
            for l in 0..st.len().saturating_sub(1) {
                if st[l + 1] < st[l] + 2 {
                    continue;
                }
                for last in [false, true] {
                    let k = if last { st[l + 1] - 1 } else { st[l] };
                    ends.entry(((xy[[k, 0]] / ENDS_G).floor() as i64, (xy[[k, 1]] / ENDS_G).floor() as i64))
                        .or_default()
                        .push((l, last));
                }
            }
            ends
        })
    }
    fn n(&self) -> usize {
        self.st.len().saturating_sub(1)
    }
    fn len(&self, l: usize) -> f64 {
        if self.st[l + 1] > self.st[l] { self.arc[self.st[l + 1] - 1] } else { 0.0 }
    }
    fn end(&self, l: usize, last: bool) -> (f64, f64) {
        let k = if last { self.st[l + 1] - 1 } else { self.st[l] };
        (self.xy[[k, 0]], self.xy[[k, 1]])
    }
    /// The point t m along line l, None off its ends.
    fn at(&self, l: usize, t: f64) -> Option<(f64, f64)> {
        let (i0, i1) = (self.st[l], self.st[l + 1]);
        if i1 < i0 + 2 || t < 0.0 || t > self.arc[i1 - 1] {
            return None;
        }
        for k in i0 + 1..i1 {
            if self.arc[k] >= t {
                let d = self.arc[k] - self.arc[k - 1];
                let f = if d > 0.0 { (t - self.arc[k - 1]) / d } else { 0.0 };
                return Some((
                    self.xy[[k - 1, 0]] + f * (self.xy[[k, 0]] - self.xy[[k - 1, 0]]),
                    self.xy[[k - 1, 1]] + f * (self.xy[[k, 1]] - self.xy[[k - 1, 1]]),
                ));
            }
        }
        Some(self.end(l, true))
    }
    /// Unit direction of line l at its start (pointing into it) or at its end (pointing out of it).
    fn dir(&self, l: usize, last: bool) -> (f64, f64) {
        let (i0, i1) = (self.st[l], self.st[l + 1]);
        let (a, b) = if last { (i1 - 2, i1 - 1) } else { (i0, i0 + 1) };
        let (dx, dy) = (self.xy[[b, 0]] - self.xy[[a, 0]], self.xy[[b, 1]] - self.xy[[a, 1]]);
        let n = dx.hypot(dy).max(1e-12);
        (dx / n, dy / n)
    }
    /// The straightest line (not UNDER, a railway for a railway, a road for a road) joined to line l's end (last) or
    /// start within tol m and turning less than acos turn: (line, its end there is its last point).
    fn next(&self, l: usize, last: bool, kind: &numpy::ndarray::ArrayView1<i64>, tol: f64, turn: f64) -> Option<(usize, bool)> {
        let p = self.end(l, last);
        let d = self.dir(l, last);
        let d = if last { d } else { (-d.0, -d.1) }; // the way out of l at that end
        let mut best: Option<(f64, usize, bool)> = None;
        let (gx, gy) = ((p.0 / ENDS_G).floor() as i64, (p.1 / ENDS_G).floor() as i64);
        let mut near: Vec<(usize, bool)> = Vec::new();
        for dx in -1..=1 {
            for dy in -1..=1 {
                if let Some(v) = self.ends().get(&(gx + dx, gy + dy)) {
                    near.extend(v.iter().copied());
                }
            }
        }
        near.sort_unstable();       // (in the order a loop over every line took them: ties go the same way)
        for (o, oend) in near {
            if o == l || under(kind[o]) || (kind[o] == RAIL) != (kind[l] == RAIL) {
                continue;
            }
            {
                let q = self.end(o, oend);
                if (q.0 - p.0).hypot(q.1 - p.1) >= tol {
                    continue;
                }
                let e = self.dir(o, oend);
                let e = if oend { (-e.0, -e.1) } else { e }; // the way into o from that end
                let c = d.0 * e.0 + d.1 * e.1;
                if c >= turn && best.map_or(true, |(bc, _, _)| c > bc) {
                    best = Some((c, o, oend));
                }
            }
        }
        best.map(|(_, o, oend)| (o, oend))
    }
    /// at(), past line l's ends on along the line joined there (next: one step).
    fn at_on(&self, l: usize, t: f64, kind: &numpy::ndarray::ArrayView1<i64>, tol: f64, turn: f64) -> Option<(f64, f64)> {
        let len = self.len(l);
        if t >= 0.0 && t <= len {
            return self.at(l, t);
        }
        let (last, over) = if t > len { (true, t - len) } else { (false, -t) };
        let (o, oend) = self.next(l, last, kind, tol, turn)?;
        self.at(o, if oend { self.len(o) - over } else { over })
    }
    fn bbox(&self, l: usize) -> (f64, f64, f64, f64) {
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in self.st[l]..self.st[l + 1] {
            x0 = x0.min(self.xy[[k, 0]]);
            x1 = x1.max(self.xy[[k, 0]]);
            y0 = y0.min(self.xy[[k, 1]]);
            y1 = y1.max(self.xy[[k, 1]]);
        }
        (x0, y0, x1, y1)
    }
}

/// The crossings of the ground lines (xy, starts; kind 0 road, 1 railway, 2 a railway's short tunnel, 3 a road's
/// underpass mapped as a short tunnel; half widths m;
/// group: the feature, whose pieces never cross each other) that are no junction (no end point of either within prm[0] m), and where to read the ground: rows of cand (i, j, arc
/// on i, arc on j, sin of the angle, how) and 8 sample points per row in samples (i at -d2, -d1, +d1, +d2 m along it
/// from the crossing, then j; NaN off a line's end; d1 the other's half width over the sine (at least prm[3]) + prm[1],
/// d2 = d1 + prm[2]; past a line's end on along the line joined there, as gradesep_spans goes on). how: 0 compare the heights; 1 i passes over j's short tunnel (a railway's: i a road); 2 i is a gap between two decks
/// (both its ends within prm[4] m of deck_ends (rows x, y, that deck's length m), shorter than prm[5] m) crossing a
/// ground line, or shorter than prm[7] times the longer deck it joins: a bridge whole. One row per pair of lines and
/// crossing (a crossing at a shared point of two segments counted once). prm[8] (optional) non-zero: the how 1 rows
/// alone (only the pairs with a short tunnel are tried: gradesep.py's tunnels-only mode, which keeps no other; the
/// same rows in the same order as the others left out of the whole answer).
#[pyfunction]
pub fn gradesep_candidates<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    starts: PyReadonlyArray1<'py, i64>,
    kind: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
    group: PyReadonlyArray1<'py, i64>,
    deck_ends: PyReadonlyArray2<'py, f64>,
    prm: PyReadonlyArray1<'py, f64>,
) -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let (xy, starts, kind, half, group, de, prm) = (xy.as_array(), starts.as_array(), kind.as_array(), half.as_array(),
                                                   group.as_array(), deck_ends.as_array(), prm.as_array());
    let (end_m, gap, step, min_sin, touch, gap_max, turn, gap_share) =
        (prm[0], prm[1], prm[2], prm[3], prm[4], prm[5], prm[6], prm[7]);
    let under_only = prm.len() > 8 && prm[8] != 0.0;
    let ln = Lines::new(xy, starts);
    let n = ln.n();
    // the longest deck ending at p (deck_ends rows x, y, its deck's length), 0 none
    let tg = touch.max(1e-3);
    let mut ends_grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for k in 0..de.nrows() {
        ends_grid.entry(((de[[k, 0]] / tg).floor() as i64, (de[[k, 1]] / tg).floor() as i64)).or_default().push(k);
    }
    let deck_at = |p: (f64, f64)| {
        let (gx, gy) = ((p.0 / tg).floor() as i64, (p.1 / tg).floor() as i64);
        let mut best = 0.0f64;
        for dx in -1..=1 {
            for dy in -1..=1 {
                for &k in ends_grid.get(&(gx + dx, gy + dy)).map(|v| v.as_slice()).unwrap_or(&[]) {
                    if (de[[k, 0]] - p.0).hypot(de[[k, 1]] - p.1) < touch {
                        best = best.max(de[[k, 2]]);
                    }
                }
            }
        }
        best
    };
    let gap_end: Vec<(f64, f64)> = (0..n)
        .map(|l| {
            // (under_only: no gap rows, how 2, wanted)
            if under_only || under(kind[l]) || ln.st[l + 1] < ln.st[l] + 2 || ln.len(l) >= gap_max {
                (0.0, 0.0)
            } else {
                (deck_at(ln.end(l, false)), deck_at(ln.end(l, true)))
            }
        })
        .collect();
    let gap_line: Vec<bool> = gap_end.iter().map(|&(a, b)| a > 0.0 && b > 0.0).collect();
    let boxes: Vec<_> = (0..n).map(|l| ln.bbox(l)).collect();
    let mut rows: Vec<[f64; 6]> = Vec::new();
    let mut gap_done = vec![false; n];
    for l in 0..n {
        // shorter than a deck it joins: a ramp's or a flyover's stretch the map left untagged (a freeway between the
        // short bridges over its cross streets runs on the ground: longer than those)
        if gap_line[l] && ln.len(l) < gap_end[l].0.max(gap_end[l].1) * gap_share {
            gap_done[l] = true;
            rows.push([l as f64, l as f64, 0.0, 0.0, 1.0, 2.0]);
        }
    }
    // the lines whose boxes may overlap, by a grid of their boxes (every pair was tried: 0.13 s a city cell, ~7000 lines)
    const G: f64 = 128.0;
    let cell_of = |v: f64| (v / G).floor() as i64;
    // (under_only: the grid of the short tunnels alone, ugrid, for the other lines; a tunnel's own pairs by a look
    // through every line for those of its grid squares: the same lines in the same order as the whole grid's)
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut ugrid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let gridded = |l: usize| {
        let (x0, y0, x1, y1) = boxes[l];
        !(!(x0.is_finite() && y0.is_finite()) || (x1 - x0) / G > 64.0 || (y1 - y0) / G > 64.0)
    };
    for l in 0..n {
        if !gridded(l) {
            continue;   // (no points, or a line some km across: in the "wide" list)
        }
        if under_only && !under(kind[l]) {
            continue;
        }
        let (x0, y0, x1, y1) = boxes[l];
        let g = if under_only { &mut ugrid } else { &mut grid };
        for gx in cell_of(x0)..=cell_of(x1) {
            for gy in cell_of(y0)..=cell_of(y1) {
                g.entry((gx, gy)).or_default().push(l);
            }
        }
    }
    let wide: Vec<usize> = (0..n)
        .filter(|&l| {
            let (x0, y0, x1, y1) = boxes[l];
            x0.is_finite() && y0.is_finite() && ((x1 - x0) / G > 64.0 || (y1 - y0) / G > 64.0)
        })
        .collect();
    let uwide: Vec<usize> = wide.iter().copied().filter(|&l| under(kind[l])).collect();
    let mut stamp = vec![usize::MAX; n];
    for a in 0..n {
        let (ax0, ay0, ax1, ay1) = boxes[a];
        if ln.st[a + 1] < ln.st[a] + 2 {
            continue;
        }
        let mut near: Vec<usize> = Vec::new();
        if (ax1 - ax0) / G > 64.0 || (ay1 - ay0) / G > 64.0 {
            near.extend(a + 1..n);
        } else if under_only && !under(kind[a]) {
            // (only the short tunnels: their grid and the wide ones)
            for gx in cell_of(ax0)..=cell_of(ax1) {
                for gy in cell_of(ay0)..=cell_of(ay1) {
                    for &b in ugrid.get(&(gx, gy)).map(|v| v.as_slice()).unwrap_or(&[]) {
                        if b > a && stamp[b] != a {
                            stamp[b] = a;
                            near.push(b);
                        }
                    }
                }
            }
            for &b in &uwide {
                if b > a && stamp[b] != a {
                    stamp[b] = a;
                    near.push(b);
                }
            }
            near.sort_unstable();
        } else if under_only {
            // (a short tunnel: every gridded line sharing one of its grid squares, as the whole grid gives them)
            let (cx0, cx1, cy0, cy1) = (cell_of(ax0), cell_of(ax1), cell_of(ay0), cell_of(ay1));
            for b in a + 1..n {
                if gridded(b) {
                    let (bx0, by0, bx1, by1) = boxes[b];
                    let (dx0, dx1, dy0, dy1) = (cell_of(bx0), cell_of(bx1), cell_of(by0), cell_of(by1));
                    if dx0.max(cx0) <= dx1.min(cx1) && dy0.max(cy0) <= dy1.min(cy1) {
                        near.push(b);
                        continue;
                    }
                }
                if wide.binary_search(&b).is_ok() {
                    near.push(b);
                }
            }
        } else {
            for gx in cell_of(ax0)..=cell_of(ax1) {
                for gy in cell_of(ay0)..=cell_of(ay1) {
                    for &b in grid.get(&(gx, gy)).map(|v| v.as_slice()).unwrap_or(&[]) {
                        if b > a && stamp[b] != a {
                            stamp[b] = a;
                            near.push(b);
                        }
                    }
                }
            }
            for &b in &wide {
                if b > a && stamp[b] != a {
                    stamp[b] = a;
                    near.push(b);
                }
            }
            near.sort_unstable();       // (the pairs in the order the full loop took them)
        }
        for b in near {
            if ln.st[b + 1] < ln.st[b] + 2 || group[a] == group[b] || (under(kind[a]) && under(kind[b])) {
                continue;
            }
            if under_only && !under(kind[a]) && !under(kind[b]) {
                continue;   // (how 0 / 2 rows only: none wanted; gap_done is theirs alone)
            }
            // over a short tunnel: only a road (a railway's own pieces, or one railway by another, are no overpass)
            if (kind[a] == UNDER && kind[b] != ROAD) || (kind[b] == UNDER && kind[a] != ROAD) {
                continue;
            }
            let mut found: Vec<(f64, f64)> = Vec::new();
            let (bx0, by0, bx1, by1) = boxes[b];
            if bx1 < ax0 || bx0 > ax1 || by1 < ay0 || by0 > ay1 {
                continue;
            }
            for k in ln.st[a]..ln.st[a + 1] - 1 {
                let (px, py_) = (xy[[k, 0]], xy[[k, 1]]);
                let (rx, ry) = (xy[[k + 1, 0]] - px, xy[[k + 1, 1]] - py_);
                for m in ln.st[b]..ln.st[b + 1] - 1 {
                    let (qx, qy) = (xy[[m, 0]], xy[[m, 1]]);
                    let (sx, sy) = (xy[[m + 1, 0]] - qx, xy[[m + 1, 1]] - qy);
                    let den = rx * sy - ry * sx;
                    if !(den.abs() > 1e-12) {
                        continue;
                    }
                    let t = ((qx - px) * sy - (qy - py_) * sx) / den;
                    let u = ((qx - px) * ry - (qy - py_) * rx) / den;
                    if !(0.0..=1.0).contains(&t) || !(0.0..=1.0).contains(&u) {
                        continue;
                    }
                    let p = (px + t * rx, py_ + t * ry);
                    if found.iter().any(|q| (q.0 - p.0).hypot(q.1 - p.1) < 1.0) {
                        continue;
                    }
                    found.push(p);
                    let sa = ln.arc[k] + t * (ln.arc[k + 1] - ln.arc[k]);
                    let sb = ln.arc[m] + u * (ln.arc[m + 1] - ln.arc[m]);
                    let (ra, rb) = (rx.hypot(ry), sx.hypot(sy));
                    let sin = (den / (ra * rb).max(1e-12)).abs();
                    let (up, lo, su, sl, how) = if under(kind[b]) {
                        (a, b, sa, sb, 1.0)
                    } else if under(kind[a]) {
                        (b, a, sb, sa, 1.0)
                    } else {
                        let junction = [ln.end(a, false), ln.end(a, true), ln.end(b, false), ln.end(b, true)]
                            .iter()
                            .any(|e| (e.0 - p.0).hypot(e.1 - p.1) < end_m);
                        if junction {
                            continue;
                        }
                        if gap_line[a] || gap_line[b] {
                            for g in [a, b] {
                                if gap_line[g] && !gap_done[g] {
                                    gap_done[g] = true;
                                    rows.push([g as f64, if g == a { b } else { a } as f64, 0.0, 0.0, sin, 2.0]);
                                }
                            }
                            continue;
                        }
                        (a, b, sa, sb, 0.0)
                    };
                    rows.push([up as f64, lo as f64, su, sl, sin, how]);
                }
            }
        }
    }
    let mut cand = Array2::<f64>::zeros((rows.len(), 6));
    let mut smp = Array2::<f64>::from_elem((rows.len() * 8, 2), f64::NAN);
    for (r, row) in rows.iter().enumerate() {
        for c in 0..6 {
            cand[[r, c]] = row[c];
        }
        if row[5] != 0.0 {
            continue;
        }
        let (i, j) = (row[0] as usize, row[1] as usize);
        let s = row[4].max(min_sin);
        for (o, (l, at, other)) in [(i, row[2], j), (j, row[3], i)].into_iter().enumerate() {
            let d1 = half[other] / s + gap;
            for (q, d) in [-(d1 + step), -d1, d1, d1 + step].into_iter().enumerate() {
                if let Some((x, y)) = ln.at_on(l, at + d, &kind, touch, turn) {
                    smp[[r * 8 + o * 4 + q, 0]] = x;
                    smp[[r * 8 + o * 4 + q, 1]] = y;
                }
            }
        }
    }
    (cand.into_pyarray(py), smp.into_pyarray(py))
}

fn mean(v: &[f64]) -> Option<f64> {
    let g: Vec<f64> = v.iter().copied().filter(|x| x.is_finite()).collect();
    if g.is_empty() { None } else { Some(g.iter().sum::<f64>() / g.len() as f64) }
}

fn median(v: &[f64]) -> Option<f64> {
    let mut g: Vec<f64> = v.iter().copied().filter(|x| x.is_finite()).collect();
    if g.is_empty() {
        return None;
    }
    g.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = g.len();
    Some(if n % 2 == 1 { g[n / 2] } else { 0.5 * (g[n / 2 - 1] + g[n / 2]) })
}

/// The bridge spans (rows line, from m, to m, the crossing's how) of gradesep_candidates' crossings with the ground heights z (8 per
/// row, NaN unknown): how 0 a bridge on the line whose ground is higher by prm[0] m (medians) and by prm[1] m on
/// each side of each line; how 1 on i; then the other's half width + prm[2] m either side of the crossing over the
/// sine (at least prm[4]), at least prm[3] m; how 2 the whole line. A span past a line's end goes on along the line
/// joined there (an end within prm[5] m, the straightest of the same kind turning less than acos prm[6]).
#[pyfunction]
pub fn gradesep_spans<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    starts: PyReadonlyArray1<'py, i64>,
    kind: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
    cand: PyReadonlyArray2<'py, f64>,
    z: PyReadonlyArray2<'py, f64>,
    prm: PyReadonlyArray1<'py, f64>,
) -> Bound<'py, PyArray2<f64>> {
    let (xy, starts, kind, half, cand, z, prm) =
        (xy.as_array(), starts.as_array(), kind.as_array(), half.as_array(), cand.as_array(), z.as_array(), prm.as_array());
    let (sep, side, clear, min_half, min_sin, tol, turn) = (prm[0], prm[1], prm[2], prm[3], prm[4], prm[5], prm[6]);
    let ln = Lines::new(xy, starts);
    let n = ln.n();
    let mut spans: Vec<(usize, f64, f64, f64)> = Vec::new();
    for r in 0..cand.nrows() {
        let (i, j, si, sj, sin, how) =
            (cand[[r, 0]] as usize, cand[[r, 1]] as usize, cand[[r, 2]], cand[[r, 3]], cand[[r, 4]], cand[[r, 5]]);
        if how == 2.0 {
            spans.push((i, 0.0, ln.len(i), 2.0));
            continue;
        }
        let (up, lo, s) = if how == 1.0 {
            (i, j, si)
        } else {
            let zr: Vec<f64> = (0..8).map(|q| z[[r, q]]).collect();
            let (zi, zj) = (&zr[0..4], &zr[4..8]);
            let (mi, mj) = match (median(zi), median(zj)) {
                (Some(a), Some(b)) => (a, b),
                _ => continue,
            };
            let (zu, zl, mu, ml, up, lo, s) =
                if mi >= mj { (zi, zj, mi, mj, i, j, si) } else { (zj, zi, mj, mi, j, i, sj) };
            if mu - ml < sep {
                continue;
            }
            let sides = [mean(&zu[0..2]), mean(&zu[2..4]), mean(&zl[0..2]), mean(&zl[2..4])];
            if sides.iter().any(|v| v.is_none()) {
                continue;
            }
            let sd: Vec<f64> = sides.iter().map(|v| v.unwrap()).collect();
            if sd[0] - ml < side || sd[1] - ml < side || mu - sd[2] < side || mu - sd[3] < side {
                continue;
            }
            (up, lo, s)
        };
        if under(kind[up]) {
            continue;
        }
        let h = ((half[lo] + clear) / sin.max(min_sin)).max(min_half);
        spans.push((up, s - h, s + h, how));
    }
    // past a line's end: on along the straightest line joined there (one step)
    let mut more = Vec::new();
    for &(l, a, b, how) in &spans {
        for (last, over) in [(false, -a), (true, b - ln.len(l))] {
            if over <= 0.0 {
                continue;
            }
            let best = ln.next(l, last, &kind, tol, turn);
            if let Some((o, oend)) = best {
                let lo_ = ln.len(o);
                more.push(if oend { (o, lo_ - over, lo_, how) } else { (o, 0.0, over, how) });
            }
        }
    }
    spans.extend(more);
    let mut out = Array2::<f64>::zeros((spans.len(), 4));
    for (r, &(l, a, b, how)) in spans.iter().enumerate() {
        out[[r, 0]] = l as f64;
        out[[r, 1]] = a.max(0.0);
        out[[r, 2]] = b.min(ln.len(l));
        out[[r, 3]] = how;
    }
    out.into_pyarray(py)
}

/// numpy's interp for one x over increasing xp (binary search for the last xp[j] <= x; x at xp[j] exactly: fp[j]; the
/// slope form, tried from the other end when it is NaN; below xp[0]: fp[0], above or at the last: fp[last]).
fn np_interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if n == 0 {
        return f64::NAN;
    }
    if x.is_nan() {
        return x;
    }
    if !(x >= xp[0]) {
        return fp[0];
    }
    if !(x < xp[n - 1]) {
        return fp[n - 1];
    }
    // (the last j with xp[j] <= x, x < xp[n - 1]: j < n - 1)
    let (mut lo, mut hi) = (0usize, n - 1);
    while hi - lo > 1 {
        let mid = (lo + hi) / 2;
        if xp[mid] <= x {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let j = lo;
    if x == xp[j] {
        return fp[j];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if r.is_nan() && fp[j] == fp[j + 1] {
            r = fp[j];
        }
    }
    r
}

/// gradesep._cut: the part of polyline xy between fractions a < b of its length (xy[:1] when it has none).
fn cut(xy: &[[f64; 2]], a: f64, b: f64) -> Vec<[f64; 2]> {
    let mut s = Vec::with_capacity(xy.len());
    s.push(0.0f64);
    for k in 1..xy.len() {
        let d = (xy[k][0] - xy[k - 1][0]).hypot(xy[k][1] - xy[k - 1][1]);
        let last = *s.last().unwrap();
        s.push(last + d);
    }
    let total = *s.last().unwrap();
    if !(total > 0.0) {
        return vec![xy[0]];
    }
    let (sa, sb) = (a * total, b * total);
    let xs: Vec<f64> = xy.iter().map(|p| p[0]).collect();
    let ys: Vec<f64> = xy.iter().map(|p| p[1]).collect();
    let mut out = vec![[np_interp(sa, &s, &xs), np_interp(sa, &s, &ys)]];
    for k in 0..xy.len() {
        if s[k] > sa && s[k] < sb {
            out.push(xy[k]);
        }
    }
    out.push([np_interp(sb, &s, &xs), np_interp(sb, &s, &ys)]);
    out
}

/// One layer's arrays as gradesep_gather / gradesep_gather_rows read them.
struct Layer<'a> {
    xy: numpy::ndarray::ArrayView2<'a, f64>,
    co: numpy::ndarray::ArrayView1<'a, i64>,
    ro: numpy::ndarray::ArrayView1<'a, i64>,
    po: numpy::ndarray::ArrayView1<'a, i64>,
    poff: numpy::ndarray::ArrayView1<'a, i64>,
    pk: numpy::ndarray::ArrayView1<'a, i64>,
    pa: numpy::ndarray::ArrayView1<'a, f64>,
    pb: numpy::ndarray::ArrayView1<'a, f64>,
}

/// The lines and deck ends gathered so far (gather_row).
#[derive(Default)]
struct Gathered {
    pts: Vec<f64>,
    starts: Vec<i64>,
    line_row: Vec<i64>,
    line_piece: Vec<i64>,
    de: Vec<f64>,
    deck_row: Vec<i64>,
}

/// Feature i of lay cut into its pieces (its parts' rings of 2 or more points, each cut at each piece's fractions):
/// the ground pieces appended to g's lines (row r, piece index), the decks' end points and
/// lengths to g.de. gradesep_gather's loop, the same in gradesep_gather_rows.
fn gather_row(lay: &Layer, i: usize, r: i64, g: &mut Gathered) {
    let (p0, p1) = (lay.poff[i] as usize, lay.poff[i + 1] as usize);
    if p1 <= p0 {
        return;
    }
    for p in lay.po[i] as usize..lay.po[i + 1] as usize {
        for gr in lay.ro[p] as usize..lay.ro[p + 1] as usize {
            let (a, b) = (lay.co[gr] as usize, lay.co[gr + 1] as usize);
            if b - a < 2 {
                continue;
            }
            let ring: Vec<[f64; 2]> = (a..b).map(|k| [lay.xy[[k, 0]], lay.xy[[k, 1]]]).collect();
            for q in p0..p1 {
                let piece = if lay.pa[q] == 0.0 && lay.pb[q] == 1.0 { ring.clone() } else { cut(&ring, lay.pa[q], lay.pb[q]) };
                if piece.len() < 2 {
                    continue;
                }
                if lay.pk[q] < 0 {
                    let seg: Vec<f64> = (1..piece.len())
                        .map(|k| (piece[k][0] - piece[k - 1][0]).hypot(piece[k][1] - piece[k - 1][1]))
                        .collect();
                    let l = crate::levels::np_sum(&seg);
                    let (f, e) = (piece[0], piece[piece.len() - 1]);
                    g.de.extend_from_slice(&[f[0], f[1], l, e[0], e[1], l]);
                    g.deck_row.push(r);
                    continue;
                }
                for v in &piece {
                    g.pts.push(v[0]);
                    g.pts.push(v[1]);
                }
                g.starts.push((g.pts.len() / 2) as i64);
                g.line_row.push(r);
                g.line_piece.push(q as i64);
            }
        }
    }
}

/// gradesep._spans' lines (2026-11-02: 1.9 of its 3.6 s on 6 Midtown cells was this loop in Python, each layer walked
/// again for each of the nine cells round it). rows (r,): the layer's features in order; for each, its parts' rings
/// (offsets part_off / ring_off / coord_off into xy, the layer in the asking cell's frame) cut into its pieces
/// (piece_off[i]..piece_off[i + 1] of pk (a line's kind, -1 for a deck), pa, pb). Returns (line points (m, 2), their
/// starts (l + 1,), each line's row index and piece index, deck ends and lengths (2d, 3), each deck's row index).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn gradesep_gather<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, coord_off: PyReadonlyArray1<'py, i64>,
                            ring_off: PyReadonlyArray1<'py, i64>, part_off: PyReadonlyArray1<'py, i64>,
                            rows: PyReadonlyArray1<'py, i64>, piece_off: PyReadonlyArray1<'py, i64>,
                            pk: PyReadonlyArray1<'py, i64>, pa: PyReadonlyArray1<'py, f64>,
                            pb: PyReadonlyArray1<'py, f64>)
                            -> (Bound<'py, PyArray2<f64>>, Bound<'py, numpy::PyArray1<i64>>,
                                Bound<'py, numpy::PyArray1<i64>>, Bound<'py, numpy::PyArray1<i64>>,
                                Bound<'py, PyArray2<f64>>, Bound<'py, numpy::PyArray1<i64>>) {
    let (xy, co, ro, po) = (xy.as_array(), coord_off.as_array(), ring_off.as_array(), part_off.as_array());
    let (rows, poff, pk, pa, pb) = (rows.as_array(), piece_off.as_array(), pk.as_array(), pa.as_array(), pb.as_array());
    let lay = Layer { xy, co, ro, po, poff, pk, pa, pb };
    let mut g = Gathered::default();
    g.starts.push(0);
    for (r, &i) in rows.iter().enumerate() {
        gather_row(&lay, i as usize, r as i64, &mut g);
    }
    let Gathered { pts, starts, line_row, line_piece, de, deck_row } = g;
    let m = pts.len() / 2;
    let d = deck_row.len();
    (Array2::from_shape_vec((m, 2), pts).expect("points").into_pyarray(py), starts.into_pyarray(py),
     line_row.into_pyarray(py), line_piece.into_pyarray(py),
     Array2::from_shape_vec((2 * d, 3), de).expect("decks").into_pyarray(py), deck_row.into_pyarray(py))
}

/// One layer for gradesep_select: (xy (N, 2) in the asking cell's frame, coord_off, ring_off, part_off).
type SelectLayer<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                         PyReadonlyArray1<'py, i64>);

/// np.minimum / np.maximum's NaN: either NaN gives NaN.
fn nan_min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a }
}

fn nan_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}

/// Each feature's [min e, min n, max e, max n] of all its coordinates (mapgeom.Features.boxes: NaN where one of them
/// is NaN, +inf / -inf without any), from coord_off[ring_off[part_off[i]]] to the next feature's.
fn feature_boxes(xy: &numpy::ndarray::ArrayView2<f64>, co: &numpy::ndarray::ArrayView1<i64>,
                 ro: &numpy::ndarray::ArrayView1<i64>, po: &numpy::ndarray::ArrayView1<i64>) -> Vec<[f64; 4]> {
    let n = po.len().saturating_sub(1);
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let (c0, c1) = (co[ro[po[i] as usize] as usize] as usize, co[ro[po[i + 1] as usize] as usize] as usize);
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        if c1 > c0 {
            b = [xy[[c0, 0]], xy[[c0, 1]], xy[[c0, 0]], xy[[c0, 1]]];
            for k in c0 + 1..c1 {
                let (x, y) = (xy[[k, 0]], xy[[k, 1]]);
                b = [nan_min(b[0], x), nan_min(b[1], y), nan_max(b[2], x), nan_max(b[3], y)];
            }
        }
        out.push(b);
    }
    out
}

/// gradesep._spans' rows before their ids: layers (SelectLayer, the first the asking cell's own). The box round the
/// first layer's features whose boxes are all finite, grown by near_m; per layer in order, the features whose boxes
/// meet it (a NaN box meets it, as numpy's test has it). (layer offsets (L + 1,) into rows, rows (r,) ascending per
/// layer); None when no box of the first layer is finite. (2026-11-08: Features.boxes and the Python loop over the
/// rows were ~1/4 of _spans on a Midtown tile.)
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn gradesep_select<'py>(py: Python<'py>, layers: Vec<SelectLayer<'py>>, near_m: f64)
                            -> Option<(Bound<'py, numpy::PyArray1<i64>>, Bound<'py, numpy::PyArray1<i64>>)> {
    let boxes: Vec<Vec<[f64; 4]>> = layers.iter()
        .map(|(xy, co, ro, po)| feature_boxes(&xy.as_array(), &co.as_array(), &ro.as_array(), &po.as_array()))
        .collect();
    let own = boxes.first()?;
    let mut bx: Option<[f64; 4]> = None;
    for b in own.iter().filter(|b| b.iter().all(|v| v.is_finite())) {
        bx = Some(match bx {
            None => *b,
            Some(c) => [c[0].min(b[0]), c[1].min(b[1]), c[2].max(b[2]), c[3].max(b[3])],
        });
    }
    let c = bx?;
    let bx = [c[0] - near_m, c[1] - near_m, c[2] + near_m, c[3] + near_m];
    let mut off = vec![0i64];
    let mut rows: Vec<i64> = Vec::new();
    for lb in &boxes {
        for (i, b) in lb.iter().enumerate() {
            if !((b[2] < bx[0]) || (b[0] > bx[2]) || (b[3] < bx[1]) || (b[1] > bx[3])) {
                rows.push(i as i64);
            }
        }
        off.push(rows.len() as i64);
    }
    Some((off.into_pyarray(py), rows.into_pyarray(py)))
}

/// One layer for gradesep_gather_rows: (xy (N, 2) in the asking cell's frame, coord_off, ring_off, part_off,
/// piece_off, pk, pa, pb as gradesep_gather's, halves (n,) each feature's half width).
type GatherLayer<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                         PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                         PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>);

/// gradesep_gather over several layers without joining them: rows r of (row_layer[r], row[r]). Returns (line points
/// (m, 2), their starts (l + 1,), each line's row index, kind, from and to fractions, half width, deck ends and
/// lengths (2d, 3)): gradesep_gather's numbers and its caller's lookups of them.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn gradesep_gather_rows<'py>(py: Python<'py>, layers: Vec<GatherLayer<'py>>, row_layer: PyReadonlyArray1<'py, i64>,
                                 row: PyReadonlyArray1<'py, i64>)
                                 -> (Bound<'py, PyArray2<f64>>, Bound<'py, numpy::PyArray1<i64>>,
                                     Bound<'py, numpy::PyArray1<i64>>, Bound<'py, numpy::PyArray1<i64>>,
                                     Bound<'py, numpy::PyArray1<f64>>, Bound<'py, numpy::PyArray1<f64>>,
                                     Bound<'py, numpy::PyArray1<f64>>, Bound<'py, PyArray2<f64>>) {
    let lays: Vec<(Layer, numpy::ndarray::ArrayView1<f64>)> = layers.iter()
        .map(|(xy, co, ro, po, poff, pk, pa, pb, h)| (Layer {
            xy: xy.as_array(), co: co.as_array(), ro: ro.as_array(), po: po.as_array(), poff: poff.as_array(),
            pk: pk.as_array(), pa: pa.as_array(), pb: pb.as_array() }, h.as_array()))
        .collect();
    let (rl, ri) = (row_layer.as_array(), row.as_array());
    let mut g = Gathered::default();
    g.starts.push(0);
    let (mut kind, mut fa, mut fb, mut half) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for r in 0..rl.len() {
        let (lay, h) = &lays[rl[r] as usize];
        let i = ri[r] as usize;
        let before = g.line_piece.len();
        gather_row(lay, i, r as i64, &mut g);
        for &q in &g.line_piece[before..] {
            let q = q as usize;
            kind.push(lay.pk[q]);
            fa.push(lay.pa[q]);
            fb.push(lay.pb[q]);
            half.push(h[i]);
        }
    }
    let Gathered { pts, starts, line_row, de, .. } = g;
    let m = pts.len() / 2;
    let d = de.len() / 6;
    (Array2::from_shape_vec((m, 2), pts).expect("points").into_pyarray(py), starts.into_pyarray(py),
     line_row.into_pyarray(py), kind.into_pyarray(py), fa.into_pyarray(py), fb.into_pyarray(py),
     half.into_pyarray(py), Array2::from_shape_vec((2 * d, 3), de).expect("decks").into_pyarray(py))
}
