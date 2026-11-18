//! levelways.promote (plan 035): a walking-class way that carries a vehicle level crossing node, with a drivable road
//! leading into it, takes that road's class; then the short walking pieces linking a promoted way to a road. Its
//! Python walked every road's rings and every walking way against the nodes and the road ends one by one (~0.6 CPU-s
//! of a 16-tile I-83 Baltimore probe, every footway of a city cell, 2026-11-12). The same tests here, the numpy
//! version's numbers (its pairwise sums, first minimum, operation order).
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::junctionsnap::np_sum;

type Pt = [f64; 2];

/// np.maximum / np.minimum / np.clip of one value (a NaN stays a NaN).
fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.max(b) }
}

fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.min(b) }
}

/// levelways._seg_dist: distance from p to the polyline ln (np.min of the pieces' distances).
fn seg_dist(p: Pt, ln: &[Pt]) -> f64 {
    let mut best = f64::INFINITY;
    for w in ln.windows(2) {
        let (a, b) = (w[0], w[1]);
        let ab = [b[0] - a[0], b[1] - a[1]];
        let num = (p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1];
        let den = nmax(ab[0] * ab[0] + ab[1] * ab[1], 1e-12);
        let t = nmin(nmax(num / den, 0.0), 1.0);
        let d = ((a[0] + t * ab[0]) - p[0]).dhypot((a[1] + t * ab[1]) - p[1]);
        best = nmin(best, d);
    }
    best
}

/// levelways._box: (x0, y0, x1, y1) round the lines, pad out.
fn bbox(ls: &[&[Pt]], pad: f64) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for ln in ls {
        for p in ln.iter() {
            b = [nmin(b[0], p[0]), nmin(b[1], p[1]), nmax(b[2], p[0]), nmax(b[3], p[1])];
        }
    }
    [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]
}

fn inside(p: Pt, b: &[f64; 4]) -> bool {
    p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3]
}

/// The length of a line: np.dhypot(*np.diff(ln, axis=0).T).sum() (pairwise).
fn length(ln: &[Pt]) -> f64 {
    let d: Vec<f64> = ln.windows(2).map(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1])).collect();
    np_sum(&d)
}

/// levelways.promote's classes: codes (n,) each feature's class (an index into the layer's class names), road (n,) the
/// features of subtype road, walk / drive / service per class code (WALK_CLASSES, DRIVE_CLASSES, "service"), the
/// layer's geometry (part_off, ring_off, coord_off, en (N, 2)) and the level crossing nodes (m, 2); params: ON_WAY_M,
/// JOIN_M, NODE_REACH_M, MAX_WAY_M, LINK_M, END_M. Returns the new codes, or None when nothing changes.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn level_ways_promote<'py>(
    py: Python<'py>,
    codes: PyReadonlyArray1<'py, i64>,
    road: PyReadonlyArray1<'py, bool>,
    walk: PyReadonlyArray1<'py, bool>,
    drive: PyReadonlyArray1<'py, bool>,
    service: PyReadonlyArray1<'py, bool>,
    part_off: PyReadonlyArray1<'py, i64>,
    ring_off: PyReadonlyArray1<'py, i64>,
    coord_off: PyReadonlyArray1<'py, i64>,
    en: PyReadonlyArray2<'py, f64>,
    nodes: PyReadonlyArray2<'py, f64>,
    params: PyReadonlyArray1<'py, f64>,
) -> PyResult<Option<Bound<'py, PyArray1<i64>>>> {
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("level_ways_promote: {m}"));
    let (codes, road, walk, drive, service) =
        (codes.as_array(), road.as_array(), walk.as_array(), drive.as_array(), service.as_array());
    let (po, ro, co, en, nodes) = (part_off.as_array(), ring_off.as_array(), coord_off.as_array(), en.as_array(),
                                   nodes.as_array());
    let pr = params.as_slice().map_err(|_| bad("params must be contiguous"))?;
    if pr.len() != 6 {
        return Err(bad("params needs 6 values"));
    }
    let (on_way, join, node_reach, max_way, link_m, end_m) = (pr[0], pr[1], pr[2], pr[3], pr[4], pr[5]);
    let n = codes.len();
    let nc = walk.len();
    if road.len() != n || po.len() != n + 1 || drive.len() != nc || service.len() != nc || en.ncols() < 2
        || nodes.ncols() < 2 {
        return Err(bad("array sizes"));
    }
    if codes.iter().any(|&c| c < 0 || c as usize >= nc) {
        return Err(bad("bad class code"));
    }
    // every feature's lines (its rings of 2 points or more), as point lists
    let pts: Vec<Pt> = (0..en.nrows()).map(|k| [en[[k, 0]], en[[k, 1]]]).collect();
    let ok = |a: i64, b: i64, len: usize| a >= 0 && b >= a && (b as usize) <= len;
    let mut lines: Vec<Vec<(usize, usize)>> = Vec::with_capacity(n);
    for i in 0..n {
        let mut out = Vec::new();
        if !ok(po[i], po[i + 1], ro.len().saturating_sub(1)) {
            return Err(bad("bad part_off"));
        }
        let (r0, r1) = (ro[po[i] as usize], ro[po[i + 1] as usize]);
        if !ok(r0, r1, co.len().saturating_sub(1)) {
            return Err(bad("bad ring_off"));
        }
        for r in r0 as usize..r1 as usize {
            if !ok(co[r], co[r + 1], pts.len()) {
                return Err(bad("bad coord_off"));
            }
            if co[r + 1] - co[r] >= 2 {
                out.push((co[r] as usize, co[r + 1] as usize));
            }
        }
        lines.push(out);
    }
    let ls_of = |i: usize| -> Vec<&[Pt]> { lines[i].iter().map(|&(a, b)| &pts[a..b]).collect() };
    let node_pts: Vec<Pt> = (0..nodes.nrows()).map(|k| [nodes[[k, 0]], nodes[[k, 1]]]).collect();
    let mut cls: Vec<usize> = codes.iter().map(|&c| c as usize).collect();
    let roads: Vec<usize> = (0..n).filter(|&i| road[i]).collect();
    // the drivable roads' ends: (class, point), by feature, ring, start then end
    let mut ends: Vec<(usize, Pt)> = Vec::new();
    for &i in &roads {
        if drive[cls[i]] {
            for ln in ls_of(i) {
                ends.push((cls[i], ln[0]));
                ends.push((cls[i], ln[ln.len() - 1]));
            }
        }
    }
    if ends.is_empty() {
        return Ok(None);
    }
    let reach = join.max(node_reach + on_way) + 1.0;
    let mut promoted: Vec<usize> = Vec::new();
    for &i in &roads {
        if !walk[cls[i]] {
            continue;
        }
        let ls = ls_of(i);
        if ls.is_empty() {
            continue;
        }
        let b = bbox(&ls, on_way + 1.0);
        let on: Vec<Pt> = node_pts
            .iter()
            .copied()
            .filter(|&p| inside(p, &b) && ls.iter().any(|ln| seg_dist(p, ln) < on_way))
            .collect();
        if on.is_empty() {
            continue;
        }
        let br = bbox(&ls, reach);
        let mut best: Option<((bool, f64), usize)> = None;
        for &(c, end) in ends.iter().filter(|e| inside(e.1, &br)) {
            // (Python's min of floats: the first of the smallest)
            let near_way = ls.iter().map(|ln| seg_dist(end, ln)).fold(None, |m: Option<f64>, d| match m {
                Some(v) if !(d < v) => Some(v),
                _ => Some(d),
            }).unwrap();
            let to_node = on.iter().map(|p| (end[0] - p[0]).dhypot(end[1] - p[1])).fold(None, |m: Option<f64>, d| {
                match m {
                    Some(v) if !(d < v) => Some(v),
                    _ => Some(d),
                }
            }).unwrap();
            let key = (service[c], to_node);
            let better = match best {
                None => true,
                Some((k, _)) => key.0 < k.0 || (key.0 == k.0 && key.1 < k.1),
            };
            if (near_way < join || to_node < node_reach) && better {
                best = Some((key, c));
            }
        }
        if let Some((_, c)) = best {
            // (Python's sum() of the lines' lengths: in order from 0)
            let mut total = 0.0;
            for ln in &ls {
                total += length(ln);
            }
            if total <= max_way {
                cls[i] = c;
                promoted.push(i);
            }
        }
    }
    if promoted.is_empty() {
        return Ok(None);
    }
    // the short walking pieces linking a promoted way to a road: the promoted ways' lines first, then the other
    // drivable roads'
    let mut dl: Vec<(usize, &[Pt])> = Vec::new();
    for &i in &promoted {
        for ln in ls_of(i) {
            dl.push((cls[i], ln));
        }
    }
    let is_promoted = {
        let mut v = vec![false; n];
        for &i in &promoted {
            v[i] = true;
        }
        v
    };
    for &i in &roads {
        if drive[cls[i]] && !is_promoted[i] {
            for ln in ls_of(i) {
                dl.push((cls[i], ln));
            }
        }
    }
    let boxes: Vec<[f64; 4]> = dl.iter().map(|(_, ln)| bbox(&[*ln], end_m + 1.0)).collect();
    for &i in &roads {
        if !walk[cls[i]] {
            continue;
        }
        let ls = ls_of(i);
        if ls.len() != 1 || length(ls[0]) > link_m {
            continue;
        }
        let ln = ls[0];
        let got: Vec<Vec<usize>> = [ln[0], ln[ln.len() - 1]]
            .iter()
            .map(|&p| {
                (0..dl.len()).filter(|&k| inside(p, &boxes[k]) && seg_dist(p, dl[k].1) < end_m).map(|k| dl[k].0).collect()
            })
            .collect();
        if !got[0].is_empty() && !got[1].is_empty() {
            let pc: Vec<usize> = promoted.iter().map(|&k| cls[k]).collect();
            cls[i] = got[0].iter().chain(got[1].iter()).copied().find(|c| pc.contains(c)).unwrap_or(got[0][0]);
        }
    }
    Ok(Some(cls.into_iter().map(|c| c as i64).collect::<Vec<i64>>().into_pyarray(py)))
}
