//! Preserve the order of pose side effects, including the Python services which still build
//! blocks and routes. A stale pose updates focus in the active frame, but keeps its source aid.
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyTuple};

use super::trackers::pose::{tracker_pose, traffic_pose};

type Obj<'py> = Bound<'py, PyAny>;

struct Pose<'a, 'py> {
    c: &'a Obj<'py>,
    s: Obj<'py>,
    msg: &'a Bound<'py, PyDict>,
    globals: &'a Bound<'py, PyDict>,
    clock: Obj<'py>,
    aid: Obj<'py>,
}

impl<'py> Pose<'_, 'py> {
    fn field(&self, key: &str) -> PyResult<Obj<'py>> {
        Ok(self
            .msg
            .get_item(key)?
            .unwrap_or_else(|| self.c.py().None().into_bound(self.c.py())))
    }
    fn global(&self, key: &str) -> PyResult<Obj<'py>> {
        self.globals.as_any().get_item(key)
    }
    fn threshold(&self, key: &str) -> PyResult<f64> {
        self.global(key)?.extract()
    }
    fn now(&self) -> PyResult<f64> {
        self.clock.call0()?.extract()
    }
    fn active(&self) -> PyResult<bool> {
        self.aid.eq(self.c.getattr("active")?)
    }
    fn number(&self, key: &str) -> PyResult<f64> {
        let v = self.field(key)?;
        if v.is_none() {
            return Ok(0.);
        }
        let n: f64 = v
            .call_method0("__float__")
            .or_else(|_| self.c.py().get_type::<PyFloat>().call1((&v,)))?
            .extract()?;
        if !n.is_finite() {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "{key} is not finite"
            )));
        }
        Ok(n)
    }
    fn float_attr(&self, obj: &Obj<'_>, name: &str, default: f64) -> PyResult<f64> {
        obj.getattr(name).map_or(Ok(default), |v| v.extract())
    }

    fn ownership(&self) -> PyResult<()> {
        self.c
            .call_method1("slots_gone", (self.field("slotsGone")?,))?;
        let held = self.field("held")?;
        let values = if let Ok(d) = held.cast::<PyDict>() {
            Some(d.values())
        } else {
            held.cast::<PyList>().ok().cloned()
        };
        if let Some(values) = values {
            let anchors = PySet::empty(self.c.py())?;
            for a in values {
                if (a.is_instance_of::<PyInt>() || a.is_instance_of::<PyFloat>())
                    && !a.is_instance_of::<PyBool>()
                {
                    anchors.add(a.call_method0("__int__")?)?;
                }
            }
            self.c.setattr("held", anchors)?;
        }
        self.s
            .call_method1("claim_sessions", (self.c, self.field("sessions")?))?;
        Ok(())
    }

    fn motion(&self, vx: f64, vy: f64) -> PyResult<()> {
        let c = self.c;
        let s = &self.s;
        c.setattr("vel", (vx, vy))?;
        let now = self.now()?;
        let active = c.getattr("active")?;
        let mut intent = c.getattr("__dict__")?.call_method1("get", ("intent",))?;
        if intent.is_none()
            || !c
                .getattr("intent_aid")
                .is_ok_and(|a| a.eq(&active).unwrap_or(false))
        {
            intent = self.global("reachmod")?.call_method0("Intent")?;
            c.setattr("intent", &intent)?;
            c.setattr("intent_aid", active)?;
        }
        intent.call_method1("feed", (now, vx, vy))?;
        let speed = crate::py_hypot(vx, vy);
        s.setattr("speed", speed)?;
        if speed > self.threshold("FAST_BASE_V")? {
            s.setattr("fast_at", now)?;
        }
        s.setattr(
            "fast",
            now - self.float_attr(s, "fast_at", -1e9)? < self.threshold("FAST_KEEP_S")?,
        )?;
        c.call_method1(
            "hold_boost",
            (self.field("guardHold")?.is(&PyBool::new(c.py(), true)),),
        )?;
        c.call_method1("catch_up", (speed,))?;
        if !s.getattr("boost_on")?.is_truthy()? || c.getattr("catchup")?.is_truthy()? {
            return Ok(());
        }
        let t0 = self.float_attr(c, "boost_t0", 0.)?;
        let since = self.now()? - t0;
        let has_map = !s.getattr("mapdata")?.is_none();
        let progress = || -> PyResult<bool> {
            let p = c.getattr("roads")?.call_method0("progress")?;
            p.get_item(0)?.ge(p.get_item(1)?)
        };
        let moving = speed > self.threshold("BOOST_MOVE_MS")?
            && since > self.threshold("BOOST_GRACE_S")?
            && (!has_map || progress()?);
        if s.getattr("boost_full")?.is_truthy()? {
            let done = !has_map || progress()?;
            let loaded = self.float_attr(c, "far_done_at", 0.)? > t0
                && !c.call_method0("far_loading")?.is_truthy()?;
            let timeout = since > self.threshold("BOOST_FULL_MAX_S")?;
            if (loaded && done) || timeout {
                s.call_method1(
                    "load_boost",
                    (false, if timeout { "timeout" } else { "loaded" }),
                )?;
            }
        } else if moving || since > self.threshold("BOOST_MAX_S")? {
            s.call_method1(
                "load_boost",
                (false, if moving { "moving" } else { "timeout" }),
            )?;
        }
        Ok(())
    }

    fn view(&self, x: f64, y: f64) -> PyResult<()> {
        let c = self.c;
        let py = c.py();
        let cam = self.field("cam")?;
        if cam.is_instance_of::<PyDict>() {
            c.setattr("cam", &cam)?;
            c.setattr("cam_aid", &self.aid)?;
        }
        let active = self.active()?;
        let view = if active {
            super::pose_view::pose_camera(
                &cam,
                &c.getattr("focus")?,
                self.threshold("VIEW_AWAY_M")?,
            )?
        } else {
            None
        };
        c.setattr("view_at", view)?;
        let foci = if active {
            super::pose_view::pose_foci(
                &self.field("foci")?,
                self.global("FOCI_MAX")?.extract()?,
                self.threshold("FOCI_RANGE_M")?,
                self.threshold("FOCI_R_MAX_M")?,
            )?
        } else {
            PyTuple::empty(py)
        };
        c.setattr("foci", foci)?;
        if self.s.getattr("horizon")?.is_truthy()? {
            let frame = self
                .s
                .getattr("anchors")?
                .get_item(&self.aid)?
                .getattr("frame")?;
            let (fx, fy) = view.unwrap_or((x, y));
            super::pose_view::pose_eye(c, &frame, fx, fy, self.msg, self.globals)?;
        }
        if let Some(hint) = c.getattr("focus_hint").ok().filter(|v| !v.is_none()) {
            let until = hint.get_item(4)?.extract::<f64>()?;
            if hint.get_item(0)?.eq(&self.aid)? && active && self.now()? < until {
                c.setattr("view_at", (hint.get_item(1)?, hint.get_item(2)?))?;
                c.setattr("eye", hint.get_item(3)?)?;
            } else if self.now()? >= until {
                c.setattr("focus_hint", py.None())?;
            }
        }
        Ok(())
    }

    fn map(&self) -> PyResult<bool> {
        let c = self.c;
        let py = c.py();
        c.setattr(
            "following",
            !self.field("follow")?.is(&PyBool::new(py, false)),
        )?;
        let view = self.field("view")?.is(&PyBool::new(py, true));
        let open = self.field("map")?.is(&PyBool::new(py, true));
        c.setattr("map_view", view)?;
        c.setattr("map_open", open)?;
        let split = c
            .getattr("map_split")
            .is_ok_and(|v| v.is_truthy().unwrap_or(false));
        if (view || open) && !split && self.now()? >= self.float_attr(c, "map_split_try", 0.)? {
            c.setattr("map_split_try", self.now()? + 2.)?;
            c.setattr(
                "map_split",
                self.s.call_method1("split_boost", (true, "3D map"))?,
            )?;
        } else if !(view || open) && split {
            c.setattr("map_split", false)?;
            self.s
                .call_method1("split_boost", (false, "3D map closed"))?;
        }
        Ok(view || open)
    }

    fn far(&self, map: bool) -> PyResult<bool> {
        let c = self.c;
        if map && c.getattr("eye")?.is_truthy()? {
            let eye = c.getattr("eye")?.extract::<f64>()?;
            let reference = c
                .getattr("zoom_ref")
                .ok()
                .filter(|v| !v.is_none())
                .map(|v| v.extract::<f64>())
                .transpose()?;
            match reference {
                Some(r) if eye <= r => {
                    if eye < r / self.threshold("MAP_ZOOM_RAMP_K")?
                        && self.float_attr(c, "lod_ramp", 1.)? >= 1.
                    {
                        c.setattr("lod_ramp", self.global("MAP_RAMP_START")?)?;
                        c.setattr("zoom_ref", eye)?;
                        self.global("log")?.call_method1(
                            "info",
                            ("3D map: zoomed in x%.0f, coarse first", r / eye),
                        )?;
                    }
                }
                _ => c.setattr("zoom_ref", eye)?,
            }
        }
        c.call_method0("_far")?.call_method1("pose", (c, &self.s))?;
        if c.getattr("teleport_pending")?.is_truthy()? {
            if self.now()? - c.getattr("teleport_t0")?.extract::<f64>()?
                <= self.threshold("TELEPORT_PAUSE_MAX_S")?
            {
                c.call_method0("refresh_status")?;
                return Ok(false);
            }
            c.setattr("teleport_pending", false)?;
            c.call_method0("unabandon_old")?;
            c.getattr("wake")?.call_method0("set")?;
        }
        Ok(true)
    }
}

#[pyfunction]
pub(super) fn connection_pose<'py>(
    c: &Obj<'py>,
    msg: &Bound<'py, PyDict>,
    globals: &Bound<'py, PyDict>,
) -> PyResult<()> {
    let py = c.py();
    let p = Pose {
        c,
        s: c.getattr("s")?,
        msg,
        globals,
        clock: globals.as_any().get_item("time")?.getattr("monotonic")?,
        aid: msg
            .get_item("anchorId")?
            .unwrap_or_else(|| py.None().into_bound(py)),
    };
    p.ownership()?;
    let lat = p.field("lat")?;
    let lon = p.field("lon")?;
    c.getattr("weather")?
        .call_method1("on_pose", (c, &lat, &lon))?;
    if let Some(feed) = p.s.getattr("feed").ok().filter(|v| !v.is_none()) {
        feed.call_method1(
            "publish",
            (
                &lat,
                &lon,
                p.field("speed")?,
                p.field("vx")?,
                p.field("vy")?,
                &p.aid,
            ),
        )?;
    }
    let numeric = |v: &Obj<'_>| v.is_instance_of::<PyInt>() || v.is_instance_of::<PyFloat>();
    if numeric(&lat) && numeric(&lon) {
        let ll = (lat.extract::<f64>()?, lon.extract::<f64>()?);
        c.setattr("pose_ll", ll)?;
        let surface = p.s.getattr("surface")?;
        if !surface.is_none() {
            surface.call_method1("prefetch", ll)?;
        }
    }
    let aid = &p.aid;
    let anchors = p.s.getattr("anchors")?;
    let active = c.getattr("active")?;
    if anchors.contains(aid)? && (active.is_none() || aid.gt(&active)?) {
        c.call_method1("commit_anchor", (aid,))?;
    }
    let (x, y, vx, vy) = (
        p.number("x")?,
        p.number("y")?,
        p.number("vx")?,
        p.number("vy")?,
    );
    if p.active()? {
        c.setattr("focus", (x, y))?;
        c.setattr("pz", p.number("z")?)?;
    } else if anchors.contains(aid)? && anchors.contains(c.getattr("active")?)? {
        let xyz = anchors
            .get_item(aid)?
            .getattr("frame")?
            .call_method1("enu_to_ecef", (x, y, 0.))?;
        let dest = anchors.get_item(c.getattr("active")?)?.getattr("frame")?;
        let v = dest.call_method1(
            "ecef_to_enu",
            (xyz.get_item(0)?, xyz.get_item(1)?, xyz.get_item(2)?),
        )?;
        c.setattr(
            "focus",
            (
                v.get_item(0)?.extract::<f64>()?,
                v.get_item(1)?.extract::<f64>()?,
            ),
        )?;
    } else {
        return Ok(());
    }
    p.motion(vx, vy)?;
    p.view(x, y)?;
    c.call_method1("_pose_ground", (aid, x, y, vx, vy, msg))?;
    let map = p.map()?;
    let tree = c.call_method1("_pose_blocks", (aid,))?;
    if !p.far(map)? {
        return Ok(());
    }
    c.call_method1("_pose_refresh", (aid, &tree))?;
    tracker_pose(
        c,
        aid,
        msg.as_any(),
        &tree,
        p.threshold("MAP_DETAIL_EYE_M")?,
        p.threshold("MAP_BUILDINGS_EYE_M")?,
    )?;
    c.call_method1("_pose_nav", (aid,))?;
    traffic_pose(c, aid)?;
    if p.active()? {
        c.call_method1("send_progress", (aid,))?;
    }
    c.call_method0("refresh_status")?;
    c.getattr("credits")?.call_method1("poke", (c,))?;
    Ok(())
}
