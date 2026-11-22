//! G6 TILE: canonical pieces clipped per 256 m tile along shared cut lines (Sutherland-Hodgman, xmin, xmax, ymin,
//! ymax in that order; an edge's intersection with a cut line computed from its endpoints in lexicographic order, so
//! both tiles make the same seam vertex), then written as canonical bytes (vertices in first-use order, i32 mm from
//! the tile's corner).
use crate::geom::*;
use crate::mark::Marks;
use crate::part::{Mesh, Tag};

pub type V3 = [i64; 3];

pub struct TileOut {
    pub i: i32,
    pub j: i32,
    pub bytes: Vec<u8>,
    pub hash: u64,
    pub tris: usize,
    pub verts: usize,
    pub mark_tris: usize,
    pub sides: [Vec<(V3, V3)>; 4],   // edges on x0, x1, y0, y1
    pub cpu: f64,
}

fn cut(p: V3, q: V3, axis: usize, c: i64) -> V3 {
    let (a, b) = if p < q { (p, q) } else { (q, p) };
    let den = (b[axis] - a[axis]) as f64;
    let t = (c - a[axis]) as f64 / den;
    let mut r = [0i64; 3];
    for k in 0..3 {
        r[k] = if k == axis { c } else { (a[k] as f64 + t * (b[k] - a[k]) as f64).round() as i64 };
    }
    r
}

fn clip(poly: &mut Vec<V3>, axis: usize, c: i64, keep_ge: bool, tmp: &mut Vec<V3>) {
    tmp.clear();
    let n = poly.len();
    for i in 0..n {
        let (p, q) = (poly[i], poly[(i + 1) % n]);
        let ip = if keep_ge { p[axis] >= c } else { p[axis] <= c };
        let iq = if keep_ge { q[axis] >= c } else { q[axis] <= c };
        if ip { tmp.push(p); }
        if ip != iq && p[axis] != c && q[axis] != c { tmp.push(cut(p, q, axis, c)); }
    }
    std::mem::swap(poly, tmp);
}

struct W { v: std::collections::BTreeMap<V3, u32>, vl: Vec<V3>, t: Vec<[u32; 3]>, tags: Vec<[u8; 8]> }

impl W {
    fn vid(&mut self, p: V3) -> u32 {
        if let Some(&i) = self.v.get(&p) { return i; }
        let i = self.vl.len() as u32;
        self.v.insert(p, i);
        self.vl.push(p);
        i
    }
}

fn emit(w: &mut W, poly: &[V3], tag: [u8; 8], box_: [i64; 4], sides: &mut [Vec<(V3, V3)>; 4], track: bool) {
    let mut ps: Vec<V3> = Vec::with_capacity(poly.len());
    for &p in poly { if ps.last() != Some(&p) { ps.push(p); } }
    while ps.len() > 1 && ps.first() == ps.last() { ps.pop(); }
    if ps.len() < 3 { return; }
    for i in 1..ps.len() - 1 {
        let (a, b, c) = (ps[0], ps[i], ps[i + 1]);
        if orient([a[0], a[1]], [b[0], b[1]], [c[0], c[1]]) == 0 { continue; }
        let t = [w.vid(a), w.vid(b), w.vid(c)];
        w.t.push(t);
        w.tags.push(tag);
    }
    if track {
        for i in 0..ps.len() {
            let (p, q) = (ps[i], ps[(i + 1) % ps.len()]);
            let e = if p < q { (p, q) } else { (q, p) };
            if p[0] == box_[0] && q[0] == box_[0] { sides[0].push(e); }
            if p[0] == box_[2] && q[0] == box_[2] { sides[1].push(e); }
            if p[1] == box_[1] && q[1] == box_[1] { sides[2].push(e); }
            if p[1] == box_[3] && q[1] == box_[3] { sides[3].push(e); }
        }
    }
}

/// Clip the mesh and markings into tile (i, j) of size `size` m whose (0, 0) tile's corner is (x0, y0) m.
pub fn tile(mesh: &Mesh, tris: &[u32], marks: &Marks, mtris: &[u32], i: i32, j: i32, x0: f64, y0: f64, size: f64) -> TileOut {
    let t0 = crate::cpu_now();
    let bx = [mm(x0 + size * i as f64), mm(y0 + size * j as f64), mm(x0 + size * (i + 1) as f64), mm(y0 + size * (j + 1) as f64)];
    let mut sides: [Vec<(V3, V3)>; 4] = [Vec::new(), Vec::new(), Vec::new(), Vec::new()];
    let mut w = W { v: Default::default(), vl: Vec::new(), t: Vec::new(), tags: Vec::new() };
    let mut poly: Vec<V3> = Vec::with_capacity(9);
    let mut tmp: Vec<V3> = Vec::with_capacity(9);
    let do_tri = |p: [V3; 3], tag: [u8; 8], w: &mut W, sides: &mut [Vec<(V3, V3)>; 4], poly: &mut Vec<V3>, tmp: &mut Vec<V3>, track: bool| {
        let inside = p.iter().all(|v| v[0] >= bx[0] && v[0] <= bx[2] && v[1] >= bx[1] && v[1] <= bx[3]);
        poly.clear();
        poly.extend_from_slice(&p);
        if !inside {
            clip(poly, 0, bx[0], true, tmp);
            if poly.len() >= 3 { clip(poly, 0, bx[2], false, tmp); }
            if poly.len() >= 3 { clip(poly, 1, bx[1], true, tmp); }
            if poly.len() >= 3 { clip(poly, 1, bx[3], false, tmp); }
        }
        emit(w, poly, tag, bx, sides, track);
    };
    for &ti in tris {
        let t = mesh.t[ti as usize];
        let g: Tag = mesh.tag[ti as usize];
        let tag = { let mut b = [0u8; 8]; b[0] = g.kind; b[1] = g.sup; b[2..6].copy_from_slice(&g.piece.to_le_bytes()); b[6..8].copy_from_slice(&g.slot.to_le_bytes()); b };
        let p = [mesh.v[t[0] as usize], mesh.v[t[1] as usize], mesh.v[t[2] as usize]];
        do_tri(p, tag, &mut w, &mut sides, &mut poly, &mut tmp, true);
    }
    let nt = w.t.len();
    for &ti in mtris {
        let t = marks.t[ti as usize];
        let tag = { let mut b = [0u8; 8]; b[0] = 10 + marks.colour[ti as usize]; b[2..6].copy_from_slice(&marks.piece[ti as usize].to_le_bytes()); b };
        let p = [marks.v[t[0] as usize], marks.v[t[1] as usize], marks.v[t[2] as usize]];
        do_tri(p, tag, &mut w, &mut sides, &mut poly, &mut tmp, false);
    }
    // canonical bytes
    let mut bytes = Vec::with_capacity(16 + w.vl.len() * 12 + w.t.len() * 20);
    bytes.extend_from_slice(&i.to_le_bytes());
    bytes.extend_from_slice(&j.to_le_bytes());
    bytes.extend_from_slice(&(w.vl.len() as u32).to_le_bytes());
    for p in &w.vl {
        bytes.extend_from_slice(&((p[0] - bx[0]) as i32).to_le_bytes());
        bytes.extend_from_slice(&((p[1] - bx[1]) as i32).to_le_bytes());
        bytes.extend_from_slice(&(p[2] as i32).to_le_bytes());
    }
    bytes.extend_from_slice(&(w.t.len() as u32).to_le_bytes());
    for (t, g) in w.t.iter().zip(&w.tags) {
        for &v in t { bytes.extend_from_slice(&v.to_le_bytes()); }
        bytes.extend_from_slice(g);
    }
    let hash = fnv(&bytes);
    for s in sides.iter_mut() { s.sort(); }
    let cpu = crate::cpu_now() - t0;
    TileOut { i, j, hash, tris: nt, verts: w.vl.len(), mark_tris: w.t.len() - nt, bytes, sides, cpu }
}

/// Triangles of the mesh (and markings) whose bounding boxes touch each tile.
pub fn buckets(v: &[[i64; 3]], t: &[[u32; 3]], x0: f64, y0: f64, size: f64, nx: i32, ny: i32) -> Vec<Vec<u32>> {
    let mut out = vec![Vec::new(); (nx * ny) as usize];
    let s = mm(size) as f64;
    let (ox, oy) = (mm(x0), mm(y0));
    for (k, tri) in t.iter().enumerate() {
        let xs = [v[tri[0] as usize][0], v[tri[1] as usize][0], v[tri[2] as usize][0]];
        let ys = [v[tri[0] as usize][1], v[tri[1] as usize][1], v[tri[2] as usize][1]];
        let fx = |x: i64| ((x - ox) as f64 / s).floor() as i32;
        let fy = |y: i64| ((y - oy) as f64 / s).floor() as i32;
        let (i0, i1) = (fx(*xs.iter().min().unwrap()), fx(*xs.iter().max().unwrap()));
        let (j0, j1) = (fy(*ys.iter().min().unwrap()), fy(*ys.iter().max().unwrap()));
        for i in i0.max(0)..=i1.min(nx - 1) {
            for j in j0.max(0)..=j1.min(ny - 1) { out[(j * nx + i) as usize].push(k as u32); }
        }
    }
    out
}
