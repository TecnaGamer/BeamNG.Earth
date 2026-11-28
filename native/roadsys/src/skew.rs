//! (M5, earthfast 627) The deck-end skew rule: whether a structure's free end is cut along the line it crosses, and
//! the end line's direction. The rule of roadshape's deck_shape (earthfast roadshape/decks.rs, BNE_ROADSHAPE) from
//! the deck-end skew audit (2026-11-25: every mapped bridge end of the 66-place corpus against NBI item 34 and the
//! finest aerials): a structure of SPAN_MAX_M or less (its parts end to end) ends along the nearest road, railway or
//! mapped waterway it crosses within REACH_M of the end, when that crossing is OFF_DEG or more off square (at most
//! MAX_DEG, clamped); a viaduct's ends are square. A record of the US bridge inventory (NBI item 34 skew, item 49
//! length) matched to the structure decides instead: 0-4 deg square, its angle toward the crossed line's side, 99
//! ("varies") the rule's.
//!
//! The road system's hand-over (handover.rs Ends) and the deck faces (deck.rs, part.rs's end rows) take one decision
//! per end from here, from the map's parts round it (the same cells whoever asks).
//!
//! IEEE + - * / and sqrt only, in a fixed order.
use crate::geom::*;

pub const KIND_ROAD: u8 = 0;
pub const KIND_RAIL: u8 = 1;
pub const KIND_WATER: u8 = 3;
pub const SPAN_MAX_M: f64 = 100.0;
pub const REACH_M: f64 = 80.0;
pub const NEAR_M: f64 = 1.0;                         // a crossing nearer the end than this joins it, not crossed
pub const MIN_CROSS_SIN: f64 = 0.25881904510252074;  // sin 15 deg: a line flatter than this runs along the deck
pub const OFF_COS: f64 = 0.9961946980917455;         // cos 5 deg: a crossing nearer square than this ends square
pub const MAX_COS: f64 = 0.5;                        // cos 60 deg: the end line at most this off square
pub const BEND_COS: f64 = 0.8660254037844387;        // cos 30 deg: the deck turning more before the crossing: square
pub const JOIN_M: f64 = 1.0;                         // parts' ends this near meet (handover.rs JOIN_M)
pub const APPROACH_COS: f64 = 0.9063077870366499;    // cos 25 deg: a part going on from another's end
pub const HOPS: usize = 64;

fn line_len(p: &[P2]) -> f64 { p.windows(2).map(|w| norm(sub(w[1], w[0]))).sum() }

/// The unit direction of p from its start over h m (None: shorter than a mm).
fn dir_from_start(p: &[P2], h: f64) -> Option<P2> {
    let mut acc = 0.0;
    for w in p.windows(2) {
        let l = norm(sub(w[1], w[0]));
        if acc + l >= h && l > 0.0 { return unit(sub(lerp(w[0], w[1], (h - acc) / l), p[0])); }
        acc += l;
    }
    unit(sub(*p.last()?, p[0]))
}

/// From part `bi`'s end (side 0: its first point, 1: its last) into the structure: the line inward (the part, then
/// the parts going on from its far end, end to end within JOIN_M and APPROACH_DEG; REACH_M of it at most) and the
/// structure's length (every part so reached, each once).
pub fn walk(parts: &[&[P2]], bi: usize, side: usize) -> (Vec<P2>, f64) {
    let own: Vec<P2> = if side == 0 { parts[bi].to_vec() } else { parts[bi].iter().rev().copied().collect() };
    let mut inward = own.clone();
    let mut total = line_len(&own);
    let mut used = vec![bi];
    let mut tail = *own.last().unwrap();
    let back = |p: &[P2]| -> Option<P2> {
        // the direction arriving at p's end, over its last metre
        let r: Vec<P2> = p.iter().rev().copied().collect();
        dir_from_start(&r, 1.0f64.min(0.5 * line_len(p))).map(|d| scale(d, -1.0))
    };
    let mut dir = back(&own);
    for _ in 0..HOPS {
        let Some(dv) = dir else { break };
        let mut next: Option<(f64, usize, bool)> = None;
        for (k, p) in parts.iter().enumerate() {
            if p.len() < 2 || used.contains(&k) { continue; }
            for oe in 0..2 {
                let q = if oe == 0 { p[0] } else { p[p.len() - 1] };
                if norm(sub(q, tail)) > JOIN_M { continue; }
                let o: Vec<P2> = if oe == 0 { p.to_vec() } else { p.iter().rev().copied().collect() };
                let Some(v) = dir_from_start(&o, 1.0f64.min(0.5 * line_len(&o))) else { continue };
                let c = dot(v, dv);
                if c >= APPROACH_COS && next.is_none_or(|nx| c > nx.0) { next = Some((c, k, oe == 1)); }
            }
        }
        let Some((_c, k, rev)) = next else { break };
        used.push(k);
        let o: Vec<P2> = if rev { parts[k].iter().rev().copied().collect() } else { parts[k].to_vec() };
        total += line_len(&o);
        if line_len(&inward) < REACH_M { inward.extend(o.iter().skip(1)); }
        tail = *o.last().unwrap();
        dir = back(&o);
    }
    (inward, total)
}

/// The end line at the start of `inward` (unit, pointing to the left of the deck's way in), or None (square): the
/// module's rule. crossed: the lines that may pass under the structure (approaches and the structure's own parts
/// left out by the caller) with their kinds; nbi: the inventory's record of the structure (skew deg or 99, length
/// m or 0, cos and sin of the skew).
pub fn decide(inward: &[P2], slen: f64, crossed: &[(&[P2], u8)], nbi: Option<[f64; 4]>) -> Option<P2> {
    if inward.len() < 2 { return None; }
    // the nearest crossing within REACH_M along the line in
    let mut best: Option<(f64, P2, P2)> = None;      // (reach, the line's direction there, the crossed direction)
    let mut acc = 0.0;
    for w in inward.windows(2) {
        let l = norm(sub(w[1], w[0]));
        if acc > REACH_M { break; }
        if l > 0.0 {
            for (q, _kind) in crossed {
                for v in q.windows(2) {
                    let Some((u, _t)) = seg_x(w[0], w[1], v[0], v[1]) else { continue };
                    let reach = acc + u * l;
                    if reach <= NEAR_M || reach > REACH_M { continue; }
                    let (Some(md), Some(cd)) = (unit(sub(w[1], w[0])), unit(sub(v[1], v[0]))) else { continue };
                    if absf(cross(md, cd)) < MIN_CROSS_SIN { continue; }
                    if best.is_none_or(|b| reach < b.0) { best = Some((reach, md, cd)); }
                }
            }
        }
        acc += l;
    }
    let (_reach, md, cd) = best?;
    let sn = absf(cross(md, cd));
    let rec = nbi.filter(|n| n[0] >= 0.0 && n[0] < 99.0 && (n[1] <= 0.0 || (n[1] >= 0.5 * slen && n[1] <= 2.0 * slen)));
    match rec {
        None => { if slen > SPAN_MAX_M || sn > OFF_COS { return None; } }
        Some(n) => { if n[2] > OFF_COS { return None; } }
    }
    let h = 2.0f64.min(0.5 * line_len(inward));
    let t = dir_from_start(inward, h)?;
    if dot(t, md) < BEND_COS { return None; }
    let left = [-t[1], t[0]];
    let mut d = if dot(cd, left) >= 0.0 { cd } else { scale(cd, -1.0) };
    if let Some(n) = rec {
        let side = if dot(d, t) >= 0.0 { 1.0 } else { -1.0 };
        d = add(scale(left, n[2]), scale(t, side * n[3]));
    }
    if dot(d, left) < MAX_COS {
        let side = if dot(d, t) >= 0.0 { 1.0 } else { -1.0 };
        d = add(scale(left, MAX_COS), scale(t, side * (1.0 - MAX_COS * MAX_COS).sqrt()));
    }
    unit(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(at: f64, c: f64, s: f64) -> Vec<P2> { vec![[at - 50.0 * c, -50.0 * s], [at + 50.0 * c, 50.0 * s]] }

    #[test]
    fn a_short_overpass_skews_a_viaduct_does_not() {
        let deck: Vec<P2> = vec![[0.0, 0.0], [40.0, 0.0]];
        let road = line(20.0, 0.5, 0.8660254037844386);
        let (inward, slen) = walk(&[&deck], 0, 0);
        assert_eq!(slen, 40.0);
        let d = decide(&inward, slen, &[(&road, KIND_ROAD)], None).unwrap();
        assert!(absf(cross(d, [0.5, 0.8660254037844386])) < 1e-12 && d[1] > 0.0, "{:?}", d);
        // the same crossing on a 180 m structure (parts end to end): square
        let p2: Vec<P2> = vec![[40.0, 0.0], [110.0, 0.0]];
        let p3: Vec<P2> = vec![[180.0, 0.0], [110.0, 0.0]];
        let (inward, slen) = walk(&[&deck, &p2, &p3], 0, 0);
        assert_eq!(slen, 180.0);
        assert!(decide(&inward, slen, &[(&road, KIND_ROAD)], None).is_none());
        // ... unless the inventory says 30 deg (its length agreeing)
        let d = decide(&inward, slen, &[(&road, KIND_ROAD)], Some([30.0, 175.0, 0.8660254037844387, 0.5])).unwrap();
        assert!(absf(d[0] - 0.5) < 1e-12 && absf(d[1] - 0.8660254037844387) < 1e-12, "{:?}", d);
        // near square (3 deg): square; nothing crossed: square
        let sq = line(20.0, 0.05233595624294383, 0.9986295347545738);
        assert!(decide(&walk(&[&deck], 0, 0).0, 40.0, &[(&sq, KIND_WATER)], None).is_none());
        assert!(decide(&walk(&[&deck], 0, 0).0, 40.0, &[], None).is_none());
    }

    #[test]
    fn the_far_end_walks_back_the_same_structure() {
        let deck: Vec<P2> = vec![[0.0, 0.0], [40.0, 0.0]];
        let road = line(30.0, 0.5, 0.8660254037844386);
        let (inward, slen) = walk(&[&deck], 0, 1);
        assert_eq!(inward[0], [40.0, 0.0]);
        let d = decide(&inward, slen, &[(&road, KIND_ROAD)], None).unwrap();
        // the line in runs -x: its left is -y; the end line along the road, pointing to that side
        assert!(d[1] < 0.0 && absf(cross(d, [0.5, 0.8660254037844386])) < 1e-12, "{:?}", d);
    }
}
