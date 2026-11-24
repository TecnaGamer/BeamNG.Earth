//! Road-cut confirmation: a bank needs a road bench, an uphill approach and a natural crest.
use crate::cliffselect::{components, extend, grow, sample};
use crate::dm::Dm;
use numpy::{
    ndarray::{Array1, Array2},
    IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::{
    cmp::Ordering,
    collections::{BinaryHeap, HashMap},
};

/// Test point discs and height slabs against locally sampled pavement segments.
/// Buckets only prune candidates; segment order and exact clearance tests remain
/// unchanged. Unknown road heights conservatively exclude the whole column.
#[pyfunction]
pub fn cliff_road_clearance<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    low: PyReadonlyArray1<'py, f64>,
    high: PyReadonlyArray1<'py, f64>,
    gaps: PyReadonlyArray1<'py, f64>,
    roads: PyReadonlyArray2<'py, f64>,
    bucket: f64,
    guard: f64,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let n = xy.shape()[0];
    if xy.shape() != [n, 2]
        || low.len() != n
        || high.len() != n
        || gaps.len() != n
        || roads.shape()[1] != 7
        || !bucket.is_finite()
        || bucket <= 0.0
        || !guard.is_finite()
        || guard < 0.0
    {
        return Err(PyValueError::new_err("invalid cliff pavement clearance arrays"));
    }
    let points = xy.as_array();
    let (low, high, gaps, roads) = (low.as_array(), high.as_array(), gaps.as_array(), roads.as_array());
    if points.iter().any(|v| !v.is_finite())
        || gaps.iter().any(|v| !v.is_finite() || *v < 0.0)
        || roads
            .rows()
            .into_iter()
            .any(|r| r.iter().take(5).any(|v| !v.is_finite()) || r[4] < 0.0)
    {
        return Err(PyValueError::new_err(
            "finite cliff coordinates and nonnegative clearances required",
        ));
    }
    let hit = py.detach(|| {
        let mut hit = vec![false; n];
        if n == 0 || roads.nrows() == 0 {
            return hit;
        }
        let mut buckets: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
        let mut min = [f64::INFINITY; 2];
        let mut max = [f64::NEG_INFINITY; 2];
        for i in 0..n {
            let (x, y) = (points[[i, 0]], points[[i, 1]]);
            buckets
                .entry(((x / bucket).floor() as i64, (y / bucket).floor() as i64))
                .or_default()
                .push(i);
            min[0] = min[0].min(x);
            min[1] = min[1].min(y);
            max[0] = max[0].max(x);
            max[1] = max[1].max(y);
        }
        let max_gap = gaps.iter().copied().fold(0.0, f64::max);
        for road in roads.rows() {
            let (ax, ay, bx, by, width, z0, z1) = (road[0], road[1], road[2], road[3], road[4], road[5], road[6]);
            let lo = [ax.min(bx) - width - max_gap, ay.min(by) - width - max_gap];
            let hi = [ax.max(bx) + width + max_gap, ay.max(by) + width + max_gap];
            if hi[0] < min[0] || hi[1] < min[1] || lo[0] > max[0] || lo[1] > max[1] {
                continue;
            }
            let x0 = (lo[0].max(min[0]) / bucket).floor() as i64;
            let y0 = (lo[1].max(min[1]) / bucket).floor() as i64;
            let x1 = (hi[0].min(max[0]) / bucket).floor() as i64;
            let y1 = (hi[1].min(max[1]) / bucket).floor() as i64;
            let (vx, vy) = (bx - ax, by - ay);
            let length = (vx * vx + vy * vy).max(1e-12);
            for x in x0..=x1 {
                for y in y0..=y1 {
                    if let Some(indices) = buckets.get(&(x, y)) {
                        for &i in indices {
                            if hit[i] || ((z0 + z1).is_finite() && !(high[i] >= z0 - guard && low[i] <= z1 + guard)) {
                                continue;
                            }
                            let (px, py) = (points[[i, 0]] - ax, points[[i, 1]] - ay);
                            let t = ((px * vx + py * vy) / length).clamp(0.0, 1.0);
                            let (dx, dy) = (px - t * vx, py - t * vy);
                            hit[i] = (dx * dx + dy * dy).sqrt() <= width + gaps[i];
                        }
                    }
                }
            }
        }
        hit
    });
    Ok(Array1::from_vec(hit).into_pyarray(py))
}

const REACH: f64 = 64.0;
// Broad-phase padding only: exact distance below still decides eligibility.
const SEARCH_PAD_M: f64 = 1e-6;
const END_REACH: f64 = 160.0;
const SHOULDER: f64 = 3.0;
const PROBE: f64 = 4.0;
const FACING: f64 = 0.75;
const CORE_SLOPE_DEG: f64 = 40.0;
const SUPPORT_SLOPE_DEG: f64 = 35.0;
const EDGE_SLOPE_DEG: f64 = 30.0;
const END_SLOPE_DEG: f64 = 20.0;
const CREST_SLOPE_DEG: f64 = 38.0;
const SLOPE_BREAK_DEG: f64 = 12.0;
const BENCH_APPROACH_DEG: f64 = 15.0;
const MIN_AVERAGE_DEG: f64 = 30.0;
const MIN_CORE_CELLS: usize = 6;
const MIN_FOOTPRINT_M2: f64 = 96.0;
const MIN_FACE_M2: f64 = 192.0;
const MIN_LENGTH_M: f64 = 24.0;
const CITY_BUFFER_M: f64 = 64.0;
const HIGHWAY_CITY_BUFFER_M: f64 = 16.0;
const PROFILE_DROP_M: f64 = 1.0;
const MIN_RISE_M: f64 = 4.0;
const END_RISE_M: f64 = 1.0;
const END_AVERAGE_DEG: f64 = 15.0;
const END_NATURAL_FRACTION: f64 = 0.5;
const END_PROFILE_STEP_M: f64 = 2.0;
const END_CREST_HEIGHT_TOLERANCE_M: f64 = 2.0;

fn cell_gradient(z: &[f64], v: usize, stride: usize, dx: f64, dy: f64) -> (f64, f64) {
    (
        ((z[v + 1] - z[v]) + (z[v + stride + 1] - z[v + stride])) / (2.0 * dx),
        ((z[v + stride] - z[v]) + (z[v + stride + 1] - z[v + 1])) / (2.0 * dy),
    )
}

const TOE_REACH_M: f64 = 4.0 * std::f64::consts::SQRT_2;
const TOE_SLOPE_DEG: f64 = 20.0;
const TOE_DOWNHILL_COS: f64 = 0.5;
const CUT_SAMPLE_HOLE_M2: f64 = 4.0;
const TOE_NEIGHBOURS: [(isize, isize); 8] = [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)];

/// Finish confirmed road cuts at their fine-grid foot/crest without lateral
/// dilation. A bench, exclusion or missing elevation stops the bounded walk.
#[pyfunction]
pub fn cliff_cut_toes<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    seed: PyReadonlyArray2<'py, bool>,
    allowed: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
    fit_crest: bool,
) -> PyResult<Bound<'py, PyArray2<bool>>> {
    let (z, seed, allowed) = (heights.as_array(), seed.as_array(), allowed.as_array());
    let (h, w) = seed.dim();
    if h == 0
        || w == 0
        || z.dim() != (h + 1, w + 1)
        || allowed.dim() != (h, w)
        || !dx.is_finite()
        || !dy.is_finite()
        || dx <= 0.0
        || dy <= 0.0
    {
        return Err(PyValueError::new_err(
            "vertex heights, aligned cell masks and positive spacing required",
        ));
    }
    let z = z
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("contiguous heights required"))?;
    let out = py.detach(|| {
        let n = w * h;
        let seed: Vec<_> = seed.iter().copied().collect();
        let allowed: Vec<_> = allowed.iter().copied().collect();
        let gradient: Vec<_> = (0..n)
            .map(|i| cell_gradient(z, (i / w) * (w + 1) + i % w, w + 1, dx, dy))
            .collect();
        let magnitude: Vec<_> = gradient.iter().map(|&(x, y)| x.dhypot(y)).collect();
        let mut steep: Vec<_> = magnitude
            .iter()
            .map(|&v| v.is_finite() && v >= slope(TOE_SLOPE_DEG))
            .collect();
        let holes: Vec<_> = (0..n)
            .map(|i| seed[i] && allowed[i] && magnitude[i].is_finite() && !steep[i])
            .collect();
        // Only small enclosed sampling gaps within a proven cut can be filled.
        // Open feet, pavement and terrace benches remain boundaries.
        for cells in components(&holes, w, h) {
            if cells.len() as f64 * dx * dy > CUT_SAMPLE_HOLE_M2 {
                continue;
            }
            let enclosed = cells.iter().all(|&i| {
                TOE_NEIGHBOURS[..4].iter().all(|&(oy, ox)| {
                    let (y, x) = ((i / w) as isize + oy, (i % w) as isize + ox);
                    if y < 0 || x < 0 || y >= h as isize || x >= w as isize {
                        return false;
                    }
                    let j = y as usize * w + x as usize;
                    seed[j] && allowed[j] && (steep[j] || holes[j])
                })
            });
            if enclosed {
                for i in cells {
                    steep[i] = true;
                }
            }
        }
        let mut distance: Vec<_> = (0..n)
            .map(|i| {
                if seed[i] && allowed[i] && steep[i] {
                    0.0
                } else {
                    f64::INFINITY
                }
            })
            .collect();
        for _ in 0..(TOE_REACH_M / dx.min(dy)).ceil() as usize {
            let old = distance.clone();
            for (oy, ox) in TOE_NEIGHBOURS {
                let step = (ox as f64 * dx).dhypot(oy as f64 * dy);
                for i in 0..n {
                    if !allowed[i] || !steep[i] {
                        continue;
                    }
                    let (y, x) = ((i / w) as isize + oy, (i % w) as isize + ox);
                    if y < 0 || x < 0 || y >= h as isize || x >= w as isize {
                        continue;
                    }
                    let j = y as usize * w + x as usize;
                    let dot = gradient[i].0 * ox as f64 * dx + gradient[i].1 * oy as f64 * dy;
                    let threshold = TOE_DOWNHILL_COS * magnitude[i] * step;
                    let along = dot >= threshold
                        || (fit_crest
                            && dot <= -threshold
                            && magnitude[i] >= slope(EDGE_SLOPE_DEG)
                            && magnitude[j] >= slope(EDGE_SLOPE_DEG));
                    if along {
                        distance[i] = distance[i].min(old[j] + step);
                    }
                }
            }
        }
        Array2::from_shape_fn((h, w), |(y, x)| distance[y * w + x] <= TOE_REACH_M)
    });
    Ok(out.into_pyarray(py))
}

fn slope(degrees: f64) -> f64 {
    degrees.to_radians().dtan()
}

#[derive(Clone, Copy, PartialEq)]
struct Step(f64, usize);
impl Eq for Step {}
impl Ord for Step {
    fn cmp(&self, o: &Self) -> Ordering {
        o.0.total_cmp(&self.0).then_with(|| o.1.cmp(&self.1))
    }
}
impl PartialOrd for Step {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

/// Continue confirmed cuts through supported end profiles, bounded by physical
/// distance rather than grid steps so changing resolution cannot lengthen cuts.
fn ends(seed: &[bool], profiles: &[bool], w: usize, h: usize, dx: f64, dy: f64) -> Vec<bool> {
    let mut distance = vec![f64::INFINITY; seed.len()];
    let mut queue = BinaryHeap::new();
    for i in 0..seed.len() {
        if seed[i] {
            distance[i] = 0.0;
            queue.push(Step(0.0, i));
        }
    }
    while let Some(Step(d, i)) = queue.pop() {
        if d != distance[i] {
            continue;
        }
        for oy in -1i64..=1 {
            for ox in -1i64..=1 {
                let (x, y) = ((i % w) as i64 + ox, (i / w) as i64 + oy);
                if (ox == 0 && oy == 0) || x < 0 || y < 0 || x >= w as i64 || y >= h as i64 {
                    continue;
                }
                let j = y as usize * w + x as usize;
                let next = d + (ox as f64 * dx).dhypot(oy as f64 * dy);
                if profiles[j] && next <= END_REACH && next < distance[j] {
                    distance[j] = next;
                    queue.push(Step(next, j));
                }
            }
        }
    }
    distance.iter().map(|v| v.is_finite()).collect()
}

/// Reject isolated steep samples: a cut needs a continuous bank with enough
/// steep core, surface area and length along its principal direction.
fn sustained(
    evidence: &[bool],
    extent: &[bool],
    gradient: &[f64],
    profiles: &[bool],
    w: usize,
    h: usize,
    dx: f64,
    dy: f64,
) -> Vec<bool> {
    let mut result = vec![false; w * h];
    for cells in components(extent, w, h) {
        if !cells.iter().any(|&i| evidence[i]) {
            continue;
        }
        let proof: Vec<_> = cells.iter().filter(|&&i| evidence[i] || profiles[i]).copied().collect();
        let (mut mx, mut my) = (0.0, 0.0);
        for &i in &proof {
            mx += (i % w) as f64 * dx;
            my += (i / w) as f64 * dy;
        }
        mx /= proof.len() as f64;
        my /= proof.len() as f64;
        let (mut xx, mut xy, mut yy) = (0.0, 0.0, 0.0);
        for &i in &proof {
            let (x, y) = ((i % w) as f64 * dx - mx, (i / w) as f64 * dy - my);
            xx += x * x;
            xy += x * y;
            yy += y * y;
        }
        let angle = 0.5 * (2.0 * xy).datan2(xx - yy);
        let (uy, ux) = angle.dsin_cos();
        let (mut lo, mut hi) = ([f64::INFINITY; 3], [f64::NEG_INFINITY; 3]);
        for &i in &proof {
            let (x, y) = ((i % w) as f64 * dx, (i / w) as f64 * dy);
            let p = [x, y, x * ux + y * uy];
            for k in 0..3 {
                lo[k] = lo[k].min(p[k]);
                hi[k] = hi[k].max(p[k]);
            }
        }
        let length = (hi[0] - lo[0] + dx)
            .max(hi[1] - lo[1] + dy)
            .max(hi[2] - lo[2] + ux.abs() * dx + uy.abs() * dy);
        let core = cells
            .iter()
            .filter(|&&i| evidence[i] && gradient[i] >= slope(CORE_SLOPE_DEG))
            .count();
        let area = cells
            .iter()
            .map(|&i| dx * dy * (1.0 + gradient[i] * gradient[i]).sqrt())
            .sum::<f64>();
        if core >= MIN_CORE_CELLS
            && proof.len() as f64 * dx * dy >= MIN_FOOTPRINT_M2
            && area >= MIN_FACE_M2
            && length >= MIN_LENGTH_M
        {
            for i in cells {
                result[i] = true;
            }
        }
    }
    result
}

fn city(cover: &[u8], labels: &[bool], corridor: Option<&[bool]>, w: usize, h: usize, spacing: f64) -> Vec<bool> {
    let seeds: Vec<_> = cover.iter().zip(labels).map(|(&c, &r)| c == 50 && !r).collect();
    let mut out = grow(&seeds, w, h, (CITY_BUFFER_M / spacing).ceil() as usize);
    if let Some(c) = corridor {
        let local = grow(&seeds, w, h, (HIGHWAY_CITY_BUFFER_M / spacing).ceil() as usize);
        for i in 0..out.len() {
            out[i] = local[i] || (out[i] && !c[i]);
        }
    }
    out
}

struct Profile {
    i: usize,
    tx: f64,
    ty: f64,
    ux: f64,
    uy: f64,
    toe_distance: f64,
    crest: f64,
    end_crest: f64,
    confirmed: bool,
    ending: bool,
    facing: bool,
    bench: bool,
}

fn select(
    z: &[f64],
    cover: &[u8],
    blocked: &[bool],
    labels: &[bool],
    corridor: Option<&[bool]>,
    centers: &[[f64; 2]],
    roads: &[[f64; 5]],
    vw: usize,
    vh: usize,
    dx: f64,
    dy: f64,
) -> (Vec<bool>, Vec<bool>) {
    let (w, h) = (vw - 1, vh - 1);
    let n = w * h;
    let natural: Vec<_> = cover.iter().map(|c| matches!(c, 10 | 20 | 30 | 60 | 100)).collect();
    let natural_z: Vec<_> = natural.iter().map(|&v| if v { 1.0 } else { 0.0 }).collect();
    let cover_z: Vec<_> = cover
        .iter()
        .zip(&natural)
        .map(|(&c, &v)| if v || c == 50 { 1.0 } else { 0.0 })
        .collect();
    let labels_z: Vec<_> = labels.iter().map(|&v| if v { 1.0 } else { 0.0 }).collect();
    let guard = city(cover, labels, corridor, vw, vh, dx.min(dy));
    let road_strip: Vec<_> = (0..z.len())
        .map(|i| cover[i] == 50 && labels[i] && corridor.is_some_and(|c| c[i]))
        .collect();
    let mut safe: Vec<_> = (0..z.len())
        .map(|i| z[i].is_finite() && ((natural[i] && !guard[i]) || road_strip[i]))
        .collect();
    let probe_safe: Vec<_> = (0..z.len())
        .map(|i| safe[i] || (z[i].is_finite() && corridor.is_some_and(|c| c[i]) && (natural[i] || cover[i] == 50)))
        .collect();
    let corners = |i: usize| {
        let v = (i / w) * vw + i % w;
        [v, v + 1, v + vw, v + vw + 1]
    };
    let mut magnitude = vec![0.0; n];
    let mut profiles = Vec::new();
    // Most roads in the detector halo cannot reach a given bank. Keep their
    // original order (including distance ties), but reject distant bounds
    // before evaluating closest points and deterministic square roots.
    let road_bounds: Vec<_> = roads
        .iter()
        .map(|&[ax, ay, bx, by, half]| {
            let reach = half + REACH + SEARCH_PAD_M;
            [
                ax.min(bx) - reach,
                ay.min(by) - reach,
                ax.max(bx) + reach,
                ay.max(by) + reach,
            ]
        })
        .collect();
    let at = |grid: &[f64], x, y| sample(grid, vw, vh, x, y);
    for i in 0..n {
        let v = (i / w) * vw + i % w;
        let (gx, gy) = cell_gradient(z, v, vw, dx, dy);
        let mag = gx.dhypot(gy);
        magnitude[i] = mag;
        if blocked[i] || !corners(i).iter().all(|&j| probe_safe[j]) || !(mag >= slope(END_SLOPE_DEG)) {
            continue;
        }
        let p = centers[i];
        let mut distance = f64::INFINITY;
        let mut foot = [0.0; 2];
        let mut half = 0.0;
        for (&[ax, ay, bx, by, width], bounds) in roads.iter().zip(&road_bounds) {
            if p[0] < bounds[0] || p[1] < bounds[1] || p[0] > bounds[2] || p[1] > bounds[3] {
                continue;
            }
            let (vx, vy) = (bx - ax, by - ay);
            let length = vx * vx + vy * vy;
            if length <= 0.0 {
                continue;
            }
            let t = (((p[0] - ax) * vx + (p[1] - ay) * vy) / length).clamp(0.0, 1.0);
            let q = [ax + t * vx, ay + t * vy];
            let d = (p[0] - q[0]).dhypot(p[1] - q[1]) - width;
            let alignment = ((p[0] - q[0]) * gx + (p[1] - q[1]) * gy) / mag / (d + width).max(1e-9);
            if d < distance && d <= REACH && alignment >= FACING {
                distance = d;
                foot = q;
                half = width;
            }
        }
        if !distance.is_finite() || distance < SHOULDER {
            continue;
        }
        let (ux, uy) = (
            (p[0] - foot[0]) / (distance + half),
            (p[1] - foot[1]) / (distance + half),
        );
        let (cx, cy) = ((i % w) as f64 + 0.5, (i / w) as f64 + 0.5);
        let td = distance - SHOULDER;
        let (tx, ty) = (cx - ux * td / dx, cy - uy * td / dy);
        let toe = at(z, tx, ty);
        let bench = (toe - at(z, tx - ux * PROBE / dx, ty - uy * PROBE / dy)).abs() / PROBE <= slope(CREST_SLOPE_DEG);
        let facing = (gx * ux + gy * uy) / mag >= FACING;
        let crest_limit = slope(CREST_SLOPE_DEG.min(mag.datan().to_degrees() - SLOPE_BREAK_DEG));
        let (mut approach_z, mut face_toe, mut face_start, mut previous_run) = (toe, toe, 0.0, 0.0);
        let (mut on_bench, mut approach) = (true, true);
        for step in 1..=16 {
            let run = (step as f64 * 4.0).min(td);
            let (px, py) = (tx + ux * run / dx, ty + uy * run / dy);
            let zz = at(z, px, py);
            approach &= zz.is_finite() && zz >= approach_z - PROFILE_DROP_M && at(&cover_z, px, py) >= 0.999;
            on_bench &= zz.is_finite() && (zz - approach_z).abs() <= (run - previous_run) * slope(BENCH_APPROACH_DEG);
            if on_bench {
                face_toe = zz;
                face_start = run;
            }
            previous_run = run;
            approach_z = zz;
        }
        let mut previous_z = at(z, cx, cy);
        let (mut path, mut end_path, mut peak) = (approach, approach, previous_z);
        let mut profile = Profile {
            i,
            tx,
            ty,
            ux,
            uy,
            toe_distance: td,
            crest: 0.0,
            end_crest: 0.0,
            confirmed: false,
            ending: false,
            facing,
            bench,
        };
        for step in 1..=(REACH / END_PROFILE_STEP_M) as usize {
            let reach = step as f64 * END_PROFILE_STEP_M;
            // Neither kind of crest can qualify beyond the road's reach or
            // without a facing bank and a road-side bench. Later samples do
            // not feed any other result, so stop before sampling their grids.
            if distance + reach > REACH || !facing || !bench {
                break;
            }
            let (px, py) = (cx + ux * reach / dx, cy + uy * reach / dy);
            let zz = at(z, px, py);
            let cover_ok = at(&cover_z, px, py) >= 0.999;
            let strict = step % 2 == 0;
            if strict {
                path &= zz.is_finite() && zz >= previous_z - PROFILE_DROP_M && cover_ok;
                previous_z = zz;
            }
            end_path &= zz.is_finite() && cover_ok && zz >= peak - PROFILE_DROP_M;
            peak = if zz.is_nan() { zz } else { peak.max(zz) };
            let sx = (at(z, px + PROBE / dx, py) - at(z, px - PROBE / dx, py)) / (2.0 * PROBE);
            let sy = (at(z, px, py + PROBE / dy) - at(z, px, py - PROBE / dy)) / (2.0 * PROBE);
            let crest_cover = at(&natural_z, px, py);
            let mut end_cover = crest_cover;
            let crest_land = crest_cover > 0.0 || at(&labels_z, px, py) >= 0.999;
            for offset in [-8.0, -4.0, 4.0, 8.0] {
                let (ex, ey) = (px - uy * offset / dx, py + ux * offset / dy);
                if crest_land && (at(z, ex, ey) - zz).abs() <= END_CREST_HEIGHT_TOLERANCE_M {
                    let e = at(&natural_z, ex, ey);
                    end_cover = if e.is_nan() || end_cover.is_nan() {
                        f64::NAN
                    } else {
                        end_cover.max(e)
                    };
                }
            }
            let span = td + reach;
            let common = distance + reach <= REACH && facing && bench && sx.dhypot(sy) <= crest_limit;
            if common
                && strict
                && path
                && crest_cover >= 0.999
                && zz - face_toe >= MIN_RISE_M
                && (zz - face_toe) / (span - face_start) >= slope(MIN_AVERAGE_DEG)
            {
                if !profile.confirmed {
                    profile.crest = reach;
                }
                profile.confirmed = true;
            }
            if common
                && end_path
                && end_cover >= END_NATURAL_FRACTION
                && zz - toe >= END_RISE_M
                && (zz - toe) / span >= slope(END_AVERAGE_DEG)
            {
                profile.end_crest = reach;
                profile.ending = true;
            }
        }
        profiles.push(profile);
    }
    if profiles.is_empty() {
        return (vec![false; n], vec![false; n]);
    }
    let mut evidence = vec![false; n];
    let mut end_profiles = vec![false; n];
    for p in &profiles {
        evidence[p.i] = p.confirmed && magnitude[p.i].datan().to_degrees() >= SUPPORT_SLOPE_DEG;
        end_profiles[p.i] = p.ending;
    }
    if let Some(c) = corridor {
        let highway: Vec<_> = (0..n).map(|i| corners(i).iter().all(|&j| c[j])).collect();
        let proof: Vec<_> = (0..n).map(|i| evidence[i] && highway[i]).collect();
        let end_highway: Vec<_> = (0..n).map(|i| end_profiles[i] && highway[i]).collect();
        let profiled = ends(&proof, &end_highway, w, h, dx, dy);
        let recovered = sustained(&proof, &profiled, &magnitude, &end_profiles, w, h, dx, dy);
        let recovered = ends(&recovered, &end_highway, w, h, dx, dy);
        let mut vertices = vec![false; z.len()];
        for i in 0..n {
            if recovered[i] {
                for j in corners(i) {
                    vertices[j] = true;
                }
            }
        }
        let spacing = dx.min(dy) / 2.0;
        for p in &profiles {
            if !recovered[p.i] {
                continue;
            }
            let span = p.toe_distance + if p.confirmed { p.crest } else { p.end_crest };
            for step in 0..=((REACH + spacing * 0.5) / spacing).floor() as usize {
                let run = step as f64 * spacing;
                if run > span {
                    continue;
                }
                let x = (p.tx + p.ux * run / dx).floor() as i64;
                let y = (p.ty + p.uy * run / dy).floor() as i64;
                for oy in 0..=1 {
                    for ox in 0..=1 {
                        vertices[(y + oy).clamp(0, vh as i64 - 1) as usize * vw
                            + (x + ox).clamp(0, vw as i64 - 1) as usize] = true;
                    }
                }
            }
        }
        let corrected: Vec<_> = (0..z.len()).map(|i| labels[i] || vertices[i]).collect();
        let guard = city(cover, &corrected, corridor, vw, vh, dx.min(dy));
        for i in 0..safe.len() {
            safe[i] =
                z[i].is_finite() && (((natural[i] || (cover[i] == 50 && vertices[i])) && !guard[i]) || road_strip[i]);
        }
    }
    let land: Vec<_> = (0..n).map(|i| corners(i).iter().all(|&j| safe[j])).collect();
    let eligible: Vec<_> = (0..n).map(|i| land[i] && !blocked[i]).collect();
    for i in 0..n {
        evidence[i] &= eligible[i];
    }
    let mut support = vec![false; n];
    for p in &profiles {
        support[p.i] = eligible[p.i] && p.facing && p.bench && magnitude[p.i].datan().to_degrees() >= EDGE_SLOPE_DEG;
    }
    let extent = extend(&evidence, &support, w, h, dx, dy);
    let result = sustained(&evidence, &extent, &magnitude, &end_profiles, w, h, dx, dy);
    for i in 0..n {
        end_profiles[i] &= eligible[i];
    }
    (ends(&result, &end_profiles, w, h, dx, dy), land)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cut_ends_use_physical_distance_on_anisotropic_grids() {
        // A straight bank reaches the same distance at either resolution.
        for spacing in [4.0, 8.0] {
            let n = 50;
            let mut seed = vec![false; n];
            seed[0] = true;
            let profiles = vec![true; n];
            let horizontal = ends(&seed, &profiles, n, 1, spacing, 3.0);
            let vertical = ends(&seed, &profiles, 1, n, 3.0, spacing);
            let expected: Vec<_> = (0..n).map(|i| i as f64 * spacing <= 160.0).collect();
            assert_eq!(horizontal, expected);
            assert_eq!(vertical, expected);
        }
    }

    #[test]
    fn cut_ends_cannot_cross_an_unsupported_gap() {
        let mut seed = vec![false; 12];
        seed[0] = true;
        let mut profiles = vec![true; 12];
        profiles[4] = false;
        assert_eq!(
            ends(&seed, &profiles, 12, 1, 4.0, 4.0),
            (0..12).map(|i| i < 4).collect::<Vec<_>>()
        );
        assert_eq!(ends(&[false; 12], &profiles, 12, 1, 4.0, 4.0), vec![false; 12]);
    }

    #[test]
    fn sustained_cut_rejects_disconnected_unconfirmed_faces() {
        // Six confirmed 4m cells form a 24m bank; a disconnected bank with
        // identical dimensions must not inherit confirmation from it.
        let mut evidence = vec![false; 13];
        evidence[..6].fill(true);
        let mut extent = vec![true; 13];
        extent[6] = false;
        let gradients = vec![2.0; 13];
        let result = sustained(&evidence, &extent, &gradients, &[false; 13], 13, 1, 4.0, 4.0);
        assert_eq!(result, evidence);
        // A short isolated sample remains rejected even with strong slope.
        assert_eq!(
            sustained(&[true], &[true], &[10.0], &[false], 1, 1, 4.0, 4.0),
            vec![false]
        );
    }
}

/// Continue confirmed cuts through adjacent profiled cells, up to END_REACH metres.
#[pyfunction]
pub fn cliff_cut_ends<'py>(
    py: Python<'py>,
    seed: PyReadonlyArray2<'py, bool>,
    profiles: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
) -> PyResult<Bound<'py, PyArray2<bool>>> {
    if seed.shape() != profiles.shape() || [dx, dy].iter().any(|v| !v.is_finite() || *v <= 0.0) {
        return Err(PyValueError::new_err(
            "matching cell masks and finite positive spacing required",
        ));
    }
    let (h, w) = (seed.shape()[0], seed.shape()[1]);
    let s: Vec<_> = seed.as_array().iter().copied().collect();
    let p: Vec<_> = profiles.as_array().iter().copied().collect();
    let out = py.detach(|| ends(&s, &p, w, h, dx, dy));
    Ok(Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

/// Filter candidate cell components by confirmed core, area and face length.
#[pyfunction]
pub fn cliff_cut_sustained<'py>(
    py: Python<'py>,
    evidence: PyReadonlyArray2<'py, bool>,
    extent: PyReadonlyArray2<'py, bool>,
    gradient: PyReadonlyArray2<'py, f64>,
    profiles: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
) -> PyResult<Bound<'py, PyArray2<bool>>> {
    if [extent.shape(), gradient.shape(), profiles.shape()]
        .iter()
        .any(|s| *s != evidence.shape())
        || [dx, dy].iter().any(|v| !v.is_finite() || *v <= 0.0)
    {
        return Err(PyValueError::new_err(
            "matching cell grids and finite positive spacing required",
        ));
    }
    let (h, w) = (evidence.shape()[0], evidence.shape()[1]);
    let e: Vec<_> = evidence.as_array().iter().copied().collect();
    let x: Vec<_> = extent.as_array().iter().copied().collect();
    let g: Vec<_> = gradient.as_array().iter().copied().collect();
    let p: Vec<_> = profiles.as_array().iter().copied().collect();
    let out = py.detach(|| sustained(&e, &x, &g, &p, w, h, dx, dy));
    Ok(Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

/// Confirm road cuts on an (H, W) height grid. Vertex masks have the same shape;
/// blocked cells are (H-1, W-1), centres are ((H-1)*(W-1), 2), and road segments
/// are (M, 5): ax, ay, bx, by, half-width. Returns selected and eligible cell masks.
#[pyfunction]
pub fn cliff_cut_select<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    classes: PyReadonlyArray2<'py, u8>,
    blocked: PyReadonlyArray2<'py, bool>,
    labels: PyReadonlyArray2<'py, bool>,
    corridor: PyReadonlyArray2<'py, bool>,
    centers: PyReadonlyArray2<'py, f64>,
    roads: PyReadonlyArray2<'py, f64>,
    dx: f64,
    dy: f64,
    has_corridor: bool,
) -> PyResult<(Bound<'py, PyArray2<bool>>, Bound<'py, PyArray2<bool>>)> {
    let (h, w) = (heights.shape()[0], heights.shape()[1]);
    if w < 2
        || h < 2
        || [classes.shape(), labels.shape(), corridor.shape()]
            .iter()
            .any(|s| *s != [h, w])
        || blocked.shape() != [h - 1, w - 1]
        || centers.shape() != [(h - 1) * (w - 1), 2]
        || roads.shape()[1] != 5
        || !dx.is_finite()
        || !dy.is_finite()
        || dx <= 0.0
        || dy <= 0.0
    {
        return Err(PyValueError::new_err(
            "matching road-cut grids, Nx2 centers, Mx5 road segments and finite positive spacing required",
        ));
    }
    let z: Vec<_> = heights.as_array().iter().copied().collect();
    let c: Vec<_> = classes.as_array().iter().copied().collect();
    let b: Vec<_> = blocked.as_array().iter().copied().collect();
    let l: Vec<_> = labels.as_array().iter().copied().collect();
    let co: Vec<_> = corridor.as_array().iter().copied().collect();
    let p: Vec<_> = centers.as_array().rows().into_iter().map(|r| [r[0], r[1]]).collect();
    let r: Vec<_> = roads
        .as_array()
        .rows()
        .into_iter()
        .map(|r| [r[0], r[1], r[2], r[3], r[4]])
        .collect();
    let (out, domain) = py.detach(|| {
        select(
            &z,
            &c,
            &b,
            &l,
            if has_corridor { Some(&co) } else { None },
            &p,
            &r,
            w,
            h,
            dx,
            dy,
        )
    });
    Ok((
        Array2::from_shape_vec((h - 1, w - 1), out).unwrap().into_pyarray(py),
        Array2::from_shape_vec((h - 1, w - 1), domain).unwrap().into_pyarray(py),
    ))
}
