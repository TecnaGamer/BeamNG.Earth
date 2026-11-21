//! Road crossings of a rail-bed window. Preserve layer, feature and ring order. A duplicate
//! worn-road id is consumed even if its first copy is unbuilt or has no usable ground ring.
use crate::railindex::box_hit;
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PySlice, PyTuple};

/// Add one layer's crossings. Keep the existing ground-parts and projection providers: the
/// former resolves the cell's current level ranges and the latter also handles non-ENU frames.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rail_crossing_roads<'py>(
    py: Python<'py>,
    boxes: PyReadonlyArray2<'py, f64>,
    near: [f64; 4],
    ids: &Bound<'py, PyAny>,
    specs: Option<&Bound<'py, PyList>>,
    classes: Option<&Bound<'py, PyAny>>,
    eligible: Option<PyReadonlyArray1<'py, bool>>,
    styles: &Bound<'py, PyDict>,
    feats: &Bound<'py, PyAny>,
    frame: &Bound<'py, PyAny>,
    roads: &Bound<'py, PyAny>,
    project: &Bound<'py, PyAny>,
    seen: &Bound<'py, PySet>,
    out: &Bound<'py, PyList>,
) -> PyResult<()> {
    let bounds = boxes.as_array();
    let mask = eligible.as_ref().map(|a| a.as_array());
    if bounds.ncols() != 4 || mask.as_ref().is_some_and(|m| m.len() != bounds.nrows()) {
        return Err(PyValueError::new_err(
            "rail_crossing_roads: inconsistent layer dimensions",
        ));
    }
    let parts = roads.getattr("ground_parts")?;
    let mut unbuilt = None;
    let rings = PyList::empty(py);
    let mut widths = Vec::new();
    for (i, b) in bounds.outer_iter().enumerate() {
        if mask.as_ref().is_some_and(|m| !m[i]) || !box_hit(&[b[0], b[1], b[2], b[3]], &near) {
            continue;
        }
        let fid = ids.get_item(i)?.str()?;
        let (half, class) = if let Some(specs) = specs {
            let spec = specs.get_item(i)?;
            if spec.is_none() || seen.contains(&fid)? {
                continue;
            }
            seen.add(fid)?;
            (spec.getattr("width")?.div(2)?, None)
        } else {
            if seen.contains(&fid)? {
                continue;
            }
            seen.add(fid)?;
            let cls = classes
                .ok_or_else(|| PyValueError::new_err("missing road classes"))?
                .get_item(i)?
                .str()?;
            let base = cls.call_method1("removesuffix", ("_link",))?;
            let style = match styles.get_item(&cls)? {
                Some(st) if st.is_truthy()? => Some(st),
                _ => styles.get_item(&base)?,
            };
            let Some(st) = style else { continue };
            // Make the mask only when the first eligible road would have asked for it.
            if unbuilt.is_none() {
                unbuilt = Some(
                    roads
                        .call_method1("unbuilt_mask", (feats,))?
                        .extract::<PyReadonlyArray1<bool>>()?,
                );
            }
            if *unbuilt
                .as_ref()
                .unwrap()
                .as_array()
                .get(i)
                .ok_or_else(|| PyValueError::new_err("short unbuilt mask"))?
            {
                continue;
            }
            (st.get_item(0)?.div(2.0)?, Some(base))
        };
        for ring in parts.call1((feats, i))?.try_iter()? {
            rings.append(ring?)?;
            widths.push((half.clone(), class.clone()));
        }
    }
    if !rings.is_empty() {
        let lines = project.call1((feats, frame, rings))?;
        for (line, (half, class)) in lines.try_iter()?.zip(widths) {
            let line = line?;
            if let Some(c) = class {
                if line.len()? >= 2 {
                    out.append((line, half, c))?;
                }
            } else {
                out.append((line, half))?;
            }
        }
    }
    Ok(())
}

type Point = [f64; 2];

fn sub(a: Point, b: Point) -> Point {
    [a[0] - b[0], a[1] - b[1]]
}
fn dot(a: Point, b: Point) -> f64 {
    a[0] * b[0] + a[1] * b[1]
}
fn norm(v: Point) -> f64 {
    dot(v, v).sqrt()
}

/// Direction of the first nearest road segment. Re-evaluate at the intersection:
/// at a vertex the segment reported by the crossing kernel need not win NumPy's argmin.
fn direction(road: &[Point], p: Point) -> Point {
    let mut best = f64::INFINITY;
    let mut dir = sub(road[1], road[0]);
    for pair in road.windows(2) {
        let d = sub(pair[1], pair[0]);
        let t = (dot(sub(p, pair[0]), d) / dot(d, d).max(1e-9)).clamp(0.0, 1.0);
        let dist = norm(sub(p, [pair[0][0] + t * d[0], pair[0][1] + t * d[1]]));
        if dist < best || dist.is_nan() {
            best = dist;
            dir = d;
            if dist.is_nan() {
                break;
            }
        }
    }
    dir
}

// The worn-line cut intersects many long, regularly resampled tracks with short streets.
// Bound the *same arithmetic* used by segment_crossings_core, including roundoff at a
// shared vertex. A new geometric segment-box epsilon could discard a valid rounded hit.
pub(super) const RUN_SEGMENTS: usize = 16;

pub(super) struct SegmentRun {
    pub(super) segments: std::ops::Range<usize>,
    min: Point,
    max: Point,
    step: Point,
}

pub(super) fn segment_runs(
    points: numpy::ndarray::ArrayView2<f64>,
    start: usize,
    end: usize,
) -> Vec<SegmentRun> {
    let n = end - start - 1;
    (0..n)
        .step_by(RUN_SEGMENTS)
        .map(|first| {
            let segments = first..(first + RUN_SEGMENTS).min(n);
            let mut run = SegmentRun {
                segments: segments.clone(),
                min: [f64::INFINITY; 2],
                max: [f64::NEG_INFINITY; 2],
                step: [0.0; 2],
            };
            for j in segments {
                for axis in 0..2 {
                    let p = points[[start + j, axis]];
                    run.min[axis] = run.min[axis].min(p);
                    run.max[axis] = run.max[axis].max(p);
                    run.step[axis] = run.step[axis].max((points[[start + j + 1, axis]] - p).abs());
                }
            }
            run
        })
        .collect()
}

impl SegmentRun {
    fn extrema(&self, axis: usize, direction: f64) -> (f64, f64) {
        if direction >= 0.0 {
            (self.min[axis], self.max[axis])
        } else {
            (self.max[axis], self.min[axis])
        }
    }

    pub(super) fn can_cross(&self, ax: f64, ay: f64, dx: f64, dy: f64) -> bool {
        // t = ((ax-px)*dy - (ay-py)*dx) / (rx*dy - ry*dx) must lie in [0,1].
        // Each operation is monotone for the fixed multiplier. The signed extrema bound
        // its rounded numerator, and the triangle inequality bounds its rounded denominator.
        // Use twice that bound so rounding the quotient at 1 can never reject an endpoint.
        // An infinite bound or an indeterminate comparison naturally falls through.
        let (x0, x1) = self.extrema(0, dy);
        let (y0, y1) = self.extrema(1, dx);
        let limit = 2.0 * (self.step[0] * dy.abs() + self.step[1] * dx.abs());
        !((ax - x1) * dy - (ay - y0) * dx > limit || (ax - x0) * dy - (ay - y1) * dx < -limit)
    }
}

struct Packed {
    points: Vec<Point>,
    offsets: Vec<i64>,
}

impl Packed {
    fn new() -> Self {
        Self {
            points: Vec::new(),
            offsets: vec![0],
        }
    }
    fn push(&mut self, line: &Bound<'_, PyAny>) -> PyResult<()> {
        let arr: PyReadonlyArray2<f64> = line.extract()?;
        let a = arr.as_array();
        if a.ncols() != 2 {
            return Err(PyValueError::new_err(
                "rail_cut_worn: expected an (n, 2) line",
            ));
        }
        self.points.extend(a.outer_iter().map(|r| [r[0], r[1]]));
        self.offsets.push(self.points.len() as i64);
        Ok(())
    }
    fn line(&self, i: usize) -> &[Point] {
        &self.points[self.offsets[i] as usize..self.offsets[i + 1] as usize]
    }
    fn view(&self) -> numpy::ndarray::ArrayView2<'_, f64> {
        numpy::ndarray::ArrayView2::from_shape((self.points.len(), 2), self.points.as_flattened())
            .unwrap()
    }
}

/// Only allocate masks for rails with actual intersections. In particular, do not allocate
/// a list for every (rail part, eligible road) pair in each overlapping bed window.
fn masks(
    rails: &Packed,
    roads: &Packed,
    widths: &[f64],
    min_sin: f64,
    margin: f64,
) -> Vec<Vec<bool>> {
    let hits = crate::segment_crossings_scan::<true>(
        rails.view(),
        rails.offsets.as_slice().into(),
        roads.view(),
        roads.offsets.as_slice().into(),
    );
    let mut cut = vec![Vec::new(); rails.offsets.len() - 1];
    let mut previous = None;
    let mut s = Vec::new();
    for (i, j, arc, x, y) in hits {
        let xy = rails.line(i);
        if previous != Some(i) {
            s = crate::railbeds::arc(xy);
            cut[i].resize(xy.len(), false);
            previous = Some(i);
        }
        let k = s
            .partition_point(|v| *v < arc)
            .saturating_sub(1)
            .min(xy.len() - 2);
        let t = sub(xy[k + 1], xy[k]);
        let r = direction(roads.line(j), [x, y]);
        let sin = (t[0] * r[1] - t[1] * r[0]).abs() / (norm(t) * norm(r)).max(1e-9);
        let reach = widths[j] / (if sin < min_sin { min_sin } else { sin }) + margin;
        for (v, flag) in s.iter().zip(&mut cut[i]) {
            *flag |= (*v - arc).abs() <= reach;
        }
    }
    cut
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rail_cut_worn<'py>(
    py: Python<'py>,
    rails: &Bound<'py, PyList>,
    wear: &Bound<'py, PyDict>,
    roads: &Bound<'py, PyList>,
    kept: &Bound<'py, PyAny>,
    normal: &Bound<'py, PyDict>,
    heritage: &Bound<'py, PyDict>,
    ends: Option<&Bound<'py, PySet>>,
    end_key: &Bound<'py, PyAny>,
    min_sin: f64,
    margin: f64,
) -> PyResult<Bound<'py, PyList>> {
    if roads.is_empty() {
        return Ok(rails.clone());
    }
    let mut groups: Vec<(Bound<PyAny>, Vec<usize>)> = Vec::new();
    for (i, row) in rails.iter().enumerate() {
        if !row.get_item(2)?.eq("ground")? {
            continue;
        }
        let fid = row.get_item(0)?;
        let Some(w) = wear.get_item(&fid)? else {
            continue;
        };
        if !w.is_truthy()? {
            continue;
        }
        let table = if kept.contains(fid)? {
            heritage
        } else {
            normal
        };
        let Some(allowed) = table.get_item(w)? else {
            continue;
        };
        if let Some((_, members)) = groups.iter_mut().find(|(a, _)| a.is(&allowed)) {
            members.push(i);
        } else {
            groups.push((allowed, vec![i]));
        }
    }
    let mut cut = vec![Vec::new(); rails.len()];
    for (allowed, members) in groups {
        let mut ground = Packed::new();
        let mut widths = Vec::new();
        for road in roads.iter() {
            if allowed.contains(road.get_item(2)?)? {
                ground.push(&road.get_item(0)?)?;
                widths.push(road.get_item(1)?.extract()?);
            }
        }
        if widths.is_empty() {
            continue;
        }
        let mut parts = Packed::new();
        for &i in &members {
            parts.push(&rails.get_item(i)?.get_item(3)?)?;
        }
        let group_masks = py.detach(|| masks(&parts, &ground, &widths, min_sin, margin));
        for (i, m) in members.into_iter().zip(group_masks) {
            cut[i] = m;
        }
    }
    let out = PyList::empty(py);
    for (row, mask) in rails.iter().zip(cut) {
        if !mask.iter().any(|&b| b) {
            out.append(row)?;
            continue;
        }
        let xy = row.get_item(3)?;
        let mut start = 0;
        for stop in 0..=mask.len() {
            if stop < mask.len() && !mask[stop] {
                continue;
            }
            if stop - start >= 2 {
                let part = xy.get_item(PySlice::new(py, start as isize, stop as isize, 1))?;
                out.append(PyTuple::new(
                    py,
                    [row.get_item(0)?, row.get_item(1)?, row.get_item(2)?, part],
                )?)?;
                if let Some(ends) = ends {
                    if start > 0 {
                        ends.add(end_key.call1((xy.get_item(start)?,))?)?;
                    }
                    if stop < mask.len() {
                        ends.add(end_key.call1((xy.get_item(stop - 1)?,))?)?;
                    }
                }
            }
            start = stop + 1;
        }
    }
    Ok(out)
}
