//! Colliders off the axis-aligned lattice (server/cdae.py COLLIDER_UNALIGN_M; 2026-11-22).
//!
//! BeamNG's static collision build on Windows degenerates on a regular axis-aligned grid of triangles: one 8k-triangle
//! grid mesh 145-171 ms a reloadCollision, 32k 1.1-1.4 s, the same grid jittered +-5 cm or turned 17 degrees 4 ms (the
//! V0 prototype, ~/bnge-tests/v0game r3). Several of our colliders are such grids (terrain patches' 8 m lattice, the
//! bus / rail station walk zones' 1-2 m lattice, town paving's 4 m cells, the 3dmr bridge slabs' 2 x 1 m grid when
//! the bridge runs N-S or E-W).
//!
//! Every vertex moves horizontally by a hash of its own position quantised to 1 mm (|dx|, |dy| <= amp), z unchanged:
//! two vertices at the same place move the same way, so a mesh stays closed and its seams with other meshes in the same
//! frame stay exact; the surface's height at a point changes by at most amp * its slope (1 mm at 50 %: no step). The
//! arithmetic is integer hashing and f64 adds rounded once to f32: the same bits on every platform.

use numpy::{PyArray2, PyReadonlyArray2, ToPyArray};
use pyo3::prelude::*;

#[inline]
fn mix(mut h: u64) -> u64 {
    // splitmix64's finaliser
    h ^= h >> 30;
    h = h.wrapping_mul(0xBF58476D1CE4E5B9);
    h ^= h >> 27;
    h = h.wrapping_mul(0x94D049BB133111EB);
    h ^ (h >> 31)
}

/// The (dx, dy) of a vertex at (x, y) for the amplitude amp (metres).
#[inline]
pub fn offset(x: f64, y: f64, amp: f64) -> (f64, f64) {
    let qx = (x * 1000.0).round() as i64 as u64;
    let qy = (y * 1000.0).round() as i64 as u64;
    let h = mix(qx.wrapping_mul(0x9E3779B97F4A7C15) ^ qy.wrapping_mul(0xC2B2AE3D27D4EB4F));
    let lo = (h & 0xffff_ffff) as f64 / 4294967296.0;
    let hi = (h >> 32) as f64 / 4294967296.0;
    ((lo - 0.5) * 2.0 * amp, (hi - 0.5) * 2.0 * amp)
}

/// positions (n, 3) moved off the lattice: float32 (n, 3).
#[pyfunction]
pub fn collider_unalign<'py>(py: Python<'py>, positions: PyReadonlyArray2<'py, f64>, amp: f64)
                             -> PyResult<Bound<'py, PyArray2<f32>>> {
    let p = positions.as_array();
    if p.ncols() != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err("collider_unalign: positions must be (n, 3)"));
    }
    if !(amp.is_finite() && amp >= 0.0 && amp <= 0.05) {
        return Err(pyo3::exceptions::PyValueError::new_err("collider_unalign: amp must be in [0, 0.05] m"));
    }
    let mut out = numpy::ndarray::Array2::<f32>::zeros((p.nrows(), 3));
    for i in 0..p.nrows() {
        let (x, y, z) = (p[[i, 0]], p[[i, 1]], p[[i, 2]]);
        let (dx, dy) = if x.is_finite() && y.is_finite() { offset(x, y, amp) } else { (0.0, 0.0) };
        out[[i, 0]] = (x + dx) as f32;
        out[[i, 1]] = (y + dy) as f32;
        out[[i, 2]] = z as f32;
    }
    Ok(out.to_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(collider_unalign, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_coincident_and_fixed() {
        let (dx, dy) = offset(12.5, -40.0, 0.002);
        assert!(dx.abs() <= 0.002 && dy.abs() <= 0.002);
        assert_eq!(offset(12.5, -40.0, 0.002), offset(12.5 + 1e-5, -40.0, 0.002)); // (same mm: same move)
        assert_ne!(offset(12.5, -40.0, 0.002), offset(14.5, -40.0, 0.002));
        // the reference bits (a change here moves every collider: bump cdae's COLLIDER_FORMAT)
        // (an independent Python reimplementation gave these, 2026-11-22)
        let (a, b) = offset(1.0, 2.0, 0.002);
        assert_eq!((a.to_bits(), b.to_bits()), (0xbf598a621374bc6b, 0xbf46a20fe6e978d5));
        let (c, d) = offset(-123.456, 7890.5, 0.002);
        assert_eq!((c.to_bits(), d.to_bits()), (0x3f431a47472b020c, 0xbf44ff047851eb85));
    }
}
