//! A racetrack's surface where raceways meet (server grading.raceway_ground, raceway_dominance; BNE_RACEWAY_DIG, road
//! revamp R1k follow-up after the user's Atlanta reports 2026-11-17: "where these roads meet the racetrack causes bumps
//! on the track").
//!
//! Each raceway map segment has its own smoothed surface (grading.raceway_surface: a grid along x across its line). A
//! point on several takes the nearest centre line's (`raceway_ground`, the old rule, kept bit for bit with numpy's
//! arithmetic for the default build). Where an infield road or a pit lane (itself highway=raceway, the same default
//! width) ends on the oval, the nearest-line rule cut the oval's banked surface along the line half way between the two
//! centre lines and put the joining road's own surface on its side: a step of 0.3-0.7 m across the racing surface.
//!
//! The layered rule (`joins`): a raceway that ends on another raceway's band gives way to it there (`dominance`, from
//! the two lines alone: the other passes through there; where both end there and do not go on from each other, the
//! wider, then the longer, wins). Round each join (the giving way zone's points within the two half widths + `blend`
//! of the join's end and within `blend` of the other's band) the giving way zone is laid first, eased to the other's
//! surface at that band's edge (on its own side; inside the band the other's surface itself), then every zone
//! elsewhere by the nearest-line rule over it, with its edge fade (`margin`) to what lies under it. Never a height
//! inside the dominant's carriageway rewritten; away from joins nothing changes.
//!
//! Sums, products, min / max, floor and sqrt only (polyproject::project): the same bits on every platform.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::polyproject;

/// v * v: f64::powi(v, 2) written out (the determinism lints: powi's exponent may call compiler-rt). powi(v, 2) is
/// exactly v * v (LLVM folds the constant exponent to one fmul; compiler-rt's __powidf2 squares once and multiplies by
/// 1.0): the same bits, racejoin_sq_is_powi checks it.
#[inline]
fn sq(v: f64) -> f64 {
    v * v
}

/// One raceway segment's smoothed surface (grading.raceway_surface's (line, stations, offsets, surface)).
pub struct Zone {
    pub line: Vec<f64>, // k x 2
    pub k: usize,
    pub ds: f64,    // stations[1] - stations[0]
    pub s_end: f64, // stations[-1]
    pub o0: f64,    // offsets[0]
    pub d_o: f64,   // offsets[1] - offsets[0]
    pub hw: f64,    // offsets[-1]
    pub nt: usize,
    pub surf: Vec<f64>, // k x nt
}

/// numpy's clip(x, lo, hi) (_NPY_MAX then _NPY_MIN: a NaN kept, +0.0 for -0.0 against a 0.0 bound).
#[inline]
fn npclip(x: f64, lo: f64, hi: f64) -> f64 {
    let m = if x.is_nan() || x > lo { x } else { lo };
    if m.is_nan() || m < hi { m } else { hi }
}

/// Python's max(a, b) for floats (b only when greater).
#[inline]
fn pymax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

#[inline]
fn smooth(u: f64) -> f64 {
    u * u * (3.0 - 2.0 * u)
}

impl Zone {
    /// The surface at a station and offset across (bilinear on the grid), as raceway_ground's numpy.
    fn at(&self, station: f64, off: f64) -> f64 {
        let fs = npclip(station / pymax(self.ds, 1e-9), 0.0, self.k as f64 - 1.0);
        let i = (fs.floor() as i64).min(self.k as i64 - 2).max(0) as usize;
        let a = fs - i as f64;
        let ft = npclip((off - self.o0) / pymax(self.d_o, 1e-9), 0.0, self.nt as f64 - 1.0);
        let j = (ft.floor() as i64).min(self.nt as i64 - 2).max(0) as usize;
        let b = ft - j as f64;
        let nt = self.nt;
        let c00 = self.surf[i * nt + j];
        let c10 = self.surf[(i + 1) * nt + j];
        let c01 = self.surf[i * nt + j + 1];
        let c11 = self.surf[(i + 1) * nt + j + 1];
        c00 * (1.0 - a) * (1.0 - b) + c10 * a * (1.0 - b) + c01 * (1.0 - a) * b + c11 * a * b
    }

    fn bbox(&self) -> (f64, f64, f64, f64) {
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for p in 0..self.k {
            let (x, y) = (self.line[2 * p], self.line[2 * p + 1]);
            // (numpy's min / max: a NaN wins)
            x0 = if x.is_nan() || x < x0 { x } else { x0 };
            x1 = if x.is_nan() || x > x1 { x } else { x1 };
            y0 = if y.is_nan() || y < y0 { y } else { y0 };
            y1 = if y.is_nan() || y > y1 { y } else { y1 };
        }
        (x0, y0, x1, y1)
    }

    /// raceway_ground's skip: the zone's box grown by `grow` misses the points' box.
    fn misses(&self, grow: f64, ext: (f64, f64, f64, f64)) -> bool {
        let (x0, y0, x1, y1) = self.bbox();
        let (ex0, ny0, ex1, ny1) = ext;
        x0 - grow > ex1 || x1 + grow < ex0 || y0 - grow > ny1 || y1 + grow < ny0
    }
}

/// Every point projected on a zone's line (lazily, once per zone).
struct Proj {
    station: Vec<f64>,
    offset: Vec<f64>,
    dist: Vec<f64>,
}

fn project_all(z: &Zone, pts: &[f64]) -> Proj {
    let m = pts.len() / 2;
    let r = polyproject::project(&z.line, 2, z.k, pts, 2, m);
    Proj { station: r.station, offset: r.offset, dist: r.dist }
}

/// The points' box as numpy's e.min() / e.max() / n.min() / n.max() (a NaN wins).
fn extents(pts: &[f64]) -> (f64, f64, f64, f64) {
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for p in pts.chunks_exact(2) {
        let (x, y) = (p[0], p[1]);
        x0 = if x.is_nan() || x < x0 { x } else { x0 };
        x1 = if x.is_nan() || x > x1 { x } else { x1 };
        y0 = if y.is_nan() || y < y0 { y } else { y0 };
        y1 = if y.is_nan() || y > y1 { y } else { y1 };
    }
    (x0, y0, x1, y1)
}

/// One join: zone `by` the zone giving way passes over, at the giving way zone's end (x, y).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Join {
    pub by: usize,
    pub x: f64,
    pub y: f64,
    /// the stations of the giving way zone it holds over when given (a window of a longer line: its reach measured
    /// on the whole line, racedig.track_surface; Codex review of 80e9b3da), NaN: join_reach on the zone's own line
    pub lo: f64,
    pub hi: f64,
}

/// The stations of zone z a join (z giving way to g at one of z's ends) holds over: from that end along z for as long
/// as z's line stays within the two half widths + blend of g's line (a connector merging alongside the oval for 100 m
/// gives way all along its overlap, not only within a radius of its end: a seam where the radius ended, r70), the
/// two lines alone deciding it.
#[allow(clippy::too_many_arguments)]
pub fn reach_of(zl: &[f64], zk: usize, zds: f64, zs_end: f64, zhw: f64, gl: &[f64], gk: usize, ghw: f64, jx: f64,
                jy: f64, blend: f64) -> (f64, f64) {
    let k = zk;
    let d0 = sq(zl[0] - jx) + sq(zl[1] - jy);
    let d1 = sq(zl[2 * (k - 1)] - jx) + sq(zl[2 * (k - 1) + 1] - jy);
    let reach = zhw + ghw + blend;
    // (each sample projected as the walk reaches it, the walk stopping at the first out of reach: a short overlap
    // costs a few projections, not the whole line's; Codex review of d40a5fcb)
    let near = |i: usize| polyproject::project(gl, 2, gk, &zl[2 * i..2 * i + 2], 2, 1).dist[0] <= reach;
    let at = |i: usize| if i + 1 == k { zs_end } else { i as f64 * zds };
    if d0 <= d1 {
        let mut i = 0;
        while i + 1 < k && near(i + 1) {
            i += 1;
        }
        (0.0, at(i) + zds)
    } else {
        let mut i = k - 1;
        while i > 0 && near(i - 1) {
            i -= 1;
        }
        (at(i) - zds, zs_end)
    }
}

fn join_reach(z: &Zone, g: &Zone, j: &Join, blend: f64) -> (f64, f64) {
    if !j.lo.is_nan() && !j.hi.is_nan() {
        return (j.lo, j.hi);
    }
    reach_of(&z.line, z.k, z.ds, z.s_end, z.hw, &g.line, g.k, g.hw, j.x, j.y, blend)
}

/// (surface (m,), covered (m,)): base at the points pts (m x 2) as the raceways' surfaces (module doc). joins: per
/// zone the joins where it gives way (`dominance`); None: the old nearest-line rule (joins and blend unused). A join
/// holds only near itself: at the points of the giving way zone along its overlap with the other from the join's end
/// (join_reach) and within blend of the other's band (every input a pure function of the two lines: the same answer
/// whatever else the planner has loaded, Codex review of 98a65723).
pub fn ground(pts: &[f64], base: &[f64], zones: &[Zone], joins: Option<&[Vec<Join>]>, margin: f64, blend: f64)
              -> (Vec<f64>, Vec<bool>) {
    let m = base.len();
    let mut out = base.to_vec();
    let mut covered = vec![false; m];
    if m == 0 || zones.is_empty() {
        return (out, covered);
    }
    let ext = extents(pts);
    let nz = zones.len();
    let mut proj: Vec<Option<Proj>> = (0..nz).map(|_| None).collect();
    let fade = pymax(margin, 1e-9);
    let skip: Vec<bool> = zones.iter().map(|z| z.misses(z.hw, ext)).collect();
    for zi in 0..nz {
        if !skip[zi] {
            proj[zi] = Some(project_all(&zones[zi], pts));
        }
    }
    // per zone that gives way: at each point, the join that holds there (its dominant zone, the dominant's surface at
    // its band's edge nearest the point, how far out of that band) or none
    let mut active: Vec<Vec<Option<(f64, f64)>>> = vec![Vec::new(); nz];
    if let Some(js) = joins {
        if blend > 0.0 {
            for zi in 0..nz {
                if skip[zi] || js[zi].is_empty() {
                    continue;
                }
                for j in &js[zi] {
                    let gz = &zones[j.by];
                    if proj[j.by].is_none() && !gz.misses(gz.hw + blend, ext) {
                        proj[j.by] = Some(project_all(gz, pts));
                    }
                }
                let z = &zones[zi];
                let p = proj[zi].as_ref().unwrap();
                let mut act = vec![None; m];
                for j in &js[zi] {
                    let g = j.by;
                    let gz = &zones[g];
                    if gz.misses(gz.hw + blend, ext) {
                        continue;
                    }
                    let gp = proj[g].as_ref().unwrap();
                    let (lo, hi) = join_reach(z, gz, j, blend);
                    for i in 0..m {
                        let (st, off) = (p.station[i], p.offset[i]);
                        if !(st >= 0.0 && st <= z.s_end && off.abs() <= z.hw && st >= lo && st <= hi) {
                            continue;
                        }
                        let (s2, o2) = (gp.station[i], gp.offset[i]);
                        let dd = o2.abs() - gz.hw;
                        if !(s2 >= 0.0 && s2 <= gz.s_end && dd <= blend) {
                            continue;
                        }
                        if act[i].is_none_or(|(b, _): (f64, f64)| dd < b) {
                            act[i] = Some((dd, gz.at(s2, o2.clamp(-gz.hw, gz.hw))));
                        }
                    }
                }
                active[zi] = act;
            }
        }
    }
    let on = |zi: usize, i: usize| -> Option<(f64, f64)> { active[zi].get(i).copied().flatten() };
    // the zones giving way where their joins hold first, then every zone elsewhere over them
    for layer in 0..2 {
        if layer == 0 && active.iter().all(|a| a.is_empty()) {
            continue;
        }
        let mut best = vec![f64::INFINITY; m];
        let mut add = vec![0.0f64; m];
        for zi in 0..nz {
            if skip[zi] || (layer == 0 && active[zi].is_empty()) {
                continue;
            }
            let z = &zones[zi];
            let p = proj[zi].as_ref().unwrap();
            for i in 0..m {
                let held = on(zi, i);
                if (layer == 0) != held.is_some() {
                    continue;
                }
                let (st, off, dist) = (p.station[i], p.offset[i], p.dist[i]);
                if !(st >= 0.0 && st <= z.s_end && off.abs() <= z.hw && dist < best[i]) {
                    continue;
                }
                let mut c = z.at(st, off);
                if let Some((bd, zt)) = held {
                    let w = smooth(npclip(1.0 - bd.max(0.0) / blend, 0.0, 1.0));
                    c += (zt - c) * w;
                }
                let u = npclip((z.hw - off.abs()) / fade, 0.0, 1.0);
                add[i] = (c - out[i]) * u * u * (3.0 - 2.0 * u);
                best[i] = dist;
                covered[i] = true;
            }
        }
        for i in 0..m {
            out[i] += add[i];
        }
    }
    (out, covered)
}

/// m: raceway ends this near each other meet at one node (the map's shared node)
const NODE_M: f64 = 0.5;

/// Per raceway line (lines[i]: k x 2 flattened, its band's half width hw[i]), the joins where it gives way (module
/// doc): an end of it on another's band (station on it, |offset| within its half width) where the other passes
/// through (a closed loop, or the end more than its half width from the other's ends). Where the other ends there too,
/// the node decides: two ends meeting there that go on from each other (directions within ~37 degrees of opposite) are
/// one through line, never giving way to a third ending there, which gives way to both of them (an oval split at its
/// pit entry: Codex review of 80e9b3da, its two short halves gave way to a longer pit lane by length); with no through
/// line there, the wider, then the longer. Every line ending at that node must be among `lines` (the caller's
/// neighbourhood of the junction is complete: grading.raceway_lines).
pub fn dominance(lines: &[Vec<f64>], hw: &[f64]) -> Vec<Vec<Join>> {
    let nz = lines.len();
    let k: Vec<usize> = lines.iter().map(|l| l.len() / 2).collect();
    let closed: Vec<bool> = (0..nz).map(|z| {
        k[z] > 3 && {
            let l = &lines[z];
            let (dx, dy) = (l[0] - l[2 * (k[z] - 1)], l[1] - l[2 * (k[z] - 1) + 1]);
            (dx * dx + dy * dy).sqrt() < 1.0
        }
    }).collect();
    let length: Vec<f64> = (0..nz).map(|z| {
        let l = &lines[z];
        (1..k[z]).map(|p| {
            let (dx, dy) = (l[2 * p] - l[2 * p - 2], l[2 * p + 1] - l[2 * p - 1]);
            (dx * dx + dy * dy).sqrt()
        }).sum()
    }).collect();
    // the unit direction out of line z's end nearest p (over up to 3 segments)
    let outdir = |z: usize, px: f64, py: f64| -> (f64, f64) {
        let l = &lines[z];
        let n = k[z];
        let d0 = (l[0] - px) * (l[0] - px) + (l[1] - py) * (l[1] - py);
        let (lx, ly) = (l[2 * (n - 1)], l[2 * (n - 1) + 1]);
        let d1 = (lx - px) * (lx - px) + (ly - py) * (ly - py);
        let (a, b) = if d0 <= d1 { (0, 3.min(n - 1)) } else { (n - 1, n.saturating_sub(4)) };
        let (vx, vy) = (l[2 * a] - l[2 * b], l[2 * a + 1] - l[2 * b + 1]);
        let len = (vx * vx + vy * vy).sqrt().max(1e-9);
        (vx / len, vy / len)
    };
    // does line z end within NODE_M of p
    let ends_at = |z: usize, px: f64, py: f64| -> bool {
        let l = &lines[z];
        let n = k[z];
        !closed[z] && [0, n - 1].iter().any(|&e| (sq(l[2 * e] - px) + sq(l[2 * e + 1] - py)).sqrt() <= NODE_M)
    };
    let going_on = |a: (f64, f64), b: (f64, f64)| a.0 * b.0 + a.1 * b.1 < -0.8;
    let mut out: Vec<Vec<Join>> = vec![Vec::new(); nz];
    for b in 0..nz {
        if k[b] < 2 || closed[b] {
            continue;
        }
        let lb = &lines[b];
        for e in [0usize, k[b] - 1] {
            let (px, py) = (lb[2 * e], lb[2 * e + 1]);
            // the lines ending at this node and their directions out of it (b among them)
            let at_node: Vec<usize> = (0..nz).filter(|&z| k[z] >= 2 && ends_at(z, px, py)).collect();
            let dir: Vec<(f64, f64)> = at_node.iter().map(|&z| outdir(z, px, py)).collect();
            // the node's through lines: its pairs of ends going on from each other, the straightest first, each end in
            // one pair at most (a pit lane leaving 15 degrees off the oval's forward half goes on from the backward
            // half too, but that half already goes on straighter with the forward one: Codex review of b68ade87)
            let mut cand: Vec<(f64, usize, usize)> = Vec::new();
            for i in 0..at_node.len() {
                for j in i + 1..at_node.len() {
                    let c = dir[i].0 * dir[j].0 + dir[i].1 * dir[j].1;
                    if c < -0.8 {
                        cand.push((c, i, j));
                    }
                }
            }
            cand.sort_by(|p, q| p.0.total_cmp(&q.0).then(p.1.cmp(&q.1)).then(p.2.cmp(&q.2)));
            let mut mate: Vec<Option<usize>> = vec![None; at_node.len()];
            for &(_c, i, j) in &cand {
                if mate[i].is_none() && mate[j].is_none() {
                    mate[i] = Some(j);
                    mate[j] = Some(i);
                }
            }
            let pos = |z: usize| at_node.iter().position(|&q| q == z);
            let through = |z: usize| pos(z).is_some_and(|i| mate[i].is_some());
            let paired = |a: usize, b: usize| match (pos(a), pos(b)) {
                (Some(i), Some(j)) => mate[i] == Some(j),
                _ => false,
            };
            let db = outdir(b, px, py);
            for a in 0..nz {
                if a == b || k[a] < 2 {
                    continue;
                }
                let r = polyproject::project(&lines[a], 2, k[a], &[px, py], 2, 1);
                let (s0, o0) = (r.station[0], r.offset[0]);
                if !(s0 >= 0.0 && s0 <= length[a] && o0.abs() <= hw[a]) {
                    continue;
                }
                let gives = if closed[a] || (s0 > hw[a] && s0 < length[a] - hw[a]) {
                    true
                } else if paired(a, b) || (!ends_at(a, px, py) && going_on(outdir(a, px, py), db)) {
                    false
                } else if through(b) {
                    false
                } else if through(a) {
                    true
                } else {
                    hw[a] > hw[b] || (hw[a] == hw[b] && length[a] > length[b])
                };
                if gives {
                    out[b].push(Join { by: a, x: px, y: py, lo: f64::NAN, hi: f64::NAN });
                }
            }
        }
    }
    out
}

type ZoneArgs<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray2<'py, f64>, f64, f64, f64, f64, f64);

fn zones_of(zs: &[ZoneArgs<'_>]) -> PyResult<Vec<Zone>> {
    zs.iter().map(|(line, surf, ds, s_end, o0, d_o, hw)| {
        let (la, sa) = (line.as_array(), surf.as_array());
        let (k, nt) = (sa.nrows(), sa.ncols());
        if la.nrows() != k || la.ncols() < 2 || k < 2 || nt < 2 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "raceway_ground: a zone's line rows as its surface's, 2 or more, 2 offsets or more"));
        }
        Ok(Zone {
            line: (0..k).flat_map(|r| [la[[r, 0]], la[[r, 1]]]).collect(),
            k,
            ds: *ds,
            s_end: *s_end,
            o0: *o0,
            d_o: *d_o,
            hw: *hw,
            nt,
            surf: sa.iter().copied().collect(),
        })
    }).collect()
}

/// grading.raceway_ground: (surface (m,), covered (m,) bool) of points (e, n) with base (m,) and the zones [(line (k,
/// 2), surface (k, nt), stations[1] - stations[0], stations[-1], offsets[0], offsets[1] - offsets[0], offsets[-1])];
/// joins None: the nearest-line rule, else per zone [(the zone it gives way to, the join's x, y, the stations it holds
/// over or NaN, NaN: join_reach)] (blend m).
#[pyfunction]
#[pyo3(signature = (e, n, base, zones, margin, joins=None, blend=0.0))]
#[allow(clippy::type_complexity)]
pub fn raceway_ground<'py>(py: Python<'py>, e: PyReadonlyArray1<'py, f64>, n: PyReadonlyArray1<'py, f64>,
                           base: PyReadonlyArray1<'py, f64>, zones: Vec<ZoneArgs<'py>>, margin: f64,
                           joins: Option<Vec<Vec<(usize, f64, f64, f64, f64)>>>, blend: f64)
                           -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<bool>>)> {
    let (ea, na, ba) = (e.as_array(), n.as_array(), base.as_array());
    let m = ba.len();
    if ea.len() != m || na.len() != m {
        return Err(pyo3::exceptions::PyValueError::new_err("raceway_ground: e, n and base of one length"));
    }
    let zs = zones_of(&zones)?;
    let js: Option<Vec<Vec<Join>>> = joins.map(|v| v.into_iter()
        .map(|l| l.into_iter().map(|(by, x, y, lo, hi)| Join { by, x, y, lo, hi }).collect()).collect());
    if let Some(j) = &js {
        if j.len() != zs.len() || j.iter().flatten().any(|q| q.by >= zs.len()) {
            return Err(pyo3::exceptions::PyValueError::new_err("raceway_ground: joins per zone, zone indices"));
        }
    }
    let pts: Vec<f64> = (0..m).flat_map(|i| [ea[i], na[i]]).collect();
    let bv: Vec<f64> = ba.iter().copied().collect();
    let (out, cov) = py.detach(|| ground(&pts, &bv, &zs, js.as_deref(), margin, blend));
    Ok((out.into_pyarray(py), cov.into_pyarray(py)))
}

/// grading.raceway_dominance: per line [(line (k, 2), band half width)] its joins [(the line it gives way to, x, y)].
#[pyfunction]
pub fn raceway_dominance(py: Python<'_>, lines: Vec<(PyReadonlyArray2<'_, f64>, f64)>)
                         -> PyResult<Vec<Vec<(usize, f64, f64)>>> {
    let mut ls = Vec::with_capacity(lines.len());
    let mut hw = Vec::with_capacity(lines.len());
    for (l, h) in &lines {
        let a = l.as_array();
        if a.ncols() < 2 {
            return Err(pyo3::exceptions::PyValueError::new_err("raceway_dominance: lines of xy rows"));
        }
        ls.push((0..a.nrows()).flat_map(|r| [a[[r, 0]], a[[r, 1]]]).collect::<Vec<f64>>());
        hw.push(*h);
    }
    Ok(py.detach(|| dominance(&ls, &hw)).into_iter()
        .map(|v| v.into_iter().map(|j| (j.by, j.x, j.y)).collect()).collect())
}

/// racedig.track_surface: the stations of line z (k x 2, resampled every ds, s_end its length, band half width zhw)
/// a join at (jx, jy) on line g (band half width ghw) holds over (join_reach), on the whole lines.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn raceway_join_reach(z: PyReadonlyArray2<'_, f64>, zds: f64, zs_end: f64, zhw: f64, g: PyReadonlyArray2<'_, f64>,
                          ghw: f64, jx: f64, jy: f64, blend: f64) -> PyResult<(f64, f64)> {
    let (za, ga) = (z.as_array(), g.as_array());
    if za.nrows() < 2 || ga.nrows() < 2 || za.ncols() < 2 || ga.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("raceway_join_reach: lines of 2 xy rows or more"));
    }
    let zl: Vec<f64> = (0..za.nrows()).flat_map(|r| [za[[r, 0]], za[[r, 1]]]).collect();
    let gl: Vec<f64> = (0..ga.nrows()).flat_map(|r| [ga[[r, 0]], ga[[r, 1]]]).collect();
    Ok(reach_of(&zl, za.nrows(), zds, zs_end, zhw, &gl, ga.nrows(), ghw, jx, jy, blend))
}

#[cfg(test)]
mod tests {
    #[test]
    #[allow(clippy::disallowed_methods)]    // (the old expression, compared once)
    fn racejoin_sq_is_powi() {
        let mut x: u64 = 0x9e37_79b9_7f4a_7c15;
        for _ in 0..200_000 {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            let v = f64::from_bits(x);
            let w = (x as f64 / u64::MAX as f64 - 0.5) * 2e4;
            for t in [v, w, -w, 0.0, -0.0, f64::MIN_POSITIVE, 1e-300, 1e300, f64::INFINITY, f64::NAN] {
                let (a, b) = (t.powi(2), super::sq(t));
                assert!(a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan()), "{t:e}");
            }
        }
    }

    use super::*;

    /// A straight raceway along x (y = y0), half width hw, its surface f(station, offset).
    fn zone_x(x0: f64, x1: f64, y0: f64, hw: f64, f: impl Fn(f64, f64) -> f64) -> Zone {
        let k = ((x1 - x0) / 2.0).round() as usize + 1;
        let nt = (2.0 * hw).ceil() as usize + 1;
        let line: Vec<f64> = (0..k).flat_map(|i| [x0 + 2.0 * i as f64, y0]).collect();
        let offs: Vec<f64> = (0..nt).map(|j| -hw + 2.0 * hw * j as f64 / (nt - 1) as f64).collect();
        let surf: Vec<f64> = (0..k).flat_map(|i| offs.iter().map(move |&o| (i, o)).collect::<Vec<_>>())
            .map(|(i, o)| f(2.0 * i as f64, o)).collect();
        Zone { line, k, ds: 2.0, s_end: 2.0 * (k - 1) as f64, o0: offs[0], d_o: offs[1] - offs[0], hw, nt, surf }
    }

    /// A straight raceway along y from (x, y0) to (x, y1).
    fn zone_y(x: f64, y0: f64, y1: f64, hw: f64, h: f64) -> Zone {
        let mut z = zone_x(0.0, (y1 - y0).abs(), 0.0, hw, |_, _| h);
        let dir = if y1 >= y0 { 1.0 } else { -1.0 };
        for i in 0..z.k {
            let s = z.line[2 * i];
            z.line[2 * i] = x;
            z.line[2 * i + 1] = y0 + dir * s;
        }
        z
    }

    #[test]
    fn dominant_kept_minor_meets_its_edge() {
        // the oval along x, banked (0.1 m per m across); a pit road along y ending on its centre line, 1 m lower
        let oval = zone_x(-100.0, 100.0, 0.0, 9.0, |_, o| 0.1 * o);
        let pit = zone_y(0.0, 60.0, 0.0, 9.0, -1.0);
        let zones = vec![oval, pit];
        let dom = dominance(&zones.iter().map(|z| z.line.clone()).collect::<Vec<_>>(), &[9.0, 9.0]);
        assert!(dom[0].is_empty());
        assert_eq!(dom[1].iter().map(|j| (j.by, j.x, j.y)).collect::<Vec<_>>(), vec![(0, 0.0, 0.0)]);
        let pts: Vec<f64> = (0..=60).flat_map(|i| [0.0, i as f64]).collect();
        let base = vec![5.0; 61];
        let (old, _) = ground(&pts, &base, &zones, None, 2.0, 20.0);
        let (new, cov) = ground(&pts, &base, &zones, Some(&dom), 2.0, 20.0);
        assert!(cov.iter().all(|&c| c));
        // on the oval's carriageway (|y| <= 7): the oval's surface (0.1 y), whatever the pit road
        for y in 0..=7 {
            assert!((new[y] - 0.1 * y as f64).abs() < 1e-9, "y {y}: {}", new[y]);
        }
        // the old rule: the pit road's -1 from half way (y >= 5)
        assert!((old[6] - (-1.0)).abs() < 1e-9);
        // the pit road meets the oval's edge height (0.9) and eases to its own over 20 m
        assert!((new[9] - 0.9).abs() < 1e-9);
        assert!((new[40] - (-1.0)).abs() < 1e-9);
        for w in new.windows(2).skip(9) {
            assert!((w[1] - w[0]).abs() < 0.2, "{:?}", w);
        }
    }

    #[test]
    fn a_join_holds_only_along_its_overlap() {
        // a road that runs 60 m off the oval, bends in and joins it: away from the oval (past its overlap from the
        // join) nothing changes; a point where both bands meet only by the old rule's crossing far from the join
        // keeps the old answer
        let oval = zone_x(-200.0, 200.0, 0.0, 9.0, |_, o| 0.1 * o);
        let road = zone_y(-150.0, 60.0, 0.0, 9.0, -1.0);       // (x = -150, joining the oval at (-150, 0))
        let lines: Vec<Vec<f64>> = vec![oval.line.clone(), road.line.clone()];
        let dom = dominance(&lines, &[9.0, 9.0]);
        assert_eq!(dom[1].iter().map(|j| (j.by, j.x, j.y)).collect::<Vec<_>>(), vec![(0, -150.0, 0.0)]);
        let (lo, hi) = join_reach(&road, &oval, &dom[1][0], 20.0);
        assert!(lo >= 60.0 - 38.0 - 2.0 - 1e-9 && hi >= 60.0, "{lo} {hi}");
        let zones = vec![oval, road];
        // 45 m up the road (past its overlap): as the old rule
        let pts = [-150.0, 45.0];
        let (a, _) = ground(&pts, &[5.0], &zones, None, 2.0, 20.0);
        let (b, _) = ground(&pts, &[5.0], &zones, Some(&dom), 2.0, 20.0);
        assert_eq!(a[0].to_bits(), b[0].to_bits());
    }

    #[test]
    fn split_through_route_and_continuations() {
        // the oval split at x = 0 into two ways meeting end to end (the longer one 100 m, the other 60 m), a pit road
        // ending there from the side: it gives way to both; the two oval pieces go on from each other
        let a: Vec<f64> = (0..=50).flat_map(|i| [-100.0 + 2.0 * i as f64, 0.0]).collect();
        let b: Vec<f64> = (0..=30).flat_map(|i| [2.0 * i as f64, 0.0]).collect();
        let c: Vec<f64> = (0..=20).flat_map(|i| [0.0, 40.0 - 2.0 * i as f64]).collect();
        let dom = dominance(&[a.clone(), b.clone(), c.clone()], &[9.0, 9.0, 9.0]);
        assert!(dom[0].is_empty() && dom[1].is_empty(), "{dom:?}");
        assert_eq!(dom[2].iter().map(|j| j.by).collect::<Vec<_>>(), vec![0, 1]);
        // two ways end to end alone: no one gives way
        assert!(dominance(&[a, b], &[9.0, 9.0]).iter().all(|v| v.is_empty()));
        // a road crossing the band away from its ends without ending there: no join
        let d: Vec<f64> = (0..=40).flat_map(|i| [10.0, -40.0 + 2.0 * i as f64]).collect();
        let e: Vec<f64> = (0..=50).flat_map(|i| [-50.0 + 2.0 * i as f64, 0.0]).collect();
        assert!(dominance(&[e, d], &[9.0, 9.0]).iter().all(|v| v.is_empty()));
    }

    #[test]
    fn a_split_oval_never_gives_way_to_a_longer_pit_lane() {
        // Codex review of 80e9b3da: the oval split at the pit entry into two short halves (60 m each), the pit lane
        // 100 m, all one width: by length both halves gave way to the pit lane. They go on from each other: one
        // through line; the pit lane gives way to both
        let a: Vec<f64> = (0..=30).flat_map(|i| [-60.0 + 2.0 * i as f64, 0.0]).collect();
        let b: Vec<f64> = (0..=30).flat_map(|i| [2.0 * i as f64, 0.0]).collect();
        let c: Vec<f64> = (0..=50).flat_map(|i| [0.0, -100.0 + 2.0 * i as f64]).collect();
        let dom = dominance(&[a, b, c], &[9.0, 9.0, 9.0]);
        assert!(dom[0].is_empty() && dom[1].is_empty(), "{dom:?}");
        assert_eq!(dom[2].iter().map(|j| j.by).collect::<Vec<_>>(), vec![0, 1]);
        // a fork of two branches at an angle, neither going on from the trunk: the trunk is the through line? no:
        // nothing goes on straight there, so the widths and lengths decide
        let t: Vec<f64> = (0..=50).flat_map(|i| [-100.0 + 2.0 * i as f64, 0.0]).collect();
        let l: Vec<f64> = (0..=20).flat_map(|i| [1.4 * i as f64, 1.4 * i as f64]).collect();
        let r: Vec<f64> = (0..=20).flat_map(|i| [1.4 * i as f64, -1.4 * i as f64]).collect();
        let dom = dominance(&[t, l, r], &[9.0, 9.0, 9.0]);
        assert!(dom[0].is_empty(), "{dom:?}");
    }

    #[test]
    fn a_shallow_pit_fork_gives_way_to_the_oval() {
        // Codex review of b68ade87: the pit lane leaves 15 degrees off the forward half: it goes on from the backward
        // half nearly as straight as the forward half does; the straightest pair is the through line, the pit lane
        // gives way to both halves
        let a: Vec<f64> = (0..=30).flat_map(|i| [-60.0 + 2.0 * i as f64, 0.0]).collect();
        let b: Vec<f64> = (0..=30).flat_map(|i| [2.0 * i as f64, 0.0]).collect();
        let (c15, s15) = (0.965_925_826_289_068_3_f64, 0.258_819_045_102_520_7_f64); // cos, sin 15 degrees (no platform maths)
        let c: Vec<f64> = (0..=50).flat_map(|i| [2.0 * i as f64 * c15, -2.0 * i as f64 * s15]).collect();
        let dom = dominance(&[a, b, c], &[9.0, 9.0, 9.0]);
        assert!(dom[0].is_empty() && dom[1].is_empty(), "{dom:?}");
        assert_eq!(dom[2].iter().map(|j| j.by).collect::<Vec<_>>(), vec![0, 1]);
    }

    #[test]
    fn a_window_keeps_its_whole_line_reach() {
        // a join's reach measured on the whole line and given to a window of it holds there as given
        let oval = zone_x(-200.0, 200.0, 0.0, 9.0, |_, o| 0.1 * o);
        let road = zone_y(-150.0, 60.0, 0.0, 9.0, -1.0);
        let js = vec![vec![], vec![Join { by: 0, x: -150.0, y: 0.0, lo: 1000.0, hi: 2000.0 }]];
        // (an interval off this window: the join does not hold here, the old rule's answer)
        let pts = [-150.0, 12.0];
        let zones = vec![oval, road];
        let (a, _) = ground(&pts, &[5.0], &zones, None, 2.0, 20.0);
        let (b, _) = ground(&pts, &[5.0], &zones, Some(&js), 2.0, 20.0);
        assert_eq!(a[0].to_bits(), b[0].to_bits());
    }

    #[test]
    fn nearest_rule_without_joins() {
        let z = zone_x(0.0, 20.0, 0.0, 5.0, |s, o| s + 10.0 * o);
        let pts = [10.0, 1.0, 10.0, 4.5, 10.0, 6.0, 30.0, 0.0];
        let base = [100.0; 4];
        let (out, cov) = ground(&pts, &base, &[z], None, 1.0, 0.0);
        assert!((out[0] - 20.0).abs() < 1e-9);
        // in the fade (0.5 m from the edge): half way by smoothstep
        assert!((out[1] - (55.0 + (100.0 - 55.0) * 0.5)).abs() < 1e-9);
        assert_eq!(out[2], 100.0);
        assert_eq!(out[3], 100.0);
        assert_eq!(cov, vec![true, true, false, false]);
    }
}
