//! FAST_GAP's landings (grading._Planner._landings_in and _landing_chain): the free ends of the fast roads' decks round
//! some points, each once at its 0.1 m key (BNE_ROADSYS=surface: its direction to 1e-4, the same whichever map cell's
//! copy of the deck was read), a deck end another deck goes on from being a joint, no landing; and the approach a car
//! drives off one of them on (the ground parts of the deck's class chained from it). Python keeps the memo and its
//! marks and reads the map's lines (_raw_lines); this is the numpy version's arithmetic bit for bit (dnp's hypot, its
//! interp, its dot of two 2-vectors, numpy's pairwise sum, CPython's round).
use crate::devctx::pairwise;
use crate::dm::{interp, Dm};
use crate::dmmatmul::dot1;
use crate::roadnet::pyfloat::py_round;

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::HashMap;
use std::collections::HashSet;

type P = [f64; 2];

/// x with -0.0 as 0.0: the bits a Python dict / set key of x hashes and compares by (-0.0 == 0.0).
fn nz(x: f64) -> u64 {
    (x + 0.0).to_bits()
}

/// roads._dir_at: the unit direction out of polyline p at its start (start: into it) or into its end, read over look m
/// (np.interp at min(look, its length) along it); (0, 0) under 1e-9.
fn dir_at(p: &[P], start: bool, look: f64) -> P {
    let q: Vec<P> = if start { p.to_vec() } else { p.iter().rev().copied().collect() };
    let mut s = vec![0.0f64; q.len()];
    for i in 1..q.len() {
        s[i] = s[i - 1] + (q[i][0] - q[i - 1][0]).dhypot(q[i][1] - q[i - 1][1]);
    }
    let last = s[s.len() - 1];
    let t = if last < look { last } else { look };
    let xs: Vec<f64> = q.iter().map(|v| v[0]).collect();
    let ys: Vec<f64> = q.iter().map(|v| v[1]).collect();
    let qx = interp(&[t], &s, &xs, None, None)[0];
    let qy = interp(&[t], &s, &ys, None, None)[0];
    let u = if start { [qx - q[0][0], qy - q[0][1]] } else { [q[0][0] - qx, q[0][1] - qy] };
    let n = u[0].dhypot(u[1]);
    if n > 1e-9 { [u[0] / n, u[1] / n] } else { [0.0, 0.0] }
}

/// grading._along: the points d m along polyline line from its start (those past its length + 1e-9 left out).
fn along(line: &[P], d: &[f64]) -> Vec<P> {
    let mut sl = vec![0.0f64; line.len()];
    for i in 1..line.len() {
        sl[i] = sl[i - 1] + (line[i][0] - line[i - 1][0]).dhypot(line[i][1] - line[i - 1][1]);
    }
    let lim = sl[sl.len() - 1] + 1e-9;
    let d: Vec<f64> = d.iter().copied().filter(|&v| v <= lim).collect();
    let xs: Vec<f64> = line.iter().map(|v| v[0]).collect();
    let ys: Vec<f64> = line.iter().map(|v| v[1]).collect();
    let (x, y) = (interp(&d, &sl, &xs, None, None), interp(&d, &sl, &ys, None, None));
    x.into_iter().zip(y).map(|(a, b)| [a, b]).collect()
}

fn lines_of(xy: &PyReadonlyArray2<'_, f64>, offs: &[usize]) -> PyResult<(Vec<P>, Vec<(usize, usize)>)> {
    let a = xy.as_array();
    if a.ncols() < 2 {
        return Err(PyValueError::new_err("landings: points of 2 columns"));
    }
    let pts: Vec<P> = (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect();
    let mut spans = Vec::with_capacity(offs.len().saturating_sub(1));
    for w in offs.windows(2) {
        if w[0] > w[1] || w[1] > pts.len() {
            return Err(PyValueError::new_err("landings: offsets out of order"));
        }
        spans.push((w[0], w[1]));
    }
    Ok((pts, spans))
}

/// Python's tuple order of floats (no NaNs here): the first unequal element decides.
fn cmp_f(a: &[f64], b: &[f64]) -> Ordering {
    for (x, y) in a.iter().zip(b) {
        match x.partial_cmp(y) {
            Some(Ordering::Equal) | None => continue,
            Some(o) => return o,
        }
    }
    Ordering::Equal
}

type Row = (f64, f64, f64, f64, String);

fn row_lt(a: &Row, b: &Row) -> bool {
    match cmp_f(&[a.0, a.1, a.2, a.3], &[b.0, b.1, b.2, b.3]) {
        Ordering::Less => true,
        Ordering::Greater => false,
        Ordering::Equal => a.4 < b.4,
    }
}

/// grading._Planner._landings_in's deck ends: [(x, y, vx, vy, class)] of the bridges' lines (xy (n, 2) split at offs,
/// in the cells' order; cls each line's class, None for a link's), sorted by their ends' 0.1 m keys: both ends of every
/// line (the direction into the deck read over look m: roads._dir_at), those of a class in classes, not a joint
/// (another end within join m whose direction meets this one's, dot < -0.5), each key's least row (snap: the end at its
/// key and the direction out into the road to 1e-4; else the exact values), the key kept as first met.
#[pyfunction]
#[pyo3(signature = (xy, offs, cls, classes, join_m, look_m, snap))]
#[allow(clippy::too_many_arguments)]
pub fn landing_ends(py: Python<'_>, xy: PyReadonlyArray2<'_, f64>, offs: Vec<usize>, cls: Vec<Option<String>>,
                    classes: Vec<String>, join_m: f64, look_m: f64, snap: bool) -> PyResult<Vec<Row>> {
    let (pts, spans) = lines_of(&xy, &offs)?;
    if spans.len() != cls.len() {
        return Err(PyValueError::new_err("landing_ends: one class per line"));
    }
    if spans.iter().any(|&(a, b)| a == b) {
        return Err(PyValueError::new_err("landing_ends: an empty line"));
    }
    Ok(py.detach(|| {
        let mut ends: Vec<(P, P, Option<&str>)> = Vec::with_capacity(2 * spans.len());
        for (k, &(a, b)) in spans.iter().enumerate() {
            let ln = &pts[a..b];
            let c = cls[k].as_deref();
            ends.push((ln[0], dir_at(ln, true, look_m), c));
            let d = dir_at(ln, false, look_m);
            ends.push((ln[ln.len() - 1], [-d[0], -d[1]], c));
        }
        let mut keys: Vec<(f64, f64)> = Vec::new();
        let mut rows: Vec<Row> = Vec::new();
        let mut at: HashMap<(u64, u64), usize> = HashMap::new();
        for &(e, d, c) in &ends {
            let Some(c) = c.filter(|c| classes.iter().any(|k| k == c)) else { continue };
            if ends.iter().any(|&(e2, d2, _)| (e2[0] - e[0]).dhypot(e2[1] - e[1]) <= join_m && dot1(&d2, &d) < -0.5) {
                continue;
            }
            let key = (py_round(e[0], 1), py_round(e[1], 1));
            let row: Row = if snap {
                (key.0, key.1, py_round(-d[0], 4), py_round(-d[1], 4), c.to_string())
            } else {
                (e[0], e[1], -d[0], -d[1], c.to_string())
            };
            match at.get(&(nz(key.0), nz(key.1))) {
                Some(&i) => {
                    if row_lt(&row, &rows[i]) {
                        rows[i] = row;
                    }
                }
                None => {
                    at.insert((nz(key.0), nz(key.1)), rows.len());
                    keys.push(key);
                    rows.push(row);
                }
            }
        }
        let mut order: Vec<usize> = (0..rows.len()).collect();
        order.sort_by(|&i, &j| cmp_f(&[keys[i].0, keys[i].1], &[keys[j].0, keys[j].1]));
        order.into_iter().map(|i| rows[i].clone()).collect()
    }))
}

/// A ground line's identity in _landing_chain: its ends to 0.01 m and its point count.
type Kk = (f64, f64, f64, f64, usize);

fn kk_eq(a: &Kk, b: &Kk) -> bool {
    a.0 == b.0 && a.1 == b.1 && a.2 == b.2 && a.3 == b.3 && a.4 == b.4
}

fn kk_cmp(a: &Kk, b: &Kk) -> Ordering {
    match cmp_f(&[a.0, a.1, a.2, a.3], &[b.0, b.1, b.2, b.3]) {
        Ordering::Equal => a.4.cmp(&b.4),
        o => o,
    }
}

/// grading._Planner._landing_chain's approach of the deck end (ex, ey), direction (vx, vy) into the road: the ground
/// lines xy (n, 2) split at offs (in the cells' order; cand: of the deck's class, not a link, built), each once by its
/// ends to 0.01 m and length (the first met), candidates and the rest each in that key's order. From the end the
/// candidate starting within start_m (first) / link_m (after) heading on within cos (the first one's gap to it too, past
/// join_m), the one joined at the node (join_m) before one beside it, then most straight, then the least key; chained
/// while under chain_m long. None without one; else (the approach line, its consecutive points 1e-6 apart, the used
/// lines' indices in order, the other lines' indices: not used, their boxes within id_m + 1 m of the approach's points
/// near_d along it).
#[pyfunction]
#[pyo3(signature = (end, v, xy, offs, cand, near_d, params))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn landing_chain<'py>(py: Python<'py>, end: (f64, f64), v: (f64, f64), xy: PyReadonlyArray2<'py, f64>,
                          offs: Vec<usize>, cand: Vec<bool>, near_d: PyReadonlyArray1<'py, f64>,
                          params: (f64, f64, f64, f64, f64, f64, f64))
                          -> PyResult<Option<(Bound<'py, PyArray2<f64>>, Vec<usize>, Vec<usize>)>> {
    let (pts, spans) = lines_of(&xy, &offs)?;
    if spans.len() != cand.len() {
        return Err(PyValueError::new_err("landing_chain: one flag per line"));
    }
    let near_d = near_d.as_slice()?.to_vec();
    let (start_m, link_m, join_m, cos, chain_m, id_m, look_m) = params;
    let got = py.detach(|| {
        // (each line once by its key, the first met; candidates and the rest each sorted by it)
        let mut seen: HashSet<(u64, u64, u64, u64, usize)> = HashSet::new();
        let (mut cands, mut rest): (Vec<(Kk, usize)>, Vec<(Kk, usize)>) = (Vec::new(), Vec::new());
        for (i, &(a, b)) in spans.iter().enumerate() {
            let g = &pts[a..b];
            if g.len() < 2 {
                continue;
            }
            let l = g[g.len() - 1];
            let kk: Kk = (py_round(g[0][0], 2), py_round(g[0][1], 2), py_round(l[0], 2), py_round(l[1], 2), g.len());
            if !seen.insert((nz(kk.0), nz(kk.1), nz(kk.2), nz(kk.3), kk.4)) {
                continue;
            }
            if cand[i] { cands.push((kk, i)) } else { rest.push((kk, i)) }
        }
        cands.sort_by(|a, b| kk_cmp(&a.0, &b.0));
        rest.sort_by(|a, b| kk_cmp(&a.0, &b.0));
        let line = |i: usize, rev: bool| -> Vec<P> {
            let (a, b) = spans[i];
            if rev { pts[a..b].iter().rev().copied().collect() } else { pts[a..b].to_vec() }
        };
        let mut out: Vec<P> = vec![[end.0, end.1]];
        let mut used: Vec<usize> = Vec::new();         // (indices into cands)
        let (mut at, mut u, mut total, mut first) = ([end.0, end.1], [v.0, v.1], 0.0f64, true);
        while total < chain_m {
            // (rank: (off the node, -cos, key, reversed), the least)
            let mut best: Option<(bool, f64, usize, bool, Vec<P>)> = None;
            for (ci, (kk, gi)) in cands.iter().enumerate() {
                if used.iter().any(|&q| kk_eq(&cands[q].0, kk)) {
                    continue;
                }
                for rev in [false, true] {
                    let q = line(*gi, rev);
                    let d0 = (q[0][0] - at[0]).dhypot(q[0][1] - at[1]);
                    if d0 > (if first { start_m } else { link_m }) {
                        continue;
                    }
                    let w = dir_at(&q, true, look_m);
                    let mut cs = dot1(&w, &u);
                    if first && d0 > join_m {
                        let gap = [(q[0][0] - at[0]) / d0, (q[0][1] - at[1]) / d0];
                        let c2 = dot1(&gap, &u);
                        if c2 < cs {
                            cs = c2;
                        }
                    }
                    if cs < cos {
                        continue;
                    }
                    let off = d0 > join_m;
                    let better = match &best {
                        None => true,
                        Some((bo, bc, bi, br, _)) => {
                            let o = off.cmp(bo).then_with(|| (-cs).partial_cmp(&-*bc).unwrap_or(Ordering::Equal))
                                .then_with(|| kk_cmp(kk, &cands[*bi].0)).then_with(|| rev.cmp(br));
                            o == Ordering::Less
                        }
                    };
                    if better {
                        best = Some((off, cs, ci, rev, q));
                    }
                }
            }
            let Some((_o, _c, ci, _r, q)) = best else { break };
            used.push(ci);
            let seg: Vec<f64> = q.windows(2).map(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1])).collect();
            total += pairwise(&seg);
            at = q[q.len() - 1];
            u = dir_at(&q, false, look_m);
            first = false;
            out.extend_from_slice(&q);
        }
        if used.is_empty() {
            return None;
        }
        let mut ap: Vec<P> = Vec::with_capacity(out.len());
        for i in 0..out.len() {
            if i == 0 || (out[i][0] - out[i - 1][0]).dhypot(out[i][1] - out[i - 1][1]) > 1e-6 {
                ap.push(out[i]);
            }
        }
        if ap.len() < 2 {
            return None;
        }
        let near = along(&ap, &near_d);
        let mut lo = [f64::INFINITY; 2];
        let mut hi = [f64::NEG_INFINITY; 2];
        for p in &near {
            for k in 0..2 {
                lo[k] = lo[k].min(p[k]);
                hi[k] = hi[k].max(p[k]);
            }
        }
        let lo = [lo[0] - id_m - 1.0, lo[1] - id_m - 1.0];
        let hi = [hi[0] + id_m + 1.0, hi[1] + id_m + 1.0];
        let mut other = Vec::new();
        for (kk, gi) in cands.iter().chain(rest.iter()) {
            if used.iter().any(|&q| kk_eq(&cands[q].0, kk)) {
                continue;
            }
            let (a, b) = spans[*gi];
            let g = &pts[a..b];
            let (mut gl, mut gh) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
            for p in g {
                for k in 0..2 {
                    gl[k] = gl[k].min(p[k]);
                    gh[k] = gh[k].max(p[k]);
                }
            }
            if gh[0] < lo[0] || gh[1] < lo[1] || gl[0] > hi[0] || gl[1] > hi[1] {
                continue;
            }
            other.push(*gi);
        }
        let used: Vec<usize> = used.iter().map(|&q| cands[q].1).collect();
        Some((ap, used, other))
    });
    Ok(got.map(|(ap, used, other)| {
        let n = ap.len();
        let flat: Vec<f64> = ap.into_iter().flatten().collect();
        (numpy::ndarray::Array2::from_shape_vec((n, 2), flat).unwrap().into_pyarray(py), used, other)
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dir_at_straight_and_short() {
        let p = [[0.0, 0.0], [20.0, 0.0]];
        assert_eq!(dir_at(&p, true, 10.0), [1.0, 0.0]);
        assert_eq!(dir_at(&p, false, 10.0), [1.0, 0.0]);
        assert_eq!(dir_at(&[[1.0, 1.0]], true, 10.0), [0.0, 0.0]);
    }

    #[test]
    fn along_clips_to_the_length() {
        let p = [[0.0, 0.0], [3.0, 0.0]];
        let got = along(&p, &[0.0, 2.0, 4.0]);
        assert_eq!(got, vec![[0.0, 0.0], [2.0, 0.0]]);
    }

    #[test]
    fn keys_merge_signed_zero() {
        assert_eq!(nz(-0.0), nz(0.0));
        assert_eq!(cmp_f(&[-0.0, 1.0], &[0.0, 2.0]), Ordering::Less);
    }
}
