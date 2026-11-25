//! Prepare the process worker's lookup specs and publish each completed record with its original tile metadata.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PyString};
use std::path::PathBuf;

type Obj<'py> = Bound<'py, PyAny>;
pub(super) struct Tile<'py> {
    tile: Obj<'py>,
    ck: Obj<'py>,
    texture: String,
    zoom: Obj<'py>,
    global_tex: Option<Obj<'py>>,
}

fn count(why: &Bound<'_, PyDict>, reason: &str) -> PyResult<()> {
    let n = why
        .get_item(reason)?
        .map(|v| v.extract::<usize>())
        .transpose()?
        .unwrap_or(0);
    why.set_item(reason, n + 1)
}

pub(super) struct Restore<'a, 'py> {
    pub server: &'a Obj<'py>,
    pub anchor: &'a Obj<'py>,
    pub globals: &'a Bound<'py, PyDict>,
    pub name_len: usize,
    pub fin: Obj<'py>,
}

impl<'py> Restore<'_, 'py> {
    pub fn prepare(
        &self,
        chunk: &Bound<'py, PyList>,
    ) -> PyResult<(Bound<'py, PyList>, Vec<Tile<'py>>, Bound<'py, PyDict>)> {
        let s = self.server;
        let py = s.py();
        let g = self.globals.as_any();
        let anchor = self.anchor;
        let im = s.getattr("imagery")?;
        let layout = s.getattr("layout")?;
        let ff = g.get_item("farfield")?;
        let revisit = g.get_item("revisitmod")?;
        let cache = s.getattr("cache_dir")?;
        let out_dir: PathBuf = s.getattr("out_dir")?.extract()?;
        let max_px: usize = g.get_item("farpvsmod")?.getattr("UP_PX_MAX")?.extract()?;
        let min_px: usize = g.get_item("farrestoremod")?.getattr("MIN_PX")?.extract()?;
        let specs = PyList::empty(py);
        let mut prep = vec![];
        let why = PyDict::new(py);
        for tile in chunk {
            let ck = s.call_method1("far_ck", (anchor.getattr("id")?, tile.getattr("key")?))?;
            let px: usize = s.call_method1("tile_tex_px", (anchor, &tile))?.extract()?;
            let mut pxs = vec![px];
            pxs.extend((1..=2).map(|k| px << k).filter(|&p| p <= max_px));
            pxs.extend((1..=3).map(|k| px >> k).filter(|&p| p >= min_px));
            let rev = s.getattr("tex_rev")?.call_method1("get", (&ck, 0))?;
            // the snow its texture is keyed with (Server.far_snow: None while winter is off; 625)
            let snow = s.call_method1("far_snow", (anchor, &tile))?;
            let texture: String = s
                .call_method1("texture_name", (ck.get_item(0)?, ck.get_item(1)?, rev))?
                .extract()?;
            let (zoom, global_tex) = if tile.is_instance(&ff.getattr("GTile")?)? {
                let z: i64 = tile.getattr("z")?.extract()?;
                let cells = layout.getattr("cells")?;
                let zoom = ff.call_method1("global_dem_zoom", (z, &cells))?;
                let reference = ff.call_method1("global_dem_zoom", (z + 1, cells))?;
                let mut found = None;
                for p in pxs {
                    let k = s.call_method1("tex_key", (anchor, &tile, p, &snow))?;
                    let v = revisit.call_method1("load", (&cache, "fartex", k))?;
                    if !v.is_none()
                        && v.get_item(0)?
                            .call_method1("get", ("tex.dds",))?
                            .is_instance(&revisit.getattr("Side")?)?
                    {
                        found = Some((v, p));
                        break;
                    }
                }
                let Some((v, p)) = found else {
                    count(&why, "global texture")?;
                    continue;
                };
                specs.append((
                    "global",
                    z,
                    tile.getattr("x")?,
                    tile.getattr("y")?,
                    &zoom,
                    reference,
                    &layout,
                    vec![p],
                    self.name_len,
                ))?;
                (zoom, Some(v))
            } else {
                let level: i64 = tile.getattr("level")?.extract()?;
                let zooms = anchor.getattr("zooms")?;
                let zoom = zooms.get_item(level.max(0))?;
                let q = if level <= 0 {
                    s.call_method1("anchor_q", (anchor,))?
                } else {
                    py.None().into_bound(py)
                };
                let reference = if level > 0 {
                    zooms.get_item(level - 1)?
                } else {
                    py.None().into_bound(py)
                };
                let tex = PyDict::new(py);
                tex.set_item("path", out_dir.join(&texture).as_os_str())?;
                tex.set_item("px", px)?;
                if s.getattr("road_masks")?.is_truthy()? {
                    let mask: String = g
                        .get_item("surfacemod")?
                        .call_method1("road_mask_name", (&texture,))?
                        .extract()?;
                    tex.set_item(
                        "mask",
                        (
                            out_dir.join(mask).as_os_str(),
                            s.call_method1("road_mask_px", (level,))?,
                        ),
                    )?;
                } else {
                    tex.set_item("mask", py.None())?;
                }
                let chain = PyList::empty(py);
                for src in im.getattr("chain")?.try_iter()? {
                    chain.append(src?.getattr("name")?)?;
                }
                tex.set_item("chain", chain)?;
                tex.set_item("tone", s.getattr("tone")?)?;
                tex.set_item("leaf_on", im.getattr("leaf_on")?)?;
                tex.set_item("season_mode", im.getattr("season_mode")?)?;
                tex.set_item("snow", &snow)?;
                tex.set_item("keep_fin", self.name_len)?;
                tex.call_method1("update", (s.call_method0("gen_far_tex")?,))?;
                if !self.fin.is_none() {
                    tex.set_item("fin", &self.fin)?;
                }
                specs.append((
                    "tile",
                    &tile,
                    &zoom,
                    q,
                    reference,
                    g.get_item("BLOCK_DRAWN")?
                        .contains(s.getattr("near_visual")?)?,
                    &layout,
                    tex,
                    pxs,
                ))?;
                (zoom, None)
            };
            prep.push(Tile {
                tile,
                ck,
                texture,
                zoom,
                global_tex,
            });
        }
        Ok((specs, prep, why))
    }

    pub fn write(
        &self,
        prep: Vec<Tile<'py>>,
        found: &Obj<'py>,
        why: &Bound<'py, PyDict>,
        t0: f64,
    ) -> PyResult<(Bound<'py, PyDict>, f64)> {
        let s = self.server;
        let py = s.py();
        let g = self.globals.as_any();
        let staging = g.get_item("staging")?;
        let tmp = staging.getattr("tmp_for")?;
        let farrestore = g.get_item("farrestoremod")?;
        let placeholder = farrestore.call_method1("placeholder", (self.name_len,))?;
        let out_dir: PathBuf = s.getattr("out_dir")?.extract()?;
        let items = PyList::empty(py);
        let mut metadata = vec![];
        for (t, got) in prep.into_iter().zip(found.try_iter()?) {
            let got = got?;
            if got.is_none() {
                let reason = if t.global_tex.is_some() {
                    "global shape".into()
                } else {
                    format!("L{} shape", t.tile.getattr("level")?)
                };
                count(why, &reason)?;
                continue;
            }
            let sides = if let Some(tex) = &t.global_tex {
                let sides = PyDict::new(py);
                sides.set_item(
                    "tex.dds",
                    tex.get_item(0)?.get_item("tex.dds")?.getattr("path")?,
                )?;
                sides
            } else {
                got.get_item(1)?.cast_into::<PyDict>()?
            };
            let Some(tex_src) = sides.get_item("tex.dds")? else {
                continue;
            };
            let material = s.getattr("matslots")?.call_method1("take", (&t.ck,))?;
            if material.len()? != self.name_len {
                continue;
            }
            let side_list = PyList::empty(py);
            let add_side = |src: Obj<'py>, path: PathBuf| -> PyResult<()> {
                side_list.append((src, tmp.call1((path.as_os_str(),))?, path.as_os_str()))
            };
            add_side(tex_src, out_dir.join(&t.texture))?;
            if t.global_tex.is_none() && s.getattr("road_masks")?.is_truthy()? {
                if let Some(mask) = sides.get_item("mask.dds")? {
                    if s.call_method1("road_mask_px", (t.tile.getattr("level")?,))?
                        .is_truthy()?
                    {
                        let name: String = g
                            .get_item("surfacemod")?
                            .call_method1("road_mask_name", (&t.texture,))?
                            .extract()?;
                        add_side(mask, out_dir.join(name))?;
                    }
                }
            }
            let name: String = s
                .call_method1("tile_name", (t.ck.get_item(0)?, t.ck.get_item(1)?))?
                .extract()?;
            let shape = out_dir.join(name);
            let slot_name = s.getattr("matslots")?.call_method1("file", (&material,))?;
            let slot = out_dir.join(slot_name.extract::<String>()?);
            items.append((
                got.get_item(0)?,
                &placeholder,
                &material,
                (
                    tmp.call1((shape.as_os_str(),))?,
                    shape.as_os_str(),
                    tmp.call1((slot.as_os_str(),))?,
                    slot.as_os_str(),
                ),
                side_list,
            ))?;
            metadata.push((t, got.get_item(2)?, material, slot_name, sides));
        }
        let results = if items.is_empty() {
            PyList::empty(py).into_any()
        } else {
            farrestore.call_method1("restore", (items, staging.getattr("WARM_READ")?))?
        };
        let t2 = py
            .import("time")?
            .call_method0("perf_counter")?
            .extract::<f64>()?;
        let infos = PyDict::new(py);
        for ((t, px, material, shape, sides), r) in metadata.into_iter().zip(results.try_iter()?) {
            let r = r?;
            if r.is_instance_of::<PyString>() {
                g.get_item("log")?
                    .call_method1("debug", ("far restore: %s: %s", t.tile.getattr("key")?, r))?;
                continue;
            }
            let extra = r.get_item(1)?;
            let required = extra.call_method1("get", ("sides",))?;
            if required.is_truthy()? {
                let mut missing = false;
                for side in required.try_iter()? {
                    if !sides.contains(side?)? {
                        missing = true;
                        break;
                    }
                }
                if missing {
                    continue;
                }
            }
            let kw = PyDict::new(py);
            let photo = if let Some(tex) = &t.global_tex {
                let photo = tex.get_item(1)?;
                if !s.getattr("colormatch")?.is_none() || !s.getattr("deshadow")?.is_none() {
                    let pending = g.get_item("kept_pending")?.call1((&photo,))?;
                    kw.set_item(
                        "cm_pending",
                        py.get_type::<PyFrozenSet>().call1((pending,))?,
                    )?;
                }
                let tile = &t.tile;
                kw.set_item(
                    "frame",
                    g.get_item("farfield")?.call_method1(
                        "global_frame_ll",
                        (tile.getattr("z")?, tile.getattr("x")?, tile.getattr("y")?),
                    )?,
                )?;
                photo
            } else {
                extra.clone()
            };
            if !s.getattr("credits")?.is_none() {
                let loc = s.call_method1("tile_where", (self.anchor, &t.tile))?;
                let (x, y, sz) = (
                    loc.get_item(1)?.extract::<f64>()?,
                    loc.get_item(2)?.extract::<f64>()?,
                    loc.get_item(3)?.extract::<f64>()?,
                );
                kw.set_item(
                    "credits",
                    g.get_item("creditsmod")?.call_method1(
                        "tile_sources",
                        (
                            loc.get_item(0)?,
                            (x, y, x + sz, y + sz),
                            s.getattr("dem")?,
                            &t.zoom,
                            photo.get_item("used")?,
                        ),
                    )?,
                )?;
            }
            kw.set_item("tile", &t.tile)?;
            kw.set_item("cliff_colors", extra.call_method1("get", ("cliff_colors", false))?)?;
            kw.set_item("tex_px", px)?;
            kw.set_item("shape", shape)?;
            kw.set_item(
                "rev",
                s.getattr("tex_rev")?.call_method1("get", (&t.ck, 0))?,
            )?;
            kw.set_item("err_n", extra.call_method1("get", ("err_n",))?)?;
            let pos = py.get_type::<PyList>().call1((extra.get_item("pos")?,))?;
            let info = g.get_item("TileInfo")?.call(
                (
                    pos,
                    extra.get_item("err")?.extract::<f64>()?,
                    extra.get_item("sink")?,
                    extra.get_item("skirt")?.extract::<f64>()?,
                    r.get_item(2)?,
                    r.get_item(0)?,
                    t2 - t0,
                    material,
                    t.texture,
                    py.get_type::<pyo3::types::PyInt>()
                        .call1((photo.get_item("tex_bytes")?,))?,
                    0.,
                ),
                Some(&kw),
            )?;
            s.getattr("degraded")?.call_method1("discard", (&t.ck,))?;
            infos.set_item(t.ck, info)?;
        }
        Ok((infos, t2))
    }
}
