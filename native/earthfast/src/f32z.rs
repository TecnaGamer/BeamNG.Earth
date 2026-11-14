//! Compressed float32 rasters for the server's disk caches (dem.py's decoded DEM mirror with BNGE_DEM_MIRROR_ZSTD=1):
//! the four bytes of every value split into four planes (the exponent bytes of neighbouring heights agree, so the
//! planes compress far better than the interleaved floats), then one zstd frame. A 512² decoded DEM tile (1 MB as an
//! .npy) packs to ~11 % and unpacks in ~3 ms; the .npy mirror was ~3.7 GB of SSD writes in a 13 km first visit
//! (2026-11-04). Lossless: f32_unpack gives back the same bits.

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

const MAGIC: &[u8; 4] = b"F32Z";

pub fn pack(v: &[f32], level: i32) -> Result<Vec<u8>, String> {
    let n = v.len();
    let mut planes = vec![0u8; 4 * n];
    for (i, x) in v.iter().enumerate() {
        let b = x.to_le_bytes();
        for k in 0..4 {
            planes[k * n + i] = b[k];
        }
    }
    let body = zstd::bulk::compress(&planes, level).map_err(|e| format!("zstd: {e}"))?;
    let mut out = Vec::with_capacity(12 + body.len());
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&(n as u64).to_le_bytes());
    out.extend_from_slice(&body);
    Ok(out)
}

pub fn unpack(data: &[u8]) -> Result<Vec<f32>, String> {
    if data.len() < 12 || &data[..4] != MAGIC {
        return Err("not an F32Z raster".into());
    }
    let n = u64::from_le_bytes(data[4..12].try_into().unwrap()) as usize;
    let planes = zstd::bulk::decompress(&data[12..], 4 * n).map_err(|e| format!("zstd: {e}"))?;
    if planes.len() != 4 * n {
        return Err("truncated F32Z raster".into());
    }
    Ok((0..n).map(|i| f32::from_le_bytes([planes[i], planes[n + i], planes[2 * n + i], planes[3 * n + i]])).collect())
}

/// dem.py: a float32 raster (flattened) packed (level: zstd's).
#[pyfunction]
#[pyo3(signature = (a, level = 1))]
pub fn f32_pack<'py>(py: Python<'py>, a: PyReadonlyArray1<'py, f32>, level: i32) -> PyResult<Bound<'py, PyBytes>> {
    let v = a.as_slice().map_err(|e| PyValueError::new_err(format!("{e}")))?.to_vec();
    let out = py.detach(|| pack(&v, level)).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &out))
}

/// dem.py: a packed raster back (flattened float32).
#[pyfunction]
pub fn f32_unpack<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyArray1<f32>>> {
    let v = py.detach(|| unpack(data)).map_err(PyValueError::new_err)?;
    Ok(PyArray1::from_vec(py, v))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_bits() {
        let v: Vec<f32> = (0..4096).map(|i| 100.0 + (i as f32 * 0.37).sin() * 3.0 + if i % 97 == 0 { f32::NAN } else { 0.0 })
            .collect();
        let p = pack(&v, 1).unwrap();
        assert!(p.len() < 4 * v.len());
        let back = unpack(&p).unwrap();
        assert_eq!(back.len(), v.len());
        for (a, b) in v.iter().zip(back.iter()) {
            assert_eq!(a.to_bits(), b.to_bits());
        }
        assert!(unpack(b"nope").is_err());
    }
}
