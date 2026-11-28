//! The taxiways' fillets (server/aeroways.py _fillets, _fillet, _straight, _from, _seg_dist): where a way's end meets another
//! way's or a runway's centreline, or a way's vertex anywhere along it meets a runway's, the corner between the edges filled
//! by an arc tangent to both. A port of the Python as it is, step for step and in its evaluation order, to the bit (numpy's
//! pairwise sum for a 1-D `.sum()`, Python's float `%`, the libm functions of dm.rs): the airports' cold cost was a quarter
//! fillets (the user, 2026-11-17: "do the next biggest pay off now"); every vertex measured against every segment.

use crate::dm::Dm;
use crate::standpaths::{pymod, pysum};

type P = [f64; 2];

#[inline]
fn sub(a: P, b: P) -> P { [a[0] - b[0], a[1] - b[1]] }
#[inline]
fn add(a: P, b: P) -> P { [a[0] + b[0], a[1] + b[1]] }
#[inline]
fn mul(a: P, s: f64) -> P { [a[0] * s, a[1] * s] }
#[inline]
fn hyp(a: P) -> f64 { a[0].dhypot(a[1]) }
#[inline]
fn dot(a: P, b: P) -> f64 { a[0] * b[0] + a[1] * b[1] }
#[inline]
fn clip(x: f64, lo: f64, hi: f64) -> f64 { if x < lo { lo } else if x > hi { hi } else { x } }
#[inline]
fn fmax(a: f64, b: f64) -> f64 { if b > a { b } else { a } }
#[inline]
fn fmin(a: f64, b: f64) -> f64 { if b < a { b } else { a } }
#[inline]
fn rad(deg: f64) -> f64 { deg * (std::f64::consts::PI / 180.0) }      // (math.radians)

/// The constants (aeroways.py: the caller passes them).
pub struct Fp { pub join_tol_m: f64, pub fillet_min_deg: f64, pub fillet_r_k: f64, pub fillet_max_k: f64,
                pub fillet_arc_n: usize, pub fillet_min_run_m: f64, pub straight_deg: f64 }

/// aeroways._seg_dist of one segment.
#[inline]
fn seg_dist(p: P, a: P, b: P) -> f64 {
    let ab = sub(b, a);
    let t = clip(dot(sub(p, a), ab) / fmax(dot(ab, ab), 1e-12), 0.0, 1.0);
    hyp(sub(add(a, mul(ab, t)), p))
}

/// aeroways._straight: how far the polyline runs on from its first point within max_deg of its first direction.
fn straight(line: &[P], max_deg: f64) -> f64 {
    let mut d: Vec<P> = vec![];
    let mut seg: Vec<f64> = vec![];
    for i in 1..line.len() {
        let di = sub(line[i], line[i - 1]);
        let s = di[0].dhypot(di[1]);
        if s > 1e-6 {
            d.push(di);
            seg.push(s);
        }
    }
    if seg.is_empty() { return 0.0; }
    let u0 = [d[0][0] / seg[0], d[0][1] / seg[0]];
    let lim = rad(max_deg).dcos();
    for k in 0..seg.len() {
        let u = [d[k][0] / seg[k], d[k][1] / seg[k]];
        if u[0] * u0[0] + u[1] * u0[1] < lim { return pysum(&seg[..k]); }
    }
    pysum(&seg)
}

/// aeroways._from: the polyline from its point nearest p, on in `direction`.
fn from(line: &[P], p: P, direction: P) -> Vec<P> {
    let (mut bk, mut bd, mut bq) = (0usize, f64::INFINITY, [0.0, 0.0]);
    for k in 0..line.len() - 1 {
        let (a, ab) = (line[k], sub(line[k + 1], line[k]));
        let tt = clip(dot(sub(p, a), ab) / fmax(dot(ab, ab), 1e-12), 0.0, 1.0);
        let q = add(a, mul(ab, tt));
        let dist = hyp(sub(q, p));
        if dist < bd { bd = dist; bk = k; bq = q; }
    }
    let ab = sub(line[bk + 1], line[bk]);
    let mut out = vec![bq];
    if ab[0] * direction[0] + ab[1] * direction[1] >= 0.0 {
        out.extend_from_slice(&line[bk + 1..]);
    } else {
        out.extend(line[..=bk].iter().rev());
    }
    out
}

/// aeroways._fillet: the fillets where a way leaves the pavement it joins at v.
fn fillet(v: P, head: &[P], half: f64, r: f64, t: P, joined: &[P], fp: &Fp) -> Vec<Vec<P>> {
    let d = sub(head[1], head[0]);
    let dl = fmax(hyp(d), 1e-9);
    let u = [d[0] / dl, d[1] / dl];
    if (u[0] * t[1] - u[1] * t[0]).abs() < rad(fp.fillet_min_deg).dsin() { return vec![]; }
    let straight_u = straight(head, fp.straight_deg);
    let n_u = [-u[1], u[0]];
    let n_t = [-t[1], t[0]];
    let m = if n_t[0] * u[0] + n_t[1] * u[1] > 0.0 { n_t } else { [-n_t[0], -n_t[1]] };
    let pi = std::f64::consts::PI;
    let mut out = vec![];
    for &sg in &[1.0f64, -1.0] {
        let tp = if (u[0] * t[1] - u[1] * t[0]) * sg > 0.0 { t } else { [-t[0], -t[1]] };
        let (p1, p2) = (add(v, mul(n_u, sg * half)), add(v, mul(m, r)));
        let den = u[0] * tp[1] - u[1] * tp[0];
        if den.abs() < 1e-9 { continue; }
        let w = sub(p2, p1);
        let c = add(p1, mul(u, (w[0] * tp[1] - w[1] * tp[0]) / den));
        let h = fmax(-1.0, fmin(1.0, u[0] * tp[0] + u[1] * tp[1])).dacos() / 2.0;
        if h < 1e-3 || h > pi / 2.0 - 1e-3 { continue; }
        let mut rf = fp.fillet_r_k * half;
        let mut run = rf / h.dtan();
        let cv = sub(c, v);
        let room_u = straight_u - (cv[0] * u[0] + cv[1] * u[1]);
        let room_t = straight(&from(joined, v, tp), fp.straight_deg) - (cv[0] * tp[0] + cv[1] * tp[1]);
        let cap = fmin(fmin(fp.fillet_max_k * half, room_u), room_t);
        if cap < fp.fillet_min_run_m { continue; }
        if run > cap {
            run = cap;
            rf = run * h.dtan();
        }
        let (t1, t2) = (add(c, mul(u, run)), add(c, mul(tp, run)));
        let ut = add(u, tp);
        let bl = fmax(hyp(ut), 1e-9);
        let bis = [ut[0] / bl, ut[1] / bl];
        let o = add(c, mul(bis, rf / h.dsin()));
        let a1 = (t1[1] - o[1]).datan2(t1[0] - o[0]);
        let a2 = (t2[1] - o[1]).datan2(t2[0] - o[0]);
        let da = pymod(a2 - a1 + pi, 2.0 * pi) - pi;
        let n = fp.fillet_arc_n;
        let mut ring = vec![c];
        for k in 0..n {
            let s = if k == n - 1 { 1.0 } else { (k as f64) * (1.0 / ((n - 1) as f64)) };   // (np.linspace(0, 1, n))
            let ang = a1 + da * s;
            ring.push([o[0] + rf * ang.dcos(), o[1] + rf * ang.dsin()]);
        }
        out.push(ring);
    }
    out
}

/// aeroways._fillets: ways, runways [(xy (n, 2), half width)] -> the fillets' rings.
pub fn fillets(ways: &[(Vec<P>, f64)], runways: &[(Vec<P>, f64)], fp: &Fp) -> Vec<Vec<P>> {
    if ways.is_empty() { return vec![]; }
    let others: Vec<&(Vec<P>, f64)> = ways.iter().chain(runways.iter()).collect();
    let (mut sa, mut sb, mut shalf, mut sown) = (vec![], vec![], vec![], vec![]);
    for (i, (xy, h)) in others.iter().enumerate() {
        for k in 0..xy.len().saturating_sub(1) {
            sa.push(xy[k]);
            sb.push(xy[k + 1]);
            shalf.push(*h);
            sown.push(i);
        }
    }
    let nw = ways.len();
    let any_runway = sown.iter().any(|&o| o >= nw);
    let mut out = vec![];
    for (wi, (xy, half)) in ways.iter().enumerate() {
        let n = xy.len();
        let idx: Vec<usize> = if any_runway { (0..n).collect() } else { vec![0, n - 1] };
        for k in idx {
            let end = k == 0 || k == n - 1;
            let (mut j, mut best) = (usize::MAX, f64::INFINITY);
            for s in 0..sa.len() {
                if sown[s] == wi || (!end && sown[s] < nw) { continue; }       // (not the way itself; inside a way: runways only)
                let dd = seg_dist(xy[k], sa[s], sb[s]);
                if dd < best { best = dd; j = s; }        // (np.argmin of the rest, the excluded at inf: the first of equals)
            }
            if j == usize::MAX || !(best <= fp.join_tol_m) { continue; }
            let r = shalf[j];
            if r < *half { continue; }
            let td = sub(sb[j], sa[j]);
            let tl = fmax(hyp(td), 1e-9);
            let t = [td[0] / tl, td[1] / tl];
            let joined = &others[sown[j]].0;
            let fwd: Vec<P> = xy[k..].to_vec();
            let back: Vec<P> = xy[..=k].iter().rev().copied().collect();
            for head in [fwd, back] {
                if head.len() >= 2 { out.extend(fillet(xy[k], &head, *half, r, t, joined, fp)); }
            }
        }
    }
    out
}

// ------------------------------------------------------------------------------------------------------- binding

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

fn lines(pts: PyReadonlyArray2<'_, f64>, starts: PyReadonlyArray1<'_, i64>, half: PyReadonlyArray1<'_, f64>)
         -> PyResult<Vec<(Vec<P>, f64)>> {
    let (p, s, h) = (pts.as_array(), starts.as_array(), half.as_array());
    if s.len() != h.len() + 1 || (p.nrows() > 0 && p.ncols() < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("aero_fillets: points (m, 2), starts (L + 1,), half (L,)"));
    }
    let mut out = vec![];
    for i in 0..h.len() {
        let (a, b) = (s[i], s[i + 1]);
        if a < 0 || b < a + 2 || b as usize > p.nrows() {
            return Err(pyo3::exceptions::PyValueError::new_err("aero_fillets: each line two points or more, within the points"));
        }
        out.push(((a as usize..b as usize).map(|k| [p[[k, 0]], p[[k, 1]]]).collect(), h[i]));
    }
    Ok(out)
}

/// aeroways._fillets: the ways and the runways (local metres: points (m, 2), starts (L + 1,), half widths (L,)), the
/// constants (join_tol_m, fillet_min_deg, fillet_r_k, fillet_max_k, fillet_arc_n, fillet_min_run_m, straight_deg) ->
/// (the fillets' points (N, 2), starts (F + 1,)).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn aero_fillets<'py>(py: Python<'py>, way_pts: PyReadonlyArray2<'py, f64>, way_starts: PyReadonlyArray1<'py, i64>,
                     way_half: PyReadonlyArray1<'py, f64>, rw_pts: PyReadonlyArray2<'py, f64>,
                     rw_starts: PyReadonlyArray1<'py, i64>, rw_half: PyReadonlyArray1<'py, f64>, params: Vec<f64>)
                     -> PyResult<(Bound<'py, numpy::PyArray2<f64>>, Vec<i64>)> {
    let ways = lines(way_pts, way_starts, way_half)?;
    let runways = lines(rw_pts, rw_starts, rw_half)?;
    if params.len() != 7 || params[4] < 2.0 {
        return Err(pyo3::exceptions::PyValueError::new_err("aero_fillets: 7 constants, the arc 2 points or more"));
    }
    let fp = Fp { join_tol_m: params[0], fillet_min_deg: params[1], fillet_r_k: params[2], fillet_max_k: params[3],
                  fillet_arc_n: params[4] as usize, fillet_min_run_m: params[5], straight_deg: params[6] };
    let rings = py.detach(|| fillets(&ways, &runways, &fp));
    let mut flat = vec![];
    let mut starts = vec![0i64];
    for r in &rings {
        for q in r { flat.push(q[0]); flat.push(q[1]); }
        starts.push((flat.len() / 2) as i64);
    }
    let n = flat.len() / 2;
    Ok((numpy::ndarray::Array2::from_shape_vec((n, 2), flat).unwrap().into_pyarray(py), starts))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(aero_fillets, m)?)?;
    Ok(())
}
