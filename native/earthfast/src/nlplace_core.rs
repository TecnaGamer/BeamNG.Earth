//! Source-position roadside feet: native clearance, deck rejection and tile ownership.
//! The caller supplies coordinates transformed by the existing geodesy kernels and
//! sampled ground heights. Never slide a restriction to a different carriageway.
use std::collections::HashSet;

#[derive(Clone, Debug)]
pub struct Candidate {
    pub id: String,
    pub xy: [f64; 2],
    pub z: f64,
    pub travel: [f64; 2],
    pub radius: f64,
    pub deck_margin: f64,
}

fn distance2(p: [f64; 2], a: [f64; 2], b: [f64; 2]) -> f64 {
    let ab = [b[0] - a[0], b[1] - a[1]];
    let length2 = ab[0] * ab[0] + ab[1] * ab[1];
    let t = if length2 > 0.0 {
        (((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / length2).clamp(0.0, 1.0)
    } else { 0.0 };
    let d = [p[0] - a[0] - t * ab[0], p[1] - a[1] - t * ab[1]];
    d[0] * d[0] + d[1] * d[1]
}

fn clear(c: &Candidate, poly: &[[f64; 2]]) -> bool {
    let mut inside = false;
    for i in 0..poly.len() {
        let (a, b) = (poly[i], poly[(i + 1) % poly.len()]);
        if distance2(c.xy, a, b) <= c.radius * c.radius { return false; }
        if (a[1] > c.xy[1]) != (b[1] > c.xy[1]) {
            let x = a[0] + (c.xy[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]);
            if c.xy[0] < x { inside = !inside; }
        }
    }
    !inside
}

pub fn place(candidates: &[Candidate], polygons: &[Vec<[f64; 2]>], decks: &[[f64; 5]],
             bounds: [f64; 4]) -> Result<Vec<usize>, &'static str> {
    if !bounds.iter().all(|v| v.is_finite() && v.abs() < 1e9)
        || bounds[0] >= bounds[2] || bounds[1] >= bounds[3] { return Err("tile bounds"); }
    let mut boxes = Vec::new();
    for poly in polygons {
        if poly.len() < 3 || !poly.iter().flatten().all(|v| v.is_finite() && v.abs() < 1e9) {
            return Err("pavement polygon");
        }
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for p in poly { b[0] = b[0].min(p[0]); b[1] = b[1].min(p[1]); b[2] = b[2].max(p[0]); b[3] = b[3].max(p[1]); }
        boxes.push(b);
    }
    if decks.iter().any(|d| !d.iter().all(|v| v.is_finite() && v.abs() < 1e9) || d[4] < 0.0) {
        return Err("deck geometry");
    }
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for (i, c) in candidates.iter().enumerate() {
        let n2 = c.travel[0] * c.travel[0] + c.travel[1] * c.travel[1];
        if c.id.is_empty() || c.id.len() > 128 || seen.contains(c.id.as_str())
            || ![c.xy[0], c.xy[1], c.z, c.travel[0], c.travel[1], c.radius, c.deck_margin].iter().all(|v| v.is_finite())
            || !(0.5..=1.5).contains(&n2) || !(0.0..=2.0).contains(&c.radius)
            || !(0.0..=5.0).contains(&c.deck_margin)
            || c.xy[0] < bounds[0] || c.xy[0] >= bounds[2] || c.xy[1] < bounds[1] || c.xy[1] >= bounds[3] { continue; }
        if polygons.iter().zip(&boxes).any(|(poly, b)| {
            c.xy[0] + c.radius >= b[0] && c.xy[0] - c.radius <= b[2]
                && c.xy[1] + c.radius >= b[1] && c.xy[1] - c.radius <= b[3] && !clear(c, poly)
        }) { continue; }
        if decks.iter().any(|d| distance2(c.xy, [d[0], d[1]], [d[2], d[3]])
            < (d[4] + c.deck_margin) * (d[4] + c.deck_margin)) { continue; }
        seen.insert(c.id.as_str());
        out.push(i);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn candidate(x: f64, y: f64) -> Candidate {
        Candidate { id: "source".into(), xy: [x, y], z: 0.0, travel: [0.0, 1.0], radius: 0.22, deck_margin: 1.0 }
    }
    fn road() -> Vec<[f64; 2]> { vec![[-5.0,-5.0],[5.0,-5.0],[5.0,5.0],[-5.0,5.0],[-5.0,-5.0]] }
    const BOX: [f64; 4] = [-20.0,-20.0,20.0,20.0];

    #[test] fn whole_foot_clears_road() {
        for (y, expected) in [(0.0,false),(5.1,false),(5.3,true)] {
            assert_eq!(!place(&[candidate(0.0,y)], &[road()], &[], BOX).unwrap().is_empty(), expected);
        }
    }
    #[test] fn half_open_owner() {
        assert!(place(&[candidate(0.0,0.0)], &[], &[], [-10.0,-10.0,0.0,10.0]).unwrap().is_empty());
        assert_eq!(place(&[candidate(0.0,0.0)], &[], &[], [0.0,-10.0,10.0,10.0]).unwrap(), vec![0]);
    }
    #[test] fn failed_first_copy_does_not_hide_valid_copy() {
        assert_eq!(place(&[candidate(0.0,0.0),candidate(0.0,10.0),candidate(1.0,10.0)], &[road()], &[], BOX).unwrap(), vec![1]);
    }
    #[test] fn bridge_context_never_falls_to_terrain() {
        assert!(place(&[candidate(0.0,0.0)], &[], &[[-10.0,0.0,10.0,0.0,3.0]], BOX).unwrap().is_empty());
        assert_eq!(place(&[candidate(0.0,5.0)], &[], &[[-10.0,0.0,10.0,0.0,3.0]], BOX).unwrap(), vec![0]);
    }
    #[test] fn invalid_ground_and_direction_rejected() {
        let mut c = candidate(0.0,0.0);c.z=f64::NAN;
        assert!(place(&[c], &[], &[], BOX).unwrap().is_empty());
        let mut c = candidate(0.0,0.0);c.travel=[0.0,0.0];
        assert!(place(&[c], &[], &[], BOX).unwrap().is_empty());
    }
    #[test] fn malformed_context_rejects_whole_call() {
        assert!(place(&[], &[vec![[f64::NAN,0.0];3]], &[], BOX).is_err());
        assert!(place(&[], &[], &[], [0.0,0.0,0.0,1.0]).is_err());
    }
}
