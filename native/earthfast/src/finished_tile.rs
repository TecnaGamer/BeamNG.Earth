//! The imagery cache's BNF1 record. Keep the encoded tile and its holes in Rust from disk to mosaic.
use std::{fs, path::Path, time::UNIX_EPOCH};

use numpy::ndarray::{Array2, Array3};
use numpy::IntoPyArray;
use pyo3::{
    prelude::*,
    types::{PyBytes, PyTuple},
};

use crate::fartex::TILE;

pub enum Pixels {
    Jpeg(Vec<u8>),
    Rgb(Array3<u8>),
}

#[pyclass(frozen)]
pub struct CachedTile {
    pub pixels: Pixels,
    pub hole: Option<Array2<bool>>,
    #[pyo3(get)]
    uniform: bool,
    #[pyo3(get)]
    relit: bool,
    #[pyo3(get)]
    cm: bool,
    pending: Vec<(u8, u32, u32)>,
}

#[pymethods]
impl CachedTile {
    #[getter]
    fn wait<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(
            py,
            self.pending
                .iter()
                .map(|&(k, x, y)| (if k == 1 { "ds" } else { "cm" }, (x, y))),
        )
    }

    #[getter]
    fn is_jpeg(&self) -> bool {
        matches!(self.pixels, Pixels::Jpeg(_))
    }

    // Only consumers inspecting a tile outside the rendering path need a Python representation.
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyAny> {
        match &self.pixels {
            Pixels::Jpeg(jpeg) => PyBytes::new(py, jpeg).into_any(),
            Pixels::Rgb(rgb) => rgb.clone().into_pyarray(py).into_any(),
        }
    }

    #[getter(hole)]
    fn get_hole<'py>(&self, py: Python<'py>) -> Option<Bound<'py, numpy::PyArray2<bool>>> {
        self.hole.as_ref().map(|h| h.clone().into_pyarray(py))
    }

    fn for_mosaic(
        slf: Bound<'_, Self>,
        last: bool,
    ) -> (Bound<'_, Self>, u8, Option<Bound<'_, Self>>) {
        let al = if slf.get().relit { 254 } else { 255 };
        let mask = (!last && slf.get().hole.is_some()).then(|| slf.clone());
        (slf, al, mask)
    }

    fn nbytes(&self) -> usize {
        let pixels = match &self.pixels {
            Pixels::Jpeg(v) => v.len(),
            Pixels::Rgb(a) => a.len(),
        };
        pixels + self.hole.as_ref().map_or(0, |a| a.len()) + 200
    }
}

fn uniform_png(data: &[u8]) -> Option<Array3<u8>> {
    // Check the dimensions before the decoder allocates the output. The cache writer always emits 8-bit RGB.
    if data.get(16..24)? != [0, 0, 1, 0, 0, 0, 1, 0] {
        return None;
    }
    let (w, h, channels, buf) = crate::texdds::decode_png(data).ok()?;
    let rgb = match channels {
        3 => buf,
        1 => buf.into_iter().flat_map(|v| [v; 3]).collect(),
        4 => buf
            .chunks_exact(4)
            .flat_map(|p| [p[0], p[1], p[2]])
            .collect(),
        _ => return None,
    };
    Array3::from_shape_vec((h, w, 3), rgb).ok()
}

fn read(path: &Path, raw: &Path) -> Option<(CachedTile, usize)> {
    let data = fs::read(path).ok()?;
    if data.get(..4)? != b"BNF1" {
        return None;
    }
    let flags = *data.get(4)?;
    let nwait = *data.get(5)? as usize;
    let size = u64::from_le_bytes(data.get(8..16)?.try_into().ok()?);
    let mtime = u64::from_le_bytes(data.get(16..24)?.try_into().ok()?);
    if let Ok(meta) = fs::metadata(raw) {
        let ns = meta
            .modified()
            .ok()?
            .duration_since(UNIX_EPOCH)
            .ok()?
            .as_nanos();
        if (meta.len(), ns) != (0, 0) && (meta.len(), ns) != (size, mtime as u128) {
            return None;
        }
    }
    let offset = 24 + nwait * 9;
    let mut pending = Vec::with_capacity(nwait);
    for item in data.get(24..offset)?.chunks_exact(9) {
        pending.push((
            item[0],
            u32::from_le_bytes(item[1..5].try_into().ok()?),
            u32::from_le_bytes(item[5..9].try_into().ok()?),
        ));
    }
    let start = offset + 4;
    let n = u32::from_le_bytes(data.get(offset..start)?.try_into().ok()?) as usize;
    let end = start.checked_add(n)?;
    let body = data.get(start..end)?;
    let uniform = flags & 1 != 0;
    let pixels = if uniform {
        Pixels::Rgb(uniform_png(body)?)
    } else if body.starts_with(&[0xff, 0xd8]) {
        Pixels::Jpeg(body.to_vec())
    } else {
        return None;
    };
    let hole = if flags & 8 != 0 {
        let bits = data.get(end..end.checked_add(TILE * TILE / 8)?)?;
        Some(Array2::from_shape_fn((TILE, TILE), |(y, x)| {
            bits[(y * TILE + x) / 8] & (128 >> (x % 8)) != 0
        }))
    } else {
        None
    };
    Some((
        CachedTile {
            pixels,
            hole,
            uniform,
            relit: flags & 2 != 0,
            cm: flags & 4 != 0,
            pending,
        },
        data.len(),
    ))
}

#[pyfunction]
pub fn read_finished_tile(
    py: Python<'_>,
    path: std::path::PathBuf,
    raw: std::path::PathBuf,
) -> Option<(CachedTile, usize)> {
    py.detach(|| read(&path, &raw))
}
