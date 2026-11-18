//! The rail parts over short road tunnels (the full port's railways._underpass_masks / _underpass_distance /
//! _span_underpasses / _clip, 487). Every node window of railprofile's lattice (~800 a Manhattan visit's 25 road tiles)
//! measured its parts against every tunnel near it and cut them in numpy: the parts' boxes in a Python loop, a mask
//! per part per tunnel, a run graph per tunnel, then the ground parts cut to the window, ~3 of the rail stack's ~18
//! CPU-s. The same products, sums and comparisons per point as the numpy version (elementwise: bit for bit):
//! np.gradient's central differences, np.clip / np.maximum's NaN, np.argmin's first of equals, Python's max(s, 1e-6).
use crate::chaingraph::EndJoin;
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

type Pt = [f64; 2];

/// railways.UNDERPASS_MIN_SIN: a track this square or more to the tunnel crosses it.
const MIN_SIN: f64 = 0.25;

/// np.maximum: a NaN either side wins.
fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}

/// np.clip(x, 0, 1) as numpy 2's clip loop gives it: a NaN stays, -0.0 stays (only what is below 0 or over 1 moves).
fn clip01(x: f64) -> f64 {
    x.clamp(0.0, 1.0)
}

/// The column minima and maxima of points (x0, y0, x1, y1), numpy's min / max: a NaN in a column is that column's.
fn bbox(p: &[Pt]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    let mut nan = [false; 2];
    for q in p {
        for c in 0..2 {
            if q[c].is_nan() {
                nan[c] = true;
            } else {
                if q[c] < b[c] { b[c] = q[c]; }
                if q[c] > b[c + 2] { b[c + 2] = q[c]; }
            }
        }
    }
    for c in 0..2 {
        if nan[c] {
            b[c] = f64::NAN;
            b[c + 2] = f64::NAN;
        }
    }
    b
}

/// railways._box_hit
fn box_hit(b: &[f64; 4], n: &[f64; 4]) -> bool {
    !(b[2] < n[0] || b[0] > n[2] || b[3] < n[1] || b[1] > n[3])
}

/// A tunnel's centreline as _underpass_distance reads it: its segments of length >= 1e-6 (start, step, length, the arc
/// at their start), and max(arc length, 1e-6).
struct Tunnel {
    pts: Vec<Pt>,
    half: f64,
    seg: Vec<(Pt, Pt, f64, f64)>,
    total: f64,
}

impl Tunnel {
    fn new(pts: Vec<Pt>, half: f64) -> Tunnel {
        let mut s = vec![0.0f64; pts.len()];
        for k in 1..pts.len() {
            s[k] = s[k - 1] + (pts[k][0] - pts[k - 1][0]).dhypot(pts[k][1] - pts[k - 1][1]);
        }
        let mut seg = Vec::new();
        for k in 0..pts.len().saturating_sub(1) {
            let d = [pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]];
            let len = d[0].dhypot(d[1]);
            if len >= 1e-6 {
                seg.push((pts[k], d, len, s[k]));
            }
        }
        let last = s.last().copied().unwrap_or(0.0);
        let total = if 1e-6 > last { 1e-6 } else { last };
        Tunnel { pts, half, seg, total }
    }

    /// railways._underpass_distance of one point: (distance, fraction along); (inf, 0) where no segment is nearer than
    /// infinity (none, or NaNs: dd NaN is inf there).
    fn distance(&self, p: Pt) -> (f64, f64) {
        let mut best = f64::INFINITY;
        let mut got: Option<(usize, f64)> = None;
        for (j, &(a, d, len, _)) in self.seg.iter().enumerate() {
            let (px, py) = (p[0] - a[0], p[1] - a[1]);
            let t = clip01((px * d[0] + py * d[1]) / (len * len));
            let ex = p[0] - (a[0] + t * d[0]);
            let ey = p[1] - (a[1] + t * d[1]);
            let mut dd = (ex * ex + ey * ey).sqrt();
            if dd.is_nan() {
                dd = f64::INFINITY;
            }
            if dd < best {
                best = dd;
                got = Some((j, t));
            }
        }
        match got {
            Some((j, t)) => {
                let (_, _, len, s0) = self.seg[j];
                (best, (s0 + t * len) / self.total)
            }
            None => (f64::INFINITY, 0.0),
        }
    }
}

/// Which runs (index ranges into pts) cross polyline g: roads._crossings(runs, [g]) through the kernel it called
/// (segment_crossings_core: its boxes' rejection, then den beyond 1e-12 and both fractions within [0, 1]).
fn crossing_runs(pts: &[Pt], runs: &[(usize, usize)], g: &[Pt]) -> Vec<bool> {
    use numpy::ndarray::{ArrayView1, ArrayView2};
    let mut flat: Vec<f64> = Vec::new();
    let mut off: Vec<i64> = vec![0];
    for &(a, b) in runs {
        flat.extend(pts[a..=b].iter().flat_map(|q| [q[0], q[1]]));
        off.push((flat.len() / 2) as i64);
    }
    let gflat: Vec<f64> = g.iter().flat_map(|q| [q[0], q[1]]).collect();
    let goff = [0i64, g.len() as i64];
    let pa = ArrayView2::from_shape((flat.len() / 2, 2), &flat).expect("runs (n, 2)");
    let ga = ArrayView2::from_shape((g.len(), 2), &gflat).expect("line (k, 2)");
    let mut out = vec![false; runs.len()];
    let got = crate::segment_crossings_core(pa, ArrayView1::from(&off[..]), ga, ArrayView1::from(&goff[..]));
    for (pi, _g, _s, _x, _y) in got {
        out[pi] = true;
    }
    out
}

/// The parts end to end (Rust-owned: the numpy borrows are let go before the GIL is).
struct Parts {
    pts: Vec<Pt>,
    off: Vec<usize>,
}

impl Parts {
    fn of(lines: &[PyReadonlyArray2<'_, f64>], what: &str) -> PyResult<Parts> {
        let mut pts = Vec::new();
        let mut off = vec![0usize];
        for l in lines {
            let a = l.as_array();
            if a.ncols() != 2 {
                return Err(pyo3::exceptions::PyValueError::new_err(format!("{what}: lines (k, 2)")));
            }
            pts.extend(a.rows().into_iter().map(|r| [r[0], r[1]]));
            off.push(pts.len());
        }
        Ok(Parts { pts, off })
    }
    fn n(&self) -> usize {
        self.off.len() - 1
    }
    fn xy(&self, p: usize) -> &[Pt] {
        &self.pts[self.off[p]..self.off[p + 1]]
    }
}

enum Fail {
    Value(&'static str),
    Index(&'static str),
}

impl From<Fail> for PyErr {
    fn from(f: Fail) -> PyErr {
        match f {
            Fail::Value(m) => pyo3::exceptions::PyValueError::new_err(m),
            Fail::Index(m) => pyo3::exceptions::PyIndexError::new_err(m),
        }
    }
}

/// The parts' boxes (railways._rail_boxes: an empty part has none, numpy's zero-size reduction).
fn boxes(parts: &Parts) -> Result<Vec<[f64; 4]>, Fail> {
    (0..parts.n()).map(|p| {
        let xy = parts.xy(p);
        if xy.is_empty() { Err(Fail::Value("rail underpasses: a part of no points")) } else { Ok(bbox(xy)) }
    }).collect()
}

/// railways._underpass_masks of one tunnel: per part with any, its masked points (ascending) and their fractions along
/// the tunnel (_underpass_distance's, at those points).
#[allow(clippy::type_complexity)]
fn masks(parts: &Parts, bx: &[[f64; 4]], tun: &Tunnel, margin: f64) -> Result<Vec<(usize, Vec<usize>, Vec<f64>)>, Fail> {
    let line = &tun.pts;
    if line.is_empty() {
        return Err(Fail::Value("rail underpasses: a tunnel of no points"));
    }
    let pad = tun.half + margin;
    let lb = bbox(line);
    let near = [lb[0] - pad, lb[1] - pad, lb[2] + pad, lb[3] + pad];
    let dir = [line[line.len() - 1][0] - line[0][0], line[line.len() - 1][1] - line[0][1]];
    let dnorm = (dir[0] * dir[0] + dir[1] * dir[1]).sqrt();
    // (per part: its covered flags and fractions, and its runs (a, b): points a..=b)
    let mut cand: Vec<(usize, Vec<bool>, Vec<f64>)> = Vec::new();
    let mut runs: Vec<(usize, usize, usize)> = Vec::new();      // (index in cand, a, b)
    for (p, b) in bx.iter().enumerate() {
        if !box_hit(b, &near) {
            continue;
        }
        let xy = parts.xy(p);
        let k = xy.len();
        let mut cov = vec![false; k];
        let mut along = vec![0.0f64; k];
        let mut any = false;
        for i in 0..k {
            let q = xy[i];
            if !(q[0] >= near[0] && q[0] <= near[2] && q[1] >= near[1] && q[1] <= near[3]) {
                continue;
            }
            if k < 2 {
                return Err(Fail::Value("rail underpasses: a part of one point (np.gradient)"));
            }
            // np.gradient (unit spacing, first-order ends)
            let t = if i == 0 {
                [xy[1][0] - xy[0][0], xy[1][1] - xy[0][1]]
            } else if i == k - 1 {
                [xy[k - 1][0] - xy[k - 2][0], xy[k - 1][1] - xy[k - 2][1]]
            } else {
                [(xy[i + 1][0] - xy[i - 1][0]) / 2.0, (xy[i + 1][1] - xy[i - 1][1]) / 2.0]
            };
            let cross = (t[0] * dir[1] - t[1] * dir[0]).abs()
                / nmax((t[0] * t[0] + t[1] * t[1]).sqrt() * dnorm, 1e-9);
            let (dist, frac) = tun.distance(q);
            if dist <= pad && cross > MIN_SIN {
                cov[i] = true;
                along[i] = frac;
                any = true;
            }
        }
        if !any {
            continue;       // (no run: its mask stays zeros)
        }
        let c = cand.len();
        // spans = cov[:-1] | cov[1:]; each run of true spans [a, b) the points a..=b
        let mut i = 0;
        while i + 1 < k {
            if cov[i] || cov[i + 1] {
                let a = i;
                while i + 1 < k && (cov[i] || cov[i + 1]) {
                    i += 1;
                }
                runs.push((c, a, i));
            } else {
                i += 1;
            }
        }
        cand.push((p, cov, along));
    }
    if runs.is_empty() {
        return Ok(Vec::new());
    }
    let spans: Vec<(usize, usize)> = runs.iter().map(|&(c, a, b)| (parts.off[cand[c].0] + a, parts.off[cand[c].0] + b))
        .collect();
    let crossing = crossing_runs(&parts.pts, &spans, line);
    if !crossing.iter().any(|&x| x) {
        return Ok(Vec::new());
    }
    // roads._chain_graph of the runs (ends within 0.5 m joined), and the nodes connected to a crossing run's
    let mut j = EndJoin::new(0.5);
    let mut ends = Vec::with_capacity(runs.len());
    for &(c, a, b) in &runs {
        let xy = parts.xy(cand[c].0);
        let s = j.join(xy[a][0], xy[a][1]).0;
        let e = j.join(xy[b][0], xy[b][1]).0;
        ends.push((s, e));
    }
    let nn = ends.iter().map(|&(s, e)| s.max(e) + 1).max().unwrap_or(0);
    let mut parent: Vec<usize> = (0..nn).collect();
    fn find(parent: &mut [usize], mut a: usize) -> usize {
        while parent[a] != a {
            parent[a] = parent[parent[a]];
            a = parent[a];
        }
        a
    }
    for &(s, e) in &ends {
        let (rs, re) = (find(&mut parent, s), find(&mut parent, e));
        if rs != re {
            parent[rs] = re;
        }
    }
    let mut live = vec![false; nn];
    for (r, &(s, _)) in ends.iter().enumerate() {
        if crossing[r] {
            let root = find(&mut parent, s);
            live[root] = true;
        }
    }
    let mut out: Vec<(usize, Vec<usize>, Vec<f64>)> = Vec::new();
    for (r, &(c, a, b)) in runs.iter().enumerate() {
        if !live[find(&mut parent, ends[r].0)] {
            continue;
        }
        let (p, cov, along) = (&cand[c].0, &cand[c].1, &cand[c].2);
        if out.last().is_none_or(|o| o.0 != *p) {
            out.push((*p, Vec::new(), Vec::new()));
        }
        let o = out.last_mut().expect("pushed");
        for i in a..=b {
            if cov[i] {
                o.1.push(i);
                o.2.push(along[i]);
            }
        }
    }
    out.retain(|o| !o.1.is_empty());
    Ok(out)
}

fn tunnels_of(lines: &[PyReadonlyArray2<'_, f64>], halves: &[f64]) -> PyResult<Vec<Tunnel>> {
    if lines.len() != halves.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("rail underpasses: one half width per tunnel"));
    }
    let tp = Parts::of(lines, "rail underpasses' tunnels")?;
    Ok((0..tp.n()).map(|t| Tunnel::new(tp.xy(t).to_vec(), halves[t])).collect())
}

/// railways._underpass_masks of each tunnel (lines (k, 2), halves its half widths) over the parts (k, 2), margin m past
/// the half width: (tunnel, part, point, fraction along the tunnel) of every masked point, by tunnel, part and point.
#[pyfunction]
#[pyo3(signature = (parts, lines, halves, margin))]
#[allow(clippy::type_complexity)]
pub fn rail_underpass_hits<'py>(py: Python<'py>, parts: Vec<PyReadonlyArray2<'py, f64>>,
                                lines: Vec<PyReadonlyArray2<'py, f64>>, halves: Vec<f64>, margin: f64)
                                -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                             Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>)> {
    let pp = Parts::of(&parts, "rail_underpass_hits")?;
    let tun = tunnels_of(&lines, &halves)?;
    drop(parts);
    drop(lines);
    let got: Result<_, Fail> = py.detach(|| {
        let (mut ti, mut pi, mut ki, mut fr) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
        if tun.is_empty() {
            return Ok((ti, pi, ki, fr));
        }
        let bx = boxes(&pp)?;
        for (t, tn) in tun.iter().enumerate() {
            for (p, idx, along) in masks(&pp, &bx, tn, margin)? {
                for (i, f) in idx.into_iter().zip(along) {
                    ti.push(t as i64);
                    pi.push(p as i64);
                    ki.push(i as i64);
                    fr.push(f);
                }
            }
        }
        Ok((ti, pi, ki, fr))
    });
    let (ti, pi, ki, fr) = got?;
    Ok((ti.into_pyarray(py), pi.into_pyarray(py), ki.into_pyarray(py), fr.into_pyarray(py)))
}

/// railways._underpass_distance: (distance, fraction along) of each point xy (k, 2) to the tunnel centreline line.
#[pyfunction]
#[pyo3(signature = (xy, line))]
#[allow(clippy::type_complexity)]
pub fn rail_underpass_distance<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, line: PyReadonlyArray2<'py, f64>)
                                    -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let pp = Parts::of(std::slice::from_ref(&xy), "rail_underpass_distance")?;
    let tun = tunnels_of(std::slice::from_ref(&line), &[0.0])?.pop().expect("one tunnel");
    drop(xy);
    drop(line);
    let (d, f): (Vec<f64>, Vec<f64>) = py.detach(|| pp.pts.iter().map(|&q| tun.distance(q)).unzip());
    Ok((d.into_pyarray(py), f.into_pyarray(py)))
}

/// railways._span_underpasses then (near given) railways._clip: the parts (k, 2) with each ground part's
/// (ground flags) points over a tunnel (lines, halves, margin m) made "bridge" pieces, then each ground piece cut to its
/// runs of points inside near (x0, y0, x1, y1), one point more each side. The pieces in order as (part, first point,
/// last point, made a bridge); no tunnels: the parts as they are, before the cut.
#[pyfunction]
#[pyo3(signature = (parts, ground, lines, halves, margin, near))]
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
pub fn rail_span_clip<'py>(py: Python<'py>, parts: Vec<PyReadonlyArray2<'py, f64>>, ground: Vec<bool>,
                           lines: Vec<PyReadonlyArray2<'py, f64>>, halves: Vec<f64>, margin: f64,
                           near: Option<(f64, f64, f64, f64)>)
                           -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                        Bound<'py, PyArray1<bool>>)> {
    let pp = Parts::of(&parts, "rail_span_clip")?;
    if ground.len() != pp.n() {
        return Err(pyo3::exceptions::PyValueError::new_err("rail_span_clip: one ground flag per part"));
    }
    let tun = tunnels_of(&lines, &halves)?;
    drop(parts);
    drop(lines);
    let clip = near.is_some();
    let near = near.map_or([0.0; 4], |n| [n.0, n.1, n.2, n.3]);
    let got: Result<_, Fail> = py.detach(|| {
        // (part, a, b, bridge, ground) before the cut
        let mut pieces: Vec<(usize, usize, usize, bool, bool)> = Vec::new();
        if tun.is_empty() {
            for (p, &g) in ground.iter().enumerate() {
                pieces.push((p, 0, pp.xy(p).len().wrapping_sub(1), false, g));
            }
        } else {
            let bx = boxes(&pp)?;
            let mut cov: Vec<Vec<bool>> = (0..pp.n()).map(|p| vec![false; pp.xy(p).len()]).collect();
            for tn in &tun {
                for (p, idx, _) in masks(&pp, &bx, tn, margin)? {
                    for i in idx {
                        cov[p][i] = true;
                    }
                }
            }
            for (p, &g) in ground.iter().enumerate() {
                let k = pp.xy(p).len();
                if !g {
                    pieces.push((p, 0, k - 1, false, false));
                    continue;
                }
                if k < 2 {
                    return Err(Fail::Index("rail_span_clip: a ground part of one point"));
                }
                let m = &cov[p];
                let span = |i: usize| m[i] || m[i + 1];
                let mut a = 0;
                for i in 1..k - 1 {
                    if span(i) != span(i - 1) {
                        pieces.push((p, a, i, span(a), !span(a)));
                        a = i;
                    }
                }
                pieces.push((p, a, k - 1, span(a), !span(a)));
            }
        }
        let (mut pi, mut ai, mut bi, mut br) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
        let mut put = |p: usize, a: usize, b: usize, bridge: bool| {
            pi.push(p as i64);
            ai.push(a as i64);
            bi.push(b as i64);
            br.push(bridge);
        };
        for &(p, a, b, bridge, on_ground) in &pieces {
            if !clip || !on_ground {
                put(p, a, b, bridge);
                continue;
            }
            // _clip: b + 1 - a points (none for an empty part: b wraps)
            let xy = &pp.xy(p)[a..b.wrapping_add(1)];
            let inside: Vec<bool> = xy.iter()
                .map(|q| q[0] >= near[0] && q[0] <= near[2] && q[1] >= near[1] && q[1] <= near[3]).collect();
            if inside.iter().all(|&v| v) {
                put(p, a, b, bridge);
                continue;
            }
            let n = xy.len();
            let grow = |i: usize| inside[i] || (i > 0 && inside[i - 1]) || (i + 1 < n && inside[i + 1]);
            let mut i = 0;
            while i < n {
                if !grow(i) {
                    i += 1;
                    continue;
                }
                let s = i;
                while i < n && grow(i) {
                    i += 1;
                }
                if i - s >= 2 {
                    put(p, a + s, a + i - 1, bridge);
                }
            }
        }
        Ok((pi, ai, bi, br))
    });
    let (pi, ai, bi, br) = got?;
    Ok((pi.into_pyarray(py), ai.into_pyarray(py), bi.into_pyarray(py), br.into_pyarray(py)))
}
