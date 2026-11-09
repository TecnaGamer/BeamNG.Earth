//! OpenStreetMap way ids of Overture records and their rows in a lane / rail tag shard (railtags.LaneTags.join): the
//! record's way id parsed (railtags.way_id: "w<digits>...") and searched in the shard's sorted ids in one call, with
//! the GIL held (a few µs of work: a numpy searchsorted per road released and waited for the GIL under the road
//! workers' threads; a regex, a lookup and a slice per road and per way part were 14 % of a dense tile, 2026-11-08).
//! The records come as numpy's fixed-width unicode (codes (n, width) uint32: its UCS-4 code points, NUL padded), so
//! no Python string is made for them.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

/// A record's way id: none (no "w" and ASCII digit run at its start), the number and its digits' end, or "ask
/// railtags.way_id" (a non-ASCII character right after the digits: Python's \d takes every Unicode decimal digit;
/// more than 18 digits: past i64).
enum Way {
    None,
    Id(i64, usize),
    Ask,
}

fn parse(c: &[u32]) -> Way {
    if c.first() != Some(&(b'w' as u32)) {
        return Way::None;
    }
    let mut j = 1;
    while j < c.len() && (b'0' as u32..=b'9' as u32).contains(&c[j]) {
        j += 1;
    }
    if j < c.len() && c[j] >= 0x80 {
        return Way::Ask;
    }
    if j == 1 {
        return Way::None;
    }
    if j - 1 > 18 {
        return Way::Ask;
    }
    let mut v: i64 = 0;
    for &d in &c[1..j] {
        v = v * 10 + (d - b'0' as u32) as i64;
    }
    Way::Id(v, j)
}

/// The tag set index of way v in the sorted ids (numpy searchsorted's left side), or -1.
fn find(ids: &numpy::ndarray::ArrayView1<i64>, idx: &numpy::ndarray::ArrayView1<i32>, v: i64) -> i64 {
    let n = ids.len().min(idx.len());
    let (mut lo, mut hi) = (0usize, n);
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if ids[mid] < v {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    if lo < n && ids[lo] == v { idx[lo] as i64 } else { -1 }
}

/// The digits of a record (ASCII, from its parse) as a str.
fn digits<'py>(py: Python<'py>, c: &[u32], end: usize) -> Bound<'py, PyString> {
    let mut buf = [0u8; 18];           // (parse: at most 18 ASCII digits)
    for (k, &d) in c[1..end].iter().enumerate() {
        buf[k] = d as u8;
    }
    PyString::new(py, std::str::from_utf8(&buf[..end - 1]).unwrap_or(""))
}

/// Per record (rows of codes) the tag set index of its way id in the shard (ids sorted, idx their tag set indices):
/// -1 no way id or not in the shard, -2 for railtags.way_id to decide.
#[pyfunction]
pub fn way_tag_rows<'py>(py: Python<'py>, codes: PyReadonlyArray2<'py, u32>, ids: PyReadonlyArray1<'py, i64>,
                         idx: PyReadonlyArray1<'py, i32>) -> Bound<'py, PyArray1<i64>> {
    let (codes, ids, idx) = (codes.as_array(), ids.as_array(), idx.as_array());
    let w = codes.ncols();
    let mut row = vec![0u32; w];
    let mut out = Vec::with_capacity(codes.nrows());
    for r in codes.rows() {
        for (k, v) in r.iter().enumerate() {
            row[k] = *v;
        }
        out.push(match parse(&row) {
            Way::None => -1,
            Way::Ask => -2,
            Way::Id(v, _) => find(&ids, &idx, v),
        });
    }
    out.into_pyarray(py)
}

/// LaneTags.join's lane parts: per selected rule row k (codes[k] its record, feature[k] its feature, sorted by feature)
/// the tuple (start[k], end[k], way id (railtags.way_id's str) or None, its tag set tags[...] or a new {}), the rows
/// of a feature in one list. ask(k) -> (way id, tags) for the records left to railtags.way_id. Returns (features (f,),
/// [list of each]).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn lane_parts<'py>(py: Python<'py>, codes: PyReadonlyArray2<'py, u32>, feature: PyReadonlyArray1<'py, i64>,
                       start: PyReadonlyArray1<'py, f64>, end: PyReadonlyArray1<'py, f64>,
                       ids: PyReadonlyArray1<'py, i64>, idx: PyReadonlyArray1<'py, i32>, tags: &Bound<'py, PyList>,
                       ask: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyList>)> {
    let (codes, feature, start, end, ids, idx) = (codes.as_array(), feature.as_array(), start.as_array(),
                                                  end.as_array(), ids.as_array(), idx.as_array());
    let m = codes.nrows();
    if feature.len() != m || start.len() != m || end.len() != m {
        return Err(pyo3::exceptions::PyValueError::new_err("lane_parts: inconsistent arrays"));
    }
    let w = codes.ncols();
    let mut row = vec![0u32; w];
    let mut feats: Vec<i64> = Vec::new();
    let lists = PyList::empty(py);
    let mut cur: Option<Bound<'py, PyList>> = None;
    for k in 0..m {
        for (j, v) in codes.row(k).iter().enumerate() {
            row[j] = *v;
        }
        let (wid, tag): (PyObject, PyObject) = match parse(&row) {
            Way::None => (py.None(), PyDict::new(py).into_any().unbind()),
            Way::Ask => {
                let got = ask.call1((k,))?;
                (got.get_item(0)?.unbind(), got.get_item(1)?.unbind())
            }
            Way::Id(v, e) => {
                let t = find(&ids, &idx, v);
                let tag = if t >= 0 { tags.get_item(t as usize)?.unbind() } else { PyDict::new(py).into_any().unbind() };
                (digits(py, &row, e).into_any().unbind(), tag)
            }
        };
        let item = PyTuple::new(py, [start[k].into_pyobject(py)?.into_any().unbind(),
                                     end[k].into_pyobject(py)?.into_any().unbind(), wid, tag])?;
        if feats.last() != Some(&feature[k]) || cur.is_none() {
            feats.push(feature[k]);
            let l = PyList::empty(py);
            lists.append(&l)?;
            cur = Some(l);
        }
        cur.as_ref().unwrap().append(item)?;
    }
    Ok((feats.into_pyarray(py), lists))
}
