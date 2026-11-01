//! Ease road profiles only at shared mapped connectors. Spatial proximity is not connectivity:
//! a cul-de-sac, another turn of a hairpin, and a tunnel roof can be close to a road without joining it.

use std::collections::BTreeMap;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

fn ease(u: f64) -> f64 {
    if u < 1.0 { 0.5 * (1.0 + (std::f64::consts::PI * u.max(0.0)).cos()) } else { 0.0 }
}

#[derive(Clone, Copy)]
struct Node {
    plan: usize,
    end: i64,
    segment: usize,
    t: f64,
    arc: f64,
    point: [f64; 2],
}

/// xy/z/off: concatenated plans. links: (plan, connector identity, end 0/1 or -1 for an interior connector).
/// points: each link's mapped position on its own source geometry, NOT the other road's nearest point.
/// rid gives deterministic feature ordering. No connectors means no snapping; no geometric fallback.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn snap_junctions<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>, off: PyReadonlyArray1<'py, i64>,
    rank: PyReadonlyArray1<'py, i64>, half: PyReadonlyArray1<'py, f64>, abut: PyReadonlyArray2<'py, u8>,
    rid: PyReadonlyArray1<'py, i64>, links: PyReadonlyArray2<'py, i64>, points: PyReadonlyArray2<'py, f64>,
    reach: f64, end_m: f64, taper_m: f64, max_m: f64, coarse: bool,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<u8>>)> {
    let xy = xy.as_array();
    let mut zz = z.as_array().to_vec();
    let off = off.as_array();
    let rank = rank.as_array();
    let half = half.as_array();
    let abut = abut.as_array();
    let rid = rid.as_array();
    let links = links.as_array();
    let points = points.as_array();
    let np = rank.len();
    if xy.ncols() != 2 || zz.len() != xy.nrows() || off.len() != np + 1 || off[0] != 0
        || off[np] != zz.len() as i64 || off.iter().any(|&x| x < 0)
        || off.iter().zip(off.iter().skip(1)).any(|(a, b)| a > b)
        || half.len() != np || rid.len() != np || abut.dim() != (np, 2)
        || links.ncols() != 3 || points.dim() != (links.nrows(), 2)
    {
        return Err(pyo3::exceptions::PyValueError::new_err("snap_junctions: array sizes differ"));
    }
    let mut arcs = Vec::with_capacity(np);
    for j in 0..np {
        let (a, b) = (off[j] as usize, off[j + 1] as usize);
        let mut s = vec![0.0; b - a];
        for i in 1..s.len() {
            s[i] = s[i - 1] + (xy[[a + i, 0]] - xy[[a + i - 1, 0]])
                .hypot(xy[[a + i, 1]] - xy[[a + i - 1, 1]]);
        }
        arcs.push(s);
    }
    let mut groups: BTreeMap<i64, Vec<Node>> = BTreeMap::new();
    for h in 0..links.nrows() {
        let (j, node, end) = (links[[h, 0]], links[[h, 1]], links[[h, 2]]);
        if j < 0 || j >= np as i64 || !(-1..=1).contains(&end) {
            return Err(pyo3::exceptions::PyValueError::new_err("snap_junctions: invalid connector"));
        }
        let j = j as usize;
        let a = off[j] as usize;
        let s = &arcs[j];
        if s.len() < 2 { continue; }
        let point = [points[[h, 0]], points[[h, 1]]];
        let (mut segment, mut t, mut distance) = (0, 0.0, f64::INFINITY);
        if end >= 0 {
            segment = if end == 0 { 0 } else { s.len() - 2 };
            t = end as f64;
            let i = a + if end == 0 { 0 } else { s.len() - 1 };
            distance = (xy[[i, 0]] - point[0]).hypot(xy[[i, 1]] - point[1]);
        } else {
            for i in 0..s.len() - 1 {
                let (dx, dy) = (xy[[a + i + 1, 0]] - xy[[a + i, 0]], xy[[a + i + 1, 1]] - xy[[a + i, 1]]);
                let u = (((point[0] - xy[[a + i, 0]]) * dx + (point[1] - xy[[a + i, 1]]) * dy)
                    / (dx * dx + dy * dy).max(1e-12)).clamp(0.0, 1.0);
                let d = (xy[[a + i, 0]] + u * dx - point[0]).hypot(xy[[a + i, 1]] + u * dy - point[1]);
                if d < distance { segment = i; t = u; distance = d; }
            }
        }
        // junction_nodes established membership on the original ground/clipped source piece.
        // A valid corner can be far from this plan's resampling chord: projection samples the
        // proven participant, it does not discover a join or choose another road.
        if !distance.is_finite() { continue; }
        let arc = s[segment] + t * (s[segment + 1] - s[segment]);
        groups.entry(node).or_default().push(Node { plan: j, end, segment, t, arc, point });
    }
    let compare_plans = |j: usize, k: usize| {
        (-rank[j], rid[j]).cmp(&(-rank[k], rid[k]))
            .then_with(|| xy[[off[j] as usize, 0]].total_cmp(&xy[[off[k] as usize, 0]]))
            .then_with(|| xy[[off[j] as usize, 1]].total_cmp(&xy[[off[k] as usize, 1]]))
            .then_with(|| xy[[off[j + 1] as usize - 1, 0]].total_cmp(&xy[[off[k + 1] as usize - 1, 0]]))
            .then_with(|| xy[[off[j + 1] as usize - 1, 1]].total_cmp(&xy[[off[k + 1] as usize - 1, 1]]))
    };
    for members in groups.values_mut() {
        members.sort_by(|a, b| compare_plans(a.plan, b.plan).then_with(|| a.arc.total_cmp(&b.arc)));
    }
    let mut incident = vec![Vec::new(); np];
    for (node, members) in &groups {
        for m in members { incident[m.plan].push((*node, *m)); }
    }
    let mut order: Vec<usize> = (0..np).filter(|&j| arcs[j].len() >= 2).collect();
    order.sort_by(|&j, &k| compare_plans(j, k));
    let mut changed = vec![0u8; np];
    for j in order {
        let a = off[j] as usize;
        let s = &arcs[j];
        if s.len() < 2 { continue; }
        let length = s[s.len() - 1];
        let taper = taper_m.min(0.5 * length).max(1e-6);
        let mut dz = vec![0.0; s.len()];
        for (node, own) in &incident[j] {
            if own.end >= 0 && abut[[j, own.end as usize]] != 0 { continue; }
            let interior = own.end < 0;
            if interior && (!coarse || own.arc <= end_m || own.arc >= length - end_m) { continue; }
            let mut best: Option<(f64, i64, Node)> = None;
            for other in &groups[node] {
                let k = other.plan;
                if k == j || rid[k] == rid[j] || rank[k] < rank[j] || (interior && rank[k] == rank[j]) { continue; }
                let d = (own.point[0] - other.point[0]).hypot(own.point[1] - other.point[1]);
                if d > half[k] + reach { continue; }
                let sk = &arcs[k];
                if rank[k] == rank[j] && other.arc.min(sk[sk.len() - 1] - other.arc) < end_m { continue; }
                if best.map_or(true, |b| (d, rid[k]) < (b.0, b.1)) { best = Some((d, rid[k], *other)); }
            }
            let Some((_, _, other)) = best else { continue; };
            let q = off[other.plan] as usize + other.segment;
            let target = zz[q] + other.t * (zz[q + 1] - zz[q]);
            let i = own.segment;
            let current = zz[a + i] + dz[i] + own.t * (zz[a + i + 1] + dz[i + 1] - zz[a + i] - dz[i]);
            let delta = target - current;
            if delta.abs() > max_m { continue; }
            for (i, &arc) in s.iter().enumerate() { dz[i] += delta * ease((arc - own.arc).abs() / taper); }
        }
        if dz.iter().any(|&d| d != 0.0) {
            for (i, d) in dz.into_iter().enumerate() { zz[a + i] += d; }
            changed[j] = 1;
        }
    }
    Ok((zz.into_pyarray(py), changed.into_pyarray(py)))
}
