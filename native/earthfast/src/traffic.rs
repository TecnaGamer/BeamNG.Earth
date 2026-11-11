//! Cosmetic road and bridge routes (#565). Python supplies shared road rules, lane shifts and solved deck heights;
//! chunking, selection, lane geometry and relief simplification stay here. No connection to the physics graph.

use blake2::{digest::consts::U8, Blake2s, Digest};
use numpy::{ndarray::Array2, IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*, types::{PyDict, PyList}};
use std::collections::HashSet;

const MAX_PATHS: usize = 128;
const MAX_POINTS: usize = 512;
const MAX_TILE_POINTS: usize = 8192;
const CHUNK_M: f64 = 2000.0;
const STEP_M: f64 = 20.0;
const MIN_LENGTH_M: f64 = 80.0;
const CLASSES: [&str; 10] = ["motorway", "trunk", "primary", "secondary", "tertiary", "residential",
    "unclassified", "living_street", "service", "track"];
const SPACING: [f64; 10] = [55.0, 65.0, 50.0, 70.0, 100.0, 220.0, 300.0, 450.0, 500.0, 3000.0];

type Input<'py> = (String, String, String, bool, f64, f64, Option<f64>, bool, Vec<(usize, PyReadonlyArray2<'py, f64>)>);

struct Road {
    id: String, class: String, surface: String, oneway: bool, sign: f64, width: f64,
    mph: Option<f64>, skip: bool, parts: Vec<Part>,
}

struct Part { index: usize, points: Vec<[f64; 3]>, bridge: bool }

struct Candidate {
    source: usize, group: usize, class: usize, rank: [u8; 8], id: String, suffix: String,
    xy: Vec<[f64; 2]>, z: Option<Vec<f64>>, directions: Vec<i32>, offset: f64, speed: f64, spacing: f64, reach: f64, surface: String,
}

struct Route {
    id: String, points: Vec<[f64; 3]>, speed: f64, spacing: f64, reach: f64, class: String, surface: String,
}

fn simplify(xy: &[[f64; 2]], tolerance: f64) -> Vec<usize> {
    crate::sidewalks::simplify(&xy.iter().map(|p| p.to_vec()).collect::<Vec<_>>(), tolerance)
}

fn arcs(xy: &[[f64; 2]]) -> Vec<f64> {
    let mut out = vec![0.0];
    for p in xy.windows(2) { out.push(out.last().unwrap() + (p[1][0] - p[0][0]).hypot(p[1][1] - p[0][1])); }
    out
}

fn interpolate(xy: &[[f64; 3]], s: &[f64], at: f64) -> [f64; 3] {
    if at <= 0.0 { return xy[0]; }
    if at >= s[s.len() - 1] { return xy[xy.len() - 1]; }
    let k = s.partition_point(|v| *v <= at) - 1;
    let t = (at - s[k]) / (s[k + 1] - s[k]);
    std::array::from_fn(|j| xy[k][j] + t * (xy[k + 1][j] - xy[k][j]))
}

fn pieces(raw: &[[f64; 3]], bounds: [f64; 4], bridge: bool) -> Vec<(usize, Vec<[f64; 3]>)> {
    if raw.len() < 2 || raw.iter().flatten().any(|v| !v.is_finite()) { return Vec::new(); }
    let xy: Vec<_> = raw.iter().enumerate().filter(|(i, p)| *i == 0 ||
        (p[0] - raw[i - 1][0]).hypot(p[1] - raw[i - 1][1]) > 0.01).map(|(_, p)| *p).collect();
    if xy.len() < 2 { return Vec::new(); }
    let s = arcs(&xy.iter().map(|p| [p[0], p[1]]).collect::<Vec<_>>());
    let length = *s.last().unwrap();
    if !(MIN_LENGTH_M..=100000.0).contains(&length) { return Vec::new(); }
    let mut out = Vec::new();
    for k in 0..(length / CHUNK_M).ceil() as usize {
        let (a, b) = (k as f64 * CHUNK_M, ((k + 1) as f64 * CHUNK_M).min(length));
        if b - a < MIN_LENGTH_M { continue; }
        let mut p = vec![interpolate(&xy, &s, a)];
        p.extend(xy.iter().zip(&s).filter(|(_, t)| **t > a && **t < b).map(|(p, _)| *p));
        p.push(interpolate(&xy, &s, b));
        if p.iter().all(|p| p[0] < bounds[0]) || p.iter().all(|p| p[0] > bounds[2]) ||
            p.iter().all(|p| p[1] < bounds[1]) || p.iter().all(|p| p[1] > bounds[3]) { continue; }
        // Deck relief is already solved by the road builder. Retain it while thinning the centreline, before sampling lanes.
        let keep = crate::sidewalks::simplify(&p.iter().map(|p| p.to_vec()).collect::<Vec<_>>(), if bridge { 0.15 } else { 0.5 });
        let p: Vec<_> = keep.iter().map(|i| p[*i]).collect();
        let steps: Vec<_> = p.windows(2).map(|p|
            (((p[1][0] - p[0][0]).hypot(p[1][1] - p[0][1]) / STEP_M).ceil() as usize).max(1)).collect();
        if steps.iter().sum::<usize>() + 1 > MAX_POINTS { continue; }
        let mut dense = vec![p[0]];
        for (seg, n) in p.windows(2).zip(steps) {
            for j in 1..=n {
                let t = j as f64 / n as f64;
                dense.push(std::array::from_fn(|j| seg[0][j] + (seg[1][j] - seg[0][j]) * t));
            }
        }
        out.push((k, dense));
    }
    out
}

// The shared road builder's joined left normals, with the same 2x miter bound (roads.offset_polyline).
fn offset(xy: &[[f64; 2]], d: &[f64]) -> Vec<[f64; 2]> {
    let normals: Vec<_> = xy.windows(2).map(|p| {
        let (dx, dy) = (p[1][0] - p[0][0], p[1][1] - p[0][1]);
        let len = dx.hypot(dy).max(1e-9);
        [-dy / len, dx / len]
    }).collect();
    xy.iter().enumerate().map(|(i, p)| {
        let n = if i == 0 { normals[0] } else if i == xy.len() - 1 { normals[i - 1] } else {
            let v = [normals[i - 1][0] + normals[i][0], normals[i - 1][1] + normals[i][1]];
            let len = v[0].hypot(v[1]).max(1e-9);
            let m = [v[0] / len, v[1] / len];
            let scale = (m[0] * normals[i][0] + m[1] * normals[i][1]).clamp(0.5, 1.0);
            [m[0] / scale, m[1] / scale]
        };
        [p[0] + n[0] * d[i], p[1] + n[1] * d[i]]
    }).collect()
}

fn compact(xy: &[[f64; 2]], z: &[f64]) -> Vec<[f64; 3]> {
    let relief: Vec<_> = arcs(xy).into_iter().zip(z).map(|(s, z)| [s, *z]).collect();
    let mut keep = vec![false; xy.len()];
    for i in simplify(xy, 0.5).into_iter().chain(simplify(&relief, 0.15)) { keep[i] = true; }
    xy.iter().zip(z).enumerate().filter(|(i, _)| keep[*i]).map(|(_, (p, z))|
        [p[0], p[1], *z].map(|v| (v * 1000.0).round_ties_even() / 1000.0)).collect()
}

#[pyclass(name = "TrafficRoutes")]
pub struct Routes {
    candidates: Vec<Candidate>, used: Vec<bool>, counts: [usize; 3], points: usize, path_limit: usize,
    pass: usize, cursor: usize, current: Option<usize>, lanes: Vec<[f64; 2]>, routes: Vec<Route>,
}

impl Routes {
    fn prepare(roads: Vec<Road>, bounds: [f64; 4], left_hand: bool, major_only: bool) -> Self {
        let mut candidates = Vec::new();
        let mut seen = HashSet::new();
        for (source, road) in roads.into_iter().enumerate() {
            let Some(class) = CLASSES.iter().position(|c| *c == road.class) else { continue; };
            if !["paved", "cobble", "gravel", "dirt"].contains(&road.surface.as_str()) || !seen.insert(road.id.clone()) || road.skip { continue; }
            let group = if road.surface == "gravel" || road.surface == "dirt" || class == 9 { 2 } else if class < 5 { 0 } else { 1 };
            if major_only && group != 0 { continue; }
            let mut speed = road.mph.map(|v| (v * 0.44704).min(28.0)).unwrap_or(8.0);
            let mut spacing = SPACING[class];
            if group == 2 { speed = speed.min(8.0); spacing = spacing.max(1600.0); }
            for raw in &road.parts {
                let prefix = if raw.bridge { 'b' } else { 'g' };
                for (piece, points) in pieces(&raw.points, bounds, raw.bridge) {
                    let suffix = format!("{prefix}{}c{piece}", raw.index);
                    let xy = points.iter().map(|p| [p[0], p[1]]).collect();
                    let z = raw.bridge.then(|| points.iter().map(|p| p[2]).collect());
                    let digest = Blake2s::<U8>::digest(format!("{}:{suffix}", road.id).as_bytes());
                    candidates.push(Candidate { source, group, class, rank: digest.into(), id: road.id.clone(), suffix, xy, z,
                        directions: if road.oneway { vec![if road.sign < 0.0 { -1 } else { 1 }] } else { vec![1, -1] },
                        offset: if road.oneway { 0.0 } else { (if left_hand { -1.0 } else { 1.0 }) * (road.width / 4.0).min(1.7) },
                        speed: (speed * 1000.0).round_ties_even() / 1000.0, spacing,
                        reach: if group == 0 { 10000.0 } else { 6000.0 }, surface: road.surface.clone() });
                }
            }
        }
        candidates.sort_by(|a, b| (a.group, a.class, a.rank, &a.id, &a.suffix).cmp(&(b.group, b.class, b.rank, &b.id, &b.suffix)));
        Self { used: vec![false; candidates.len()], candidates, counts: [0; 3], points: 0,
            path_limit: if major_only { 32 } else { MAX_PATHS }, pass: 0, cursor: 0, current: None,
            lanes: Vec::new(), routes: Vec::new() }
    }
}

#[pymethods]
impl Routes {
    #[new]
    fn new(py: Python<'_>, inputs: Vec<Input<'_>>, bounds: [f64; 4], left_hand: bool, major_only: bool) -> PyResult<Self> {
        let mut roads = Vec::new();
        for (id, class, surface, oneway, sign, width, mph, skip, parts) in inputs {
            let mut xy = Vec::new();
            for (index, part) in parts {
                let a = part.as_array();
                if a.ncols() != 2 && a.ncols() != 3 { return Err(PyValueError::new_err("traffic road points must have two or three columns")); }
                let bridge = a.ncols() == 3;
                xy.push(Part { index, points: a.rows().into_iter().map(|p| [p[0], p[1], if bridge { p[2] } else { 0.0 }]).collect(), bridge });
            }
            roads.push(Road { id, class, surface, oneway, sign, width, mph, skip, parts: xy });
        }
        Ok(py.detach(|| Self::prepare(roads, bounds, left_hand, major_only)))
    }

    #[staticmethod]
    fn limits() -> (usize, usize, usize, f64, f64, f64) {
        (MAX_PATHS, MAX_POINTS, MAX_TILE_POINTS, CHUNK_M, STEP_M, MIN_LENGTH_M)
    }

    fn next<'py>(&mut self, py: Python<'py>) -> PyResult<Option<(usize, Bound<'py, PyArray2<f64>>)>> {
        if self.current.is_some() { return Err(PyValueError::new_err("accept the current traffic route first")); }
        while self.pass < 2 && self.routes.len() < self.path_limit {
            if self.cursor == self.candidates.len() { self.pass += 1; self.cursor = 0; continue; }
            let i = self.cursor;
            self.cursor += 1;
            let c = &self.candidates[i];
            if self.used[i] || (self.pass == 0 && self.counts[c.group] + c.directions.len() > [64, 48, 16][c.group]) ||
                self.routes.len() + c.directions.len() > self.path_limit || self.points + c.xy.len() * c.directions.len() > MAX_TILE_POINTS { continue; }
            self.current = Some(i);
            let xy = Array2::from_shape_vec((c.xy.len(), 2), c.xy.iter().flatten().copied().collect()).unwrap();
            return Ok(Some((c.source, xy.into_pyarray(py))));
        }
        Ok(None)
    }

    fn lane_points<'py>(&mut self, py: Python<'py>, shifts: Option<PyReadonlyArray1<'py, f64>>) -> PyResult<Bound<'py, PyArray2<f64>>> {
        let i = self.current.ok_or_else(|| PyValueError::new_err("no pending traffic route"))?;
        let c = &self.candidates[i];
        let shift = shifts.map(|v| v.as_array().to_vec());
        if shift.as_ref().is_some_and(|v| v.len() != c.xy.len() || v.iter().any(|v| !v.is_finite())) {
            return Err(PyValueError::new_err("traffic lane shifts must match the pending points and be finite"));
        }
        self.lanes = py.detach(|| {
            let centre = shift.as_ref().map(|v| offset(&c.xy, v)).unwrap_or_else(|| c.xy.clone());
            c.directions.iter().flat_map(|dir| offset(&centre, &vec![-c.offset * *dir as f64; centre.len()])).collect()
        });
        Ok(Array2::from_shape_vec((self.lanes.len(), 2), self.lanes.iter().flatten().copied().collect()).unwrap().into_pyarray(py))
    }

    fn deck_heights<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyArray1<f64>>>> {
        let i = self.current.ok_or_else(|| PyValueError::new_err("no pending traffic route"))?;
        let c = &self.candidates[i];
        Ok(c.z.as_ref().map(|z| c.directions.iter().flat_map(|_| z.iter().copied()).collect::<Vec<_>>().into_pyarray(py)))
    }

    fn accept(&mut self, py: Python<'_>, heights: PyReadonlyArray1<'_, f64>) -> PyResult<()> {
        let i = self.current.ok_or_else(|| PyValueError::new_err("no pending traffic route"))?;
        let z = heights.as_array();
        let c = &self.candidates[i];
        if z.len() != self.lanes.len() || z.len() != c.xy.len() * c.directions.len() {
            return Err(PyValueError::new_err("traffic heights must match the lane points"));
        }
        let made = py.detach(|| {
            let mut out = Vec::new();
            for (k, direction) in c.directions.iter().enumerate() {
                let a = k * c.xy.len();
                let h: Vec<_> = z.iter().skip(a).take(c.xy.len()).copied().collect();
                if h.iter().any(|v| !v.is_finite()) { continue; }
                let mut points = compact(&self.lanes[a..a + c.xy.len()], &h);
                if *direction < 0 { points.reverse(); }
                out.push(Route { id: format!("{}:{}:{direction}", c.id, c.suffix), points, speed: c.speed,
                    spacing: c.spacing, reach: c.reach, class: CLASSES[c.class].into(), surface: c.surface.clone() });
            }
            out
        });
        self.points += made.iter().map(|r| r.points.len()).sum::<usize>();
        self.counts[c.group] += made.len();
        self.routes.extend(made);
        self.used[i] = true;
        self.current = None;
        self.lanes.clear();
        Ok(())
    }

    fn routes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let out = PyList::empty(py);
        for r in &self.routes {
            let row = PyDict::new(py);
            row.set_item("id", &r.id)?;
            row.set_item("points", r.points.iter().map(|p| p.to_vec()).collect::<Vec<_>>())?;
            row.set_item("speed", r.speed)?;
            row.set_item("offset", 0.0)?;
            row.set_item("spacing", r.spacing)?;
            row.set_item("reach", r.reach)?;
            row.set_item("roadClass", &r.class)?;
            row.set_item("surface", &r.surface)?;
            out.append(row)?;
        }
        Ok(out)
    }
}
