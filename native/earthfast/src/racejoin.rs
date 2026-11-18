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
//! The layered rule (`layered`): a raceway that ends on another raceway's band gives way to it (`dominance`: the other
//! passes through there, or, where the through raceway is split at the junction, the straightest pair of the others
//! ending there is the through route). The minor raceways are laid first (nearest line among them), each point's
//! surface eased to the dominant raceway's surface at the dominant's band edge over `blend` m out from it (on the
//! minor's own side; inside the dominant's band the dominant's surface itself), then the other raceways over them with
//! their edge fade (`margin`) to what lies under them. Never a height inside the dominant's carriageway rewritten.
//!
//! Sums, products, min / max, floor and sqrt only (polyproject::project): the same bits on every platform.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::polyproject;

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

/// (surface (m,), covered (m,)): base at the points pts (m x 2) as the raceways' surfaces (module doc). dom: per zone
/// the zones it gives way to (`dominance`); None: the old nearest-line rule (dom and blend unused).
pub fn ground(pts: &[f64], base: &[f64], zones: &[Zone], dom: Option<&[Vec<usize>]>, margin: f64, blend: f64)
              -> (Vec<f64>, Vec<bool>) {
    let m = base.len();
    let mut out = base.to_vec();
    let mut covered = vec![false; m];
    if m == 0 || zones.is_empty() {
        return (out, covered);
    }
    let ext = extents(pts);
    let mut proj: Vec<Option<Proj>> = (0..zones.len()).map(|_| None).collect();
    let layers: Vec<Vec<usize>> = match dom {
        None => vec![(0..zones.len()).collect()],
        Some(d) => vec![(0..zones.len()).filter(|&z| !d[z].is_empty()).collect(),
                        (0..zones.len()).filter(|&z| d[z].is_empty()).collect()],
    };
    let fade = pymax(margin, 1e-9);
    for layer in layers {
        let mut best = vec![f64::INFINITY; m];
        let mut add = vec![0.0f64; m];
        for &zi in &layer {
            let z = &zones[zi];
            if z.misses(z.hw, ext) {
                continue;
            }
            if proj[zi].is_none() {
                proj[zi] = Some(project_all(z, pts));
            }
            let mine: Vec<usize> = match dom {
                Some(d) if blend > 0.0 => d[zi].iter().copied()
                    .filter(|&g| !zones[g].misses(zones[g].hw + blend, ext)).collect(),
                _ => Vec::new(),
            };
            for &g in &mine {
                if proj[g].is_none() {
                    proj[g] = Some(project_all(&zones[g], pts));
                }
            }
            let p = proj[zi].as_ref().unwrap();
            for i in 0..m {
                let (st, off, dist) = (p.station[i], p.offset[i], p.dist[i]);
                if !(st >= 0.0 && st <= z.s_end && off.abs() <= z.hw && dist < best[i]) {
                    continue;
                }
                let mut c = z.at(st, off);
                if !mine.is_empty() {
                    // toward the dominant's surface at its band's edge (inside the band: its surface there)
                    let (mut bd, mut zt) = (f64::INFINITY, c);
                    for &g in &mine {
                        let (gz, gp) = (&zones[g], proj[g].as_ref().unwrap());
                        let (s2, o2) = (gp.station[i], gp.offset[i]);
                        let dd = o2.abs() - gz.hw;
                        if s2 >= 0.0 && s2 <= gz.s_end && dd <= blend && dd < bd {
                            zt = gz.at(s2, o2.clamp(-gz.hw, gz.hw));
                            bd = dd;
                        }
                    }
                    if bd.is_finite() {
                        let w = smooth(npclip(1.0 - bd.max(0.0) / blend, 0.0, 1.0));
                        c += (zt - c) * w;
                    }
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

/// Per raceway line (lines[i]: k x 2 flattened, its band's half width hw[i]), the lines it gives way to (module doc):
/// an end of it on another's band (station on it, |offset| within its half width) where that one passes through (a
/// closed loop, or the end more than its half width from that one's ends), or, where only ends meet there and 3 or
/// more lines end on each other's band, the straightest pair of the others when straighter than this one with either.
pub fn dominance(lines: &[Vec<f64>], hw: &[f64]) -> Vec<Vec<usize>> {
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
        let d0 = (l[0] - px).powi(2) + (l[1] - py).powi(2);
        let d1 = (l[2 * (n - 1)] - px).powi(2) + (l[2 * (n - 1) + 1] - py).powi(2);
        let (a, b) = if d0 <= d1 { (0, 3.min(n - 1)) } else { (n - 1, n.saturating_sub(4)) };
        let (vx, vy) = (l[2 * a] - l[2 * b], l[2 * a + 1] - l[2 * b + 1]);
        let len = (vx * vx + vy * vy).sqrt().max(1e-9);
        (vx / len, vy / len)
    };
    let mut dom: Vec<Vec<usize>> = vec![Vec::new(); nz];
    for b in 0..nz {
        if k[b] < 2 || closed[b] {
            continue;
        }
        let lb = &lines[b];
        for e in [0usize, k[b] - 1] {
            let (px, py) = (lb[2 * e], lb[2 * e + 1]);
            let (mut thru, mut ending) = (Vec::new(), Vec::new());
            for a in 0..nz {
                if a == b || k[a] < 2 {
                    continue;
                }
                let r = polyproject::project(&lines[a], 2, k[a], &[px, py], 2, 1);
                let (s0, o0) = (r.station[0], r.offset[0]);
                if !(s0 >= 0.0 && s0 <= length[a] && o0.abs() <= hw[a]) {
                    continue;
                }
                if closed[a] || (s0 > hw[a] && s0 < length[a] - hw[a]) {
                    thru.push(a);
                } else {
                    ending.push(a);
                }
            }
            if !thru.is_empty() {
                dom[b].extend(thru);
            } else if ending.len() >= 2 {
                let db = outdir(b, px, py);
                let dirs: Vec<(f64, f64)> = ending.iter().map(|&z| outdir(z, px, py)).collect();
                let mut best: Option<(f64, usize, usize)> = None;
                for i in 0..ending.len() {
                    for j in i + 1..ending.len() {
                        let c = dirs[i].0 * dirs[j].0 + dirs[i].1 * dirs[j].1;
                        if best.is_none_or(|q| c < q.0) {
                            best = Some((c, ending[i], ending[j]));
                        }
                    }
                }
                let cb = dirs.iter().map(|d| db.0 * d.0 + db.1 * d.1).fold(f64::INFINITY, f64::min);
                if let Some((c, p, q)) = best {
                    if c < cb {
                        dom[b].push(p);
                        dom[b].push(q);
                    }
                }
            }
        }
        dom[b].sort_unstable();
        dom[b].dedup();
    }
    dom
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
/// dom None: the nearest-line rule, else per zone the indices of the zones it gives way to (layered, blend m).
#[pyfunction]
#[pyo3(signature = (e, n, base, zones, margin, dom=None, blend=0.0))]
#[allow(clippy::type_complexity)]
pub fn raceway_ground<'py>(py: Python<'py>, e: PyReadonlyArray1<'py, f64>, n: PyReadonlyArray1<'py, f64>,
                           base: PyReadonlyArray1<'py, f64>, zones: Vec<ZoneArgs<'py>>, margin: f64,
                           dom: Option<Vec<Vec<usize>>>, blend: f64)
                           -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<bool>>)> {
    let (ea, na, ba) = (e.as_array(), n.as_array(), base.as_array());
    let m = ba.len();
    if ea.len() != m || na.len() != m {
        return Err(pyo3::exceptions::PyValueError::new_err("raceway_ground: e, n and base of one length"));
    }
    let zs = zones_of(&zones)?;
    if let Some(d) = &dom {
        if d.len() != zs.len() || d.iter().flatten().any(|&g| g >= zs.len()) {
            return Err(pyo3::exceptions::PyValueError::new_err("raceway_ground: dom per zone, zone indices"));
        }
    }
    let pts: Vec<f64> = (0..m).flat_map(|i| [ea[i], na[i]]).collect();
    let bv: Vec<f64> = ba.iter().copied().collect();
    let (out, cov) = py.detach(|| ground(&pts, &bv, &zs, dom.as_deref(), margin, blend));
    Ok((out.into_pyarray(py), cov.into_pyarray(py)))
}

/// grading.raceway_dominance: per line [(line (k, 2), band half width)] the sorted indices of the lines it gives way to.
#[pyfunction]
pub fn raceway_dominance(py: Python<'_>, lines: Vec<(PyReadonlyArray2<'_, f64>, f64)>) -> PyResult<Vec<Vec<usize>>> {
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
    Ok(py.detach(|| dominance(&ls, &hw)))
}

#[cfg(test)]
mod tests {
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
        assert_eq!(dom, vec![vec![], vec![0]]);
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
    fn split_through_route_is_the_straight_pair() {
        // the oval split at x = 0 into two ways meeting end to end, a pit road ending there from the side
        let a: Vec<f64> = (0..=50).flat_map(|i| [-100.0 + 2.0 * i as f64, 0.0]).collect();
        let b: Vec<f64> = (0..=50).flat_map(|i| [2.0 * i as f64, 0.0]).collect();
        let c: Vec<f64> = (0..=20).flat_map(|i| [0.0, 40.0 - 2.0 * i as f64]).collect();
        let dom = dominance(&[a.clone(), b.clone(), c.clone()], &[9.0, 9.0, 9.0]);
        assert_eq!(dom, vec![vec![], vec![], vec![0, 1]]);
        // two ways end to end alone: no one gives way
        assert_eq!(dominance(&[a, b], &[9.0, 9.0]), vec![Vec::<usize>::new(), vec![]]);
        // a road crossing the band away from its ends without ending there: no relation
        let d: Vec<f64> = (0..=40).flat_map(|i| [10.0, -40.0 + 2.0 * i as f64]).collect();
        let e: Vec<f64> = (0..=50).flat_map(|i| [-50.0 + 2.0 * i as f64, 0.0]).collect();
        assert_eq!(dominance(&[e, d], &[9.0, 9.0]), vec![Vec::<usize>::new(), vec![]]);
    }

    #[test]
    fn nearest_rule_without_dom() {
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
