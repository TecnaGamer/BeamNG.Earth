//! gradesep.py's glue round its kernels (the road-semantics port, step 6): a layer's crossing lines cut into their
//! pieces (_layer_rows), the pieces' fractions in a cell's frame (_to_frame / _metric) and back (_spans' back), the
//! rows round a cell each id once with their owners in the cell's layer (_rows / _owners), and the road tunnels that
//! are underpasses (_underpasses: the ground read through a callback). As Python they were ~6 % of a cold Midtown
//! road tile (per-feature numpy, string sorts, a union-find), each layer walked again for the nine cells round it.
//! The Python's numbers bit for bit: numpy's pairwise sums and interp, libm's hypot / cos, CPython's math.hypot in the
//! union's distance test, Python's min (the first of equals).

use std::collections::HashMap;

use numpy::ndarray::{ArrayView1, ArrayView2};
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::gradesep::{feature_boxes, np_interp, select_core};
use crate::levels::{feature_length, np_sum};
use crate::{py_hypot, segment_crossings_core};

const BRIDGE: i8 = 1;

/// Python's min(a, b): a unless b < a.
#[inline]
fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's max(a, b): a unless b > a.
#[inline]
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

fn us(a: &ArrayView1<i64>) -> Vec<i64> {
    a.iter().copied().collect()
}

/// gradesep._layer_rows' pieces of every feature (gradesep._pieces): kinds (n,) a feature's crossing line kind (-1
/// none, 0 a drawn road, 1 a drawn railway); its map ranges (roads.tag_level_ranges: tag_off (n + 1,), tag_rng (m, 2),
/// tag_kind (m,): 1 bridge, 2 tunnel). Ground pieces of its kind, its bridges as decks (-1), a railway's short tunnels
/// (2: shorter than short_m by its length, roads._feature_length_m), a road's tunnels (3). Returns (piece_off (n + 1,),
/// pk, pa, pb).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn gradesep_layer_rows<'py>(py: Python<'py>, kinds: PyReadonlyArray1<'py, i64>, tag_off: PyReadonlyArray1<'py, i64>,
                                tag_rng: PyReadonlyArray2<'py, f64>, tag_kind: PyReadonlyArray1<'py, i8>,
                                lonlat: PyReadonlyArray2<'py, f64>, coord_off: PyReadonlyArray1<'py, i64>,
                                ring_off: PyReadonlyArray1<'py, i64>, part_off: PyReadonlyArray1<'py, i64>,
                                min_part: f64, short_m: f64)
                                -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                             Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (kinds, toff, trng, tkind) = (kinds.as_array(), tag_off.as_array(), tag_rng.as_array(), tag_kind.as_array());
    let ll = lonlat.as_array();
    let (co, ro, po) = (us(&coord_off.as_array()), us(&ring_off.as_array()), us(&part_off.as_array()));
    let n = kinds.len();
    if toff.len() != n + 1 || po.len() != n + 1 {
        return Err(PyValueError::new_err("gradesep_layer_rows: a value per feature"));
    }
    let (poff, pk, pa, pb) = py.detach(|| {
        let (mut poff, mut pk, mut pa, mut pb) = (vec![0i64], Vec::new(), Vec::new(), Vec::new());
        for i in 0..n {
            let k = kinds[i];
            if k >= 0 {
                let (t0, t1) = (toff[i] as usize, toff[i + 1] as usize);
                if t1 <= t0 {
                    pk.push(k);
                    pa.push(0.0);
                    pb.push(1.0);
                } else {
                    let mut length: Option<f64> = None;
                    let mut at = 0.0f64;
                    for t in t0..t1 {
                        let (a, b, kk) = (trng[[t, 0]], trng[[t, 1]], tkind[t]);
                        if a - at > min_part {
                            pk.push(k);
                            pa.push(at);
                            pb.push(a);
                        }
                        if kk == BRIDGE {
                            pk.push(-1);
                            pa.push(a);
                            pb.push(b);
                        } else if k == 1 {
                            let l = *length.get_or_insert_with(|| feature_length(&ll, &co, &ro, &po, i));
                            if (b - a) * l < short_m {
                                pk.push(2);
                                pa.push(a);
                                pb.push(b);
                            }
                        } else if k == 0 {
                            pk.push(3);
                            pa.push(a);
                            pb.push(b);
                        }
                        at = pmax(at, b);
                    }
                    if 1.0 - at > min_part {
                        pk.push(k);
                        pa.push(at);
                        pb.push(1.0);
                    }
                }
            }
            poff.push(pk.len() as i64);
        }
        (poff, pk, pa, pb)
    });
    Ok((poff.into_pyarray(py), pk.into_pyarray(py), pa.into_pyarray(py), pb.into_pyarray(py)))
}

/// gradesep._metric: feature i's one ring's arc fractions in lon/lat (sub_polyline's metric) and in the frame, None
/// without a single ring of 2 points or more (or a zero length).
fn metric(ll: &ArrayView2<f64>, xy: &ArrayView2<f64>, co: &[i64], ro: &[i64], po: &[i64], i: usize)
          -> Option<(Vec<f64>, Vec<f64>)> {
    let mut ring = None;
    for p in po[i] as usize..po[i + 1] as usize {
        for r in ro[p] as usize..ro[p + 1] as usize {
            let (a, b) = (co[r] as usize, co[r + 1] as usize);
            if b >= a + 2 {
                if ring.is_some() {
                    return None;
                }
                ring = Some((a, b));
            }
        }
    }
    let (a, b) = ring?;
    let lat: Vec<f64> = (a..b).map(|k| ll[[k, 1]]).collect();
    let c = (np_sum(&lat) / ((b - a) as f64)).to_radians().cos();
    let (mut ss, mut se) = (vec![0.0f64; b - a], vec![0.0f64; b - a]);
    for k in a + 1..b {
        let j = k - a;
        ss[j] = ss[j - 1] + ((ll[[k, 0]] - ll[[k - 1, 0]]) * c).hypot(ll[[k, 1]] - ll[[k - 1, 1]]);
        se[j] = se[j - 1] + (xy[[k, 0]] - xy[[k - 1, 0]]).hypot(xy[[k, 1]] - xy[[k - 1, 1]]);
    }
    let (sl, el) = (ss[b - a - 1], se[b - a - 1]);
    if sl <= 0.0 || el <= 0.0 {
        return None;
    }
    Some((ss.iter().map(|v| v / sl).collect(), se.iter().map(|v| v / el).collect()))
}

/// gradesep._to_frame: the pieces' fractions (pa, pb) in the frame's metric (xy: the layer in the frame), a feature's
/// all where any of its pieces starts or ends inside it.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn gradesep_to_frame<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, xy: PyReadonlyArray2<'py, f64>,
                              coord_off: PyReadonlyArray1<'py, i64>, ring_off: PyReadonlyArray1<'py, i64>,
                              part_off: PyReadonlyArray1<'py, i64>, piece_off: PyReadonlyArray1<'py, i64>,
                              pa: PyReadonlyArray1<'py, f64>, pb: PyReadonlyArray1<'py, f64>)
                              -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (ll, xy) = (lonlat.as_array(), xy.as_array());
    let (co, ro, po) = (us(&coord_off.as_array()), us(&ring_off.as_array()), us(&part_off.as_array()));
    let poff = us(&piece_off.as_array());
    let (mut a, mut b) = (pa.as_array().to_vec(), pb.as_array().to_vec());
    if poff.len() != po.len() || *poff.last().unwrap_or(&0) as usize != a.len() || a.len() != b.len()
        || xy.nrows() != ll.nrows() {
        return Err(PyValueError::new_err("gradesep_to_frame: sizes differ"));
    }
    py.detach(|| {
        let inner = |v: f64| v > 0.0 && v < 1.0;
        for i in 0..po.len().saturating_sub(1) {
            let q = poff[i] as usize..poff[i + 1] as usize;
            if !q.clone().any(|q| inner(a[q]) || inner(b[q])) {
                continue;
            }
            if let Some((m0, m1)) = metric(&ll, &xy, &co, &ro, &po, i) {
                for q in q {
                    a[q] = np_interp(a[q], &m0, &m1);
                    b[q] = np_interp(b[q], &m0, &m1);
                }
            }
        }
    });
    Ok((a.into_pyarray(py), b.into_pyarray(py)))
}

/// _spans' back: fractions xs of features rows in the frame's metric as the level ranges measure them (x outside
/// (0, 1) or no metric: x).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn gradesep_back<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, xy: PyReadonlyArray2<'py, f64>,
                          coord_off: PyReadonlyArray1<'py, i64>, ring_off: PyReadonlyArray1<'py, i64>,
                          part_off: PyReadonlyArray1<'py, i64>, rows: PyReadonlyArray1<'py, i64>,
                          xs: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyArray1<f64>> {
    let (ll, xy) = (lonlat.as_array(), xy.as_array());
    let (co, ro, po) = (us(&coord_off.as_array()), us(&ring_off.as_array()), us(&part_off.as_array()));
    let (rows, xs) = (rows.as_array(), xs.as_array());
    let mut memo: HashMap<usize, Option<(Vec<f64>, Vec<f64>)>> = HashMap::new();
    let out: Vec<f64> = (0..rows.len()).map(|k| {
        let (i, x) = (rows[k] as usize, xs[k]);
        let m = memo.entry(i).or_insert_with(|| metric(&ll, &xy, &co, &ro, &po, i));
        match m {
            Some((m0, m1)) if x > 0.0 && x < 1.0 => np_interp(x, m1, m0),
            _ => x,
        }
    }).collect();
    out.into_pyarray(py)
}

/// One layer for gradesep_rows: (xy (N, 2) in the asking cell's frame, coord_off, ring_off, part_off, its ids as UCS-4
/// rows (n, w): roadlanes.ucs4).
type RowsLayer<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                       PyReadonlyArray1<'py, i64>, PyReadonlyArray2<'py, u32>);

/// An id's code points without the zero padding (numpy's str order is these slices' order).
fn id_of<'a>(ids: &'a ArrayView2<u32>, r: usize) -> &'a [u32] {
    let row = ids.row(r).to_slice().expect("ucs4 rows are contiguous");
    let mut e = row.len();
    while e > 0 && row[e - 1] == 0 {
        e -= 1;
    }
    &row[..e]
}

/// gradesep._rows and _owners: the features of layers (the cell's own first) whose boxes come within near_m of the own
/// layer's box (gradesep_select), each id once (its first layer and row), in their ids' order; with each one's row in
/// the own layer by its id (itself where the own ids ascend strictly, else the last row with it), -1 where none.
/// Returns (row_layer, row, owner), None when the own layer has no box.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn gradesep_rows<'py>(py: Python<'py>, layers: Vec<RowsLayer<'py>>, near_m: f64)
                          -> PyResult<Option<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                              Bound<'py, PyArray1<i64>>)>> {
    let views: Vec<_> = layers.iter().map(|(xy, co, ro, po, ids)| (xy.as_array(), co.as_array(), ro.as_array(),
                                                                    po.as_array(), ids.as_array())).collect();
    for (_, _, _, po, ids) in &views {
        if ids.nrows() + 1 != po.len() {
            return Err(PyValueError::new_err("gradesep_rows: an id per feature"));
        }
    }
    let got = py.detach(|| {
        let boxes: Vec<Vec<[f64; 4]>> = views.iter().map(|(xy, co, ro, po, _)| feature_boxes(xy, co, ro, po)).collect();
        let (off, sel) = select_core(&boxes, near_m)?;
        let mut lay = Vec::with_capacity(sel.len());
        for k in 0..off.len() - 1 {
            for _ in off[k]..off[k + 1] {
                lay.push(k);
            }
        }
        let fid = |t: usize| id_of(&views[lay[t]].4, sel[t] as usize);
        let mut order: Vec<usize> = (0..sel.len()).collect();
        order.sort_by(|&x, &y| fid(x).cmp(fid(y)));         // (stable: each id's first copy leads)
        let mut first = Vec::new();
        for (k, &t) in order.iter().enumerate() {
            if k == 0 || fid(order[k - 1]) != fid(t) {
                first.push(t);
            }
        }
        let own = &views[0].4;
        let n_own = own.nrows();
        let ascending = n_own < 2 || (1..n_own).all(|r| id_of(own, r) > id_of(own, r - 1));
        let mut by_id: HashMap<&[u32], i64> = HashMap::new();
        if !ascending {
            for r in 0..n_own {
                by_id.insert(id_of(own, r), r as i64);
            }
        }
        let (mut rl, mut rr, mut ow) = (Vec::new(), Vec::new(), Vec::new());
        for &t in &first {
            let (l, r) = (lay[t], sel[t]);
            let o = if !ascending {
                by_id.get(fid(t)).copied().unwrap_or(-1)
            } else if l == 0 {
                r
            } else {
                let id = fid(t);
                let (mut lo, mut hi) = (0usize, n_own);         // (np.searchsorted: the first own id >= it)
                while lo < hi {
                    let mid = (lo + hi) / 2;
                    if id_of(own, mid) < id { lo = mid + 1 } else { hi = mid }
                }
                if lo < n_own && id_of(own, lo) == id { lo as i64 } else { -1 }
            };
            rl.push(l as i64);
            rr.push(r);
            ow.push(o);
        }
        Some((rl, rr, ow))
    });
    Ok(got.map(|(rl, rr, ow)| (rl.into_pyarray(py), rr.into_pyarray(py), ow.into_pyarray(py))))
}

/// numpy's nanmax / nanmin of a slice (NaN when all are).
fn nanmax(v: &[f64]) -> f64 {
    v.iter().copied().filter(|x| !x.is_nan()).fold(f64::NAN, |a, b| if a.is_nan() || b > a { b } else { a })
}

fn nanmin(v: &[f64]) -> f64 {
    v.iter().copied().filter(|x| !x.is_nan()).fold(f64::NAN, |a, b| if a.is_nan() || b < a { b } else { a })
}

/// gradesep._underpasses: keep per line (pts rows starts[n]..starts[n + 1]; kinds, groups) False for a road's tunnel
/// (kind 3) no other ground line crosses over away from their ends, or with too much ground over it; prm: TOUCH_M,
/// END_M, PORTAL_OUT_M, SHORT_ROAD_TUNNEL_M, UNDERPASS_COVER_M, roads.TUNNEL_OPEN_COVER, SEP_M; cut_side: CUT_SIDE_M;
/// ground(x (k,), y (k,)) -> z (k,): the DEM at frame points (asked once, when any tunnel is crossed).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn gradesep_underpasses<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, starts: PyReadonlyArray1<'py, i64>,
                                 kinds: PyReadonlyArray1<'py, i64>, groups: PyReadonlyArray1<'py, i64>, prm: Vec<f64>,
                                 cut_side: Vec<f64>, ground: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (xy, st, kinds, groups) = (pts.as_array(), us(&starts.as_array()), kinds.as_array(), groups.as_array());
    if !(prm.len() == 7 || prm.len() == 8) || st.len() != kinds.len() + 1 || groups.len() != kinds.len() {
        return Err(PyValueError::new_err("gradesep_underpasses: sizes differ"));
    }
    let (touch, end_m, portal_out, short_tunnel, under_cover, open_cover, sep) =
        (prm[0], prm[1], prm[2], prm[3], prm[4], prm[5], prm[6]);
    // prm[7] (238): a crossing by an over line's end that another ground line goes on from counts (a junction standing
    // over the tunnel, its arms crossing it within END_M of the node: Vienna's S1 under Rautenweg, GitHub #1083)
    let joined_ends = prm.len() > 7 && prm[7] != 0.0;
    let nl = kinds.len();
    let mut keep = vec![true; nl];
    let cand: Vec<usize> = (0..nl).filter(|&l| kinds[l] == 3).collect();
    if cand.is_empty() {
        return Ok(keep.into_pyarray(py));
    }
    let line = |l: usize| -> Vec<[f64; 2]> { (st[l] as usize..st[l + 1] as usize).map(|k| [xy[[k, 0]], xy[[k, 1]]]).collect() };
    // (asks: crossing points, the two portals, a longer one's sides; at: (cand line, i0, i1, sides, cover))
    let prep = py.detach(|| {
        let lens: Vec<f64> = cand.iter().map(|&c| {
            let p = line(c);
            np_sum(&(1..p.len()).map(|k| (p[k][0] - p[k - 1][0]).hypot(p[k][1] - p[k - 1][1])).collect::<Vec<_>>())
        }).collect();
        let mut parent: Vec<usize> = (0..cand.len()).collect();
        fn find(parent: &mut [usize], mut x: usize) -> usize {
            while parent[x] != x {
                parent[x] = parent[parent[x]];
                x = parent[x];
            }
            x
        }
        let mut grid: HashMap<(i64, i64), Vec<(usize, [f64; 2])>> = HashMap::new();
        for p in 0..cand.len() {
            let lp = line(cand[p]);
            for e in [lp[0], lp[lp.len() - 1]] {
                let (gx, gy) = ((e[0] / touch).floor() as i64, (e[1] / touch).floor() as i64);
                for dx in -1..=1 {
                    for dy in -1..=1 {
                        if let Some(v) = grid.get(&(gx + dx, gy + dy)) {
                            let near: Vec<usize> = v.iter().filter(|(_, f)| py_hypot(e[0] - f[0], e[1] - f[1]) < touch)
                                .map(|(q, _)| *q).collect();
                            for q in near {
                                let rp = find(&mut parent, p);
                                let rq = find(&mut parent, q);
                                parent[rq] = rp;
                            }
                        }
                    }
                }
                grid.entry((gx, gy)).or_default().push((p, e));
            }
        }
        let mut total: HashMap<usize, f64> = HashMap::new();
        for p in 0..cand.len() {
            let r = find(&mut parent, p);
            let t = total.get(&r).copied().unwrap_or(0.0);
            total.insert(r, t + lens[p]);
        }
        let long: Vec<bool> = (0..cand.len()).map(|p| {
            let r = find(&mut parent, p);
            total[&r] >= short_tunnel
        }).collect();
        // the ground lines crossing them: those whose box meets a tunnel's (1 m round)
        let mut over: Vec<usize> = (0..nl).filter(|&l| kinds[l] == 0 || kinds[l] == 1).collect();
        let bx = |l: usize| -> [f64; 4] {
            let (a, b) = (st[l] as usize, st[l + 1] as usize);
            let mut r = [xy[[a, 0]], xy[[a, 1]], xy[[a, 0]], xy[[a, 1]]];
            for k in a + 1..b {
                let (x, y) = (xy[[k, 0]], xy[[k, 1]]);
                r = [nmin(r[0], x), nmin(r[1], y), nmax(r[2], x), nmax(r[3], y)];
            }
            r
        };
        if !over.is_empty() && xy.nrows() > 0 {
            let cb: Vec<[f64; 4]> = cand.iter().map(|&c| bx(c)).collect();
            over.retain(|&o| {
                let ob = bx(o);
                cb.iter().any(|c| ob[0] <= c[2] + 1.0 && ob[2] >= c[0] - 1.0 && ob[1] <= c[3] + 1.0 && ob[3] >= c[1] - 1.0)
            });
        }
        let mut hits: Vec<Vec<(usize, [f64; 2])>> = vec![Vec::new(); cand.len()];
        if !over.is_empty() {
            let (mut lp, mut ls) = (Vec::new(), vec![0i64]);
            for &o in &over {
                for q in line(o) {
                    lp.push(q[0]);
                    lp.push(q[1]);
                }
                ls.push((lp.len() / 2) as i64);
            }
            let (mut gp, mut gs) = (Vec::new(), vec![0i64]);
            for &c in &cand {
                for q in line(c) {
                    gp.push(q[0]);
                    gp.push(q[1]);
                }
                gs.push((gp.len() / 2) as i64);
            }
            let pa = ArrayView2::from_shape((lp.len() / 2, 2), &lp).unwrap();
            let ga = ArrayView2::from_shape((gp.len() / 2, 2), &gp).unwrap();
            for (pi, g, _arc, x, y) in segment_crossings_core(pa, ArrayView1::from(&ls), ga, ArrayView1::from(&gs)) {
                hits[g].push((pi, [x, y]));
            }
        }
        let mut ask: Vec<[f64; 2]> = Vec::new();
        let mut at: Vec<(usize, usize, usize, usize, f64)> = Vec::new();
        let mut drop = Vec::new();
        let end_pts = |l: usize| -> [[f64; 2]; 2] {
            let (a, b) = (st[l] as usize, st[l + 1] as usize - 1);
            [[xy[[a, 0]], xy[[a, 1]]], [xy[[b, 0]], xy[[b, 1]]]]
        };
        // (joined_ends) an over line's end e another ground line goes on from (an end within touch), none of them the
        // tunnel's own road (group g) and no tunnel piece ending within end_m of it (a portal's junction)
        let goes_on = |o: usize, e: [f64; 2], g: i64| -> bool {
            let mut on = false;
            for &q in &over {
                if q == o || st[q + 1] < st[q] + 2 {
                    continue;
                }
                if end_pts(q).iter().any(|f| (f[0] - e[0]).hypot(f[1] - e[1]) < touch) {
                    if groups[q] == g {
                        return false;
                    }
                    on = true;
                }
            }
            on && !cand.iter().any(|&t| st[t + 1] >= st[t] + 2
                                   && end_pts(t).iter().any(|f| (f[0] - e[0]).hypot(f[1] - e[1]) < end_m))
        };
        for (p, &c) in cand.iter().enumerate() {
            let mut ptv: Vec<[f64; 2]> = Vec::new();
            let mut owners: Vec<usize> = Vec::new();
            for &(pi, pt) in &hits[p] {
                let o = over[pi];
                let lo = line(o);
                let ends = [lo[0], lo[lo.len() - 1]];
                let d = pmin((ends[0][0] - pt[0]).hypot(ends[0][1] - pt[1]), (ends[1][0] - pt[0]).hypot(ends[1][1] - pt[1]));
                let through = joined_ends && d < end_m && ends.iter()
                    .all(|e| (e[0] - pt[0]).hypot(e[1] - pt[1]) >= end_m || goes_on(o, *e, groups[c]));
                if groups[o] != groups[c] && (d >= end_m || through) {
                    ptv.push(pt);
                    owners.push(o);
                }
            }
            if ptv.is_empty() {
                drop.push(c);
                continue;
            }
            let lc = line(c);
            let m = lc.len();
            let u0 = [lc[0][0] - lc[1][0], lc[0][1] - lc[1][1]];
            let u1 = [lc[m - 1][0] - lc[m - 2][0], lc[m - 1][1] - lc[m - 2][1]];
            let h0 = pmax(u0[0].hypot(u0[1]), 1e-9);
            let h1 = pmax(u1[0].hypot(u1[1]), 1e-9);
            let portals = [[lc[0][0] + portal_out * u0[0] / h0, lc[0][1] + portal_out * u0[1] / h0],
                           [lc[m - 1][0] + portal_out * u1[0] / h1, lc[m - 1][1] + portal_out * u1[1] / h1]];
            let mut sides: Vec<[f64; 2]> = Vec::new();
            if long[p] {
                for (pt, &o) in ptv.iter().zip(&owners) {
                    let ol = line(o);
                    let k = if ol.len() > 1 {
                        let mut best = 0usize;
                        let mut bd = f64::NAN;
                        for j in 0..ol.len() - 1 {
                            let mx = 0.5 * (ol[j][0] + ol[j + 1][0]) - pt[0];
                            let my = 0.5 * (ol[j][1] + ol[j + 1][1]) - pt[1];
                            let d = mx.hypot(my);
                            // np.argmin: the first minimum, a NaN first of all
                            if j == 0 || (!bd.is_nan() && (d.is_nan() || d < bd)) {
                                best = j;
                                bd = d;
                            }
                        }
                        best
                    } else {
                        0
                    };
                    let nx = (k + 1).min(ol.len() - 1);
                    let t = [ol[nx][0] - ol[k][0], ol[nx][1] - ol[k][1]];
                    let h = pmax(t[0].hypot(t[1]), 1e-9);
                    let t = [t[0] / h, t[1] / h];
                    for &d in &cut_side {
                        sides.push([pt[0] + d * t[0], pt[1] + d * t[1]]);
                    }
                    for &d in &cut_side {
                        sides.push([pt[0] - d * t[0], pt[1] - d * t[1]]);
                    }
                }
            }
            at.push((c, ask.len(), ask.len() + ptv.len(), sides.len(), if long[p] { open_cover } else { under_cover }));
            ask.extend(ptv);
            ask.extend(portals);
            ask.extend(sides);
        }
        (ask, at, drop)
    });
    let (ask, at, drop) = prep;
    for c in drop {
        keep[c] = false;
    }
    if !ask.is_empty() {
        let xs: Vec<f64> = ask.iter().map(|p| p[0]).collect();
        let ys: Vec<f64> = ask.iter().map(|p| p[1]).collect();
        let z: Vec<f64> = ground.call1((xs.into_pyarray(py), ys.into_pyarray(py)))?.extract()?;
        if z.len() != ask.len() {
            return Err(PyValueError::new_err("gradesep_underpasses: a height per point"));
        }
        let nd = cut_side.len();
        for (c, i0, i1, ns, cover) in at {
            if !(nanmax(&z[i0..i1]) - nanmin(&z[i1..i1 + 2]) < cover) {
                keep[c] = false;
            } else if ns > 0 {
                let zs = &z[i1 + 2..i1 + 2 + ns];
                let ncross = ns / (2 * nd);
                let mut any = false;
                for x in 0..ncross {
                    let side_max: Vec<f64> = (0..2).map(|s| nanmax(&zs[(x * 2 + s) * nd..(x * 2 + s + 1) * nd])).collect();
                    if nanmin(&side_max) - z[i0 + x] >= sep {
                        any = true;
                    }
                }
                if !any {
                    keep[c] = false;
                }
            }
        }
    }
    Ok(keep.into_pyarray(py))
}

/// np.minimum / np.maximum's NaN: either NaN gives NaN.
fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a }
}

fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}
