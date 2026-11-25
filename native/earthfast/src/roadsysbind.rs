//! The road surface + bridge rewrite (native/roadsys, BNE_ROADSYS) as the server calls it: server/roadsys.py hands a
//! read's segment table (RoadsysSegments), asks for the tile's read list (roadsys_reads) until nothing is missing,
//! then builds the tile's shadow product (roadsys_build); tools/widecorpus/checks.py's audit (roadsys_audit) runs the
//! same M0 checks the CLI does. All maths in native/roadsys (no platform maths, no FMA, BTreeMap only).
use numpy::{PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use roadsys::io::{Place, Seg, Style};
use std::collections::BTreeMap;

/// The segments of a read, deduplicated (one copy per segment: the one with the most points, ties by the smaller
/// cell name, as roadshape.SegmentWindow chooses) and with each road split into lane sections taken either whole
/// (its sections where they cover all of it) or as its sections, never both (lanesections.replaced_roads' rule).
/// areas (RoadsysAreas): OSM's mapped road areas round the read (the products' audit; fit: BNE_ROADSYS_AREAS=on, the
/// carriageway edges' soft targets, native/roadsys areas.rs).
#[pyclass(frozen)]
pub struct RoadsysSegments { place: Place, dropped: (usize, usize, usize), fit: bool }

/// OSM's mapped road areas in a frame (server/osmareas.py: the tiles of a read's cells decoded by oah_decode, their
/// points in the frame): keys (2 x the OSM id, + 1 for a relation), values, layers, flags, the rings (each ring's
/// polygon index, role 0 outer / 1 inner, its first point in pts; pt_off has one more), pts (n, 2) and the dataset's
/// build identity (in the keys where the fit is on).
#[pyclass(frozen)]
pub struct RoadsysAreas { areas: Vec<roadsys::areas::Area>, build: u64 }

#[pymethods]
impl RoadsysAreas {
    #[new]
    #[pyo3(signature = (keys, values, layers, flags, ring_poly, ring_role, pt_off, pts, build))]
    #[allow(clippy::too_many_arguments)]
    fn new(keys: PyReadonlyArray1<'_, u64>, values: Vec<String>, layers: PyReadonlyArray1<'_, i64>, flags: PyReadonlyArray1<'_, i64>,
           ring_poly: PyReadonlyArray1<'_, i64>, ring_role: PyReadonlyArray1<'_, i64>, pt_off: PyReadonlyArray1<'_, i64>,
           pts: PyReadonlyArray2<'_, f64>, build: u64) -> PyResult<Self> {
        let (keys, layers, flags) = (keys.as_slice()?, layers.as_slice()?, flags.as_slice()?);
        let (rp, rr, po) = (ring_poly.as_slice()?, ring_role.as_slice()?, pt_off.as_slice()?);
        let pts = pts.as_array();
        let n = keys.len();
        if values.len() != n || layers.len() != n || flags.len() != n || rr.len() != rp.len() || po.len() != rp.len() + 1
            || (pts.nrows() > 0 && pts.ncols() != 2) || po.last().map(|&v| v as usize != pts.nrows()).unwrap_or(true) {
            return Err(PyValueError::new_err("RoadsysAreas: inconsistent columns"));
        }
        let mut areas: Vec<roadsys::areas::Area> = (0..n).map(|i| roadsys::areas::Area {
            key: keys[i], value: values[i].clone(), layer: layers[i], flags: flags[i].clamp(0, 255) as u8, links: Vec::new(), rings: Vec::new() }).collect();
        for k in 0..rp.len() {
            let (i, a, b) = (rp[k], po[k], po[k + 1]);
            if i < 0 || i as usize >= n || a < 0 || b < a || b as usize > pts.nrows() {
                return Err(PyValueError::new_err("RoadsysAreas: a ring out of range"));
            }
            let r: Vec<[f64; 2]> = (a as usize..b as usize).map(|j| [pts[[j, 0]], pts[[j, 1]]]).collect();
            if r.iter().any(|q| !q[0].is_finite() || !q[1].is_finite()) { return Err(PyValueError::new_err("RoadsysAreas: non-finite point")); }
            areas[i as usize].rings.push((if rr[k] != 0 { 1 } else { 0 }, r));
        }
        Ok(RoadsysAreas { areas, build })
    }

    fn count(&self) -> usize { self.areas.len() }
}

/// "<road>~lanes<a>-<b>" -> (road, a, b).
fn section_of(sid: &str) -> Option<(&str, i64, i64)> {
    let (road, span) = sid.split_once("~lanes")?;
    let (a, b) = span.split_once('-')?;
    Some((road, a.parse().ok()?, b.parse().ok()?))
}

#[pymethods]
impl RoadsysSegments {
    #[new]
    /// xsec (610, BNE_ROADSYS_XSEC): each segment's cross-section tags (native/roadsys xsec.rs's text); given, the
    /// cross-section model is on (every stroke's strip slots) whatever the texts hold; None: off, as before.
    /// areas / fit (608, BNE_ROADSYS_AREAS): the mapped road areas round the read and whether they are the carriageway
    /// edges' targets. Both on (617): the fit sets the carriageway's whole width and edges, the strips divide it.
    /// rails (626, M5): railway lines at ground level round the read [(points (n, 2) in the frame, clearance m)]: the floors
    /// of the structures crossing them.
    #[pyo3(signature = (ids, cells, texts, nums, ints, pts, off, con_seg, con_id, con_v, rng_seg, rng, style, areas = None, fit = false, xsec = None, rails = None))]
    #[allow(clippy::too_many_arguments)]
    fn new(ids: Vec<String>, cells: Vec<String>, texts: Vec<(String, String, String, String, String)>,
           nums: PyReadonlyArray2<'_, f64>, ints: PyReadonlyArray2<'_, i64>, pts: PyReadonlyArray2<'_, f64>,
           off: PyReadonlyArray1<'_, i64>, con_seg: PyReadonlyArray1<'_, i64>, con_id: Vec<String>,
           con_v: PyReadonlyArray1<'_, i64>, rng_seg: PyReadonlyArray1<'_, i64>, rng: PyReadonlyArray2<'_, f64>,
           style: (String, String, f64, f64, f64, f64, (f64, f64), (f64, f64), (f64, f64), bool),
           areas: Option<PyRef<'_, RoadsysAreas>>, fit: bool, xsec: Option<Vec<String>>, rails: Option<Vec<(Vec<(f64, f64)>, f64)>>) -> PyResult<Self> {
        let n = ids.len();
        let (nums, ints, pts, off) = (nums.as_array(), ints.as_array(), pts.as_array(), off.as_slice()?);
        if cells.len() != n || texts.len() != n || nums.nrows() != n || nums.ncols() != 4 || ints.nrows() != n
            || ints.ncols() != 9 || pts.ncols() != 2 || off.len() != n + 1 || off[n] as usize != pts.nrows() {
            return Err(PyValueError::new_err("RoadsysSegments: inconsistent columns"));
        }
        if xsec.as_ref().map(|x| x.len() != n).unwrap_or(false) {
            return Err(PyValueError::new_err("RoadsysSegments: xsec must hold one text per segment"));
        }
        let (con_seg, con_v, rng_seg) = (con_seg.as_slice()?, con_v.as_slice()?, rng_seg.as_slice()?);
        let rng = rng.as_array();
        if con_seg.len() != con_id.len() || con_v.len() != con_id.len() || rng_seg.len() != rng.nrows() || (rng.nrows() > 0 && rng.ncols() != 4) {
            return Err(PyValueError::new_err("RoadsysSegments: inconsistent connector or range columns"));
        }
        let mut segs: Vec<Seg> = Vec::with_capacity(n);
        for i in 0..n {
            let (a, b) = (off[i] as usize, off[i + 1] as usize);
            if b < a + 2 || b > pts.nrows() { segs.push(Seg::default()); continue; }
            let p: Vec<[f64; 2]> = (a..b).map(|k| [pts[[k, 0]], pts[[k, 1]]]).collect();
            if p.iter().any(|q| !q[0].is_finite() || !q[1].is_finite()) { return Err(PyValueError::new_err("RoadsysSegments: non-finite point")); }
            let t = &texts[i];
            segs.push(Seg {
                fid: ids[i].clone(), code: t.0.clone(), cls_s: t.1.clone(), turn: t.2.clone(), turn_bw: t.3.clone(), place: t.4.clone(),
                pave: nums[[i, 0]], drive: nums[[i, 1]], kmh: nums[[i, 2]], lane_w: nums[[i, 3]],
                inferred: ints[[i, 0]], urban: ints[[i, 1]] != 0, cls: ints[[i, 2]], link: ints[[i, 3]] != 0, dir: ints[[i, 4]],
                lanes: ints[[i, 5]], lfw: ints[[i, 6]], lbw: ints[[i, 7]], region: ints[[i, 8]],
                pts: p, cons: Vec::new(), bridge: Vec::new(), tunnel: Vec::new(), level: Vec::new(), depth: 0.0,
                xsec: xsec.as_ref().map(|x| x[i].clone()).unwrap_or_default(), maxh: Vec::new(),
            });
        }
        for k in 0..con_id.len() {
            let s = con_seg[k] as usize;
            if s < n { segs[s].cons.push((con_id[k].clone(), con_v[k].max(0) as usize)); }
        }
        for k in 0..rng.nrows() {
            let s = rng_seg[k] as usize;
            if s >= n { continue; }
            let (a, b, kind, v) = (rng[[k, 0]], rng[[k, 1]], rng[[k, 2]], rng[[k, 3]]);
            // (kinds: 0 bridge, 1 tunnel, 2 level, 3 drawn depth, 4 (M5) a height limit, maxheight sign, of v m)
            match kind as i64 { 0 => segs[s].bridge.push((a, b)), 1 => segs[s].tunnel.push((a, b)), 3 => { if v > segs[s].depth { segs[s].depth = v; } }, 4 => segs[s].maxh.push((a, b, v)), _ => segs[s].level.push((a, b, v)) }
        }
        // one copy per segment: a row's id is "<segment>|<its first point>" and the point differs between two cells'
        // copies, so copies are grouped by the segment's own id (roadshape.SegmentCell's sid), the cell whose copy has
        // the most points over its parts wins (ties: the smaller cell name), and all of that copy's parts are kept
        let base = |fid: &str| fid.split('|').next().unwrap_or(fid).to_string();
        let mut points: BTreeMap<(String, String), usize> = BTreeMap::new();
        for i in 0..n {
            if segs[i].pts.len() < 2 { continue; }
            *points.entry((base(&segs[i].fid), cells[i].clone())).or_insert(0) += segs[i].pts.len();
        }
        let mut owner: BTreeMap<String, (usize, String)> = BTreeMap::new();
        for ((sid, cell), &np) in &points {
            match owner.get(sid) {
                Some((bp, _)) if *bp >= np => {}
                _ => { owner.insert(sid.clone(), (np, cell.clone())); }
            }
        }
        let mut best: BTreeMap<String, usize> = BTreeMap::new();
        for i in 0..n {
            if segs[i].pts.len() < 2 { continue; }
            if owner.get(&base(&segs[i].fid)).map(|o| o.1 == cells[i]).unwrap_or(false) {
                best.entry(segs[i].fid.clone()).or_insert(i);
            }
        }
        let copies = n - best.len();
        // lane sections: whole road or sections, never both
        let mut spans: BTreeMap<String, Vec<(i64, i64)>> = BTreeMap::new();
        for fid in best.keys() {
            if let Some((road, a, b)) = section_of(&base(fid)) { spans.entry(road.to_string()).or_default().push((a, b)); }
        }
        let mut covered: BTreeMap<String, bool> = BTreeMap::new();
        for (road, mut v) in spans {
            v.sort();
            let mut end = 0;
            for (a, b) in v { if a > end { break; } end = end.max(b); }
            covered.insert(road, end >= 1_000_000_000);
        }
        let (mut parents, mut sections) = (0, 0);
        let mut keep: Vec<usize> = Vec::new();
        for (fid, &i) in &best {
            let b = base(fid);
            if let Some((road, _, _)) = section_of(&b) {
                let has_parent = best.keys().any(|f| base(f) == road);
                if has_parent && !covered[road] { sections += 1; continue; }
            } else if covered.get(&b).copied().unwrap_or(false) { parents += 1; continue; }
            keep.push(i);
        }
        let segs: Vec<Seg> = keep.into_iter().map(|i| std::mem::take(&mut segs[i])).collect();
        let st = Style { centre: style.0, left_edge: style.1, w: style.2, w_motorway: style.3, edge: style.4, edge_motorway: style.5,
                         centre_urban: style.6, lane_urban: style.7, lane_motorway: style.8, double_centre: style.9 };
        let (areas, area_build) = match &areas { Some(a) => (a.areas.clone(), a.build), None => (Vec::new(), 0) };
        let fit = fit && !areas.is_empty();
        let rails: Vec<(Vec<[f64; 2]>, f64)> = rails.unwrap_or_default().into_iter()
            .filter(|(p, c)| p.len() >= 2 && c.is_finite() && p.iter().all(|q| q.0.is_finite() && q.1.is_finite()))
            .map(|(p, c)| (p.into_iter().map(|q| [q.0, q.1]).collect(), c)).collect();
        let place = Place { name: String::new(), country: segs.first().map(|s| s.code.clone()).unwrap_or_default(), style: st, segs,
                            xsec: xsec.is_some(), areas, area_build, rails, ..Default::default() };
        Ok(RoadsysSegments { place, dropped: (copies, parents, sections), fit })
    }

    /// (segments kept, copies dropped, whole roads dropped for their sections, sections dropped for their road)
    fn counts(&self) -> (usize, usize, usize, usize) { (self.place.segs.len(), self.dropped.0, self.dropped.1, self.dropped.2) }
}

fn opts(cut: (f64, f64)) -> roadsys::net::Opts { roadsys::net::Opts { cut_grid: cut.0, cut_offset: cut.1, ..Default::default() } }

/// opts() with the areas' fit of segs (RoadsysSegments' fit: BNE_ROADSYS_AREAS=on and areas given).
fn opts_of(segs: &RoadsysSegments, cut: (f64, f64)) -> roadsys::net::Opts { roadsys::net::Opts { areas: segs.fit, ..opts(cut) } }

/// The tile's read list from the segments read so far: (boxes in the frame to read, segment ids whose every copy
/// must be read, [delivered, full, program, profile] stroke counts, cpu s).
#[pyfunction]
/// field: the tile's profiles come from the profile squares (roadsys_build's field): no vertical component is read.
#[pyo3(signature = (segs, bbox, cut = (4000.0, 2000.0), field = false))]
pub fn roadsys_reads(py: Python<'_>, segs: PyRef<'_, RoadsysSegments>, bbox: [f64; 4], cut: (f64, f64), field: bool)
                     -> (Vec<[f64; 4]>, Vec<String>, [usize; 4], f64) {
    let place = &segs.place;
    let o = opts_of(&segs, cut);
    let r = py.detach(|| roadsys::product::reads_f(place, bbox, o, field));
    (r.boxes, r.fids, r.counts, r.cpu)
}

/// The tile's shadow product from a complete read: (JSON: timings, read counts, network / profile / surface
/// stats, checks, seams, the pieces' keys; the tile's canonical bytes). ground(e, n) -> heights (float64 arrays in
/// the frame) is called once, with every point the profiles need.
/// dump: a file to write the build's whole input to (io::write_tile: the segments and the ground heights it asked
/// for), for `roadsys tile` on another platform (the determinism test).
#[pyfunction]
/// boxes: the converged read list's boxes (roadsys_reads): only the segments meeting one are built (the rest of the
/// cells read is in no piece's read list; the same bytes either way).
/// field: (z_of(points (n, 2), tangents (n, 2), map features) -> heights (NaN: no square holds it), its key): the
/// profiles from the profile squares where they hold the road (M2).
#[pyo3(signature = (segs, quad, id, frame, ground, cut = (4000.0, 2000.0), dump = None, boxes = None, field = None))]
#[allow(clippy::too_many_arguments)]
pub fn roadsys_build<'py>(py: Python<'py>, segs: PyRef<'py, RoadsysSegments>, quad: [[f64; 2]; 4], id: (i32, i32), frame: u64,
                          ground: Bound<'py, PyAny>, cut: (f64, f64), dump: Option<String>, boxes: Option<Vec<[f64; 4]>>,
                          field: Option<(Bound<'py, PyAny>, u64)>) -> PyResult<(String, Bound<'py, PyBytes>)> {
    let kept;
    let place = match &boxes {
        None => &segs.place,
        Some(bx) => {
            kept = Place { segs: segs.place.segs.iter().filter(|s| roadsys::product::seg_in(s, bx)).cloned().collect(), ..segs.place.clone_meta() };
            &kept
        }
    };
    let ground = ground.unbind();
    let err: std::sync::Mutex<Option<PyErr>> = std::sync::Mutex::new(None);
    let asked: std::sync::Mutex<Vec<f64>> = std::sync::Mutex::new(Vec::new());
    let g_of = |pts: &[[f64; 2]]| -> Vec<f64> {
        Python::attach(|py| -> Vec<f64> {
            let e = PyArray1::from_vec(py, pts.iter().map(|p| p[0]).collect());
            let n = PyArray1::from_vec(py, pts.iter().map(|p| p[1]).collect());
            let got = ground.bind(py).call1((e, n)).and_then(|r| r.extract::<Vec<f64>>());
            match got {
                Ok(v) if v.len() == pts.len() => { asked.lock().unwrap().extend_from_slice(&v); v }
                Ok(_) => { *err.lock().unwrap() = Some(PyValueError::new_err("roadsys_build: ground returned another number of heights")); vec![0.0; pts.len()] }
                Err(e) => { *err.lock().unwrap() = Some(e); vec![0.0; pts.len()] }
            }
        })
    };
    let fkey = field.as_ref().map(|f| f.1).unwrap_or(0);
    let ffn = field.map(|f| f.0.unbind());
    let z_of = |pts: &[[f64; 2]], tans: &[[f64; 2]], feats: &[String]| -> Vec<f64> {
        let Some(f) = &ffn else { return vec![f64::NAN; pts.len()] };
        Python::attach(|py| -> Vec<f64> {
            let n = pts.len();
            let p = PyArray1::from_vec(py, pts.iter().flat_map(|q| [q[0], q[1]]).collect()).reshape([n, 2]);
            let t = PyArray1::from_vec(py, tans.iter().flat_map(|q| [q[0], q[1]]).collect()).reshape([n, 2]);
            let got = match (p, t) {
                (Ok(p), Ok(t)) => f.bind(py).call1((p, t, feats.to_vec())).and_then(|r| r.extract::<Vec<f64>>()),
                (Err(e), _) | (_, Err(e)) => Err(e),
            };
            match got {
                Ok(v) if v.len() == n => v,
                Ok(_) => { *err.lock().unwrap() = Some(PyValueError::new_err("roadsys_build: field returned another number of heights")); vec![f64::NAN; n] }
                Err(e) => { *err.lock().unwrap() = Some(e); vec![f64::NAN; n] }
            }
        })
    };
    let fld: Option<roadsys::product::Field> = if ffn.is_some() { Some((&z_of, fkey)) } else { None };
    let o = opts_of(&segs, cut);
    let out = py.detach(|| roadsys::product::build_f(place, quad, id, frame, &g_of, fld, o));
    if let Some(e) = err.lock().unwrap().take() { return Err(e); }
    if let Some(path) = dump {
        let t = roadsys::io::TileInput { place: place.clone(), quad, id, frame, cut, ground: asked.lock().unwrap().clone(), areas_on: o.areas };
        std::fs::write(&path, roadsys::io::write_tile(&t)).map_err(|e| PyValueError::new_err(format!("{path}: {e}")))?;
    }
    Ok((out.json, PyBytes::new(py, &out.bytes)))
}

/// BNE_ROADSYS=heights (M2): a profile square (native/roadsys square.rs): the one profile per carriageway of the
/// strokes with a Tier B section in one square of the region frame's grid, solved from its window's map alone.
#[pyclass(frozen)]
pub struct RoadsysSquare { pub(crate) sq: roadsys::square::Square }

#[pymethods]
impl RoadsysSquare {
    /// The square back from its bytes (to_bytes; a checksum and the format are checked).
    #[staticmethod]
    fn from_bytes(data: &[u8]) -> PyResult<Self> {
        roadsys::square::Square::from_bytes(data).map(|sq| RoadsysSquare { sq }).map_err(PyValueError::new_err)
    }
    fn to_bytes<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> { PyBytes::new(py, &self.sq.to_bytes()) }
    /// (JSON stats, key, (i, j), JSON cpu ms of its solve: empty for one read from bytes)
    fn info(&self) -> (String, u64, (i64, i64), String) { (self.sq.json.clone(), self.sq.key, self.sq.sq, self.sq.cpu_json.clone()) }
    /// Its terrain targets (square.rs pins): [(map feature, points (n, 2), tangents (n, 2), fade, share, half width,
    /// hand (n,), low (n,))], heights left to roadsys_blend. hand: 1 where the profile owns the ground (round a structure
    /// end, a floor over it) falling to 0 away; low: the same round a floor under a structure (there the ground is only
    /// dug) (M3).
    #[allow(clippy::type_complexity)]
    fn pins<'py>(&self, py: Python<'py>) -> PyResult<Vec<(String, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, f64, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)>> {
        let mut out = Vec::new();
        for p in roadsys::square::pins(&self.sq) {
            let n = p.pts.len();
            let a = PyArray1::from_vec(py, p.pts.iter().flat_map(|q| [q[0], q[1]]).collect()).reshape([n, 2])?;
            let t = PyArray1::from_vec(py, p.tan.iter().flat_map(|q| [q[0], q[1]]).collect()).reshape([n, 2])?;
            out.push((p.feat, a, t, PyArray1::from_vec(py, p.w), PyArray1::from_vec(py, p.share), p.half, PyArray1::from_vec(py, p.hand), PyArray1::from_vec(py, p.low)));
        }
        Ok(out)
    }
}

/// The square's window (its read box in the region frame) and the squares whose weight reaches a box.
#[pyfunction]
pub fn roadsys_square_window(sq: (i64, i64)) -> [f64; 4] { roadsys::square::window(sq) }

#[pyfunction]
pub fn roadsys_squares_of_box(bbox: [f64; 4]) -> Vec<(i64, i64)> { roadsys::square::squares_of_box(bbox) }

/// A square solved from its window's segments: ground(e, n) -> heights (float64, the frame) is called once with every
/// knot point. boxes: only the segments meeting one are built (default: the window).
#[pyfunction]
/// obs(runs) -> heights: the measured deck heights per structure run [(points (n, 2), half width, level)], an array
/// per run (NaN: none), or None.
/// tunnels (629, T1: BNE_ROADSYS_TUNNELS): the square makes the tunnel record (native/roadsys tunnel.rs) too.
#[pyo3(signature = (segs, sq, ground, cut = (4000.0, 2000.0), dump = None, obs = None, tunnels = false))]
#[allow(clippy::too_many_arguments)]
pub fn roadsys_square(py: Python<'_>, segs: PyRef<'_, RoadsysSegments>, sq: (i64, i64), ground: Bound<'_, PyAny>,
                      cut: (f64, f64), dump: Option<String>, obs: Option<Bound<'_, PyAny>>, tunnels: bool) -> PyResult<RoadsysSquare> {
    let win = roadsys::square::window(sq);
    // (the squares' heights are the lane rules' own: no mapped areas in them, BNE_ROADSYS_AREAS or not)
    let place = Place { segs: segs.place.segs.iter().filter(|s| roadsys::product::seg_in(s, &[win])).cloned().collect(), areas: Vec::new(),
                        area_build: 0, ..segs.place.clone_meta() };
    let ground = ground.unbind();
    let err: std::sync::Mutex<Option<PyErr>> = std::sync::Mutex::new(None);
    let asked: std::sync::Mutex<Vec<f64>> = std::sync::Mutex::new(Vec::new());
    let g_of = |pts: &[[f64; 2]]| -> Vec<f64> {
        Python::attach(|py| -> Vec<f64> {
            let e = PyArray1::from_vec(py, pts.iter().map(|p| p[0]).collect());
            let n = PyArray1::from_vec(py, pts.iter().map(|p| p[1]).collect());
            match ground.bind(py).call1((e, n)).and_then(|r| r.extract::<Vec<f64>>()) {
                Ok(v) if v.len() == pts.len() && v.iter().all(|x| x.is_finite()) => { asked.lock().unwrap().extend_from_slice(&v); v }
                Ok(_) => { *err.lock().unwrap() = Some(PyValueError::new_err("roadsys_square: ground returned another number of heights, or a non-finite one")); vec![0.0; pts.len()] }
                Err(e) => { *err.lock().unwrap() = Some(e); vec![0.0; pts.len()] }
            }
        })
    };
    let obs = obs.map(|o| o.unbind());
    let seen: std::sync::Mutex<Vec<f64>> = std::sync::Mutex::new(Vec::new());
    let o_of = |runs: &[roadsys::prof::ObsRun]| -> Vec<Vec<f64>> {
        let Some(ob) = &obs else { return runs.iter().map(|r| vec![f64::NAN; r.pts.len()]).collect() };
        Python::attach(|py| -> Vec<Vec<f64>> {
            let mut args = Vec::with_capacity(runs.len());
            for r in runs {
                let n = r.pts.len();
                let a = PyArray1::from_vec(py, r.pts.iter().flat_map(|q| [q[0], q[1]]).collect());
                match a.reshape([n, 2]) { Ok(a) => args.push((a, r.half, r.level)), Err(e) => { *err.lock().unwrap() = Some(e); return Vec::new(); } }
            }
            match ob.bind(py).call1((args,)).and_then(|r| r.extract::<Vec<Vec<f64>>>()) {
                Ok(v) if v.len() == runs.len() && v.iter().zip(runs).all(|(a, r)| a.len() == r.pts.len()) => {
                    let mut sn = seen.lock().unwrap();
                    for a in &v { sn.extend_from_slice(a); }
                    v
                }
                Ok(_) => { *err.lock().unwrap() = Some(PyValueError::new_err("roadsys_square: obs returned other shapes")); Vec::new() }
                Err(e) => { *err.lock().unwrap() = Some(e); Vec::new() }
            }
        })
    };
    let out = py.detach(|| roadsys::square::solve_obs(&place, sq, &g_of, Some(&o_of), roadsys::net::Opts { tunnels, ..opts(cut) }));
    if let Some(e) = err.lock().unwrap().take() { return Err(e); }
    if let Some(path) = dump {
        asked.lock().unwrap().extend_from_slice(&seen.lock().unwrap());
        // (the square's whole input, for `roadsys square` on another platform: the determinism test)
        let t = roadsys::io::TileInput { place: place.clone(), quad: [[0.0; 2]; 4], id: (sq.0 as i32, sq.1 as i32), frame: 0, cut,
                                         ground: asked.lock().unwrap().clone(), areas_on: false };
        std::fs::write(&path, roadsys::io::write_tile(&t)).map_err(|e| PyValueError::new_err(format!("{path}: {e}")))?;
    }
    Ok(RoadsysSquare { sq: out })
}

/// The blended heights at points (n, 2) with tangents (n, 2) (zero rows: none) on map features feats (one per point)
/// over the squares given (square.rs blend; deck: on a structure of that feature): NaN where no square answers.
#[pyfunction]
pub fn roadsys_blend<'py>(py: Python<'py>, squares: Vec<PyRef<'py, RoadsysSquare>>, pts: PyReadonlyArray2<'py, f64>,
                          tans: PyReadonlyArray2<'py, f64>, feats: Vec<String>, deck: bool) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (p, t) = (pts.as_array(), tans.as_array());
    if p.ncols() != 2 || t.ncols() != 2 || t.nrows() != p.nrows() || feats.len() != p.nrows() {
        return Err(PyValueError::new_err("roadsys_blend: inconsistent columns"));
    }
    let mut m: BTreeMap<(i64, i64), &roadsys::square::Square> = BTreeMap::new();
    for s in &squares { m.insert(s.sq.sq, &s.sq); }
    let out: Vec<f64> = (0..p.nrows()).map(|i| {
        let q = [p[[i, 0]], p[[i, 1]]];
        let tv = [t[[i, 0]], t[[i, 1]]];
        let tt = if tv[0] == 0.0 && tv[1] == 0.0 { None } else { Some(tv) };
        roadsys::square::blend(&m, q, tt, &feats[i], deck).unwrap_or(f64::NAN)
    }).collect();
    Ok(PyArray1::from_vec(py, out))
}

/// (M3) The drawn depth (m) of the structure of map feature feats[i] under each point (square.rs sample_depth: the
/// first square in the weights' order that holds it): NaN where none does; the full depth (2.2 m) where its segments
/// gave none.
#[pyfunction]
pub fn roadsys_deck_depth<'py>(py: Python<'py>, squares: Vec<PyRef<'py, RoadsysSquare>>, pts: PyReadonlyArray2<'py, f64>,
                               tans: PyReadonlyArray2<'py, f64>, feats: Vec<String>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (p, t) = (pts.as_array(), tans.as_array());
    if p.ncols() != 2 || t.ncols() != 2 || t.nrows() != p.nrows() || feats.len() != p.nrows() {
        return Err(PyValueError::new_err("roadsys_deck_depth: inconsistent columns"));
    }
    let mut m: BTreeMap<(i64, i64), &roadsys::square::Square> = BTreeMap::new();
    for s in &squares { m.insert(s.sq.sq, &s.sq); }
    let out: Vec<f64> = (0..p.nrows()).map(|i| {
        let q = [p[[i, 0]], p[[i, 1]]];
        let tv = [t[[i, 0]], t[[i, 1]]];
        let tt = if tv[0] == 0.0 && tv[1] == 0.0 { None } else { Some(tv) };
        roadsys::square::weights(q).iter().find_map(|(k, _w)| m.get(k).and_then(|s| s.sample_depth(q, tt, &feats[i]))).unwrap_or(f64::NAN)
    }).collect();
    Ok(PyArray1::from_vec(py, out))
}

/// (M3) A bridge chain's lifts onto its abutments' ground (native/roadsys endmatch.rs): parts [(points (n, 2), heights
/// (n,))], ends [(ground just past its first end, past its last end)] (NaN: not read) -> the lift per part vertex.
#[pyfunction]
#[pyo3(signature = (parts, ends, lift_max, drop_max = None, holds = None))]
pub fn roadsys_end_lifts<'py>(py: Python<'py>, parts: Vec<(PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, f64>)>,
                              ends: Vec<(f64, f64)>, lift_max: f64, drop_max: Option<f64>, holds: Option<Vec<Vec<f64>>>) -> PyResult<Vec<Bound<'py, PyArray1<f64>>>> {
    if ends.len() != parts.len() { return Err(PyValueError::new_err("roadsys_end_lifts: one ends pair per part")); }
    let mut pp = Vec::with_capacity(parts.len());
    for (p, z) in &parts {
        let (p, z) = (p.as_array(), z.as_slice()?);
        if p.ncols() != 2 || p.nrows() != z.len() { return Err(PyValueError::new_err("roadsys_end_lifts: inconsistent part")); }
        pp.push(((0..p.nrows()).map(|i| [p[[i, 0]], p[[i, 1]]]).collect::<Vec<_>>(), z.to_vec()));
    }
    let got = roadsys::endmatch::lifts_held(&pp, &ends, lift_max, drop_max.unwrap_or(0.0), &holds.unwrap_or_else(|| vec![Vec::new(); pp.len()]));
    Ok(got.into_iter().map(|v| PyArray1::from_vec(py, v)).collect())
}

/// (M3) The floors the squares hold over other roads inside a box: [(x, y, map feature)] (square.rs floor_points),
/// each square's own, in square then stroke order (duplicates between squares kept: the caller matches by distance).
#[pyfunction]
pub fn roadsys_floor_points(squares: Vec<PyRef<'_, RoadsysSquare>>, bbox: [f64; 4]) -> Vec<(f64, f64, String)> {
    let mut out = Vec::new();
    for s in &squares { for (p, f) in s.sq.floor_points(bbox) { out.push((p[0], p[1], f)); } }
    out
}

/// The process's profile solve store emptied (tests: a cold process's results).
#[pyfunction]
pub fn roadsys_clear() { roadsys::prof::store_clear(); }

/// The M0 audit of one scene (native/roadsys audit.rs; its input as tools/widecorpus/checks.py writes it).
#[pyfunction]
pub fn roadsys_audit(py: Python<'_>, data: &[u8]) -> PyResult<String> {
    let d = data.to_vec();
    py.detach(move || roadsys::audit::run(&d)).map_err(PyValueError::new_err)
}


// ------------------------------------------------------------------------- M3 structures: deck ends' hand-over

fn lines2(name: &str, v: &[(PyReadonlyArray2<'_, f64>, f64)]) -> PyResult<Vec<roadsys::handover::Part>> {
    let mut out = Vec::with_capacity(v.len());
    for (p, hw) in v {
        let a = p.as_array();
        if a.ncols() != 2 { return Err(PyValueError::new_err(format!("{name}: points must be (n, 2)"))); }
        out.push(roadsys::handover::Part { pts: (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect(), hw: *hw });
    }
    Ok(out)
}

/// (M3, BNE_ROADSYS=structures) The ground ends of the deck parts round a place in one frame (native/roadsys
/// handover.rs): bridges [(line (n, 2), half width)], grounds the same (every ground part of a road), tunnels [line];
/// s the terrain lattice's square. drops(x, y): the m every evaluator of the physics ground lowers it by there.
/// (627) crossed [(line (n, 2), kind: 1 railway, 3 waterway)] and nbi [None or (skew deg, length m, cos, sin)] per
/// bridge part: the ends skewed by roadsys skew.rs's rule (None: square, as before); skews() lists them.
#[pyclass(frozen)]
#[allow(clippy::type_complexity)]
pub struct RoadsysEnds { ends: roadsys::handover::Ends,
                         src: (Vec<roadsys::handover::Part>, Vec<roadsys::handover::Part>, Vec<Vec<[f64; 2]>>, Option<roadsys::handover::SkewIn>) }

#[pymethods]
impl RoadsysEnds {
    #[new]
    #[pyo3(signature = (bridges, grounds, tunnels, s, crossed=None, nbi=None, noskew=None))]
    #[allow(clippy::type_complexity)]
    fn new(bridges: Vec<(PyReadonlyArray2<'_, f64>, f64)>, grounds: Vec<(PyReadonlyArray2<'_, f64>, f64)>,
           tunnels: Vec<PyReadonlyArray2<'_, f64>>, s: f64, crossed: Option<Vec<(PyReadonlyArray2<'_, f64>, i64)>>,
           nbi: Option<Vec<Option<(f64, f64, f64, f64)>>>, noskew: Option<Vec<bool>>) -> PyResult<Self> {
        let b = lines2("RoadsysEnds bridges", &bridges)?;
        let g = lines2("RoadsysEnds grounds", &grounds)?;
        let mut t = Vec::with_capacity(tunnels.len());
        for p in &tunnels {
            let a = p.as_array();
            if a.ncols() != 2 { return Err(PyValueError::new_err("RoadsysEnds tunnels: points must be (n, 2)")); }
            t.push((0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect::<Vec<_>>());
        }
        if !(s > 0.0) { return Err(PyValueError::new_err("RoadsysEnds: the lattice square must be > 0")); }
        let sk = match crossed {
            None => None,
            Some(cr) => {
                let mut k = roadsys::handover::SkewIn::default();
                for (p, kind) in &cr {
                    let a = p.as_array();
                    if a.ncols() != 2 || !(0..=255).contains(kind) { return Err(PyValueError::new_err("RoadsysEnds crossed: (n, 2) lines, kinds 0-255")); }
                    k.crossed.push(((0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect(), *kind as u8));
                }
                let nb = nbi.unwrap_or_default();
                if !nb.is_empty() && nb.len() != b.len() { return Err(PyValueError::new_err("RoadsysEnds nbi: one per bridge part")); }
                k.nbi = nb.into_iter().map(|r| r.map(|(a, l, c, sn)| [a, l, c, sn])).collect();
                if k.nbi.iter().flatten().any(|r| r.iter().any(|v| !v.is_finite())) { return Err(PyValueError::new_err("RoadsysEnds nbi: finite values")); }
                k.noskew = noskew.unwrap_or_default();
                if !k.noskew.is_empty() && k.noskew.len() != g.len() { return Err(PyValueError::new_err("RoadsysEnds noskew: one per ground part")); }
                Some(k)
            }
        };
        let ends = roadsys::handover::Ends::build(&b, g.clone(), &t, s, sk.as_ref());
        Ok(RoadsysEnds { ends, src: (b, g, t, sk) })
    }
    fn count(&self) -> usize { self.ends.ends.len() }
    /// (627) [(x, y, dx, dy)] of the skewed ends: the end's point and its end line (unit, to the left of the way in).
    fn skews(&self) -> Vec<(f64, f64, f64, f64)> {
        self.ends.ends.iter().filter_map(|e| e.dir.map(|d| (e.p[0], e.p[1], d[0], d[1]))).collect()
    }
    /// [(x, y)] of each end's point (tests, probes).
    fn points(&self) -> Vec<(f64, f64)> { self.ends.ends.iter().map(|e| (e.p[0], e.p[1])).collect() }
    fn boxes(&self, grow: f64) -> Vec<[f64; 4]> { self.ends.boxes(grow) }
    /// (630) The under-deck ceiling of decks round these ends (RoadsysCeil).
    fn ceil(&self, decks: Vec<(PyReadonlyArray2<'_, f64>, f64, f64)>, rails: Vec<PyReadonlyArray2<'_, f64>>) -> PyResult<RoadsysCeil> {
        let mut ds = Vec::with_capacity(decks.len());
        for (p, hw, depth) in &decks {
            let a = p.as_array();
            if a.ncols() != 3 { return Err(PyValueError::new_err("RoadsysEnds.ceil decks: points must be (n, 3)")); }
            ds.push(roadsys::ceil::Deck { pts: (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect(),
                                          h: (0..a.nrows()).map(|i| a[[i, 2]]).collect(), hw: *hw, depth: *depth });
        }
        let mut rl = Vec::with_capacity(rails.len());
        for p in &rails {
            let a = p.as_array();
            if a.ncols() != 2 { return Err(PyValueError::new_err("RoadsysEnds.ceil rails: points must be (n, 2)")); }
            rl.push((0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect::<Vec<_>>());
        }
        let (b, g, t, sk) = &self.src;
        let src = roadsys::ceil::encode_input(b, g, t, self.ends.s, sk.as_ref(), &ds, &rl);
        Ok(RoadsysCeil { ceil: roadsys::ceil::Ceil::new(self.ends.clone(), &ds, &rl), src })
    }
    fn drops<'py>(&self, py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
        let (x, y) = (x.as_slice()?.to_vec(), y.as_slice()?.to_vec());
        if x.len() != y.len() { return Err(PyValueError::new_err("RoadsysEnds.drops: x and y differ in length")); }
        let e = &self.ends;
        let out = py.detach(move || x.iter().zip(y.iter()).map(|(&a, &b)| if a.is_finite() && b.is_finite() { e.drop_at(a, b) } else { 0.0 }).collect::<Vec<f64>>());
        Ok(PyArray1::from_vec(py, out))
    }
}

/// (630, M5, BNE_ROADSYS=surface) The ground under the decks round a place at most at their soffits less 0.3 m
/// (native/roadsys ceil.rs), with these ends' drop: decks [(line (n, 3): x, y and the top's height per vertex, half
/// width, the structure's drawn depth)], rails [line (n, 2)] (their beds keep their ground). ground(x, y, z): the
/// physics ground from the ground z (the drop, then the ceiling); input(): the whole input's bytes (`roadsys ceil`).
#[pyclass(frozen)]
pub struct RoadsysCeil { ceil: roadsys::ceil::Ceil, src: Vec<u8> }

#[pymethods]
impl RoadsysCeil {
    fn count(&self) -> usize { self.ceil.count() }
    fn ground<'py>(&self, py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>)
                   -> PyResult<Bound<'py, PyArray1<f64>>> {
        let (x, y, z) = (x.as_slice()?.to_vec(), y.as_slice()?.to_vec(), z.as_slice()?.to_vec());
        if x.len() != y.len() || x.len() != z.len() { return Err(PyValueError::new_err("RoadsysCeil.ground: x, y and z differ in length")); }
        let c = &self.ceil;
        let out = py.detach(move || x.iter().zip(y.iter()).zip(z.iter())
            .map(|((&a, &b), &h)| if a.is_finite() && b.is_finite() && h.is_finite() { c.ground(a, b, h) } else { h }).collect::<Vec<f64>>());
        Ok(PyArray1::from_vec(py, out))
    }
    fn input<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> { PyBytes::new(py, &self.src) }
}

fn zone_args(p: &PyReadonlyArray2<'_, f64>, uv: &PyReadonlyArray2<'_, f64>, t: &PyReadonlyArray2<'_, i64>, m: &PyReadonlyArray1<'_, i64>,
             i0: i64, j0: i64, s: f64, z: &PyReadonlyArray2<'_, f64>, drop: &PyReadonlyArray2<'_, f64>, tile_uv: f64)
             -> PyResult<(roadsys::handover::Soup, roadsys::handover::Grid)> {
    let (pa, ua, ta, ma, za, da) = (p.as_array(), uv.as_array(), t.as_array(), m.as_slice()?, z.as_array(), drop.as_array());
    if pa.ncols() != 3 || ua.ncols() != 2 || ua.nrows() != pa.nrows() || ta.ncols() != 3 || ta.nrows() != ma.len()
        || za.dim() != da.dim() || !(s > 0.0) || !(tile_uv > 0.0) {
        return Err(PyValueError::new_err("roadsys_handover_zone: inconsistent arrays"));
    }
    let n = pa.nrows() as i64;
    let mut soup = roadsys::handover::Soup::default();
    for i in 0..pa.nrows() { soup.p.push([pa[[i, 0]], pa[[i, 1]], pa[[i, 2]]]); soup.uv.push([ua[[i, 0]], ua[[i, 1]]]); }
    for k in 0..ta.nrows() {
        let tr = [ta[[k, 0]], ta[[k, 1]], ta[[k, 2]]];
        if tr.iter().any(|&v| v < 0 || v >= n) || ma[k] < 0 { return Err(PyValueError::new_err("roadsys_handover_zone: a triangle index out of range")); }
        soup.t.push([tr[0] as u32, tr[1] as u32, tr[2] as u32]);
        soup.m.push(ma[k] as u32);
    }
    let (ny, nx) = za.dim();
    let mut g = roadsys::handover::Grid { i0, j0, nx, ny, s, z: Vec::with_capacity(nx * ny), drop: Vec::with_capacity(nx * ny) };
    for b in 0..ny { for a in 0..nx { g.z.push(za[[b, a]]); g.drop.push(da[[b, a]]); } }
    Ok((soup, g))
}

/// (M3) roadsys_handover_zone's whole input as one file's bytes (native/roadsys handover.rs encode_input: the CLI's
/// `roadsys handover IN` runs it again, on Linux and on Windows, for the byte comparison).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn roadsys_handover_input<'py>(py: Python<'py>, ends: PyRef<'py, RoadsysEnds>, p: PyReadonlyArray2<'py, f64>, uv: PyReadonlyArray2<'py, f64>,
                                   t: PyReadonlyArray2<'py, i64>, m: PyReadonlyArray1<'py, i64>, top: u32, i0: i64, j0: i64, s: f64,
                                   z: PyReadonlyArray2<'py, f64>, drop: PyReadonlyArray2<'py, f64>, tile_uv: f64) -> PyResult<Bound<'py, PyBytes>> {
    let (soup, g) = zone_args(&p, &uv, &t, &m, i0, j0, s, &z, &drop, tile_uv)?;
    let (b, gr, tu, sk) = &ends.src;
    Ok(PyBytes::new(py, &roadsys::handover::encode_input(b, gr, tu, &soup, top, &g, tile_uv, sk.as_ref())))
}

/// (M3) A deck's shape rebuilt round its ground ends on one anchor's terrain lattice (handover.rs zone): positions
/// (n, 3) and uvs (n, 2) in the anchor's world, triangles (m, 3), their materials (m,), the top's material; the lattice
/// from node (i0, j0) at square s, its quantised heights z and drops (ny, nx); tile_uv m per texture repeat of the
/// zone's polygons. Returns (positions, uvs, triangles, materials, stats JSON).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn roadsys_handover_zone<'py>(py: Python<'py>, ends: PyRef<'py, RoadsysEnds>, p: PyReadonlyArray2<'py, f64>, uv: PyReadonlyArray2<'py, f64>,
                                  t: PyReadonlyArray2<'py, i64>, m: PyReadonlyArray1<'py, i64>, top: u32, i0: i64, j0: i64, s: f64,
                                  z: PyReadonlyArray2<'py, f64>, drop: PyReadonlyArray2<'py, f64>, tile_uv: f64)
                                  -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray1<i64>>, String)> {
    let (soup, g) = zone_args(&p, &uv, &t, &m, i0, j0, s, &z, &drop, tile_uv)?;
    let e = &ends.ends;
    let (out, st) = py.detach(move || roadsys::handover::zone(&soup, top, e, &g, tile_uv));
    let np_ = out.p.len();
    let mut pp = numpy::ndarray::Array2::<f64>::zeros((np_, 3));
    let mut uu = numpy::ndarray::Array2::<f64>::zeros((np_, 2));
    for (i, v) in out.p.iter().enumerate() { for c in 0..3 { pp[[i, c]] = v[c]; } uu[[i, 0]] = out.uv[i][0]; uu[[i, 1]] = out.uv[i][1]; }
    let mut tt = numpy::ndarray::Array2::<i64>::zeros((out.t.len(), 3));
    for (k, tr) in out.t.iter().enumerate() { for c in 0..3 { tt[[k, c]] = tr[c] as i64; } }
    let mm: Vec<i64> = out.m.iter().map(|&v| v as i64).collect();
    let js = format!("{{\"ends\":{},\"ends_kept\":{},\"squares_u\":{},\"squares_h\":{},\"squares_z\":{},\"cleared\":{},\"replaced\":{},\"zone_tris\":{},\"split\":{},\"max_mismatch_mm\":{:.1}}}",
                     st.ends, st.ends_kept, st.squares_u, st.squares_h, st.squares_z, st.cleared, st.replaced, st.zone_tris, st.split, st.max_mismatch_mm);
    Ok((PyArray2::from_owned_array(py, pp), PyArray2::from_owned_array(py, uu), PyArray2::from_owned_array(py, tt), PyArray1::from_vec(py, mm), js))
}

// ------------------------------------------------------------------------------- M4 surface: decks from the faces

/// (M4, BNE_ROADSYS=surface) A tile's decks from the road system's own structure faces (native/roadsys deck.rs), in
/// the tile's own frame: segs from a complete read (roadsys_reads with field = true; boxes its converged boxes), quad
/// the tile's corners, ground(e, n) -> heights, lines [(map feature, points (n, 3))] today's deck heights. Returns
/// (positions (n, 3), triangles (m, 3), kinds (m,), feature index (m,; -1 none), depths (m,), features,
/// runs [(points (k, 3), outward normals (k, 2), closed, feature index)], marking positions (j, 3), marking triangles,
/// marking colours (0 white, 1 yellow), fallback features, stats JSON).
#[pyfunction]
/// dump: a file to write the build's whole input to (deck::encode_input), for `roadsys decks` on another platform.
/// skews (627): [(x, y, dx, dy)] the skewed structure ends (RoadsysEnds.skews in the tile's frame): their end rows.
#[pyo3(signature = (segs, quad, ground, lines, cut = (4000.0, 2000.0), boxes = None, dump = None, skews = None))]
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
pub fn roadsys_decks<'py>(py: Python<'py>, segs: PyRef<'py, RoadsysSegments>, quad: [[f64; 2]; 4], ground: Bound<'py, PyAny>,
                          lines: Vec<(String, PyReadonlyArray2<'py, f64>)>, cut: (f64, f64), boxes: Option<Vec<[f64; 4]>>,
                          dump: Option<String>, skews: Option<Vec<(f64, f64, f64, f64)>>)
                          -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                       Bound<'py, PyArray1<f64>>, Vec<String>, Vec<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, bool, i64)>,
                                       Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray1<i64>>, Vec<String>, String)> {
    let kept;
    let place = match &boxes {
        None => &segs.place,
        Some(bx) => {
            kept = Place { segs: segs.place.segs.iter().filter(|s| roadsys::product::seg_in(s, bx)).cloned().collect(), ..segs.place.clone_meta() };
            &kept
        }
    };
    let mut dl = Vec::with_capacity(lines.len());
    for (f, p) in &lines {
        let a = p.as_array();
        if a.ncols() != 3 { return Err(PyValueError::new_err("roadsys_decks: a deck line's points must be (n, 3)")); }
        dl.push(roadsys::deck::DeckLine { feat: f.clone(), p: (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]], a[[i, 2]]]).collect() });
    }
    let ground = ground.unbind();
    let err: std::sync::Mutex<Option<PyErr>> = std::sync::Mutex::new(None);
    let asked: std::sync::Mutex<Vec<f64>> = std::sync::Mutex::new(Vec::new());
    let g_of = |pts: &[[f64; 2]]| -> Vec<f64> {
        Python::attach(|py| -> Vec<f64> {
            let e = PyArray1::from_vec(py, pts.iter().map(|p| p[0]).collect());
            let n = PyArray1::from_vec(py, pts.iter().map(|p| p[1]).collect());
            match ground.bind(py).call1((e, n)).and_then(|r| r.extract::<Vec<f64>>()) {
                Ok(v) if v.len() == pts.len() => { asked.lock().unwrap().extend_from_slice(&v); v }
                Ok(_) => { *err.lock().unwrap() = Some(PyValueError::new_err("roadsys_decks: ground returned another number of heights")); vec![0.0; pts.len()] }
                Err(e) => { *err.lock().unwrap() = Some(e); vec![0.0; pts.len()] }
            }
        })
    };
    let op = opts_of(&segs, cut);
    let sk: Vec<[f64; 4]> = skews.unwrap_or_default().into_iter().map(|(a, b, c, d)| [a, b, c, d]).collect();
    if sk.iter().any(|q| q.iter().any(|v| !v.is_finite())) { return Err(PyValueError::new_err("roadsys_decks: skews must be finite")); }
    let o = py.detach(|| roadsys::deck::build_skewed(place, quad, &g_of, &dl, op, &sk));
    if let Some(e) = err.lock().unwrap().take() { return Err(e); }
    if let Some(path) = dump {
        let b = roadsys::deck::encode_input_skewed(place, quad, cut, &asked.lock().unwrap(), &dl, op.areas, &sk);
        std::fs::write(&path, b).map_err(|e| PyValueError::new_err(format!("{path}: {e}")))?;
    }
    let a3 = |v: &[[f64; 3]]| { let mut a = numpy::ndarray::Array2::<f64>::zeros((v.len(), 3)); for (i, q) in v.iter().enumerate() { for c in 0..3 { a[[i, c]] = q[c]; } } a };
    let t3 = |v: &[[u32; 3]]| { let mut a = numpy::ndarray::Array2::<i64>::zeros((v.len(), 3)); for (i, q) in v.iter().enumerate() { for c in 0..3 { a[[i, c]] = q[c] as i64; } } a };
    let mut runs = Vec::with_capacity(o.runs.len());
    for r in &o.runs {
        let mut n = numpy::ndarray::Array2::<f64>::zeros((r.out.len(), 2));
        for (i, q) in r.out.iter().enumerate() { n[[i, 0]] = q[0]; n[[i, 1]] = q[1]; }
        runs.push((PyArray2::from_owned_array(py, a3(&r.p)), PyArray2::from_owned_array(py, n), r.closed, r.feat as i64));
    }
    Ok((PyArray2::from_owned_array(py, a3(&o.v)), PyArray2::from_owned_array(py, t3(&o.t)),
        PyArray1::from_vec(py, o.kind.iter().map(|&k| k as i64).collect()), PyArray1::from_vec(py, o.feat.iter().map(|&k| k as i64).collect()),
        PyArray1::from_vec(py, o.depth.clone()), o.feats.clone(), runs,
        PyArray2::from_owned_array(py, a3(&o.mv)), PyArray2::from_owned_array(py, t3(&o.mt)),
        PyArray1::from_vec(py, o.mc.iter().map(|&k| k as i64).collect()), o.fallback.clone(), o.json.clone()))
}

/// (610, BNE_ROADSYS_XSEC) the half extent (m from its mapped line) of one segment's carriageway with its
/// cross-section, from its RoadsysSegments columns (texts, nums, ints as server/roadsys.py _table writes them) and its
/// cross-section text; None when the text does not shape it (today's widths). The deck ends' hand-over reads it.
#[pyfunction]
pub fn roadsys_xsec_half_extent(texts: (String, String, String, String, String), nums: [f64; 4], ints: [i64; 9], xsec: String) -> Option<f64> {
    let s = Seg {
        code: texts.0, cls_s: texts.1, turn: texts.2, turn_bw: texts.3, place: texts.4,
        pave: nums[0], drive: nums[1], kmh: nums[2], lane_w: nums[3],
        inferred: ints[0], urban: ints[1] != 0, cls: ints[2], link: ints[3] != 0, dir: ints[4], lanes: ints[5], lfw: ints[6],
        lbw: ints[7], region: ints[8], xsec, ..Default::default()
    };
    roadsys::net::xsec_half_extent(&s)
}

/// (608) The mapped road areas' audit of a surface (native/roadsys areas.rs run_input: the "RSAREA01" input
/// tools/widecorpus/checks.py writes): its JSON.
#[pyfunction]
pub fn roadsys_areas_audit(py: Python<'_>, data: &[u8]) -> PyResult<String> {
    let b = data.to_vec();
    py.detach(move || roadsys::areas::run_input(&b)).map_err(PyValueError::new_err)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(roadsys_decks, m)?)?;
    m.add_class::<RoadsysAreas>()?;
    m.add_function(wrap_pyfunction!(roadsys_areas_audit, m)?)?;
    m.add_class::<RoadsysSegments>()?;
    m.add_function(wrap_pyfunction!(roadsys_reads, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_build, m)?)?;
    m.add_class::<RoadsysSquare>()?;
    m.add_function(wrap_pyfunction!(roadsys_square_window, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_squares_of_box, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_square, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_blend, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_deck_depth, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_end_lifts, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_floor_points, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_xsec_half_extent, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_clear, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_audit, m)?)?;
    m.add_class::<RoadsysEnds>()?;
    m.add_class::<RoadsysCeil>()?;
    m.add_function(wrap_pyfunction!(roadsys_handover_zone, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_handover_input, m)?)?;
    Ok(())
}
