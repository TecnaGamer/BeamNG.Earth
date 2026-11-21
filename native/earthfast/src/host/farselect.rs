//! The far field's selection called from Rust (the Rust port's phase 2b, part 2): farfield.select / select_global's
//! wrappers (the kernels, farsel.rs / farglobal.rs, are called here directly), and earthd Connection's update_far,
//! horizon_select, _far_unchanged, view, lod_ppr, kfun, forced, map_cone, global_on, eye_clearance and
//! prebuild_coarse. Python keeps the horizon grids (Server.horizon_grid builds them), the tile objects (farfield.Tile /
//! GTile / Piece: the builders take them) and the regroup call (Connection._regroup).
//!
//! The selection's state (the refined nodes, the held ones, the last selection's inputs and slack, the camera's
//! pixels-per-radian history) lives in the connection's FarState, under its lock; no Python runs while it is held.
use std::collections::{BTreeMap, BTreeSet, VecDeque};

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArrayMethods, PyReadonlyArray2};
use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::types::{PyModule, PyTuple};

use super::*;
use crate::dm::Dm;

/// A quadtree node: an anchor's (level, i, j) or a global tile's ("m", z, x, y).
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
pub enum Node {
    A(i32, i64, i64),
    G(u32, i64, i64),
}

impl Node {
    pub fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        match *self {
            Node::A(l, i, j) => (l, i, j).into_pyobject(py),
            Node::G(z, x, y) => ("m", z, x, y).into_pyobject(py),
        }
    }

    pub fn of(t: &Bound<'_, PyAny>) -> PyResult<Node> {
        let t = t.cast::<PyTuple>()?;
        if t.len() == 4 {
            Ok(Node::G(t.get_item(1)?.extract()?, t.get_item(2)?.extract()?, t.get_item(3)?.extract()?))
        } else {
            let (l, i, j): (i32, i64, i64) = t.extract()?;
            Ok(Node::A(l, i, j))
        }
    }

    fn plain(&self) -> Option<(i32, i64, i64)> {
        match *self {
            Node::A(l, i, j) => Some((l, i, j)),
            Node::G(..) => None,
        }
    }

    fn global(&self) -> Option<(u32, i64, i64)> {
        match *self {
            Node::G(z, x, y) => Some((z, x, y)),
            Node::A(..) => None,
        }
    }
}

/// _far_unchanged's discrete inputs (equal: the same selection, given a move under the slack).
#[derive(Clone, PartialEq)]
struct Disc {
    active: i64,
    forced: Vec<[f64; 4]>,
    lod_scale: f64,
    map_view: bool,
    map_open: bool,
    lod_ramp: f64,
    ramp_none: bool,
    min_level: i32,
    held: Vec<Node>,
    disc: Option<bool>,
    max_level: Option<i32>,
    /// (the eye, the grid's address: the grid itself is held with it)
    hz: Option<(f64, usize)>,
    view_cap: Option<f64>,
    global: bool,
    cone: Option<Vec<f64>>,
    high: bool,
    view: Option<([f64; 6], i64)>,
}

type PieceKey = (i32, i64, i64, u32, i64, i64);

/// The selection's part of a connection's far state.
#[derive(Default)]
pub struct SelSt {
    /// Exact-level parent boxes on the driving path, also used to order their builds.
    pub driving: Vec<[f64; 4]>,
    pub refined: BTreeSet<Node>,
    pub node_changed: BTreeMap<Node, f64>,
    /// bumped whenever Python sets the desired tiles or the refined nodes (the last selection's result replaced)
    pub gen: u64,
    /// (inputs, continuous inputs, slack, gen, the horizon grid held) of the last selection whose inputs were kept
    last: Option<(Disc, Vec<f64>, f64, u64, Option<Py<PyAny>>)>,
    /// (monotonic time, ppr): decreasing pprs, the sliding maximum (lod_ppr)
    ppr_hist: VecDeque<(f64, f64)>,
    pub lod_grid: Option<Py<PyAny>>,
    /// the last selection's pieces: reused while their polygon and quad are the same (a new object each time held the
    /// cycle collector and the tiles' records)
    pieces: HashMap<PieceKey, (Vec<f64>, Vec<f64>, Py<PyAny>)>,
    /// selections skipped by the gate (the tests)
    pub skipped: u64,
}

// --- small helpers ----------------------------------------------------------------------------------------------------

fn pyfloat(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(f) = v.cast::<PyFloat>() {
        return Ok(f.value());
    }
    v.py().get_type::<PyFloat>().call1((v,))?.extract()
}

/// float(v), None on the errors Python's `except (KeyError, TypeError, ValueError)` took
fn try_float(py: Python<'_>, v: PyResult<Bound<'_, PyAny>>) -> PyResult<Option<f64>> {
    let r = v.and_then(|v| pyfloat(&v));
    match r {
        Ok(x) => Ok(Some(x)),
        Err(e) => {
            if e.is_instance_of::<PyKeyError>(py) || e.is_instance_of::<PyTypeError>(py)
                || e.is_instance_of::<PyValueError>(py) {
                Ok(None)
            } else {
                Err(e)
            }
        }
    }
}

fn attr_or<'py>(o: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    match o.getattr(name) {
        Ok(v) => Ok(Some(v)),
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyAttributeError>(o.py()) => Ok(None),
        Err(e) => Err(e),
    }
}

fn opt<'py>(v: Bound<'py, PyAny>) -> Option<Bound<'py, PyAny>> {
    if v.is_none() { None } else { Some(v) }
}

fn ff_mod(py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
    py.import("farfield")
}

fn fget(m: &Bound<'_, PyAny>, name: &str) -> PyResult<f64> {
    pyfloat(&m.getattr(name)?)
}

fn iget(m: &Bound<'_, PyAny>, name: &str) -> PyResult<i64> {
    m.getattr(name)?.extract()
}

/// numpy.ascontiguousarray(a, numpy.float64), readonly
fn contig<'py>(a: &Bound<'py, PyAny>) -> PyResult<PyReadonlyArray2<'py, f64>> {
    let np = a.py().import("numpy")?;
    np.call_method1("ascontiguousarray", (a, np.getattr("float64")?))?.extract()
}

/// Request whole swap groups along the car's path. At 30 m/s the usual 150 m exact ring
/// allows only five seconds for *all* siblings to build, import and replace their parent.
const DRIVE_LEAD_S: f64 = 12.0;
const DRIVE_LEAD_MAX_M: f64 = 512.0;

fn driving_boxes((x, y): (f64, f64), (vx, vy): (f64, f64), parent: f64, exact_radius: f64) -> Vec<[f64; 4]> {
    let speed = vx.dhypot(vy);
    let lead = (speed * DRIVE_LEAD_S).min(DRIVE_LEAD_MAX_M);
    if !speed.is_finite() || exact_radius <= 0.0 || lead <= exact_radius {
        return Vec::new();
    }
    let (ex, ey) = (x + (vx / speed) * lead, y + (vy / speed) * lead);
    let cells = |a: f64, b: f64| (a.min(b) / parent).floor() as i64..=(a.max(b) / parent).floor() as i64;
    let mut boxes = Vec::new();
    for i in cells(x, ex) {
        for j in cells(y, ey) {
            if crate::clearance::seg_cell(x, y, ex, ey, i, j, parent) {
                let (left, bottom) = (i as f64 * parent, j as f64 * parent);
                boxes.push([left, bottom, left + parent, bottom + parent]);
            }
        }
    }
    boxes
}

fn level_size(ff: &Bound<'_, PyModule>, level: i64) -> PyResult<f64> {
    Ok(fget(ff, "BASE")? * libm::ldexp(1.0, level as i32))
}

/// CPython's math.hypot (its own algorithm, not always the correctly rounded one: the deleted Python's values bit for bit)
fn py_hypot(py: Python<'_>, a: f64, b: f64) -> PyResult<f64> {
    py.import("math")?.getattr("hypot")?.call1((a, b))?.extract()
}

/// The camera for screen-space LOD: (cx, cy, dz, ppr, tau, reach, h, cap, cells), the errors {(aid, node): err} (or a
/// callable / a dict by node, aid None) and the global tiles' {(z, x, y): err}.
pub struct ViewA<'py> {
    pub v: [f64; 9],
    pub errs: Bound<'py, PyAny>,
    pub aid: Bound<'py, PyAny>,
    pub gerr: Bound<'py, PyAny>,
}

/// One selection's inputs (farfield.select's arguments).
pub struct SelIn<'py> {
    pub px: f64,
    pub py: f64,
    pub forced: Vec<(f64, f64, f64, f64)>,
    pub prev: Vec<Node>,
    pub hold: Vec<Node>,
    /// K of levels 0..=max_level (0 below level 1)
    pub ks: Vec<f64>,
    pub h: f64,
    pub radius: f64,
    pub max_level: i32,
    pub min_level: i32,
    pub radii: Vec<(i32, f64)>,
    pub disc: bool,
    /// (the eye m above sea level, the HorizonGrid)
    pub horizon: Option<(f64, Bound<'py, PyAny>)>,
    pub forced_level: i32,
    pub view: Option<ViewA<'py>>,
    pub eye_h: f64,
    pub cone: Option<Vec<f64>>,
}

/// The selection's result: (key, tile) in Python's dict order (a key again replaces the value in its place).
#[derive(Default)]
pub struct Out {
    items: Vec<(Key, Py<PyAny>)>,
    at: HashMap<Key, usize>,
}

impl Out {
    fn put(&mut self, k: Key, v: Py<PyAny>) {
        if let Some(&i) = self.at.get(&k) {
            self.items[i].1 = v;
        } else {
            self.at.insert(k.clone(), self.items.len());
            self.items.push((k, v));
        }
    }

    pub fn dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        for (k, v) in &self.items {
            d.set_item(k.as_ref(), v.bind(py))?;
        }
        Ok(d)
    }
}

/// The refined finest global tiles the anchor's nodes fill (farfield.Region): [(z, x, y)], their quads.
struct RegionA {
    tiles: Vec<(u32, i64, i64)>,
    quads: Vec<Vec<f64>>,
}

fn horizon_reach(ff: &Bound<'_, PyModule>, eye: f64) -> PyResult<f64> {
    pyfloat(&ff.getattr("horizon_distance")?.call1((eye,))?)
}

/// farfield.select's call of far_select_nodes, the tiles made (Tile by tile_of; Piece, reused from `memo`).
fn sel_plain<'py>(py: Python<'py>, ff: &Bound<'py, PyModule>, a: &SelIn<'py>, max_level: i32, hz_on: bool,
                  region: Option<&RegionA>, mut memo: Option<&mut HashMap<PieceKey, (Vec<f64>, Vec<f64>, Py<PyAny>)>>,
                  seen: &mut Vec<PieceKey>, out: &mut Out) -> PyResult<(Vec<Node>, f64)> {
    let n = (max_level.max(0) + 1) as usize;
    let ks: Vec<f64> = (0..n).map(|l| if l >= 1 { a.ks[l] } else { 0.0 }).collect();
    let hz = match (&a.horizon, hz_on) {
        (Some((eye, grid)), true) => Some((horizon_reach(ff, *eye)?, fget(ff, "HORIZON_MIN")?, fget(ff, "R_REFRACTED")?,
                                           contig(&grid.getattr("base")?)?, iget(ff, "HORIZON_GRID_LEVEL")? as i32,
                                           iget(ff, "HORIZON_MAX_LEVEL")? as i32)),
        _ => None,
    };
    let vw = a.view.as_ref().filter(|v| v.v[4] > 0.0).map(|v| {
        (v.v[0], v.v[1], v.v[2], v.v[3], v.v[4], v.v[5], v.v[6], v.v[7], v.v[8], v.errs.clone(), v.aid.clone())
    });
    let reg = match region {
        Some(r) => {
            let flat: Vec<f64> = r.quads.iter().flatten().copied().collect();
            let arr = Array2::from_shape_vec((r.quads.len(), 8), flat)
                .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py);
            Some((arr.readonly(), fget(ff, "GLOBAL_MIN_AREA")?))
        }
        None => None,
    };
    let prev: Vec<(i32, i64, i64)> = a.prev.iter().filter_map(Node::plain).collect();
    let hold: Vec<(i32, i64, i64)> = a.hold.iter().filter_map(Node::plain).collect();
    let (items, refined, slack) = crate::farsel::far_select_nodes(
        a.px, a.py, a.radius, a.disc, max_level, a.min_level, a.forced_level, a.h, a.eye_h, ks, a.radii.clone(), prev,
        hold, a.forced.clone(), hz, vw, reg, a.cone.clone())?;
    let tile_of = ff.getattr("tile_of")?;
    let piece = ff.getattr("Piece")?;
    for (lvl, i, j, qi, poly) in items {
        if qi < 0 {
            let k = format!("{lvl}_{i}_{j}");
            let t = tile_of.call1((k.as_str(),))?;
            out.put(Key::from(k.as_str()), t.unbind());
        } else {
            let r = region.ok_or_else(|| PyRuntimeError::new_err("a piece without a region"))?;
            let (z, x, y) = r.tiles[qi as usize];
            let quad = &r.quads[qi as usize];
            let pk: PieceKey = (lvl, i, j, z, x, y);
            let k = Key::from(format!("{lvl}_{i}_{j}_c{z}_{x}_{y}").as_str());
            let have = memo.as_ref().and_then(|m| m.get(&pk)).filter(|(p, q, _)| *p == poly && q == quad)
                .map(|(_, _, o)| o.clone_ref(py));
            let t = match have {
                Some(t) => t,
                None => {
                    let t = piece.call1((lvl, i, j, z, x, y, PyTuple::new(py, &poly)?, PyTuple::new(py, quad)?))?.unbind();
                    if let Some(m) = memo.as_mut() {
                        m.insert(pk, (poly, quad.clone(), t.clone_ref(py)));
                    }
                    t
                }
            };
            seen.push(pk);
            out.put(k, t);
        }
    }
    Ok((refined.into_iter().map(|(l, i, j)| Node::A(l, i, j)).collect(), slack))
}

type GMemo = Vec<((u64, u64, u32), HashMap<(u32, i64, i64), Py<PyAny>>)>;
/// select_global's tiles of an anchor's world (lat0, lon0, zf) -> {(z, x, y): GTile}: the last few anchors
static GTILE_MEMO: Mutex<GMemo> = Mutex::new(Vec::new());
const GTILE_MEMO_MAX: usize = 20000;

/// (global part s, anchor part s, regions, global tiles) of the last select_global (the slow selection's log)
static LAST_GLOBAL: Mutex<Option<(f64, f64, usize, usize)>> = Mutex::new(None);

/// farfield.select_global: the levels from GLOBAL_MIN_LEVEL up as global tiles, the anchor's nodes (L <= 2, Tile /
/// Piece) in the refined zoom-zf tiles. Returns the refined nodes and the slack; the tiles in `out`.
#[allow(clippy::too_many_arguments)]
fn sel_global<'py>(py: Python<'py>, ff: &Bound<'py, PyModule>, lat0: f64, lon0: f64, zf: u32, a: &SelIn<'py>,
                   anchor_nodes: bool, memo: Option<&mut HashMap<PieceKey, (Vec<f64>, Vec<f64>, Py<PyAny>)>>,
                   seen: &mut Vec<PieceKey>, out: &mut Out) -> PyResult<(BTreeSet<Node>, f64)> {
    let gmin = iget(ff, "GLOBAL_MIN_LEVEL")? as i32;
    let gprev: Vec<(u32, i64, i64)> = a.prev.iter().filter_map(Node::global).collect();
    let ghold: Vec<(u32, i64, i64)> = a.hold.iter().filter_map(Node::global).collect();
    let ks: Vec<f64> = (0..(a.max_level + 1).max(0) as usize).map(|l| if l >= 1 { a.ks[l] } else { 0.0 }).collect();
    let rmin = a.radii.iter().filter(|(m, _)| *m >= a.min_level).map(|(_, r)| *r)
        .fold(None, |acc: Option<f64>, r| Some(match acc { Some(m) if !(r > m) => m, _ => r })).unwrap_or(0.0);
    let hz = match &a.horizon {
        Some((eye, g)) => {
            let merc = opt(g.getattr("merc")?);
            let merc = match merc {
                Some(m) => Some((m.get_item(0)?.extract::<u32>()?, m.get_item(1)?.extract::<i64>()?,
                                 m.get_item(2)?.extract::<i64>()?, m.get_item(3)?.extract::<PyReadonlyArray2<'py, f64>>()?)),
                None => None,
            };
            Some((horizon_reach(ff, *eye)?, fget(ff, "HORIZON_MIN")?, fget(ff, "R_REFRACTED")?, contig(&g.getattr("base")?)?,
                  fget(g, "half")?, level_size(ff, iget(ff, "HORIZON_GRID_LEVEL")?)?, merc))
        }
        None => None,
    };
    let view = match a.view.as_ref().filter(|v| v.v[4] > 0.0) {
        Some(v) => Some((v.v[0], v.v[1], v.v[2], v.v[3], v.v[4], v.v[5], v.v[6], v.v[7], v.v[8], v.gerr.cast::<PyDict>()?.clone())),
        None => None,
    };
    let t0 = std::time::Instant::now();
    let (tiles, grefined, regions, gslack) = crate::farglobal::far_global_select(
        lat0, lon0, a.px, a.py, a.radius, a.disc, zf, a.max_level, ks, rmin, a.h, gprev, ghold, a.forced.clone(), hz,
        a.eye_h, a.cone.clone(), view)?;
    let gtile = ff.getattr("GTile")?;
    let ntiles = tiles.len();
    {
        // (the memo's lock is never held while Python runs: no GTile made, nor one let go, under it; another thread
        // waiting for it holds the GIL)
        let mk = (lat0.to_bits(), lon0.to_bits(), zf);
        let (have, gone): (Vec<Option<Py<PyAny>>>, Vec<HashMap<(u32, i64, i64), Py<PyAny>>>) = {
            let mut gm = GTILE_MEMO.lock().unwrap_or_else(|p| p.into_inner());
            let mut gone = Vec::new();
            let pos = match gm.iter().position(|(k, m)| *k == mk && m.len() <= GTILE_MEMO_MAX) {
                Some(p) => p,
                None => {
                    if let Some(p) = gm.iter().position(|(k, _)| *k == mk) {
                        gone.push(std::mem::take(&mut gm[p].1));
                        p
                    } else {
                        if gm.len() >= 4 {
                            gone.push(gm.remove(0).1);
                        }
                        gm.push((mk, HashMap::new()));
                        gm.len() - 1
                    }
                }
            };
            let m = &gm[pos].1;
            (tiles.iter().map(|(z, x, y, _, _)| m.get(&(*z, *x, *y)).map(|t| t.clone_ref(py))).collect(), gone)
        };
        drop(gone);
        let mut made = Vec::new();
        for ((z, x, y, q, s), t) in tiles.into_iter().zip(have) {
            let t = match t {
                Some(t) => t,
                None => {
                    let t = gtile.call1((z, x, y, gmin + zf as i32 - z as i32, PyTuple::new(py, &q)?, s))?.unbind();
                    made.push(((z, x, y), t.clone_ref(py)));
                    t
                }
            };
            out.put(Key::from(format!("m{z}_{x}_{y}").as_str()), t);
        }
        if !made.is_empty() {
            let mut gm = GTILE_MEMO.lock().unwrap_or_else(|p| p.into_inner());
            if let Some(p) = gm.iter().position(|(k, _)| *k == mk) {
                for (k, t) in made.drain(..) {
                    gm[p].1.entry(k).or_insert(t);
                }
            }
            // (made's leftovers, a memo replaced meanwhile, are let go after the lock)
        }
        drop(made);
    }
    let mut refined: BTreeSet<Node> = grefined.into_iter().map(|(z, x, y)| Node::G(z, x, y)).collect();
    let t1 = t0.elapsed().as_secs_f64();
    let mut regions = regions;
    if !regions.is_empty() && !anchor_nodes {
        // (a camera high above the ground, the 3D map: the finest global tiles are leaves, no anchor nodes in them)
        for (x, y, q) in &regions {
            // (the mean edge as Python made it: math.hypot, and sum's compensated addition)
            let e = PyList::empty(py);
            for k in 0..4 {
                e.append(py_hypot(py, q[(2 * k + 2) % 8] - q[2 * k], q[(2 * k + 3) % 8] - q[2 * k + 1])?)?;
            }
            let s: f64 = py.import("builtins")?.getattr("sum")?.call1((e,))?.extract::<f64>()? / 4.0;
            let t = gtile.call1((zf, *x, *y, gmin, PyTuple::new(py, q)?, s))?;
            out.put(Key::from(format!("m{zf}_{x}_{y}").as_str()), t.unbind());
            refined.remove(&Node::G(zf, *x, *y));
        }
        regions.clear();
    }
    let mut slack = gslack;
    let nreg = regions.len();
    if !regions.is_empty() {
        let reg = RegionA { tiles: regions.iter().map(|(x, y, _)| (zf, *x, *y)).collect(),
                            quads: regions.into_iter().map(|(_, _, q)| q).collect() };
        let a2 = SelIn { horizon: None, max_level: gmin - 1, ..clone_in(a) };
        let (aref, s2) = sel_plain(py, ff, &a2, gmin - 1, false, Some(&reg), memo, seen, out)?;
        refined.extend(aref);
        if s2 < slack {
            slack = s2;
        }
    }
    *LAST_GLOBAL.lock().unwrap_or_else(|p| p.into_inner()) = Some((t1, t0.elapsed().as_secs_f64() - t1, nreg, ntiles));
    Ok((refined, slack))
}

fn clone_in<'py>(a: &SelIn<'py>) -> SelIn<'py> {
    SelIn {
        px: a.px, py: a.py, forced: a.forced.clone(), prev: a.prev.clone(), hold: a.hold.clone(), ks: a.ks.clone(), h: a.h,
        radius: a.radius, max_level: a.max_level, min_level: a.min_level, radii: a.radii.clone(), disc: a.disc,
        horizon: a.horizon.as_ref().map(|(e, g)| (*e, g.clone())), forced_level: a.forced_level,
        view: a.view.as_ref().map(|v| ViewA { v: v.v, errs: v.errs.clone(), aid: v.aid.clone(), gerr: v.gerr.clone() }),
        eye_h: a.eye_h, cone: a.cone.clone(),
    }
}

// --- farfield.select / select_global for Python (the tests, the probes) ------------------------------------------------

fn nodes_of(v: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<Node>> {
    let mut out = Vec::new();
    if let Some(v) = v {
        for n in v.try_iter()? {
            out.push(Node::of(&n?)?);
        }
    }
    Ok(out)
}

fn boxes_of(v: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<(f64, f64, f64, f64)>> {
    let mut out = Vec::new();
    if let Some(v) = v {
        for b in v.try_iter()? {
            let f = floats(&b?)?;
            if f.len() != 4 {
                return Err(PyValueError::new_err("a forced box is (x0, y0, x1, y1)"));
            }
            out.push((f[0], f[1], f[2], f[3]));
        }
    }
    Ok(out)
}

fn radii_of(v: &Bound<'_, PyAny>) -> PyResult<Vec<(i32, f64)>> {
    let mut out = Vec::new();
    for kv in v.call_method0("items")?.try_iter()? {
        let kv = kv?;
        out.push((kv.get_item(0)?.extract()?, pyfloat(&kv.get_item(1)?)?));
    }
    Ok(out)
}

/// A farfield.View's values (None: tau 0 or no view)
fn view_of<'py>(v: Option<&Bound<'py, PyAny>>) -> PyResult<Option<ViewA<'py>>> {
    let Some(v) = v else { return Ok(None) };
    let tau = fget(v, "tau")?;
    if !(tau > 0.0) {
        return Ok(None);
    }
    let err = v.getattr("err")?;
    let (errs, aid) = match (attr_or(&err, "errs")?, attr_or(&err, "aid")?) {
        (Some(e), Some(a)) => (e, a),
        _ => (err.clone(), v.py().None().into_bound(v.py())),
    };
    Ok(Some(ViewA {
        v: [fget(v, "cx")?, fget(v, "cy")?, fget(v, "dz")?, fget(v, "ppr")?, tau, fget(v, "reach")?, fget(v, "h")?,
            fget(v, "cap")?, fget(v, "cells")?],
        errs, aid, gerr: v.getattr("gerr")?,
    }))
}

#[allow(clippy::too_many_arguments)]
fn sel_in_py<'py>(ff: &Bound<'py, PyModule>, px: f64, py_: f64, forced: Option<&Bound<'py, PyAny>>,
                  prev: Option<&Bound<'py, PyAny>>, kfun: Option<&Bound<'py, PyAny>>, h: Option<f64>, radius: Option<f64>,
                  max_level: Option<i32>, hold: Option<&Bound<'py, PyAny>>, min_level: i32,
                  radii: Option<&Bound<'py, PyAny>>, layout: Option<&Bound<'py, PyAny>>, disc: bool,
                  horizon: Option<&Bound<'py, PyAny>>, forced_level: i32, view: Option<&Bound<'py, PyAny>>, eye_h: f64,
                  cone: Option<Vec<f64>>) -> PyResult<SelIn<'py>> {
    let layout = match layout { Some(l) => l.clone(), None => ff.getattr("V131")? };
    let kfun = match kfun { Some(k) => k.clone(), None => layout.getattr("k")? };
    let max_level = match max_level { Some(m) => m, None => iget(ff, "MAX_LEVEL")? as i32 };
    let radii = match radii {
        Some(r) => radii_of(r)?,
        None => radii_of(&ff.getattr("NEAR_RADII")?.get_item(layout.getattr("cells")?)?)?,
    };
    let mut ks = Vec::new();
    for l in 0..=max_level.max(3) {
        ks.push(if l >= 1 { pyfloat(&kfun.call1((l,))?)? } else { 0.0 });
    }
    let horizon = match horizon {
        Some(hz) => Some((fget(hz, "eye")?, hz.getattr("grid")?)),
        None => None,
    };
    Ok(SelIn {
        px, py: py_, forced: boxes_of(forced)?, prev: nodes_of(prev)?, hold: nodes_of(hold)?, ks,
        h: match h { Some(h) => h, None => fget(ff, "HYSTERESIS")? },
        radius: match radius { Some(r) => r, None => fget(ff, "RADIUS")? },
        max_level, min_level, radii, disc, horizon, forced_level, view: view_of(view)?, eye_h, cone,
    })
}

fn result_py<'py>(py: Python<'py>, out: &Out, refined: impl IntoIterator<Item = Node>, slack: f64, with_slack: bool)
                  -> PyResult<Bound<'py, PyTuple>> {
    let set = PySet::empty(py)?;
    for n in refined {
        set.add(n.to_py(py)?)?;
    }
    if with_slack {
        PyTuple::new(py, [out.dict(py)?.into_any(), set.into_any(), PyFloat::new(py, slack).into_any()])
    } else {
        PyTuple::new(py, [out.dict(py)?.into_any(), set.into_any()])
    }
}

/// farfield.select: the desired tiles for a player at (px, py) ({key: Tile / Piece}, the refined nodes; with_slack:
/// and the slack). region: (zf, [(x, y, quad)]) the refined finest global tiles the nodes fill, or None.
#[pyfunction]
#[pyo3(signature = (px, py, forced=None, prev_refined=None, kfun=None, h=None, radius=None, max_level=None, hold=None,
                    min_level=0, radii=None, layout=None, disc=false, horizon=None, forced_level=0, view=None,
                    region=None, eye_h=0.0, cone=None, with_slack=false))]
#[allow(clippy::too_many_arguments)]
pub fn far_select<'py>(gil: Python<'py>, px: f64, py: f64, forced: Option<&Bound<'py, PyAny>>, prev_refined: Option<&Bound<'py, PyAny>>,
                       kfun: Option<&Bound<'py, PyAny>>, h: Option<f64>, radius: Option<f64>, max_level: Option<i32>,
                       hold: Option<&Bound<'py, PyAny>>, min_level: i32, radii: Option<&Bound<'py, PyAny>>,
                       layout: Option<&Bound<'py, PyAny>>, disc: bool, horizon: Option<&Bound<'py, PyAny>>,
                       forced_level: i32, view: Option<&Bound<'py, PyAny>>, region: Option<&Bound<'py, PyAny>>,
                       eye_h: f64, cone: Option<Vec<f64>>, with_slack: bool) -> PyResult<Bound<'py, PyTuple>> {
    let ff = ff_mod(gil)?;
    let a = sel_in_py(&ff, px, py, forced, prev_refined, kfun, h, radius, max_level, hold, min_level, radii, layout, disc,
                      horizon, forced_level, view, eye_h, cone)?;
    let reg = match region {
        Some(r) => {
            let zf: u32 = r.get_item(0)?.extract()?;
            let mut tiles = Vec::new();
            let mut quads = Vec::new();
            for it in r.get_item(1)?.try_iter()? {
                let it = it?;
                tiles.push((zf, it.get_item(0)?.extract()?, it.get_item(1)?.extract()?));
                quads.push(floats(&it.get_item(2)?)?);
            }
            Some(RegionA { tiles, quads })
        }
        None => None,
    };
    let mut out = Out::default();
    let (refined, slack) = sel_plain(gil, &ff, &a, a.max_level, true, reg.as_ref(), None, &mut Vec::new(), &mut out)?;
    result_py(gil, &out, refined, slack, with_slack)
}

/// farfield.select_global (frame: the anchor's EnuFrame, its lat0 / lon0; zf the finest global zoom).
#[pyfunction]
#[pyo3(signature = (frame, px, py, zf, forced=None, prev_refined=None, kfun=None, h=None, radius=None, max_level=None,
                    hold=None, min_level=0, radii=None, layout=None, disc=false, horizon=None, forced_level=0,
                    view=None, anchor_nodes=true, eye_h=0.0, cone=None, with_slack=false))]
#[allow(clippy::too_many_arguments)]
pub fn far_select_global<'py>(frame: &Bound<'py, PyAny>, px: f64, py: f64, zf: u32, forced: Option<&Bound<'py, PyAny>>,
                              prev_refined: Option<&Bound<'py, PyAny>>, kfun: Option<&Bound<'py, PyAny>>, h: Option<f64>,
                              radius: Option<f64>, max_level: Option<i32>, hold: Option<&Bound<'py, PyAny>>,
                              min_level: i32, radii: Option<&Bound<'py, PyAny>>, layout: Option<&Bound<'py, PyAny>>,
                              disc: bool, horizon: Option<&Bound<'py, PyAny>>, forced_level: i32,
                              view: Option<&Bound<'py, PyAny>>, anchor_nodes: bool, eye_h: f64, cone: Option<Vec<f64>>,
                              with_slack: bool) -> PyResult<Bound<'py, PyTuple>> {
    let gil = frame.py();
    let ff = ff_mod(gil)?;
    let a = sel_in_py(&ff, px, py, forced, prev_refined, kfun, h, radius, max_level, hold, min_level, radii, layout, disc,
                      horizon, forced_level, view, eye_h, cone)?;
    let mut out = Out::default();
    let (refined, slack) = sel_global(gil, &ff, fget(frame, "lat0")?, fget(frame, "lon0")?, zf, &a, anchor_nodes, None,
                                      &mut Vec::new(), &mut out)?;
    result_py(gil, &out, refined, slack, with_slack)
}

/// (global part s, anchor part s, regions, global tiles) of the last select_global, or None (farfield's
/// LAST_GLOBAL_SELECT).
#[pyfunction]
pub fn far_last_global_select() -> Option<(f64, f64, usize, usize)> {
    *LAST_GLOBAL.lock().unwrap_or_else(|p| p.into_inner())
}

// --- the connection's selection (earthd Connection) ----------------------------------------------------------------

/// The horizon's part of a selection (horizon_select's extra arguments).
#[derive(Default)]
struct Ext<'py> {
    max_level: Option<i32>,
    disc: Option<bool>,
    horizon: Option<(f64, Bound<'py, PyAny>)>,
}

/// What a selection reads: the connection, the server, farfield and earthd's globals (live: the tests patch them).
struct Cx<'a, 'py> {
    py: Python<'py>,
    conn: &'a Bound<'py, PyAny>,
    s: &'a Bound<'py, PyAny>,
    ff: Bound<'py, PyModule>,
    g: Bound<'py, PyDict>,
}

impl<'py> Cx<'_, 'py> {
    fn g(&self, name: &str) -> PyResult<Bound<'py, PyAny>> {
        self.g.get_item(name)?.ok_or_else(|| PyKeyError::new_err(format!("earthd.{name}")))
    }

    fn gf(&self, name: &str) -> PyResult<f64> {
        pyfloat(&self.g(name)?)
    }

    fn truthy(&self, o: &Bound<'py, PyAny>, name: &str) -> PyResult<bool> {
        match attr_or(o, name)? {
            Some(v) => v.is_truthy(),
            None => Ok(false),
        }
    }

    fn map_on(&self) -> PyResult<bool> {
        Ok(self.truthy(self.conn, "map_view")? || self.truthy(self.conn, "map_open")?)
    }

    fn focus(&self) -> PyResult<(f64, f64)> {
        let f = self.conn.getattr("view_focus")?;
        Ok((pyfloat(&f.get_item(0)?)?, pyfloat(&f.get_item(1)?)?))
    }

    fn eye(&self) -> PyResult<Option<f64>> {
        match attr_or(self.conn, "eye")?.and_then(opt) {
            Some(e) => Ok(Some(pyfloat(&e)?)),
            None => Ok(None),
        }
    }

    fn log_msg(&self, level: &str, args: Bound<'py, PyTuple>) -> PyResult<()> {
        self.g("log")?.call_method1(level, args)?;
        Ok(())
    }

    fn perf(&self) -> PyResult<f64> {
        pyfloat(&self.g("time")?.call_method0("perf_counter")?)
    }

    /// Connection.global_on: global far tiles for this selection (the mod places them, the radius within Web Mercator)
    fn global_on(&self, anchor: &Bound<'py, PyAny>, radius: f64) -> PyResult<bool> {
        Ok(self.truthy(self.conn, "global_far")?
            && fget(anchor, "lat")?.abs() + (radius + 20000.0) / 110574.0 < fget(&self.ff, "GLOBAL_MAX_LAT")?)
    }

    /// K of levels 0..=n (Connection.kfun: the server's times the mod's terrain detail, times MAP_LOD_SCALE with the 3D
    /// map's view frame), times `ramp` (FIRST_RAMP_K) when given
    fn ks(&self, n: i32, ramp: Option<f64>) -> PyResult<Vec<f64>> {
        let k = self.s.getattr("kfun")?;
        let lod_ramp = match attr_or(self.conn, "lod_ramp")? { Some(v) => pyfloat(&v)?, None => 1.0 };
        let scale = fget(self.conn, "lod_scale")? * if self.map_on()? { self.gf("MAP_LOD_SCALE")? * lod_ramp } else { 1.0 };
        let mut out = Vec::new();
        for l in 0..=n.max(3) {
            if l < 1 {
                out.push(0.0);
                continue;
            }
            let v = k.call1((l,))?;
            let mut x = if scale == 1.0 { pyfloat(&v)? } else { pyfloat(&v.mul(scale)?)? };
            if let Some(r) = ramp {
                x *= r;
            }
            out.push(x);
        }
        Ok(out)
    }

    /// Connection.forced: boxes refined to the forced level (terrain / detail mode: the blocks; none in mesh mode)
    fn forced(&self) -> PyResult<Vec<(f64, f64, f64, f64)>> {
        let nv = self.s.getattr("near_visual")?;
        if !self.g("BLOCK_DRAWN")?.contains(&nv)? {
            return Ok(Vec::new());
        }
        let g = if nv.eq("detail")? { self.gf("DETAIL_RING_M")? } else { 0.0 };
        let mut out = Vec::new();
        for name in ["block", "pending_block", "predicted_block"] {
            if let Some(b) = attr_or(self.conn, name)?.and_then(opt) {
                let bb = floats(&b.getattr("bbox")?)?;
                out.push((bb[0] - g, bb[1] - g, bb[2] + g, bb[3] + g));
            }
        }
        Ok(out)
    }

    /// Connection.map_cone: the 3D map's view cone for the selection, or None
    fn map_cone(&self, aid: &Bound<'py, PyAny>) -> PyResult<Option<Vec<f64>>> {
        let cam = self.conn.getattr("cam")?;
        if !self.map_on()? || !cam.is_instance_of::<PyDict>() {
            return Ok(None);
        }
        let ca = attr_or(self.conn, "cam_aid")?.unwrap_or_else(|| self.py.None().into_bound(self.py));
        if !ca.eq(aid)? {
            return Ok(None);
        }
        let c = self.ff.getattr("view_cone")?.call1((&cam,))?;
        if c.is_none() || c.len()? != 8 {
            return Ok(None);
        }
        let mut v = floats(&c)?;
        v.push(self.gf("MAP_OUT_OF_VIEW_K")?);
        Ok(Some(v))
    }
}

impl FarState {
    fn cx<'a, 'py>(&self, conn: &'a Bound<'py, PyAny>, s: &'a Bound<'py, PyAny>) -> PyResult<Cx<'a, 'py>> {
        let py = conn.py();
        let g = self.cfg.globals.as_ref().ok_or_else(|| PyRuntimeError::new_err("FarState: no earthd globals (g)"))?;
        Ok(Cx { py, conn, s, ff: ff_mod(py)?, g: g.bind(py).clone() })
    }

    /// Connection.eye_clearance: the camera's height over the ground under the view (0 below EYE_LOD_MIN_M), the
    /// highest terrain of the horizon grid's cell there
    fn eye_clear(&self, c: &Cx<'_, '_>) -> PyResult<f64> {
        let Some(eye) = c.eye()? else { return Ok(0.0) };
        if eye < c.gf("EYE_LOD_MIN_M")? {
            return Ok(0.0);
        }
        let grid = self.lock(c.py)?.sel.lod_grid.as_ref().map(|g| g.clone_ref(c.py));
        let mut zg = 0.0;
        if let Some(grid) = grid {
            let gl = iget(&c.ff, "HORIZON_GRID_LEVEL")?;
            let s = level_size(&c.ff, gl)?;
            let (fx, fy) = c.focus()?;
            let z = pyfloat(&grid.bind(c.py).call_method1("zmax", (gl, (fx / s).floor() as i64, (fy / s).floor() as i64))?)?;
            if z.is_finite() {
                zg = if z > 0.0 { z } else { 0.0 };
            }
        }
        let e = eye - zg;
        Ok(if e > 0.0 { e } else { 0.0 })
    }

    /// Connection.lod_ppr: the pixels per radian the screen-space LOD goes by (this pose's, capped at LOD_PPR_ZOOM
    /// times a LOD_BASE_FOV_DEG camera's, the most of the last LOD_PPR_WINDOW_S seconds)
    fn lod_ppr_at(&self, py: Python<'_>, g: &Bound<'_, PyDict>, h: f64, fov: f64, now: f64) -> PyResult<f64> {
        let gf = |n: &str| -> PyResult<f64> { pyfloat(&g.get_item(n)?.ok_or_else(|| PyKeyError::new_err(n.to_string()))?) };
        let rad = std::f64::consts::PI / 180.0;
        let mut ppr = h / (2.0 * (fov * rad / 2.0).dtan());
        let cap = gf("LOD_PPR_ZOOM")? * h / (2.0 * (gf("LOD_BASE_FOV_DEG")? * rad / 2.0).dtan());
        if cap < ppr {
            ppr = cap;
        }
        let win = gf("LOD_PPR_WINDOW_S")?;
        let mut st = self.lock(py)?;
        let hist = &mut st.sel.ppr_hist;
        while hist.back().is_some_and(|b| b.1 <= ppr) {
            hist.pop_back();
        }
        hist.push_back((now, ppr));
        while hist.front().is_some_and(|f| now - f.0 > win) {
            hist.pop_front();
        }
        Ok(hist.front().map(|f| f.1).unwrap_or(ppr))
    }

    /// Connection.view: the camera for screen-space LOD from pose.cam, or None
    fn view_a<'py>(&self, c: &Cx<'_, 'py>) -> PyResult<Option<ViewA<'py>>> {
        let cam = c.conn.getattr("cam")?;
        let tau = match attr_or(c.conn, "lod_px")?.and_then(opt) {
            Some(v) => pyfloat(&v)?,
            None => fget(c.s, "lod_px")?,
        };
        let active = c.conn.getattr("active")?;
        let ca = attr_or(c.conn, "cam_aid")?.unwrap_or_else(|| c.py.None().into_bound(c.py));
        if !cam.is_instance_of::<PyDict>() || !(tau > 0.0) || !ca.eq(&active)? {
            return Ok(None);
        }
        let d = cam.cast::<PyDict>()?;
        let item = |k: &str| -> PyResult<Bound<'py, PyAny>> {
            d.get_item(k)?.ok_or_else(|| PyKeyError::new_err(k.to_string()))
        };
        let or = |k: &str, dflt: f64| -> PyResult<Bound<'py, PyAny>> {
            let v = d.get_item(k)?;
            Ok(match v { Some(v) if v.is_truthy()? => v, _ => PyFloat::new(c.py, dflt).into_any() })
        };
        let (Some(cx), Some(cy), Some(cz)) = (try_float(c.py, item("x"))?, try_float(c.py, item("y"))?, try_float(c.py, item("z"))?) else {
            return Ok(None);
        };
        let (Some(h), Some(fov)) = (try_float(c.py, or("h", 0.0))?, try_float(c.py, or("fovDeg", 60.0))?) else { return Ok(None) };
        if !(h > 0.0 && 1.0 < fov && fov < 170.0 && cx.is_finite() && cy.is_finite() && cz.is_finite()) {
            return Ok(None);
        }
        let now = self.now(c.py)?;
        let ppr = self.lod_ppr_at(c.py, &c.g, h, fov, now)?;
        let (mut reach, mut cap) = (fget(&c.ff, "SSE_REACH")?, fget(&c.ff, "SSE_SPACING_CAP")?);
        if let Ok(t) = std::env::var("BNGE_LOD") {
            if !t.is_empty() {
                let v: Vec<f64> = t.split(',').map(|s| s.trim().parse::<f64>()).collect::<Result<_, _>>()
                    .map_err(|e| PyValueError::new_err(format!("BNGE_LOD: {e}")))?;
                if v.len() != 2 {
                    return Err(PyValueError::new_err("BNGE_LOD: reach,cap"));
                }
                (reach, cap) = (v[0], v[1]);
            }
        }
        let pz = match attr_or(c.conn, "pz")? { Some(v) => pyfloat(&v)?, None => cz };
        let dz = cz - pz;
        Ok(Some(ViewA {
            v: [cx, cy, if dz > 0.0 { dz } else { 0.0 }, ppr, tau, reach, fget(&c.ff, "SSE_HYSTERESIS")?, cap,
                pyfloat(&c.s.getattr("layout")?.getattr("cells")?)?],
            errs: c.s.getattr("lod_errs")?, aid: active, gerr: c.s.getattr("glod_errs")?,
        }))
    }

    /// Connection.horizon_select: (radius, the extra arguments) for the active anchor: the disc to the line-of-sight
    /// horizon with the per-tile cull; HORIZON_MIN without a cull until the anchor's zmax grid is built
    fn horizon_select<'py>(&self, c: &Cx<'_, 'py>) -> PyResult<(f64, Ext<'py>)> {
        let anchor = c.s.getattr("anchors")?.get_item(c.conn.getattr("active")?)?;
        let eye = c.eye()?.unwrap_or(0.0);
        let mut grid = opt(c.s.call_method1("horizon_grid", (&anchor, eye))?);
        let mut focus = c.focus()?;
        if grid.is_none() && c.truthy(c.conn, "global_far")? {
            if let Some(ph) = attr_or(c.conn, "prev_horizon")?.and_then(opt) {
                // (a rebase whose grid is not built yet: the previous anchor's, the focus in its plane)
                let pframe = ph.get_item(0)?;
                grid = Some(ph.get_item(1)?);
                let ecef = anchor.getattr("frame")?.call_method1("enu_to_ecef", (focus.0, focus.1, 0.0))?;
                let enu = pframe.call_method1("ecef_to_enu", PyTuple::new(c.py, ecef.try_iter()?.collect::<PyResult<Vec<_>>>()?)?)?;
                focus = (pyfloat(&enu.get_item(0)?)?, pyfloat(&enu.get_item(1)?)?);
            }
        }
        let mut cull = None;
        let radius = match &grid {
            None => fget(&c.ff, "HORIZON_MIN")?,
            Some(g) => {
                let rz = g.call_method1("radius", (focus.0, focus.1, eye))?;
                let (radius, zr) = (pyfloat(&rz.get_item(0)?)?, rz.get_item(1)?);
                cull = Some((eye, g.clone()));
                self.lock(c.py)?.sel.lod_grid = Some(g.clone().unbind());
                let rrel = c.gf("RADIUS_REPORT_REL")?;
                let cur = fget(c.conn, "radius")?;
                if !zr.eq(c.conn.getattr("zmax_region")?)? || (radius - cur).abs() > rrel * cur {
                    c.log_msg("info", PyTuple::new(c.py, [
                        PyString::new(c.py, "horizon: eye %.0f m, highest visible terrain %.0f m, radius %.0f km").into_any(),
                        PyFloat::new(c.py, eye).into_any(), zr.clone(), PyFloat::new(c.py, radius / 1000.0).into_any()])?)?;
                }
                c.conn.setattr("zmax_region", zr)?;
                radius
            }
        };
        c.conn.setattr("radius", radius)?;
        let sent = attr_or(c.conn, "radius_sent")?.and_then(opt);
        let report = match sent {
            None => true,
            Some(s) => {
                let s = pyfloat(&s)?;
                (radius - s).abs() > c.gf("RADIUS_REPORT_REL")? * s
            }
        };
        if report {
            let last = attr_or(c.conn, "last_status")?.filter(|v| v.is_truthy().unwrap_or(false));
            let text = match last {
                Some(t) => t,
                None => PyString::new(c.py, "Horizon {:.0f} km").call_method1("format", (radius / 1000.0,))?,
            };
            c.conn.call_method1("status", (text,))?;
        }
        Ok((radius, Ext { max_level: Some(iget(c.s, "max_level")? as i32), disc: Some(true), horizon: cull }))
    }

    /// Connection.update_far: the desired set, the swap groups and the build queue; the streamer woken. gate (a pose):
    /// not made again when nothing the selection goes by has changed (the last one's slack).
    pub(super) fn update_far_impl(&self, conn: &Bound<'_, PyAny>, s: &Bound<'_, PyAny>, gate: bool) -> PyResult<()> {
        let c = self.cx(conn, s)?;
        let py = c.py;
        let active = conn.getattr("active")?;
        if active.is_none() {
            return Ok(());
        }
        let now = self.now(py)?;
        let keep = fget(s, "retired_keep")?;
        let hold_s = fget(&c.ff, "HOLD_S")?;
        {
            let mut st = self.lock(py)?;
            st.retired.retain(|_, t| now - *t < keep);
            st.sel.node_changed.retain(|_, t| now - *t < hold_s);
        }
        let (mut radius, mut ext) = if s.getattr("horizon")?.is_truthy()? {
            self.horizon_select(&c)?
        } else {
            (fget(s, "radius")?, Ext::default())
        };
        let view_cap = attr_or(conn, "view_cap")?.and_then(opt).map(|v| pyfloat(&v)).transpose()?;
        let map_on = c.map_on()?;
        if let Some(vc) = view_cap {
            if !map_on {
                if vc < radius {
                    radius = vc;
                }
                ext.disc = Some(true);
            }
        }
        let anchor = s.getattr("anchors")?.get_item(&active)?;
        let t_sel = c.perf()?;
        let ramp = !conn.getattr("first_ramp")?.is_none();
        let mut forced = c.forced()?;
        let mut min_level = iget(s, "min_level")? as i32;
        let mut view = self.view_a(&c)?;
        let max_level = match ext.max_level { Some(m) => m, None => iget(&c.ff, "MAX_LEVEL")? as i32 };
        let ks = if ramp {
            forced.clear();
            view = None;
            min_level = min_level.max(c.gf("FIRST_RAMP_MIN_LEVEL")? as i32);
            c.ks(max_level, Some(c.gf("FIRST_RAMP_K")?))?
        } else {
            c.ks(max_level, None)?
        };
        let global = c.global_on(&anchor, radius)?;
        let eye = c.eye()?;
        let high = eye.is_some_and(|e| e > c.gf("MAP_HIGH_EYE_M").unwrap_or(f64::INFINITY));
        let eye_h = self.eye_clear(&c)?;
        let (fx, fy) = c.focus()?;
        let radii = radii_of(&s.getattr("near_radii")?)?;
        let mut forced_level = iget(s, "forced_level")? as i32;
        let mut driving = Vec::new();
        if !ramp && !map_on && s.getattr("near_visual")?.eq("mesh")? && conn.getattr("view_at")?.is_none() {
            let exact_radius = radii.iter().find(|(l, _)| *l == min_level).map_or(0.0, |(_, r)| *r);
            if !global || eye_h < exact_radius {
                driving = driving_boxes((fx, fy), conn.getattr("vel")?.extract()?,
                                        level_size(&c.ff, i64::from(min_level + 1))?, exact_radius);
                forced.extend(driving.iter().map(|&[x0, y0, x1, y1]| (x0, y0, x1, y1)));
                forced_level = min_level;
            }
        }
        let cone = if global { c.map_cone(&anchor.getattr("id")?)? } else { None };
        // _far_unchanged's inputs
        let pend = if gate {
            let lod_ramp = match attr_or(conn, "lod_ramp")? { Some(v) => pyfloat(&v)?, None => 1.0 };
            let held: Vec<Node> = self.lock(py)?.sel.node_changed.keys().copied().collect();
            let disc = Disc {
                active: active.extract()?, forced: forced.iter().map(|b| [b.0, b.1, b.2, b.3]).collect(),
                lod_scale: fget(conn, "lod_scale")?, map_view: c.truthy(conn, "map_view")?,
                map_open: c.truthy(conn, "map_open")?, lod_ramp, ramp_none: !ramp, min_level, held, disc: ext.disc,
                max_level: ext.max_level, hz: ext.horizon.as_ref().map(|(e, g)| (*e, g.as_ptr() as usize)), view_cap, global,
                cone: cone.clone(), high,
                view: view.as_ref().map(|v| ([v.v[3], v.v[4], v.v[5], v.v[7], v.v[6], v.v[8]],
                                             attr_or(s, "lod_errs_gen").ok().flatten().and_then(|g| g.extract().ok()).unwrap_or(0))),
            };
            let mut cont = vec![fx, fy, radius, eye_h];
            if let Some(v) = &view {
                cont.extend([v.v[0], v.v[1], v.v[2]]);
            }
            let eps = c.gf("SELECT_SLACK_EPS")?;
            let last = {
                let st = self.lock(py)?;
                let gen = st.sel.gen;
                st.sel.last.as_ref().filter(|(ld, lc, _, lgen, _)| *ld == disc && lc.len() == cont.len() && *lgen == gen)
                    .map(|(_, lc, slack, _, _)| (lc.clone(), *slack))
            };
            if let Some((b, slack)) = last {
                {
                    let a = &cont;
                    let mut moved = py_hypot(py, a[0] - b[0], a[1] - b[1])? + (a[2] - b[2]).abs() + (a[3] - b[3]).abs();
                    if a.len() > 4 {
                        moved += py_hypot(py, a[4] - b[4], a[5] - b[5])? + (a[6] - b[6]).abs();
                    }
                    if moved < slack - eps {
                        self.lock(py)?.sel.skipped += 1;
                        // (the same selection surely: not made again; the build order goes by the focus, as after one)
                        conn.call_method0("_regroup")?;
                        conn.getattr("wake")?.call_method0("set")?;
                        return Ok(());
                    }
                }
            }
            Some((disc, cont, ext.horizon.as_ref().map(|(_, g)| g.clone().unbind())))
        } else {
            None
        };
        let (prev, hold): (Vec<Node>, Vec<Node>) = {
            let st = self.lock(py)?;
            (st.sel.refined.iter().copied().collect(), st.sel.node_changed.keys().copied().collect())
        };
        let a = SelIn {
            px: fx, py: fy, forced, prev, hold, ks, h: fget(s, "hysteresis")?, radius, max_level, min_level,
            radii, disc: ext.disc.unwrap_or(false), horizon: ext.horizon,
            forced_level, view, eye_h: 0.0, cone: None,
        };
        let mut out = Out::default();
        let mut seen = Vec::new();
        let mut memo = std::mem::take(&mut self.lock(py)?.sel.pieces);
        let res = if global {
            if high {
                s.call_method1("start_world_warm", (&anchor,))?;
            }
            let frame = anchor.getattr("frame")?;
            let a = SelIn { eye_h, cone, ..a };
            sel_global(py, &c.ff, fget(&frame, "lat0")?, fget(&frame, "lon0")?, anchor.getattr("zf")?.extract()?, &a,
                       !high && !ramp, Some(&mut memo), &mut seen, &mut out)
        } else {
            sel_plain(py, &c.ff, &a, a.max_level, true, None, Some(&mut memo), &mut seen, &mut out)
                .map(|(r, sl)| (r.into_iter().collect(), sl))
        };
        let keep_seen: HashSet<PieceKey> = seen.into_iter().collect();
        memo.retain(|k, _| keep_seen.contains(k));
        self.lock(py)?.sel.pieces = memo;
        let (refined, slack) = res?;
        let t_sel = c.perf()? - t_sel;
        if t_sel > c.gf("SLOW_SELECT_S")? {
            let lg = far_last_global_select();
            c.log_msg("warning", PyTuple::new(py, [
                PyString::new(py, "far selection took %.2f s (radius %.0f km, eye %.0f m, %d tiles, %d refined nodes; \
                                   global part, anchor part s, regions, global tiles: %s)").into_any(),
                PyFloat::new(py, t_sel).into_any(), PyFloat::new(py, radius / 1000.0).into_any(),
                PyFloat::new(py, eye.unwrap_or(0.0)).into_any(), out.items.len().into_pyobject(py)?.into_any(),
                refined.len().into_pyobject(py)?.into_any(), lg.into_pyobject(py)?.into_any()])?)?;
        }
        let dict = out.dict(py)?;
        let mut recs = Vec::with_capacity(out.items.len());
        let changed;
        {
            let mut st = self.lock(py)?;
            if !st.desired.is_empty() {
                let diff: Vec<Node> = refined.symmetric_difference(&st.sel.refined).copied().collect();
                for n in diff {
                    st.sel.node_changed.insert(n, now);
                }
            }
            for (k, t) in &out.items {
                recs.push(st.rec_keyed(t.bind(py), Some(PyString::new(py, k).as_any()))?);
            }
            changed = recs.len() != st.desired.len() || recs.iter().any(|r| !st.desired.contains_key(&r.key));
        }
        if self.cfg.far_trace {
            let last = attr_or(conn, "_sel_logged")?.map(|v| pyfloat(&v)).transpose()?.unwrap_or(0.0);
            let mono = self.now(py)?;
            if mono - last > 0.5 {
                conn.setattr("_sel_logged", mono)?;
                let mut lv: BTreeMap<i64, i64> = BTreeMap::new();
                for r in &recs {
                    *lv.entry(r.level).or_default() += 1;
                }
                let lvd = PyDict::new(py);
                for (l, n) in lv {
                    lvd.set_item(l, n)?;
                }
                let held = self.lock(py)?.sel.node_changed.len();
                c.log_msg("info", PyTuple::new(py, [
                    PyString::new(py, "fartrace select eye %.0f radius %.0f km focus %.0f,%.0f held %d levels %s").into_any(),
                    PyFloat::new(py, eye.unwrap_or(-1.0)).into_any(), PyFloat::new(py, radius / 1000.0).into_any(),
                    PyFloat::new(py, fx).into_any(), PyFloat::new(py, fy).into_any(), held.into_pyobject(py)?.into_any(),
                    lvd.into_any()])?)?;
            }
        }
        if ramp {
            let gk = PyList::empty(py);
            for (k, _) in &out.items {
                if kind_of(k) == Kind::Global {
                    gk.append(k.as_ref())?;
                }
            }
            s.getattr("coarse_keys")?.call_method1("update", (gk,))?;
        }
        conn.getattr("__dict__")?.set_item("_desired", &dict)?;
        {
            let mut st = self.lock(py)?;
            st.sel.driving = driving;
            st.sel.refined = refined;
            let mut m = OMap::new();
            for r in recs {
                m.insert(r.key.clone(), r);
            }
            st.desired = m;
            let gen = st.sel.gen;
            st.sel.last = pend.map(|(d, cont, keep)| (d, cont, slack, gen, keep));
        }
        conn.call_method0("_regroup")?;
        if changed {
            let t = c.perf()?;
            let mut st = self.lock(py)?;
            if !st.dirty {
                st.dirty = true;
                st.batch = Batch { start: t, sent: 0, swaps: 0 };
            }
        }
        conn.getattr("wake")?.call_method0("set")?;
        Ok(())
    }

    /// Connection.prebuild_coarse: a teleport's coarse cover (the global far tiles round anchor.prebuild_focus, as the
    /// commit's first selection will choose them) built ahead, nearest first. Returns the builds started.
    pub(super) fn prebuild_coarse_impl(&self, conn: &Bound<'_, PyAny>, s: &Bound<'_, PyAny>, anchor: &Bound<'_, PyAny>)
                                       -> PyResult<usize> {
        let c = self.cx(conn, s)?;
        let py = c.py;
        let Some(focus) = attr_or(anchor, "prebuild_focus")?.and_then(opt) else { return Ok(0) };
        if !c.g("FIRST_RAMP")?.is_truthy()? || !c.truthy(conn, "global_far")? || c.map_on()? {
            return Ok(0);
        }
        let (fx, fy) = (pyfloat(&focus.get_item(0)?)?, pyfloat(&focus.get_item(1)?)?);
        let mut radius = fget(s, "radius")?;
        let mut ext = Ext::default();
        if s.getattr("horizon")?.is_truthy()? {
            let grid = opt(s.getattr("horizon_grids")?.call_method1("get", (anchor.getattr("id")?,))?);
            let eye = c.eye()?.unwrap_or(0.0);
            radius = match &grid {
                None => fget(&c.ff, "HORIZON_MIN")?,
                Some(g) => pyfloat(&g.call_method1("radius", (fx, fy, eye))?.get_item(0)?)?,
            };
            ext = Ext { max_level: Some(iget(s, "max_level")? as i32), disc: Some(true), horizon: grid.map(|g| (eye, g)) };
        }
        if let Some(vc) = attr_or(conn, "view_cap")?.and_then(opt) {
            let vc = pyfloat(&vc)?;
            if vc < radius {
                radius = vc;
            }
            ext.disc = Some(true);
        }
        if !c.global_on(anchor, radius)? {
            return Ok(0);
        }
        let max_level = match ext.max_level { Some(m) => m, None => iget(&c.ff, "MAX_LEVEL")? as i32 };
        let a = SelIn {
            px: fx, py: fy, forced: Vec::new(), prev: Vec::new(), hold: Vec::new(),
            ks: c.ks(max_level, Some(c.gf("FIRST_RAMP_K")?))?, h: fget(s, "hysteresis")?, radius, max_level,
            min_level: iget(s, "min_level")? as i32, radii: radii_of(&s.getattr("near_radii")?)?,
            disc: ext.disc.unwrap_or(false), horizon: ext.horizon, forced_level: iget(s, "forced_level")? as i32,
            view: None, eye_h: 0.0, cone: None,
        };
        let frame = anchor.getattr("frame")?;
        let mut out = Out::default();
        sel_global(py, &c.ff, fget(&frame, "lat0")?, fget(&frame, "lon0")?, anchor.getattr("zf")?.extract()?, &a, false,
                   None, &mut Vec::new(), &mut out)?;
        let new = out.dict(py)?;
        s.getattr("coarse_keys")?.call_method1("update", (&new,))?;
        let order = c.ff.getattr("nearest_first")?.call1((new.values(), fx, fy))?;
        let (tiles, inflight) = (s.getattr("tiles")?, s.getattr("tile_inflight")?);
        let aid = anchor.getattr("id")?;
        let kw = PyDict::new(py);
        kw.set_item("shield", false)?;
        let mut n: usize = 0;
        for t in order.try_iter()? {
            let t = t?;
            let ck = s.call_method1("far_ck", (&aid, t.getattr("key")?))?;
            if !tiles.contains(&ck)? && !inflight.contains(&ck)? {
                s.call_method("tile", (anchor, &t), Some(&kw))?;
                n += 1;
            }
        }
        c.log_msg("info", PyTuple::new(py, [
            PyString::new(py, "anchor %d: %d coarse far tiles built ahead (%d chosen, radius %.0f km)").into_any(), aid,
            n.into_pyobject(py)?.into_any(), new.len().into_pyobject(py)?.into_any(),
            PyFloat::new(py, radius / 1000.0).into_any()])?)?;
        Ok(n)
    }

    pub(super) fn eye_clearance_impl(&self, conn: &Bound<'_, PyAny>, s: &Bound<'_, PyAny>) -> PyResult<f64> {
        let c = self.cx(conn, s)?;
        self.eye_clear(&c)
    }

    /// (cx, cy, dz, ppr, tau, reach, h, cap, cells, errs, aid, gerr) of Connection.view, or None (the tests)
    pub(super) fn view_impl<'py>(&self, conn: &Bound<'py, PyAny>, s: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        let c = self.cx(conn, s)?;
        let Some(v) = self.view_a(&c)? else { return Ok(None) };
        let mut items: Vec<Bound<'py, PyAny>> = v.v.iter().map(|x| PyFloat::new(conn.py(), *x).into_any()).collect();
        items.extend([v.errs, v.aid, v.gerr]);
        Ok(Some(PyTuple::new(conn.py(), items)?))
    }

    pub(super) fn lod_ppr_impl(&self, py: Python<'_>, h: f64, fov: f64, now: f64) -> PyResult<f64> {
        let g = self.cfg.globals.as_ref().ok_or_else(|| PyRuntimeError::new_err("FarState: no earthd globals (g)"))?;
        self.lod_ppr_at(py, g.bind(py), h, fov, now)
    }

    pub(super) fn ks_impl(&self, conn: &Bound<'_, PyAny>, s: &Bound<'_, PyAny>, n: i32) -> PyResult<Vec<f64>> {
        let c = self.cx(conn, s)?;
        c.ks(n, None)
    }

    /// The active anchor (server.anchors[conn.active]) or None.
    fn anchor_of<'py>(c: &Cx<'_, 'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let active = c.conn.getattr("active")?;
        if active.is_none() {
            return Ok(None);
        }
        Ok(opt(c.s.getattr("anchors")?.call_method1("get", (active,))?))
    }

    /// Connection.pvs_reveal: far tiles shown (or sent) with less texture than they should have now (a far PVS that no
    /// longer hides them): stale, re-rendered live nearest first (far_retex).
    pub(super) fn pvs_reveal_impl(&self, conn: &Bound<'_, PyAny>, s: &Bound<'_, PyAny>) -> PyResult<()> {
        let c = self.cx(conn, s)?;
        let py = c.py;
        let Some(_anchor) = Self::anchor_of(&c)? else { return Ok(()) };
        if !conn.getattr("first_ramp")?.is_none() || !s.getattr("pvs_on")?.is_truthy()? {
            return Ok(());     // (a teleport's coarse cover: sharpened as the full selection replaces it)
        }
        let aid = aid_of(&conn.getattr("active")?)?;
        let ga = self.ga();
        let keys: Vec<(Ck, Py<PyAny>)> = {
            let st = self.lock(py)?;
            st.desired.iter().filter(|(k, _)| st.shown.contains_key(&st.ck(ga, aid, k)) || st.sent.contains_key(*k))
                .map(|(k, t)| (st.ck(ga, aid, k), t.obj.clone_ref(py))).collect()
        };
        let tiles = s.getattr("tiles")?;
        let mut cands = Vec::new();
        let pairs = PyList::empty(py);
        for (ck, t) in keys {
            let ckt = ck_tuple(py, &ck)?;
            let Some(info) = opt(tiles.call_method1("get", (&ckt,))?) else { continue };
            if info.getattr("material")?.is_none() || info.getattr("stale")?.is_truthy()? {
                continue;
            }
            pairs.append((&info, t.bind(py)))?;
            cands.push((ckt, info));
        }
        let short = conn.call_method1("tex_short_many", (pairs,))?;
        let kw = PyDict::new(py);
        kw.set_item("pump", false)?;
        let mut n: usize = 0;
        for ((ckt, info), sh) in cands.into_iter().zip(short.try_iter()?) {
            if !sh?.is_truthy()? {
                continue;
            }
            info.setattr("stale", true)?;
            s.call_method("retex_request", (ckt,), Some(&kw))?;
            n += 1;
        }
        if n > 0 {
            let stats = s.getattr("pvs_stats")?;
            stats.set_item("revealed", stats.get_item("revealed")?.add(n)?)?;
            c.log_msg("info", PyTuple::new(py, [PyString::new(py, "far PVS: %d far tiles in sight now, sharpened").into_any(),
                                            n.into_pyobject(py)?.into_any()])?)?;
            s.call_method0("_retex_pump")?;
        }
        Ok(())
    }

    /// Connection.far_lazy_tick: the far tiles shown larger than their level's texture (farpvs.UP_ON) get their best
    /// photos (the lazy sources) fetched after the near ones, nearest first (Imagery.far_warm), each rendered again when
    /// they are in. Every FAR_LAZY_EVERY_S at most.
    pub(super) fn far_lazy_tick_impl(&self, conn: &Bound<'_, PyAny>, s: &Bound<'_, PyAny>) -> PyResult<()> {
        let c = self.cx(conn, s)?;
        let py = c.py;
        let im = s.getattr("imagery")?;
        let anchor = Self::anchor_of(&c)?;
        let Some(anchor) = anchor.filter(|_| !im.is_none()) else { return Ok(()) };
        if !c.g("imagerymod")?.getattr("FAR_LAZY")?.is_truthy()? || !c.g("farpvsmod")?.getattr("UP_ON")?.is_truthy()?
            || c.map_on()? {
            return Ok(());
        }
        let now = self.now(py)?;
        let at = match attr_or(conn, "_far_lazy_at")? { Some(v) => pyfloat(&v)?, None => 0.0 };
        if now - at < c.gf("FAR_LAZY_EVERY_S")? {
            return Ok(());
        }
        conn.setattr("_far_lazy_at", now)?;
        let queued = conn.getattr("__dict__")?.call_method1("setdefault", ("far_lazy", PyDict::new(py)))?;
        let (x, y) = c.focus()?;
        let loop_ = py.import("asyncio")?.call_method0("get_running_loop")?;
        let soon = loop_.getattr("call_soon_threadsafe")?;
        let lazy_done = s.getattr("far_lazy_done")?;
        let partial = py.import("functools")?.getattr("partial")?;
        let piece = c.ff.getattr("Piece")?;
        let gutter = c.g("imagerymod")?.getattr("gutter_square")?;
        let aid = aid_of(&conn.getattr("active")?)?;
        let ga = self.ga();
        let items: Vec<(Ck, Py<PyAny>)> = {
            let st = self.lock(py)?;
            st.desired.iter().map(|(k, t)| (st.ck(ga, aid, k), t.obj.clone_ref(py))).collect()
        };
        let tiles = s.getattr("tiles")?;
        for (ck, _t) in items {
            let ckt = ck_tuple(py, &ck)?;
            let Some(info) = opt(tiles.call_method1("get", (&ckt,))?) else { continue };
            let tex_px = info.getattr("tex_px")?;
            let tile = info.getattr("tile")?;
            if info.getattr("material")?.is_none() || !tex_px.is_truthy()? || tile.is_none()
                || info.getattr("coarse")?.is_truthy()? {
                continue;
            }
            let node = if tile.is_instance(&piece)? { tile.getattr("base")? } else { tile.clone() };
            if tex_px.le(s.call_method1("tex_size", (node.getattr("level")?,))?)?
                || queued.call_method1("get", (&ckt,))?.eq(&tex_px)? {
                continue;
            }
            queued.set_item(&ckt, &tex_px)?;
            let w = s.call_method1("tile_where", (&anchor, &tile))?;
            let b = floats(&node.getattr("bbox")?)?;
            let mx = |a: f64, b: f64| { let mut m = a; if b > m { m = b; } if 0.0 > m { m = 0.0; } m };
            let d = py_hypot(py, mx(b[0] - x, x - b[2]), mx(b[1] - y, y - b[3]))?;
            let done = partial.call1((&soon, &lazy_done, &ckt))?;
            let sq = gutter.call1((w.get_item(1)?, w.get_item(2)?, w.get_item(3)?, &tex_px))?;
            let mut args: Vec<Bound<'_, PyAny>> = vec![ckt.clone().into_any(), PyFloat::new(py, d).into_any(), w.get_item(0)?];
            for v in sq.try_iter()? {
                args.push(v?);
            }
            args.push(tex_px.clone());
            args.push(done);
            im.call_method1("far_warm", PyTuple::new(py, args)?)?;
        }
        Ok(())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(far_select, m)?)?;
    m.add_function(wrap_pyfunction!(far_select_global, m)?)?;
    m.add_function(wrap_pyfunction!(far_last_global_select, m)?)?;
    Ok(())
}
