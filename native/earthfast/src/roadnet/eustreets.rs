//! _road_specs' European street pass (BNE_EU_ROAD_DETAILS: PR #505's street edges) in Rust, byte for byte the Python it
//! replaced: which roads it plans (a built street class, not a link, divided, pass road or roundabout ring), each one's
//! middle point and country, its mapped width (the width rule's, else OpenStreetMap's, else Germany's surveyed or
//! France's rural one) and frontage. The plan itself (europewalks.plan: the locality from the bundled place names, the
//! pavement's hash, the verges) stays a Python callback, as does the country (europewalks.country_for).

use std::collections::BTreeSet;


use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyTuple};

use super::countries::np_index;
use super::pyobj::{py_float, py_str};
use super::pyobj::{CArr1, CArr2};

/// A lazily asked Python value (the width rule's table, width_rejected's OSM mask): f() at the first need.
struct Lazy<'py> {
    f: Bound<'py, PyAny>,
    got: Option<Bound<'py, PyAny>>,
}

impl<'py> Lazy<'py> {
    fn get(&mut self) -> PyResult<&Bound<'py, PyAny>> {
        if self.got.is_none() {
            self.got = Some(self.f.call0()?);
        }
        Ok(self.got.as_ref().unwrap())
    }
}

/// roads._rule_value(table, i): the value of feature i's major rule, None without a table, rules or one (and None where
/// the rule's value is None itself: the pass's `width is None` takes it as no width, as the Python did; Codex on
/// 490d400d).
fn rule_value<'py>(table: &Bound<'py, PyAny>, i: usize) -> PyResult<Option<Bound<'py, PyAny>>> {
    if table.is_none() || table.getattr("feature")?.len()? == 0 {
        return Ok(None);
    }
    let j: i64 = table.call_method1("major", (i,))?.extract()?;
    if j < 0 {
        return Ok(None);
    }
    let v = table.getattr("value")?.get_item(j)?;
    Ok(if v.is_none() { None } else { Some(v) })
}

/// roads._osm(feats, "width", i, lo, hi) of the column (None absent): float(col[i]) where a number, finite, in lo..hi.
fn osm_value(col: Option<&Bound<'_, PyAny>>, i: usize, lo: f64, hi: f64) -> PyResult<Option<f64>> {
    let Some(col) = col else { return Ok(None) };
    let Some(v) = py_float(&col.get_item(i)?)? else { return Ok(None) };
    if !v.is_finite() || v < lo || v > hi {
        return Ok(None);
    }
    Ok(Some(v))
}

/// The pass over `specs` (franceroads.adjust_specs' list, changed in place and returned): each spec of a road that is
/// built (kind of `built`), a street (cls of `streets`), not a link, divided (median) or pass road (mountain), not a
/// mapped ring (str(junction[i]) of `rings`), with points (its first point p0 < the first past it p1), whose middle
/// point (lonlat[(p0 + p1) // 2], Python floats) country_of gives a country, becomes plan(spec, country, lon, lat,
/// str(names[i]) or "", str(tags[i]) or "", width, front, infer). width: the width rule's value (rule_table(): asked
/// at the first need) where it is in 1-40 by float(), else OSM's width in osm_lo..osm_hi unless rejected()[i] (asked
/// only with an OSM width column), else spec.width where i is in `surveyed` (Germany's surveyed width) or France's
/// rural experiment has the road (france_on, spec.france, not spec.mountain, not spec.urban). front: (0.0, 0.0)
/// where frontage[i, 0] is NaN, else (frontage[i, 0], frontage[i, 1]).
#[pyfunction]
#[pyo3(signature = (specs, part_off, ring_off, coord_off, lonlat, junction, rings, built, streets, names, tags,
                    frontage, surveyed, france_on, osm_width, osm_lo, osm_hi, rule_table, rejected, country_of, plan,
                    infer))]
#[allow(clippy::too_many_arguments)]
pub fn eu_street_pass<'py>(py: Python<'py>, specs: Bound<'py, PyList>, part_off: CArr1<'py, i64>,
                           ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>,
                           lonlat: CArr2<'py, f64>, junction: Option<Bound<'py, PyAny>>, rings: Vec<String>,
                           built: Vec<String>, streets: Vec<String>, names: Option<Bound<'py, PyAny>>,
                           tags: Option<Bound<'py, PyAny>>, frontage: CArr2<'py, f64>,
                           surveyed: Bound<'py, PyAny>, france_on: bool, osm_width: Option<Bound<'py, PyAny>>,
                           osm_lo: f64, osm_hi: f64, rule_table: Bound<'py, PyAny>, rejected: Bound<'py, PyAny>,
                           country_of: Bound<'py, PyAny>, plan: Bound<'py, PyAny>, infer: bool)
                           -> PyResult<Bound<'py, PyList>> {
    let (po, ro, co) = (part_off.as_slice()?, ring_off.as_slice()?, coord_off.as_slice()?);
    let ll = lonlat.as_array();
    super::lanesplit::check_lonlat(ll.ncols())?;
    let fr = frontage.as_array();
    let rings: BTreeSet<String> = rings.into_iter().collect();
    let built: BTreeSet<String> = built.into_iter().collect();
    let streets: BTreeSet<String> = streets.into_iter().collect();
    let junction = junction.filter(|c| !c.is_none());
    let names = names.filter(|c| !c.is_none());
    let tags = tags.filter(|c| !c.is_none());
    let osm_width = osm_width.filter(|c| !c.is_none());
    let mut table = Lazy { f: rule_table, got: None };
    let mut rej = Lazy { f: rejected, got: None };
    let first = |i: i64| -> PyResult<i64> {
        let p = po[np_index(po.len(), i)?];
        let r = ro[np_index(ro.len(), p)?];
        Ok(co[np_index(co.len(), r)?])
    };
    for i in 0..specs.len() {
        let spec = specs.get_item(i)?;
        if spec.is_none()
            || !built.contains(&py_str(&spec.getattr("kind")?)?)
            || !streets.contains(&py_str(&spec.getattr("cls")?)?)
            || spec.getattr("link")?.is_truthy()?
            || spec.getattr("median")?.is_truthy()?
            || spec.getattr("mountain")?.is_truthy()?
            || match &junction {
                Some(c) => rings.contains(&py_str(&c.get_item(i)?)?),
                None => false,
            }
        {
            continue;
        }
        let (a, b) = (first(i as i64)?, first(i as i64 + 1)?);
        if b <= a {
            continue;
        }
        // ((a + b) // 2 of numpy int64s: wrapping, floor division; offsets from map data never panic: Codex on
        // 490d400d)
        let k = np_index(ll.nrows(), a.wrapping_add(b).div_euclid(2))?;
        let (lon, lat) = (ll[[k, 0]], ll[[k, 1]]);
        let country = country_of.call1((lon, lat))?;
        if country.is_none() {
            continue;
        }
        // width = mapped_rule_width(feats, i); not 1.0 <= float(width) <= 40.0: mapped_osm_width(feats, i, *OSM_WIDTH_M)
        let mut width: Option<Bound<'py, PyAny>> = rule_value(table.get()?, i)?;
        let in_range = match &width {
            Some(w) => {
                let v = super::pyobj::py_float_strict(w)?;
                (1.0..=40.0).contains(&v)
            }
            None => false,
        };
        if !in_range {
            width = None;
            let rejected_here = match &osm_width {
                Some(_) => rej.get()?.get_item(i)?.is_truthy()?,
                None => false,
            };
            if !rejected_here {
                width = osm_value(osm_width.as_ref(), i, osm_lo, osm_hi)?.map(|v| PyFloat::new(py, v).into_any());
            }
        }
        if width.is_none() && surveyed.contains(i)? {
            width = Some(spec.getattr("width")?);
        }
        if width.is_none() && france_on && spec.getattr_opt("france")?.map(|v| v.is_truthy()).transpose()?.unwrap_or(false)
            && !spec.getattr("mountain")?.is_truthy()? && !spec.getattr("urban")?.is_truthy()?
        {
            width = Some(spec.getattr("width")?);
        }
        if i >= fr.nrows() || fr.ncols() < 2 {
            return Err(PyIndexError::new_err(format!("index {i} is out of bounds for axis 0 with size {}", fr.nrows())));
        }
        let front = if fr[[i, 0]].is_nan() { (0.0, 0.0) } else { (fr[[i, 0]], fr[[i, 1]]) };
        let name = match &names {
            Some(c) => py_str(&c.get_item(i)?)?,
            None => String::new(),
        };
        let tag = match &tags {
            Some(c) => py_str(&c.get_item(i)?)?,
            None => String::new(),
        };
        let args = PyTuple::new(py, [spec.clone(), country, PyFloat::new(py, lon).into_any(),
                                     PyFloat::new(py, lat).into_any(), name.into_pyobject(py)?.into_any(),
                                     tag.into_pyobject(py)?.into_any(),
                                     width.unwrap_or_else(|| py.None().into_bound(py)),
                                     PyTuple::new(py, [front.0, front.1])?.into_any(),
                                     pyo3::types::PyBool::new(py, infer).to_owned().into_any()])?;
        specs.set_item(i, plan.call1(args)?)?;
    }
    Ok(specs)
}
