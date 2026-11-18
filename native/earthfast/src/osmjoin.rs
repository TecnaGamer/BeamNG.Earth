//! osmtags.join's tag columns in Rust (the full Rust port: the OpenStreetMap tag join, after its rows: osmrows), byte
//! for byte the Python it replaced: each road's way id (way_id of its src_record), the same-named way round a segment
//! without one (_named_way), the float columns (lanes by roadlanes.count, from the road's own way on a motorway or trunk),
//! the text columns (mapgeom.text_column), and a way without lanes taking the same-named way's going the same way.
//! roadlanes.join stays the facade's (its geometry match: a later piece).
//!
//! Input contract (R2a, roadnet::pyobj::Contract): attrs an exact dict whose src_record / name / class columns are of the
//! contract; tags an exact dict of int way ids -> exact dicts of str keys and the contract's scalars (osm_tags'); the
//! layer's offsets int64 and lonlat a (k, 2+) float64 array. Anything else TypeError.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString};

use crate::dm::Dm;
use crate::roadnet::countries::np_index;
use crate::roadnet::pyobj::{py_float, CArr1, CArr2, Contract};

const NAME_MATCH_M: f64 = 25.0;
const MAX_LANES: f64 = 12.0;

/// roadlanes.count(v): float(v) a whole count 1..12, else None (TypeError / ValueError of float() none).
fn count(v: Option<&Bound<'_, PyAny>>) -> PyResult<Option<i64>> {
    let Some(v) = v else { return Ok(None) };
    Ok(match py_float(v)? {
        Some(n) if n.is_finite() && n.fract() == 0.0 && (1.0..=MAX_LANES).contains(&n) => Some(n as i64),
        _ => None,
    })
}

/// str(v): an exact str as it is, anything else through str().
fn text<'py>(v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    match v.cast_exact::<PyString>() {
        Ok(s) => Ok(s.clone()),
        Err(_) => v.str(),
    }
}

/// A str's code points as a lookup key: its UTF-8, lone surrogates through surrogatepass (their 3-byte forms never
/// occur in valid UTF-8, so two strs share a key only when they are equal).
fn key(s: &Bound<'_, PyString>) -> PyResult<Vec<u8>> {
    match s.to_str() {
        Ok(v) => Ok(v.as_bytes().to_vec()),
        Err(_) => Ok(s.call_method1("encode", ("utf-8", "surrogatepass"))?.cast::<pyo3::types::PyBytes>()?.as_bytes().to_vec()),
    }
}

/// osmtags.way_id(record): int of the digits after a leading "w" (re's \d), else None. ASCII digits read here; a
/// record whose digits run into any non-ASCII character (re's Unicode \d) is asked of the Python.
fn way_id<'py>(py: Python<'py>, rec: &Bound<'py, PyString>, k: &[u8], py_way_id: &Bound<'py, PyAny>)
               -> PyResult<Option<Bound<'py, PyAny>>> {
    let Some(rest) = k.strip_prefix(b"w") else { return Ok(None) };
    let nd = rest.iter().position(|c| !c.is_ascii_digit()).unwrap_or(rest.len());
    if matches!(rest.get(nd), Some(c) if !c.is_ascii()) {
        let got = py_way_id.call1((rec,))?;
        return Ok(if got.is_none() { None } else { Some(got) });
    }
    if nd == 0 {
        return Ok(None);
    }
    let digits = std::str::from_utf8(&rest[..nd]).expect("ASCII digits");
    Ok(Some(py.get_type::<PyInt>().call1((digits,))?))
}

/// One (way id, x0, y0, x1, y1) candidate of a name (by_name / sense).
struct Cand<'py> {
    w: Bound<'py, PyAny>,
    bx: [f64; 4],
}

/// The layer's geometry, read as the Python read it for _named_way.
struct Geo<'a> {
    po: &'a [i64],
    ro: &'a [i64],
    co: &'a [i64],
    ll: numpy::ndarray::ArrayView2<'a, f64>,
}

/// osmtags._named_way(feats, i, by_name) with by_name's list of the road's name already chosen (cands): the smallest
/// box (strictly, the first of equal ones) of the candidates holding the road's middle vertex grown by NAME_MATCH_M.
fn named_way<'py>(g: &Geo<'_>, i: usize, cands: &[Cand<'py>]) -> PyResult<Option<Bound<'py, PyAny>>> {
    if cands.is_empty() {
        return Ok(None);
    }
    let first = |i: i64| -> PyResult<i64> {
        let p = g.po[np_index(g.po.len(), i)?];
        let r = g.ro[np_index(g.ro.len(), p)?];
        Ok(g.co[np_index(g.co.len(), r)?])
    };
    let (a, b) = (first(i as i64)?, first(i as i64 + 1)?);
    if b <= a {
        return Ok(None);
    }
    let k = np_index(g.ll.nrows(), a.wrapping_add(b).wrapping_sub(1).div_euclid(2))?;
    if g.ll.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("not enough values to unpack (expected 2, got 1)"));
    }
    let (lon, lat) = (g.ll[[k, 0]], g.ll[[k, 1]]);
    let my = NAME_MATCH_M / 111320.0;
    let c = lat.to_radians().dcos();
    let mx = my / if 0.1 > c { 0.1 } else { c };
    let mut best: Option<(f64, &Bound<'py, PyAny>)> = None;
    for cd in cands {
        let [x0, y0, x1, y1] = cd.bx;
        if x0 - mx <= lon && lon <= x1 + mx && y0 - my <= lat && lat <= y1 + my {
            let area = (x1 - x0) * (y1 - y0);
            if best.is_none() || area < best.unwrap().0 {
                best = Some((area, &cd.w));
            }
        }
    }
    Ok(best.map(|(_, w)| w.clone()))
}

/// osmtags.join(feats, tags)'s columns (attrs changed in place, in the Python's order); returns the roads' way ids (ids:
/// int or None each) for roadlanes.join.
#[pyfunction]
#[pyo3(signature = (attrs, tags, n, part_off, ring_off, coord_off, lonlat, floats, texts, highways, oneway_yes,
                    py_way_id, text_column))]
#[allow(clippy::too_many_arguments)]
pub fn osm_join<'py>(py: Python<'py>, attrs: Bound<'py, PyAny>, tags: Bound<'py, PyAny>, n: usize,
                     part_off: CArr1<'py, i64>, ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>,
                     lonlat: CArr2<'py, f64>, floats: Vec<String>, texts: Vec<String>, highways: Vec<String>,
                     oneway_yes: Vec<String>, py_way_id: Bound<'py, PyAny>, text_column: Bound<'py, PyAny>)
                     -> PyResult<Bound<'py, PyList>> {
    let refuse = |what: &str| pyo3::exceptions::PyTypeError::new_err(format!("osm_join: {what} (the R2a input contract)"));
    let k = Contract::new(py)?;
    let attrs = attrs.cast_exact::<PyDict>().map_err(|_| refuse("attrs is no exact dict"))?.clone();
    let tags = tags.cast_exact::<PyDict>().map_err(|_| refuse("tags is no exact dict"))?.clone();
    for (w, t) in tags.iter() {
        if !w.is_exact_instance_of::<PyInt>() {
            return Err(refuse("a way id is no int"));
        }
        let t = t.cast_exact::<PyDict>().map_err(|_| refuse("a way's tags are no exact dict"))?;
        for (key, v) in t.iter() {
            if !key.is_exact_instance_of::<PyString>() || !(k.scalar(&v) || key.to_string().starts_with('_')) {
                return Err(refuse("a tag is outside the contract"));
            }
        }
    }
    for col in ["src_record", "name", "class"] {
        if let Some(c) = attrs.get_item(col)? {
            k.column(&c, col)?;
        }
    }
    let g = Geo { po: part_off.as_slice()?, ro: ring_off.as_slice()?, co: coord_off.as_slice()?, ll: lonlat.as_array() };
    let tag_of = |w: &Bound<'py, PyAny>| -> PyResult<Option<Bound<'py, PyDict>>> {
        Ok(match tags.get_item(w)? { Some(t) => Some(t.cast_into::<PyDict>()?), None => None })
    };
    // ids = [way_id(str(recs[i])) if recs is not None else None for i in range(n)]
    let recs = attrs.get_item("src_record")?.filter(|v| !v.is_none());
    let mut ids: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(n);
    for i in 0..n {
        ids.push(match &recs {
            Some(r) => {
                let rec = text(&r.get_item(i)?)?;
                way_id(py, &rec, &key(&rec)?, &py_way_id)?
            }
            None => None,
        });
    }
    // by_name: str(name) -> [(way, bx0, by0, bx1, by1)] of the named ways with a box (all four keys present), in the
    // tags' order; a box's values exact floats (the contract: osm_tags' floats, whose arithmetic is the Python's)
    let boxed = |t: &Bound<'py, PyDict>| -> PyResult<Option<[f64; 4]>> {
        for k in ["bx0", "by0", "bx1", "by1"] {
            if !t.contains(k)? {
                return Ok(None);
            }
        }
        let mut bx = [0.0; 4];
        for (j, k) in ["bx0", "by0", "bx1", "by1"].iter().enumerate() {
            let v = t.get_item(k)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(*k))?;
            // an exact float (osm_tags' values: Python's arithmetic of them is IEEE f64's; numpy scalars carry their
            // own error policy, refused)
            bx[j] = v.cast_exact::<PyFloat>().map_err(|_| refuse("a box value is no float"))?.value();
        }
        Ok(Some(bx))
    };
    let mut by_name: HashMap<Vec<u8>, Vec<Cand<'py>>> = HashMap::new();
    for (w, t) in tags.iter() {
        let t = t.cast_into::<PyDict>()?;
        let name = t.get_item("name")?;
        let truthy = match &name { Some(v) => v.is_truthy()?, None => false };
        if truthy {
            if let Some(bx) = boxed(&t)? {
                by_name.entry(key(&text(name.as_ref().unwrap())?)?).or_default().push(Cand { w: w.clone(), bx });
            }
        }
    }
    let names = attrs.get_item("name")?.filter(|v| !v.is_none());
    // str(oneway) in ONEWAY_YES
    let yes = |ow: &[u8]| oneway_yes.iter().any(|h| h.as_bytes() == ow);
    // named = [w if w in tags else _named_way(feats, i, by_name) for i, w in enumerate(ids)]
    let mut named: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(n);
    for (i, w) in ids.iter().enumerate() {
        let inside = match w { Some(w) => tags.contains(w)?, None => tags.contains(py.None())? };
        if inside {
            named.push(w.clone());
            continue;
        }
        let Some(nm) = &names else { named.push(None); continue };
        let k = key(&text(&nm.get_item(i)?)?)?;
        named.push(match by_name.get(&k) { Some(c) => named_way(&g, i, c)?, None => None });
    }
    // classes[i] in roadlanes.HIGHWAYS (a set of str: the value's hash and ==): a str or numpy.str_ (a text column's
    // element: str's hash and ==) compared here, anything else asked of the set
    let np_str = py.import("numpy")?.getattr("str_")?;
    let hw_set = pyo3::types::PySet::new(py, &highways)?;
    let hw = |c: &Bound<'py, PyAny>| -> PyResult<bool> {
        if c.is_exact_instance_of::<PyString>() || c.get_type().is(&np_str) {
            let s = c.cast::<PyString>()?;
            return Ok(match s.to_str() { Ok(v) => highways.iter().any(|h| h == v), Err(_) => false });
        }
        hw_set.contains(c)
    };
    let classes = attrs.get_item("class")?;
    let class_at = |i: usize| -> PyResult<Bound<'py, PyAny>> {
        match &classes {
            Some(c) => c.get_item(i),
            None => Ok(PyString::new(py, "").into_any()),
        }
    };
    let np = py.import("numpy")?;
    for c in &floats {
        let key = if c.starts_with("osm_") { c.clone() } else { format!("osm_{c}") };
        if c == "lanes" {
            // src = [ids[i] if classes[i] in HIGHWAYS else w for i, w in enumerate(named)] (every class read first), then
            // np.array([roadlanes.count(tags.get(w, {}).get("lanes")) or np.nan for w in src]): int64 where every road
            // has a count, else float64 (numpy's own choice from the values)
            let mut src: Vec<Option<Bound<'py, PyAny>>> = Vec::with_capacity(n);
            for i in 0..n {
                src.push(if hw(&class_at(i)?)? { ids[i].clone() } else { named[i].clone() });
            }
            let vals = PyList::empty(py);
            for w in &src {
                let t = match w { Some(w) => tag_of(w)?, None => None };
                let v = match t { Some(t) => count(t.get_item(c)?.as_ref())?, None => None };
                match v {
                    Some(x) => vals.append(x)?,
                    None => vals.append(PyFloat::new(py, f64::NAN))?,
                }
            }
            attrs.set_item(&key, np.call_method1("array", (vals,))?)?;
            continue;
        }
        let mut out = Vec::with_capacity(n);
        for w in &named {
            out.push(match w {
                Some(w) => match tag_of(w)?.map(|t| t.get_item(c)).transpose()?.flatten() {
                    Some(v) => crate::roadnet::pyobj::py_float_strict(&v)?,
                    None => f64::NAN,
                },
                None => f64::NAN,
            });
        }
        attrs.set_item(&key, numpy::IntoPyArray::into_pyarray(out, py))?;
    }
    for c in &texts {
        let key = if c.starts_with("osm_") { c.clone() } else { format!("osm_{c}") };
        let src = if c == "osm_oneway" { &ids } else { &named };
        let vals = PyList::empty(py);
        for w in src {
            match w {
                Some(w) => match tag_of(w)?.map(|t| t.get_item(c)).transpose()?.flatten() {
                    Some(v) => vals.append(v.str()?)?,
                    None => vals.append("")?,
                },
                None => vals.append("")?,
            }
        }
        attrs.set_item(&key, text_column.call1((vals,))?)?;
    }
    // a way without a lane count takes the same-named way's going the same way (one-way or not)
    let lanes = attrs.get_item("osm_lanes")?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("osm_lanes"))?;
    let ows = attrs.get_item("osm_oneway")?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("osm_oneway"))?;
    let mut sense: HashMap<(Vec<u8>, bool), Vec<Cand<'py>>> = HashMap::new();
    for (w, t) in tags.iter() {
        let t = t.cast_into::<PyDict>()?;
        if !t.contains("lanes")? {
            continue;
        }
        let name = t.get_item("name")?;
        let truthy = match &name { Some(v) => v.is_truthy()?, None => false };
        if !truthy {
            continue;
        }
        if let Some(bx) = boxed(&t)? {
            let ow = match t.get_item("osm_oneway")? { Some(v) => key(&text(&v)?)?, None => Vec::new() };
            let k = (key(&text(name.as_ref().unwrap())?)?, yes(&ow));
            sense.entry(k).or_default().push(Cand { w: w.clone(), bx });
        }
    }
    if !sense.is_empty() {
        if let Some(nm) = &names {
            for i in 0..n {
                let lv = lanes.get_item(i)?;
                let isnan = np.call_method1("isnan", (&lv,))?.is_truthy()?;
                if !(isnan && !hw(&class_at(i)?)?) {
                    continue;
                }
                let name = key(&text(&nm.get_item(i)?)?)?;
                let ow = key(&text(&ows.get_item(i)?)?)?;
                let k = (name, yes(&ow));
                let empty: Vec<Cand<'py>> = Vec::new();
                let cands = sense.get(&k).unwrap_or(&empty);
                if let Some(w) = named_way(&g, i, cands)? {
                    let t = tags.get_item(&w)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("way"))?
                        .cast_into::<PyDict>()?;
                    let v = t.get_item("lanes")?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("lanes"))?;
                    match count(Some(&v))? {
                        Some(x) => lanes.set_item(i, x)?,
                        None => lanes.set_item(i, f64::NAN)?,
                    }
                }
            }
        }
    }
    PyList::new(py, ids.into_iter().map(|w| w.unwrap_or_else(|| py.None().into_bound(py))))
}
