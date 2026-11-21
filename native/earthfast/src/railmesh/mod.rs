//! Rail tile assembly. The profile and crossing providers are shared with the terrain grader.
//! Read ground in the same batches, keep original part indices across retries, and publish
//! furniture and crossing ownership only after accepting the mesh.
mod furniture;
mod mesh;
mod obstacles;
mod profile;
mod sections;
use crate::busstation::{array, points, rows, Point};
use mesh::{at, RailMesh};
use numpy::IntoPyArray;
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyDict, PyFrozenSet, PyList, PySlice, PyTuple},
};
use sections::Run;
use std::collections::BTreeSet;
type Obj<'p> = Bound<'p, PyAny>;
type List<'p> = Bound<'p, PyList>;
type Bounds = [f64; 4];
fn expanded([a, b, c, d]: Bounds, m: f64) -> Bounds {
    [a - m, b - m, c + m, d + m]
}
fn contains(b: Bounds, p: Point) -> bool {
    p.0 >= b[0] && p.1 >= b[1] && p.0 <= b[2] && p.1 <= b[3]
}
fn bbox_hits(p: &Obj, b: Bounds) -> PyResult<bool> {
    let a = points(p.extract()?)?;
    if a.is_empty() {
        return Err(PyValueError::new_err(
            "zero-size array to reduction operation minimum which has no identity",
        ));
    }
    Ok(a.iter().any(|p| p.0 >= b[0])
        && a.iter().any(|p| p.0 <= b[2])
        && a.iter().any(|p| p.1 >= b[1])
        && a.iter().any(|p| p.1 <= b[3]))
}
fn runs(mask: &[bool]) -> Vec<(usize, usize)> {
    let mut i = 0;
    let mut out = vec![];
    while i < mask.len() {
        if mask[i] {
            i += 1;
            continue;
        }
        let start = i;
        while i < mask.len() && !mask[i] {
            i += 1
        }
        if i - start >= 2 {
            out.push((start, i - 1));
        }
    }
    out
}
#[pyfunction]
fn rail_visible_runs(mask: Vec<bool>) -> Vec<(usize, usize)> {
    runs(&mask)
}
#[pyfunction]
fn rail_arcs<'p>(
    py: Python<'p>,
    xy: numpy::PyReadonlyArray2<f64>,
) -> PyResult<Bound<'p, numpy::PyArray1<f64>>> {
    let pts = points(xy)?;
    Ok(arclengths(&pts).into_pyarray(py))
}
#[pyfunction]
fn rail_left<'p>(
    py: Python<'p>,
    xy: numpy::PyReadonlyArray2<f64>,
) -> PyResult<Bound<'p, numpy::PyArray2<f64>>> {
    let pts = points(xy)?;
    if pts.len() < 2 {
        return Err(PyValueError::new_err("Shape of array too small to calculate a numerical gradient, at least (edge_order + 1) elements are required."));
    }
    Ok(array(py, &normals(&pts)))
}
fn arclengths(p: &[Point]) -> Vec<f64> {
    let mut s = vec![0.];
    for w in p.windows(2) {
        s.push(s[s.len() - 1] + (w[1] - w[0]).norm());
    }
    s
}
fn line_axes(p: &[Point]) -> (Vec<Point>, Vec<f64>) {
    (normals(p), arclengths(p))
}
fn normals(p: &[Point]) -> Vec<Point> {
    let mut u = Vec::with_capacity(p.len());
    for i in 0..p.len() {
        let mut d = p[(i + 1).min(p.len() - 1)] - p[i.saturating_sub(1)];
        if i > 0 && i + 1 < p.len() {
            d = d / 2.;
        }
        u.push((d / d.norm().max(1e-9)).left());
    }
    u
}
fn subset<'p>(seq: &Obj<'p>, keep: &[usize]) -> PyResult<List<'p>> {
    PyList::new(
        seq.py(),
        keep.iter()
            .map(|&i| seq.get_item(i))
            .collect::<PyResult<Vec<_>>>()?,
    )
}
fn heights<'p>(py: Python<'p>, ground: &Obj<'p>, pts: &[Point]) -> PyResult<Vec<f64>> {
    let ans = ground.call1((array(py, pts),))?;
    let z: Vec<f64> = py
        .import("numpy")?
        .call_method1("asarray", (ans, "float64"))?
        .extract()?;
    if z.len() != pts.len() {
        return Err(PyValueError::new_err(
            "ground sample count differs from point count",
        ));
    }
    Ok(z)
}
fn cross_heights<'p>(py: Python<'p>, cr: &Obj<'p>, ground: &Obj<'p>) -> PyResult<List<'p>> {
    let out = PyList::empty(py);
    if cr.len()? == 0 {
        return Ok(out);
    }
    let mut all = vec![];
    for line in cr.try_iter()? {
        all.extend(points(line?.get_item(0)?.extract()?)?);
    }
    let z = heights(py, ground, &all)?.into_pyarray(py);
    let mut k = 0;
    for l in cr.try_iter()? {
        let l = l?;
        let xy = l.get_item(0)?;
        let end = k + xy.len()?;
        out.append((
            xy,
            l.get_item(1)?,
            z.get_item(PySlice::new(py, k as isize, end as isize, 1))?,
        ))?;
        k = end;
    }
    Ok(out)
}
struct Foot {
    width: Vec<f64>,
    z: Vec<f64>,
}
fn feet<'p>(
    py: Python<'p>,
    rw: &Bound<'p, PyModule>,
    p: &[Point],
    u: &[Point],
    top: &[f64],
    others: &Obj<'p>,
    crossers: &Obj<'p>,
    ground: &Obj<'p>,
) -> PyResult<Vec<Foot>> {
    let near = 1.8 + 1.5 * (0.3 + 0.4);
    let mut feet = vec![];
    for sg in [1., -1.] {
        let pts: Vec<_> = (0..p.len()).map(|i| p[i] + u[i] * sg * near).collect();
        let mut z = heights(py, ground, &pts)?;
        let width: Vec<_> = top
            .iter()
            .zip(&z)
            .map(|(t, z)| (1.8 + 1.5 * ((t - z).max(0.) + 0.4)).min(6.))
            .collect();
        let far: Vec<_> = (0..p.len()).filter(|&i| width[i] > near + 0.5).collect();
        if !far.is_empty() {
            let pts: Vec<_> = far.iter().map(|&i| p[i] + u[i] * sg * width[i]).collect();
            for (i, value) in far.into_iter().zip(heights(py, ground, &pts)?) {
                z[i] = value;
            }
        }
        feet.push(Foot { width, z });
    }
    let xy = array(py, p);
    for (foot, sg) in feet.iter_mut().zip([1., -1.]) {
        let sd: Vec<_> = u.iter().map(|u| *u * sg).collect();
        let lim: Vec<f64> = rw
            .call_method1(
                "_foot_limits",
                (
                    &xy,
                    array(py, &sd),
                    foot.width.clone().into_pyarray(py),
                    others,
                    crossers,
                ),
            )?
            .extract()?;
        if lim.iter().zip(&foot.width).any(|(a, b)| a < b) {
            let pts: Vec<_> = (0..p.len()).map(|i| p[i] + sd[i] * lim[i]).collect();
            foot.z = heights(py, ground, &pts)?;
            foot.width = lim;
        }
    }
    Ok(feet)
}
struct Tile<'p> {
    rw: Bound<'p, PyModule>,
    frame: Obj<'p>,
    cells: List<'p>,
    bbox: Obj<'p>,
    mesh: Bound<'p, RailMesh>,
    ground: Obj<'p>,
    rails: List<'p>,
    prof: List<'p>,
    runs: Option<List<'p>>,
    worn: Obj<'p>,
    gauges: Obj<'p>,
    crossers: Obj<'p>,
    style: Obj<'p>,
    small: List<'p>,
    obstacles: Option<Obj<'p>>,
    finals: List<'p>,
    clip: Bounds,
    concrete: i64,
    stone: bool,
}
impl<'p> Tile<'p> {
    fn obstacles(&mut self) -> PyResult<Obj<'p>> {
        if self.obstacles.is_none() {
            self.obstacles = Some(
                self.rw
                    .call_method1("pier_obstacles", (&self.frame, &self.cells, &self.bbox))?,
            );
        }
        Ok(self.obstacles.as_ref().unwrap().clone())
    }
    fn draw(&mut self, n: usize, mut sw: Vec<bool>, gone: bool) -> PyResult<()> {
        let py = self.rw.py();
        let rail = self.rails.get_item(n)?;
        let (fid, class, kind, mut xy): (String, String, String, Obj) = rail.extract()?;
        let (mut zb, zg): (Obj, Obj) = self.prof.get_item(n)?.extract()?;
        let mut p = points(xy.extract()?)?;
        let mut inside: Vec<_> = p.iter().map(|&p| contains(self.clip, p)).collect();
        if gone || !inside.contains(&true) {
            return Ok(());
        }
        let (mut u, mut s) = line_axes(&p);
        let w: usize = self.worn.call_method1("get", (&fid, 0))?.extract()?;
        let rw = self.rw.clone();
        let mat = |name: &str| -> PyResult<String> { rw.getattr(name)?.get_item(w)?.extract() };
        let ballast = self.mesh.borrow_mut().mat(&mat("BALLAST_WORN")?);
        let steel = self.mesh.borrow_mut().mat(&mat("STEEL_WORN")?);
        let half_g = self
            .gauges
            .call_method1(
                "get",
                (&fid, self.rw.call_method1("gauge", (&class, &self.style))?),
            )?
            .extract::<f64>()?
            / 2.
            + 0.07 / 2.;
        let mut top: Vec<_> = zb.extract::<Vec<f64>>()?.iter().map(|z| z + 0.3).collect();
        if top.len() != p.len() || sw.len() != p.len() {
            return Err(PyValueError::new_err(
                "rail profile length differs from geometry",
            ));
        }
        let mut cut: Vec<_> = inside.iter().map(|x| !x).collect();
        let mut slopes = vec![];
        if kind == "ground" {
            let rr = self.runs.as_ref().map(|r| r.get_item(n)).transpose()?;
            let mut blk: Vec<bool> = match &rr {
                Some(r) if r.is_none() => vec![false; p.len()],
                Some(r) => r.get_item("blk")?.extract()?,
                None => self
                    .rw
                    .call_method1("_blocked", (&xy, &zb, &self.crossers))?
                    .extract()?,
            };
            if blk.len() != p.len() {
                return Err(PyValueError::new_err(
                    "crossing mask length differs from rail",
                ));
            }
            if blk.contains(&true) {
                let q = xy.get_item(blk.clone().into_pyarray(py))?;
                let z = self.ground.call1((q,))?;
                let z = py
                    .import("numpy")?
                    .call_method1("asarray", (z, "float64"))?;
                zb = self.rw.call_method1(
                    "_flush",
                    (
                        s.clone().into_pyarray(py),
                        &zb,
                        blk.clone().into_pyarray(py),
                        z,
                    ),
                )?;
                self.finals.set_item(n, &zb)?;
                top = zb.extract::<Vec<f64>>()?.iter().map(|z| z + 0.3).collect();
                if let Some(r) = rr.filter(|r| !r.is_none()) {
                    let (new_xy, new_z, new_blk, src): (Obj, Obj, Vec<bool>, Vec<isize>) = self
                        .rw
                        .call_method1("_insert_edges", (&xy, &zb, r))?
                        .extract()?;
                    xy = new_xy;
                    p = points(xy.extract()?)?;
                    blk = new_blk;
                    if src.len() != s.len() {
                        let (mut normals, arc) = line_axes(&p);
                        let mut last = 0;
                        let mut mask = vec![];
                        for (i, k) in src.into_iter().enumerate() {
                            if k >= 0 {
                                let k = k as usize;
                                normals[i] = u[k];
                                last = last.max(k);
                            }
                            mask.push(sw[last]);
                        }
                        sw = mask;
                        u = normals;
                        s = arc;
                        top = new_z
                            .extract::<Vec<f64>>()?
                            .iter()
                            .map(|z| z + 0.3)
                            .collect();
                        inside = p.iter().map(|&p| contains(self.clip, p)).collect();
                    }
                }
            }
            cut = inside.iter().zip(blk).map(|(i, b)| b || !i).collect();
            let others = PyList::empty(py);
            for r in self.rails.iter() {
                if !r.get_item(0)?.eq(&fid)? {
                    others.append((r.get_item(0)?, r.get_item(3)?))?;
                }
            }
            slopes = feet(
                py,
                &self.rw,
                &p,
                &u,
                &top,
                &others,
                &self.crossers,
                &self.ground,
            )?;
        }
        let sa = s.clone().into_pyarray(py);
        let ua = array(py, &u);
        let ta = top.clone().into_pyarray(py);
        let tram = class == "tram";
        for (i, j) in runs(&cut) {
            let k = i..=j;
            let run = Run {
                p: &p[k.clone()],
                u: &u[k.clone()],
                top: &top[k.clone()],
                v: s[k.clone()].iter().map(|v| v / 3.).collect(),
            };
            if kind == "ground" {
                let stone = self.stone && w == 0;
                let mut mesh = self.mesh.borrow_mut();
                let mat = if stone {
                    mesh.mat("bne_rail_ballast_stone")
                } else {
                    ballast
                };
                run.ballast(
                    &mut mesh,
                    [
                        (&slopes[0].width[k.clone()], &slopes[0].z[k.clone()]),
                        (&slopes[1].width[k.clone()], &slopes[1].z[k]),
                    ],
                    mat,
                    if stone { 0.8 } else { 3. },
                );
            } else if kind == "tunnel" {
                run.tunnel(
                    &mut self.mesh.borrow_mut(),
                    ballast,
                    self.concrete,
                    i == 0,
                    j + 1 == p.len(),
                );
            } else {
                run.deck(&mut self.mesh.borrow_mut(), tram, ballast, self.concrete);
                let obstacles = self.obstacles()?;
                let pw = if tram { 2.6 } else { 3.6 };
                let args = PyTuple::new(
                    py,
                    [
                        sa.as_any(),
                        &xy,
                        ua.as_any(),
                        ta.as_any(),
                        &zg,
                        &i.into_pyobject(py)?.into_any(),
                        &j.into_pyobject(py)?.into_any(),
                        &obstacles,
                        &self.ground,
                        &25f64.into_pyobject(py)?.into_any(),
                        &1.4f64.into_pyobject(py)?.into_any(),
                        &1.5f64.into_pyobject(py)?.into_any(),
                        vec![1.4 / 2., pw / 2.].into_pyarray(py).as_any(),
                    ],
                )?;
                let piers = self.rw.call_method1("pier_sites", args)?;
                if !piers.is_none() {
                    self.small
                        .append((piers, 1.4 / 2., pw / 2., self.concrete))?;
                }
            }
            run.heads(&mut self.mesh.borrow_mut(), half_g, steel, tram, false);
            if !tram {
                let gone = self.rw.getattr("SLEEPERS_GONE")?.get_item(w)?;
                let sp = self
                    .rw
                    .call_method1("_sleepers", (&sa, &xy, &ua, &ta, i, j, gone))?;
                if !sp.is_none() {
                    self.small.append((
                        sp,
                        0.26 / 2.,
                        2.6 / 2.,
                        self.mesh.borrow_mut().mat(&mat("SLEEPER_WORN")?),
                    ))?;
                }
            }
        }
        if !tram {
            let mask: Vec<_> = inside.iter().zip(sw).map(|(i, s)| !i || s).collect();
            for (i, j) in runs(&mask) {
                let k = i..=j;
                let run = Run {
                    p: &p[k.clone()],
                    u: &u[k.clone()],
                    top: &top[k.clone()],
                    v: s[k].iter().map(|v| v / 3.).collect(),
                };
                let mut mesh = self.mesh.borrow_mut();
                let coll = mesh.mat("bne_rail_collider");
                run.heads(&mut mesh, half_g, coll, false, true);
            }
        }
        Ok(())
    }
}
fn covered(under: &Obj, xy: &Obj) -> PyResult<bool> {
    if under.is_none() {
        return Ok(false);
    }
    let v = xy
        .py()
        .import("numpy")?
        .call_method1("asarray", (under.call1((xy,))?, "bool"))?;
    Ok(v.call_method0("mean")?.extract::<f64>()? >= 0.5)
}
fn pack<'p>(mesh: Obj<'p>, decks: List<'p>, both: bool) -> PyResult<Obj<'p>> {
    let py = mesh.py();
    if both {
        Ok((mesh, decks).into_pyobject(py)?.into_any())
    } else {
        Ok(mesh)
    }
}
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn rail_tile_mesh<'p>(
    py: Python<'p>,
    frame: Obj<'p>,
    tx: Obj<'p>,
    ty: Obj<'p>,
    cells: List<'p>,
    mut ground: Obj<'p>,
    style: Obj<'p>,
    far: Obj<'p>,
    both: bool,
    load: Obj<'p>,
    elev: Obj<'p>,
    drop: &Obj<'p>,
    furniture: Obj<'p>,
    under: Obj<'p>,
    records: Obj<'p>,
) -> PyResult<Obj<'p>> {
    let rw = py.import("railways")?;
    let roads = py.import("roads")?;
    let fb = py.import("footbridges")?;
    let mut skip: BTreeSet<usize> = BTreeSet::new();
    let mut rec = records;
    loop {
        let bbox = roads.call_method1("tile_bbox", (&tx, &ty))?;
        let bounds: Bounds = bbox.extract()?;
        let ex = expanded(bounds, roads.getattr("OVERLAP")?.extract()?);
        let clip = expanded(ex, 2.);
        let fit = rw.getattr("RAIL_ROAD_FIT")?.is_truthy()?;
        let (tunnels, worn, gauges, mut rails) = if fit {
            if rec.is_none() {
                rec = rw.call_method1(
                    "crossing_records",
                    (
                        &frame, &tx, &ty, &cells, &ground, &style, &far, &load, &elev,
                    ),
                )?;
            }
            ground = rec.get_item("ground")?;
            (
                rec.get_item("tunnels")?,
                rec.get_item("worn")?,
                rec.get_item("gauges")?,
                rec.get_item("rails")?,
            )
        } else {
            let tunnels = rw.call_method1("road_underpasses", (&frame, &cells, &bbox))?;
            ground = rw.call_method1("_mixed", (&ground, &far, &bbox))?;
            let worn = PyDict::new(py);
            let gauges = PyDict::new(py);
            let kw = PyDict::new(py);
            kw.set_item("clip", true)?;
            kw.set_item("worn", &worn)?;
            kw.set_item("gauges", &gauges)?;
            kw.set_item("underpasses", &tunnels)?;
            let rails = rw.call_method("parts", (&frame, &cells, &bbox), Some(&kw))?;
            (tunnels, worn.into_any(), gauges.into_any(), rails)
        };
        let walk = PyList::empty(py);
        let bridges = if rw.getattr("FOOTBRIDGES_ON")?.is_truthy()? {
            fb.call_method1("parts", (&frame, &cells, &bbox))?
        } else {
            PyList::empty(py).into_any()
        };
        let mut hit = false;
        for r in rails.try_iter()? {
            if bbox_hits(&r?.get_item(3)?, clip)? {
                hit = true;
                break;
            }
        }
        if !hit {
            rails = PyList::empty(py).into_any();
        }
        for b in bridges.try_iter()? {
            let b = b?;
            let xy = b.get_item(1)?;
            if bbox_hits(&xy, clip)? && !covered(&under, &xy)? {
                walk.append(b)?;
            }
        }
        let decks = PyList::empty(py);
        if rails.len()? == 0 && walk.is_empty() {
            return pack(py.None().into_bound(py), decks, both);
        }
        let mut keep = Vec::new();
        for i in 0..rails.len()? {
            if !skip.contains(&i) && !drop.contains(i)? {
                keep.push(i);
            }
        }
        let (prof, runs) = if rails.len()? == 0 {
            (PyList::empty(py), None)
        } else if fit {
            (
                subset(&rec.get_item("prof")?, &keep)?,
                Some(subset(&rec.get_item("runs")?, &keep)?),
            )
        } else {
            let coarse = rw.call_method1("coarse_underpasses", (&frame, &cells, &bbox, &rails))?;
            let p = rw.call_method1(
                "_beds",
                (&frame, &rails, &ground, tunnels.add(coarse)?, &load, &elev),
            )?;
            (subset(&p, &keep)?, None)
        };
        let rails = subset(&rails, &keep)?;
        let mut carried = vec![];
        let mut ground_lines = vec![];
        for (r, p) in rails.iter().zip(prof.iter()) {
            let class = r.get_item(1)?;
            let kind = r.get_item(2)?;
            let xy = r.get_item(3)?;
            let gone = kind.eq("bridge")? && covered(&under, &xy)?;
            carried.push(gone);
            if kind.eq("ground")? && !class.eq("tram")? {
                ground_lines.push(xy.clone());
            }
            if both && !gone && kind.eq("bridge")? {
                let z: Vec<f64> = p.get_item(0)?.extract()?;
                let line = points(xy.extract()?)?;
                if line.len() != z.len() {
                    return Err(PyValueError::new_err("deck/profile lengths differ"));
                }
                let deck = PyDict::new(py);
                deck.set_item(
                    "n",
                    rows(
                        py,
                        line.into_iter()
                            .zip(z)
                            .map(|(p, z)| at(p, z + 0.3))
                            .collect(),
                    ),
                )?;
                deck.set_item(
                    "w",
                    if class.eq("tram")? {
                        2. * 1.3
                    } else {
                        2. * 2.4
                    },
                )?;
                decks.append(deck)?;
            }
        }
        let true_cross = rw.getattr("TRUE_CROSSERS_ON")?.is_truthy()?;
        let (all_cr, cr, crossers) = if fit && rec.get_item("rails")?.len()? > 0 {
            (
                rec.get_item("all_cr")?,
                rec.get_item("cr")?,
                rec.get_item("crossers")?,
            )
        } else {
            let all = rw.call_method1("crossings", (&frame, &cells, &bbox, 50.))?;
            let cr = if true_cross {
                rw.call_method1("true_crossers", (ground_lines, &all))?
            } else {
                all.clone()
            }
            .add(&tunnels)?;
            let cross = cross_heights(py, &cr, &ground)?;
            (all, cr, cross.into_any())
        };
        let walk_cr = if rw.getattr("FOOT_CLEAR")?.is_truthy()?
            && !walk.is_empty()
            && true_cross
            && all_cr.len()? + tunnels.len()? > cr.len()?
        {
            cross_heights(py, &all_cr.add(&tunnels)?, &ground)?.into_any()
        } else {
            crossers.clone()
        };
        let switch = rw.call_method1("_switch_masks", (&rails,))?;
        let mut mesh = RailMesh::new([bounds[0], bounds[1], 0.]);
        mesh.mat("bne_rail_ballast");
        let concrete = mesh.mat("bne_bridge_concrete");
        mesh.mat("bne_rail_steel");
        let mesh = Bound::new(py, mesh)?;
        let finals = PyList::new(
            py,
            prof.iter()
                .map(|p| p.get_item(0))
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let mut tile = Tile {
            rw: rw.clone(),
            frame: frame.clone(),
            cells: cells.clone(),
            bbox: bbox.clone(),
            mesh: mesh.clone(),
            ground: ground.clone(),
            rails: rails.clone(),
            prof,
            runs,
            worn: worn.clone(),
            gauges: gauges.clone(),
            crossers: crossers.clone(),
            style: style.clone(),
            small: PyList::empty(py),
            obstacles: None,
            finals,
            clip,
            concrete,
            stone: py.import("railxing")?.getattr("ENABLED")?.is_truthy()?,
        };
        for (i, gone) in carried.into_iter().enumerate() {
            tile.draw(i, switch.get_item(i)?.extract()?, gone)?;
        }
        let props = if rw.getattr("STATION_FURNITURE_ON")?.is_truthy()? {
            let cut = if fit {
                rec.get_item("cut")?
            } else {
                py.None().into_bound(py)
            };
            furniture::tile(py, &tile, ex, &all_cr, &cut, !furniture.is_none())?
        } else {
            PyList::empty(py).into_any()
        };
        if !walk.is_empty() {
            tile.obstacles()?;
        }
        fb.call_method1(
            "add",
            (
                &mesh,
                &walk,
                &ground,
                walk_cr,
                ex,
                concrete,
                &tile.small,
                tile.obstacles,
            ),
        )?;
        let budget: usize = rw.getattr("TILE_VERTS_MAX")?.extract()?;
        let mut small = Vec::new();
        for t in tile.small.iter() {
            let size = t.get_item(2)?.extract::<f64>()?;
            small.push((size, t));
        }
        small.sort_by(|a, b| b.0.total_cmp(&a.0));
        for (_, entry) in small {
            let room = budget.saturating_sub(mesh.borrow().nv()) / 8;
            if room == 0 {
                break;
            }
            let (parts, hx, hy, mi): (Obj, f64, f64, i64) = entry.extract()?;
            let c = parts.get_item(0)?;
            let n = c.len()?.min(room);
            let sl = PySlice::new(py, 0, n as isize, 1);
            mesh.call_method1(
                "boxes",
                (
                    c.get_item(&sl)?,
                    parts.get_item(1)?.get_item(&sl)?,
                    parts.get_item(2)?.get_item(&sl)?,
                    hx,
                    hy,
                    parts.get_item(3)?.get_item(&sl)?,
                    parts.get_item(4)?.get_item(&sl)?,
                    mi,
                ),
            )?;
        }
        if mesh.borrow().nv() > budget {
            if rails.len() > 1 {
                let mut ranking = Vec::new();
                for (i, r) in rails.iter().enumerate() {
                    let wear: usize = worn.call_method1("get", (r.get_item(0)?, 0))?.extract()?;
                    let length: f64 = rw
                        .call_method1("_arc", (r.get_item(3)?,))?
                        .get_item(-1)?
                        .extract()?;
                    ranking.push((wear, -length, keep[i]));
                }
                ranking.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.total_cmp(&b.1)));
                let count = 1.max(ranking.len() / 4).min(ranking.len() - 1);
                skip.extend(ranking.iter().rev().take(count).map(|r| r.2));
                continue;
            }
            if fit {
                rec.set_item("kept", PyFrozenSet::empty(py)?)?;
            }
            return pack(py.None().into_bound(py), decks, both);
        }
        if fit {
            rec.set_item("kept", PyFrozenSet::new(py, &keep)?)?;
        }
        if !furniture.is_none() {
            furniture.call_method1("__iadd__", (props,))?;
        }
        return pack(mesh.call_method0("result")?, decks, both);
    }
}
pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_visible_runs, m)?)?;
    m.add_function(wrap_pyfunction!(rail_arcs, m)?)?;
    m.add_function(wrap_pyfunction!(rail_left, m)?)?;
    obstacles::register(m)?;
    furniture::register(m)?;
    profile::register(m)?;
    m.add_class::<RailMesh>()?;
    m.add_function(wrap_pyfunction!(rail_tile_mesh, m)?)
}
