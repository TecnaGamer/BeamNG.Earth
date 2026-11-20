//! Connection-level ordering. A pass can complete jobs synchronously, so consult each
//! prerequisite after its tracker has run. Navigation still runs between scenery and traffic.
use super::*;

struct Pose<'py> {
    conn: Obj<'py>,
    server: Obj<'py>,
    aid: Obj<'py>,
    pos: Point,
}
impl<'py> Pose<'py> {
    fn new(conn: &Obj<'py>, aid: &Obj<'py>) -> PyResult<Self> {
        Ok(Self {
            conn: conn.clone(),
            server: conn.getattr("s")?,
            aid: aid.clone(),
            pos: conn.getattr("focus")?.extract()?,
        })
    }
    fn frame(&self) -> PyResult<Obj<'py>> {
        let a = self.server.getattr("anchors")?.call_method1("get", (&self.aid,))?;
        if a.is_none() {
            Ok(a)
        } else {
            a.getattr("frame")
        }
    }
    fn run(&self, tr: &Obj<'py>, build: Obj<'py>, kw: &Bound<'py, PyDict>) -> PyResult<()> {
        let lp = tr.py().import("asyncio")?.call_method0("get_running_loop")?;
        tr.call_method(
            "on_pose",
            (&self.conn, &self.aid, self.pos.0, self.pos.1, lp, build),
            Some(kw),
        )?;
        Ok(())
    }
    fn view_frame(&self) -> PyResult<Bound<'py, PyDict>> {
        let kw = PyDict::new(self.conn.py());
        kw.set_item("view", self.conn.getattr("view_at")?)?;
        kw.set_item("frame", self.frame()?)?;
        Ok(kw)
    }
    fn roads(&self, msg: &Obj<'py>, tree: &Obj<'py>) -> PyResult<()> {
        let py = self.conn.py();
        let tr = self.conn.getattr("roads")?;
        let kw = PyDict::new(py);
        kw.set_item("vel", self.conn.getattr("vel")?)?;
        kw.set_item("ground_busy", self.conn.getattr("terrain_wait")?.gt(0)?)?;
        kw.set_item("view", self.conn.getattr("view_at")?)?;
        kw.set_item("foci", self.conn.getattr("foci")?)?;
        let ready = if tr.getattr("geo").is_ok_and(|v| v.is_truthy().unwrap_or(false)) {
            let frame = self.frame()?;
            tr.setattr(
                "deck_first",
                py.import("deckspans")?
                    .call_method1("wait_keys", (msg.call_method1("get", ("deckWait",))?,))?,
            )?;
            kw.set_item("frame", &frame)?;
            kw.set_item("tree", tree)?;
            self.run(&tr, self.server.getattr("build_roads_geo")?, &kw)?;
            if frame.is_none() {
                PySet::empty(py)?.into_any()
            } else {
                tr.call_method1(
                    "ready_tiles",
                    (&frame, self.pos.0, self.pos.1, py.import("roads")?.getattr("SEND_M")?),
                )?
            }
        } else {
            self.run(&tr, self.server.getattr("build_roads_tile")?, &kw)?;
            tr.getattr("sent")?
        };
        self.server.call_method1(
            "osm_fuel_want",
            (
                &self.conn,
                &self.aid,
                tr.getattr("car")
                    .unwrap_or_else(|_| pyo3::types::PyTuple::empty(py).into_any()),
            ),
        )?;
        if let Ok(src) = self.server.getattr("mapdata")?.getattr("highway_signs") {
            if !src.is_none() && self.conn.getattr("signs_on")?.is_truthy()? {
                let kw = PyDict::new(py);
                kw.set_item("version", src.getattr("tags")?.call_method0("known")?)?;
                let lp = py.import("asyncio")?.call_method0("get_running_loop")?;
                self.conn.getattr("signs")?.call_method(
                    "on_pose",
                    (
                        &self.conn,
                        &self.aid,
                        self.pos.0,
                        self.pos.1,
                        lp,
                        self.server.getattr("build_signs_tile")?,
                        ready,
                    ),
                    Some(&kw),
                )?;
            }
        }
        Ok(())
    }

    fn buildings(&self) -> PyResult<()> {
        self.conn.call_method0("building_reach")?;
        let geo = self.conn.getattr("far_geo")?.is_truthy()?;
        let kw = self.view_frame()?;
        let run = |name: &str, anchor_builder: Option<&str>| -> PyResult<()> {
            let builder = match anchor_builder {
                Some(name) if !geo => self.server.getattr(name)?,
                _ => self.conn.call_method1("build_far_geo", (name,))?,
            };
            self.run(&self.conn.getattr(name)?, builder, &kw)
        };
        run("bldfar", Some("build_far_bld_tile"))?;
        if self.conn.call_method0("far_first_pending")?.is_truthy()? {
            return Ok(());
        }
        let packs = self.conn.getattr("bldpack")?;
        if !packs.is_none() {
            run("bldpack", None)?;
        }
        let far = self.conn.getattr("bldfar")?;
        let near_done = complete(&far)? && (packs.is_none() || complete(&packs)?);
        let sky = self.conn.getattr("bldsky")?;
        if !sky.is_none() && near_done {
            run("bldsky", Some("build_sky_tile"))?;
            let hz = self.conn.getattr("bldhz")?;
            if !hz.is_none() && hz.getattr("send_m")?.gt(0)? && complete(&sky)? {
                run("bldhz", None)?;
            }
        }
        if !self.conn.getattr("brfar")?.is_none() && complete(&far)? {
            run("brfar", Some("build_far_bridge_tile"))?;
        }
        Ok(())
    }
}

fn complete(tr: &Obj<'_>) -> PyResult<bool> {
    let (done, want): (usize, usize) = tr.call_method0("progress")?.extract()?;
    Ok(done >= want)
}

#[pyfunction]
pub(super) fn tracker_pose<'py>(
    conn: &Obj<'py>,
    aid: &Obj<'py>,
    msg: &Obj<'py>,
    tree: &Obj<'py>,
    detail_eye: f64,
    building_eye: f64,
) -> PyResult<()> {
    if !aid.eq(conn.getattr("active")?)? {
        return Ok(());
    }
    let map = conn.getattr("map_view")?.is_truthy()?;
    let p = Pose::new(conn, aid)?;
    if !map || conn.call_method0("eye_clearance")?.le(detail_eye)? {
        if !p.server.getattr("mapdata")?.is_none() {
            p.roads(msg, tree)?;
        }
        if conn.getattr("turbine_geo")?.is_truthy()? {
            let kw = p.view_frame()?;
            if kw.get_item("frame")?.is_some_and(|fr| !fr.is_none()) {
                p.run(&conn.getattr("turbines")?, p.server.getattr("build_turbines_geo")?, &kw)?;
            }
        }
        let service = p.server.getattr("veg")?;
        if !service.is_none() {
            let frame = p.frame()?;
            if !frame.is_none() {
                let ll = frame.call_method1("enu_to_geodetic", (p.pos.0, p.pos.1, 0.))?;
                service.call_method1(
                    "warm_around",
                    (ll.get_item(0)?.extract::<f64>()?, ll.get_item(1)?.extract::<f64>()?),
                )?;
            }
            let tr = conn.getattr("veg")?;
            let builder = if tr.getattr("geo")?.is_truthy()? {
                "build_veg_geo"
            } else {
                "build_veg_tile"
            };
            let foci = pyo3::types::PyList::empty(conn.py());
            for f in conn.getattr("foci")?.try_iter()? {
                let f = f?;
                if f.get_item(3)?.is_truthy()? {
                    foci.append(f)?;
                }
            }
            let kw = PyDict::new(conn.py());
            kw.set_item("view", conn.getattr("view_at")?)?;
            kw.set_item("frame", frame)?;
            kw.set_item("tree", tree)?;
            kw.set_item("foci", foci)?;
            p.run(&tr, conn.getattr(builder)?, &kw)?;
        }
    }
    if (!map || conn.call_method0("eye_clearance")?.le(building_eye)?) && p.server.getattr("buildings")?.is_truthy()? {
        p.buildings()?;
    }
    Ok(())
}

#[pyfunction]
pub(super) fn traffic_pose<'py>(conn: &Obj<'py>, aid: &Obj<'py>) -> PyResult<()> {
    if !conn.getattr("traffic_on")?.is_truthy()?
        || !aid.eq(conn.getattr("active")?)?
        || conn.getattr("map_view")?.is_truthy()?
        || !conn.call_method1("nav_may_start", (aid,))?.is_truthy()?
    {
        return Ok(());
    }
    let p = Pose::new(conn, aid)?;
    let kw = p.view_frame()?;
    let tr = conn.getattr("distant_traffic")?;
    let build = p.server.getattr("build_traffic_geo")?;
    p.run(&tr, build.clone(), &kw)?;
    let (done, want): (usize, usize) = tr.call_method0("progress")?.extract()?;
    if done >= want.div_ceil(2) {
        let far = PyDict::new(conn.py());
        far.set_item("far", true)?;
        let build = conn
            .py()
            .import("functools")?
            .call_method("partial", (build,), Some(&far))?;
        p.run(&conn.getattr("distant_traffic_far")?, build, &kw)?;
    }
    Ok(())
}
