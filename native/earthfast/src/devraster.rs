//! Polygon parts filled into a bit raster (server development.py: the street-development context of PR #670).
//!
//! fill(h, w, xy, ring_off, part_off, part_bits) -> (h, w) u8 (devctx.rs's DevContext): every part (its first ring
//! the outline, the others its holes) filled even-odd, sampled at pixel centres (col + 0.5, row + 0.5 in xy's pixel
//! units), its bits ORed in where it is filled. A hole only cuts its own part (another building inside a courtyard stays).
//! One call per road tile instead of a Pillow image per part and channel in Python.

pub fn fill(h: usize, w: usize, xy: &[[f64; 2]], ring_off: &[i64], part_off: &[i64], part_bits: &[u8]) -> Vec<u8> {
    let mut out = vec![0u8; h * w];
    let mut xs: Vec<f64> = Vec::new();
    let np = part_bits.len().min(part_off.len().saturating_sub(1));
    for p in 0..np {
        let bits = part_bits[p];
        if bits == 0 {
            continue;
        }
        let (r0, r1) = (part_off[p].max(0) as usize, part_off[p + 1].max(0) as usize);
        if r1 <= r0 || r1 + 1 > ring_off.len() {
            continue;
        }
        // the part's rows: its outline's extent
        let (a0, a1) = (ring_off[r0].max(0) as usize, ring_off[r0 + 1].max(0) as usize);
        if a1 > xy.len() || a1 < a0 + 3 {
            continue;
        }
        let (mut ymin, mut ymax) = (f64::INFINITY, f64::NEG_INFINITY);
        for q in &xy[a0..a1] {
            if !q[0].is_finite() || !q[1].is_finite() {
                ymin = f64::INFINITY;
                break;
            }
            ymin = ymin.min(q[1]);
            ymax = ymax.max(q[1]);
        }
        if !ymin.is_finite() {
            continue;
        }
        let row0 = ((ymin - 0.5).ceil().max(0.0)) as usize;
        let row1 = (((ymax - 0.5).floor() + 1.0).min(h as f64).max(0.0)) as usize;
        for row in row0..row1 {
            let yc = row as f64 + 0.5;
            xs.clear();
            for r in r0..r1 {
                if r + 1 >= ring_off.len() {
                    break;
                }
                let (b0, b1) = (ring_off[r].max(0) as usize, (ring_off[r + 1].max(0) as usize).min(xy.len()));
                if b1 < b0 + 3 {
                    continue;
                }
                let ring = &xy[b0..b1];
                let n = ring.len();
                for i in 0..n {
                    let (pa, pb) = (ring[i], ring[(i + 1) % n]);
                    if (pa[1] <= yc && yc < pb[1]) || (pb[1] <= yc && yc < pa[1]) {
                        xs.push(pa[0] + (yc - pa[1]) * (pb[0] - pa[0]) / (pb[1] - pa[1]));
                    }
                }
            }
            if xs.len() < 2 {
                continue;
            }
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let line = &mut out[row * w..(row + 1) * w];
            for pair in xs.chunks_exact(2) {
                let c0 = ((pair[0] - 0.5).ceil().max(0.0)) as usize;
                let c1 = (((pair[1] - 0.5).ceil()).min(w as f64).max(0.0)) as usize;
                for c in c0..c1.max(c0) {
                    line[c] |= bits;
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn square_with_a_hole_and_another_part_inside_it() {
        // outline 0..10, hole 3..7 (part 0, bit 1); a small square 4..6 inside the hole (part 1, bit 2)
        let sq = |a: f64, b: f64| vec![[a, a], [b, a], [b, b], [a, b]];
        let mut xy = sq(0.0, 10.0);
        xy.extend(sq(3.0, 7.0));
        xy.extend(sq(4.0, 6.0));
        let ring_off = [0i64, 4, 8, 12];
        let part_off = [0i64, 2, 3];
        let out = fill(10, 10, &xy, &ring_off, &part_off, &[1, 2]);
        assert_eq!(out[0], 1);
        assert_eq!(out[3 * 10 + 3], 0, "the hole");
        assert_eq!(out[5 * 10 + 5], 2, "the other part in the hole stays");
        assert_eq!(out[9 * 10 + 9], 1);
        assert_eq!(out.iter().filter(|&&v| v & 1 != 0).count(), 100 - 16);
    }
}
