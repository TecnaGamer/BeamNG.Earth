//! Deck junctions as one surface (roadmesh._junction_surfaces, roads.deck_pads; GitHub #1083's lids, 2026-11-12).
//!
//! A junction the map draws over a lid or a viaduct is several joints a few metres apart (dual carriageways, turn
//! lanes), every short deck part between two of them. Per joint, a part shared by two joints could take neither
//! joint's height field, so each kept its own flat slab: at Vienna's S1 under Rautenweg 16 parts overlapped at
//! different heights and tilts, their slabs' sides standing through each other's lanes. Here:
//!  - `junction_groups`: the deck parts' ends grouped into joints (as roadmesh did), the per-joint fields (a joint of
//!    three or more parts no other joint shares), and the clusters: parts linked through joints into one component
//!    holding two or more such joints, one field over all of them, its ends that go on alone pinned
//!    (centre and both edges at the end's height);
//!  - `field_pins`: the weights of a compactly supported correction (Wendland C2) that puts a field exactly on its pins,
//!    and its value anywhere;
//!  - `pad_links`: the junction pads of joints a short deck part links, merged.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

fn find(p: &mut [usize], mut a: usize) -> usize {
    while p[a] != a {
        p[a] = p[p[a]];
        a = p[a];
    }
    a
}

fn union(p: &mut [usize], a: usize, b: usize) {
    let (ra, rb) = (find(p, a), find(p, b));
    if ra != rb {
        let (lo, hi) = if ra < rb { (ra, rb) } else { (rb, ra) };
        p[hi] = lo;
    }
}

pub struct Groups {
    pub group: Vec<i64>,                // per end (2 per part, -1: part too long)
    pub field: Vec<i64>,                // per part: its field, -1 none
    pub kind: Vec<i64>,                 // per field: 0 one joint, 1 a cluster
    pub joint: Vec<[f64; 3]>,           // per field
    pub members: Vec<[i64; 3]>,         // (field, part, 1 when the entry runs from its end 1 (reversed))
    pub pins: Vec<[f64; 4]>,            // (field, x, y, z): kind 0 the entries' far ends (all must hold), kind 1 pins
}

/// ends: per part k rows 2k (start), 2k + 1 (end): x, y, z, ux, uy (unit direction into the part); plen (n,) its
/// length; half (n,) its half width; skip (n,) parts that may share no field; bbox (n, 4) its plan box.
/// prm: join_m, join_z, max_part_m, max_branches, cluster_on, cluster_max_parts, cluster_max_m.
pub fn junction_groups_core(ends: &[[f64; 5]], plen: &[f64], half: &[f64], skip: &[bool], bbox: &[[f64; 4]],
                            prm: &[f64; 7]) -> Groups {
    let n = plen.len();
    let (join_m, join_z, max_part, max_br) = (prm[0], prm[1], prm[2], prm[3] as usize);
    let (cl_on, cl_parts, cl_m) = (prm[4] > 0.5, prm[5] as usize, prm[6]);
    // ends in part order, start then end, of the parts no longer than max_part; greedy groups (each end joins the first
    // group whose every end is within join_m in plan and whose heights with it span join_z at most)
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut group = vec![-1i64; 2 * n];
    for k in 0..n {
        if !(plen[k] <= max_part) {
            continue;
        }
        for e in [2 * k, 2 * k + 1] {
            let p = ends[e];
            let mut hit = None;
            for (g, mem) in groups.iter().enumerate() {
                let near = mem.iter().all(|&q| (ends[q][0] - p[0]).dhypot(ends[q][1] - p[1]) <= join_m);
                if !near {
                    continue;
                }
                let (mut lo, mut hi) = (p[2], p[2]);
                for &q in mem {
                    lo = lo.min(ends[q][2]);
                    hi = hi.max(ends[q][2]);
                }
                if hi - lo <= join_z {
                    hit = Some(g);
                    break;
                }
            }
            match hit {
                Some(g) => {
                    groups[g].push(e);
                    group[e] = g as i64;
                }
                None => {
                    group[e] = groups.len() as i64;
                    groups.push(vec![e]);
                }
            }
        }
    }
    let parts_of = |mem: &Vec<usize>| {
        let mut v: Vec<usize> = mem.iter().map(|&e| e / 2).collect();
        v.sort_unstable();
        v.dedup();
        v
    };
    let gparts: Vec<Vec<usize>> = groups.iter().map(parts_of).collect();
    let cand: Vec<bool> = gparts.iter().map(|p| p.len() >= 3).collect();
    let mut occ = vec![0usize; n];
    for (g, ps) in gparts.iter().enumerate() {
        if cand[g] {
            for &k in ps {
                occ[k] += 1;
            }
        }
    }
    let mut out = Groups { group, field: vec![-1; n], kind: Vec::new(), joint: Vec::new(), members: Vec::new(),
                           pins: Vec::new() };
    // clusters: parts linked through groups of two or more parts; a component with two or more candidate joints
    if cl_on {
        let mut par: Vec<usize> = (0..n).collect();
        for ps in &gparts {
            for &k in ps.iter().skip(1) {
                union(&mut par, ps[0], k);
            }
        }
        let mut comps: Vec<Vec<usize>> = Vec::new();
        let mut at = vec![usize::MAX; n];
        for k in 0..n {
            if !(plen[k] <= max_part) {
                continue;
            }
            let r = find(&mut par, k);
            if at[r] == usize::MAX {
                at[r] = comps.len();
                comps.push(Vec::new());
            }
            comps[at[r]].push(k);
        }
        for comp in comps {
            let cgroups: Vec<usize> = (0..groups.len()).filter(|&g| cand[g] && comp.contains(&gparts[g][0])).collect();
            // (a skipped part, one with sidewalks, a lane shift, a taper, or an end at a deck the joints leave out, is
            // in its cluster all the same: the field is a height at every point, whatever the slab's outline, and an end
            // at a left-out deck is a lone end, pinned)
            if cgroups.len() < 2 || comp.len() > cl_parts {
                continue;
            }
            let mut bb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
            for &k in &comp {
                bb[0] = bb[0].min(bbox[k][0]);
                bb[1] = bb[1].min(bbox[k][1]);
                bb[2] = bb[2].max(bbox[k][2]);
                bb[3] = bb[3].max(bbox[k][3]);
            }
            if bb[2] - bb[0] > cl_m || bb[3] - bb[1] > cl_m {
                continue;
            }
            let f = out.kind.len() as i64;
            let mut j = [0.0f64; 3];
            let mut cnt = 0.0;
            for &g in &cgroups {
                for &e in &groups[g] {
                    for d in 0..3 {
                        j[d] += ends[e][d];
                    }
                    cnt += 1.0;
                }
            }
            out.kind.push(1);
            out.joint.push([j[0] / cnt, j[1] / cnt, j[2] / cnt]);
            for &k in &comp {
                out.field[k] = f;
                occ[k] = usize::MAX;
                out.members.push([f, k as i64, 0]);
                for e in [2 * k, 2 * k + 1] {
                    let g = out.group[e];
                    if g < 0 || gparts[g as usize].len() != 1 {
                        continue;
                    }
                    let p = ends[e];
                    let (lx, ly) = (-p[4] * half[k], p[3] * half[k]);
                    for s in [0.0, 1.0, -1.0] {
                        out.pins.push([f as f64, p[0] + s * lx, p[1] + s * ly, p[2]]);
                    }
                }
            }
        }
    }
    // one joint's field: three to max_br parts, none in another candidate joint, none skipped
    for g in 0..groups.len() {
        if !cand[g] || gparts[g].len() > max_br || gparts[g].iter().any(|&k| skip[k] || occ[k] > 1) {
            continue;
        }
        let f = out.kind.len() as i64;
        let mut j = [0.0f64; 3];
        for &e in &groups[g] {
            for d in 0..3 {
                j[d] += ends[e][d];
            }
        }
        let c = groups[g].len() as f64;
        out.kind.push(0);
        out.joint.push([j[0] / c, j[1] / c, j[2] / c]);
        for &e in &groups[g] {
            let k = e / 2;
            out.field[k] = f;
            out.members.push([f, k as i64, (e % 2) as i64]);
            let far = ends[e ^ 1];
            out.pins.push([f as f64, far[0], far[1], far[2]]);
        }
    }
    out
}

/// junction_groups_core for Python: ends (2n, 5), plen (n,), half (n,), skip (n,) (nonzero: skipped), bbox (n, 4),
/// prm (7,). (group per end (2n,), field per part (n,), fields (f, 4): kind, joint x, y, z; members (m, 3); pins (q, 4)).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn junction_groups<'py>(py: Python<'py>, ends: PyReadonlyArray2<'py, f64>, plen: PyReadonlyArray1<'py, f64>,
                            half: PyReadonlyArray1<'py, f64>, skip: PyReadonlyArray1<'py, i64>,
                            bbox: PyReadonlyArray2<'py, f64>, prm: PyReadonlyArray1<'py, f64>)
                            -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>,
                                         Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>)> {
    let (ends, plen, half, skip, bbox, prm) = (ends.as_array(), plen.as_array(), half.as_array(), skip.as_array(),
                                               bbox.as_array(), prm.as_array());
    let n = plen.len();
    if ends.nrows() != 2 * n || ends.ncols() != 5 || half.len() != n || skip.len() != n || bbox.nrows() != n
        || bbox.ncols() != 4 || prm.len() != 7 {
        return Err(pyo3::exceptions::PyValueError::new_err("junction_groups: mismatched arrays"));
    }
    let e: Vec<[f64; 5]> = (0..2 * n).map(|i| [ends[[i, 0]], ends[[i, 1]], ends[[i, 2]], ends[[i, 3]], ends[[i, 4]]])
        .collect();
    let b: Vec<[f64; 4]> = (0..n).map(|i| [bbox[[i, 0]], bbox[[i, 1]], bbox[[i, 2]], bbox[[i, 3]]]).collect();
    let pl: Vec<f64> = plen.iter().copied().collect();
    let hf: Vec<f64> = half.iter().copied().collect();
    let sk: Vec<bool> = skip.iter().map(|&v| v != 0).collect();
    let p: [f64; 7] = [prm[0], prm[1], prm[2], prm[3], prm[4], prm[5], prm[6]];
    let g = py.detach(|| junction_groups_core(&e, &pl, &hf, &sk, &b, &p));
    let nf = g.kind.len();
    let fields: Vec<f64> = (0..nf).flat_map(|f| [g.kind[f] as f64, g.joint[f][0], g.joint[f][1], g.joint[f][2]]).collect();
    let mem: Vec<i64> = g.members.iter().flat_map(|m| m.iter().copied()).collect();
    let pins: Vec<f64> = g.pins.iter().flat_map(|q| q.iter().copied()).collect();
    Ok((g.group.into_pyarray(py), g.field.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((nf, 4), fields).expect("fields").into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((g.members.len(), 3), mem).expect("members").into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((g.pins.len(), 4), pins).expect("pins").into_pyarray(py)))
}

fn wendland(r: f64, radius: f64) -> f64 {
    let q = r / radius;
    if q >= 1.0 {
        0.0
    } else {
        let a = 1.0 - q;
        a * a * a * a * (4.0 * q + 1.0)
    }
}

/// The weights w of sum_k w_k phi(|x - pin_k|) (phi Wendland C2 of radius) taking the values resid at the pins
/// (Gaussian elimination, partial pivoting; pins nearer than a millimetre share their first's weight). Pure.
pub fn pin_weights_core(pins: &[[f64; 2]], resid: &[f64], radius: f64) -> Vec<f64> {
    let k = pins.len();
    let mut a = vec![0.0f64; k * k];
    let mut b = resid.to_vec();
    for i in 0..k {
        for j in 0..k {
            a[i * k + j] = wendland((pins[i][0] - pins[j][0]).dhypot(pins[i][1] - pins[j][1]), radius);
        }
        a[i * k + i] += 1e-9;
    }
    let mut perm: Vec<usize> = (0..k).collect();
    for c in 0..k {
        let mut best = c;
        for r in c + 1..k {
            if a[perm[r] * k + c].abs() > a[perm[best] * k + c].abs() {
                best = r;
            }
        }
        perm.swap(c, best);
        let pr = perm[c];
        let d = a[pr * k + c];
        if d.abs() < 1e-12 {
            continue;
        }
        for r in c + 1..k {
            let rr = perm[r];
            let f = a[rr * k + c] / d;
            if f == 0.0 {
                continue;
            }
            for j in c..k {
                a[rr * k + j] -= f * a[pr * k + j];
            }
            b[rr] -= f * b[pr];
        }
    }
    let mut w = vec![0.0f64; k];
    for c in (0..k).rev() {
        let pr = perm[c];
        let mut s = b[pr];
        for j in c + 1..k {
            s -= a[pr * k + j] * w[j];
        }
        let d = a[pr * k + c];
        w[c] = if d.abs() < 1e-12 { 0.0 } else { s / d };
    }
    w
}

pub fn pin_eval_core(xy: &[[f64; 2]], pins: &[[f64; 2]], w: &[f64], radius: f64) -> Vec<f64> {
    xy.iter().map(|q| {
        pins.iter().zip(w).map(|(p, &wk)| wk * wendland((q[0] - p[0]).dhypot(q[1] - p[1]), radius)).sum()
    }).collect()
}

/// field_pins: with resid given, the weights (k,) at pins (k, 2); with xy given instead (resid empty), the correction
/// at xy (m, 2) of weights w. One function: mode 0 solve (pins, resid), mode 1 eval (pins, w as resid, xy).
#[pyfunction]
pub fn field_pins<'py>(py: Python<'py>, pins: PyReadonlyArray2<'py, f64>, vals: PyReadonlyArray1<'py, f64>,
                       xy: PyReadonlyArray2<'py, f64>, radius: f64, mode: i64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (pins, vals, xy) = (pins.as_array(), vals.as_array(), xy.as_array());
    if pins.ncols() != 2 || vals.len() != pins.nrows() || xy.ncols() != 2 || !(radius > 0.0) {
        return Err(pyo3::exceptions::PyValueError::new_err("field_pins: mismatched arrays"));
    }
    let p: Vec<[f64; 2]> = (0..pins.nrows()).map(|i| [pins[[i, 0]], pins[[i, 1]]]).collect();
    let v: Vec<f64> = vals.iter().copied().collect();
    let got = if mode == 0 {
        py.detach(|| pin_weights_core(&p, &v, radius))
    } else {
        let q: Vec<[f64; 2]> = (0..xy.nrows()).map(|i| [xy[[i, 0]], xy[[i, 1]]]).collect();
        py.detach(|| pin_eval_core(&q, &p, &v, radius))
    };
    Ok(got.into_pyarray(py))
}

/// The junction pads to merge (roads.deck_pads): joint jp[i] of incident part i of deck dk[i] (-1: a ground road) of
/// length dl[i]; padded[j] whether joint j has a pad. Joints with pads that a deck no longer than max_m links (its
/// two ends at padded joints) are one component: per joint its component's smallest joint, -1 alone or unpadded.
pub fn pad_links_core(jp: &[usize], dk: &[i64], dl: &[f64], padded: &[bool], max_m: f64) -> Vec<i64> {
    let nj = padded.len();
    let mut par: Vec<usize> = (0..nj).collect();
    let mut first: std::collections::BTreeMap<i64, usize> = std::collections::BTreeMap::new();
    for i in 0..jp.len() {
        if dk[i] < 0 || !(dl[i] <= max_m) || jp[i] >= nj || !padded[jp[i]] {
            continue;
        }
        match first.get(&dk[i]) {
            Some(&j) => union(&mut par, j, jp[i]),
            None => {
                first.insert(dk[i], jp[i]);
            }
        }
    }
    let mut size = vec![0usize; nj];
    for j in 0..nj {
        if padded[j] {
            let r = find(&mut par, j);
            size[r] += 1;
        }
    }
    (0..nj).map(|j| {
        if !padded[j] {
            return -1;
        }
        let r = find(&mut par, j);
        if size[r] > 1 { r as i64 } else { -1 }
    }).collect()
}

#[pyfunction]
pub fn pad_links<'py>(py: Python<'py>, jp: PyReadonlyArray1<'py, i64>, dk: PyReadonlyArray1<'py, i64>,
                      dl: PyReadonlyArray1<'py, f64>, padded: PyReadonlyArray1<'py, i64>, max_m: f64)
                      -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (jp, dk, dl, padded) = (jp.as_array(), dk.as_array(), dl.as_array(), padded.as_array());
    if dk.len() != jp.len() || dl.len() != jp.len() || jp.iter().any(|&v| v < 0) {
        return Err(pyo3::exceptions::PyValueError::new_err("pad_links: mismatched arrays"));
    }
    let a: Vec<usize> = jp.iter().map(|&v| v as usize).collect();
    let b: Vec<i64> = dk.iter().copied().collect();
    let c: Vec<f64> = dl.iter().copied().collect();
    let d: Vec<bool> = padded.iter().map(|&v| v != 0).collect();
    Ok(pad_links_core(&a, &b, &c, &d, max_m).into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn part(a: [f64; 3], b: [f64; 3]) -> ([f64; 5], [f64; 5]) {
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let l = dx.dhypot(dy);
        ([a[0], a[1], a[2], dx / l, dy / l], [b[0], b[1], b[2], -dx / l, -dy / l])
    }

    #[test]
    fn two_joints_one_cluster() {
        // joints J1 (0, 0) and J2 (10, 0) linked by a part; two arms more at each: one cluster, eight pins (the four
        // arms' far ends, centre and edges... 4 arms x 3)
        let pts = [([0.0, 0.0, 10.0], [10.0, 0.0, 10.2]), ([0.0, 0.0, 10.0], [0.0, 20.0, 9.0]),
                   ([0.0, 0.0, 10.0], [0.0, -20.0, 9.0]), ([10.0, 0.0, 10.2], [10.0, 20.0, 9.2]),
                   ([10.0, 0.0, 10.2], [10.0, -20.0, 9.2])];
        let mut ends = Vec::new();
        let mut bb = Vec::new();
        for (a, b) in pts {
            let (e0, e1) = part(a, b);
            ends.push(e0);
            ends.push(e1);
            bb.push([a[0].min(b[0]), a[1].min(b[1]), a[0].max(b[0]), a[1].max(b[1])]);
        }
        let n = pts.len();
        let g = junction_groups_core(&ends, &vec![15.0; n], &vec![4.0; n], &vec![false; n], &bb,
                                     &[0.75, 0.75, 60.0, 8.0, 1.0, 40.0, 150.0]);
        assert_eq!(g.kind, vec![1]);
        assert!(g.field.iter().all(|&f| f == 0));
        assert_eq!(g.pins.len(), 12);
        // off: no field (each joint's parts shared with the other)
        let g = junction_groups_core(&ends, &vec![15.0; n], &vec![4.0; n], &vec![false; n], &bb,
                                     &[0.75, 0.75, 60.0, 8.0, 0.0, 40.0, 150.0]);
        assert!(g.kind.is_empty());
    }

    #[test]
    fn pins_are_met() {
        let pins = [[0.0, 0.0], [3.0, 0.0], [0.0, 4.0]];
        let r = [0.1, -0.2, 0.05];
        let w = pin_weights_core(&pins, &r, 8.0);
        let got = pin_eval_core(&pins, &pins, &w, 8.0);
        for (a, b) in got.iter().zip(r) {
            assert!((a - b).abs() < 1e-6);
        }
        assert_eq!(pin_eval_core(&[[20.0, 0.0]], &pins, &w, 8.0)[0], 0.0);
    }

    #[test]
    fn pads_linked_by_a_short_deck() {
        // joints 0, 1 linked by deck 5 (12 m); joint 2 by deck 6 (40 m): too long
        let got = pad_links_core(&[0, 1, 1, 2, 0], &[5, 5, 6, 6, -1], &[12.0, 12.0, 40.0, 40.0, 0.0],
                                 &[true, true, true], 24.0);
        assert_eq!(got, vec![0, 0, -1]);
    }
}
