//! railprofile's lattice nodes (the full port, 492): a node's bed heights by (fid, kind) and centimetre key. The
//! Python grouped a node's parts by (fid, kind), joined each group's keys and heights and took numpy's unique of the
//! reversed keys (the last of equal keys, as a dict's update) group by group: ~0.9 CPU-s of a Manhattan visit's
//! ~800 nodes. Now one call packs every group (rail_node_pack) into a RailNode the node memo holds, and a lookup
//! (RailNode.find: the key, then its eight neighbours a centimetre off in railprofile's order) is one call per
//! (node, part). Integer keys only: no float arithmetic.
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A centimetre key (a, b) as one int64, numpy's wrapping arithmetic: (a + 2^31) << 32 | (b + 2^31).
#[inline]
fn pack(a: i64, b: i64) -> i64 {
    const H: i64 = 1 << 31;
    (a.wrapping_add(H).wrapping_shl(32)) | b.wrapping_add(H)
}

/// The keys a centimetre off tried after the key itself, in railprofile._find's order.
const NEIGHBOURS: [(i64, i64); 9] = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)];

/// A node's groups ((fid, kind): the caller's numbering): each one's packed centimetre keys, sorted and unique, and
/// the height of each key's last point.
#[pyclass(module = "earthfast", frozen)]
pub struct RailNode {
    start: Box<[usize]>,
    keys: Box<[i64]>,
    zs: Box<[f64]>,
}

#[pymethods]
impl RailNode {
    /// The points kept (the node memo's measure).
    fn __len__(&self) -> usize {
        self.keys.len()
    }

    /// (k,): group g's height at each centimetre key cm (k, 2), or at the first of the keys a centimetre off that it
    /// has, or NaN.
    fn find<'py>(&self, py: Python<'py>, g: usize, cm: PyReadonlyArray2<'py, i64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
        if g >= self.start.len() - 1 {
            return Err(PyValueError::new_err("RailNode.find: no such group"));
        }
        let c = cm.as_array();
        if c.nrows() > 0 && c.ncols() != 2 {
            return Err(PyValueError::new_err("RailNode.find: cm (k, 2)"));
        }
        let (keys, zs) = (&self.keys[self.start[g]..self.start[g + 1]], &self.zs[self.start[g]..self.start[g + 1]]);
        let out: Vec<f64> = (0..c.nrows()).map(|k| {
            let (a, b) = (c[[k, 0]], c[[k, 1]]);
            NEIGHBOURS.iter().find_map(|&(da, db)| keys.binary_search(&pack(a.wrapping_add(da), b.wrapping_add(db))).ok())
                .map_or(f64::NAN, |at| zs[at])
        }).collect();
        Ok(out.into_pyarray(py))
    }
}

/// Group g's (key, height) pairs: the keys sorted, each key's height that of its last point in the group's order
/// (its parts in turn, each part's points in turn).
fn pack_group(pts: &mut [(i64, usize)], z: &[f64], keys: &mut Vec<i64>, zs: &mut Vec<f64>) {
    // (key, then the later point first: the first of each key is its last point)
    pts.sort_unstable_by(|p, q| p.0.cmp(&q.0).then(q.1.cmp(&p.1)));
    let mut prev: Option<i64> = None;
    for &(k, at) in pts.iter() {
        if prev != Some(k) {
            keys.push(k);
            zs.push(z[at]);
            prev = Some(k);
        }
    }
}

/// A node's RailNode: cm (n, 2) the points' centimetre keys and z (n,) their heights, end to end by part (off (parts
/// + 1,)), group (parts,) each part's group (0..groups, the caller's (fid, kind) numbering). A point two parts of a
/// group share has the later part's height.
#[pyfunction]
pub fn rail_node_pack(py: Python<'_>, cm: PyReadonlyArray2<'_, i64>, z: PyReadonlyArray1<'_, f64>,
                      off: PyReadonlyArray1<'_, i64>, group: PyReadonlyArray1<'_, i64>, groups: usize)
                      -> PyResult<RailNode> {
    let (c, zv, ov, gv) = (cm.as_array(), z.as_array(), off.as_array(), group.as_array());
    let n = zv.len();
    if c.nrows() != n || (n > 0 && c.ncols() != 2) || ov.len() != gv.len() + 1 || groups > gv.len() {
        return Err(PyValueError::new_err("rail_node_pack: cm (n, 2), z (n,), off (parts + 1,), group (parts,), groups <= parts"));
    }
    let off: Vec<usize> = ov.iter().map(|&o| usize::try_from(o).ok().filter(|&o| o <= n))
        .collect::<Option<_>>().ok_or_else(|| PyValueError::new_err("rail_node_pack: off out of range"))?;
    if off.windows(2).any(|w| w[0] > w[1]) {
        return Err(PyValueError::new_err("rail_node_pack: off not increasing"));
    }
    let group: Vec<usize> = gv.iter().map(|&g| usize::try_from(g).ok().filter(|&g| g < groups))
        .collect::<Option<_>>().ok_or_else(|| PyValueError::new_err("rail_node_pack: group out of range"))?;
    let packed: Vec<i64> = (0..n).map(|k| pack(c[[k, 0]], c[[k, 1]])).collect();
    let zv: Vec<f64> = zv.iter().copied().collect();
    Ok(py.detach(move || {
        let mut by: Vec<Vec<usize>> = vec![Vec::new(); groups];
        for (p, &g) in group.iter().enumerate() {
            by[g].push(p);
        }
        let (mut start, mut keys, mut zs) = (Vec::with_capacity(groups + 1), Vec::with_capacity(n), Vec::with_capacity(n));
        let mut pts: Vec<(i64, usize)> = Vec::new();
        start.push(0);
        for parts in &by {
            pts.clear();
            for &p in parts {
                pts.extend((off[p]..off[p + 1]).map(|at| (packed[at], at)));
            }
            pack_group(&mut pts, &zv, &mut keys, &mut zs);
            start.push(keys.len());
        }
        // (boxed: the memo counts the unique keys, so nothing more is held)
        RailNode { start: start.into_boxed_slice(), keys: keys.into_boxed_slice(), zs: zs.into_boxed_slice() }
    }))
}
