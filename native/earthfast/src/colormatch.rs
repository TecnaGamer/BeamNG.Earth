//! Regional flight-seam analysis. The worker's duty cycle is observed between filter passes.
use crate::dm::Dm;
use numpy::ndarray::{s, Array2, Array3, ArrayView2, ArrayViewMut3};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::{exceptions::PyValueError, prelude::*};

pub(crate) fn rf_pass(mut x: ArrayViewMut3<f32>, cut: ArrayView2<bool>, a: f32) {
    let (rows, cols, channels) = x.dim();
    for r in 0..rows {
        for c in 1..cols {
            let f = if cut[[r, c]] { 0. } else { a };
            for k in 0..channels {
                let cur = x[[r, c, k]];
                x[[r, c, k]] = cur + f * (x[[r, c - 1, k]] - cur);
            }
        }
        for c in (0..cols.saturating_sub(1)).rev() {
            let f = if cut[[r, c + 1]] { 0. } else { a };
            for k in 0..channels {
                let cur = x[[r, c, k]];
                x[[r, c, k]] = cur + f * (x[[r, c + 1, k]] - cur);
            }
        }
    }
}

struct Cuts {
    horizontal: Array2<bool>,
    vertical: Array2<bool>,
}

impl Cuts {
    fn new(h: usize, w: usize, cut: Option<ArrayView2<bool>>) -> PyResult<Self> {
        if cut.is_some_and(|m| m.dim() != (h, w)) {
            return Err(PyValueError::new_err("cut shape differs from image"));
        }
        let horizontal = Array2::from_shape_fn((h, w), |(y, x)| {
            x > 0 && cut.is_some_and(|m| m[[y, x]] || m[[y, x - 1]])
        });
        let vertical = Array2::from_shape_fn((w, h), |(x, y)| {
            y > 0 && cut.is_some_and(|m| m[[y, x]] || m[[y - 1, x]])
        });
        Ok(Self {
            horizontal,
            vertical,
        })
    }

    fn blur(
        &self,
        py: Python<'_>,
        mut image: Array3<f32>,
        sigma: f64,
        iters: i32,
        pace: &Bound<'_, PyAny>,
    ) -> PyResult<Array3<f32>> {
        if sigma == 0. && iters > 0 {
            return Err(pyo3::exceptions::PyZeroDivisionError::new_err(
                "sigma is zero",
            ));
        }
        for i in 0..iters {
            let s = sigma * 3f64.sqrt() * 2f64.dpowf((iters - i - 1) as f64)
                / (4f64.dpowf(iters as f64) - 1.).sqrt();
            let a = (-2f64.sqrt() / s).dexp() as f32;
            for (axis, cut) in [&self.horizontal, &self.vertical].into_iter().enumerate() {
                pace.call0()?;
                // Traverse short row groups so large cut fields still honour the worker's duty cycle.
                let mut view = if axis == 0 {
                    image.view_mut()
                } else {
                    image.view_mut().permuted_axes([1, 0, 2])
                };
                for r in (0..view.dim().0).step_by(64) {
                    let end = (r + 64).min(view.dim().0);
                    py.detach(|| {
                        rf_pass(
                            view.slice_mut(s![r..end, .., ..]),
                            cut.slice(s![r..end, ..]),
                            a,
                        )
                    });
                    pace.call0()?;
                }
            }
        }
        Ok(image)
    }
}

#[pyfunction]
pub fn colour_blur<'py>(
    py: Python<'py>,
    image: PyReadonlyArray3<'py, f32>,
    sigma: f64,
    iters: i32,
    pace: &Bound<'py, PyAny>,
    cut: Option<PyReadonlyArray2<'py, bool>>,
) -> PyResult<Bound<'py, PyArray3<f32>>> {
    let a = image.as_array();
    let cut = cut.as_ref().map(|m| m.as_array());
    let (h, w, _) = a.dim();
    let (cuts, data) = py.detach(|| (Cuts::new(h, w, cut), a.as_standard_layout().into_owned()));
    Ok(cuts?.blur(py, data, sigma, iters, pace)?.into_pyarray(py))
}

#[pyfunction]
pub fn colour_ratio<'py>(
    py: Python<'py>,
    u: PyReadonlyArray3<'py, f32>,
    s2: PyReadonlyArray3<'py, f32>,
    weights: PyReadonlyArray2<'py, f32>,
    sigma: f64,
    robust: usize,
    tau: f32,
    eps: f32,
    usgs: bool,
    pace: &Bound<'py, PyAny>,
    cut: Option<PyReadonlyArray2<'py, bool>>,
) -> PyResult<(
    Bound<'py, PyArray3<f32>>,
    Bound<'py, PyArray2<f32>>,
    Option<Bound<'py, PyArray3<f32>>>,
)> {
    let (u, s2, w0) = (u.as_array(), s2.as_array(), weights.as_array());
    let cut = cut.as_ref().map(|m| m.as_array());
    let (h, w, c) = u.dim();
    if c != 3 || s2.dim() != u.dim() || w0.dim() != (h, w) {
        return Err(PyValueError::new_err("invalid colour-ratio inputs"));
    }
    let log = |x: f32| (x as f64).dln() as f32;
    let cuts = py.detach(|| Cuts::new(h, w, cut))?;
    let raw = py.detach(|| Array3::from_shape_fn(u.dim(), |i| log(u[i] + eps) - log(s2[i] + eps)));
    let mut weight = w0.to_owned();
    for i in 0..=robust {
        pace.call0()?;
        let last = usgs && i == robust;
        let x = py.detach(|| {
            Array3::from_shape_fn((h, w, if last { 7 } else { 6 }), |(r, c, k)| match k {
                0..3 => u[[r, c, k]] * weight[[r, c]],
                3..6 => s2[[r, c, k - 3]] * weight[[r, c]],
                _ => weight[[r, c]],
            })
        });
        let b = cuts.blur(py, x, sigma, 3, pace)?;
        let sm = py.detach(|| {
            Array3::from_shape_fn(u.dim(), |(r, c, k)| {
                log(b[[r, c, k]] + eps * 0.25) - log(b[[r, c, k + 3]] + eps * 0.25)
            })
        });
        if i == robust {
            let lu = last.then(|| {
                py.detach(|| {
                    Array3::from_shape_fn(u.dim(), |(r, c, k)| {
                        log(b[[r, c, k]] / b[[r, c, 6]].max(1e-6) + eps)
                    })
                })
            });
            return Ok((
                sm.into_pyarray(py),
                weight.into_pyarray(py),
                lu.map(|a| a.into_pyarray(py)),
            ));
        }
        py.detach(|| {
            for ((r, c), v) in weight.indexed_iter_mut() {
                let d = |k| {
                    let e: f32 = raw[[r, c, k]] - sm[[r, c, k]];
                    e * e
                };
                let q = ((d(0) + d(1) + d(2)) / 3.0).sqrt() / tau;
                *v = w0[[r, c]] * ((-0.5f32 * (q * q)) as f64).dexp() as f32;
            }
        });
    }
    unreachable!()
}

fn dilate(mask: ArrayView2<bool>) -> Array2<bool> {
    let (h, w) = mask.dim();
    Array2::from_shape_fn((h, w), |(r, c)| {
        (r.saturating_sub(1)..=(r + 1).min(h - 1))
            .any(|y| (c.saturating_sub(1)..=(c + 1).min(w - 1)).any(|x| mask[[y, x]]))
    })
}

#[pyfunction]
pub fn colour_dilate<'py>(
    py: Python<'py>,
    mask: PyReadonlyArray2<'py, bool>,
    radius: i32,
) -> Bound<'py, PyArray2<bool>> {
    let a = mask.as_array();
    py.detach(|| (0..radius).fold(a.to_owned(), |m, _| dilate(m.view())))
        .into_pyarray(py)
}

// Each component is identified by the index of its first cell in row-major nonzero order.
#[pyfunction]
pub fn seam_components<'py>(
    py: Python<'py>,
    mask: PyReadonlyArray2<'py, bool>,
    pace: &Bound<'py, PyAny>,
) -> PyResult<(
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
)> {
    let m = mask.as_array();
    let (h, w) = m.dim();
    let (ys, xs, indices) = py.detach(|| {
        let (mut ys, mut xs) = (Vec::new(), Vec::new());
        let indices = Array2::from_shape_fn((h, w), |(r, c)| {
            if !m[[r, c]] {
                return usize::MAX;
            }
            let k = ys.len();
            ys.push(r as i64);
            xs.push(c as i64);
            k
        });
        (ys, xs, indices)
    });
    fn root(p: &mut [usize], mut i: usize) -> usize {
        while p[i] != i {
            p[i] = p[p[i]];
            i = p[i];
        }
        i
    }
    let mut parent: Vec<_> = (0..ys.len()).collect();
    for start in (0..ys.len()).step_by(4096) {
        pace.call0()?;
        py.detach(|| {
            for i in start..(start + 4096).min(ys.len()) {
                let (r, c) = (ys[i], xs[i]);
                for (dy, dx) in [(0, 1), (1, -1), (1, 0), (1, 1)] {
                    if let Some(&j) = indices.get([(r + dy) as usize, (c + dx) as usize]) {
                        if j == usize::MAX {
                            continue;
                        }
                        let (a, b) = (root(&mut parent, i), root(&mut parent, j));
                        parent[a.max(b)] = a.min(b);
                    }
                }
            }
        });
    }
    let roots = py.detach(|| {
        (0..parent.len())
            .map(|i| root(&mut parent, i) as i64)
            .collect::<Vec<_>>()
    });
    Ok((
        ys.into_pyarray(py),
        xs.into_pyarray(py),
        roots.into_pyarray(py),
    ))
}
