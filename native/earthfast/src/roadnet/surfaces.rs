//! Road surfaces from the map's values (roads.surface_token, roads.surface_kind, roads._map_surface_values and
//! _spec_inputs' surface columns), byte for byte the Python they replaced.

use std::collections::{BTreeMap, BTreeSet};

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

use super::pyobj::{items, lower_is, py_str, py_strip};
use super::pyobj::CArr1;

/// Python's str.lower() of s: ASCII by itself, anything else by CPython's own (its Unicode tables and special
/// casing: the token is an output, Spec.surf).
fn py_lower(py: Python<'_>, s: &str) -> PyResult<String> {
    if s.is_ascii() {
        return Ok(s.to_ascii_lowercase());
    }
    PyString::new(py, s).call_method0("lower")?.extract::<String>()
}

/// roads.surface_token of a value's str(): lower-cased, the part before the first ";", then before the first ":",
/// stripped.
fn token_of(py: Python<'_>, s: &str) -> PyResult<String> {
    let low = py_lower(py, s)?;
    let a = low.split(';').next().unwrap_or("");
    let b = a.split(':').next().unwrap_or("");
    Ok(py_strip(b).to_owned())
}

/// The kinds' codes (roads._KINDS): paved, cobble, gravel, dirt.
const PAVED: i8 = 0;
const COBBLE: i8 = 1;
const GRAVEL: i8 = 2;
const DIRT: i8 = 3;
const KINDS: [&str; 4] = ["paved", "cobble", "gravel", "dirt"];

/// roads' surface sets and STYLE's paved flags, made once per process (roads._surface_rules).
#[pyclass(frozen, module = "earthfast")]
pub struct SurfaceRules {
    paved: BTreeSet<String>,
    cobble: BTreeSet<String>,
    gravel: BTreeSet<String>,
    unpaved: BTreeSet<String>,
    style_paved: BTreeMap<String, bool>,
    unknown_paved: bool,
}

impl SurfaceRules {
    fn style_kind(&self, cls: Option<&str>) -> i8 {
        let paved = cls.and_then(|c| self.style_paved.get(c).copied()).unwrap_or(self.unknown_paved);
        if paved { PAVED } else { DIRT }
    }

    /// roads.surface_kind(cls, surface) given surface's token (None: no value).
    fn kind_of_token(&self, cls: Option<&str>, tok: Option<&str>) -> i8 {
        if let Some(s) = tok {
            if self.paved.contains(s) {
                return PAVED;
            }
            if self.cobble.contains(s) {
                return COBBLE;
            }
            if self.gravel.contains(s) {
                return GRAVEL;
            }
            if s == "unpaved" && cls != Some("track") {
                return GRAVEL;
            }
            if self.unpaved.contains(s) {
                return DIRT;
            }
        }
        self.style_kind(cls)
    }
}

fn opt_str(v: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if v.is_none() { Ok(None) } else { Ok(Some(py_str(v)?)) }
}

/// Surface tokens in order of first appearance (_spec_inputs' token()).
#[derive(Default)]
struct Tokens {
    list: Vec<String>,
    at: BTreeMap<String, i32>,
}

impl Tokens {
    fn index(&mut self, t: &str) -> i32 {
        if let Some(&k) = self.at.get(t) {
            return k;
        }
        let k = self.list.len() as i32;
        self.at.insert(t.to_owned(), k);
        self.list.push(t.to_owned());
        k
    }
}

#[pymethods]
impl SurfaceRules {
    /// SurfaceRules(paved, cobble (with its aliases), gravel, unpaved, {class: paved}, unknown paved).
    #[new]
    fn new(paved: Vec<String>, cobble: Vec<String>, gravel: Vec<String>, unpaved: Vec<String>,
           style_paved: BTreeMap<String, bool>, unknown_paved: bool) -> Self {
        SurfaceRules {
            paved: paved.into_iter().collect(),
            cobble: cobble.into_iter().collect(),
            gravel: gravel.into_iter().collect(),
            unpaved: unpaved.into_iter().collect(),
            style_paved,
            unknown_paved,
        }
    }

    /// roads.surface_token(surface): None for None.
    fn token(&self, py: Python<'_>, surface: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        match opt_str(surface)? {
            None => Ok(None),
            Some(s) => Ok(Some(token_of(py, &s)?)),
        }
    }

    /// roads.surface_kind(cls, surface): "paved", "cobble", "gravel" or "dirt" (cls: STYLE's class, another or None
    /// as "unknown").
    fn kind(&self, py: Python<'_>, cls: &Bound<'_, PyAny>, surface: &Bound<'_, PyAny>) -> PyResult<&'static str> {
        let t = match opt_str(surface)? {
            None => None,
            Some(s) => Some(token_of(py, &s)?),
        };
        if let Ok(c) = cls.cast_exact::<PyString>() {
            return Ok(KINDS[self.kind_of_token(Some(c.to_str()?), t.as_deref()) as usize]);
        }
        // (any other class value as the Python asked it, at its own points: `cls != "track"` its rich comparison,
        // STYLE.get(cls) a dict's lookup, its hash and ==; a str subclass's __str__ is never asked: Codex round 2 on
        // ec3397ee)
        if let Some(s) = t.as_deref() {
            if self.paved.contains(s) {
                return Ok(KINDS[PAVED as usize]);
            }
            if self.cobble.contains(s) {
                return Ok(KINDS[COBBLE as usize]);
            }
            if self.gravel.contains(s) {
                return Ok(KINDS[GRAVEL as usize]);
            }
            if s == "unpaved" && cls.ne("track")? {
                return Ok(KINDS[GRAVEL as usize]);
            }
            if self.unpaved.contains(s) {
                return Ok(KINDS[DIRT as usize]);
            }
        }
        let style = pyo3::types::PyDict::new(py);
        for (k, v) in &self.style_paved {
            style.set_item(k, *v)?;
        }
        let paved = match style.get_item(cls)? {
            Some(v) => v.is_truthy()?,
            None => self.unknown_paved,
        };
        Ok(KINDS[if paved { PAVED } else { DIRT } as usize])
    }

    /// _spec_inputs' surface columns: {"skind" int8 (surface_kind of each class and value), "stok" int32 (its token's
    /// index in "tokens", -1 for no value), "reg_pre" bool (the regional gravel rule may decide it: REGIONAL_ON, no
    /// value, a REGIONAL_CLASSES class, no mapped lanes, no good smoothness, no paved photo verdict), "tokens" (in
    /// order of first appearance)}. vals: each feature's value (None: none); verdicts: the aerial's [(feature,
    /// verdict)] (roadsurf.layer_verdicts) on the roads with no value: unpaved -> "gravel" (a track stays as it is),
    /// another -> a paved photo verdict ("asphalt" for a track).
    #[pyo3(signature = (cls, vals, verdicts, unpaved, osm_lanes, good, regional_on, regional))]
    #[allow(clippy::too_many_arguments)]
    fn spec_surfaces<'py>(&self, py: Python<'py>, cls: Vec<String>, vals: Bound<'py, PyList>,
                          verdicts: Vec<(usize, i64)>, unpaved: i64, osm_lanes: CArr1<'py, f64>,
                          good: CArr1<'py, bool>, regional_on: bool, regional: Vec<String>)
                          -> PyResult<Bound<'py, PyDict>> {
        let n = cls.len();
        let (lanes, good) = (osm_lanes.as_slice()?, good.as_slice()?);
        let mut vals: Vec<Option<String>> = vals.iter().map(|v| opt_str(&v)).collect::<PyResult<_>>()?;
        // (reg_pre is np.fromiter(..., n) & np.isnan(osm_lanes) & ~good & ~photo_paved: numpy broadcasting, below)
        if vals.len() != n {
            return Err(pyo3::exceptions::PyValueError::new_err("spec_surfaces: columns of other lengths"));
        }
        let mut photo = vec![false; n];
        for &(i, v) in &verdicts {
            if i >= n {
                return Err(pyo3::exceptions::PyIndexError::new_err("spec_surfaces: a verdict past the layer"));
            }
            if vals[i].is_some() {
                continue;
            }
            if v == unpaved {
                if cls[i] != "track" {
                    vals[i] = Some("gravel".to_owned());
                }
            } else {
                photo[i] = true;
                if cls[i] == "track" {
                    vals[i] = Some("asphalt".to_owned());
                }
            }
        }
        let regional: BTreeSet<String> = regional.into_iter().collect();
        let mut tokens = Tokens::default();
        let mut toks: Vec<Option<String>> = Vec::with_capacity(n);
        for v in &vals {
            toks.push(match v {
                None => None,
                Some(s) => Some(token_of(py, s)?),
            });
        }
        let skind: Vec<i8> = (0..n).map(|i| self.kind_of_token(Some(&cls[i]), toks[i].as_deref())).collect();
        let stok: Vec<i32> = toks.iter().map(|t| t.as_deref().map_or(-1, |t| tokens.index(t))).collect();
        // (numpy's broadcasting of the four operands, in order: equal lengths, or one of them 1 (which may stretch to
        // any length, 0 too); another pair is numpy's ValueError)
        let mut len = n;
        for k in [lanes.len(), good.len(), n] {
            if k == len || k == 1 {
                continue;
            }
            if len == 1 {
                len = k;
                continue;
            }
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "operands could not be broadcast together with shapes ({len},) ({k},)")));
        }
        let at = |l: usize, i: usize| if l == 1 { 0 } else { i };
        let reg_pre: Vec<bool> = (0..len).map(|i| {
            let j = at(n, i);
            regional_on && vals[j].is_none() && regional.contains(&cls[j]) && lanes[at(lanes.len(), i)].is_nan()
                && !good[at(good.len(), i)] && !photo[j]
        }).collect();
        let d = PyDict::new(py);
        d.set_item("skind", skind.into_pyarray(py))?;
        d.set_item("stok", stok.into_pyarray(py))?;
        d.set_item("reg_pre", reg_pre.into_pyarray(py))?;
        d.set_item("tokens", PyList::new(py, tokens.list)?)?;
        Ok(d)
    }

    /// _spec_inputs' Swedish surface columns: (se_tag bool, se_kind int8, se_tok int32, tokens): each road in Sweden
    /// (sweden) with an OSM surface tag (osm_surface[j]: not None, "", "None" or "nan") has its own kind and token,
    /// its index in tokens (spec_surfaces' list, gaining the new ones in road order).
    fn sweden_surfaces<'py>(&self, py: Python<'py>, cls: Vec<String>, sweden: CArr1<'py, bool>,
                            osm_surface: Option<Bound<'py, PyAny>>, tokens: Vec<String>)
                            -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<i8>>,
                                         Bound<'py, PyArray1<i32>>, Bound<'py, PyList>)> {
        let se = sweden.as_slice()?;
        let n = se.len();
        let mut tok = Tokens::default();
        for t in &tokens {
            tok.index(t);
        }
        let mut se_tag = vec![false; n];
        let mut se_kind = vec![0i8; n];
        let mut se_tok = vec![-1i32; n];
        let stags = osm_surface.filter(|c| !c.is_none());
        for j in 0..n {
            if !se[j] {
                continue;
            }
            let Some(stags) = stags.as_ref() else { continue };
            let tag = stags.get_item(j)?;         // (stags[j], the column's own element: str() of a numpy scalar)
            if tag.is_none() {
                continue;
            }
            let tag = py_str(&tag)?;
            if tag.is_empty() || tag == "None" || tag == "nan" {
                continue;
            }
            let t = token_of(py, &tag)?;
            se_tag[j] = true;
            se_kind[j] = self.kind_of_token(cls.get(j).map(|c| c.as_str()), Some(&t));
            se_tok[j] = tok.index(&t);
        }
        Ok((se_tag.into_pyarray(py), se_kind.into_pyarray(py), se_tok.into_pyarray(py), PyList::new(py, tok.list)?))
    }
}

/// roads._map_surface_values: each feature's surface value: OpenStreetMap's surface (str(), stripped; not "", "nan"
/// or "none" in any case), else the surface rule's value covering the most of it (the table's own element, a numpy
/// str), else None.
#[pyfunction]
#[pyo3(signature = (n, osm_surface, table))]
pub fn map_surface_values<'py>(py: Python<'py>, n: usize, osm_surface: Option<Bound<'py, PyAny>>,
                               table: Option<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyList>> {
    let mut out: Vec<Option<Bound<'py, PyAny>>> = vec![None; n];
    if let Some(vals) = items(osm_surface.as_ref())? {
        for (i, v) in vals.iter().enumerate().take(n) {
            let s = py_str(v)?;
            let s = py_strip(&s);
            if !s.is_empty() && !lower_is(s, "nan") && !lower_is(s, "none") {
                out[i] = Some(PyString::new(py, s).into_any());
            }
        }
    }
    if let Some(t) = table.filter(|t| !t.is_none()) {
        if t.getattr("feature")?.len()? > 0 && n > 0 {
            let maj: CArr1<i64> = t.call_method1("majors", (n,))?.extract()?;
            let maj = maj.as_slice()?;
            let value = t.getattr("value")?;
            for i in 0..n {
                if maj[i] >= 0 && out[i].is_none() {
                    out[i] = Some(value.get_item(maj[i])?);
                }
            }
        }
    }
    PyList::new(py, out)
}

/// _spec_inputs' "aisle": the subclass rule's majority value is "parking_aisle" (==), for the roads in Sweden.
#[pyfunction]
pub fn aisles<'py>(py: Python<'py>, sweden: CArr1<'py, bool>, table: Option<Bound<'py, PyAny>>)
                   -> PyResult<Bound<'py, PyArray1<bool>>> {
    let se = sweden.as_slice()?;
    let n = se.len();
    let mut out = vec![false; n];
    if let Some(t) = table.filter(|t| !t.is_none()) {
        if t.getattr("feature")?.len()? > 0 && se.iter().any(|&b| b) {
            let maj: CArr1<i64> = t.call_method1("majors", (n,))?.extract()?;
            let maj = maj.as_slice()?;
            let value = t.getattr("value")?;
            let key = PyString::new(py, "parking_aisle");
            for j in 0..n {
                if se[j] && maj[j] >= 0 {
                    out[j] = value.get_item(maj[j])?.eq(&key)?;
                }
            }
        }
    }
    Ok(out.into_pyarray(py))
}
