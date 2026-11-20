//! Canonical copies for the fork and lane graphs. A long segment can reach cells beyond
//! the graph's input rings. Discover those cells before choosing a whole copy of the segment.
use std::collections::{BTreeMap, BTreeSet};

use numpy::PyReadonlyArray2;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::dm::Dm;
use crate::roadnet::pyfloat::{tile_xy, MAX_LAT};

/// As mapcells.cells_for_points with no padding. Once there are more than limit
/// distinct cells the caller only uses the original rings.
fn copy_cells(
    ll: numpy::ndarray::ArrayView2<'_, f64>,
    z: u32,
    limit: usize,
) -> Option<BTreeSet<String>> {
    let n = 1_i64 << z;
    let (mut west, mut south, mut east, mut north) = (
        f64::INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NEG_INFINITY,
    );
    for p in ll.rows() {
        west = west.min(p[0]);
        east = east.max(p[0]);
        south = south.min(p[1]);
        north = north.max(p[1]);
    }
    let mut cells = BTreeSet::new();
    if south > MAX_LAT || north < -MAX_LAT {
        return Some(cells);
    }
    let (x0, y0) = tile_xy(north, west, n as f64);
    let (x1, y1) = tile_xy(south, east, n as f64);
    let x0 = x0.floor() as i64;
    let x1 = (x1.ceil() as i64 - 1).clamp(x0, x0 + n - 1);
    let y0 = (y0.floor() as i64).clamp(0, n - 1);
    let y1 = (y1.ceil() as i64 - 1).clamp(y0, n - 1);
    let count = (x1 - x0 + 1) * (y1 - y0 + 1);
    if count <= 4096 {
        if count as usize > limit {
            return None;
        }
        for y in y0..=y1 {
            for x in x0..=x1 {
                cells.insert(format!("{z}/{}/{y}", x.rem_euclid(n)));
            }
        }
    } else {
        // A globe-spanning bbox uses the cells of the individual vertices.
        for p in ll.rows() {
            if p[1].abs() > MAX_LAT {
                continue;
            }
            let (x, y) = tile_xy(p[1], p[0], n as f64);
            cells.insert(format!(
                "{z}/{}/{}",
                (x.floor() as i64).rem_euclid(n),
                (y.floor() as i64).clamp(0, n - 1)
            ));
            if cells.len() > limit {
                return None;
            }
        }
    }
    Some(cells)
}

#[derive(Default)]
struct Copy {
    points: usize,
    parts: Vec<String>,
    cells: Option<BTreeSet<String>>,
}

#[pyclass(frozen)]
pub struct SegmentCell {
    by_id: BTreeMap<String, Copy>,
}

#[pymethods]
impl SegmentCell {
    #[new]
    fn new(
        z: u32,
        parts: Vec<String>,
        lines: Vec<PyReadonlyArray2<'_, f64>>,
        limit: usize,
    ) -> PyResult<Self> {
        if parts.len() != lines.len() || z > 24 {
            return Err(PyValueError::new_err("SegmentCell: inconsistent inputs"));
        }
        let mut by_id = BTreeMap::new();
        for (part, line) in parts.into_iter().zip(lines) {
            let ll = line.as_array();
            if ll.ncols() != 2 || ll.is_empty() || ll.iter().any(|x| !x.is_finite()) {
                return Err(PyValueError::new_err("SegmentCell: invalid lon/lat line"));
            }
            let sid = part.split('|').next().unwrap().to_owned();
            let copy = by_id.entry(sid).or_insert_with(|| Copy {
                cells: Some(BTreeSet::new()),
                ..Default::default()
            });
            copy.points += ll.nrows();
            copy.parts.push(part);
            if let Some(cells) = &mut copy.cells {
                match copy_cells(ll, z, limit) {
                    Some(extra) => {
                        cells.extend(extra);
                        if cells.len() > limit {
                            copy.cells = None;
                        }
                    }
                    None => copy.cells = None,
                }
            }
        }
        for copy in by_id.values_mut() {
            copy.parts.sort();
        }
        Ok(Self { by_id })
    }
}

#[pyclass(frozen)]
pub struct SegmentWindow {
    candidates: BTreeMap<String, BTreeSet<String>>,
    #[pyo3(get)]
    cells: Vec<String>,
}

#[pymethods]
impl SegmentWindow {
    #[new]
    fn new(rings: BTreeMap<String, PyRef<'_, SegmentCell>>, limit: usize) -> Self {
        let mut found = BTreeMap::<String, BTreeSet<String>>::new();
        for (cell, index) in &rings {
            for sid in index.by_id.keys() {
                found.entry(sid.clone()).or_default().insert(cell.clone());
            }
        }
        let mut cells: BTreeSet<String> = rings.keys().cloned().collect();
        for (sid, ring) in &mut found {
            let mut all = ring.clone();
            let short = ring.iter().all(|c| {
                if let Some(extra) = &rings[c].by_id[sid].cells {
                    all.extend(extra.iter().cloned());
                    all.len() <= limit
                } else {
                    false
                }
            });
            if short {
                *ring = all;
            }
            cells.extend(ring.iter().cloned());
        }
        Self {
            candidates: found,
            cells: cells.into_iter().collect(),
        }
    }

    fn choose(
        &self,
        inputs: BTreeMap<String, PyRef<'_, SegmentCell>>,
    ) -> PyResult<Vec<(String, String)>> {
        let mut out = Vec::new();
        for (sid, cells) in &self.candidates {
            let mut best = None;
            for cell in cells {
                let index = inputs
                    .get(cell)
                    .ok_or_else(|| PyValueError::new_err("SegmentWindow: missing cell"))?;
                if let Some(copy) = index.by_id.get(sid) {
                    if best.is_none_or(|(_, b): (&String, &Copy)| copy.points > b.points) {
                        best = Some((cell, copy));
                    }
                }
            }
            if let Some((cell, copy)) = best {
                out.extend(copy.parts.iter().map(|part| (cell.clone(), part.clone())));
            }
        }
        Ok(out)
    }
}

/// The connector's nearest vertex in the feature's own metric. Equal distances
/// (including repeated vertices) take the first index, as numpy.argmin.
#[pyfunction]
pub fn connector_vertices(ll: PyReadonlyArray2<'_, f64>, starts: Vec<f64>) -> PyResult<Vec<usize>> {
    let ll = ll.as_array();
    if ll.ncols() != 2 || ll.is_empty() {
        return Err(PyValueError::new_err("connector_vertices: empty line"));
    }
    Ok(vertices(ll, &starts))
}

pub(super) fn vertices(ll: numpy::ndarray::ArrayView2<'_, f64>, starts: &[f64]) -> Vec<usize> {
    let scale = 111320.0 * ll[[0, 1]].to_radians().dcos();
    let mut shares = vec![0.0; ll.nrows()];
    for i in 1..ll.nrows() {
        let dx = (ll[[i, 0]] - ll[[i - 1, 0]]) * scale;
        let dy = (ll[[i, 1]] - ll[[i - 1, 1]]) * 110574.0;
        shares[i] = shares[i - 1] + dx.dhypot(dy);
    }
    let length = shares[shares.len() - 1];
    if length > 0.0 {
        for s in &mut shares {
            *s /= length;
        }
    }
    starts
        .iter()
        .map(|s| {
            let mut best = 0;
            for i in 1..shares.len() {
                if (shares[i] - s).abs() < (shares[best] - s).abs() {
                    best = i;
                }
            }
            best
        })
        .collect()
}
