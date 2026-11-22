//! The road surface + bridge rewrite (native/roadsys, BNE_ROADSYS) as the server calls it: server/roadsys.py hands a
//! read's segment table (RoadsysSegments), asks for the tile's read list (roadsys_reads) until nothing is missing,
//! then builds the tile's shadow product (roadsys_build); tools/widecorpus/checks.py's audit (roadsys_audit) runs the
//! same M0 checks the CLI does. All maths in native/roadsys (no platform maths, no FMA, BTreeMap only).
use numpy::{PyArray1, PyReadonlyArray1, PyReadonlyArray2};
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
                pts: p, cons: Vec::new(), bridge: Vec::new(), tunnel: Vec::new(), level: Vec::new(),
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
            match kind as i64 { 0 => segs[s].bridge.push((a, b)), 1 => segs[s].tunnel.push((a, b)), _ => segs[s].level.push((a, b, v)) }
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
#[pyo3(signature = (segs, bbox, cut = (4000.0, 2000.0)))]
pub fn roadsys_reads(py: Python<'_>, segs: PyRef<'_, RoadsysSegments>, bbox: [f64; 4], cut: (f64, f64))
                     -> (Vec<[f64; 4]>, Vec<String>, [usize; 4], f64) {
    let place = &segs.place;
    let r = py.detach(|| roadsys::product::reads(place, bbox, opts(cut)));
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
#[pyo3(signature = (segs, quad, id, frame, ground, cut = (4000.0, 2000.0), dump = None, boxes = None))]
#[allow(clippy::too_many_arguments)]
pub fn roadsys_build<'py>(py: Python<'py>, segs: PyRef<'py, RoadsysSegments>, quad: [[f64; 2]; 4], id: (i32, i32), frame: u64,
                          ground: Bound<'py, PyAny>, cut: (f64, f64), dump: Option<String>, boxes: Option<Vec<[f64; 4]>>)
                          -> PyResult<(String, Bound<'py, PyBytes>)> {
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
    let out = py.detach(|| roadsys::product::build(place, quad, id, frame, &g_of, opts(cut)));
    if let Some(e) = err.lock().unwrap().take() { return Err(e); }
    if let Some(path) = dump {
        let t = roadsys::io::TileInput { place: place.clone(), quad, id, frame, cut, ground: asked.lock().unwrap().clone() };
        std::fs::write(&path, roadsys::io::write_tile(&t)).map_err(|e| PyValueError::new_err(format!("{path}: {e}")))?;
    }
    Ok((out.json, PyBytes::new(py, &out.bytes)))
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
    m.add_function(wrap_pyfunction!(roadsys_clear, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_audit, m)?)?;
    Ok(())
}
