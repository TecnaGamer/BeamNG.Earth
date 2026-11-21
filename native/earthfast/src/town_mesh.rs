use super::*;
use numpy::ndarray::Array2;

/// A pedestrian area's indexed top and its exposed kerbs, relative to the
/// same origin as the adjoining walks. The skin samples the final road ground.
#[pyfunction]
pub fn town_area_mesh<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    tris: PyReadonlyArray2<'py, i64>,
    materials: Vec<i64>,
    edges: Vec<[usize; 2]>,
    origin: [f64; 3],
) -> PyResult<MeshOut<'py>> {
    let (p, t) = (points.as_array(), tris.as_array());
    if p.ncols() != 3
        || t.ncols() != 3
        || t.nrows() != materials.len()
        || t.iter().any(|&i| i < 0 || i as usize >= p.nrows())
        || edges.iter().flatten().any(|&i| i >= p.nrows())
    {
        return Err(PyValueError::new_err("invalid town paving mesh"));
    }
    let mut m = Mesh::default();
    for q in p.rows() {
        m.p.push([
            q[0] - origin[0],
            q[1] - origin[1],
            q[2] + CURB_H - origin[2],
        ]);
        m.uv.push([q[0] / BRICK_REPEAT_M, q[1] / BRICK_REPEAT_M]);
    }
    m.t.extend(t.rows().into_iter().map(|r| [r[0], r[1], r[2]]));
    m.m = materials;
    for [a, b] in edges {
        let (qa, qb) = (m.p[a], m.p[b]);
        let bottom = |p: [f64; 3]| [p[0], p[1], p[2] - CURB_H - SINK];
        let u = (qb[0] - qa[0]).dhypot(qb[1] - qa[1]) / CURB_REPEAT_M;
        let k = m.add(
            vec![qa, bottom(qa), bottom(qb), qb],
            vec![
                [0., v_light_low()],
                [0., v_band_low()],
                [u, v_band_low()],
                [u, v_light_low()],
            ],
        );
        m.t.extend([[k, k + 1, k + 2], [k, k + 2, k + 3]]);
        m.m.extend([0, 0]);
    }
    Ok(mesh_out(py, m))
}

/// Weld on the serialized vertex precision. Collision has no texture seams.
/// The drawn mesh keeps independent texture coordinates at atlas boundaries.
#[pyfunction]
pub fn town_weld<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    tris: PyReadonlyArray2<'py, i64>,
    uv: PyReadonlyArray2<'py, f64>,
    materials: PyReadonlyArray1<'py, i64>,
    collision: bool,
    origin: [f64; 3],
) -> PyResult<MeshOut<'py>> {
    use std::collections::BTreeMap;
    let (p, t, uv, mat) = (
        points.as_array(),
        tris.as_array(),
        uv.as_array(),
        materials.as_array(),
    );
    if p.ncols() != 3
        || t.ncols() != 3
        || mat.len() != t.nrows()
        || uv.ncols() != 2
        || p.nrows() != uv.nrows()
    {
        return Err(PyValueError::new_err("invalid town mesh"));
    }
    let mut m = Mesh::default();
    let mut known = BTreeMap::new();
    for (i, face) in t.rows().into_iter().enumerate() {
        let mut tri = [0; 3];
        for j in 0..3 {
            let k = face[j] as usize;
            if k >= p.nrows() {
                return Err(PyValueError::new_err("town vertex out of bounds"));
            }
            let v = [p[[k, 0]], p[[k, 1]], p[[k, 2]]];
            let u = if collision {
                [0.; 2]
            } else if matches!(mat[i], 3 | 19 | 20) {
                let repeat = match mat[i] {
                    19 => 3.6,
                    20 => 2.5,
                    _ => BRICK_REPEAT_M,
                };
                [(v[0] + origin[0]) / repeat, (v[1] + origin[1]) / repeat]
            } else {
                [uv[[k, 0]], uv[[k, 1]]]
            };
            let key = (
                v.map(|x| (x * 1e6).round() as i64),
                u.map(|x| (x as f32).to_bits()),
                mat[i],
            );
            tri[j] = *known.entry(key).or_insert_with(|| {
                m.p.push(v);
                m.uv.push(u);
                m.p.len() as i64 - 1
            });
        }
        if tri[0] != tri[1] && tri[1] != tri[2] && tri[0] != tri[2] {
            m.t.push(tri);
            m.m.push(mat[i]);
        }
    }
    Ok(mesh_out(py, m))
}

/// Exterior edges at the exact width breaks used by the slab, for collision
/// bounds and clipping adjoining pedestrian areas in the same district.
#[pyfunction]
pub fn town_outline<'py>(
    py: Python<'py>,
    curb: PyReadonlyArray2<'py, f64>,
    normals: PyReadonlyArray2<'py, f64>,
    profile: Vec<(f64, f64)>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>)> {
    let (c, n) = (curb.as_array(), normals.as_array());
    if c.ncols() != 3
        || c.nrows() < 2
        || n.nrows() != c.nrows()
        || n.ncols() != 2
        || profile.is_empty()
    {
        return Err(PyValueError::new_err("invalid town outline"));
    }
    let arc = arclen(&pts3(&c, 0..c.nrows()));
    let width_station: Vec<_> = profile.iter().map(|p| p.0 * arc.last().unwrap()).collect();
    let widths: Vec<_> = profile.iter().map(|p| p.1).collect();
    let mut stations = arc.clone();
    stations.extend_from_slice(&width_station);
    stations.sort_by(f64::total_cmp);
    stations.dedup();
    let mut front = Array2::zeros((stations.len(), 3));
    let mut back = front.clone();
    for k in 0..3 {
        let values = c.column(k).to_vec();
        for (j, &s) in stations.iter().enumerate() {
            front[[j, k]] = interp(s, &arc, &values);
        }
    }
    for k in 0..2 {
        let dirs = n.column(k).to_vec();
        for (j, &s) in stations.iter().enumerate() {
            back[[j, k]] =
                front[[j, k]] + interp(s, &width_station, &widths) * interp(s, &arc, &dirs);
        }
    }
    back.column_mut(2).assign(&front.column(2));
    Ok((front.into_pyarray(py), back.into_pyarray(py)))
}
