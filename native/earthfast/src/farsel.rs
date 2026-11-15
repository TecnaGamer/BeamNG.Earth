//! The far field's per-pose selection kernels (earthd's event loop asks them on every pose; in Python they were the
//! loop's largest share of a drive's poses, several times slower still while ~20 threads of the server competed for the
//! interpreter: 2-7 s stalls in a BeamMP session at Baltimore, 2026-11-15):
//!
//! - far_select_nodes: farfield.select, the anchor's own quadtree (the distance rule with hysteresis and holds, the
//!   forced boxes, the near radii, the disc or square radius, the line-of-sight horizon cull, the screen-space LOD, the
//!   3D map's view cone, and with global far tiles the region the anchor's nodes fill: a node across its edge cut into
//!   one piece per global tile it overlaps). The same traversal order as the Python it replaces (the tiles come out in
//!   the order it found them).
//! - far_horizon_radius: farfield.HorizonGrid.radius, the far radius to the line-of-sight horizon over the zmax grid.
use crate::dm::Dm;
use crate::farglobal::{clip_convex, poly_area, tighten, SLACK_PAD};

use numpy::PyReadonlyArray2;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashSet;

type Node = (i32, i64, i64);

fn level_size(level: i32) -> f64 {
    256.0 * 2f64.powi(level)
}

/// Python's max(a, b, ...) of floats (the first largest; NaN never wins over an earlier value).
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// The horizontal distance from (x, y) to the box [x0, x1] x [y0, y1] (farfield: math.hypot of the gaps).
fn box_dist(x0: f64, y0: f64, x1: f64, y1: f64, x: f64, y: f64) -> f64 {
    pmax(pmax(x0 - x, 0.0), x - x1).dhypot(pmax(pmax(y0 - y, 0.0), y - y1))
}

/// farfield.switch_distance with K(L) = ks[L] (levels >= 1).
fn switch_distance(level: i32, ks: &[f64], radii: &[(i32, f64)], min_level: i32) -> f64 {
    if level <= min_level {
        return 0.0;
    }
    let mut d = if level >= 1 { ks.get(level as usize).copied().unwrap_or(0.0) * level_size(level) } else { 0.0 };
    for &(m, r) in radii {
        if min_level <= m && m < level {
            d = pmax(d, r);
        }
    }
    d
}

/// The zmax pyramid of farfield.HorizonGrid (base: the grid level's cells, [j, i] for node (i - n/2, j - n/2); each
/// level above the 2 x 2 maximum, NaN spreading as numpy's max does) and the cull's reach (farfield.Horizon).
struct Horizon {
    reach: f64,
    min_d: f64,
    r: f64,
    grid_level: i32,
    levels: Vec<(usize, Vec<f64>)>,     // (m, [jj * m + ii]) from grid_level up
}

fn nanmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}

impl Horizon {
    fn new(reach: f64, min_d: f64, r: f64, base: &numpy::ndarray::ArrayView2<'_, f64>, grid_level: i32,
           max_level: i32) -> PyResult<Horizon> {
        let n = base.nrows();
        if base.ncols() != n {
            return Err(PyValueError::new_err("far_select_nodes: a square zmax grid"));
        }
        let mut levels = vec![(n, base.iter().copied().collect::<Vec<f64>>())];
        for _ in grid_level + 1..=max_level {
            let (m0, a) = levels.last().unwrap();
            let m = m0 / 2;
            let mut b = vec![0.0; m * m];
            for j in 0..m {
                for i in 0..m {
                    let v = nanmax(nanmax(a[2 * j * m0 + 2 * i], a[2 * j * m0 + 2 * i + 1]),
                                   nanmax(a[(2 * j + 1) * m0 + 2 * i], a[(2 * j + 1) * m0 + 2 * i + 1]));
                    b[j * m + i] = v;
                }
            }
            levels.push((m, b));
        }
        Ok(Horizon { reach, min_d, r, grid_level, levels })
    }

    /// HorizonGrid.zmax(level, i, j): a finer node takes its grid-level ancestor's; outside the grid +inf.
    fn zmax(&self, mut level: i32, mut i: i64, mut j: i64) -> f64 {
        if level < self.grid_level {
            let k = self.grid_level - level;
            level = self.grid_level;
            i >>= k;
            j >>= k;
        }
        let Some((m, a)) = self.levels.get((level - self.grid_level) as usize) else { return f64::INFINITY };
        let m = *m as i64;
        let (ii, jj) = (i + m / 2, j + m / 2);
        if 0 <= ii && ii < m && 0 <= jj && jj < m { a[(jj * m + ii) as usize] } else { f64::INFINITY }
    }

    fn keeps(&self, level: i32, i: i64, j: i64, d: f64) -> bool {
        if d <= self.min_d {
            return true;
        }
        d <= self.reach + (2.0 * self.r * pmax(0.0, self.zmax(level, i, j))).sqrt()
    }

    /// How far d can move before keeps' answer may change.
    fn keeps_slack(&self, level: i32, i: i64, j: i64, d: f64) -> f64 {
        let t = self.reach + (2.0 * self.r * pmax(0.0, self.zmax(level, i, j))).sqrt();
        let s = (d - self.min_d).abs();
        if t.is_finite() { s.min((d - t).abs()) } else { s }
    }
}

/// farfield.out_of_view: the factor a node's distance counts with outside the 3D map's view cone.
fn out_of_view(c: &[f64], x0: f64, y0: f64, x1: f64, y1: f64, d: f64) -> f64 {
    if c.len() < 9 || d <= 0.0 {
        return 1.0;
    }
    let (cx, cy, cz) = ((x0 + x1) / 2.0 - c[0], (y0 + y1) / 2.0 - c[1], c[7] - c[2]);
    let r = (cx * cx + cy * cy + cz * cz).sqrt();
    let slack = pmin(1.0, (x1 - x0).dhypot(y1 - y0) * 0.5 / pmax(r, 1.0)).dasin();
    let a = pmax(-1.0, pmin(1.0, (cx * c[3] + cy * c[4] + cz * c[5]) / pmax(r, 1e-9))).dacos();
    if a - slack <= pmax(-1.0, pmin(1.0, c[6])).dacos() { 1.0 } else { pmax(1.0, c[8]) }
}

/// The screen-space LOD camera (farfield.View). The built tiles' normal errors: errs a dict {(aid, (level, i, j)):
/// err} (earthd's lod_errs, aid the anchor's id) looked up node by node, or a callable err((level, i, j)) -> err or None.
struct View<'a, 'py> {
    cx: f64,
    cy: f64,
    dz: f64,
    ppr: f64,
    tau: f64,
    reach: f64,
    hs: f64,
    cap: f64,
    cells: f64,
    errs: &'a Bound<'py, PyAny>,
    aid: &'a Bound<'py, PyAny>,
}

impl View<'_, '_> {
    fn err(&self, node: Node) -> PyResult<Option<f64>> {
        let got = match self.errs.cast::<PyDict>() {
            Ok(d) if self.aid.is_none() => d.get_item(node)?,
            Ok(d) => d.get_item((self.aid, node))?,
            Err(_) => Some(self.errs.call1((node,))?),
        };
        match got {
            Some(v) if !v.is_none() => Ok(Some(v.extract::<f64>()?)),
            _ => Ok(None),
        }
    }

    fn pixels(&self, node: Node, x0: f64, y0: f64, x1: f64, y1: f64) -> PyResult<Option<f64>> {
        let Some(e) = self.err(node)? else { return Ok(None) };
        let e = pmin(e, self.cap * (x1 - x0) / self.cells);
        let dxy = pmax(pmax(x0 - self.cx, 0.0), self.cx - x1).dhypot(pmax(pmax(y0 - self.cy, 0.0), self.cy - y1));
        Ok(Some(e * self.ppr / pmax(dxy.dhypot(self.dz), 1.0)))
    }

    #[allow(clippy::too_many_arguments)]
    fn decide(&self, split: bool, was: bool, node: Node, b: [f64; 4], d: f64, d3: f64, lim: f64, lim_f: f64, h: f64)
              -> PyResult<bool> {
        let Some(p) = self.pixels(node, b[0], b[1], b[2], b[3])? else { return Ok(split) };
        let want = p > self.tau || (was && p > self.tau / (1.0 + self.hs));
        Ok(if split && !want {
            d3 < lim_f || (was && d3 < (1.0 + h) * lim_f)
        } else if want && !split {
            d < self.reach * lim
        } else {
            split
        })
    }

    /// How far the camera and the player can move before decide's answer may change (INF: the error not known).
    #[allow(clippy::too_many_arguments)]
    fn slack(&self, was: bool, node: Node, b: [f64; 4], d: f64, d3: f64, lim: f64, lim_f: f64, h: f64)
             -> PyResult<f64> {
        let Some(e) = self.err(node)? else { return Ok(f64::INFINITY) };
        let e = pmin(e, self.cap * (b[2] - b[0]) / self.cells);
        let dxy = pmax(pmax(b[0] - self.cx, 0.0), self.cx - b[2]).dhypot(pmax(pmax(b[1] - self.cy, 0.0), self.cy - b[3]));
        let dm = pmax(dxy.dhypot(self.dz), 1.0);
        let d1 = e * self.ppr / self.tau;
        let mut s = f64::INFINITY;
        for v in [dm - d1, dm - d1 * (1.0 + self.hs), d - self.reach * lim, d3 - lim_f] {
            tighten(&mut s, v);
        }
        if was {
            tighten(&mut s, d3 - (1.0 + h) * lim_f);
        }
        Ok(s)
    }
}

type P2 = [f64; 2];

/// The region (farfield.Region): the refined finest global tiles' quads in the anchor's world, and their bbox.
struct Region {
    quads: Vec<Vec<P2>>,
    qb: Vec<[f64; 4]>,
    bbox: [f64; 4],
    min_area: f64,
}

enum Cover {
    Inside,
    Outside,
    Across(Vec<(usize, Vec<f64>)>),
}

impl Region {
    /// Region.cover: inside the region, outside it, or across its edge (its overlap with each quad).
    fn cover(&self, x0: f64, y0: f64, x1: f64, y1: f64) -> Cover {
        let sq = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
        let mut got = Vec::new();
        let mut sum = 0.0;
        for (i, q) in self.quads.iter().enumerate() {
            let b = &self.qb[i];
            if !(b[0] <= x1 && x0 <= b[2] && b[1] <= y1 && y0 <= b[3]) {
                continue;
            }
            let p = clip_convex(&sq, q);
            let a = poly_area(&p).abs();
            if a > self.min_area {
                sum += a;
                got.push((i, p.iter().flatten().copied().collect::<Vec<f64>>()));
            }
        }
        let a = (x1 - x0) * (y1 - y0);
        if sum >= a - pmax(1e-7 * a, self.min_area * (1 + got.len()) as f64) {
            Cover::Inside
        } else if got.is_empty() {
            Cover::Outside
        } else {
            Cover::Across(got)
        }
    }
}

/// farfield.select's traversal for a player at (px, py): [(level, i, j, piece: -1 for a whole node, else the region
/// quad's index it is cut to, its polygon flat (empty for a whole node))] in the order found, and the refined nodes.
/// ks[L]: K of level L (L >= 1); radii [(min level m, r)]; prev / hold: nodes refined last time / held; forced: boxes;
/// horizon: (reach, min_d, R', the zmax grid's base (n, n), its level, the top level) or None; view: (cx, cy, dz, ppr,
/// tau, reach, hs, cap, cells, errs {(aid, node): err}, aid) or None; region: (quads (n, 8), min_area) or None; cone:
/// the 3D map's view cone (9 values) or None.
#[pyfunction]
#[pyo3(signature = (px, py, radius, disc, max_level, min_level, forced_level, h, eye_h, ks, radii, prev, hold, forced,
                    horizon=None, view=None, region=None, cone=None))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn far_select_nodes<'py>(px: f64, py: f64, radius: f64, disc: bool, max_level: i32, min_level: i32,
                             forced_level: i32, h: f64, eye_h: f64, ks: Vec<f64>, radii: Vec<(i32, f64)>,
                             prev: Vec<Node>, hold: Vec<Node>, forced: Vec<(f64, f64, f64, f64)>,
                             horizon: Option<(f64, f64, f64, PyReadonlyArray2<'py, f64>, i32, i32)>,
                             view: Option<(f64, f64, f64, f64, f64, f64, f64, f64, f64, Bound<'py, PyAny>,
                                           Bound<'py, PyAny>)>,
                             region: Option<(PyReadonlyArray2<'py, f64>, f64)>, cone: Option<Vec<f64>>)
                             -> PyResult<(Vec<(i32, i64, i64, i64, Vec<f64>)>, Vec<Node>, f64)> {
    let hz = match &horizon {
        Some((reach, min_d, r, base, gl, ml)) => Some(Horizon::new(*reach, *min_d, *r, &base.as_array(), *gl, *ml)?),
        None => None,
    };
    let view = view.as_ref().filter(|v| v.4 > 0.0).map(|v| View {
        cx: v.0, cy: v.1, dz: pmax(0.0, v.2), ppr: v.3, tau: v.4, reach: v.5, hs: v.6, cap: v.7, cells: v.8,
        errs: &v.9, aid: &v.10,
    });
    let region = match &region {
        Some((q, min_area)) => {
            let a = q.as_array();
            if a.ncols() != 8 || a.nrows() == 0 {
                return Err(PyValueError::new_err("far_select_nodes: region quads (n, 8)"));
            }
            let quads: Vec<Vec<P2>> = (0..a.nrows()).map(|r| (0..4).map(|c| [a[[r, 2 * c]], a[[r, 2 * c + 1]]]).collect())
                .collect();
            let qb: Vec<[f64; 4]> = quads.iter().map(|q| {
                let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
                for p in q {
                    b = [b[0].min(p[0]), b[1].min(p[1]), b[2].max(p[0]), b[3].max(p[1])];
                }
                b
            }).collect();
            // (Region.bbox: numpy's min / max over every corner)
            let mut bb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
            for b in &qb {
                bb = [bb[0].min(b[0]), bb[1].min(b[1]), bb[2].max(b[2]), bb[3].max(b[3])];
            }
            Some(Region { quads, qb, bbox: bb, min_area: *min_area })
        }
        None => None,
    };
    let prev: HashSet<Node> = prev.into_iter().collect();
    let hold: HashSet<Node> = hold.into_iter().collect();
    let (lo_x, lo_y, hi_x, hi_y) = (px - radius, py - radius, px + radius, py + radius);
    let s = level_size(max_level);
    let [rx0, ry0, rx1, ry1] = match &region {
        Some(r) => r.bbox,
        None => [lo_x, lo_y, hi_x, hi_y],
    };
    let mut stack: Vec<Node> = Vec::new();
    // (the slack: the selection is the same while the player, the radius, the eye height and the camera move less than
    // this in all: every bound a node's answer goes by is farther from its value; the roots' range too)
    let mut slack = SLACK_PAD;
    if region.is_none() {
        for v in [lo_x - (lo_x / s).floor() * s, (hi_x / s).ceil() * s - hi_x, lo_y - (lo_y / s).floor() * s,
                  (hi_y / s).ceil() * s - hi_y] {
            tighten(&mut slack, v);
        }
    }
    for j in (ry0 / s).floor() as i64..(ry1 / s).ceil() as i64 {
        for i in (rx0 / s).floor() as i64..(rx1 / s).ceil() as i64 {
            stack.push((max_level, i, j));
        }
    }
    // the floor of the screen-space LOD's merge: the near radii under each level
    let near_under = |lvl: i32| radii.iter().filter(|(m, _)| min_level <= *m && *m < lvl).map(|(_, r)| *r)
        .fold(None, |a: Option<f64>, r| Some(match a { Some(a) => pmax(a, r), None => r })).unwrap_or(0.0);
    let (mut out, mut refined) = (Vec::new(), Vec::new());
    while let Some(node) = stack.pop() {
        let (lvl, i, j) = node;
        let s = level_size(lvl);
        let (x0, y0, x1, y1) = (i as f64 * s, j as f64 * s, (i + 1) as f64 * s, (j + 1) as f64 * s);
        let mut d = None;
        if disc {
            let dd = box_dist(x0, y0, x1, y1, px, py);
            d = Some(dd);
            tighten(&mut slack, dd - radius);
            if let Some(hz) = &hz {
                tighten(&mut slack, hz.keeps_slack(lvl, i, j, dd));
            }
            if dd >= radius || hz.as_ref().is_some_and(|hz| !hz.keeps(lvl, i, j, dd)) {
                continue;
            }
        } else {
            for v in [x0 - hi_x, x1 - lo_x, y0 - hi_y, y1 - lo_y] {
                tighten(&mut slack, v);
            }
            if !(x0 < hi_x && x1 > lo_x && y0 < hi_y && y1 > lo_y) {
                continue;
            }
        }
        let cover = match &region {
            Some(r) => match r.cover(x0, y0, x1, y1) {
                Cover::Outside => continue,
                c => Some(c),
            },
            None => None,
        };
        if lvl > min_level {
            let split = if lvl > forced_level && forced.iter().any(|b| x0 < b.2 && x1 > b.0 && y0 < b.3 && y1 > b.1) {
                true
            } else if hold.contains(&node) {
                prev.contains(&node)
            } else {
                let d = d.unwrap_or_else(|| box_dist(x0, y0, x1, y1, px, py));
                let lim = switch_distance(lvl, &ks, &radii, min_level);
                let mut d3 = d.dhypot(eye_h);
                let mut fac = 1.0;
                if let Some(c) = cone.as_deref() {
                    fac = out_of_view(c, x0, y0, x1, y1, d);
                    d3 *= fac;
                    if out_of_view(c, x0, y0, x1, y1, 1e-9) != 1.0 {
                        tighten(&mut slack, d);     // (the node under the camera, d 0, counts as in view)
                    }
                }
                let was = prev.contains(&node);
                let split = d3 < lim || (was && d3 < (1.0 + h) * lim);
                // (d3 moves at most fac x the player's move plus the eye's)
                tighten(&mut slack, (d3 - lim) / fac);
                if was {
                    tighten(&mut slack, (d3 - (1.0 + h) * lim) / fac);
                }
                match &view {
                    Some(v) => {
                        let lim_f = if lvl - 1 > min_level {
                            pmax(switch_distance(lvl - 1, &ks, &radii, min_level), near_under(lvl))
                        } else {
                            lim
                        };
                        tighten(&mut slack, v.slack(was, node, [x0, y0, x1, y1], d, d3, lim, lim_f, h)? / fac);
                        v.decide(split, was, node, [x0, y0, x1, y1], d, d3, lim, lim_f, h)?
                    }
                    None => split,
                }
            };
            if split {
                refined.push(node);
                let (i2, j2) = (2 * i, 2 * j);
                stack.extend([(lvl - 1, i2, j2), (lvl - 1, i2 + 1, j2), (lvl - 1, i2, j2 + 1), (lvl - 1, i2 + 1, j2 + 1)]);
                continue;
            }
        }
        match cover {
            Some(Cover::Across(got)) => {
                for (qi, poly) in got {
                    out.push((lvl, i, j, qi as i64, poly));
                }
            }
            _ => out.push((lvl, i, j, -1, Vec::new())),
        }
    }
    Ok((out, refined, slack))
}

/// farfield.HorizonGrid.radius: (the far radius, zmax_region) for an eye `reach` m from its sea-level horizon
/// (sqrt(2 R' h_eye)) at (px, py) over the zmax grid's base (n, n; [j, i] the cell whose west / south edges are
/// edges[i] / edges[j], `cell` m on a side): zmax_region the highest cell rising above the eye's horizon (its nearest
/// point within reach + sqrt(2 R' z)), the radius reach + sqrt(2 R' zmax_region) within [r_min, r_max].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn far_horizon_radius(py: Python<'_>, base: PyReadonlyArray2<'_, f64>, edges: Vec<f64>, cell: f64, px: f64,
                          pyy: f64, reach: f64, r: f64, r_min: f64, r_max: f64) -> PyResult<(f64, f64)> {
    let a = base.as_array();
    let n = edges.len();
    if a.nrows() != n || a.ncols() != n {
        return Err(PyValueError::new_err("far_horizon_radius: base (n, n) and n edges"));
    }
    let z: Vec<f64> = a.iter().copied().collect();
    Ok(py.detach(|| {
        let dx: Vec<f64> = edges.iter().map(|&e| pmax(pmax(e - px, 0.0), px - (e + cell))).collect();
        let dy: Vec<f64> = edges.iter().map(|&e| pmax(pmax(e - pyy, 0.0), pyy - (e + cell))).collect();
        // (numpy: z[vis].max() over the visible cells, NaN spreading; 0 when none)
        let mut zr: Option<f64> = None;
        for j in 0..n {
            for i in 0..n {
                let zc = z[j * n + i];
                let d = dx[i].dhypot(dy[j]);       // (dnp.hypot: the libm crate's)
                let zp = if zc.is_nan() { f64::NAN } else if zc > 0.0 { zc } else { 0.0 };
                if d <= reach + (2.0 * r * zp).sqrt() {
                    zr = Some(match zr { Some(m) => nanmax(m, zc), None => zc });
                }
            }
        }
        let zr = zr.unwrap_or(0.0);
        let hd = (2.0 * r * pmax(0.0, zr)).sqrt();
        (pmin(pmax(reach + hd, r_min), r_max), zr)
    }))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(far_select_nodes, m)?)?;
    m.add_function(wrap_pyfunction!(far_horizon_radius, m)?)?;
    Ok(())
}
