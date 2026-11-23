//! Conservative natural-cliff detection on a geographic halo, before tile ownership clipping.
use crate::dm::Dm;
use numpy::{ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::cmp::Ordering;
use std::collections::BinaryHeap;

const CORE_CELLS: usize = 6;
const FACE_REACH: f64 = 12.0;
const SCARP_REACH: f64 = 40.0;
const SCARP_WIDTH: f64 = 48.0;
const SCARP_DROP: f64 = 10.0;
const BARE_SCARP_AVERAGE_DEG: f64 = 30.0;
const BARE_SCARP_DROP: f64 = 6.0;
const BARE_SCARP_CORE_DEG: f64 = 40.0;
const BARE_SCARP_SIZE_RATIO: f64 = 0.5;
const PROFILE_STEP: f64 = 4.0;
const PROFILE_PROBE: f64 = 2.0;
const SHORE_HEIGHT: f64 = 6.0;
const WATER_LABEL_REACH: f64 = 24.0;

fn slope(degrees: f64) -> f64 {
    degrees.to_radians().dtan()
}

/// A bounded four-neighbour walk; diagonal corners must never join separate faces.
fn neighbours(i: usize, w: usize, h: usize) -> impl Iterator<Item = usize> {
    let (x, y) = (i % w, i / w);
    [
        (y > 0).then(|| i - w),
        (x > 0).then(|| i - 1),
        (y + 1 < h).then(|| i + w),
        (x + 1 < w).then(|| i + 1),
    ]
    .into_iter()
    .flatten()
}

pub(crate) fn components(mask: &[bool], w: usize, h: usize) -> Vec<Vec<usize>> {
    let mut seen = vec![false; mask.len()];
    let mut result = Vec::new();
    for seed in 0..mask.len() {
        if !mask[seed] || seen[seed] {
            continue;
        }
        let mut cells = vec![seed];
        seen[seed] = true;
        let mut at = 0;
        while at < cells.len() {
            for j in neighbours(cells[at], w, h) {
                if mask[j] && !seen[j] {
                    seen[j] = true;
                    cells.push(j);
                }
            }
            at += 1;
        }
        result.push(cells);
    }
    result
}

/// Separable square dilation in O(n), including radii larger than the grid.
pub(crate) fn grow(mask: &[bool], w: usize, h: usize, radius: usize) -> Vec<bool> {
    let mut horizontal = vec![false; mask.len()];
    let mut out = horizontal.clone();
    for y in 0..h {
        let mut prefix = vec![0usize; w + 1];
        for x in 0..w {
            prefix[x + 1] = prefix[x] + usize::from(mask[y * w + x]);
        }
        for x in 0..w {
            horizontal[y * w + x] =
                prefix[x.saturating_add(radius).saturating_add(1).min(w)] > prefix[x.saturating_sub(radius)];
        }
    }
    for x in 0..w {
        let mut prefix = vec![0usize; h + 1];
        for y in 0..h {
            prefix[y + 1] = prefix[y] + usize::from(horizontal[y * w + x]);
        }
        for y in 0..h {
            out[y * w + x] =
                prefix[y.saturating_add(radius).saturating_add(1).min(h)] > prefix[y.saturating_sub(radius)];
        }
    }
    out
}

#[derive(Clone, Copy, PartialEq)]
struct Step(f64, usize);
impl Eq for Step {}
impl Ord for Step {
    fn cmp(&self, other: &Self) -> Ordering {
        other.0.total_cmp(&self.0).then_with(|| other.1.cmp(&self.1))
    }
}
impl PartialOrd for Step {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

pub(crate) fn extend(seed: &[bool], support: &[bool], w: usize, h: usize, dx: f64, dy: f64) -> Vec<bool> {
    extend_with_reach(seed, support, w, h, dx, dy, FACE_REACH)
}

fn extend_with_reach(seed: &[bool], support: &[bool], w: usize, h: usize, dx: f64, dy: f64, reach: f64) -> Vec<bool> {
    let mut distance = vec![f64::INFINITY; seed.len()];
    let mut queue = BinaryHeap::new();
    for i in 0..seed.len() {
        if seed[i] {
            distance[i] = 0.0;
            if neighbours(i, w, h).any(|j| support[j] && !seed[j]) {
                queue.push(Step(0.0, i));
            }
        }
    }
    while let Some(Step(d, i)) = queue.pop() {
        if d != distance[i] {
            continue;
        }
        for j in neighbours(i, w, h) {
            let next = d + if i / w == j / w { dx } else { dy };
            if support[j] && next <= reach && next < distance[j] {
                distance[j] = next;
                queue.push(Step(next, j));
            }
        }
    }
    distance.iter().map(|d| d.is_finite()).collect()
}

pub(crate) fn sample(z: &[f64], w: usize, h: usize, x: f64, y: f64) -> f64 {
    if !x.is_finite() || !y.is_finite() || x < 0.0 || y < 0.0 || x > (w - 1) as f64 || y > (h - 1) as f64 {
        return f64::NAN;
    }
    let (ix, iy) = ((x.floor() as usize).min(w - 2), (y.floor() as usize).min(h - 2));
    let (fx, fy) = (x - ix as f64, y - iy as f64);
    (z[iy * w + ix] * (1.0 - fx) + z[iy * w + ix + 1] * fx) * (1.0 - fy)
        + (z[(iy + 1) * w + ix] * (1.0 - fx) + z[(iy + 1) * w + ix + 1] * fx) * fy
}

#[pyfunction]
pub fn cliff_components(py: Python<'_>, mask: PyReadonlyArray2<'_, bool>) -> Vec<Vec<usize>> {
    let (h, w) = (mask.shape()[0], mask.shape()[1]);
    let values: Vec<_> = mask.as_array().iter().copied().collect();
    py.detach(|| components(&values, w, h))
}

#[pyfunction]
pub fn cliff_grow<'py>(
    py: Python<'py>,
    mask: PyReadonlyArray2<'py, bool>,
    radius: usize,
) -> Bound<'py, PyArray2<bool>> {
    let (h, w) = (mask.shape()[0], mask.shape()[1]);
    let values: Vec<_> = mask.as_array().iter().copied().collect();
    let out = py.detach(|| grow(&values, w, h, radius));
    Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py)
}

#[pyfunction]
pub fn cliff_extend<'py>(
    py: Python<'py>,
    seed: PyReadonlyArray2<'py, bool>,
    support: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
) -> PyResult<Bound<'py, PyArray2<bool>>> {
    if seed.shape() != support.shape() || !dx.is_finite() || !dy.is_finite() || dx <= 0.0 || dy <= 0.0 {
        return Err(PyValueError::new_err(
            "matching masks and positive finite spacing required",
        ));
    }
    let (h, w) = (seed.shape()[0], seed.shape()[1]);
    let a: Vec<_> = seed.as_array().iter().copied().collect();
    let b: Vec<_> = support.as_array().iter().copied().collect();
    let out = py.detach(|| extend(&a, &b, w, h, dx, dy));
    Ok(Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

struct Thresholds {
    dx: f64,
    dy: f64,
    area: f64,
    minimum: f64,
    city: f64,
    support: f64,
    core_area: f64,
    footprint: f64,
}

fn select(z: &[f64], cover: &[u8], blocked: &[bool], vw: usize, vh: usize, p: &Thresholds) -> Vec<bool> {
    let (w, h) = (vw - 1, vh - 1);
    let n = w * h;
    let city = grow(
        &cover.iter().map(|&c| c == 50).collect::<Vec<_>>(),
        vw,
        vh,
        (p.city / p.dx.min(p.dy)).ceil() as usize,
    );
    let mut gradients = vec![[0.0; 2]; n];
    let mut magnitude = vec![0.0; n];
    let mut eligible = vec![false; n];
    let mut shore = vec![false; n];
    let mut bare = vec![false; n];
    for i in 0..n {
        let v = (i / w) * vw + i % w;
        let corners = [v, v + 1, v + vw, v + vw + 1];
        let gx = ((z[v + 1] - z[v]) + (z[v + vw + 1] - z[v + vw])) / (2.0 * p.dx);
        let gy = ((z[v + vw] - z[v]) + (z[v + vw + 1] - z[v + 1])) / (2.0 * p.dy);
        gradients[i] = [gx, gy];
        magnitude[i] = gx.dhypot(gy);
        let allowed = |j: usize| z[j].is_finite() && !city[j] && matches!(cover[j], 10 | 20 | 30 | 60 | 100);
        eligible[i] = !blocked[i] && corners.iter().all(|&j| allowed(j));
        bare[i] = eligible[i] && corners.iter().all(|&j| cover[j] == 60);
        let lo = corners.iter().map(|&j| z[j]).fold(f64::INFINITY, f64::min);
        let hi = corners.iter().map(|&j| z[j]).fold(f64::NEG_INFINITY, f64::max);
        // Water labels also occur in shadows on inland cliffs. They may only
        // extend an already-confirmed dry face by WATER_LABEL_REACH, never seed one.
        shore[i] = !blocked[i]
            && corners
                .iter()
                .all(|&j| allowed(j) || (cover[j] == 80 && z[j].is_finite() && !city[j]))
            && corners.iter().any(|&j| cover[j] == 80)
            && magnitude[i] >= slope(p.minimum)
            && hi - lo >= SHORE_HEIGHT
            && hi >= SHORE_HEIGHT;
    }
    let cell_area = p.dx * p.dy;
    let required = (p.footprint / cell_area).ceil() as usize;
    let core_required = CORE_CELLS.max((p.core_area / cell_area).ceil() as usize);
    let mask = |angle| {
        (0..n)
            .map(|i| eligible[i] && magnitude[i] >= slope(angle))
            .collect::<Vec<_>>()
    };
    let candidate = mask(p.support);
    let extreme: Vec<_> = (0..n)
        .map(|i| candidate[i] && magnitude[i] >= slope(p.minimum))
        .collect();
    let mut cores = vec![false; n];
    for cells in components(&extreme, w, h) {
        if cells.len() >= core_required {
            for i in cells {
                cores[i] = true;
            }
        }
    }
    let area = |cells: &[usize]| {
        cells
            .iter()
            .map(|&i| cell_area * (1.0 + magnitude[i] * magnitude[i]).sqrt())
            .sum::<f64>()
    };
    let mut keep = vec![false; n];
    for cells in components(&candidate, w, h) {
        if cells.len() >= required && area(&cells) >= p.area && cells.iter().any(|&i| cores[i]) {
            for i in cells {
                keep[i] = true;
            }
        }
    }
    let face_angle = if p.support == 45.0 { 40.0 } else { p.support };
    let support = mask(face_angle);
    let edge = mask(if p.support == 45.0 { 35.0 } else { p.support });
    let mut scarp = vec![false; n];
    for i in 0..n {
        if !support[i] {
            continue;
        }
        let [gx, gy] = gradients[i];
        let (ux, uy) = (gx / magnitude[i], gy / magnitude[i]);
        let mut ends = [(f64::NAN, f64::NAN); 2];
        for (side, sign) in [1.0, -1.0].into_iter().enumerate() {
            for step in 1..=(SCARP_REACH / PROFILE_STEP) as usize {
                let r = step as f64 * PROFILE_STEP;
                let (x, y) = (
                    (i % w) as f64 + 0.5 + sign * ux * r / p.dx,
                    (i / w) as f64 + 0.5 + sign * uy * r / p.dy,
                );
                let sx = (sample(z, vw, vh, x + PROFILE_PROBE / p.dx, y)
                    - sample(z, vw, vh, x - PROFILE_PROBE / p.dx, y))
                    / (2.0 * PROFILE_PROBE);
                let sy = (sample(z, vw, vh, x, y + PROFILE_PROBE / p.dy)
                    - sample(z, vw, vh, x, y - PROFILE_PROBE / p.dy))
                    / (2.0 * PROFILE_PROBE);
                if sx.dhypot(sy) <= slope(30.0) {
                    ends[side] = (r, sample(z, vw, vh, x, y));
                    break;
                }
            }
        }
        let width = ends[0].0 + ends[1].0;
        let drop = ends[0].1 - ends[1].1;
        // Rounded shoulders dilute the profile average on exposed escarpments.
        // The steep core, total drop, bounded width and area checks still apply.
        let average = if bare[i] { BARE_SCARP_AVERAGE_DEG } else { 40.0 };
        let min_drop = if bare[i] { BARE_SCARP_DROP } else { SCARP_DROP };
        scarp[i] = width <= SCARP_WIDTH && drop >= min_drop && drop / width >= slope(average);
    }
    let scarp_core: Vec<_> = (0..n)
        .map(|i| {
            let angle = if bare[i] { BARE_SCARP_CORE_DEG } else { 50.0 };
            scarp[i] && magnitude[i] >= slope(face_angle.max(angle))
        })
        .collect();
    let mut substantial = vec![false; n];
    for cells in components(&scarp_core, w, h) {
        if cells.len() >= CORE_CELLS {
            for i in cells {
                substantial[i] = true;
            }
        }
    }
    let extent = extend(&scarp, &edge, w, h, p.dx, p.dy);
    let mut result = extend(&keep, &edge, w, h, p.dx, p.dy);
    for cells in components(&extent, w, h) {
        // Exposed escarpments often break into short ribs. Retain the same
        // six-cell core and exclusions while admitting smaller bounded faces.
        // Bare ribs may share that core across the same connected face.
        let exposed = cells.iter().all(|&i| bare[i]);
        let ratio = if exposed { BARE_SCARP_SIZE_RATIO } else { 1.0 };

        let core = CORE_CELLS.max((p.core_area * ratio / cell_area).ceil() as usize);
        let footprint_cells = if exposed { cells.len() } else { cells.iter().filter(|&&i| scarp[i]).count() };
        if footprint_cells >= required
            && area(&cells) >= p.area * ratio
            && cells.iter().filter(|&&i| scarp_core[i]).count() >= core
            && (exposed || cells.iter().any(|&i| substantial[i]))
        {
            for i in cells {
                result[i] = true;
            }
        }
    }
    extend_with_reach(&result, &shore, w, h, p.dx, p.dy, WATER_LABEL_REACH)
}

/// Detect on the full halo supplied by the caller; ownership is applied after detection.
#[pyfunction]
pub fn cliff_select<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    classes: PyReadonlyArray2<'py, u8>,
    blocked: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
    min_area: f64,
    min_slope: f64,
    city_buffer: f64,
    support_slope: f64,
    min_core_area: f64,
    min_footprint: f64,
) -> PyResult<Bound<'py, PyArray2<bool>>> {
    let shape = heights.shape();
    let (h, w) = (shape[0], shape[1]);
    if w < 2
        || h < 2
        || classes.shape() != shape
        || blocked.shape() != [h - 1, w - 1]
        || [dx, dy, min_area, min_core_area, min_footprint]
            .iter()
            .any(|v| !v.is_finite() || *v <= 0.0)
        || !city_buffer.is_finite()
        || city_buffer < 0.0
        || !(0.0 < support_slope && support_slope <= min_slope && min_slope < 90.0)
    {
        return Err(PyValueError::new_err(
            "matching vertex grids, cell mask and valid cliff thresholds required",
        ));
    }
    let z: Vec<_> = heights.as_array().iter().copied().collect();
    let c: Vec<_> = classes.as_array().iter().copied().collect();
    let b: Vec<_> = blocked.as_array().iter().copied().collect();
    let p = Thresholds {
        dx,
        dy,
        area: min_area,
        minimum: min_slope,
        city: city_buffer,
        support: support_slope,
        core_area: min_core_area,
        footprint: min_footprint,
    };
    let out = py.detach(|| select(&z, &c, &b, w, h, &p));
    Ok(Array2::from_shape_vec((h - 1, w - 1), out).unwrap().into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn params() -> Thresholds {
        Thresholds {
            dx: 4.0,
            dy: 4.0,
            area: 1024.0,
            minimum: 58.0,
            city: 0.0,
            support: 45.0,
            core_area: 128.0,
            footprint: 256.0,
        }
    }
    #[test]
    fn flat_and_isolated_spikes_do_not_seed_cliffs() {
        let mut z = vec![0.0; 25 * 25];
        let cover = vec![60; z.len()];
        let blocked = vec![false; 24 * 24];
        assert!(!select(&z, &cover, &blocked, 25, 25, &params()).iter().any(|v| *v));
        z[12 * 25 + 12] = 100.0;
        assert!(!select(&z, &cover, &blocked, 25, 25, &params()).iter().any(|v| *v));
    }
    #[test]
    fn exclusions_precede_connectivity() {
        let z: Vec<_> = (0..25 * 25).map(|i| (i % 25) as f64 * 10.0).collect();
        let cover = vec![60; z.len()];
        let mut blocked = vec![false; 24 * 24];
        let a = select(&z, &cover, &blocked, 25, 25, &params());
        assert!(a.iter().all(|v| *v));
        blocked.fill(true);
        assert!(!select(&z, &cover, &blocked, 25, 25, &params()).iter().any(|v| *v));
        assert!(!select(&z, &vec![0; z.len()], &vec![false; 24 * 24], 25, 25, &params())
            .iter()
            .any(|v| *v));
    }
    #[test]
    fn growth_cannot_cross_a_gap_or_wrap() {
        assert_eq!(components(&[true, false, false, true], 2, 2).len(), 2);
        assert_eq!(
            grow(&[true, false, false, false, false, false], 3, 2, 1),
            vec![true, true, false, true, true, false]
        );
        assert_eq!(
            extend(&[true, false, false, false], &[true, false, true, true], 4, 1, 4.0, 4.0),
            vec![true, false, false, false]
        );
    }
}
