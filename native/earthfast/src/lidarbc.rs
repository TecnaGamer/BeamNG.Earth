//! Pick the newest available BC survey at each point. A projected border rejects
//! distant files before the full grid is projected. File reads stay lazy and ordered.
use numpy::{ndarray::ArrayD, IntoPyArray, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArrayDyn};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::{BTreeMap, BTreeSet};

fn limits(values: impl Iterator<Item = f64>) -> (f64, f64) {
    values.fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), v| {
        (a.min(v), b.max(v))
    })
}

#[pyfunction]
#[pyo3(signature=(lat,lon,keys,epsg,boxes,years,proj,read,used,wide,max_strip,coarse))]
pub fn bc_sample<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
    lon: PyReadonlyArrayDyn<'py, f64>,
    keys: Vec<String>,
    epsg: PyReadonlyArray1<'py, i64>,
    boxes: PyReadonlyArray2<'py, f64>,
    years: PyReadonlyArray1<'py, i64>,
    proj: &Bound<'py, PyAny>,
    read: &Bound<'py, PyAny>,
    used: Option<&Bound<'py, PyAny>>,
    wide: f64,
    max_strip: Option<f64>,
    coarse: Option<f64>,
) -> PyResult<
    Option<(
        Bound<'py, numpy::PyArrayDyn<f32>>,
        Bound<'py, numpy::PyArrayDyn<bool>>,
    )>,
> {
    let (codes, b, years) = (epsg.as_array(), boxes.as_array(), years.as_array());
    let (la, lo) = (lat.as_array(), lon.as_array());
    let n = keys.len();
    if la.shape() != lo.shape()
        || la.is_empty()
        || codes.len() != n
        || years.len() != n
        || b.dim() != (n, 4)
    {
        return Err(PyValueError::new_err("invalid lidar index or sample grid"));
    }
    let (south, north) = limits(la.iter().copied());
    let (west, east) = limits(lo.iter().copied());
    let (mut blat, mut blon) = (Vec::with_capacity(40), Vec::with_capacity(40));
    for edge in 0..4 {
        for j in 0..10 {
            let t = if j == 9 { 1. } else { j as f64 * (1. / 9.) };
            let (a, o) = match edge {
                0 => (south + (north - south) * t, west),
                1 => (south + (north - south) * t, east),
                2 => (south, west + (east - west) * t),
                _ => (north, west + (east - west) * t),
            };
            blat.push(a);
            blon.push(o);
        }
    }
    let (blat, blon) = (blat.into_pyarray(py), blon.into_pyarray(py));
    let mut candidates = Vec::new();
    let mut coords = BTreeMap::new();
    let intersects = |j: usize, x: (f64, f64), y: (f64, f64), margin: f64| {
        b[[j, 0]] < x.1 + margin
            && b[[j, 2]] > x.0 - margin
            && b[[j, 1]] < y.1 + margin
            && b[[j, 3]] > y.0 - margin
    };
    for code in codes.iter().copied().collect::<BTreeSet<_>>() {
        if !proj.call_method1("supported", (code,))?.is_truthy()? {
            continue;
        }
        let (bx, by) = proj
            .call_method1("forward", (code, &blat, &blon))?
            .extract::<(PyReadonlyArray1<f64>, PyReadonlyArray1<f64>)>()?;
        let (xr, yr) = (
            limits(bx.as_array().iter().copied()),
            limits(by.as_array().iter().copied()),
        );
        if !(0..n).any(|j| codes[j] == code && intersects(j, xr, yr, 50.)) {
            continue;
        }
        let xy = proj.call_method1("forward_grid", (code, lat.as_any(), lon.as_any()))?;
        let np = py.import("numpy")?;
        let points = |i| -> PyResult<PyReadonlyArrayDyn<'_, f64>> {
            np.call_method1("asarray", (xy.get_item(i)?,))?.extract()
        };
        let (x, y) = (points(0)?, points(1)?);
        let (x, y) = (
            x.as_array().iter().copied().collect::<Vec<_>>(),
            y.as_array().iter().copied().collect::<Vec<_>>(),
        );
        if x.len() != la.len() || y.len() != la.len() {
            return Err(PyValueError::new_err("projection returned the wrong shape"));
        }
        let (xr, yr) = (limits(x.iter().copied()), limits(y.iter().copied()));
        candidates.extend((0..n).filter(|&j| codes[j] == code && intersects(j, xr, yr, 0.)));
        coords.insert(code, (x, y));
    }
    candidates.sort_by(|&i, &j| {
        years[j]
            .cmp(&years[i])
            .then_with(|| ((b[[i, 2]] - b[[i, 0]]) > wide).cmp(&((b[[j, 2]] - b[[j, 0]]) > wide)))
            .then_with(|| keys[i].cmp(&keys[j]))
    });
    let (mut out, mut ok) = (vec![0.; la.len()], vec![false; la.len()]);
    let mut left = out.len();
    for j in candidates {
        let (x, y) = &coords[&codes[j]];
        let ids = (0..out.len())
            .filter(|&i| {
                !ok[i]
                    && x[i] >= b[[j, 0]]
                    && x[i] <= b[[j, 2]]
                    && y[i] >= b[[j, 1]]
                    && y[i] <= b[[j, 3]]
            })
            .collect::<Vec<_>>();
        if ids.is_empty() {
            continue;
        }
        let xs = ids
            .iter()
            .map(|&i| x[i])
            .collect::<Vec<_>>()
            .into_pyarray(py);
        let ys = ids
            .iter()
            .map(|&i| y[i])
            .collect::<Vec<_>>()
            .into_pyarray(py);
        let skip_strips = max_strip.is_some_and(|m| b[[j, 2]] - b[[j, 0]] > m);
        let got = read.call1((&keys[j], xs, ys, skip_strips, coarse))?;
        if got.is_none() {
            continue;
        }
        let (heights, valid) = got.extract::<(PyReadonlyArray1<f32>, PyReadonlyArray1<bool>)>()?;
        let (heights, valid) = (heights.as_array(), valid.as_array());
        if heights.len() != ids.len() || valid.len() != ids.len() {
            return Err(PyValueError::new_err(
                "lidar file returned the wrong point count",
            ));
        }
        let before = left;
        for (k, &i) in ids.iter().enumerate() {
            if valid[k] {
                out[i] = heights[k];
                ok[i] = true;
                left -= 1;
            }
        }
        if left != before {
            if let Some(files) = used {
                files.call_method1("append", (&keys[j],))?;
            }
        }
        if left == 0 {
            break;
        }
    }
    if left == out.len() {
        return Ok(None);
    }
    let shape = la.raw_dim();
    Ok(Some((
        ArrayD::from_shape_vec(shape.clone(), out)
            .unwrap()
            .into_pyarray(py),
        ArrayD::from_shape_vec(shape, ok).unwrap().into_pyarray(py),
    )))
}

/// Pixel centres on the file's rows. NumPy arange forms its machine step
/// by subtracting the stored first value from the stored second one.
fn centres(start: f64, end: f64, step: f64) -> PyResult<Vec<f64>> {
    if !(step > 0. && start.is_finite() && end.is_finite()) {
        return Err(PyValueError::new_err("invalid lidar row spacing"));
    }
    let len = ((end - start) / step).ceil() as usize;
    let second = start + step;
    let delta = second - start;
    Ok((0..len)
        .map(|j| {
            let v = match j {
                0 => start,
                1 => second,
                _ => start + j as f64 * delta,
            };
            v.floor() + 0.5
        })
        .collect())
}

#[pyfunction]
#[pyo3(signature=(reader,bounds,step,coarse,keys,codes,boxes,years,proj,fit,limit))]
pub fn bc_sparse<'py>(
    py: Python<'py>,
    reader: &Bound<'py, PyAny>,
    bounds: (f64, f64, f64, f64),
    step: f64,
    coarse: Option<f64>,
    keys: Vec<String>,
    codes: PyReadonlyArray1<'py, i64>,
    boxes: PyReadonlyArray2<'py, f64>,
    years: PyReadonlyArray1<'py, i64>,
    proj: &Bound<'py, PyAny>,
    fit: &Bound<'py, PyAny>,
    limit: f64,
) -> PyResult<(
    Bound<'py, numpy::PyArray1<f64>>,
    Bound<'py, numpy::PyArray1<f64>>,
    Bound<'py, numpy::PyArray1<f32>>,
)> {
    use numpy::ndarray::Array2;
    let (s, w, n, e) = bounds;
    let (codes, b, years) = (codes.as_array(), boxes.as_array(), years.as_array());
    if codes.len() != keys.len() || years.len() != keys.len() || b.dim() != (keys.len(), 4) {
        return Err(PyValueError::new_err("invalid survey index"));
    }
    let ll = Array2::from_shape_fn((81, 2), |(i, j)| {
        if j == 0 {
            s + (n - s) * ((i / 9) as f64 / 8.)
        } else {
            w + (e - w) * ((i % 9) as f64 / 8.)
        }
    });
    let la = ll.column(0).to_owned().into_pyarray(py);
    let lo = ll.column(1).to_owned().into_pyarray(py);
    let ll = ll.into_pyarray(py);
    let mut fits = BTreeMap::new();
    for code in codes.iter().copied().collect::<BTreeSet<_>>() {
        if !proj.call_method1("supported", (code,))?.is_truthy()? {
            continue;
        }
        let (x, y) = proj
            .call_method1("forward", (code, &la, &lo))?
            .extract::<(PyReadonlyArray1<f64>, PyReadonlyArray1<f64>)>()?;
        let (x, y) = (x.as_array(), y.as_array());
        let xr = limits(x.iter().copied());
        let yr = limits(y.iter().copied());
        let design = Array2::from_shape_fn((81, 3), |(i, j)| match j {
            0 => x[i],
            1 => y[i],
            _ => 1.,
        });
        let coef = fit
            .call1((design.into_pyarray(py), &ll))?
            .get_item(0)?
            .extract::<PyReadonlyArray2<f64>>()?;
        fits.insert(code, (xr, yr, coef.as_array().to_owned()));
    }
    let mut order = (0..keys.len()).collect::<Vec<_>>();
    order.sort_by(|&i, &j| years[j].cmp(&years[i]).then_with(|| keys[i].cmp(&keys[j])));
    let (mut lats, mut lons, mut hs) = (Vec::new(), Vec::new(), Vec::new());
    for j in order {
        let Some(&((x0, x1), (y0, y1), ref coef)) = fits.get(&codes[j]) else {
            continue;
        };
        if b[[j, 0]] > x1 || b[[j, 2]] < x0 || b[[j, 1]] > y1 || b[[j, 3]] < y0 {
            continue;
        }
        let c = reader.call_method1("_cog", (&keys[j],))?;
        if c.is_none() {
            continue;
        }
        let c = reader.call_method1("_overview", (&keys[j], c, coarse))?;
        if c.is_none() {
            continue;
        }
        let (cx0, ry1): (f64, f64) = c
            .call_method1("pixel_of", (x0.max(b[[j, 0]]), y0.max(b[[j, 1]])))?
            .extract()?;
        let (cx1, ry0): (f64, f64) = c
            .call_method1("pixel_of", (x1.min(b[[j, 2]]), y1.min(b[[j, 3]])))?
            .extract()?;
        let sx: f64 = c.getattr("sx")?.extract()?;
        let sy: f64 = c.getattr("sy")?.extract()?;
        let dr = step / sy;
        let rows = centres((ry0 / dr).ceil() * dr, ry1, dr)?;
        let cols = centres(cx0, cx1, step / 2. / sx)?;
        if rows.is_empty() || cols.is_empty() {
            continue;
        }
        let count = rows
            .len()
            .checked_mul(cols.len())
            .ok_or_else(|| PyValueError::new_err("too many lidar points"))?;
        let r = (0..count)
            .map(|i| rows[i / cols.len()])
            .collect::<Vec<_>>()
            .into_pyarray(py);
        let col = (0..count)
            .map(|i| cols[i % cols.len()])
            .collect::<Vec<_>>()
            .into_pyarray(py);
        let (values, ok) = c
            .call_method1("sample_nearest", (col, r))?
            .extract::<(PyReadonlyArray1<f32>, PyReadonlyArray1<bool>)>()?;
        let (v, ok) = (values.as_array(), ok.as_array());
        if v.len() != count || ok.len() != count {
            return Err(PyValueError::new_err("wrong lidar strip sample count"));
        }
        let x0: f64 = c.getattr("x0")?.extract()?;
        let y0: f64 = c.getattr("y0")?.extract()?;
        for i in 0..count {
            if !ok[i] || !(v[i] > limit as f32) {
                continue;
            }
            let x = x0 + cols[i % cols.len()] * sx;
            let y = y0 - rows[i / cols.len()] * sy;
            let eval = |k: usize| x * coef[[0, k]] + y * coef[[1, k]] + coef[[2, k]];
            let (la, lo) = (eval(0), eval(1));
            if la >= s && la <= n && lo >= w && lo <= e {
                lats.push(la);
                lons.push(lo);
                hs.push(v[i]);
            }
        }
    }
    Ok((
        lats.into_pyarray(py),
        lons.into_pyarray(py),
        hs.into_pyarray(py),
    ))
}
