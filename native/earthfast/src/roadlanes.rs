//! Lane evidence (server roadlanes.py generated_counts) and the lane flow over a cell and the eight round it (server
//! roads.py _lane_flow_local): their per-feature loops were Python, ~8 ms per roads layer and ~10 ms per 3 x 3 solve
//! (a dense Manhattan road tile asked ~370 layers and ~160 solves, 2026-11-08). The Python reference these match
//! output for output is server/tests/lanes_reference.py.
use numpy::{IntoPyArray, PyArray1, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::{PyIndexError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyFloat;
use std::collections::{BTreeSet, HashMap, HashSet};

use crate::tapers::{lane_flow_core, NodeKey};

const MAX_LANES: f64 = 12.0;    // roadlanes.MAX_LANES

/// A fast hasher for the kernels' lookup maps (rustc's FxHash: a rotate, xor and multiply per word): SipHash was most
/// of lane_flow_cells (15k road ids and 30k road ends a solve). Only for maps that are looked up, never iterated in
/// an order the output depends on.
#[derive(Default, Clone, Copy)]
pub(crate) struct Fx(u64);

impl Fx {
    #[inline]
    fn add(&mut self, w: u64) {
        self.0 = (self.0.rotate_left(5) ^ w).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }
}

impl std::hash::Hasher for Fx {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut it = bytes.chunks_exact(8);
        for c in &mut it {
            self.add(u64::from_le_bytes([c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]]));
        }
        let r = it.remainder();
        if !r.is_empty() {
            let mut b = [0u8; 8];
            b[..r.len()].copy_from_slice(r);
            self.add(u64::from_le_bytes(b));
        }
    }
    #[inline]
    fn write_u32(&mut self, i: u32) {
        self.add(i as u64);
    }
    #[inline]
    fn write_u64(&mut self, i: u64) {
        self.add(i);
    }
    #[inline]
    fn write_i64(&mut self, i: i64) {
        self.add(i as u64);
    }
    #[inline]
    fn write_usize(&mut self, i: usize) {
        self.add(i as u64);
    }
    #[inline]
    fn finish(&self) -> u64 {
        self.0.rotate_left(26)      // (the multiply mixes upward: its high bits to the low ones the table indexes by)
    }
}

pub(crate) type FxBuild = std::hash::BuildHasherDefault<Fx>;

/// A road id's code points as a set key: equal as the strings, hashed by their length and last 12 code points (GERS
/// ids are UUIDs: the tail is random; equal ids hash equal, so this only spreads the buckets).
#[derive(PartialEq, Eq)]
struct IdKey<'a>(&'a [u32]);

impl std::hash::Hash for IdKey<'_> {
    fn hash<H: std::hash::Hasher>(&self, h: &mut H) {
        h.write_usize(self.0.len());
        for &c in &self.0[self.0.len().saturating_sub(12)..] {
            h.write_u32(c);
        }
    }
}

/// roadlanes.count of a float: a whole count from minimum to MAX_LANES, else None.
fn count_f(n: f64, minimum: f64) -> Option<i64> {
    if n.is_finite() && n.fract() == 0.0 && minimum <= n && n <= MAX_LANES { Some(n as i64) } else { None }
}

/// roadlanes.count of any Python value: float(value) (a str parsed as Python parses it), None where that raises
/// TypeError / ValueError; any other error is raised (as the Python's).
fn count_obj(py: Python<'_>, v: &Bound<'_, PyAny>, minimum: f64) -> PyResult<Option<i64>> {
    if v.is_none() {
        return Ok(None);
    }
    if let Ok(f) = v.downcast::<PyFloat>() {
        return Ok(count_f(f.value(), minimum));
    }
    match py.get_type::<PyFloat>().call1((v,)) {
        Ok(f) => Ok(count_f(f.downcast::<PyFloat>()?.value(), minimum)),
        Err(e) if e.is_instance_of::<PyTypeError>(py) || e.is_instance_of::<PyValueError>(py) => Ok(None),
        Err(e) => Err(e),
    }
}

/// An attrs column of counts: absent (the Python's [None] * n), float64 values, or any sequence.
enum Col<'py> {
    Missing,
    F64(Vec<f64>),
    Seq(Bound<'py, PyAny>),
}

impl<'py> Col<'py> {
    fn new(c: Option<Bound<'py, PyAny>>) -> Col<'py> {
        match c {
            None => Col::Missing,
            Some(c) => match c.downcast::<PyArray1<f64>>() {
                Ok(a) => Col::F64(a.readonly().as_array().to_vec()),
                Err(_) => Col::Seq(c),
            },
        }
    }

    /// count(column[i], minimum)
    fn count(&self, py: Python<'py>, i: usize, minimum: f64) -> PyResult<Option<i64>> {
        match self {
            Col::Missing => Ok(None),
            Col::F64(v) => match v.get(i) {
                Some(&x) => Ok(count_f(x, minimum)),
                None => Err(PyIndexError::new_err("index out of range")),
            },
            Col::Seq(s) => count_obj(py, &s.get_item(i)?, minimum),
        }
    }
}

/// One end of a highway piece (roadlanes.continuations): its point and unit tangent into the piece (m, from the
/// layer's origin), its connectors' ids there (sorted), its flow (+1 leaving / -1 arriving / 0 two-way).
struct End<'a> {
    p: [f64; 2],
    t: [f64; 2],
    ids: Vec<&'a [u32]>,
    flow: i64,
}

fn disjoint(a: &[&[u32]], b: &[&[u32]]) -> bool {
    !a.iter().any(|x| b.binary_search(x).is_ok())
}

/// A str's code points without NumPy's zero padding ('a' and 'a\0' are one string to it).
fn trim(s: &[u32]) -> &[u32] {
    let mut e = s.len();
    while e > 0 && s[e - 1] == 0 {
        e -= 1;
    }
    &s[..e]
}

/// The rows of a C-contiguous (n, w) UCS-4 array, each trimmed.
fn str_rows<'a>(a: &'a numpy::ndarray::ArrayView2<u32>, what: &str) -> PyResult<Vec<&'a [u32]>> {
    let (n, w) = (a.nrows(), a.ncols());
    if n == 0 || w == 0 {
        return Ok(vec![&[][..]; n]);
    }
    match a.as_slice() {
        Some(s) => Ok((0..n).map(|r| trim(&s[r * w..(r + 1) * w])).collect()),
        None => Err(PyValueError::new_err(format!("{what}: str rows must be C-contiguous"))),
    }
}

/// Per feature: roadlanes.generated_counts before rampoverrides (-1: None). base, total, fwd, bwd, both: the attrs
/// columns lane_count (else osm_lanes), osm_lane_total, osm_lanes_forward, osm_lanes_backward, osm_lanes_both_ways
/// (None: absent); oneway / oneway_has: the oneway rule's major value per feature and whether it has one; eligible:
/// a motorway / trunk, not a ramp nor a roundabout; hw: its class code (1 motorway, 2 trunk); the layer's geometry
/// (part_off, ring_off, coord_off, lonlat) with origin and scale (m per degree) of its first point; the connectors
/// rule's rows (feature, start as f64, value as UCS-4 code points (rows, w), zero-padded as NumPy pads a str array).
#[pyfunction]
#[pyo3(signature = (base, total, fwd, bwd, both, oneway, oneway_has, eligible, hw, part_off, ring_off, coord_off,
                    lonlat, origin, scale, con_feature, con_start, con_value))]
pub fn generated_lanes<'py>(py: Python<'py>, base: Option<Bound<'py, PyAny>>, total: Option<Bound<'py, PyAny>>,
                            fwd: Option<Bound<'py, PyAny>>, bwd: Option<Bound<'py, PyAny>>,
                            both: Option<Bound<'py, PyAny>>, oneway: PyReadonlyArray1<'py, f64>,
                            oneway_has: PyReadonlyArray1<'py, bool>, eligible: PyReadonlyArray1<'py, bool>,
                            hw: PyReadonlyArray1<'py, i64>, part_off: PyReadonlyArray1<'py, i64>,
                            ring_off: PyReadonlyArray1<'py, i64>, coord_off: PyReadonlyArray1<'py, i64>,
                            lonlat: PyReadonlyArray2<'py, f64>, origin: (f64, f64), scale: (f64, f64),
                            con_feature: PyReadonlyArray1<'py, i64>, con_start: PyReadonlyArray1<'py, f64>,
                            con_value: PyReadonlyArray2<'py, u32>) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (dir, has, elig, hw) = (oneway.as_array(), oneway_has.as_array(), eligible.as_array(), hw.as_array());
    let n = elig.len();
    if dir.len() != n || has.len() != n || hw.len() != n {
        return Err(PyValueError::new_err("generated_lanes: per-feature arrays of different lengths"));
    }
    let (base, total, fwd, bwd, both) = (Col::new(base), Col::new(total), Col::new(fwd), Col::new(bwd), Col::new(both));
    // the map's counts: the way's own (or PR #471's geometry match), the lane tags' total, else its direction's
    let mut counts: Vec<Option<i64>> = Vec::with_capacity(n);
    for i in 0..n {
        counts.push(base.count(py, i, 1.0)?);
    }
    for i in 0..n {
        if let Some(t) = total.count(py, i, 1.0)? {
            counts[i] = Some(t);
        } else if counts[i].is_none() {
            let f = fwd.count(py, i, 0.0)?;
            let b = bwd.count(py, i, 0.0)?;
            let bo = both.count(py, i, 0.0)?.unwrap_or(0);
            if !has[i] && f.is_some() && b.is_some() {
                counts[i] = count_f((f.unwrap() + b.unwrap() + bo) as f64, 1.0);
            } else if has[i] {
                counts[i] = (if dir[i] > 0.0 { f } else { b }).and_then(|k| count_f(k as f64, 1.0));
            }
        }
    }
    let missing: Vec<usize> = (0..n).filter(|&i| elig[i] && counts[i].is_none()).collect();
    if !missing.is_empty() {
        let joins = continuations(&dir, &has, &elig, &hw, &part_off.as_array(), &ring_off.as_array(),
                                  &coord_off.as_array(), &lonlat.as_array(), origin, scale, &con_feature.as_array(),
                                  &con_start.as_array(), &con_value.as_array())?;
        // bounded_components: missing chains with exactly two known ends that agree take their count
        let cand: HashSet<usize> = missing.iter().copied().collect();
        let mut unseen: BTreeSet<usize> = missing.iter().copied().collect();
        while let Some(&start) = unseen.iter().next() {
            let mut component: HashSet<usize> = HashSet::new();
            let mut order: Vec<usize> = Vec::new();
            let (mut pending, mut outside, mut closed) = (vec![start], Vec::new(), true);
            while let Some(i) = pending.pop() {
                if !component.insert(i) {
                    continue;
                }
                order.push(i);
                for end in 0..2usize {
                    match joins.get(&(i, end)) {
                        None => closed = false,
                        Some(&(j, _)) => {
                            if cand.contains(&j) { pending.push(j) } else { outside.push(j) }
                        }
                    }
                }
            }
            for c in &order {
                unseen.remove(c);
            }
            if closed && outside.len() == 2 && outside[0] != outside[1] {
                let agreed = counts[outside[0]];
                if agreed.is_some() && agreed == counts[outside[1]] {
                    for &c in &order {
                        counts[c] = agreed;
                    }
                }
            }
        }
    }
    Ok(counts.into_iter().map(|c| c.unwrap_or(-1)).collect::<Vec<i64>>().into_pyarray(py))
}

/// roadlanes.continuations: unique reciprocal matches at aligned ends, respecting flow and connectors; {(feature,
/// end): (feature, end)}. The same arithmetic as the NumPy (no fused multiply-adds: x * x + y * y, as its dot).
#[allow(clippy::too_many_arguments)]
fn continuations(dir: &numpy::ndarray::ArrayView1<f64>, has: &numpy::ndarray::ArrayView1<bool>,
                 elig: &numpy::ndarray::ArrayView1<bool>, hw: &numpy::ndarray::ArrayView1<i64>,
                 po: &numpy::ndarray::ArrayView1<i64>, ro: &numpy::ndarray::ArrayView1<i64>,
                 co: &numpy::ndarray::ArrayView1<i64>, ll: &numpy::ndarray::ArrayView2<f64>, origin: (f64, f64),
                 scale: (f64, f64), con_feature: &numpy::ndarray::ArrayView1<i64>,
                 con_start: &numpy::ndarray::ArrayView1<f64>, con_value: &numpy::ndarray::ArrayView2<u32>)
                 -> PyResult<HashMap<(usize, usize), (usize, usize)>> {
    let n = elig.len();
    let values = str_rows(con_value, "generated_lanes")?;
    if values.len() != con_feature.len() || con_start.len() != con_feature.len() {
        return Err(PyValueError::new_err("generated_lanes: connector arrays of different lengths"));
    }
    // the connectors rule's rows of each eligible feature
    let mut con_rows: HashMap<usize, Vec<usize>> = HashMap::new();
    for r in 0..con_feature.len() {
        let f = con_feature[r];
        if f >= 0 && (f as usize) < n && elig[f as usize] {
            con_rows.entry(f as usize).or_default().push(r);
        }
    }
    let mut ends: HashMap<(usize, usize), End> = HashMap::new();
    let mut order: Vec<(usize, usize)> = Vec::new();
    let mut buckets: HashMap<(i64, i64), Vec<(usize, usize)>> = HashMap::new();
    for i in 0..n {
        if !elig[i] {
            continue;
        }
        let (p0, p1) = (po[i] as usize, po[i + 1] as usize);
        if p1 - p0 != 1 {
            continue;
        }
        let (r0, r1) = (ro[p0] as usize, ro[p0 + 1] as usize);
        if r1 - r0 != 1 {
            continue;
        }
        let (c0, c1) = (co[r0] as usize, co[r0 + 1] as usize);
        let raw: Vec<[f64; 2]> = (c0..c1).map(|k| [(ll[[k, 0]] - origin.0) * scale.0, (ll[[k, 1]] - origin.1) * scale.1])
            .collect();
        if raw.len() < 2 {
            continue;
        }
        let mut line = vec![raw[0]];
        for k in 1..raw.len() {
            let (dx, dy) = (raw[k][0] - raw[k - 1][0], raw[k][1] - raw[k - 1][1]);
            if (dx * dx + dy * dy).sqrt() > 0.01 {
                line.push(raw[k]);
            }
        }
        if line.len() < 2 {
            continue;
        }
        let m = line.len();
        let sign = if !has[i] {
            0
        } else {
            let d = dir[i];
            if d.is_nan() {
                return Err(PyValueError::new_err("cannot convert float NaN to integer"));
            }
            if d > 0.0 { 1 } else if d < 0.0 { -1 } else { 0 }
        };
        for (end, other) in [(0usize, 1usize), (1usize, m - 2)] {
            let at = if end == 0 { 0 } else { m - 1 };
            let mut t = [line[other][0] - line[at][0], line[other][1] - line[at][1]];
            let nrm = (t[0] * t[0] + t[1] * t[1]).sqrt();
            t = [t[0] / nrm, t[1] / nrm];
            let mut ids: Vec<&[u32]> = con_rows.get(&i).map_or_else(Vec::new, |rows| {
                rows.iter().filter(|&&r| (con_start[r] - end as f64).abs() < 0.001).map(|&r| values[r]).collect()
            });
            ids.sort_unstable();
            ids.dedup();
            let flow = sign * if end == 1 { 1 } else { -1 };
            let p = line[at];
            ends.insert((i, end), End { p, t, ids, flow });
            order.push((i, end));
            buckets.entry((p[0].floor() as i64, p[1].floor() as i64)).or_default().push((i, end));
        }
    }
    let mut choices: HashMap<(usize, usize), (usize, usize)> = HashMap::new();
    for a in &order {
        let e = &ends[a];
        let key = (e.p[0].floor() as i64, e.p[1].floor() as i64);
        let mut near: Vec<(usize, usize)> = Vec::new();
        for dx in -1..=1i64 {
            for dy in -1..=1i64 {
                if let Some(list) = buckets.get(&(key.0 + dx, key.1 + dy)) {
                    for b in list {
                        let o = &ends[b];
                        let (ddx, ddy) = (e.p[0] - o.p[0], e.p[1] - o.p[1]);
                        if a.0 != b.0 && hw[a.0] == hw[b.0] && e.flow == -o.flow
                            && !(!e.ids.is_empty() && !o.ids.is_empty() && disjoint(&e.ids, &o.ids))
                            && (ddx * ddx + ddy * ddy).sqrt() <= 0.75 && e.t[0] * o.t[0] + e.t[1] * o.t[1] < -0.9 {
                            near.push(*b);
                        }
                    }
                }
            }
        }
        if near.len() == 1 {
            choices.insert(*a, near[0]);
        }
    }
    Ok(choices.iter().filter(|(a, b)| choices.get(b) == Some(a)).map(|(a, b)| (*a, *b)).collect())
}

/// roads.py _lane_flow_local's solve over a cell's roads layer and those round it: per layer its road ids as UCS-4
/// (n, w) (a NumPy str array's code points, zero-padded), its roads' first and last points (n, 4: lon, lat, lon, lat),
/// their point counts (n,) and lane_flow attrs rows (n, 4). Each road id once, the first layer's first (as NumPy's
/// unique over the layers in order); returns (n0,) for the first layer's roads: the count found (NaN: none, or the
/// road's id is an earlier row's).
#[pyfunction]
pub fn lane_flow_cells<'py>(py: Python<'py>, ids: Vec<PyReadonlyArray2<'py, u32>>, pts: Vec<PyReadonlyArray2<'py, f64>>,
                            npts: Vec<PyReadonlyArray1<'py, i64>>, attrs: Vec<PyReadonlyArray2<'py, f64>>,
                            max_lanes: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let k = ids.len();
    if pts.len() != k || npts.len() != k || attrs.len() != k {
        return Err(PyValueError::new_err("lane_flow_cells: per-layer lists of different lengths"));
    }
    let idv: Vec<_> = ids.iter().map(|a| a.as_array()).collect();
    let ptv: Vec<_> = pts.iter().map(|a| a.as_array()).collect();
    let npv: Vec<_> = npts.iter().map(|a| a.as_array()).collect();
    let atv: Vec<_> = attrs.iter().map(|a| a.as_array()).collect();
    let mut work = 0usize;
    for l in 0..k {
        let r = idv[l].nrows();
        if ptv[l].nrows() != r || npv[l].len() != r || atv[l].nrows() != r || ptv[l].ncols() < 4 || atv[l].ncols() < 4 {
            return Err(PyValueError::new_err("lane_flow_cells: a layer's arrays of different lengths"));
        }
        work += r * (idv[l].ncols() + 8);
    }
    let n0 = if k > 0 { idv[0].nrows() } else { 0 };
    let rows_of: Vec<Vec<&[u32]>> = idv.iter().map(|a| str_rows(a, "lane_flow_cells")).collect::<PyResult<_>>()?;
    let out = crate::release(py, work, || {
        let key = crate::tapers::node_key;
        // each id once, the first layer's first: its row is kept; of those, the counted roads (a one-way sign, two
        // points or more) go to the solve, the others only block the nodes they end at
        let total: usize = rows_of.iter().map(|r| r.len()).sum();
        let mut seen: HashSet<IdKey, FxBuild> = HashSet::with_capacity_and_hasher(total, Default::default());
        let mut first = vec![false; total];
        let mut counted: Vec<(NodeKey, NodeKey, [f64; 4])> = Vec::new();
        let mut mine: Vec<usize> = Vec::new();         // per counted road: its row in the first layer, or n0
        let mut g = 0;
        for (l, rows) in rows_of.iter().enumerate() {
            let (p, a, np) = (&ptv[l], &atv[l], &npv[l]);
            for (r, id) in rows.iter().enumerate() {
                if seen.insert(IdKey(id)) {
                    first[g] = true;
                    if a[[r, 0]] != 0.0 && np[r] >= 2 {
                        counted.push((key(p[[r, 0]], p[[r, 1]]), key(p[[r, 2]], p[[r, 3]]),
                                      [a[[r, 0]], a[[r, 1]], a[[r, 2]], a[[r, 3]]]));
                        mine.push(if l == 0 { r } else { n0 });
                    }
                }
                g += 1;
            }
        }
        let mut out = vec![f64::NAN; n0];
        if counted.is_empty() {
            return out;
        }
        let mut layers: Vec<(usize, usize, usize)> = Vec::with_capacity(k);     // (layer, first global row, rows)
        let mut base = 0;
        for (l, rows) in rows_of.iter().enumerate() {
            layers.push((l, base, rows.len()));
            base += rows.len();
        }
        let (first, ptv, atv, npv) = (&first, &ptv, &atv, &npv);
        let blockers = layers.into_iter().flat_map(move |(l, b, m)| {
            (0..m).filter(move |&r| first[b + r] && atv[l][[r, 0]] == 0.0 && npv[l][r] >= 2)
                .flat_map(move |r| [key(ptv[l][[r, 0]], ptv[l][[r, 1]]), key(ptv[l][[r, 2]], ptv[l][[r, 3]])])
        });
        let got = lane_flow_core(&counted, blockers, max_lanes);
        for (c, &r) in mine.iter().enumerate() {
            if r < n0 {
                out[r] = got[c];
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}
