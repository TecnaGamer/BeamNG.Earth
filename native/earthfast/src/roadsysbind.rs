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
#[pyclass(frozen)]
pub struct RoadsysSegments { place: Place, dropped: (usize, usize, usize) }

/// "<road>~lanes<a>-<b>" -> (road, a, b).
fn section_of(sid: &str) -> Option<(&str, i64, i64)> {
    let (road, span) = sid.split_once("~lanes")?;
    let (a, b) = span.split_once('-')?;
    Some((road, a.parse().ok()?, b.parse().ok()?))
}

#[pymethods]
impl RoadsysSegments {
    #[new]
    #[pyo3(signature = (ids, cells, texts, nums, ints, pts, off, con_seg, con_id, con_v, rng_seg, rng, style))]
    #[allow(clippy::too_many_arguments)]
    fn new(ids: Vec<String>, cells: Vec<String>, texts: Vec<(String, String, String, String, String)>,
           nums: PyReadonlyArray2<'_, f64>, ints: PyReadonlyArray2<'_, i64>, pts: PyReadonlyArray2<'_, f64>,
           off: PyReadonlyArray1<'_, i64>, con_seg: PyReadonlyArray1<'_, i64>, con_id: Vec<String>,
           con_v: PyReadonlyArray1<'_, i64>, rng_seg: PyReadonlyArray1<'_, i64>, rng: PyReadonlyArray2<'_, f64>,
           style: (String, String, f64, f64, f64, f64, (f64, f64), (f64, f64), (f64, f64), bool)) -> PyResult<Self> {
        let n = ids.len();
        let (nums, ints, pts, off) = (nums.as_array(), ints.as_array(), pts.as_array(), off.as_slice()?);
        if cells.len() != n || texts.len() != n || nums.nrows() != n || nums.ncols() != 4 || ints.nrows() != n
            || ints.ncols() != 9 || pts.ncols() != 2 || off.len() != n + 1 || off[n] as usize != pts.nrows() {
            return Err(PyValueError::new_err("RoadsysSegments: inconsistent columns"));
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
            match kind as i64 { 0 => segs[s].bridge.push((a, b)), 1 => segs[s].tunnel.push((a, b)), 3 => { if v > segs[s].depth { segs[s].depth = v; } }, _ => segs[s].level.push((a, b, v)) }
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
        let place = Place { name: String::new(), country: segs.first().map(|s| s.code.clone()).unwrap_or_default(), style: st, segs, ..Default::default() };
        Ok(RoadsysSegments { place, dropped: (copies, parents, sections) })
    }

    /// (segments kept, copies dropped, whole roads dropped for their sections, sections dropped for their road)
    fn counts(&self) -> (usize, usize, usize, usize) { (self.place.segs.len(), self.dropped.0, self.dropped.1, self.dropped.2) }
}

fn opts(cut: (f64, f64)) -> roadsys::net::Opts { roadsys::net::Opts { cut_grid: cut.0, cut_offset: cut.1 } }

/// The tile's read list from the segments read so far: (boxes in the frame to read, segment ids whose every copy
/// must be read, [delivered, full, program, profile] stroke counts, cpu s).
#[pyfunction]
/// field: the tile's profiles come from the profile squares (roadsys_build's field): no vertical component is read.
#[pyo3(signature = (segs, bbox, cut = (4000.0, 2000.0), field = false))]
pub fn roadsys_reads(py: Python<'_>, segs: PyRef<'_, RoadsysSegments>, bbox: [f64; 4], cut: (f64, f64), field: bool)
                     -> (Vec<[f64; 4]>, Vec<String>, [usize; 4], f64) {
    let place = &segs.place;
    let r = py.detach(|| roadsys::product::reads_f(place, bbox, opts(cut), field));
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
    let out = py.detach(|| roadsys::product::build_f(place, quad, id, frame, &g_of, fld, opts(cut)));
    if let Some(e) = err.lock().unwrap().take() { return Err(e); }
    if let Some(path) = dump {
        let t = roadsys::io::TileInput { place: place.clone(), quad, id, frame, cut, ground: asked.lock().unwrap().clone() };
        std::fs::write(&path, roadsys::io::write_tile(&t)).map_err(|e| PyValueError::new_err(format!("{path}: {e}")))?;
    }
    Ok((out.json, PyBytes::new(py, &out.bytes)))
}

/// BNE_ROADSYS=heights (M2): a profile square (native/roadsys square.rs): the one profile per carriageway of the
/// strokes with a Tier B section in one square of the region frame's grid, solved from its window's map alone.
#[pyclass(frozen)]
pub struct RoadsysSquare { sq: roadsys::square::Square }

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
#[pyo3(signature = (segs, sq, ground, cut = (4000.0, 2000.0), dump = None, obs = None))]
#[allow(clippy::too_many_arguments)]
pub fn roadsys_square(py: Python<'_>, segs: PyRef<'_, RoadsysSegments>, sq: (i64, i64), ground: Bound<'_, PyAny>,
                      cut: (f64, f64), dump: Option<String>, obs: Option<Bound<'_, PyAny>>) -> PyResult<RoadsysSquare> {
    let win = roadsys::square::window(sq);
    let place = Place { segs: segs.place.segs.iter().filter(|s| roadsys::product::seg_in(s, &[win])).cloned().collect(), ..segs.place.clone_meta() };
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
    let out = py.detach(|| roadsys::square::solve_obs(&place, sq, &g_of, Some(&o_of), opts(cut)));
    if let Some(e) = err.lock().unwrap().take() { return Err(e); }
    if let Some(path) = dump {
        asked.lock().unwrap().extend_from_slice(&seen.lock().unwrap());
        // (the square's whole input, for `roadsys square` on another platform: the determinism test)
        let t = roadsys::io::TileInput { place: place.clone(), quad: [[0.0; 2]; 4], id: (sq.0 as i32, sq.1 as i32), frame: 0, cut,
                                         ground: asked.lock().unwrap().clone() };
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

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
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
    m.add_function(wrap_pyfunction!(roadsys_clear, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_audit, m)?)?;
    Ok(())
}
