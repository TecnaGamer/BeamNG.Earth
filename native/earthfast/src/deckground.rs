//! Bridges in the elevation data (grading.DECK_GROUND_*). A surface model (Copernicus GLO-30, outside the lidar areas)
//! holds bridge decks, their pylons and cables as ground, and some lidar models fill a removed deck's gap at about its
//! height: under the Pont de Normandie's south approach the tiles hold a 60-150 m ridge exactly under the deck over
//! ~8 m of marsh (GitHub #1098), and the terrain copied it as a sheer wall under the deck. A deck spans OVER low
//! ground, so the real ground under one is never a ridge along it reaching toward it: where the elevation under a
//! mapped deck stands well above the ground on both sides of it, the ground under it is the line across between the
//! two sides.
//!
//! Per deck (a polyline and its half width), stations every `step` m along it, each with:
//!  - the elevation at the centre line;
//!  - a profile out to each side at `offs` m past the deck's edge; samples inside another deck's footprint (`mask`:
//!    a twin carriageway's deck, a ramp over it) are left out. Each side's ground is the profile's foot: the first
//!    sample past which it falls no more steeply than `slope` (the ridge's smeared flanks are part of the ridge);
//!  - the trigger: the centre's height over the higher of two side samples at `fix` m past the edge, along the
//!    normal and along lines turned `dirs` from it; the least of them. A road or railway embankment crossing under the
//!    deck lies along one of those lines, both its samples on it: no rise over it. A deck along a slope or a cutting's
//!    edge has its higher side at least as high as its centre: no rise either. Lines whose samples would lie past the
//!    deck's ends (on its approaches) are not used.
//!  The rise is median-filtered over three stations; its weight goes from 0 at `t0` m to 1 at `t1` m.
//! Apply: at a point within a corrected deck's corridor (from foot to foot, `blend` m of fade past the feet and `end`
//! m past the deck's ends), the ground is lowered toward the line between the feet (never raised), by that weight;
//! of several decks, the deepest lowering (order independent).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// A deck's centre line, its arcs and half width.
pub struct Line {
    pub p: Vec<[f64; 2]>,
    pub arc: Vec<f64>,
    pub half: f64,
}

impl Line {
    pub fn new(p: Vec<[f64; 2]>, half: f64) -> Line {
        let mut arc = Vec::with_capacity(p.len());
        let mut acc = 0.0;
        for i in 0..p.len() {
            if i > 0 {
                acc += ((p[i][0] - p[i - 1][0]).powi(2) + (p[i][1] - p[i - 1][1]).powi(2)).sqrt();
            }
            arc.push(acc);
        }
        Line { p, arc, half }
    }

    pub fn length(&self) -> f64 {
        *self.arc.last().unwrap_or(&0.0)
    }

    /// (point, unit tangent) at arc s (clamped to the line); a vertex's tangent the mean of its two segments'.
    pub fn at(&self, s: f64) -> ([f64; 2], [f64; 2]) {
        let n = self.p.len();
        let s = s.max(0.0).min(self.length());
        let mut j = 0;
        while j + 2 < n && self.arc[j + 1] <= s {
            j += 1;
        }
        // (a zero-length segment: the next one with a length)
        while j + 2 < n && self.arc[j + 1] - self.arc[j] < 1e-9 {
            j += 1;
        }
        let (a, b) = (self.p[j], self.p[j + 1]);
        let len = self.arc[j + 1] - self.arc[j];
        let t = if len > 1e-9 { ((s - self.arc[j]) / len).max(0.0).min(1.0) } else { 0.0 };
        let c = [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])];
        let mut u = unit([b[0] - a[0], b[1] - a[1]]);
        // (at an inner vertex: the mean of the segments either side)
        if t >= 1.0 - 1e-9 && j + 2 < n {
            let w = unit([self.p[j + 2][0] - b[0], self.p[j + 2][1] - b[1]]);
            u = unit([u[0] + w[0], u[1] + w[1]]);
        } else if t <= 1e-9 && j > 0 {
            let w = unit([a[0] - self.p[j - 1][0], a[1] - self.p[j - 1][1]]);
            u = unit([u[0] + w[0], u[1] + w[1]]);
        }
        (c, u)
    }

    /// (arc s, signed distance d: + left) of point q against the nearest segment; past the ends along the end
    /// segments' lines (s < 0, s > length).
    pub fn project(&self, q: [f64; 2]) -> (f64, f64, f64) {
        let n = self.p.len();
        let (mut best, mut bs, mut bd) = (f64::INFINITY, 0.0, 0.0);
        for j in 0..n - 1 {
            let (a, b) = (self.p[j], self.p[j + 1]);
            let (vx, vy) = (b[0] - a[0], b[1] - a[1]);
            let l2 = vx * vx + vy * vy;
            if l2 < 1e-18 {
                continue;
            }
            let l = l2.sqrt();
            let (wx, wy) = (q[0] - a[0], q[1] - a[1]);
            let tr = (wx * vx + wy * vy) / l2;
            let t = tr.max(0.0).min(1.0);
            let (cx, cy) = (a[0] + t * vx - q[0], a[1] + t * vy - q[1]);
            let dist2 = cx * cx + cy * cy;
            if dist2 < best {
                best = dist2;
                let tt = if (j == 0 && tr < 0.0) || (j == n - 2 && tr > 1.0) { tr } else { t };
                bs = self.arc[j] + tt * l;
                bd = (vx * wy - vy * wx) / l;
            }
        }
        (bs, bd, best.sqrt())
    }

    fn bbox(&self) -> [f64; 4] {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for q in &self.p {
            b[0] = b[0].min(q[0]);
            b[1] = b[1].min(q[1]);
            b[2] = b[2].max(q[0]);
            b[3] = b[3].max(q[1]);
        }
        b
    }
}

fn unit(v: [f64; 2]) -> [f64; 2] {
    let l = (v[0] * v[0] + v[1] * v[1]).sqrt();
    if l > 1e-12 { [v[0] / l, v[1] / l] } else { [1.0, 0.0] }
}

/// The probe settings (grading.DECK_GROUND_*).
pub struct Probe {
    pub step: f64,
    pub offs: Vec<f64>,     // m past the deck's edge, increasing
    pub fix: Vec<f64>,      // m past the edge: the trigger's samples (each side the first not inside another deck)
    pub dirs: Vec<f64>,     // radians from the normal (0 first)
    pub reach: f64,         // m from the centre line no sample goes past (the callers' cell reach)
}

impl Probe {
    /// Arcs of a line's stations (none on a line shorter than 1 cm).
    pub fn stations(&self, line: &Line) -> Vec<f64> {
        let l = line.length();
        if !(l > 0.01) || line.p.len() < 2 {
            return vec![];
        }
        let n = ((l / self.step).ceil() as usize).max(1) + 1;
        (0..n).map(|i| l * i as f64 / (n - 1) as f64).collect()
    }

    pub fn per_station(&self) -> usize {
        1 + 2 * self.offs.len() + 2 * self.dirs.len() * self.fix.len()
    }

    /// The sample points of one station, in order: centre; left offsets; right offsets; per direction its left and
    /// right point at each `fix`. (Offsets past `reach` are still listed: the fit leaves them out.)
    pub fn points(&self, line: &Line, s: f64, out: &mut Vec<[f64; 2]>) {
        let (c, t) = line.at(s);
        let nrm = [-t[1], t[0]];
        out.push(c);
        for sgn in [1.0, -1.0] {
            for &o in &self.offs {
                let r = sgn * (line.half + o);
                out.push([c[0] + r * nrm[0], c[1] + r * nrm[1]]);
            }
        }
        for &phi in &self.dirs {
            let (cs, sn) = (phi.cos(), phi.sin());
            let u = [cs * nrm[0] + sn * t[0], cs * nrm[1] + sn * t[1]];
            for &f in &self.fix {
                let r = (line.half + f) / cs.max(0.05);
                out.push([c[0] + r * u[0], c[1] + r * u[1]]);
                out.push([c[0] - r * u[0], c[1] - r * u[1]]);
            }
        }
    }
}

/// One deck's correction: per station its arc, the ground left and right (m), the feet's distances from the centre
/// line left and right (m) and its weight (0..1).
#[derive(Clone, Debug, Default)]
pub struct Patch {
    pub s: Vec<f64>,
    pub zl: Vec<f64>,
    pub zr: Vec<f64>,
    pub ol: Vec<f64>,
    pub or: Vec<f64>,
    pub w: Vec<f64>,
}

/// The fit settings.
pub struct Fit {
    pub slope: f64,
    pub t0: f64,
    pub t1: f64,
    pub mask_m: f64,
}

fn masked(q: [f64; 2], masks: &[&Line], margin: f64) -> bool {
    for m in masks {
        let lim = m.half + margin;
        let b = m.bbox();
        if q[0] < b[0] - lim || q[0] > b[2] + lim || q[1] < b[1] - lim || q[1] > b[3] + lim {
            continue;
        }
        let (_s, _d, dist) = m.project(q);
        // (project measures past the ends along the end segments: the distance to the segments themselves)
        if dist < lim {
            return true;
        }
    }
    false
}

/// The correction of one deck from its stations' samples z (Probe::points' order, per station), the other decks
/// near it (masks: its own copy may be among them: a sample is never inside its own deck). None: nothing to correct.
pub fn fit_deck(line: &Line, probe: &Probe, fit: &Fit, z: &[f64], masks: &[&Line]) -> Option<Patch> {
    let st = probe.stations(line);
    let ns = st.len();
    let per = probe.per_station();
    if ns == 0 || z.len() != ns * per {
        return None;
    }
    let k = probe.offs.len();
    let nf = probe.fix.len();
    let l = line.length();
    let mut pts = Vec::with_capacity(per);
    let mut rise = vec![f64::NEG_INFINITY; ns];
    let mut foot = vec![[usize::MAX; 2]; ns];
    let mut valid: Vec<Vec<bool>> = Vec::with_capacity(ns);
    // (a mask's own copy of this deck: the same line; never counted, a sample past its edge is not inside it anyway)
    let others: Vec<&Line> = masks.iter().copied().filter(|m| !(m.p == line.p)).collect();
    for (i, &s) in st.iter().enumerate() {
        pts.clear();
        probe.points(line, s, &mut pts);
        let zs = &z[i * per..(i + 1) * per];
        let ok: Vec<bool> = (0..per).map(|j| {
            if j == 0 {
                return zs[0].is_finite();
            }
            if !zs[j].is_finite() {
                return false;
            }
            let o = if j <= 2 * k { probe.offs[(j - 1) % k] } else { probe.fix[(j - 1 - 2 * k) / 2 % nf] };
            if line.half + o > probe.reach {
                return false;
            }
            !masked(pts[j], &others, fit.mask_m)
        }).collect();
        let zc = zs[0];
        if ok[0] {
            // each side's foot: the first valid sample past which the profile falls no more steeply than slope
            for side in 0..2 {
                let idx: Vec<usize> = (0..k).filter(|&m| ok[1 + side * k + m]).collect();
                if idx.is_empty() {
                    continue;
                }
                let mut f = *idx.last().unwrap();
                for w in idx.windows(2) {
                    let (a, b) = (w[0], w[1]);
                    let (za, zb) = (zs[1 + side * k + a], zs[1 + side * k + b]);
                    if zb >= za - fit.slope * (probe.offs[b] - probe.offs[a]) {
                        f = a;
                        break;
                    }
                }
                foot[i][side] = f;
            }
            // the trigger: the least rise over the higher of the two samples of each line
            let mut least = f64::INFINITY;
            for (d, &phi) in probe.dirs.iter().enumerate() {
                // (each side's nearest sample not inside another deck, nor past the deck's ends on a turned line)
                let pick = |side: usize| (0..nf).map(|f| 1 + 2 * k + 2 * (d * nf + f) + side).find(|&j| {
                    ok[j] && (phi == 0.0 || {
                        let shift = (line.half + probe.fix[(j - 1 - 2 * k) / 2 % nf]) * phi.tan().abs();
                        s - shift >= 0.0 && s + shift <= l
                    })
                });
                let (jl, jr) = match (pick(0), pick(1)) {
                    (Some(a), Some(b)) => (a, b),
                    _ => continue,
                };
                let r = zc - zs[jl].max(zs[jr]);
                if r < least {
                    least = r;
                }
            }
            if least.is_finite() {
                rise[i] = least;
            }
        }
        valid.push(ok);
    }
    // the rise median-filtered over three stations (two at an end: the lower)
    let mut w = vec![0.0; ns];
    for i in 0..ns {
        let lo = i.saturating_sub(1);
        let hi = (i + 1).min(ns - 1);
        let mut v: Vec<f64> = (lo..=hi).map(|j| rise[j]).collect();
        v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let m = if v.len() == 3 { v[1] } else { v[0] };
        let x = if fit.t1 > fit.t0 { (m - fit.t0) / (fit.t1 - fit.t0) } else if m >= fit.t0 { 1.0 } else { 0.0 };
        w[i] = if x.is_finite() { x.max(0.0).min(1.0) } else { 0.0 };
        if foot[i][0] == usize::MAX || foot[i][1] == usize::MAX {
            w[i] = 0.0;
        }
    }
    if !w.iter().any(|&v| v > 0.0) {
        return None;
    }
    // the feet widened to their neighbours' (a station's flank read at the next one's foot), where valid there
    let mut out = Patch::default();
    for i in 0..ns {
        let zs = &z[i * per..(i + 1) * per];
        let mut zz = [0.0f64; 2];
        let mut oo = [0.0f64; 2];
        for side in 0..2 {
            let own = foot[i][side];
            if own != usize::MAX {
                let lo = i.saturating_sub(1);
                let hi = (i + 1).min(ns - 1);
                let mut f = (lo..=hi).filter_map(|j| if foot[j][side] != usize::MAX { Some(foot[j][side]) } else { None })
                    .max().unwrap_or(own);
                while f > own && !valid[i][1 + side * k + f] {
                    f -= 1;
                }
                zz[side] = zs[1 + side * k + f];
                oo[side] = line.half + probe.offs[f];
            } else {
                zz[side] = zs[0];
                oo[side] = line.half;
            }
        }
        out.s.push(st[i]);
        out.zl.push(zz[0]);
        out.zr.push(zz[1]);
        out.ol.push(oo[0]);
        out.or.push(oo[1]);
        out.w.push(w[i]);
    }
    Some(out)
}

/// The deepest lowering of base at q by the patches' decks (0 where none reaches it).
pub fn lowering(q: [f64; 2], base: f64, lines: &[&Line], patches: &[&Patch], boxes: &[[f64; 4]], blend: f64,
                end: f64) -> f64 {
    let mut best = 0.0f64;
    for ((line, p), b) in lines.iter().zip(patches.iter()).zip(boxes.iter()) {
        if q[0] < b[0] || q[0] > b[2] || q[1] < b[1] || q[1] > b[3] || p.s.is_empty() {
            continue;
        }
        let (s, d, _dist) = line.project(q);
        let l = line.length();
        let wa = if s < 0.0 { 1.0 - (-s) / end } else if s > l { 1.0 - (s - l) / end } else { 1.0 };
        if !(wa > 0.0) {
            continue;
        }
        let sc = s.max(0.0).min(l);
        // (the station interval holding sc)
        let n = p.s.len();
        let (zl, zr, ol, or, w) = if n == 1 {
            (p.zl[0], p.zr[0], p.ol[0], p.or[0], p.w[0])
        } else {
            let mut i = match p.s.binary_search_by(|v| v.partial_cmp(&sc).unwrap_or(std::cmp::Ordering::Less)) {
                Ok(i) => i,
                Err(i) => i.saturating_sub(1),
            };
            if i >= n - 1 {
                i = n - 2;
            }
            let span = p.s[i + 1] - p.s[i];
            let f = if span > 1e-12 { ((sc - p.s[i]) / span).max(0.0).min(1.0) } else { 0.0 };
            let lerp = |a: &Vec<f64>| a[i] + (a[i + 1] - a[i]) * f;
            (lerp(&p.zl), lerp(&p.zr), lerp(&p.ol), lerp(&p.or), lerp(&p.w))
        };
        if !(w > 0.0) {
            continue;
        }
        let o = if d >= 0.0 { ol } else { or };
        let ad = d.abs();
        let wl = if ad <= o {
            1.0
        } else if ad >= o + blend {
            0.0
        } else {
            let x = (ad - o) / blend;
            1.0 - x * x * (3.0 - 2.0 * x)
        };
        if !(wl > 0.0) {
            continue;
        }
        let t = if d >= ol { zl } else if d <= -or { zr } else { zr + (zl - zr) * (d + or) / (ol + or) };
        let r = w * wa * wl * (base - t);
        if r > best {
            best = r;
        }
    }
    best
}

// ------------------------------------------------------------------------------------------------- Python glue

fn lines_of(off: &[i64], xy: &numpy::ndarray::ArrayView2<f64>, half: &[f64]) -> PyResult<Vec<Line>> {
    let n = half.len();
    if off.len() != n + 1 || off.iter().any(|&o| o < 0 || o as usize > xy.nrows()) || xy.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_ground: inconsistent lines"));
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let (a, b) = (off[i] as usize, off[i + 1] as usize);
        if b < a {
            return Err(pyo3::exceptions::PyValueError::new_err("deck_ground: inconsistent lines"));
        }
        out.push(Line::new((a..b).map(|r| [xy[[r, 0]], xy[[r, 1]]]).collect(), half[i]));
    }
    Ok(out)
}

/// grading._Planner._deck_patches' sample points: per deck (rows off[i]..off[i + 1] of xy, half width half[i]), its
/// stations' points in order (deck_ground_fit reads the heights there in the same order). (m, 2). fix: (f,).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_ground_probe<'py>(py: Python<'py>, off: PyReadonlyArray1<'py, i64>, xy: PyReadonlyArray2<'py, f64>,
                              half: PyReadonlyArray1<'py, f64>, step: f64, offs: PyReadonlyArray1<'py, f64>,
                              fix: PyReadonlyArray1<'py, f64>,
                              dirs: PyReadonlyArray1<'py, f64>, reach: f64) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let lines = lines_of(off.as_slice()?, &xy.as_array(), half.as_slice()?)?;
    let probe = Probe { step, offs: offs.as_slice()?.to_vec(), fix: fix.as_slice()?.to_vec(), dirs: dirs.as_slice()?.to_vec(), reach };
    let flat = py.detach(|| {
        let mut pts: Vec<[f64; 2]> = Vec::new();
        for line in &lines {
            for s in probe.stations(line) {
                probe.points(line, s, &mut pts);
            }
        }
        pts
    });
    let n = flat.len();
    let v: Vec<f64> = flat.into_iter().flat_map(|q| q.into_iter()).collect();
    Ok(numpy::ndarray::Array2::from_shape_vec((n, 2), v)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?.into_pyarray(py))
}

/// The decks' corrections from their samples z (deck_ground_probe's order): masks: the other decks (rows moff of
/// mxy, half widths mhalf), deck i's being mask_idx[mask_off[i]..mask_off[i + 1]]. (active (n,) bool; the active
/// decks' stations: off (n_active + 1,), rows (m, 6): arc, ground left, ground right, foot left, foot right, weight).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_ground_fit<'py>(py: Python<'py>, off: PyReadonlyArray1<'py, i64>, xy: PyReadonlyArray2<'py, f64>,
                            half: PyReadonlyArray1<'py, f64>, step: f64, offs: PyReadonlyArray1<'py, f64>,
                              fix: PyReadonlyArray1<'py, f64>,
                            dirs: PyReadonlyArray1<'py, f64>, reach: f64, z: PyReadonlyArray1<'py, f64>,
                            moff: PyReadonlyArray1<'py, i64>, mxy: PyReadonlyArray2<'py, f64>,
                            mhalf: PyReadonlyArray1<'py, f64>, mask_off: PyReadonlyArray1<'py, i64>,
                            mask_idx: PyReadonlyArray1<'py, i64>, slope: f64, t0: f64, t1: f64, mask_m: f64)
                            -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>)> {
    let lines = lines_of(off.as_slice()?, &xy.as_array(), half.as_slice()?)?;
    let masks = lines_of(moff.as_slice()?, &mxy.as_array(), mhalf.as_slice()?)?;
    let (mo, mi) = (mask_off.as_slice()?, mask_idx.as_slice()?);
    if mo.len() != lines.len() + 1 || mo.iter().any(|&v| v < 0 || v as usize > mi.len())
        || mi.iter().any(|&v| v < 0 || v as usize >= masks.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_ground_fit: inconsistent masks"));
    }
    let probe = Probe { step, offs: offs.as_slice()?.to_vec(), fix: fix.as_slice()?.to_vec(), dirs: dirs.as_slice()?.to_vec(), reach };
    let fit = Fit { slope, t0, t1, mask_m };
    let z = z.as_slice()?;
    let per = probe.per_station();
    let counts: Vec<usize> = lines.iter().map(|l| probe.stations(l).len() * per).collect();
    if counts.iter().sum::<usize>() != z.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_ground_fit: z does not match the probe"));
    }
    let got: Vec<Option<Patch>> = py.detach(|| {
        let mut at = 0;
        let mut out = Vec::with_capacity(lines.len());
        for (i, line) in lines.iter().enumerate() {
            let zs = &z[at..at + counts[i]];
            at += counts[i];
            let ms: Vec<&Line> = (mo[i] as usize..mo[i + 1] as usize).map(|j| &masks[mi[j] as usize]).collect();
            out.push(fit_deck(line, &probe, &fit, zs, &ms));
        }
        out
    });
    let active: Vec<bool> = got.iter().map(|g| g.is_some()).collect();
    let mut po = vec![0i64];
    let mut rows: Vec<f64> = Vec::new();
    for p in got.iter().flatten() {
        for i in 0..p.s.len() {
            rows.extend_from_slice(&[p.s[i], p.zl[i], p.zr[i], p.ol[i], p.or[i], p.w[i]]);
        }
        po.push(po.last().unwrap() + p.s.len() as i64);
    }
    let nr = rows.len() / 6;
    Ok((active.into_pyarray(py), po.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((nr, 6), rows)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?.into_pyarray(py)))
}

/// base (n,) at points (e, n) lowered under the corrected decks (lines off / xy / half; their stations soff / rows as
/// deck_ground_fit made them): None when nothing changes, else the new heights.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_ground_apply<'py>(py: Python<'py>, e: PyReadonlyArray1<'py, f64>, n: PyReadonlyArray1<'py, f64>,
                              base: PyReadonlyArray1<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                              xy: PyReadonlyArray2<'py, f64>, half: PyReadonlyArray1<'py, f64>,
                              soff: PyReadonlyArray1<'py, i64>, rows: PyReadonlyArray2<'py, f64>, blend: f64, end: f64)
                              -> PyResult<Option<Bound<'py, PyArray1<f64>>>> {
    let lines = lines_of(off.as_slice()?, &xy.as_array(), half.as_slice()?)?;
    let (e, nn, base) = (e.as_slice()?, n.as_slice()?, base.as_slice()?);
    let (so, r) = (soff.as_slice()?, rows.as_array());
    if e.len() != nn.len() || e.len() != base.len() || so.len() != lines.len() + 1 || r.ncols() != 6
        || so.iter().any(|&v| v < 0 || v as usize > r.nrows()) {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_ground_apply: inconsistent arrays"));
    }
    let patches: Vec<Patch> = (0..lines.len()).map(|i| {
        let mut p = Patch::default();
        for k in so[i] as usize..so[i + 1] as usize {
            p.s.push(r[[k, 0]]);
            p.zl.push(r[[k, 1]]);
            p.zr.push(r[[k, 2]]);
            p.ol.push(r[[k, 3]]);
            p.or.push(r[[k, 4]]);
            p.w.push(r[[k, 5]]);
        }
        p
    }).collect();
    let out: Option<Vec<f64>> = py.detach(|| {
        let boxes: Vec<[f64; 4]> = lines.iter().zip(patches.iter()).map(|(l, p)| {
            let b = l.bbox();
            let o = p.ol.iter().chain(p.or.iter()).fold(0.0f64, |a, &v| a.max(v)) + blend + end;
            [b[0] - o, b[1] - o, b[2] + o, b[3] + o]
        }).collect();
        let lr: Vec<&Line> = lines.iter().collect();
        let pr: Vec<&Patch> = patches.iter().collect();
        let mut out: Option<Vec<f64>> = None;
        for i in 0..e.len() {
            if !base[i].is_finite() {
                continue;
            }
            let low = lowering([e[i], nn[i]], base[i], &lr, &pr, &boxes, blend, end);
            if low > 0.0 {
                out.get_or_insert_with(|| base.to_vec())[i] = base[i] - low;
            }
        }
        out
    });
    Ok(out.map(|v| v.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn probe() -> Probe {
        Probe {
            step: 6.0,
            offs: vec![3.0, 6.0, 9.0, 12.0, 15.0, 19.0, 23.0, 28.0, 34.0, 41.0, 49.0, 58.0, 68.0, 80.0, 94.0, 110.0,
                       128.0, 148.0],
            fix: vec![15.0, 25.0, 36.0],
            dirs: vec![0.0, 35f64.to_radians(), -35f64.to_radians(), 60f64.to_radians(), -60f64.to_radians()],
            reach: 160.0,
        }
    }

    fn fit() -> Fit {
        Fit { slope: 0.15, t0: 2.0, t1: 4.0, mask_m: 1.5 }
    }

    /// The samples of line's stations from ground(x, y).
    fn sample(line: &Line, pr: &Probe, ground: &dyn Fn(f64, f64) -> f64) -> Vec<f64> {
        let mut pts = Vec::new();
        for s in pr.stations(line) {
            pr.points(line, s, &mut pts);
        }
        pts.iter().map(|q| ground(q[0], q[1])).collect()
    }

    fn deck() -> Line {
        // a 400 m deck west-east along y = 0, half width 5
        Line::new(vec![[-200.0, 0.0], [0.0, 0.0], [200.0, 0.0]], 5.0)
    }

    /// marsh at 8 m with the deck as a smeared ridge 100 m high along y = 0 (|y| < 25 m)
    fn ridge(_x: f64, y: f64) -> f64 {
        8.0 + 100.0 * (1.0 - (y.abs() / 25.0)).max(0.0)
    }

    #[test]
    fn ridge_under_a_deck_is_taken_out() {
        let (pr, ft, l) = (probe(), fit(), deck());
        let z = sample(&l, &pr, &ridge);
        let p = fit_deck(&l, &pr, &ft, &z, &[]).expect("a ridge");
        assert!(p.w.iter().all(|&w| w == 1.0));
        assert!(p.zl.iter().chain(p.zr.iter()).all(|&v| (v - 8.0).abs() < 0.01), "{:?}", p.zl);
        assert!(p.ol.iter().all(|&o| o >= 25.0 && o < 40.0), "{:?}", p.ol);
        let (lr, pp) = (vec![&l], vec![&p]);
        let bx = vec![[-400.0, -400.0, 400.0, 400.0]];
        for y in [-30.0, -20.0, -5.0, 0.0, 3.0, 12.0, 24.0] {
            let b = ridge(50.0, y);
            let low = lowering([50.0, y], b, &lr, &pp, &bx, 4.0, 3.0);
            assert!((b - low - 8.0).abs() < 0.01, "y {} -> {}", y, b - low);
        }
        // past the deck's end (its approach): left
        assert_eq!(lowering([210.0, 0.0], 108.0, &lr, &pp, &bx, 4.0, 3.0), 0.0);
        assert!(lowering([201.0, 0.0], 108.0, &lr, &pp, &bx, 4.0, 3.0) > 0.0);
    }

    #[test]
    fn valley_under_a_deck_stays() {
        // a V valley across the deck (along x): the ground under the deck is lower in the middle, flat across
        let (pr, ft, l) = (probe(), fit(), deck());
        let z = sample(&l, &pr, &|x: f64, _y: f64| 20.0 + 0.4 * x.abs());
        assert!(fit_deck(&l, &pr, &ft, &z, &[]).is_none());
        // a river channel along under the deck: lower, never raised
        let z = sample(&l, &pr, &|_x: f64, y: f64| if y.abs() < 20.0 { 2.0 } else { 8.0 });
        assert!(fit_deck(&l, &pr, &ft, &z, &[]).is_none());
    }

    #[test]
    fn embankment_crossing_under_stays() {
        // a road embankment 8 m high, 30 m wide at its toe, crossing the deck at 90, 60 and 40 degrees
        let (pr, ft, l) = (probe(), fit(), deck());
        for ang in [90f64, 60.0, 40.0] {
            let a = ang.to_radians();
            let (ux, uy) = (a.cos(), a.sin());
            let bank = move |x: f64, y: f64| {
                let d = (x * uy - y * ux).abs();      // distance from the embankment's axis through (0, 0)
                3.0 + 8.0 * ((15.0 - d) / 8.0).max(0.0).min(1.0)
            };
            let z = sample(&l, &pr, &bank);
            assert!(fit_deck(&l, &pr, &ft, &z, &[]).is_none(), "angle {}", ang);
        }
    }

    #[test]
    fn slope_and_cutting_edge_stay() {
        let (pr, ft, l) = (probe(), fit(), deck());
        // a deck along a hillside (the ground rising to the north)
        let z = sample(&l, &pr, &|_x: f64, y: f64| 50.0 + 0.5 * y);
        assert!(fit_deck(&l, &pr, &ft, &z, &[]).is_none());
        // a deck along a railway cutting's edge: a 10 m drop just south of it
        let z = sample(&l, &pr, &|_x: f64, y: f64| if y < -8.0 { 20.0 } else { 30.0 });
        assert!(fit_deck(&l, &pr, &ft, &z, &[]).is_none());
    }

    #[test]
    fn twin_deck_is_masked() {
        // two carriageways' decks 14 m apart over one ridge: each one's sides read past the other's footprint
        let (pr, ft) = (probe(), fit());
        let a = Line::new(vec![[-200.0, 0.0], [200.0, 0.0]], 5.0);
        let b = Line::new(vec![[-200.0, 14.0], [200.0, 14.0]], 5.0);
        let g = |_x: f64, y: f64| 8.0 + 100.0 * (1.0 - ((y - 7.0).abs() / 30.0)).max(0.0);
        let z = sample(&a, &pr, &g);
        let p = fit_deck(&a, &pr, &ft, &z, &[&a, &b]).expect("a ridge");
        assert!(p.ol.iter().all(|&o| o >= 37.0), "{:?}", p.ol);
        assert!(p.zl.iter().chain(p.zr.iter()).all(|&v| (v - 8.0).abs() < 0.01));
        let (lr, pp) = (vec![&a], vec![&p]);
        let bx = vec![[-400.0, -400.0, 400.0, 400.0]];
        let b7 = g(0.0, 7.0);
        assert!((b7 - lowering([0.0, 7.0], b7, &lr, &pp, &bx, 4.0, 3.0) - 8.0).abs() < 0.01);
    }

    #[test]
    fn small_bump_is_left() {
        let (pr, ft, l) = (probe(), fit(), deck());
        let z = sample(&l, &pr, &|_x: f64, y: f64| 8.0 + 1.5 * (1.0 - y.abs() / 10.0).max(0.0));
        assert!(fit_deck(&l, &pr, &ft, &z, &[]).is_none());
    }

    #[test]
    fn project_signs_and_ends() {
        let l = deck();
        let (s, d, _) = l.project([10.0, 3.0]);
        assert!((s - 210.0).abs() < 1e-9 && (d - 3.0).abs() < 1e-9);
        let (s, d, _) = l.project([-210.0, -2.0]);
        assert!((s + 10.0).abs() < 1e-9 && (d + 2.0).abs() < 1e-9);
    }
}
