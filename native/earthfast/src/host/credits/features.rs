//! Feature attribution uses the same provider provenance and optional offline inventories as tile builders.
use super::{strings, Obj};
use crate::dm::Dm;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use std::collections::BTreeSet;

const RANGE: f64 = 1500.;
type Ids = BTreeSet<String>;

fn attr<'py>(obj: &Obj<'py>, name: &str) -> Obj<'py> {
    obj.getattr(name)
        .unwrap_or_else(|_| obj.py().None().into_bound(obj.py()))
}

struct Sources<'py> {
    server: Obj<'py>,
    active: Obj<'py>,
    focus: (f64, f64),
    roads: bool,
    trees: bool,
    veg: bool,
    surface: bool,
    sea: bool,
    gravity: Obj<'py>,
    airports: Obj<'py>,
}

impl<'py> Sources<'py> {
    fn of(c: &Obj<'py>) -> PyResult<Self> {
        let server = c.getattr("s")?;
        let active = c.getattr("active")?;
        let shown = |name| -> PyResult<bool> {
            let tracker = attr(c, name);
            Ok(attr(&tracker, "aid").eq(&active)? && attr(&tracker, "sent").is_truthy()?)
        };
        let sea = attr(c, "sea_sent");
        let sea = sea.is_truthy()?
            && sea.get_item(0)?.eq(&active)?
            && sea.get_item(1)?.is_truthy()?
            && sea.get_item(1)?.get_item(0)?.is_truthy()?;
        let g = attr(&attr(c, "gravity"), "msg");
        let gravity = if let Ok(d) = g.cast::<PyDict>() {
            d.call_method1("get", ("source",))?
        } else {
            false.into_pyobject(c.py())?.to_owned().into_any()
        };
        Ok(Self {
            focus: c.getattr("focus")?.extract()?,
            roads: shown("roads")?,
            trees: shown("veg")?,
            veg: !attr(&server, "veg").is_none(),
            surface: !attr(&server, "surface").is_none(),
            sea,
            gravity,
            airports: attr(&server, "airports"),
            server,
            active,
        })
    }

    fn key(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        (
            &self.active,
            self.roads,
            self.trees,
            self.veg,
            self.surface,
            self.sea,
            &self.gravity,
            !self.airports.is_none(),
        )
            .into_pyobject(py)
    }

    fn ids(&self, py: Python<'py>) -> PyResult<Ids> {
        let mut ids = Ids::new();
        let anchors = attr(&self.server, "anchors");
        if anchors.is_none() {
            return Ok(ids);
        }
        let anchor = anchors.call_method1("get", (&self.active,))?;
        if anchor.is_none() {
            return Ok(ids);
        }
        let ll = anchor
            .getattr("frame")?
            .call_method1("enu_to_geodetic", (self.focus.0, self.focus.1, 0.))?;
        let (lat, lon) = (
            ll.get_item(0)?.extract::<f64>()?,
            ll.get_item(1)?.extract::<f64>()?,
        );
        let md = attr(&self.server, "mapdata");
        let roads = !md.is_none() && self.roads;
        let veg = self.veg && self.trees;
        if md.hasattr("layer_provenance")? {
            let dlat = RANGE / 110540.;
            let dlon = RANGE / (111320. * lat.to_radians().dcos().max(0.1));
            let cells = py.import("mapcells")?.call_method1(
                "cells_for_bbox",
                (lat - dlat, lon - dlon, lat + dlat, lon + dlon),
            )?;
            for cell in cells.try_iter()? {
                let cell = cell?;
                for (layer, enabled) in [
                    ("roads", roads),
                    ("buildings", roads),
                    ("water", roads),
                    ("landcover", self.surface),
                    ("canopy", veg),
                ] {
                    if enabled {
                        let provenance = md.call_method1("layer_provenance", (&cell, layer))?;
                        if let Ok(d) = provenance.cast::<PyDict>() {
                            add_provenance(&mut ids, d)?;
                        }
                    }
                }
            }
        }
        if self.sea {
            ids.insert("sea:noaa".into());
        }
        if roads {
            if let Ok(bt) = py.import("bridgetypes") {
                if let Ok(n) = bt
                    .call_method1("nbi_near", (lat, lon, RANGE))
                    .and_then(|a| a.get_item(0)?.len())
                {
                    if n > 0 {
                        ids.insert("bridge:nbi".into());
                    }
                    if bt
                        .call_method1("wd_near", (lat, lon, RANGE))
                        .and_then(|v| v.is_truthy())
                        .unwrap_or(false)
                    {
                        ids.insert("bridge:wikidata".into());
                    }
                }
            }
            let roofs = attr(&md, "lod2_roofs");
            let cache = attr(&md, "cache_dir");
            if !roofs.is_none() {
                let dir = attr(&roofs, "cache_dir");
                let local = attr(&roofs, "local");
                if dir.is_truthy()? || local.is_truthy()? {
                    if let Ok(found) = py
                        .import("lod2roofs")
                        .and_then(|m| {
                            m.call_method1("credited_near", (dir, lat, lon, RANGE, local))
                        })
                        .and_then(|a| strings(&a))
                    {
                        ids.extend(found);
                    }
                }
            }
            for (module, func, id) in [
                ("threedbag", "credited_near", "roof:3dbag"),
                ("nycmassing", "credited_near", "bld:nyc-3d"),
                ("bdtopo", "credited_near", "bld:bdtopo-fr"),
                ("roadz", "hk_credited_near", "roadz:hk"),
            ] {
                if module == "bdtopo" && attr(&md, "bdtopo").is_none() {
                    continue;
                }
                if py
                    .import(module)
                    .and_then(|m| m.call_method1(func, (&cache, lat, lon, RANGE)))
                    .and_then(|v| v.is_truthy())
                    .unwrap_or(false)
                {
                    ids.insert(id.into());
                }
            }
            if py
                .import("threedmr")
                .and_then(|m| {
                    m.call_method1(
                        "credited_near",
                        (lat, lon, RANGE, attr(&self.server, "root")),
                    )
                })
                .and_then(|v| v.is_truthy())
                .unwrap_or(false)
            {
                ids.insert("model:3dmr".into());
            }
        }
        let db = attr(&self.airports, "db");
        if !db.is_none()
            && db
                .call_method1("near", (lat, lon, RANGE))
                .and_then(|v| v.is_truthy())
                .unwrap_or(false)
        {
            ids.insert("air:ourairports".into());
        }
        if self.gravity.eq("egm2008")? {
            ids.insert("grav:egm2008".into());
        }
        if veg {
            ids.insert("climate:beck-kg".into());
        }
        Ok(ids)
    }
}

fn add_provenance(ids: &mut Ids, d: &Bound<'_, PyDict>) -> PyResult<()> {
    let id = d.call_method1("get", ("id", ""))?.extract::<String>()?;
    match id.as_str() {
        "esa-worldcover" => {
            ids.insert("land:esa-worldcover".into());
        }
        "meta-wri-chm" | "lidarbc-chm" => {
            ids.insert("canopy:meta-wri-chm".into());
            if id == "lidarbc-chm" {
                ids.insert("canopy:lidarbc-chm".into());
            }
        }
        _ if id.starts_with("overture:") => {
            if let Some(rows) = d.get_item("datasets")?.filter(|v| !v.is_none()) {
                for row in rows.try_iter()? {
                    let row = row?;
                    if row.call_method1("get", ("features", 1))?.gt(0)? {
                        ids.insert("map:Overture".into());
                        let name = row.call_method1("get", ("dataset",))?;
                        if name.is_truthy()? {
                            ids.insert(format!("map:{name}"));
                        }
                    }
                }
            }
        }
        _ => (),
    }
    Ok(())
}

#[pyfunction]
pub(super) fn credit_features(py: Python<'_>, conn: &Obj<'_>) -> PyResult<Ids> {
    Sources::of(conn)?.ids(py)
}

pub(super) fn kept(py: Python<'_>, conn: &Obj<'_>) -> PyResult<Ids> {
    let s = Sources::of(conn)?;
    let key = s.key(py)?;
    let now: f64 = py.import("time")?.call_method0("monotonic")?.extract()?;
    let prev = attr(conn, "_feature_kept");
    if !prev.is_none()
        && prev.get_item(0)?.eq(&key)?
        && now - prev.get_item(2)?.extract::<f64>()? < 30.
    {
        let (x, y): (f64, f64) = prev.get_item(1)?.extract()?;
        if crate::py_hypot(s.focus.0 - x, s.focus.1 - y) < 100. {
            return prev.get_item(3)?.extract();
        }
    }
    let ids = s.ids(py)?;
    conn.setattr("_feature_kept", (key, s.focus, now, &ids))?;
    Ok(ids)
}
