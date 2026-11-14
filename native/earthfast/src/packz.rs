//! The revisit cache's record codec (server/revisitpack.py, 2026-11-11): one zstd frame per record, written once and
//! read on every revisit. Level 1: a far tile's record (513 KB raw) packs to 29 % in ~1.2 ms and unpacks in ~0.5 ms,
//! where the zips of before (deflate level 1, Python's zipfile) took 2.5 ms to write and 0.9 ms to read; a road
//! plan (828 KB) 1 ms against deflate's 6.7 ms. The GIL is released while it runs (the workers' other threads go on).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// data as one zstd frame (level: zstd's; 1 for the revisit cache).
#[pyfunction]
#[pyo3(signature = (data, level = 1))]
pub fn zstd_pack<'py>(py: Python<'py>, data: &[u8], level: i32) -> PyResult<Bound<'py, PyBytes>> {
    let out = py
        .detach(|| zstd::bulk::compress(data, level))
        .map_err(|e| PyValueError::new_err(format!("zstd: {e}")))?;
    Ok(PyBytes::new(py, &out))
}

/// The bytes of zstd frame data, which must unpack to exactly size bytes (a record's raw length: a damaged or
/// truncated frame is an error, never a short result). Unpacked straight into the new bytes object: no second buffer
/// and copy.
#[pyfunction]
pub fn zstd_unpack<'py>(py: Python<'py>, data: &[u8], size: usize) -> PyResult<Bound<'py, PyBytes>> {
    PyBytes::new_with(py, size, |buf: &mut [u8]| {
        let got = py
            .detach(|| zstd::bulk::Decompressor::new().and_then(|mut d| d.decompress_to_buffer(data, buf)))
            .map_err(|e| PyValueError::new_err(format!("zstd: {e}")))?;
        if got != size {
            return Err(PyValueError::new_err(format!("zstd: {got} bytes, {size} expected")));
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn round_trip() {
        let data: Vec<u8> = (0..100_000u32).flat_map(|i| (i % 251).to_le_bytes()).collect();
        let z = zstd::bulk::compress(&data, 1).unwrap();
        assert!(z.len() < data.len() / 4);
        assert_eq!(zstd::bulk::decompress(&z, data.len()).unwrap(), data);
        assert!(zstd::bulk::decompress(&z[..z.len() / 2], data.len()).is_err());
    }
}
