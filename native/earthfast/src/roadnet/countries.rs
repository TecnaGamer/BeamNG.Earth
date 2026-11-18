//! The European street rules' country lookup (europewalks.country_for: PR #505's regional scenery, BNE_EU_ROAD_DETAILS)
//! in Rust: the supported territories' box test, the border bands (countrymap.rs, the file countrybounds.bundled() reads)
//! and europeinfra.COUNTRIES, for points and for each road's middle point.

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use std::collections::BTreeSet;
use std::sync::Arc;



use crate::countrymap::{map, Map};
use super::pyobj::{CArr1, CArr2};

/// numpy's a[i] on an axis of `len` (a negative i counts from the end): the index, IndexError past either end.
pub(crate) fn np_index(len: usize, i: i64) -> PyResult<usize> {
    let n = len as i64;
    if i < -n || i >= n {
        return Err(PyIndexError::new_err(format!("index {i} is out of bounds for axis 0 with size {len}")));
    }
    Ok(if i < 0 { (i + n) as usize } else { i as usize })
}

/// The border index countrybounds.bundled() names, asked at every point past the box test as europewalks.country_for
/// asked it (bundled() may fetch the regional data; on a cold start its manifest can be missing at one point, Absent,
/// and published by the next: Codex on 98e07733): `index()` gives the band file's path, or None (countrybounds.Absent:
/// that point's lookup None). The last path's map is kept (map() keeps every opened one anyway).
struct Borders<'py> {
    index: Bound<'py, PyAny>,
    last: Option<(String, Arc<Map>)>,
}

impl<'py> Borders<'py> {
    fn get(&mut self) -> PyResult<Option<Arc<Map>>> {
        let path = self.index.call0()?;
        if path.is_none() {
            return Ok(None);
        }
        let path = path.extract::<String>()?;
        if let Some((p, m)) = &self.last {
            if *p == path {
                return Ok(Some(m.clone()));
            }
        }
        // (countrybounds.BandIndex opened the file in its constructor; a file it could not open raised there)
        let m = map(&path)?;
        self.last = Some((path, m.clone()));
        Ok(Some(m))
    }
}

/// europewalks.country_for(lon, lat): None south of 25 N and between 168 W and 33 W (the supported territories and
/// the eastern end of Russia), else countrybounds' code at the point (BandIndex.at: None where the lookup raised a
/// ValueError, none was found or two territories overlap) when europeinfra.COUNTRIES has it.
fn country_for(b: &mut Borders, lon: f64, lat: f64, countries: &BTreeSet<String>) -> PyResult<Option<String>> {
    if lat < 25.0 || (-168.0 < lon && lon < -33.0) {
        return Ok(None);
    }
    let Some(m) = b.get()? else { return Ok(None) };
    // (the band's read and decompression without the GIL, as country_index ran it: Codex on 98e07733)
    let mm = m.clone();
    let Ok(k) = b.index.py().detach(move || mm.at(lat, lon)) else { return Ok(None) };
    if k == 0 {
        return Ok(None);
    }
    // ([None] + codes)[k]: a code past the list is the IndexError BandIndex.codes_at raised (not caught by at)
    let Some(code) = m.codes.get(k as usize - 1) else {
        return Err(PyIndexError::new_err("list index out of range"));
    };
    Ok(countries.contains(code).then(|| code.clone()))
}

/// europewalks.country_for of each point (lons, lats); index: () -> the border file's path or None (see Borders).
#[pyfunction]
pub fn europe_countries<'py>(index: Bound<'py, PyAny>, lons: CArr1<'py, f64>, lats: CArr1<'py, f64>,
                             countries: Vec<String>) -> PyResult<Vec<Option<String>>> {
    let (lo, la) = (lons.as_array(), lats.as_array());
    if lo.len() != la.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("lons and lats differ in length"));
    }
    let countries: BTreeSet<String> = countries.into_iter().collect();
    let mut b = Borders { index, last: None };
    lo.iter().zip(la.iter()).map(|(&x, &y)| country_for(&mut b, x, y, &countries)).collect()
}

/// europewalks.country_for at the middle point of each road of mask (in feature order): its first point p0 and the
/// first point past it p1 (coord_off[ring_off[part_off[i]]], ... [i + 1]), lonlat[p0 + (p1 - p0) // 2] where p1 > p0;
/// None for the others. The arrays indexed as numpy did (IndexError past an end, a negative index from the end).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn europe_road_countries<'py>(index: Bound<'py, PyAny>, part_off: CArr1<'py, i64>,
                                  ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>,
                                  lonlat: CArr2<'py, f64>, mask: CArr1<'py, bool>,
                                  countries: Vec<String>) -> PyResult<Vec<Option<String>>> {
    let (po, ro, co, mask) = (part_off.as_slice()?, ring_off.as_slice()?, coord_off.as_slice()?, mask.as_slice()?);
    let ll = lonlat.as_array();
    super::lanesplit::check_lonlat(ll.ncols())?;
    let countries: BTreeSet<String> = countries.into_iter().collect();
    let mut b = Borders { index, last: None };
    let first = |i: i64| -> PyResult<i64> {
        let p = po[np_index(po.len(), i)?];
        let r = ro[np_index(ro.len(), p)?];
        Ok(co[np_index(co.len(), r)?])
    };
    let mut out = vec![None; mask.len()];
    for (i, &m) in mask.iter().enumerate() {
        if !m {
            continue;
        }
        let (p0, p1) = (first(i as i64)?, first(i as i64 + 1)?);
        if p1 > p0 {
            // (p0 + (p1 - p0) // 2 of numpy int64s: wrapping, floor division; never a panic from map data)
            let k = np_index(ll.nrows(), p0.wrapping_add(p1.wrapping_sub(p0).div_euclid(2)))?;
            out[i] = country_for(&mut b, ll[[k, 0]], ll[[k, 1]], &countries)?;
        }
    }
    Ok(out)
}
