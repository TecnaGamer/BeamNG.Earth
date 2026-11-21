//! Mapped pedestrian areas. All clipping is in the tile frame. Claim the smaller areas
//! first, and cut a polygon exactly at road, parking, hole and building boundaries.
use super::*;
use std::collections::BTreeMap;

type Pt = (f64, f64);

fn rect(p: &Poly, b: [f64; 4]) -> Option<Poly> {
    intersection(
        p,
        &[
            (1., 0., b[0]),
            (0., 1., b[1]),
            (-1., 0., -b[2]),
            (0., -1., -b[3]),
        ],
    )
}

fn contains(c: &FrontageCoverage, q: Pt) -> bool {
    c.keys(q, q)
        .iter()
        .filter_map(|k| c.cells.get(k))
        .flatten()
        .any(|&i| {
            c.shapes[i]
                .planes
                .iter()
                .all(|&(x, y, d)| q.0 * x + q.1 * y >= d - 1e-9)
        })
}

type Plan<'py> = (
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Vec<i64>,
    Vec<[i64; 2]>,
);

#[pyfunction]
pub fn town_paving<'py>(
    py: Python<'py>,
    bounds: [f64; 4],
    areas: Vec<(
        PyReadonlyArray2<'py, f64>,
        Vec<PyReadonlyArray2<'py, f64>>,
        i64,
    )>,
    mut reserved: PyRefMut<'py, FrontageCoverage>,
) -> PyResult<Plan<'py>> {
    let [x0, y0, x1, y1] = bounds;
    if !bounds.iter().all(|v| v.is_finite()) || x1 < x0 || y1 < y0 || (x1 - x0).max(y1 - y0) > 4096.
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid town paving bounds",
        ));
    }
    let read = |p: &PyReadonlyArray2<f64>| -> PyResult<Poly> {
        if p.as_array().ncols() < 2 {
            return Err(pyo3::exceptions::PyValueError::new_err("expected xy rings"));
        }
        Ok(ring_of(p))
    };
    let mut paved = FrontageCoverage::new(x0, y0, x1, y1);
    let mut shapes = Vec::new();
    for (p, holes, mat) in areas {
        let ring = read(&p)?;
        let mut voids = FrontageCoverage::new(x0, y0, x1, y1);
        for h in holes {
            for q in convex_parts(&read(&h)?) {
                voids.reserve_piece(&q);
            }
        }
        shapes.push((area2(&ring).abs(), ring, mat, voids));
    }
    shapes.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.2.cmp(&b.2)));
    let mut faces = Vec::new();
    for (_, ring, mat, voids) in shapes {
        for q in convex_parts(&ring) {
            let Some(q) = rect(&q, bounds) else {
                continue;
            };
            let mut parts = vec![q.clone()];
            for planes in [&*reserved, &voids]
                .into_iter()
                .flat_map(|c| c.nearby(&q).into_iter().map(|i| &c.shapes[i].planes))
            {
                parts = parts
                    .into_iter()
                    .flat_map(|p| subtract(&p, planes))
                    .collect();
                if parts.is_empty() {
                    break;
                }
            }
            for part in parts {
                paved.reserve_piece(&part);
                reserved.reserve_piece(&part);
                faces.push((part, mat));
            }
        }
    }
    let mut pts = Vec::new();
    let mut indices = BTreeMap::new();
    let mut index = |p: Pt| {
        // One shared vertex per micrometre. Deterministic across clipping orders.
        let key = ((p.0 * 1e6).round() as i64, (p.1 * 1e6).round() as i64);
        *indices.entry(key).or_insert_with(|| {
            pts.push([key.0 as f64 * 1e-6, key.1 as f64 * 1e-6]);
            pts.len() as i64 - 1
        })
    };
    let mut tri = Vec::new();
    let mut mats = Vec::new();
    let mut edges = Vec::new();
    // Subdivide in XY before sampling the road's actual drawn ground.
    const STEP: f64 = 4.;
    for (part, mat) in faces {
        let (lo, hi) = FrontageCoverage::bounds(&part);
        for ix in (lo.0 / STEP).floor() as i64..(hi.0 / STEP).ceil() as i64 {
            for iy in (lo.1 / STEP).floor() as i64..(hi.1 / STEP).ceil() as i64 {
                let x = ix as f64 * STEP;
                let y = iy as f64 * STEP;
                let Some(cell) = rect(&part, [x, y, x + STEP, y + STEP]) else {
                    continue;
                };
                if area2(&cell) <= 1e-8 {
                    continue;
                }
                let ids: Vec<_> = cell.iter().map(|&p| index(p)).collect();
                for i in 1..ids.len() - 1 {
                    if area2(&[cell[0], cell[i], cell[i + 1]]) > 1e-9 {
                        tri.push([ids[0], ids[i], ids[i + 1]]);
                        mats.push(mat);
                    }
                }
                for i in 0..ids.len() {
                    let j = (i + 1) % ids.len();
                    let (a, b) = (cell[i], cell[j]);
                    let (x, y) = ((a.0 + b.0) * 0.5, (a.1 + b.1) * 0.5);
                    if x <= x0 + 1e-6 || y <= y0 + 1e-6 || x >= x1 - 1e-6 || y >= y1 - 1e-6 {
                        continue;
                    }
                    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
                    let l = (dx * dx + dy * dy).sqrt();
                    if l > 1e-6 && !contains(&paved, (x + dy / l * 1e-4, y - dx / l * 1e-4)) {
                        edges.push([ids[i], ids[j]]);
                    }
                }
            }
        }
    }
    let points =
        Array2::from_shape_vec((pts.len(), 2), pts.into_iter().flatten().collect()).unwrap();
    let tris = Array2::from_shape_vec((tri.len(), 3), tri.into_iter().flatten().collect()).unwrap();
    Ok((points.into_pyarray(py), tris.into_pyarray(py), mats, edges))
}
