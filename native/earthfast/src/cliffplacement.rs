//! Placement on the shared cliff lattice. Geometric ownership is deterministic;
//! Python supplies terrain/imagery samples and serializes the resulting rows.
use crate::dm::Dm;
use numpy::{
    ndarray::{Array2, Array3},
    IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::{BTreeMap, BTreeSet};

const TOE_DROP_M: f64 = 0.25;
const FOOTPRINT_SAMPLES: usize = 8;
const NORMAL_EPSILON: f64 = 1e-9;

/// One companion slot around each outcrop. Measure radius along the face,
/// shortening its horizontal projection on steep ground. Return world XY,
/// companion height and model rank; Python samples terrain at these positions.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_companion_candidates<'py>(
    py: Python<'py>,
    parents: PyReadonlyArray2<'py, f64>,
    ids: PyReadonlyArray2<'py, i64>,
    slot: usize,
    count: usize,
    scale: f64,
    height_min: f64,
    height_max: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    // Parent columns: world x/y, normal x/y/z, outcrop height.
    let (p, ids) = (parents.as_array(), ids.as_array());
    if p.ncols() != 6
        || ids.shape() != [p.nrows(), 2]
        || count == 0
        || slot >= count
        || count > 10
        || !scale.is_finite()
        || scale <= 0.0
        || !height_min.is_finite()
        || !height_max.is_finite()
        || height_min <= 0.0
        || height_max < height_min
    {
        return Err(PyValueError::new_err(
            "parent rows, lattice IDs and valid companion configuration required",
        ));
    }
    let out = py.detach(|| {
        let mut out = Array2::zeros((p.nrows(), 4));
        for i in 0..p.nrows() {
            let hash = |salt| crate::vegclumps::mix(ids[[i, 0]], ids[[i, 1]], salt + slot as u64);
            let angle = (slot as f64 + hash(61)) * std::f64::consts::TAU / count as f64;
            let (cos, sin) = (angle.dcos(), angle.dsin());
            let grade = (p[[i, 2]] * cos + p[[i, 3]] * sin) / p[[i, 4]].max(0.1);
            let radius = p[[i, 5]] * (0.35 + 0.30 * hash(71)) / (1.0 + grade * grade).sqrt();
            out[[i, 0]] = p[[i, 0]] + cos * radius / scale;
            out[[i, 1]] = p[[i, 1]] + sin * radius;
            out[[i, 2]] = height_min + (height_max - height_min) * hash(81);
            out[[i, 3]] = hash(91);
        }
        out
    });
    Ok(out.into_pyarray(py))
}

/// A rock needs four dry, open corners and clearance from built-up land.
/// Use the selector's square dilation so both placement and cliff detection
/// agree on the city buffer, including buffers wider than a tile.
#[pyfunction]
pub fn cliff_allowed_cells<'py>(
    py: Python<'py>,
    classes: PyReadonlyArray2<'py, u8>,
    blocked: PyReadonlyArray2<'py, bool>,
    city_radius: usize,
) -> PyResult<Bound<'py, PyArray2<bool>>> {
    let (classes, blocked) = (classes.as_array(), blocked.as_array());
    let (h, w) = classes.dim();
    if h == 0 || w == 0 || blocked.shape() != [h - 1, w - 1] {
        return Err(PyValueError::new_err(
            "land-cover vertices must bound the blocked cell grid",
        ));
    }
    let out = py.detach(|| {
        let city: Vec<bool> = classes.iter().map(|&c| c == 50).collect();
        let city = crate::cliffselect::grow(&city, w, h, city_radius);
        let safe: Vec<bool> = classes
            .iter()
            .zip(city)
            .map(|(&c, urban)| matches!(c, 10 | 20 | 30 | 60 | 100) && !urban)
            .collect();
        Array2::from_shape_fn((h - 1, w - 1), |(y, x)| {
            !blocked[[y, x]]
                && safe[y * w + x]
                && safe[y * w + x + 1]
                && safe[(y + 1) * w + x]
                && safe[(y + 1) * w + x + 1]
        })
    });
    Ok(out.into_pyarray(py))
}

/// Four world-lattice samples per eligible detection cell. Preserve row-major
/// subcell order and jitter away from edges, so adjacent tiles share candidates.
/// Returns geographic XY, integer lattice IDs, and source-cell row indices.
#[pyfunction]
pub fn cliff_rubble_candidates<'py>(
    py: Python<'py>,
    origins: PyReadonlyArray2<'py, f64>,
    spacing: f64,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray1<i64>>,
)> {
    let origins = origins.as_array();
    if origins.ncols() != 2 || origins.iter().any(|v| !v.is_finite()) || !spacing.is_finite() || spacing <= 0.0 {
        return Err(PyValueError::new_err(
            "finite XY cell origins and positive rubble spacing required",
        ));
    }
    let (points, ids, parents) = py.detach(|| {
        let n = origins.nrows() * 4;
        let mut points = Array2::zeros((n, 2));
        let mut ids = Array2::zeros((n, 2));
        let mut parents = Vec::with_capacity(n);
        for (row, origin) in origins.rows().into_iter().enumerate() {
            let gi = (origin[0] / spacing).round_ties_even() as i64;
            let gj = (origin[1] / spacing).round_ties_even() as i64;
            for sub in 0..4 {
                let i = row * 4 + sub;
                let (x, y) = (gi + (sub % 2) as i64, gj + (sub / 2) as i64);
                ids[[i, 0]] = x;
                ids[[i, 1]] = y;
                points[[i, 0]] = (x as f64 + 0.2 + 0.6 * crate::vegclumps::mix(x, y, 31)) * spacing;
                points[[i, 1]] = (y as f64 + 0.2 + 0.6 * crate::vegclumps::mix(x, y, 32)) * spacing;
                parents.push(row as i64);
            }
        }
        (points, ids, parents)
    });
    Ok((points.into_pyarray(py), ids.into_pyarray(py), parents.into_pyarray(py)))
}

fn rubble_probability(slope: f64, distance: f64, full: f64, maximum: f64, reach: f64, density: f64) -> f64 {
    let t = ((slope - full) / (maximum - full)).clamp(0.0, 1.0);
    let near = (1.0 - distance / reach).clamp(0.0, 1.0);
    density * (1.0 - t * t * (3.0 - 2.0 * t)) * near.dpowf(1.4)
}

/// Evaluate the toe fan after Python supplies terrain heights and gradients.
/// Samples are x/y/z/gx/gy/source-z; seed rows retain x/y/downhill-x/downhill-y
/// and source cell indices. Return selection plus steepness/slope/distance.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_rubble_selection<'py>(
    py: Python<'py>,
    samples: PyReadonlyArray2<'py, f64>,
    seeds: PyReadonlyArray2<'py, f64>,
    ids: PyReadonlyArray2<'py, i64>,
    scale: f64,
    reach: f64,
    scatter: f64,
    full: f64,
    maximum: f64,
    density: f64,
) -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray2<f64>>)> {
    let (s, b, ids) = (samples.as_array(), seeds.as_array(), ids.as_array());
    if s.ncols() != 6
        || b.shape() != [s.nrows(), 6]
        || ids.shape() != [s.nrows(), 2]
        || [scale, reach, scatter, full, maximum, density]
            .iter()
            .any(|v| !v.is_finite())
        || scale <= 0.0
        || reach <= 0.0
        || scatter < 0.0
        || maximum <= full
        || !(0.0..=1.0).contains(&density)
    {
        return Err(PyValueError::new_err(
            "matching six-column samples/seeds, lattice IDs and valid fan parameters required",
        ));
    }
    let (selected, metrics) = py.detach(|| {
        let mut selected = vec![false; s.nrows()];
        let mut metrics = Array2::zeros((s.nrows(), 3));
        for i in 0..s.nrows() {
            let steep = s[[i, 3]].dhypot(s[[i, 4]]);
            let slope = steep.datan().to_degrees();
            let ox = (s[[i, 0]] - b[[i, 0]]) * scale;
            let oy = s[[i, 1]] - b[[i, 1]];
            let along = ox * b[[i, 2]] + oy * b[[i, 3]];
            let across = (ox * b[[i, 3]] - oy * b[[i, 2]]).abs();
            let distance = ox.dhypot(oy);
            let (gi, gj) = (ids[[i, 0]], ids[[i, 1]]);
            // Euclidean division matches Python's floor division west/south
            // of the origin; truncation would shift the large-scale clusters.
            let cluster = 0.45 + 0.55 * crate::vegclumps::mix(gi.div_euclid(4), gj.div_euclid(4), 39);
            selected[i] = along >= -1.0
                && across <= scatter
                && distance < reach
                && s[[i, 2]] <= s[[i, 5]] + 0.5
                && (s[[i, 2]] + steep).is_finite()
                && crate::vegclumps::mix(gi, gj, 33)
                    < rubble_probability(slope, distance, full, maximum, reach, density) * cluster;
            metrics[[i, 0]] = steep;
            metrics[[i, 1]] = slope;
            metrics[[i, 2]] = distance;
        }
        (selected, metrics)
    });
    Ok((selected.into_pyarray(py), metrics.into_pyarray(py)))
}

fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}

fn unit(v: [f64; 3]) -> [f64; 3] {
    let length = v.iter().map(|x| x * x).sum::<f64>().sqrt().max(NORMAL_EPSILON);
    v.map(|x| x / length)
}

/// Samples are east/west/north/south ENU positions at one-metre spacing.
/// Preserve a zero normal on degenerate terrain rather than inventing a slope.
#[pyfunction]
pub fn cliff_surface_normals<'py>(
    py: Python<'py>,
    samples: PyReadonlyArray3<'py, f64>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let s = samples.as_array();
    if s.shape()[1..] != [4, 3] {
        return Err(PyValueError::new_err("N x 4 x 3 surface samples required"));
    }
    let out = py.detach(|| {
        let mut out = Array2::zeros((s.shape()[0], 3));
        for i in 0..out.nrows() {
            let dx = std::array::from_fn(|a| s[[i, 0, a]] - s[[i, 1, a]]);
            let dy = std::array::from_fn(|a| s[[i, 2, a]] - s[[i, 3, a]]);
            let n = unit(cross(dx, dy));
            let sign = if n[2] < 0.0 { -1.0 } else { 1.0 };
            for a in 0..3 {
                out[[i, a]] = n[a] * sign;
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

fn orientation(up: [f64; 3], yaw: f64) -> [[f64; 3]; 2] {
    let heading = [yaw.dcos(), yaw.dsin(), 0.0];
    let dot = heading.iter().zip(up).map(|(a, b)| a * b).sum::<f64>();
    let tangent = unit(std::array::from_fn(|a| heading[a] - up[a] * dot));
    [tangent, cross(up, tangent)]
}

fn seat_coordinate(support: f64, normal: f64, up: f64, depth: f64, pivot_drop: f64) -> f64 {
    support - normal * depth - up * pivot_drop
}

/// Choose an outcrop's size and gravity-limited pose from its world identity.
/// The face normal controls burial, not the tall axis. Pivot correction lowers
/// the visible mass into the face because stock Utah pivots sit below its centre.
/// Return height/yaw/rank/slope rows and position/tangent/bitangent/up frames.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_outcrop_poses<'py>(
    py: Python<'py>,
    support: PyReadonlyArray2<'py, f64>,
    normals: PyReadonlyArray2<'py, f64>,
    ids: PyReadonlyArray2<'py, i64>,
    heights: (f64, f64),
    burial: (f64, f64),
    max_tilt_deg: f64,
    center_height: f64,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray3<f64>>)> {
    let (p, normals, ids) = (support.as_array(), normals.as_array(), ids.as_array());
    if p.ncols() != 3
        || normals.shape() != p.shape()
        || ids.shape() != [p.nrows(), 2]
        || [heights.0, heights.1, burial.0, burial.1, max_tilt_deg, center_height]
            .iter()
            .any(|v| !v.is_finite())
        || heights.0 <= 0.0
        || heights.1 < heights.0
        || burial.0 < 0.0
        || burial.1 < burial.0
        || !(0.0..=90.0).contains(&max_tilt_deg)
        || center_height < 0.0
    {
        return Err(PyValueError::new_err(
            "matching support/normal/ID rows and valid outcrop dimensions required",
        ));
    }
    let (properties, frames) = py.detach(|| {
        let mut properties = Array2::zeros((p.nrows(), 4));
        let mut frames = Array3::zeros((p.nrows(), 4, 3));
        for i in 0..p.nrows() {
            let hash = |salt| crate::vegclumps::mix(ids[[i, 0]], ids[[i, 1]], salt);
            let n = [normals[[i, 0]], normals[[i, 1]], normals[[i, 2]]];
            let slope = n[2].clamp(-1.0, 1.0).dacos().to_degrees();
            let height = heights.0 + (heights.1 - heights.0) * hash(54).dpowf(1.35);
            let yaw = hash(55) * std::f64::consts::TAU;
            let tilt = (slope * 0.12).min(max_tilt_deg).to_radians();
            let horizontal = (n[0] * n[0] + n[1] * n[1]).sqrt().max(NORMAL_EPSILON);
            let up = [
                n[0] / horizontal * tilt.dsin(),
                n[1] / horizontal * tilt.dsin(),
                tilt.dcos(),
            ];
            let basis = orientation(up, yaw);
            let depth = height * (burial.0 + (burial.1 - burial.0) * hash(56));
            for (k, value) in [height, yaw, hash(57), slope].into_iter().enumerate() {
                properties[[i, k]] = value;
            }
            for a in 0..3 {
                frames[[i, 0, a]] = seat_coordinate(p[[i, a]], n[a], up[a], depth, height * center_height);
                frames[[i, 1, a]] = basis[0][a];
                frames[[i, 2, a]] = basis[1][a];
                frames[[i, 3, a]] = up[a];
            }
        }
        (properties, frames)
    });
    Ok((properties.into_pyarray(py), frames.into_pyarray(py)))
}

/// Settle toe rubble on its sampled support. Smaller stones occur farther from
/// the wall; their tilt follows the ground conservatively, and their centre is
/// buried along the ground normal. Metrics are steepness/slope-degrees/distance
/// from cliff_rubble_selection. Return height/yaw/rank and the same pose layout
/// as cliff_outcrop_poses.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_rubble_poses<'py>(
    py: Python<'py>,
    support: PyReadonlyArray2<'py, f64>,
    gradients: PyReadonlyArray2<'py, f64>,
    metrics: PyReadonlyArray2<'py, f64>,
    ids: PyReadonlyArray2<'py, i64>,
    reach: f64,
    max_tilt_deg: f64,
    burial: f64,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray3<f64>>)> {
    let (p, g, m, ids) = (
        support.as_array(),
        gradients.as_array(),
        metrics.as_array(),
        ids.as_array(),
    );
    if p.ncols() != 3
        || g.shape() != [p.nrows(), 2]
        || m.shape() != p.shape()
        || ids.shape() != [p.nrows(), 2]
        || !reach.is_finite()
        || reach <= 0.0
        || !max_tilt_deg.is_finite()
        || !(0.0..=90.0).contains(&max_tilt_deg)
        || !burial.is_finite()
        || burial < 0.0
    {
        return Err(PyValueError::new_err(
            "matching rubble inputs and valid reach, tilt and burial required",
        ));
    }
    let (properties, frames) = py.detach(|| {
        let mut properties = Array2::zeros((p.nrows(), 3));
        let mut frames = Array3::zeros((p.nrows(), 4, 3));
        for i in 0..p.nrows() {
            let hash = |salt| crate::vegclumps::mix(ids[[i, 0]], ids[[i, 1]], salt);
            let height = (0.65 + 1.2 * hash(34)) * (1.0 - 0.45 * m[[i, 2]] / reach);
            let yaw = hash(35) * std::f64::consts::TAU;
            let tilt = (m[[i, 1]].to_radians() * 0.65).min(max_tilt_deg.to_radians());
            let steep = m[[i, 0]];
            let up = [
                -g[[i, 0]] / steep.max(1e-12) * tilt.dsin(),
                -g[[i, 1]] / steep.max(1e-12) * tilt.dsin(),
                tilt.dcos(),
            ];
            let length = (1.0 + steep * steep).sqrt();
            let normal = [-g[[i, 0]] / length, -g[[i, 1]] / length, 1.0 / length];
            let basis = orientation(up, yaw);
            for (k, value) in [height, yaw, hash(36)].into_iter().enumerate() {
                properties[[i, k]] = value;
            }
            for a in 0..3 {
                frames[[i, 0, a]] = seat_coordinate(p[[i, a]], normal[a], up[a], height * burial, 0.0);
                frames[[i, 1, a]] = basis[0][a];
                frames[[i, 2, a]] = basis[1][a];
                frames[[i, 3, a]] = up[a];
            }
        }
        (properties, frames)
    });
    Ok((properties.into_pyarray(py), frames.into_pyarray(py)))
}

/// Seat companions with their parent's upright axis and their own face normal.
#[pyfunction]
pub fn cliff_seat_companions<'py>(
    py: Python<'py>,
    support: PyReadonlyArray2<'py, f64>,
    normals: PyReadonlyArray2<'py, f64>,
    up: PyReadonlyArray2<'py, f64>,
    heights: PyReadonlyArray1<'py, f64>,
    burial: f64,
    center_height: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (p, n, up, h) = (
        support.as_array(),
        normals.as_array(),
        up.as_array(),
        heights.as_array(),
    );
    if p.shape() != [h.len(), 3]
        || n.shape() != p.shape()
        || up.shape() != p.shape()
        || !burial.is_finite()
        || burial < 0.0
        || !center_height.is_finite()
        || center_height < 0.0
    {
        return Err(PyValueError::new_err(
            "matching companion support/normal/up/height rows and nonnegative burial required",
        ));
    }
    Ok(py
        .detach(|| {
            Array2::from_shape_fn((h.len(), 3), |(i, a)| {
                seat_coordinate(p[[i, a]], n[[i, a]], up[[i, a]], h[i] * burial, h[i] * center_height)
            })
        })
        .into_pyarray(py))
}

/// Assign support points, before burial, to exactly one half-open tile.
#[pyfunction]
pub fn cliff_owned<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    bounds: [f64; 4],
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let p = positions.as_array();
    if p.ncols() < 2 || bounds.iter().any(|v| !v.is_finite()) || bounds[2] < bounds[0] || bounds[3] < bounds[1] {
        return Err(PyValueError::new_err(
            "XY support points and ordered finite bounds required",
        ));
    }
    let result = py.detach(|| {
        p.rows()
            .into_iter()
            .map(|r| r[0] >= bounds[0] && r[0] < bounds[2] && r[1] >= bounds[1] && r[1] < bounds[3])
            .collect::<Vec<_>>()
    });
    Ok(result.into_pyarray(py))
}

/// Stable, world-anchored outcrop candidates, sorted by lattice column then row.
/// Output columns: lattice i/j, geographic x/y, source cell i/j. Python uses
/// these only to gather terrain, biome and imagery inputs for later kernels.
#[pyfunction]
pub fn cliff_outcrop_candidates<'py>(
    py: Python<'py>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    keep: PyReadonlyArray2<'py, bool>,
    blocked: PyReadonlyArray2<'py, bool>,
    spacing: f64,
    density: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (xs, ys, keep, blocked) = (xs.as_slice()?, ys.as_slice()?, keep.as_array(), blocked.as_array());
    if xs.len() != keep.ncols() + 1
        || ys.len() != keep.nrows() + 1
        || blocked.shape() != keep.shape()
        || xs.len() < 2
        || ys.len() < 2
        || !spacing.is_finite()
        || spacing <= 0.0
        || !density.is_finite()
        || !(0.0..=1.0).contains(&density)
        || xs.iter().chain(ys).any(|v| !v.is_finite())
        || xs.windows(2).chain(ys.windows(2)).any(|p| p[1] <= p[0])
    {
        return Err(PyValueError::new_err(
            "ordered lattice edges, matching masks, positive spacing and density in [0,1] required",
        ));
    }
    let rows = py.detach(|| {
        let mut buckets = BTreeSet::new();
        for ((j, i), &selected) in keep.indexed_iter() {
            if selected && !blocked[[j, i]] {
                buckets.insert(((xs[i] / spacing).floor() as i64, (ys[j] / spacing).floor() as i64));
            }
        }
        let mut rows = Vec::new();
        for (gi, gj) in buckets {
            if crate::vegclumps::mix(gi, gj, 53) >= density {
                continue;
            }
            let x = (gi as f64 + 0.2 + 0.6 * crate::vegclumps::mix(gi, gj, 51)) * spacing;
            let y = (gj as f64 + 0.2 + 0.6 * crate::vegclumps::mix(gi, gj, 52)) * spacing;
            let (ix, jy) = (xs.partition_point(|v| *v <= x), ys.partition_point(|v| *v <= y));
            if ix == 0 || jy == 0 || ix == xs.len() || jy == ys.len() {
                continue;
            }
            rows.extend([gi as f64, gj as f64, x, y, (ix - 1) as f64, (jy - 1) as f64]);
        }
        Array2::from_shape_vec((rows.len() / 6, 6), rows).unwrap()
    });
    Ok(rows.into_pyarray(py))
}

/// Geographic sample positions around each proposed rock's physical footprint.
/// Input rows are x/y/height; output is N x 8 x 2 for terrain I/O in Python.
#[pyfunction]
pub fn cliff_footprint_queries<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    scale: f64,
) -> PyResult<Bound<'py, PyArray3<f64>>> {
    let p = points.as_array();
    if p.ncols() != 3 || !scale.is_finite() || scale <= 0.0 || p.iter().any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err(
            "finite x/y/height rows and positive horizontal scale required",
        ));
    }
    let out = py.detach(|| {
        let mut out = Array3::zeros((p.nrows(), FOOTPRINT_SAMPLES, 2));
        for k in 0..FOOTPRINT_SAMPLES {
            let angle = k as f64 * std::f64::consts::FRAC_PI_4;
            for (i, point) in p.rows().into_iter().enumerate() {
                let radius = point[2] * 0.5;
                out[[i, k, 0]] = point[0] + radius * angle.dcos() / scale;
                out[[i, k, 1]] = point[1] + radius * angle.dsin();
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

fn footprint_relief(ring: &[[f64; 3]; FOOTPRINT_SAMPLES], normal: [f64; 3]) -> (f64, f64) {
    let dot = |a: [f64; 3], b: [f64; 3]| a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    let distances = ring.map(|point| dot(point, normal));
    let mean = distances.iter().sum::<f64>() / FOOTPRINT_SAMPLES as f64;
    let relief = (distances.iter().map(|d| (d - mean).sq()).sum::<f64>() / FOOTPRINT_SAMPLES as f64).sqrt();
    let mut angles = [0.0; FOOTPRINT_SAMPLES];
    for k in 0..FOOTPRINT_SAMPLES {
        let (a, b) = (ring[k], ring[(k + 1) % FOOTPRINT_SAMPLES]);
        let facet = [
            a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0],
        ];
        let length = dot(facet, facet).sqrt().max(NORMAL_EPSILON);
        let unit = facet.map(|v| v / length);
        angles[k] = dot(unit, normal).abs().clamp(0.0, 1.0).dacos().to_degrees();
    }
    angles.sort_by(f64::total_cmp);
    // Linear 75th percentile of eight facets lies one quarter from index 5 to 6.
    (relief, angles[5] + 0.25 * (angles[6] - angles[5]))
}

/// Reject smooth inclined planes: roughness requires both relief relative to
/// the tangent plane and variation between neighboring footprint facets.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_rough_faces<'py>(
    py: Python<'py>,
    rings: PyReadonlyArray3<'py, f64>,
    positions: PyReadonlyArray2<'py, f64>,
    normals: PyReadonlyArray2<'py, f64>,
    heights: PyReadonlyArray1<'py, f64>,
    min_relief: f64,
    relief_ratio: f64,
    min_spread: f64,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (r, p, n, h) = (
        rings.as_array(),
        positions.as_array(),
        normals.as_array(),
        heights.as_array(),
    );
    if r.shape() != [h.len(), FOOTPRINT_SAMPLES, 3]
        || p.shape() != [h.len(), 3]
        || n.shape() != p.shape()
        || [min_relief, relief_ratio, min_spread]
            .iter()
            .any(|v| !v.is_finite() || *v < 0.0)
    {
        return Err(PyValueError::new_err(
            "matching N x 8 x 3 rings, N x 3 positions/normals and N heights required",
        ));
    }
    let out = py.detach(|| {
        (0..h.len())
            .map(|i| {
                let ring = std::array::from_fn(|k| std::array::from_fn(|axis| r[[i, k, axis]] - p[[i, axis]]));
                let (relief, spread) = footprint_relief(&ring, [n[[i, 0]], n[[i, 1]], n[[i, 2]]]);
                relief >= min_relief.max(h[i] * relief_ratio) && spread >= min_spread
            })
            .collect::<Vec<_>>()
    });
    Ok(out.into_pyarray(py))
}

/// Interpolate displacement on the visible mesh's diagonal, not a bilinear
/// patch. Bilinear interpolation seats rocks above or below triangular faces.
fn triangle_offset(a: f64, b: f64, c: f64, d: f64, u: f64, v: f64) -> f64 {
    if u >= v {
        a * (1.0 - u) + b * (u - v) + c * v
    } else {
        a * (1.0 - v) + c * u + d * (v - u)
    }
}

/// Sample the displaced cliff at geographic lattice points (N x 3). Heights
/// are undisturbed terrain; the surface array is H x W x 3. Buried skirt
/// vertices cannot pull exposed rocks below the original terrain.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_sample_surface<'py>(
    py: Python<'py>,
    x0: f64,
    y0: f64,
    dx: f64,
    dy: f64,
    heights: PyReadonlyArray2<'py, f64>,
    surface: PyReadonlyArray3<'py, f64>,
    points: PyReadonlyArray2<'py, f64>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (z, s, p) = (heights.as_array(), surface.as_array(), points.as_array());
    if z.nrows() < 2
        || z.ncols() < 2
        || s.shape() != [z.nrows(), z.ncols(), 3]
        || p.ncols() != 3
        || [x0, y0].iter().any(|v| !v.is_finite())
        || [dx, dy].iter().any(|v| !v.is_finite() || *v <= 0.0)
        || p.iter().any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err(
            "matching surface/height grids, positive spacing and finite N x 3 points required",
        ));
    }
    let out = py.detach(|| {
        let mut out = p.to_owned();
        for (n, point) in p.rows().into_iter().enumerate() {
            let u = ((point[0] - x0) / dx).clamp(0.0, z.ncols() as f64 - 1.0 - 1e-6);
            let v = ((point[1] - y0) / dy).clamp(0.0, z.nrows() as f64 - 1.0 - 1e-6);
            let (i, j) = (u as usize, v as usize);
            for axis in 0..3 {
                let delta = |row: usize, col: usize| {
                    let ground = match axis {
                        0 => x0 + col as f64 * dx,
                        1 => y0 + row as f64 * dy,
                        _ => z[[row, col]],
                    };
                    let value = s[[row, col, axis]] - ground;
                    if axis == 2 {
                        value.max(0.0)
                    } else {
                        value
                    }
                };
                out[[n, axis]] += triangle_offset(
                    delta(j, i),
                    delta(j, i + 1),
                    delta(j + 1, i + 1),
                    delta(j + 1, i),
                    u - i as f64,
                    v - j as f64,
                );
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rock_basis_is_right_handed_at_any_heading() {
        for tilt in [0.0_f64, 12.0, 28.0] {
            let tilt = tilt.to_radians();
            let up = [tilt.dsin(), 0.0, tilt.dcos()];
            for heading in -180..180 {
                let [tangent, bitangent] = orientation(up, (heading as f64).to_radians());
                for axis in [tangent, bitangent] {
                    assert!((axis.iter().map(|v| v * v).sum::<f64>() - 1.0).abs() < 1e-12);
                    assert!(axis.iter().zip(up).map(|(a, b)| a * b).sum::<f64>().abs() < 1e-12);
                }
                let reconstructed = cross(tangent, bitangent);
                assert!(reconstructed.iter().zip(up).all(|(a, b)| (a - b).abs() < 1e-12));
            }
        }
    }

    #[test]
    fn burial_and_pivot_offset_use_independent_directions() {
        assert_eq!(seat_coordinate(10.0, 1.0, 0.0, 2.0, 3.0), 8.0);
        assert_eq!(seat_coordinate(10.0, 0.0, 1.0, 2.0, 3.0), 7.0);
        assert_eq!(seat_coordinate(10.0, -1.0, 0.0, 2.0, 3.0), 12.0);
        assert_eq!(seat_coordinate(10.0, 0.0, 1.0, 2.0, 0.0), 10.0);
    }

    #[test]
    fn steep_plane_has_no_footprint_relief() {
        let ring = std::array::from_fn(|k| {
            let a = k as f64 * std::f64::consts::FRAC_PI_4;
            let (x, y) = (a.dcos() * 3.0, a.dsin() * 3.0);
            [x, y, 3.0 * x]
        });
        let length = 10.0_f64.sqrt();
        let (relief, spread) = footprint_relief(&ring, [-3.0 / length, 0.0, 1.0 / length]);
        assert!(relief < 1e-14);
        assert!(spread < 1e-5);
    }

    #[test]
    fn broken_surface_has_relief_and_changing_facets() {
        let ring = std::array::from_fn(|k| {
            let a = k as f64 * std::f64::consts::FRAC_PI_4;
            [a.dcos() * 3.0, a.dsin() * 3.0, if k % 2 == 0 { 2.0 } else { -2.0 }]
        });
        let (relief, spread) = footprint_relief(&ring, [0.0, 0.0, 1.0]);
        assert_eq!(relief, 2.0);
        assert!(spread > 16.0);
    }

    #[test]
    fn surface_uses_mesh_diagonal_on_both_triangles() {
        // A raised corner distinguishes triangular from bilinear sampling.
        assert_eq!(triangle_offset(0.0, 0.0, 4.0, 0.0, 0.75, 0.25), 1.0);
        assert_eq!(triangle_offset(0.0, 0.0, 4.0, 0.0, 0.25, 0.75), 1.0);
        assert_eq!(triangle_offset(0.0, 0.0, 4.0, 0.0, 0.5, 0.5), 2.0);
    }
}

/// Downhill boundaries of selected cliff cells. Output rows contain world x/y,
/// outward cell direction dx/dy, and source cell row/column. Uphill rims do not
/// seed loose rubble. Input heights are vertex samples, keep is a cell mask.
#[pyfunction]
pub fn cliff_base_seeds<'py>(
    py: Python<'py>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    heights: PyReadonlyArray2<'py, f64>,
    keep: PyReadonlyArray2<'py, bool>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (xs, ys, z, k) = (xs.as_array(), ys.as_array(), heights.as_array(), keep.as_array());
    if xs.len() < 2 || ys.len() < 2 || z.shape() != [ys.len(), xs.len()] || k.shape() != [ys.len() - 1, xs.len() - 1] {
        return Err(PyValueError::new_err(
            "vertex coordinate/height grids and matching selected cell mask required",
        ));
    }
    let out = py.detach(|| {
        let (h, w) = (k.nrows(), k.ncols());
        let mean = |j: usize, i: usize| (z[[j, i]] + z[[j + 1, i]] + z[[j, i + 1]] + z[[j + 1, i + 1]]) * 0.25;
        let mut out = Vec::new();
        for (dj, di) in [(0, 1), (0, -1), (1, 0), (-1, 0)] {
            for j in 0..h {
                for i in 0..w {
                    if !k[[j, i]] {
                        continue;
                    }
                    let (oj, oi) = (j as i64 + dj, i as i64 + di);
                    if oj < 0 || oi < 0 || oj >= h as i64 || oi >= w as i64 {
                        continue;
                    }
                    if !k[[oj as usize, oi as usize]] && mean(j, i) > mean(oj as usize, oi as usize) + TOE_DROP_M {
                        out.extend_from_slice(&[
                            (xs[i] + xs[i + 1]) * 0.5 + di as f64 * (xs[1] - xs[0]) * 0.5,
                            (ys[j] + ys[j + 1]) * 0.5 + dj as f64 * (ys[1] - ys[0]) * 0.5,
                            di as f64,
                            dj as f64,
                            j as f64,
                            i as f64,
                        ]);
                    }
                }
            }
        }
        out
    });
    Ok(Array2::from_shape_vec((out.len() / 6, 6), out)
        .unwrap()
        .into_pyarray(py))
}

/// Candidate cells within bounded downhill fans. For overlapping fans, assign
/// the nearest seed (lowest seed index on a distance tie). Return row/column/
/// seed-index triples in row-major order, without a cells-by-seeds allocation.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn cliff_base_cells<'py>(
    py: Python<'py>,
    seeds: PyReadonlyArray2<'py, f64>,
    x0: f64,
    y0: f64,
    dx: f64,
    dy: f64,
    width: usize,
    height: usize,
    scale: f64,
    reach: f64,
    scatter: f64,
) -> PyResult<Bound<'py, PyArray2<i64>>> {
    let s = seeds.as_array();
    if s.ncols() != 6
        || [dx, dy, scale, reach].iter().any(|v| !v.is_finite() || *v <= 0.0)
        || [x0, y0, scatter].iter().any(|v| !v.is_finite())
        || scatter < 0.0
        || s.iter().any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err(
            "finite seed rows, positive grid scale/reach and nonnegative scatter required",
        ));
    }
    let out = py.detach(|| {
        let mut cells: BTreeMap<(usize, usize), (f64, usize)> = BTreeMap::new();
        let (rx, ry) = ((reach / (dx * scale)).ceil() as i64, (reach / dy).ceil() as i64);
        for (id, seed) in s.rows().into_iter().enumerate() {
            let (si, sj) = (
                ((seed[0] - x0) / dx).floor() as i64,
                ((seed[1] - y0) / dy).floor() as i64,
            );
            for dj in -ry..=ry {
                for di in -rx..=rx {
                    let (i, j) = (si + di, sj + dj);
                    if i < 0 || j < 0 || i >= width as i64 || j >= height as i64 {
                        continue;
                    }
                    let ox = (x0 + (i as f64 + 0.5) * dx - seed[0]) * scale;
                    let oy = y0 + (j as f64 + 0.5) * dy - seed[1];
                    let along = ox * seed[2] + oy * seed[3];
                    let across = (ox * seed[3] - oy * seed[2]).abs();
                    let distance = ox.dhypot(oy);
                    // Query padding is one cell; exact rock footprints are tested later.
                    if along >= -(dx * scale).max(dy) && distance < reach + dy && across < scatter + dy {
                        let entry = cells.entry((j as usize, i as usize)).or_insert((distance, id));
                        if distance < entry.0 || (distance == entry.0 && id < entry.1) {
                            *entry = (distance, id);
                        }
                    }
                }
            }
        }
        cells
            .into_iter()
            .flat_map(|((j, i), (_, id))| [j as i64, i as i64, id as i64])
            .collect::<Vec<_>>()
    });
    Ok(Array2::from_shape_vec((out.len() / 3, 3), out)
        .unwrap()
        .into_pyarray(py))
}
