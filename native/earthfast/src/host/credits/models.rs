//! Corrected, offline positions of the cache's info files. Directory changes refresh
//! membership. A partially written info is retried even if the directory mtime stands still.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;

use crate::dm::Dm;
use pyo3::prelude::*;
use pyo3::sync::MutexExt;
use pyo3::types::PyDict;

#[derive(Default)]
#[pyclass(frozen, module = "earthfast")]
struct ModelPositions {
    index: Mutex<ModelIndex>,
}

#[derive(Default)]
struct ModelIndex {
    modified: Option<SystemTime>,
    by_name: BTreeMap<String, (f64, f64)>,
    by_lat: Vec<(f64, f64)>,
}

impl ModelIndex {
    fn refresh(
        &mut self,
        directory: &Path,
        stamp: Option<SystemTime>,
        resolve: &Bound<'_, PyAny>,
        cache: &Path,
    ) -> bool {
        let Ok(entries) = directory.read_dir() else {
            self.modified = None;
            return false;
        };
        let mut complete = true;
        let mut positions = Vec::new();
        for entry in entries {
            let Ok(entry) = entry else {
                complete = false;
                continue;
            };
            let name = entry.file_name();
            let Some(name) = name.to_str() else { continue };
            if !name.starts_with("info_") || !name.ends_with(".json") {
                continue;
            }
            let point = match self.by_name.get(name) {
                Some(&p) => p,
                None => {
                    // model_position also knows the shipped overrides, survey fixes and cached OSM rings.
                    let Ok(pos) = resolve
                        .call1((entry.path(), cache, &name[5..name.len() - 5]))
                        .and_then(|v| v.extract::<(f64, f64)>())
                    else {
                        complete = false;
                        continue;
                    };
                    self.by_name.insert(name.to_owned(), pos);
                    pos
                }
            };
            if point.0.is_finite() && point.1.is_finite() {
                positions.push(point);
            }
        }
        positions.sort_by(|a, b| a.0.total_cmp(&b.0));
        self.by_lat = positions;
        self.modified = if complete { stamp } else { None };
        true
    }

    fn near(&self, lat: f64, lon: f64, radius: f64) -> bool {
        let north = |y: f64| (y - lat) * 111132.;
        let start = self.by_lat.partition_point(|&(y, _)| north(y) < -radius);
        for &(y, x) in &self.by_lat[start..] {
            let dn = north(y);
            if dn > radius {
                break;
            }
            let de = (x - lon) * 111320. * ((lat + y) / 2.).to_radians().dcos();
            if crate::py_hypot(de, dn) <= radius {
                return true;
            }
        }
        false
    }
}

#[pyfunction]
pub(super) fn credited_model_near(
    py: Python<'_>,
    memo: &Bound<'_, PyDict>,
    lat: f64,
    lon: f64,
    radius: f64,
    cache: PathBuf,
    resolve: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let dir = cache.join("threedmr_cache");
    let index = match memo.get_item(&dir)? {
        Some(i) => i.cast_into::<ModelPositions>()?,
        None => {
            let i = Bound::new(py, ModelPositions::default())?;
            memo.set_item(&dir, &i)?;
            i
        }
    };
    let mut state = index.get().index.lock_py_attached(py).unwrap();
    let stamp = dir.metadata().and_then(|md| md.modified()).ok();
    if (stamp.is_none() || stamp != state.modified) && !state.refresh(&dir, stamp, resolve, &cache)
    {
        return Ok(false);
    }
    Ok(state.near(lat, lon, radius))
}
