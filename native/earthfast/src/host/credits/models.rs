//! Corrected, offline positions of the cache's current info files.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

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
    by_name: BTreeMap<String, CachedPosition>,
    by_lat: Vec<(f64, f64)>,
}

struct CachedPosition {
    seen: bool,
    info: Vec<u8>,
    point: (f64, f64),
}

impl ModelIndex {
    fn refresh(&mut self, directory: &Path, resolve: &Bound<'_, PyAny>, cache: &Path) {
        for cached in self.by_name.values_mut() {
            cached.seen = false;
        }
        let mut changed = false;
        if let Ok(entries) = directory.read_dir() {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let Some(name) = name.to_str() else { continue };
                let Some(id) = name
                    .strip_prefix("info_")
                    .and_then(|s| s.strip_suffix(".json"))
                else {
                    continue;
                };
                // These small info records can be rewritten with the same size and timestamp.
                let path = entry.path();
                let Ok(info) = std::fs::read(&path) else {
                    continue;
                };
                if let Some(cached) = self.by_name.get_mut(name) {
                    if cached.info == info {
                        cached.seen = true;
                        continue;
                    }
                }
                // The resolver also applies shipped overrides, survey fixes and cached OSM rings.
                // Leave incomplete records uncached so the next query retries them.
                if let Ok(point) = resolve
                    .call1((path, cache, id))
                    .and_then(|v| v.extract::<(f64, f64)>())
                {
                    if point.0.is_finite() && point.1.is_finite() {
                        self.by_name.insert(
                            name.to_owned(),
                            CachedPosition {
                                seen: true,
                                info,
                                point,
                            },
                        );
                        changed = true;
                    }
                }
            }
        }
        let before = self.by_name.len();
        self.by_name.retain(|_, cached| cached.seen);
        if changed || self.by_name.len() != before {
            self.by_lat.clear();
            self.by_lat
                .extend(self.by_name.values().map(|entry| entry.point));
            self.by_lat.sort_by(|a, b| a.0.total_cmp(&b.0));
        }
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
    state.refresh(&dir, resolve, &cache);
    Ok(state.near(lat, lon, radius))
}
