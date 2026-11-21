//! RGB JPEGs for base photos, finished imagery and place previews.
use numpy::PyReadonlyArray3;
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::{ffi::CStr, ptr};
use turbojpeg_sys as tj;

struct Compressor(tj::tjhandle);
impl Drop for Compressor {
    fn drop(&mut self) {
        unsafe {
            tj::tjDestroy(self.0);
        }
    }
}

struct Output(*mut u8);
impl Drop for Output {
    fn drop(&mut self) {
        unsafe { tj::tjFree(self.0) }
    }
}

fn error(handle: tj::tjhandle) -> String {
    unsafe {
        CStr::from_ptr(tj::tjGetErrorStr2(handle))
            .to_string_lossy()
            .into_owned()
    }
}

#[pyfunction]
pub fn photo_jpeg<'py>(
    py: Python<'py>,
    rgb: PyReadonlyArray3<'py, u8>,
    quality: i32,
    full_chroma: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    let rgb = rgb.as_array();
    let (h, w, channels) = rgb.dim();
    if h == 0 || w == 0 || h > 65500 || w > 65500 || channels != 3 || !(1..=100).contains(&quality)
    {
        return Err(PyValueError::new_err(
            "photo_jpeg: invalid RGB dimensions or quality",
        ));
    }
    // One handle and output buffer per call. The input borrow survives the synchronous encode.
    let (output, size) = py
        .detach(|| {
            let rgb = rgb.as_standard_layout();
            unsafe {
                let handle = tj::tjInitCompress();
                if handle.is_null() {
                    return Err(error(handle));
                }
                let handle = Compressor(handle);
                let subsampling = if full_chroma {
                    tj::TJSAMP_TJSAMP_444
                } else {
                    tj::TJSAMP_TJSAMP_420
                };
                let mut buffer = ptr::null_mut();
                let mut size = 0;
                let rc = tj::tjCompress2(
                    handle.0,
                    rgb.as_ptr(),
                    w as i32,
                    0,
                    h as i32,
                    tj::TJPF_TJPF_RGB as i32,
                    &mut buffer,
                    &mut size,
                    subsampling as i32,
                    quality,
                    tj::TJFLAG_ACCURATEDCT as i32,
                );
                let buffer = Output(buffer);
                if rc != 0 {
                    return Err(error(handle.0));
                }
                Ok((buffer, size as usize))
            }
        })
        .map_err(PyOSError::new_err)?;
    Ok(PyBytes::new(py, unsafe {
        std::slice::from_raw_parts(output.0, size)
    }))
}

// The allocation returned by tjCompress2 is independent of the compressor. It has one owner.
unsafe impl Send for Output {}
