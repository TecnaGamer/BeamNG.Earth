//! Emit pieces in the order the road mesh consumes them. Heights are sampled through
//! the caller's DEM or drawn-ground callback, with the same points and callback order.
use super::*;
use numpy::{PyArrayMethods, PyReadonlyArray2};
use std::collections::{BTreeMap, BTreeSet};

const STEEL: &str = "bne_truss_steel";
const CONCRETE: &str = "bne_bridge_concrete";
const LIGHT: &str = "bne_tunnel_light";

struct Writer<'a, 'p> {
    mesh: &'a Bound<'p, PyAny>,
}
impl Writer<'_, '_> {
    fn add(&self, mat: &str, p: Vec<[f64; 3]>, uv: Vec<[f64; 2]>, t: Vec<[i64; 3]>) -> PyResult<()> {
        let py = self.mesh.py();
        self.mesh
            .call_method1("add", (mat, rows(py, p), rows(py, uv), rows(py, t)))?;
        Ok(())
    }
    fn walls(&self, mat: &str, rep: f64, r: &[Point], z0: &[f64], z1: &[f64]) -> PyResult<()> {
        let r = ccw(r);
        let n = r.len();
        for z in [z0, z1] {
            if z.len() != 1 && z.len() != n {
                return Err(PyValueError::new_err("one wall height per vertex required"));
            }
        }
        let mut run = 0.0;
        for (i, (a, b)) in edges(&r).enumerate() {
            let j = (i + 1) % n;
            let len = (b - a).norm();
            if len < 1e-3 {
                continue;
            }
            let low = |k| z0[if z0.len() == 1 { 0 } else { k }];
            let high = |k| z1[if z1.len() == 1 { 0 } else { k }];
            let ps = vec![
                [a.0, a.1, low(i)],
                [b.0, b.1, low(j)],
                [b.0, b.1, high(j)],
                [a.0, a.1, high(i)],
            ];
            let uv = vec![
                [run / rep, low(i) / rep],
                [(run + len) / rep, low(j) / rep],
                [(run + len) / rep, high(j) / rep],
                [run / rep, high(i) / rep],
            ];
            self.add(mat, ps, uv, vec![[0, 1, 2], [0, 2, 3]])?;
            run += len;
        }
        Ok(())
    }
    fn face(&self, mat: &str, rep: f64, r: &[Point], heights: &[f64], t: Vec<[i64; 3]>, up: bool) -> PyResult<()> {
        if heights.len() != 1 && heights.len() != r.len() {
            return Err(PyValueError::new_err("one face height per vertex required"));
        }
        let ps = r
            .iter()
            .enumerate()
            .map(|(i, p)| [p.0, p.1, heights[if heights.len() == 1 { 0 } else { i }]])
            .collect();
        let uv = r.iter().map(|p| [p.0 / rep, p.1 / rep]).collect();
        let tris = if up {
            t
        } else {
            t.into_iter().map(|[a, b, c]| [c, b, a]).collect()
        };
        self.add(mat, ps, uv, tris)
    }
    fn top(&self, mat: &str, rep: f64, r: &[Point], mut z: Vec<f64>, up: bool) -> PyResult<()> {
        let mut r = open(r).to_vec();
        if signed(&r) < 0.0 {
            r.reverse();
            z.reverse()
        }
        let t = triangles(&r);
        if t.is_empty() {
            return Ok(());
        }
        self.face(mat, rep, &r, &z, t, up)
    }
    fn divided(
        &self,
        mat: &str,
        rep: f64,
        r: &[Point],
        z: impl Fn(&[Point]) -> PyResult<Vec<f64>>,
        up: bool,
        step: f64,
    ) -> PyResult<()> {
        let py = self.mesh.py();
        let mut r = open(r).to_vec();
        if signed(&r) < 0.0 {
            r.reverse()
        }
        let t = triangles(&r);
        if t.is_empty() {
            return Ok(());
        }
        let (p, t) = crate::subdivide_tris(py, array(py, &r).readonly(), rows(py, t).readonly(), step);
        let r = points(p.readonly())?;
        let t = t
            .readonly()
            .as_array()
            .rows()
            .into_iter()
            .map(|r| [r[0], r[1], r[2]])
            .collect();
        self.face(mat, rep, &r, &z(&r)?, t, up)
    }
}
fn triangles(r: &[Point]) -> Vec<[i64; 3]> {
    crate::ear_clip(&r.iter().map(|p| p.pair()).collect::<Vec<_>>())
}
#[pyfunction]
fn bus_top(
    mesh: &Bound<'_, PyAny>,
    mat: &str,
    rep: f64,
    xy: XY<'_>,
    z: PyReadonlyArray1<'_, f64>,
    up: bool,
) -> PyResult<()> {
    Writer { mesh }.top(mat, rep, &points(xy)?, z.as_array().to_vec(), up)
}
#[pyfunction]
fn bus_walls(
    mesh: &Bound<'_, PyAny>,
    mat: &str,
    rep: f64,
    xy: XY<'_>,
    z0: PyReadonlyArray1<'_, f64>,
    z1: PyReadonlyArray1<'_, f64>,
) -> PyResult<()> {
    Writer { mesh }.walls(mat, rep, &points(xy)?, &z0.as_array().to_vec(), &z1.as_array().to_vec())
}
#[pyfunction]
fn bus_face(
    mesh: &Bound<'_, PyAny>,
    mat: &str,
    rep: f64,
    xy: XY<'_>,
    zfun: &Bound<'_, PyAny>,
    up: bool,
    step: f64,
) -> PyResult<()> {
    Writer { mesh }.divided(
        mat,
        rep,
        &points(xy)?,
        |r| zfun.call1((array(mesh.py(), r),))?.extract(),
        up,
        step,
    )
}
fn square(p: Point, h: f64) -> Ring {
    vec![
        Point(p.0 - h, p.1 - h),
        Point(p.0 + h, p.1 - h),
        Point(p.0 + h, p.1 + h),
        Point(p.0 - h, p.1 + h),
    ]
}
fn over_lane(r: &[Point], paths: &[Lane], mouths: &[Point]) -> bool {
    paths.iter().any(|l| {
        let p = if l.xy.len() > 2 { open(&l.xy) } else { &l.xy };
        if p.len() == 1 {
            return inside(r, p[0]);
        }
        p.windows(2).any(|s| {
            let d = s[1] - s[0];
            let k = crate::py_floordiv(d.norm(), 3.0).max(1.0) as usize;
            (0..k).any(|i| inside(r, s[0] + d * (i as f64 / k as f64)))
        })
    }) || mouths.iter().any(|&p| inside(r, p))
}
#[pyfunction]
fn bus_over_lane(ring: XY<'_>, paths: Vec<(XY<'_>, f64)>, mouths: Vec<(f64, f64)>) -> PyResult<bool> {
    Ok(over_lane(&points(ring)?, &lanes(paths)?, &pairs(mouths)))
}
fn height(r: &[Point], mapped: Option<f64>) -> f64 {
    mapped.unwrap_or_else(|| (3.6 + 0.10 * obb(r).map_or(0.0, |b| b.width())).clamp(3.6, 8.5))
}
#[pyfunction]
fn bus_roof_height(ring: XY<'_>, mapped: Option<f64>) -> PyResult<f64> {
    Ok(height(&points(ring)?, mapped))
}
#[pyfunction]
fn bus_dem_points<'p>(py: Python<'p>, xy: XY<'p>) -> PyResult<Array<'p>> {
    let r = points(xy)?;
    let (lo, hi) = bounds(&r);
    let mut step = 6.0;
    while ((hi.0 - lo.0) / step + 1.0) * ((hi.1 - lo.1) / step + 1.0) > 400.0 {
        step *= 1.5
    }
    let mut pts = Vec::new();
    for y in arange(lo.1, hi.1 + step, step) {
        for x in arange(lo.0, hi.0 + step, step) {
            pts.push(Point(x, y))
        }
    }
    let mask = crate::contains_points(py, array(py, &r).readonly(), array(py, &pts).readonly());
    let mask = mask.readonly();
    let mut take: Vec<_> = pts
        .into_iter()
        .zip(mask.as_array())
        .filter_map(|(p, &ok)| ok.then_some(p))
        .collect();
    if take.is_empty() {
        take.push(centroid(&r))
    }
    Ok(array(py, &take))
}
fn ground(z: &Bound<'_, PyAny>, p: Point) -> PyResult<f64> {
    z.call1((array(z.py(), &[p]),))?.get_item(0)?.extract()
}
#[pyfunction]
fn bus_roofs(
    mesh: &Bound<'_, PyAny>,
    canopies: Vec<Bound<'_, PyDict>>,
    paths: Vec<(XY<'_>, f64)>,
    mouths: Vec<(f64, f64)>,
    walls: Vec<XY<'_>>,
    z: &Bound<'_, PyAny>,
    base: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    let w = Writer { mesh };
    let py = mesh.py();
    let lanes = lanes(paths)?;
    let mouths = pairs(mouths);
    let walls = rings(walls)?;
    let mut placed = Vec::new();
    for cn in canopies {
        let footprint = points(cn.as_any().get_item("ring")?.extract()?)?;
        let r = ccw(&footprint);
        if r.len() < 3 {
            continue;
        }
        let zc = match base {
            Some(f) => f.call1((&cn, array(py, &r)))?.extract()?,
            None => ground(z, centroid(&r))?,
        };
        let mapped = if cn.get_item("mapped")?.is_some_and(|v| v.is_truthy().unwrap_or(false)) {
            Some(super::super::layout::optional_float(cn.get_item("height")?)?)
        } else {
            None
        };
        // Equal-area bounding boxes keep the first edge. Measure the mapped footprint
        // in source order before winding the faces for rendering.
        let mut h = height(&footprint, mapped);
        if over_lane(&r, &lanes, &mouths) {
            h = h.max(4.8)
        }
        let zeave = zc + h;
        let zbot = zeave - 0.30;
        let pitched = if cn.get_item("flat")?.is_some_and(|v| v.is_truthy().unwrap_or(false)) {
            None
        } else {
            gable(&r)
        };
        if let Some((pts, _, halves, width)) = pitched {
            let b = obb(&r).unwrap();
            let mid = (b.v0 + b.v1) / 2.0;
            let rise = 0.12 * width;
            let z_at = |p| zeave + rise * ridge_fraction(p, b.t.left(), mid, width);
            for sel in halves {
                let ring: Vec<_> = sel.iter().map(|&i| pts[i]).collect();
                w.divided(
                    STEEL,
                    3.0,
                    &ring,
                    |r| Ok(r.iter().map(|&p| z_at(p)).collect()),
                    true,
                    12.0,
                )?;
                w.divided(
                    STEEL,
                    3.0,
                    &ring,
                    |r| Ok(r.iter().map(|&p| z_at(p) - 0.30).collect()),
                    false,
                    12.0,
                )?;
            }
            let edge = densify(&ccw(&pts), 12.0);
            let top: Vec<_> = edge.iter().map(|&p| z_at(p)).collect();
            w.walls(
                STEEL,
                3.0,
                &edge,
                &top.iter().map(|z| z - 0.30).collect::<Vec<_>>(),
                &top,
            )?;
        } else {
            w.divided(STEEL, 3.0, &r, |_| Ok(vec![zeave]), true, 12.0)?;
            w.divided(STEEL, 3.0, &r, |_| Ok(vec![zbot]), false, 12.0)?;
            w.walls(STEEL, 3.0, &densify(&ccw(&r), 12.0), &[zbot], &[zeave])?;
        }
        let pts: Vec<_> = column_points(&r, &lanes, &mouths, &walls)
            .into_iter()
            .filter(|&p| placed.iter().all(|&q: &Point| (p - q).norm() >= 1.5))
            .collect();
        placed.extend_from_slice(&pts);
        let centre = centroid(&r);
        let area: f64 = cn.as_any().get_item("area")?.extract()?;
        for &p in pts.iter().step_by(if area < 2000.0 { 2 } else { 3 }) {
            let v = centre - p;
            let d = v.norm();
            let ax = if d > 1e-6 { v / d } else { Point(1.0, 0.0) };
            let q = p + ax * 1.2f64.min(d * 0.5);
            let a = ax * 1.1 / 2.0;
            let b = ax.left() * 0.35 / 2.0;
            if inside(&r, q) {
                w.top(
                    LIGHT,
                    1.0,
                    &[q - a - b, q + a - b, q + a + b, q - a + b],
                    vec![zbot - 0.02],
                    false,
                )?
            }
        }
        let half = 0.28 / 2.0 * (1.0 + (h - 4.0).max(0.0) * 0.12);
        for p in pts {
            w.walls(STEEL, 3.0, &square(p, half), &[ground(z, p)? - 0.05], &[zbot])?
        }
    }
    Ok(())
}
#[pyfunction]
fn bus_post(mesh: &Bound<'_, PyAny>, p: (f64, f64), zg: f64) -> PyResult<()> {
    let p = Point(p.0, p.1);
    let step = 2.0 * std::f64::consts::PI / 8.0;
    let r: Vec<_> = (0..8)
        .map(|i| {
            let a = step * i as f64;
            Point(p.0 + 0.09 * a.dcos(), p.1 + 0.09 * a.dsin())
        })
        .collect();
    let w = Writer { mesh };
    w.walls(STEEL, 3.0, &r, &[zg - 0.05], &[zg + 0.9])?;
    w.top(STEEL, 3.0, &r, vec![zg + 0.9], true)
}
#[pyfunction]
fn bus_kiosk_mesh(
    mesh: &Bound<'_, PyAny>,
    spot: (f64, f64, f64, f64),
    z: &Bound<'_, PyAny>,
    collide: bool,
) -> PyResult<()> {
    let w = Writer { mesh };
    let p = Point(spot.0, spot.1);
    let t = Point(spot.2, spot.3);
    let n = t.left();
    let r = placement::rectangle(p, t * (3.2 / 2.0), n * 2.6 / 2.0);
    let zg = ground(z, p)?;
    if collide {
        return w.walls(STEEL, 3.0, &r, &[zg], &[ground(z, p)? + 2.7]);
    }
    let zg = zg + 0.18;
    w.walls(CONCRETE, 3.0, &r, &[zg - 0.05], &[zg + 2.7])?;
    let top = zg + 2.7 + 0.15;
    w.top(
        STEEL,
        3.0,
        &r.iter().map(|&p| p + Point(0.0, 0.0)).collect::<Vec<_>>(),
        vec![top],
        true,
    )?;
    w.top(
        STEEL,
        3.0,
        &placement::rectangle(p, t * (3.2 / 2.0 + 0.25), n * (2.6 / 2.0 + 0.25)),
        vec![top],
        true,
    )
}
#[pyfunction]
fn bus_collision_columns(
    mesh: &Bound<'_, PyAny>,
    roofs: Vec<XY<'_>>,
    paths: Vec<(XY<'_>, f64)>,
    mouths: Vec<(f64, f64)>,
    walls: Vec<XY<'_>>,
    z: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let w = Writer { mesh };
    let lanes = lanes(paths)?;
    let mouths = pairs(mouths);
    let walls = rings(walls)?;
    for r in rings(roofs)? {
        let r = ccw(&r);
        if r.len() < 3 {
            continue;
        }
        for p in column_points(&r, &lanes, &mouths, &walls) {
            let zg = ground(z, p)?;
            w.walls(STEEL, 3.0, &square(p, 0.28 / 2.0 * 1.2), &[zg - 0.3], &[zg + 4.0])?;
        }
    }
    Ok(())
}
type MeshResult<'p> = (
    Array<'p>,
    Bound<'p, PyArray2<i64>>,
    Array<'p>,
    Vec<String>,
    Bound<'p, PyArray1<i64>>,
);
#[pyfunction]
fn bus_mesh_result<'p>(
    py: Python<'p>,
    p: XY<'p>,
    tris: PyReadonlyArray2<'p, i64>,
    uv: XY<'p>,
    mats: Vec<String>,
    idx: PyReadonlyArray1<'p, i64>,
    b: (f64, f64, f64, f64),
    origin: [f64; 3],
    overlap: bool,
) -> PyResult<Option<MeshResult<'p>>> {
    let p = p.as_array();
    let t = tris.as_array();
    let uv = uv.as_array();
    let idx = idx.as_array();
    if p.ncols() != 3 || t.ncols() != 3 || uv.ncols() != 2 || uv.nrows() != p.nrows() || idx.len() != t.nrows() {
        return Err(PyValueError::new_err("inconsistent station mesh arrays"));
    }
    let mut keep = vec![];
    let mut used = BTreeSet::new();
    let mut materials = BTreeSet::new();
    for (i, r) in t.rows().into_iter().enumerate() {
        let v = [r[0] as usize, r[1] as usize, r[2] as usize];
        if v.iter().any(|&j| j >= p.nrows()) || idx[i] as usize >= mats.len() {
            return Err(PyValueError::new_err("invalid station mesh index"));
        }
        let in_axis = |k, lo, hi| {
            let [a, b, c] = v.map(|j| p[[j, k]]);
            if overlap {
                npmax(npmax(a, b), c) >= lo && npmin(npmin(a, b), c) < hi
            } else {
                let mid = ((0.0 + a) + b + c) / 3.0;
                mid >= lo && mid < hi
            }
        };
        if in_axis(0, b.0, b.2) && in_axis(1, b.1, b.3) {
            keep.push((v, idx[i]));
            used.extend(v);
            materials.insert(idx[i]);
        }
    }
    if keep.is_empty() {
        return Ok(None);
    }
    let mut remap = vec![0; p.nrows()];
    let mut pos = Vec::new();
    let mut uvs = Vec::new();
    for j in used {
        remap[j] = pos.len() as i64;
        pos.push([p[[j, 0]] - origin[0], p[[j, 1]] - origin[1], p[[j, 2]] - origin[2]]);
        uvs.push([uv[[j, 0]], uv[[j, 1]]]);
    }
    let ids: Vec<_> = materials.into_iter().collect();
    let mut tr = Vec::new();
    let mut mi = Vec::new();
    for (v, i) in keep {
        tr.push(v.map(|j| remap[j]));
        mi.push(ids.binary_search(&i).unwrap() as i64);
    }
    Ok(Some((
        rows(py, pos),
        rows(py, tr),
        rows(py, uvs),
        ids.iter().map(|&i| mats[i as usize].clone()).collect(),
        mi.into_pyarray(py),
    )))
}
#[pyfunction]
fn bus_boxes(vertices: XY<'_>, origin: [f64; 3]) -> PyResult<Vec<Vec<f64>>> {
    let v = vertices.as_array();
    if v.ncols() != 3 {
        return Err(PyValueError::new_err("expected xyz vertices"));
    }
    let mut boxes = BTreeMap::new();
    for row in v.rows() {
        let p = [row[0] + origin[0], row[1] + origin[1], row[2] + origin[2]];
        let key = ((p[0] / 20.0).floor() as i64, (p[1] / 20.0).floor() as i64);
        let (lo, hi) = boxes.entry(key).or_insert((p, p));
        for i in 0..3 {
            lo[i] = npmin(lo[i], p[i]);
            hi[i] = npmax(hi[i], p[i]);
        }
    }
    Ok(boxes
        .into_values()
        .map(|(lo, hi)| {
            lo.map(|x| py_round(x - 0.5, 2))
                .into_iter()
                .chain(hi.map(|x| py_round(x + 0.5, 2)))
                .collect()
        })
        .collect())
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_add_chunk, m)?)?;
    m.add_function(wrap_pyfunction!(bus_top, m)?)?;
    m.add_function(wrap_pyfunction!(bus_walls, m)?)?;
    m.add_function(wrap_pyfunction!(bus_face, m)?)?;
    m.add_function(wrap_pyfunction!(bus_over_lane, m)?)?;
    m.add_function(wrap_pyfunction!(bus_roof_height, m)?)?;
    m.add_function(wrap_pyfunction!(bus_dem_points, m)?)?;
    m.add_function(wrap_pyfunction!(bus_roofs, m)?)?;
    m.add_function(wrap_pyfunction!(bus_post, m)?)?;
    m.add_function(wrap_pyfunction!(bus_kiosk_mesh, m)?)?;
    m.add_function(wrap_pyfunction!(bus_collision_columns, m)?)?;
    m.add_function(wrap_pyfunction!(bus_mesh_result, m)?)?;
    m.add_function(wrap_pyfunction!(bus_boxes, m)?)
}

/// Parts remain available on the Python mesh (also shared by rail station builders).
/// Store vertex/UV arrays by reference; face indices are offset at append time.
#[pyfunction]
fn bus_add_chunk(
    mesh: &Bound<'_, PyAny>,
    mat: &Bound<'_, PyAny>,
    p: &Bound<'_, PyArray2<f64>>,
    uv: &Bound<'_, PyArray2<f64>>,
    t: PyReadonlyArray2<'_, i64>,
) -> PyResult<()> {
    let py = mesh.py();
    let mats = mesh.getattr("mats")?.cast_into::<PyList>()?;
    if !mats.contains(mat)? {
        mats.append(mat)?
    }
    let material = mats.index(mat)? as i64;
    let base: i64 = mesh.getattr("n")?.extract()?;
    let v = t.as_array();
    let tris = v.mapv(|idx| idx.wrapping_add(base)).into_pyarray(py);
    mesh.getattr("P")?.cast::<PyList>()?.append(p)?;
    mesh.getattr("UV")?.cast::<PyList>()?.append(uv)?;
    mesh.getattr("T")?.cast::<PyList>()?.append(tris)?;
    mesh.getattr("M")?
        .cast::<PyList>()?
        .append(vec![material; v.nrows()].into_pyarray(py))?;
    mesh.setattr("n", base + p.readonly().as_array().nrows() as i64)
}
