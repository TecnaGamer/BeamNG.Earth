//! A dirt road's surface damage limited step by step (server grading._plan; PR #811's two Python sweeps): no two
//! neighbouring samples' damage more than `cap` apart, the ends pinned at 0. A forward sweep, then a backward one, each
//! clamping a value to within cap of the one before it; the real climb is not touched (only the damage is passed).

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

pub fn clamp_steps(d: &mut [f64], cap: f64) {
    let n = d.len();
    if n < 2 || !(cap > 0.0) {
        return;
    }
    d[0] = 0.0;
    for i in 1..n {
        d[i] = d[i].max(d[i - 1] - cap).min(d[i - 1] + cap);
    }
    d[n - 1] = 0.0;
    for i in (0..n - 1).rev() {
        d[i] = d[i].max(d[i + 1] - cap).min(d[i + 1] + cap);
    }
}

/// grading._plan's damage sweeps: a copy of `damage` with neighbouring steps at most `cap`, the ends 0.
#[pyfunction]
pub fn dirt_step_clamp<'py>(py: Python<'py>, damage: PyReadonlyArray1<'py, f64>, cap: f64) -> Bound<'py, PyArray1<f64>> {
    let mut d = damage.as_array().to_vec();
    clamp_steps(&mut d, cap);
    d.into_pyarray(py)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn steps_bounded_ends_pinned() {
        let mut d = vec![0.3, -0.4, 0.5, 0.0, 0.25, -0.1];
        clamp_steps(&mut d, 0.1);
        assert_eq!(d[0], 0.0);
        assert_eq!(*d.last().unwrap(), 0.0);
        for w in d.windows(2) {
            assert!((w[1] - w[0]).abs() <= 0.1 + 1e-12);
        }
        let mut one = vec![0.7];
        clamp_steps(&mut one, 0.1);
        assert_eq!(one, vec![0.7]);   // (fewer than two samples: untouched, as the Python)
    }
}
