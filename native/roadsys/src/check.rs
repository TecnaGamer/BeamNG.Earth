//! The design's automated checks (section 10) that this prototype can run on its own output: overlap area between
//! faces at the same level (in-lane and by cause), holes (mapped centrelines not covered by any face), shared edges
//! (non-manifold, inconsistent orientation, T-junction cracks), zero-area / folded triangles, tile seams.
use crate::geom::*;
use crate::net::*;
use crate::part::Mesh;
use crate::tile::TileOut;
use std::collections::BTreeMap;

#[derive(Default, Debug, Clone)]
pub struct Checks {
    pub tris_core: usize,
    pub overlap_pairs: usize,
    pub overlap_m2: f64,
    pub overlap_lane_m2: f64,
    pub overlap_fold_m2: f64,
    pub overlap_strip_strip_m2: f64,
    pub overlap_zone_strip_m2: f64,
    pub overlap_zone_zone_m2: f64,
    pub overlap_same_piece_pairs: usize,
    pub stacked_pairs: usize,
    pub stacked_struct_pairs: usize,
    pub paved_m2: f64,
    pub lane_m2: f64,
    pub hole_samples: usize,
    pub hole_uncovered: usize,
    pub nonmanifold_edges: usize,
    pub flipped_edges: usize,
    pub boundary_edges: usize,
    pub tjunctions: usize,
    pub zero_area: usize,
    pub folded: usize,
    pub seam_pairs: usize,
    pub seam_mismatch: usize,
    pub overlap_examples: Vec<(f64, f64, f64, String)>,
    pub holes: Vec<P2>,
}

fn tri_pts(m: &Mesh, t: [u32; 3]) -> [P2; 3] {
    let p = |i: u32| { let v = m.v[i as usize]; [v[0] as f64 / 1000.0, v[1] as f64 / 1000.0] };
    [p(t[0]), p(t[1]), p(t[2])]
}

pub fn run(net: &Net, m: &Mesh, core: f64, tiles: &[TileOut], nx: i32) -> Checks {
    run_in(net, m, &|p: P2| p[0] >= -core && p[0] <= core && p[1] >= -core && p[1] <= core, None, tiles, nx)
}

/// The checks of the faces whose positions `inside` holds (only the strokes `sel` marks are sampled for holes).
pub fn run_in(net: &Net, m: &Mesh, inside: &dyn Fn(P2) -> bool, sel: Option<&[bool]>, tiles: &[TileOut], nx: i32) -> Checks {
    let mut c = Checks::default();
    let _ns = net.strokes.len() as u32;
    const CELL: f64 = 8.0;
    let mut grid: BTreeMap<(i64, i64), Vec<u32>> = BTreeMap::new();
    let mut bb: Vec<[f64; 4]> = Vec::with_capacity(m.t.len());
    let mut zc: Vec<f64> = Vec::with_capacity(m.t.len());
    for (k, &t) in m.t.iter().enumerate() {
        let p = tri_pts(m, t);
        let b = [p[0][0].min(p[1][0]).min(p[2][0]), p[0][1].min(p[1][1]).min(p[2][1]),
                 p[0][0].max(p[1][0]).max(p[2][0]), p[0][1].max(p[1][1]).max(p[2][1])];
        bb.push(b);
        zc.push((m.v[t[0] as usize][2] + m.v[t[1] as usize][2] + m.v[t[2] as usize][2]) as f64 / 3000.0);
        let ar = cross(sub(p[1], p[0]), sub(p[2], p[0])) / 2.0;
        let cen = [(p[0][0] + p[1][0] + p[2][0]) / 3.0, (p[0][1] + p[1][1] + p[2][1]) / 3.0];
        if inside(cen) {
            c.tris_core += 1;
            c.paved_m2 += absf(ar);
            if m.tag[k].kind == K_LANE || m.tag[k].kind == crate::xsec::K_BUS { c.lane_m2 += absf(ar); }
            let o = orient([m.v[t[0] as usize][0], m.v[t[0] as usize][1]], [m.v[t[1] as usize][0], m.v[t[1] as usize][1]], [m.v[t[2] as usize][0], m.v[t[2] as usize][1]]);
            if o == 0 { c.zero_area += 1; }
            if o < 0 { c.folded += 1; }
        }
        for gx in (b[0] / CELL).floor() as i64..=(b[2] / CELL).floor() as i64 {
            for gy in (b[1] / CELL).floor() as i64..=(b[3] / CELL).floor() as i64 {
                grid.entry((gx, gy)).or_default().push(k as u32);
            }
        }
    }
    // overlaps
    for (&(gx, gy), list) in &grid {
        for a in 0..list.len() {
            for b in (a + 1)..list.len() {
                let (i, j) = (list[a] as usize, list[b] as usize);
                let (bi, bj) = (bb[i], bb[j]);
                let lo = [bi[0].max(bj[0]), bi[1].max(bj[1])];
                if lo[0] > bi[2].min(bj[2]) || lo[1] > bi[3].min(bj[3]) { continue; }
                if (lo[0] / CELL).floor() as i64 != gx || (lo[1] / CELL).floor() as i64 != gy { continue; }
                if !inside(lo) { continue; }
                let (ti, tj) = (m.t[i], m.t[j]);
                let shared = ti.iter().filter(|v| tj.contains(v)).count();
                if shared >= 2 { continue; }
                let ar = tri_overlap(tri_pts(m, ti), tri_pts(m, tj));
                if ar < 0.01 { continue; }
                let (gi, gj) = (m.tag[i], m.tag[j]);
                if absf(zc[i] - zc[j]) > 1.0 {
                    c.stacked_pairs += 1;
                    if gi.sup == SUP_STRUCT || gj.sup == SUP_STRUCT || gi.sup == SUP_TUNNEL || gj.sup == SUP_TUNNEL { c.stacked_struct_pairs += 1; }
                    continue;
                }
                if gi.sup == SUP_TUNNEL || gj.sup == SUP_TUNNEL { continue; }
                c.overlap_pairs += 1;
                c.overlap_m2 += ar;
                if gi.kind == K_LANE || gj.kind == K_LANE || gi.kind == crate::xsec::K_BUS || gj.kind == crate::xsec::K_BUS { c.overlap_lane_m2 += ar; }
                let (zi, zj) = (gi.piece & 0x8000_0000 != 0, gj.piece & 0x8000_0000 != 0);
                let cause = if gi.piece == gj.piece { c.overlap_fold_m2 += ar; c.overlap_same_piece_pairs += 1; "same_piece" }
                    else if !zi && !zj { c.overlap_strip_strip_m2 += ar; "strip_strip" }
                    else if zi && zj { c.overlap_zone_zone_m2 += ar; "zone_zone" } else { c.overlap_zone_strip_m2 += ar; "zone_strip" };
                if c.overlap_examples.len() < 4000 { c.overlap_examples.push((lo[0], lo[1], ar, cause.to_string())); }
            }
        }
    }
    // holes: mapped centrelines (every 2 m, ground and structure, not tunnels) covered by some face
    for (si, st) in net.strokes.iter().enumerate() {
        if let Some(v) = sel { if !v[si] { continue; } }
        let len = st.len();
        let mut s = 1.0;
        while s < len - 1.0 {
            let (p, _) = st.line.at(s);
            let sup = st.support_at(s);
            if inside(p) && sup.kind != SUP_TUNNEL {
                c.hole_samples += 1;
                let q = mm2(p);
                let key = ((p[0] / CELL).floor() as i64, (p[1] / CELL).floor() as i64);
                let mut hit = false;
                if let Some(list) = grid.get(&key) {
                    for &k in list {
                        let t = m.t[k as usize];
                        let v = |i: u32| { let x = m.v[i as usize]; [x[0], x[1]] };
                        let (a, b, d) = (v(t[0]), v(t[1]), v(t[2]));
                        let (o1, o2, o3) = (orient(a, b, q), orient(b, d, q), orient(d, a, q));
                        if (o1 >= 0 && o2 >= 0 && o3 >= 0) || (o1 <= 0 && o2 <= 0 && o3 <= 0) { hit = true; break; }
                    }
                }
                if !hit { c.hole_uncovered += 1; c.holes.push(p); }
            }
            s += 2.0;
        }
    }
    // shared edges
    let mut em: BTreeMap<(u32, u32), (u32, i32)> = BTreeMap::new();
    for t in &m.t {
        for k in 0..3 {
            let (a, b) = (t[k], t[(k + 1) % 3]);
            let key = if a < b { (a, b) } else { (b, a) };
            let e = em.entry(key).or_insert((0, 0));
            e.0 += 1;
            e.1 += if a < b { 1 } else { -1 };
        }
    }
    let mut vgrid: BTreeMap<(i64, i64), Vec<u32>> = BTreeMap::new();
    for (k, v) in m.v.iter().enumerate() { vgrid.entry((v[0].div_euclid(4000), v[1].div_euclid(4000))).or_default().push(k as u32); }
    for (&(a, b), &(n, dsum)) in &em {
        let (pa, pb) = (m.v[a as usize], m.v[b as usize]);
        let mid = [(pa[0] + pb[0]) as f64 / 2000.0, (pa[1] + pb[1]) as f64 / 2000.0];
        if !inside(mid) { continue; }
        if n > 2 { c.nonmanifold_edges += 1; }
        if n == 2 && dsum != 0 { c.flipped_edges += 1; }
        if n == 1 {
            c.boundary_edges += 1;
            // a vertex strictly inside this boundary edge (within 2 mm of the line): a crack
            let (x0, x1) = (pa[0].min(pb[0]).div_euclid(4000), pa[0].max(pb[0]).div_euclid(4000));
            let (y0, y1) = (pa[1].min(pb[1]).div_euclid(4000), pa[1].max(pb[1]).div_euclid(4000));
            let len2 = ((pb[0] - pa[0]) as i128).pow(2) + ((pb[1] - pa[1]) as i128).pow(2);
            if len2 < 100 { continue; }
            'found: for gx in x0..=x1 {
                for gy in y0..=y1 {
                    if let Some(list) = vgrid.get(&(gx, gy)) {
                        for &q in list {
                            if q == a || q == b { continue; }
                            let pq = m.v[q as usize];
                            if (pq[2] - pa[2]).abs() > 500 && (pq[2] - pb[2]).abs() > 500 { continue; }
                            let o = orient([pa[0], pa[1]], [pb[0], pb[1]], [pq[0], pq[1]]);
                            // distance to the line = |o| / len  <= 2 mm
                            if o * o > 4 * len2 { continue; }
                            let d = ((pq[0] - pa[0]) as i128) * ((pb[0] - pa[0]) as i128) + ((pq[1] - pa[1]) as i128) * ((pb[1] - pa[1]) as i128);
                            if d > len2 / 100 && d < len2 - len2 / 100 { c.tjunctions += 1; break 'found; }
                        }
                    }
                }
            }
        }
    }
    // tile seams: tile (i, j)'s x1 side against (i+1, j)'s x0 side, y1 against (i, j+1)'s y0
    let at = |i: i32, j: i32| tiles.iter().find(|t| t.i == i && t.j == j);
    for t in tiles {
        if let Some(r) = at(t.i + 1, t.j) { c.seam_pairs += 1; if t.sides[1] != r.sides[0] { c.seam_mismatch += 1; } }
        if let Some(u) = at(t.i, t.j + 1) { c.seam_pairs += 1; if t.sides[3] != u.sides[2] { c.seam_mismatch += 1; } }
    }
    let _ = nx;
    c
}
