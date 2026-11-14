//! A cell's roads measured against the building walls in one call each way (roadwidth.RoadWidths.measure_many): the
//! map cells whose walls each road reads (road_near_cells), then every road's right of way and wall clearance
//! (road_walls), without the interpreter lock and on several threads.
//!
//! Before, each road went through Python on its own: its cells from a dozen whole-array numpy passes over a few
//! points, a lock, a memo, one kernel call per measure; and a road near a cell's edge had every wall of its 2-4 cells
//! converted into one frame and indexed again (walls_index + reframe_segs: 0.9 of the 2.3 CPU-s the walls took in
//! planning 4 Fells Point cells, 13 % of the planning, 2026-11-13). Here a road near an edge reads its first cell's
//! walls through that cell's own index and the other cells' walls through theirs: the buckets under the ray in the
//! other cell's frame (a metre of margin: the two tangent planes differ by millimetres over a cell), each candidate
//! wall converted into the road's frame by reframe_segs' very operations. The first wall along a ray is the minimum
//! over the walls it hits, whatever superset of them is tried: the same numbers as the walls of all its cells in one
//! index, to the bit.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

use crate::dm::Dm;

const MAX_THREADS: usize = 8;
const PAR_MIN: usize = 48;              // roads: fewer are measured on the calling thread

/// f over 0..n on up to MAX_THREADS threads (the order kept), on the calling thread for a few.
pub(crate) fn par_map<T: Send, F: Fn(usize) -> T + Sync>(n: usize, min_n: usize, f: F) -> Vec<T> {
    let threads = if n < min_n { 1 } else {
        std::thread::available_parallelism().map(|t| t.get()).unwrap_or(1).clamp(1, MAX_THREADS).min(n.max(1))
    };
    if threads <= 1 {
        return (0..n).map(&f).collect();
    }
    // (small strides taken in turn: a cell's long roads lie anywhere in its list)
    let next = std::sync::atomic::AtomicUsize::new(0);
    let stride = (n / (threads * 8)).max(1);
    let (f, next) = (&f, &next);
    let mut parts: Vec<(usize, Vec<T>)> = std::thread::scope(|sc| {
        let hs: Vec<_> = (0..threads).map(|_| sc.spawn(move || {
            let mut got = Vec::new();
            loop {
                let a = next.fetch_add(stride, std::sync::atomic::Ordering::Relaxed);
                if a >= n {
                    break;
                }
                got.push((a, (a..(a + stride).min(n)).map(f).collect::<Vec<T>>()));
            }
            got
        })).collect();
        hs.into_iter().flat_map(|h| h.join().unwrap()).collect()
    });
    parts.sort_by_key(|p| p.0);
    parts.into_iter().flat_map(|p| p.1).collect()
}

/// mapcells.tile_xy for one point: fractional Web-Mercator tile coordinates at n tiles per axis.
#[inline]
fn tile_xy(lat: f64, lon: f64, n: f64) -> (f64, f64) {
    const MAX_LAT: f64 = 85.0511287798066;
    // (np.clip: a NaN stays NaN)
    let lat = if lat < -MAX_LAT { -MAX_LAT } else if lat > MAX_LAT { MAX_LAT } else { lat };
    let x = (lon / 360.0 + 0.5) * n;
    let y = (0.5 - lat.to_radians().dtan().dasinh() / (2.0 * std::f64::consts::PI)) * n;
    (x, y)
}

/// roadwidth.RoadWidths._near_walls' cells of one road (lon, lat of its points): the cells within pad of its points
/// every near_step m along it, sorted as their 'z/x/y' strings sort. None for a road the Python code decides
/// (a coordinate that is not finite, fewer than one point).
fn near_cells(lon: &[f64], lat: &[f64], near_step: f64, dlat: f64, z: u32) -> Option<Vec<(i64, i64)>> {
    let k = lon.len();
    if k == 0 || lon.iter().chain(lat.iter()).any(|v| !v.is_finite()) {
        return None;
    }
    // s: the arc length, as hypot(dlat * 111320, dlon * 111320 * cos(lat))
    let mut s = Vec::with_capacity(k);
    s.push(0.0f64);
    for i in 1..k {
        let a = (lat[i] - lat[i - 1]) * 111320.0;
        let b = (lon[i] - lon[i - 1]) * 111320.0 * lat[i - 1].to_radians().dcos();
        s.push(s[i - 1] + a.dhypot(b));
    }
    let total = s[k - 1];
    if !total.is_finite() {
        return None;
    }
    // t = r_[arange(0, total, near_step), total], or one zero
    let mut t: Vec<f64> = Vec::new();
    if total > 0.0 {
        let cnt = (total / near_step).ceil().max(0.0) as usize;
        for i in 0..cnt {
            t.push(i as f64 * near_step);
        }
        t.push(total);
    } else {
        t.push(0.0);
    }
    let la: Vec<f64> = t.iter().map(|&x| crate::np_interp(x, &s, lat)).collect();
    let lo: Vec<f64> = t.iter().map(|&x| crate::np_interp(x, &s, lon)).collect();
    let mean = crate::devctx::pairwise(&la) / la.len() as f64;
    let c = mean.to_radians().dcos();
    let dlon = dlat / if c > 0.05 { c } else { 0.05 };
    let n = 1i64 << z;
    let nf = (2.0f64).powi(z as i32);
    let mut keys: Vec<(i64, i64)> = Vec::new();
    for (&la, &lo) in la.iter().zip(lo.iter()) {
        let (x0, y1) = tile_xy(la - dlat, lo - dlon, nf);
        let (x1, y0) = tile_xy(la + dlat, lo + dlon, nf);
        if !(x0.is_finite() && x1.is_finite() && y0.is_finite() && y1.is_finite()) {
            return None;
        }
        let (ix0, ix1) = (x0.floor() as i64, x1.floor() as i64);
        let iy0 = (y0.floor() as i64).clamp(0, n - 1);
        let iy1 = (y1.floor() as i64).clamp(0, n - 1);
        if ix1 - ix0 > 64 || iy1 - iy0 > 64 {
            return None;
        }
        for ix in ix0..=ix1 {
            for iy in iy0..=iy1 {
                let key = (ix.rem_euclid(n), iy);
                if !keys.contains(&key) {
                    keys.push(key);
                }
            }
        }
    }
    // sorted(keys, key=str): 'z/x/y' as text
    let mut named: Vec<(String, (i64, i64))> = keys.into_iter().map(|k| (format!("{}/{}/{}", z, k.0, k.1), k)).collect();
    named.sort();
    Some(named.into_iter().map(|p| p.1).collect())
}

/// The cells of every road: (cells (K, 2) x y, off (R + 1,), ok (R,)) of the roads lonlat[off[r]..off[r + 1]]
/// ((N, 2) lon lat): road r's cells cells[coff[r]..coff[r + 1]] in roadwidth's order (the first one's frame is the
/// road's); ok False (no cells) for a road left to the Python code.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn road_near_cells<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                            near_step: f64, dlat: f64, z: u32)
                            -> PyResult<(Bound<'py, PyArray2<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<bool>>)> {
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("road_near_cells: {m}"));
    let ll = lonlat.as_array();
    if ll.ncols() != 2 {
        return Err(bad("lonlat (n, 2)"));
    }
    let off = off.as_slice().map_err(|_| bad("off contiguous"))?;
    let np = ll.nrows();
    if off.is_empty() || off.windows(2).any(|w| w[1] < w[0]) || off[0] < 0 || *off.last().unwrap() as usize > np
        || !(near_step > 0.0) || z > 30 {
        return Err(bad("offsets"));
    }
    let lon: Vec<f64> = ll.column(0).iter().copied().collect();
    let lat: Vec<f64> = ll.column(1).iter().copied().collect();
    let nr = off.len() - 1;
    let got = py.detach(|| {
        par_map(nr, PAR_MIN * 4, |r| {
            let (a, b) = (off[r] as usize, off[r + 1] as usize);
            near_cells(&lon[a..b], &lat[a..b], near_step, dlat, z)
        })
    });
    let mut cells = Vec::new();
    let mut coff = Vec::with_capacity(nr + 1);
    let mut ok = Vec::with_capacity(nr);
    coff.push(0i64);
    for g in got {
        ok.push(g.is_some());
        for (x, y) in g.unwrap_or_default() {
            cells.push(x);
            cells.push(y);
        }
        coff.push((cells.len() / 2) as i64);
    }
    let n = cells.len() / 2;
    let arr = numpy::ndarray::Array2::from_shape_vec((n, 2), cells).map_err(|e| bad(&e.to_string()))?;
    Ok((arr.into_pyarray(py), coff.into_pyarray(py), ok.into_pyarray(py)))
}

/// A cell's walls: its frame (origin, rot), its bucket index and its segments (float32 x0 y0 x1 y1, end to end).
struct Cell<'a> {
    o: [f64; 3],
    r: [[f64; 3]; 3],
    keys: &'a [i64],
    first: &'a [i64],
    counts: &'a [i64],
    seg_of: &'a [i64],
    segs: &'a [f32],
}

/// The walls of a road's other cells in its own frame: (the cell's place among the road's, the wall) -> x0 y0 x1 y1.
type Converted = std::cell::RefCell<std::collections::HashMap<(u32, u32), [f64; 4]>>;

#[inline]
fn bucket_key(bx: i64, by: i64) -> i64 {
    (bx + (1i64 << 30)) * (1i64 << 31) + (by + (1i64 << 30))
}

/// wall_ray's test of one wall a-b against the ray p + t d: its t when hit within reach.
#[inline]
#[allow(clippy::too_many_arguments)]
fn hit(ax: f64, ay: f64, bx: f64, by: f64, px: f64, py: f64, dx: f64, dy: f64, reach: f64) -> Option<f64> {
    let (ex, ey) = (bx - ax, by - ay);
    let den = dx * ey - dy * ex;
    if den.abs() <= 1e-12 {
        return None;
    }
    let (wx, wy) = (ax - px, ay - py);
    let t = (wx * ey - wy * ex) / den;
    let u = (wx * dy - wy * dx) / den;
    if t > 0.0 && t <= reach && u >= 0.0 && u <= 1.0 { Some(t) } else { None }
}

/// The first wall of the road's cells along the ray from (px, py) by unit (dx, dy) in the first cell's frame, within
/// reach, or NaN (crate::wall_ray over the walls of all the cells in that frame).
#[allow(clippy::too_many_arguments)]
fn ray_cells(cells: &[&Cell], memo: &Converted, bucket: f64, px: f64, py: f64, dx: f64, dy: f64, reach: f64) -> f64 {
    let (qx, qy) = (px + dx * reach, py + dy * reach);
    let mut best = f64::INFINITY;
    let home = cells[0];
    {
        let bx0 = (px.min(qx) / bucket).floor() as i64;
        let bx1 = (px.max(qx) / bucket).floor() as i64;
        let by0 = (py.min(qy) / bucket).floor() as i64;
        let by1 = (py.max(qy) / bucket).floor() as i64;
        for by in by0..=by1 {
            for bx in bx0..=bx1 {
                let Ok(i) = home.keys.binary_search(&bucket_key(bx, by)) else { continue };
                for j in home.first[i]..home.first[i] + home.counts[i] {
                    let s = &home.segs[home.seg_of[j as usize] as usize * 4..][..4];
                    if let Some(t) = hit(s[0] as f64, s[1] as f64, s[2] as f64, s[3] as f64, px, py, dx, dy, reach) {
                        if t < best {
                            best = t;
                        }
                    }
                }
            }
        }
    }
    for (ci, c) in cells.iter().enumerate().skip(1) {
        if c.keys.is_empty() {
            continue;
        }
        // the ray's ends in the other cell's frame, its box a metre wider: the walls that can meet it
        let to = |x: f64, y: f64| {
            let (la, lo, _) = crate::e2g_one(x, y, 0.0, &home.o, &home.r);
            let (e, n, _) = crate::g2e_one(la, lo, 0.0, &c.o, &c.r);
            (e, n)
        };
        let (a, b) = (to(px, py), to(qx, qy));
        const PAD: f64 = 1.0;
        let bx0 = ((a.0.min(b.0) - PAD) / bucket).floor() as i64;
        let bx1 = ((a.0.max(b.0) + PAD) / bucket).floor() as i64;
        let by0 = ((a.1.min(b.1) - PAD) / bucket).floor() as i64;
        let by1 = ((a.1.max(b.1) + PAD) / bucket).floor() as i64;
        if bx1 - bx0 > 64 || by1 - by0 > 64 || bx1 < bx0 || by1 < by0 {
            continue;
        }
        // (reframe_segs' conversion of a wall's end: through lon / lat at the ground into the road's frame)
        let conv = |x: f64, y: f64| {
            let (la, lo, _) = crate::e2g_one(x, y, 0.0, &c.o, &c.r);
            let (e, n, _) = crate::g2e_one(la, lo, 0.0, &home.o, &home.r);
            (e, n)
        };
        for by in by0..=by1 {
            for bx in bx0..=bx1 {
                let Ok(i) = c.keys.binary_search(&bucket_key(bx, by)) else { continue };
                for j in c.first[i]..c.first[i] + c.counts[i] {
                    let sid = c.seg_of[j as usize] as usize;
                    // (a wall converted once a road: its rays cross the same walls again and again)
                    let w = *memo.borrow_mut().entry((ci as u32, sid as u32)).or_insert_with(|| {
                        let s = &c.segs[sid * 4..][..4];
                        let (p0, p1) = (conv(s[0] as f64, s[1] as f64), conv(s[2] as f64, s[3] as f64));
                        [p0.0, p0.1, p1.0, p1.1]
                    });
                    if let Some(t) = hit(w[0], w[1], w[2], w[3], px, py, dx, dy, reach) {
                        if t < best {
                            best = t;
                        }
                    }
                }
            }
        }
    }
    if best.is_finite() { best } else { f64::NAN }
}

/// roadwidth.measure_row and clearance_row of every road against its cells' walls.
/// lonlat (N, 2) lon lat, off (R + 1,): the roads' points; want (R,): 1 the right of way (measure_row), 2 the
/// clearance (clearance_row), 3 both; rcells / rcoff: each road's cells (indices into cells, road_near_cells' order);
/// cells: [(origin (3,), rot (3, 3), keys, first, counts, seg_of, segs (m, 4) float32)], a cell's frame and Walls;
/// measure: (step, end_skip, reach, min_samples, walled, agree); clear: (step, end_skip, reach, min_samples, q,
/// walled). Returns (row (R,), near (R,), wall_to_wall (R,), walls (R,)): NaN for None; walls False where the road's
/// cells have no wall at all (clearance_row's None, not (None, None)).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn road_walls<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                       want: PyReadonlyArray1<'py, u8>, rcells: PyReadonlyArray1<'py, i64>,
                       rcoff: PyReadonlyArray1<'py, i64>, cells: &Bound<'py, PyList>, bucket: f64,
                       measure: (f64, f64, f64, usize, f64, f64), clear: (f64, f64, f64, usize, f64, f64))
                       -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>,
                                    Bound<'py, PyArray1<bool>>)> {
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("road_walls: {m}"));
    let ll = lonlat.as_array();
    if ll.ncols() != 2 {
        return Err(bad("lonlat (n, 2)"));
    }
    let off = off.as_slice().map_err(|_| bad("off contiguous"))?;
    let want = want.as_slice().map_err(|_| bad("want contiguous"))?;
    let rcells = rcells.as_slice().map_err(|_| bad("rcells contiguous"))?;
    let rcoff = rcoff.as_slice().map_err(|_| bad("rcoff contiguous"))?;
    let nr = want.len();
    let np = ll.nrows();
    if off.len() != nr + 1 || rcoff.len() != nr + 1 || off.windows(2).any(|w| w[1] < w[0]) || off[0] < 0
        || off[nr] as usize > np || rcoff.windows(2).any(|w| w[1] < w[0]) || rcoff[0] < 0
        || rcoff[nr] as usize > rcells.len() || !(bucket > 0.0) {
        return Err(bad("offsets"));
    }
    // the cells' arrays, borrowed for the call
    let mut frames = Vec::with_capacity(cells.len());
    let mut idx: Vec<[PyReadonlyArray1<'py, i64>; 4]> = Vec::with_capacity(cells.len());
    let mut sgs: Vec<PyReadonlyArray2<'py, f32>> = Vec::with_capacity(cells.len());
    for item in cells.iter() {
        let t = item.cast::<PyTuple>().map_err(|_| bad("a cell: a tuple"))?;
        if t.len() != 7 {
            return Err(bad("a cell: (origin, rot, keys, first, counts, seg_of, segs)"));
        }
        let o: PyReadonlyArray1<'py, f64> = t.get_item(0)?.extract()?;
        let r: PyReadonlyArray2<'py, f64> = t.get_item(1)?.extract()?;
        frames.push(crate::frame_of(&o, &r)?);
        idx.push([t.get_item(2)?.extract()?, t.get_item(3)?.extract()?, t.get_item(4)?.extract()?,
                  t.get_item(5)?.extract()?]);
        sgs.push(t.get_item(6)?.extract()?);
    }
    let mut cs: Vec<Cell> = Vec::with_capacity(frames.len());
    for (k, (o, r)) in frames.iter().enumerate() {
        let keys = idx[k][0].as_slice().map_err(|_| bad("keys contiguous"))?;
        let first = idx[k][1].as_slice().map_err(|_| bad("first contiguous"))?;
        let counts = idx[k][2].as_slice().map_err(|_| bad("counts contiguous"))?;
        let seg_of = idx[k][3].as_slice().map_err(|_| bad("seg_of contiguous"))?;
        if sgs[k].as_array().ncols() != 4 && sgs[k].as_array().nrows() > 0 {
            return Err(bad("segs (m, 4)"));
        }
        let segs = sgs[k].as_slice().map_err(|_| bad("segs contiguous"))?;
        let m = (segs.len() / 4) as i64;
        if first.len() != keys.len() || counts.len() != keys.len()
            || first.iter().zip(counts.iter()).any(|(&f, &c)| f < 0 || c < 0 || (f + c) as usize > seg_of.len())
            || seg_of.iter().any(|&s| s < 0 || s >= m) {
            return Err(bad("a cell's index does not fit its walls"));
        }
        cs.push(Cell { o: *o, r: *r, keys, first, counts, seg_of, segs });
    }
    if rcells.iter().any(|&c| c < 0 || c as usize >= cs.len()) {
        return Err(bad("a road's cell out of range"));
    }
    let lon: Vec<f64> = ll.column(0).iter().copied().collect();
    let lat: Vec<f64> = ll.column(1).iter().copied().collect();
    let cs = &cs;
    let got: Vec<(f64, f64, f64, bool)> = py.detach(|| {
        par_map(nr, PAR_MIN, |r| {
            let none = (f64::NAN, f64::NAN, f64::NAN, false);
            let mine: Vec<&Cell> = rcells[rcoff[r] as usize..rcoff[r + 1] as usize].iter().map(|&c| &cs[c as usize]).collect();
            if mine.is_empty() || want[r] & 3 == 0 || mine.iter().all(|c| c.keys.is_empty()) {
                return none;
            }
            let (a, b) = (off[r] as usize, off[r + 1] as usize);
            let home = mine[0];
            let (mut xs, mut ys) = (Vec::with_capacity(b - a), Vec::with_capacity(b - a));
            for i in a..b {
                let (e, n, _) = crate::g2e_one(lat[i], lon[i], 0.0, &home.o, &home.r);
                xs.push(e);
                ys.push(n);
            }
            let memo: Converted = std::cell::RefCell::new(std::collections::HashMap::new());
            let mut out = (f64::NAN, f64::NAN, f64::NAN, true);
            if want[r] & 1 != 0 {
                let (step, end_skip, reach, min_samples, walled, agree) = measure;
                let row = crate::measure_core(&xs, &ys, step, end_skip, min_samples, walled, agree,
                                              |cx, cy, dx, dy| ray_cells(&mine, &memo, bucket, cx, cy, dx, dy, reach));
                out.0 = row.unwrap_or(f64::NAN);
            }
            if want[r] & 2 != 0 {
                let (step, end_skip, reach, min_samples, q, walled) = clear;
                let (near, w2w) = crate::clearance_core(&xs, &ys, step, end_skip, reach, min_samples, q, walled,
                                                        |cx, cy, dx, dy| ray_cells(&mine, &memo, bucket, cx, cy, dx, dy, reach));
                out.1 = near.unwrap_or(f64::NAN);
                out.2 = w2w.unwrap_or(f64::NAN);
            }
            out
        })
    });
    let row: Vec<f64> = got.iter().map(|g| g.0).collect();
    let near: Vec<f64> = got.iter().map(|g| g.1).collect();
    let w2w: Vec<f64> = got.iter().map(|g| g.2).collect();
    let walls: Vec<bool> = got.iter().map(|g| g.3).collect();
    Ok((row.into_pyarray(py), near.into_pyarray(py), w2w.into_pyarray(py), walls.into_pyarray(py)))
}
