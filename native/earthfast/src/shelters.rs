//! Mapped transit shelters: how strong a stop's evidence is, and its nominal roadside pose.
use numpy::ndarray::{ArrayView1, ArrayView2};
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// No shelter (a negative shelter tag, or not a bus stop).
pub const NONE: u8 = 0;
/// Explicit evidence: a positive shelter tag on a stop or platform, or a public-transport amenity=shelter.
pub const EXPLICIT: u8 = 1;
/// An untagged stop or bus platform: a shelter only where its whole body clears every road (shelters.py).
pub const CANDIDATE: u8 = 2;
/// An untagged public_transport=stop_position: where the bus halts on the carriageway, never a shelter spot (the
/// Hautes-Alpes and Grenoble map every stop this way, often on a junction node: PR #869's review).
pub const ON_ROAD: u8 = 3;
/// A positive shelter tag on a stop_position: the shelter stands beside the road; a platform mapped nearby carries it.
pub const EXPLICIT_ON_ROAD: u8 = 4;

fn yes(v: &str) -> bool { matches!(v, "yes" | "true" | "1") }

#[pyfunction]
pub fn shelter_evidence(class: &str, shelter: &str, amenity: &str, highway: &str, kind: &str,
                        public_transport: &str, bus: &str) -> u8 {
    if matches!(shelter, "no" | "none" | "false" | "0") {
        return NONE;
    }
    if amenity == "shelter" && matches!(kind,
        "public_transport" | "public_transit" | "bus_stop" | "bus" | "bus_shelter" | "transit") {
        return EXPLICIT;
    }
    let stop = class == "bus_stop" || highway == "bus_stop" || amenity == "bus_stop";
    // (a platform counts as a bus one only: rail and tram platforms have their own furniture)
    let platform = public_transport == "platform" && (stop || yes(bus));
    if !stop && !platform {
        return NONE;
    }
    match (yes(shelter), public_transport == "stop_position") {
        (true, true) => EXPLICIT_ON_ROAD,
        (true, false) => EXPLICIT,
        (false, true) => ON_ROAD,
        (false, false) => CANDIDATE,
    }
}

/// (signed distance to the nearest carriageway EDGE (< 0: inside it), the centreline point, its unit tangent, the
/// half width). The nearest edge, not centreline: at a junction a narrow lane's centreline can be closer than the
/// trunk road whose kerb the stop is on (PR #889's rule).
fn nearest_edge(x: f64, y: f64, segments: &ArrayView2<f64>, widths: &ArrayView1<f64>)
                -> Option<(f64, f64, f64, f64, f64, f64)> {
    let mut best: Option<(f64, f64, f64, f64, f64, f64)> = None;
    for (i, row) in segments.outer_iter().enumerate() {
        let (dx, dy) = (row[2] - row[0], row[3] - row[1]);
        let length = dx.hypot(dy);
        if length < 1e-6 { continue; }
        let t = (((x - row[0]) * dx + (y - row[1]) * dy) / (dx * dx + dy * dy)).clamp(0.0, 1.0);
        let (cx, cy) = (row[0] + t * dx, row[1] + t * dy);
        let edge = (x - cx).hypot(y - cy) - widths[i];
        if best.map_or(true, |b| edge < b.0) {
            best = Some((edge, cx, cy, dx / length, dy / length, widths[i]));
        }
    }
    best
}

fn check(segments: &ArrayView2<f64>, widths: &ArrayView1<f64>, values: &[f64]) -> PyResult<()> {
    if segments.ncols() != 4 || segments.nrows() != widths.len()
        || !segments.iter().chain(widths.iter()).chain(values.iter()).all(|v| v.is_finite())
        || widths.iter().any(|v| *v < 0.0) {
        return Err(PyValueError::new_err("shelters: invalid road segments or placement parameters"));
    }
    Ok(())
}

/// Does (x, y) stand on a carriageway (less than margin outside its edge)? An untagged bus_stop node mapped on the
/// road itself is a stop position whatever its tags say.
#[pyfunction]
pub fn shelter_on_road(x: f64, y: f64, segments: PyReadonlyArray2<f64>, widths: PyReadonlyArray1<f64>,
                       margin: f64) -> PyResult<bool> {
    let (segments, widths) = (segments.as_array(), widths.as_array());
    check(&segments, &widths, &[x, y, margin])?;
    Ok(nearest_edge(x, y, &segments, &widths).map_or(false, |b| b.0 < margin))
}

/// (x, y, yaw, beside a road): the origin with the signed face `padding` m past the nearest carriageway edge (or
/// where it is mapped, when farther), local +x away from that road; the mapped point, fallback_yaw and false when
/// no edge is within `near`.
#[pyfunction]
pub fn shelter_pose(x: f64, y: f64, segments: PyReadonlyArray2<f64>, widths: PyReadonlyArray1<f64>,
                    side_hint: f64, fallback_yaw: f64, near: f64, padding: f64, front: f64)
                    -> PyResult<(f64, f64, f64, bool)> {
    let (segments, widths) = (segments.as_array(), widths.as_array());
    check(&segments, &widths, &[x, y, side_hint, fallback_yaw, near, padding, front])?;
    if near < 0.0 || padding < 0.0 || front < 0.0 {
        return Err(PyValueError::new_err("shelters: invalid road segments or placement parameters"));
    }
    let Some((_, cx, cy, tx, ty, half)) = nearest_edge(x, y, &segments, &widths).filter(|b| b.0 <= near) else {
        return Ok((x, y, fallback_yaw, false));
    };
    let side = (x - cx) * -ty + (y - cy) * tx;
    let side = if side.abs() < 1.0 { side_hint.signum() } else { side.signum() };
    let away = (-ty * side, tx * side);
    let dist = (x - cx) * away.0 + (y - cy) * away.1;
    let (fx, fy) = if dist < half + padding {
        (cx + away.0 * (half + padding), cy + away.1 * (half + padding))
    } else { (x, y) };
    Ok((fx + front * away.0, fy + front * away.1, away.1.atan2(away.0).to_degrees().rem_euclid(360.0), true))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn evidence_levels() {
        assert_eq!(shelter_evidence("bus_stop", "", "", "", "", "", ""), CANDIDATE);
        assert_eq!(shelter_evidence("bus_stop", "yes", "", "", "", "", ""), EXPLICIT);
        assert_eq!(shelter_evidence("bus_stop", "", "", "bus_stop", "", "stop_position", ""), ON_ROAD);
        assert_eq!(shelter_evidence("bus_stop", "yes", "", "", "", "stop_position", ""), EXPLICIT_ON_ROAD);
        assert_eq!(shelter_evidence("platform", "", "", "", "", "platform", "yes"), CANDIDATE);
        assert_eq!(shelter_evidence("platform", "", "", "", "", "platform", ""), NONE);
        assert_eq!(shelter_evidence("", "", "shelter", "", "public_transport", "", ""), EXPLICIT);
        assert_eq!(shelter_evidence("", "no", "shelter", "", "public_transport", "", ""), NONE);
        assert_eq!(shelter_evidence("bus_stop", "no", "", "", "", "", ""), NONE);
        assert_eq!(shelter_evidence("bench", "", "", "", "", "", ""), NONE);
    }
}
