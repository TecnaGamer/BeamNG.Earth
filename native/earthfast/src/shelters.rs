//! Explicitly mapped transit shelters and their nominal roadside poses.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyfunction]
pub fn shelter_is_shelter(class: &str, shelter: &str, amenity: &str, highway: &str, kind: &str) -> bool {
    if matches!(shelter, "no" | "none" | "false" | "0") {
        return false;
    }
    let stop = class == "bus_stop" || highway == "bus_stop" || amenity == "bus_stop";
    let transit = amenity == "shelter" && matches!(kind,
        "public_transport" | "public_transit" | "bus_stop" | "bus" | "bus_shelter" | "transit");
    transit || (stop && matches!(shelter, "yes" | "true" | "1"))
}

#[pyfunction]
pub fn shelter_pose(x: f64, y: f64, segments: PyReadonlyArray2<f64>, widths: PyReadonlyArray1<f64>,
                    side_hint: f64, fallback_yaw: f64, near: f64, padding: f64, front: f64)
                    -> PyResult<(f64, f64, f64)> {
    let segments = segments.as_array();
    let widths = widths.as_array();
    if segments.ncols() != 4 || segments.nrows() != widths.len()
        || !segments.iter().chain(widths.iter()).all(|v| v.is_finite())
        || widths.iter().any(|v| *v < 0.0)
        || ![x, y, side_hint, fallback_yaw, near, padding, front].iter().all(|v| v.is_finite())
        || near < 0.0 || padding < 0.0 || front < 0.0 {
        return Err(PyValueError::new_err("shelter_pose: invalid road segments or placement parameters"));
    }
    let mut best = f64::INFINITY;
    let mut hit = None;
    for (i, row) in segments.outer_iter().enumerate() {
        let (dx, dy) = (row[2] - row[0], row[3] - row[1]);
        let length = dx.hypot(dy);
        if length < 1e-6 { continue; }
        let t = (((x - row[0]) * dx + (y - row[1]) * dy) / (dx * dx + dy * dy)).clamp(0.0, 1.0);
        let (cx, cy) = (row[0] + t * dx, row[1] + t * dy);
        let d2 = (x - cx).powi(2) + (y - cy).powi(2);
        if d2 < best {
            best = d2;
            hit = Some((cx, cy, dx / length, dy / length, widths[i]));
        }
    }
    let Some((cx, cy, tx, ty, half)) = hit.filter(|_| best <= near * near) else {
        return Ok((x, y, fallback_yaw));
    };
    let side = (x - cx) * -ty + (y - cy) * tx;
    let side = if side.abs() < 1.0 { side_hint.signum() } else { side.signum() };
    let away = (-ty * side, tx * side);
    let dist = (x - cx) * away.0 + (y - cy) * away.1;
    let (fx, fy) = if dist < half + padding {
        (cx + away.0 * (half + padding), cy + away.1 * (half + padding))
    } else { (x, y) };
    Ok((fx + front * away.0, fy + front * away.1,
        away.1.atan2(away.0).to_degrees().rem_euclid(360.0)))
}

#[cfg(test)]
mod tests {
    use super::shelter_is_shelter;
    #[test]
    fn stops_need_positive_shelter_evidence() {
        assert!(!shelter_is_shelter("bus_stop", "", "", "", ""));
        assert!(shelter_is_shelter("bus_stop", "yes", "", "", ""));
        assert!(shelter_is_shelter("", "", "shelter", "", "public_transport"));
        assert!(!shelter_is_shelter("", "no", "shelter", "", "public_transport"));
    }
}
