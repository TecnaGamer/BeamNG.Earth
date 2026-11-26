//! G5-lite, per tile (to cost the derived products the 0.15 s budget includes; not their final form):
//! - terrain targets: every GROUND face rasterised into the tile's 1 m grid (z = the surface, by barycentric
//!   interpolation), every STRUCTURE face into a ceiling grid (soffit - 0.3 m: the terrain must stay below it);
//! - deck slab: STRUCTURE faces extruded down by the deck depth, side walls on their free edges;
//! - parapets: a box per free edge of a STRUCTURE face (an edge no other drivable face shares);
//! - colliders: the STRUCTURE tops and the parapet boxes (counted, not decimated).
use crate::net::{SUP_GROUND, SUP_STRUCT};
use crate::part::Mesh;
use std::collections::BTreeMap;

#[derive(Default, Clone, Debug)]
pub struct Derived { pub ground_cells: usize, pub ceil_cells: usize, pub slab_tris: usize, pub parapet_edges: usize, pub collider_tris: usize, pub cpu: f64, pub hash: u64 }

pub fn derive(m: &Mesh, tris: &[u32], bx: [i64; 4]) -> Derived {
    let t0 = crate::cpu_now();
    let n = ((bx[2] - bx[0]) / 1000) as usize;
    let mut zg = vec![i32::MIN; n * n];
    let mut zc = vec![i32::MAX; n * n];
    let mut d = Derived::default();
    let mut edges: BTreeMap<(u32, u32), u32> = BTreeMap::new();
    for &ti in tris {
        let t = m.t[ti as usize];
        let g = m.tag[ti as usize];
        let p = [m.v[t[0] as usize], m.v[t[1] as usize], m.v[t[2] as usize]];
        if g.sup == SUP_STRUCT {
            d.collider_tris += 1;
            d.slab_tris += 1;
            for k in 0..3 { let (a, b) = (t[k], t[(k + 1) % 3]); *edges.entry(if a < b { (a, b) } else { (b, a) }).or_insert(0) += 1; }
        }
        if g.sup != SUP_GROUND && g.sup != SUP_STRUCT { continue; }
        // raster: cell centres inside the triangle
        let xs = [p[0][0], p[1][0], p[2][0]];
        let ys = [p[0][1], p[1][1], p[2][1]];
        let (x0, x1) = (*xs.iter().min().unwrap(), *xs.iter().max().unwrap());
        let (y0, y1) = (*ys.iter().min().unwrap(), *ys.iter().max().unwrap());
        let i0 = ((x0 - bx[0] - 500).div_euclid(1000)).max(0);
        let i1 = ((x1 - bx[0] - 500).div_euclid(1000) + 1).min(n as i64 - 1);
        let j0 = ((y0 - bx[1] - 500).div_euclid(1000)).max(0);
        let j1 = ((y1 - bx[1] - 500).div_euclid(1000) + 1).min(n as i64 - 1);
        if i0 > i1 || j0 > j1 { continue; }
        let det = ((p[1][0] - p[0][0]) as i128) * ((p[2][1] - p[0][1]) as i128) - ((p[2][0] - p[0][0]) as i128) * ((p[1][1] - p[0][1]) as i128);
        if det == 0 { continue; }
        for j in j0..=j1 {
            let cy = bx[1] + j * 1000 + 500;
            for i in i0..=i1 {
                let cx = bx[0] + i * 1000 + 500;
                let w1 = ((cx - p[0][0]) as i128) * ((p[2][1] - p[0][1]) as i128) - ((p[2][0] - p[0][0]) as i128) * ((cy - p[0][1]) as i128);
                let w2 = ((p[1][0] - p[0][0]) as i128) * ((cy - p[0][1]) as i128) - ((cx - p[0][0]) as i128) * ((p[1][1] - p[0][1]) as i128);
                let (w1, w2) = if det > 0 { (w1, w2) } else { (-w1, -w2) };
                let dd = if det > 0 { det } else { -det };
                if w1 < 0 || w2 < 0 || w1 + w2 > dd { continue; }
                let z = p[0][2] as i128 + ((p[1][2] - p[0][2]) as i128 * w1 * (if det > 0 { 1 } else { -1 }) + (p[2][2] - p[0][2]) as i128 * w2 * (if det > 0 { 1 } else { -1 })) / det;
                let k = j as usize * n + i as usize;
                if g.sup == SUP_GROUND { if z as i32 > zg[k] { zg[k] = z as i32; } } else {
                    let c = z as i32 - 1200 - 300;
                    if c < zc[k] { zc[k] = c; }
                }
            }
        }
    }
    // free edges of the structure faces: parapets + slab side walls
    for (_e, &c) in &edges { if c == 1 { d.parapet_edges += 1; d.slab_tris += 2; d.collider_tris += 12; } }
    d.slab_tris += d.slab_tris;     // the slab's bottom copy
    d.ground_cells = zg.iter().filter(|&&v| v != i32::MIN).count();
    d.ceil_cells = zc.iter().filter(|&&v| v != i32::MAX).count();
    let mut h: u64 = 0xcbf29ce484222325;
    for &v in zg.iter().chain(zc.iter()) { for b in v.to_le_bytes() { h ^= b as u64; h = h.wrapping_mul(0x100000001b3); } }
    d.hash = h;
    d.cpu = crate::cpu_now() - t0;
    d
}
