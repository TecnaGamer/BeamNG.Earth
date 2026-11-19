//! railways._blocked's kernel (earthfast.near_crossers): which points of a track lie on a crossing road at grade.
//!
//! Per point and road: the road's nearest segment (the first of equals), nearer than the road's reach, with the road's
//! height at that segment's start within dz of the point's bed. The answer of every road's every segment (each point
//! against each segment, O(points x segments): 25-60 s cell plans in south-west Baltimore's rail yards, 2026-11-15)
//! from the segments a uniform grid over the points puts near them: a segment whose box, grown by the road's reach,
//! misses a point's grid cell is farther from the point than the reach, so it can be neither a nearest segment nearer
//! than the reach nor one equal to it; the segments that are kept are taken in their own order with the same test, so
//! the nearest (and the first of equals) is the same one. Points or roads with a coordinate (or a reach) past
//! BOUND_M or not finite are answered over every segment, as before: there the distances can overflow to inf or NaN
//! (a NaN first segment held the old scan's nearest for good, which a grid leaving that segment out would not), and
//! the nearest point on a segment can round further than BOX_PAD_M off its box.
use crate::dm::Dm;

const CELL_MIN_M: f64 = 16.0;       // m: the grid's cell side, at least
const CELLS_MAX: f64 = 512.0;       // cells along the points' longer side, at most
const BOX_PAD_M: f64 = 1e-3;        // m a segment's box grows past its reach (rounding of the nearest point on it)
const GRID_VISITS_MAX: u64 = 1 << 22;   // cells of the segments' boxes, all told, past which the grid is not built
const BOUND_M: f64 = 1e7;           // m: |coordinates| and reach the grid answers (products ~1e15: no overflow, no NaN;
                                    // rounding of a nearest point ~1e-8 m, well inside BOX_PAD_M)

#[inline]
fn seg(d: &[[f64; 3]], j: usize) -> [f64; 5] {
    let (ax, ay) = (d[j][0], d[j][1]);
    let (bx, by) = (d[j + 1][0] - ax, d[j + 1][1] - ay);
    [ax, ay, bx, by, (bx * bx + by * by).max(1e-12)]
}

#[inline]
fn dist(g: &[f64; 5], px: f64, py: f64) -> f64 {
    let t = (((px - g[0]) * g[2] + (py - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
    (px - (g[0] + t * g[2])).dhypot(py - (g[1] + t * g[3]))
}

/// Point (px, py, bed z) against road c's segments [j0, j1 - 1) in their order: its nearest (the first of equals)
/// nearer than reach with the road's height at its start within dz.
#[inline]
fn on_road<I: Iterator<Item = usize>>(segs: I, d: &[[f64; 3]], px: f64, py: f64, z: f64, reach: f64, dz: f64,
                                      j0: usize) -> bool {
    let (mut best, mut bj) = (f64::INFINITY, usize::MAX);
    for n in segs {
        let dd = dist(&seg(d, j0 + n), px, py);
        if dd < best || bj == usize::MAX { best = dd; bj = n; }
    }
    bj != usize::MAX && best < reach && (z - d[j0 + bj][2]).abs() < dz
}

/// p: the points (x, y); z: their beds; d: the roads' centre lines [x, y, z] one after another, starts (C + 1,);
/// reach (C,); dz. True where a point lies on a road at grade.
pub fn near_crossers(p: &[[f64; 2]], z: &[f64], d: &[[f64; 3]], st: &[usize], reach: &[f64], dz: f64) -> Vec<bool> {
    let k = p.len();
    let nc = st.len().saturating_sub(1);
    let mut out = vec![false; k];
    let fin = |x: f64, y: f64| x.abs() <= BOUND_M && y.abs() <= BOUND_M;     // (false for NaN too)
    // the grid over the finite points
    let (mut qx0, mut qy0, mut qx1, mut qy1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for q in p {
        if fin(q[0], q[1]) {
            qx0 = qx0.min(q[0]); qy0 = qy0.min(q[1]); qx1 = qx1.max(q[0]); qy1 = qy1.max(q[1]);
        }
    }
    let gridded = qx0 <= qx1;
    let (g, nx, ny) = if gridded {
        let g = CELL_MIN_M.max((qx1 - qx0).max(qy1 - qy0) / CELLS_MAX);
        (g, ((qx1 - qx0) / g) as usize + 1, ((qy1 - qy0) / g) as usize + 1)
    } else {
        (1.0, 0, 0)
    };
    let cx = |x: f64| ((((x - qx0) / g).floor().max(0.0)) as usize).min(nx - 1);
    let cy = |y: f64| ((((y - qy0) / g).floor().max(0.0)) as usize).min(ny - 1);
    // roads the grid answers (finite lines and reach) and the others (every point over every segment)
    let mut brute: Vec<usize> = Vec::new();
    let mut entries: Vec<(u32, u32)> = Vec::new();     // (road, segment) per segment the grid holds
    let mut cells: Vec<[usize; 4]> = Vec::new();        // its cells' x0 x1 y0 y1
    let mut visits = 0u64;                              // cells of their boxes, all told
    let mut gridded = gridded && d.len() < u32::MAX as usize && nc < u32::MAX as usize;
    for c in 0..nc {
        let (j0, j1) = (st[c], st[c + 1]);
        if j1 < j0 + 2 { continue; }
        let ok = reach[c].abs() <= BOUND_M && d[j0..j1].iter().all(|q| fin(q[0], q[1]));
        if !ok || !gridded { brute.push(c); continue; }
        let m = reach[c].max(0.0) + BOX_PAD_M;
        for n in 0..(j1 - j0 - 1) {
            let (a, b) = (d[j0 + n], d[j0 + n + 1]);
            let (x0, x1) = (a[0].min(b[0]) - m, a[0].max(b[0]) + m);
            let (y0, y1) = (a[1].min(b[1]) - m, a[1].max(b[1]) + m);
            if x1 < qx0 || x0 > qx1 || y1 < qy0 || y0 > qy1 { continue; }
            let r = [cx(x0), cx(x1), cy(y0), cy(y1)];
            visits += ((r[1] - r[0] + 1) * (r[3] - r[2] + 1)) as u64;
            cells.push(r);
            entries.push((c as u32, n as u32));
        }
        if visits > GRID_VISITS_MAX {
            // (long segments over a sparse, far-spread set of points: the boxes cover more cells than the grid
            // saves; every road over every segment, the old scan, with no grid built)
            gridded = false;
            cells = Vec::new();
            entries = Vec::new();
            brute = (0..nc).filter(|&c| st[c + 1] >= st[c] + 2).collect();
            break;
        }
    }
    // the segments by cell (CSR), each cell's list in the roads' and segments' order; only the cells with points
    let (gx, gy) = if gridded { (nx, ny) } else { (0, 0) };
    let mut occ = vec![false; gx * gy];
    if gridded {
        for q in p {
            if fin(q[0], q[1]) { occ[cy(q[1]) * nx + cx(q[0])] = true; }
        }
    }
    let mut cnt = vec![0usize; gx * gy + 1];
    for b in &cells {
        for iy in b[2]..=b[3] { for ix in b[0]..=b[1] { if occ[iy * nx + ix] { cnt[iy * nx + ix + 1] += 1; } } }
    }
    for i in 0..gx * gy { cnt[i + 1] += cnt[i]; }
    let mut fill = cnt.clone();
    let mut list = vec![(0u32, 0u32); cnt[gx * gy]];
    for (b, e) in cells.iter().zip(&entries) {
        for iy in b[2]..=b[3] {
            for ix in b[0]..=b[1] {
                if !occ[iy * nx + ix] { continue; }
                let s = &mut fill[iy * nx + ix];
                list[*s] = *e;
                *s += 1;
            }
        }
    }
    for i in 0..k {
        let (px, py) = (p[i][0], p[i][1]);
        if !fin(px, py) {
            // (every road over every segment: the old answer for a point the grid does not answer)
            out[i] = (0..nc).any(|c| {
                let (j0, j1) = (st[c], st[c + 1]);
                j1 >= j0 + 2 && on_road(0..(j1 - j0 - 1), d, px, py, z[i], reach[c], dz, j0)
            });
            continue;
        }
        if gridded {
            let cell = cy(py) * nx + cx(px);
            let l = &list[cnt[cell]..cnt[cell + 1]];
            let mut a = 0;
            while a < l.len() && !out[i] {
                let c = l[a].0;
                let mut b = a;
                while b < l.len() && l[b].0 == c { b += 1; }
                let cu = c as usize;
                out[i] = on_road(l[a..b].iter().map(|e| e.1 as usize), d, px, py, z[i], reach[cu], dz, st[cu]);
                a = b;
            }
        }
        if !out[i] {
            out[i] = brute.iter().any(|&c| {
                let (j0, j1) = (st[c], st[c + 1]);
                on_road(0..(j1 - j0 - 1), d, px, py, z[i], reach[c], dz, j0)
            });
        }
    }
    out
}

/// grading.rail_yield_pieces' cut grown along the line: mask, and every point whose arc s is within reach of a masked
/// point's (|s_i - s_j| <= reach, numpy's float64). Arcs rising (a polyline's): the nearest masked arcs are the masked
/// points before and after, one pass each way (was every point against every masked one); arcs not rising or not
/// finite are taken against every masked point, numpy's min (a NaN wins).
pub fn grow_along(s: &[f64], mask: &[bool], reach: f64) -> Vec<bool> {
    let n = s.len();
    let rising = s.iter().all(|v| v.is_finite()) && s.windows(2).all(|w| w[0] <= w[1]);
    if !rising {
        let sb: Vec<f64> = (0..n).filter(|&j| mask[j]).map(|j| s[j]).collect();
        return (0..n).map(|i| {
            let mut m = f64::INFINITY;
            for &b in &sb {
                let d = (s[i] - b).abs();
                if d.is_nan() { m = d; break; }
                if d < m { m = d; }
            }
            mask[i] || (!sb.is_empty() && m <= reach)
        }).collect();
    }
    let mut out = mask.to_vec();
    let mut last: Option<f64> = None;
    for i in 0..n {
        if mask[i] { last = Some(s[i]); } else if let Some(b) = last { if (s[i] - b).abs() <= reach { out[i] = true; } }
    }
    last = None;
    for i in (0..n).rev() {
        if mask[i] { last = Some(s[i]); } else if let Some(b) = last { if (s[i] - b).abs() <= reach { out[i] = true; } }
    }
    out
}
