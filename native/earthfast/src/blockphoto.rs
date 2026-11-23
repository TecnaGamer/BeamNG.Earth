//! Composition of the terrain base photograph. The imagery and road providers run on
//! the requesting thread; a canopy copy follows edits to the ground's RGB.
use numpy::{
    ndarray::{s, Array3},
    IntoPyArray, PyArray3, PyArrayMethods, PyReadonlyArray3, PyUntypedArrayMethods,
};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyTuple};

fn paint<'py>(
    py: Python<'py>,
    rgb: Bound<'py, PyArray3<u8>>,
    canopy: Option<Bound<'py, PyArray3<u8>>>,
    painter: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyArray3<u8>>, Option<Bound<'py, PyArray3<u8>>>)> {
    let before = canopy.as_ref().map(|_| {
        let rgb = rgb.readonly();
        let view = rgb.as_array();
        py.detach(|| view.to_owned()).into_pyarray(py)
    });
    let rgb = painter.call1((rgb,))?.cast_into::<PyArray3<u8>>()?;
    let canopy = if let (Some(before), Some(canopy)) = (before, canopy) {
        Some(
            py.import("earthfast")?
                .call_method1("photo_keep_painted", (before, &rgb, canopy))?
                .cast_into()?,
        )
    } else {
        None
    };
    Ok((rgb, canopy))
}

#[pyfunction]
#[pyo3(signature=(rgb, canopy, px, prepare, detail))]
pub fn block_base_photo<'py>(
    py: Python<'py>,
    rgb: Bound<'py, PyArray3<u8>>,
    canopy: Option<Bound<'py, PyArray3<u8>>>,
    px: usize,
    prepare: &Bound<'py, PyAny>,
    detail: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyArray3<u8>>, Option<Bound<'py, PyArray3<u8>>>)> {
    let (rgb, canopy) = paint(py, rgb, canopy, prepare)?;
    let resize = |a: Bound<'py, PyArray3<u8>>| {
        let shape = a.shape();
        if shape[0] == px && shape[1] == px {
            Ok(a)
        } else if shape[0] == shape[1] {
            crate::fartex::photo_resize(py, a.readonly(), px)
        } else {
            Ok(py.import("surface")?.call_method1("enlarge", (a, px))?.cast_into()?)
        }
    };
    paint(py, resize(rgb)?, canopy.map(resize).transpose()?, detail)
}

struct Part {
    row: usize,
    col: usize,
    src_row: usize,
    src_col: usize,
    height: usize,
    width: usize,
    rgb: Py<PyArray3<u8>>,
    canopy: Option<Py<PyArray3<u8>>>,
}

#[pyclass(frozen)]
pub struct BlockPhotoOverlap {
    size: usize,
    covered_pixels: usize,
    parts: Vec<Part>,
}

#[pymethods]
impl BlockPhotoOverlap {
    #[new]
    fn new(
        py: Python<'_>,
        recent: Vec<Bound<'_, PyTuple>>,
        aid: i64,
        cx: f64,
        cy: f64,
        n: usize,
        scale: f64,
    ) -> PyResult<Self> {
        let mut parts = Vec::new();
        let count = n
            .checked_mul(n)
            .ok_or_else(|| PyValueError::new_err("invalid photo size"))?;
        let mut covered = vec![false; count];
        for entry in recent {
            if entry.get_item(0)?.extract::<i64>()? != aid {
                continue;
            }
            let rgb = entry.get_item(3)?.cast_into::<PyArray3<u8>>()?;
            if rgb.shape() != [n, n, 3] {
                continue;
            }
            let ox = entry.get_item(1)?.extract::<f64>()?;
            let oy = entry.get_item(2)?.extract::<f64>()?;
            let dx = ((cx - ox) * scale).round_ties_even();
            let dy = ((oy - cy) * scale).round_ties_even();
            if dx.abs() >= n as f64 || dy.abs() >= n as f64 {
                continue;
            }
            let p = Part {
                row: (-dy).max(0.) as usize,
                col: (-dx).max(0.) as usize,
                src_row: dy.max(0.) as usize,
                src_col: dx.max(0.) as usize,
                height: n - dy.abs() as usize,
                width: n - dx.abs() as usize,
                rgb: rgb.unbind(),
                canopy: if entry.len() > 4 {
                    entry.get_item(4)?.extract()?
                } else {
                    None
                },
            };
            if p.canopy.as_ref().is_some_and(|v| v.bind(py).shape() != [n, n, 3]) {
                return Err(PyValueError::new_err("invalid cached canopy shape"));
            }
            for y in p.row..p.row + p.height {
                covered[y * n + p.col..y * n + p.col + p.width].fill(true);
            }
            parts.push(p);
        }
        Ok(Self {
            size: n,
            covered_pixels: covered.iter().filter(|&&v| v).count(),
            parts,
        })
    }

    #[getter]
    fn complete(&self) -> bool {
        self.covered_pixels == self.size * self.size
    }
    #[getter]
    fn share(&self) -> f64 {
        self.covered_pixels as f64 / (self.size * self.size) as f64
    }

    fn paste<'py>(
        &self,
        py: Python<'py>,
        fill: Option<PyReadonlyArray3<'py, u8>>,
    ) -> PyResult<(Bound<'py, PyArray3<u8>>, Option<Bound<'py, PyArray3<u8>>>)> {
        let n = self.size;
        if fill.as_ref().is_some_and(|a| a.shape() != [n, n, 3]) || (fill.is_none() && !self.complete()) {
            return Err(PyValueError::new_err("incomplete photo overlap needs a full-size fill"));
        }
        let fill = fill.as_ref().map(|v| v.as_array());
        let mut rgb = py.detach(|| fill.map_or_else(|| Array3::zeros((n, n, 3)), |a| a.to_owned()));
        let mut canopy = self.parts.iter().any(|p| p.canopy.is_some()).then(|| rgb.clone());
        for p in &self.parts {
            let ground = p.rgb.bind(py).readonly();
            let top = p.canopy.as_ref().map(|a| a.bind(py).readonly());
            let ground = ground.as_array();
            let top = top.as_ref().map_or(ground, |a| a.as_array());
            py.detach(|| {
                let src = s![p.src_row..p.src_row + p.height, p.src_col..p.src_col + p.width, ..];
                let dst = s![p.row..p.row + p.height, p.col..p.col + p.width, ..];
                rgb.slice_mut(dst).assign(&ground.slice(src));
                if let Some(canopy) = canopy.as_mut() {
                    canopy.slice_mut(dst).assign(&top.slice(src));
                }
            });
        }
        Ok((rgb.into_pyarray(py), canopy.map(|a| a.into_pyarray(py))))
    }
}

#[pyfunction]
pub fn block_rgba<'py>(
    py: Python<'py>,
    rgb: PyReadonlyArray3<'py, u8>,
    alpha: u8,
) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let a = rgb.as_array();
    let (h, w, n) = a.dim();
    if n != 3 {
        return Err(PyValueError::new_err("terrain base requires RGB"));
    }
    Ok(py
        .detach(|| Array3::from_shape_fn((h, w, 4), |(y, x, c)| if c == 3 { alpha } else { a[[y, x, c]] }))
        .into_pyarray(py))
}
