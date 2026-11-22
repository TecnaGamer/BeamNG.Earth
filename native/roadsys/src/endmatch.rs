//! M3 (BNE_ROADSYS=heights): a bridge chain's decks meet the approach fills the plans already have.
//!
//! The profile squares give each deck its heights from one profile per carriageway; the plans' first stage gives the
//! ground roads their own (approach fills included). Each free end of a chain (an abutment: every part there leaves
//! it to one side) is brought onto the ground the plans give just past it, lifted or dropped (within lift_max /
//! drop_max); the joints take a harmonic blend of the free ends' changes (weights 1 / the part's length); along each
//! part the change is linear by arc length, except that a drop reaches no further than the part's nearest hold (the
//! floors it holds over other roads: piecewise linear through 0 there), so a floor the squares met stays met. The
//! ground at the abutments stays the plans' (roadsys.pins_near): deck end and ground agree.
//!
//! Pure: IEEE + - * / and sqrt only, fixed order (parts as given, nodes in order of first appearance, a fixed number of
//! Gauss-Seidel sweeps).
use crate::geom::*;

pub const JOIN_M: f64 = 0.75;       // part ends this close are one node (roads.plan_bridges_joined's 0.75 m)
const SWEEPS: usize = 400;

/// Per part (points, heights) and the plans' ground just past each of its two ends (NaN: not read): the heights with
/// the chain lifted onto its abutments' ground (see the module's notes). Returns the lifts per part vertex.
pub fn lifts(parts: &[(Vec<P2>, Vec<f64>)], ends: &[(f64, f64)], lift_max: f64, drop_max: f64) -> Vec<Vec<f64>> {
    lifts_held(parts, ends, lift_max, drop_max, &vec![Vec::new(); parts.len()])
}

/// lifts() with holds per part (arc stations of the floors it holds over other roads): where a part's end is dropped,
/// the drop reaches no further than its nearest hold (piecewise linear through 0 at every hold): a floor met by the
/// profile stays met.
pub fn lifts_held(parts: &[(Vec<P2>, Vec<f64>)], ends: &[(f64, f64)], lift_max: f64, drop_max: f64, holds: &[Vec<f64>]) -> Vec<Vec<f64>> {
    // nodes: part ends merged within JOIN_M, in order of first appearance
    let mut nodes: Vec<P2> = Vec::new();
    let mut count: Vec<usize> = Vec::new();
    let mut of: Vec<[usize; 2]> = Vec::with_capacity(parts.len());
    let mut dirs: Vec<Vec<P2>> = Vec::new();      // per node: each part's direction leaving it
    for (pts, _z) in parts {
        let mut ix = [usize::MAX; 2];
        if pts.len() >= 2 {
            let m = pts.len();
            for (k, (p, q)) in [(pts[0], pts[1]), (pts[m - 1], pts[m - 2])].into_iter().enumerate() {
                let found = nodes.iter().position(|r| norm(sub(*r, p)) <= JOIN_M);
                let n = match found { Some(n) => n, None => { nodes.push(p); count.push(0); dirs.push(Vec::new()); nodes.len() - 1 } };
                count[n] += 1;
                dirs[n].push(unit(sub(q, p)).unwrap_or([1.0, 0.0]));
                ix[k] = n;
            }
        }
        of.push(ix);
    }
    // a node is an abutment (free) when every part there leaves it to one side (one part, or twins merging onto the
    // ground road there); a joint has parts on both sides
    let free: Vec<bool> = dirs.iter().map(|ds| ds.iter().all(|a| ds.iter().all(|b| dot(*a, *b) > 0.0))).collect();
    let nn = nodes.len();
    // the free ends' shortfalls (a node with one part end: an abutment)
    let mut fixed = vec![false; nn];
    let mut c = vec![0.0f64; nn];
    for (i, (pts, z)) in parts.iter().enumerate() {
        if pts.len() < 2 || z.len() != pts.len() { continue; }
        for k in 0..2 {
            let n = of[i][k];
            if !free[n] { continue; }
            let g = if k == 0 { ends[i].0 } else { ends[i].1 };
            let zz = if k == 0 { z[0] } else { z[z.len() - 1] };
            let v = if g.is_finite() { clampf(g - zz, -drop_max, lift_max) } else { 0.0 };
            // (twins at one abutment: the most any of them asks)
            if !fixed[n] || absf(v) > absf(c[n]) { c[n] = v; }
            fixed[n] = true;
        }
    }
    if fixed.iter().all(|f| !f) || c.iter().all(|&v| v == 0.0) {
        return parts.iter().map(|(p, _)| vec![0.0; p.len()]).collect();
    }
    // joints: harmonic over the chain's graph (weights 1 / length), Gauss-Seidel in node order
    let len: Vec<f64> = parts.iter().map(|(p, _)| { let mut l = 0.0; for w in p.windows(2) { l += norm(sub(w[1], w[0])); } l.max(1e-3) }).collect();
    for _ in 0..SWEEPS {
        for n in 0..nn {
            if fixed[n] { continue; }
            let (mut sw, mut sv) = (0.0f64, 0.0f64);
            for (i, ix) in of.iter().enumerate() {
                if ix[0] == usize::MAX { continue; }
                for k in 0..2 {
                    if ix[k] != n { continue; }
                    let o = ix[1 - k];
                    if o == n { continue; }
                    let w = 1.0 / len[i];
                    sw += w;
                    sv += w * c[o];
                }
            }
            if sw > 0.0 { c[n] = sv / sw; }
        }
    }
    parts.iter().enumerate().map(|(i, (pts, _))| {
        if pts.len() < 2 { return vec![0.0; pts.len()]; }
        let (c0, c1) = (c[of[i][0]], c[of[i][1]]);
        // anchors: its two ends, and (where an end drops) 0 at each hold
        let mut anc: Vec<(f64, f64)> = vec![(0.0, c0)];
        if c0 < 0.0 || c1 < 0.0 {
            let mut hs: Vec<f64> = holds.get(i).cloned().unwrap_or_default().into_iter().filter(|&h| h > 0.0 && h < len[i]).collect();
            hs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            for h in hs { anc.push((h, 0.0)); }
        }
        anc.push((len[i], c1));
        let mut s = 0.0;
        let mut out = Vec::with_capacity(pts.len());
        let mut k = 0usize;
        for j in 0..pts.len() {
            if j > 0 { s += norm(sub(pts[j], pts[j - 1])); }
            while k + 2 < anc.len() && s > anc[k + 1].0 { k += 1; }
            let (a, b) = (anc[k], anc[k + 1]);
            let u = if b.0 > a.0 { clampf((s - a.0) / (b.0 - a.0), 0.0, 1.0) } else { 1.0 };
            out.push(a.1 + (b.1 - a.1) * u);
        }
        out
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_chain_is_lifted_onto_its_abutments_and_never_dropped() {
        // two parts joined at x = 50: the west abutment's ground 1 m over the deck end, the east one's under it
        let a = (vec![[0.0, 0.0], [25.0, 0.0], [50.0, 0.0]], vec![10.0, 10.5, 11.0]);
        let b = (vec![[50.0, 0.0], [100.0, 0.0]], vec![11.0, 10.0]);
        let l = lifts(&[a.clone(), b.clone()], &[(11.0, f64::NAN), (f64::NAN, 9.0)], 5.0, 0.0);
        assert!((l[0][0] - 1.0).abs() < 1e-9);
        assert!((l[1][1] - 0.0).abs() < 1e-9, "never dropped: {:?}", l);
        // the joint: harmonic, weights 1/50 both sides -> the mean 0.5, the same on both parts
        assert!((l[0][2] - 0.5).abs() < 1e-6 && (l[1][0] - 0.5).abs() < 1e-6, "{:?}", l);
        assert!((l[0][1] - 0.75).abs() < 1e-6);
        // nothing short: no lift
        let l = lifts(&[a.clone(), b.clone()], &[(9.0, f64::NAN), (f64::NAN, 9.0)], 5.0, 0.0);
        assert!(l.iter().flatten().all(|&v| v == 0.0));
        // at most lift_max
        let l = lifts(&[a.clone(), b.clone()], &[(14.0, f64::NAN), (f64::NAN, 9.0)], 1.0, 0.0);
        assert!((l[0][0] - 1.0).abs() < 1e-9);
        // twin decks merging onto one abutment: both free there, lifted alike
        let c = (vec![[0.0, 3.0], [50.0, 0.5]], vec![10.0, 11.0]);
        let d = (vec![[0.0, -3.0], [50.0, 0.0]], vec![10.0, 11.0]);
        let l = lifts(&[c, d], &[(f64::NAN, 11.5), (f64::NAN, 11.2)], 5.0, 0.0);
        assert!((l[0][1] - 0.5).abs() < 1e-9 && (l[1][1] - 0.5).abs() < 1e-9, "{:?}", l);
        // a drop stops at the part's hold (a floor it holds there)
        let e = (vec![[0.0, 0.0], [20.0, 0.0], [40.0, 0.0], [100.0, 0.0]], vec![10.0, 10.5, 11.0, 12.0]);
        let l = lifts_held(&[e], &[(8.0, f64::NAN)], 5.0, 5.0, &[vec![40.0]]);
        assert!((l[0][0] + 2.0).abs() < 1e-9 && (l[0][1] + 1.0).abs() < 1e-9 && l[0][2].abs() < 1e-9 && l[0][3].abs() < 1e-9, "{:?}", l);
    }
}
