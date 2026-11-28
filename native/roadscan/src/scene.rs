//! One tile's scan scene: its own meshes, terrain and lines, with its neighbours' meshes (and their terrain where its
//! own block ends) placed in its frame the way the mod places a tile in another world (geotiles.placement), over the
//! tile's quad grown by GROW so that a seam lies inside the scene; what is found is kept only inside the tile's own
//! quad (each seam judged once, by the tile that owns its cells).
use crate::pack::{Index, LineMeta, Pack, Placement, Ter};
use roadsys::audit::{classify, Line, Scene, Terrain, Tri};
use std::sync::Arc;

pub const GROW: f64 = 8.0;          // m the scene's quad reaches past the tile's own (seams, the ride's 5 m windows)
const NEAR: f64 = 30.0;             // m past the grown quad a neighbour's triangle is still taken (decks cut at boxes)
const TER_PAD: i64 = 4;             // lattice nodes of terrain past the grown quad

pub struct Built {
    pub scene: Scene,
    pub own_quad: [[f64; 2]; 4],
    pub lines: Vec<LineMeta>,
    pub ter: Ter,                   // the scene's terrain with its paved flags
    pub own_entries: usize,         // entries 0..own_entries are the tile's own
    pub aff: [f64; 6],
    pub key: String,
}

pub fn grow(q: &[[f64; 2]; 4], by: f64) -> [[f64; 2]; 4] {
    let c = [(q[0][0] + q[1][0] + q[2][0] + q[3][0]) / 4.0, (q[0][1] + q[1][1] + q[2][1] + q[3][1]) / 4.0];
    let mut out = *q;
    for p in out.iter_mut() {
        let d = [p[0] - c[0], p[1] - c[1]];
        let l = (d[0] * d[0] + d[1] * d[1]).sqrt();
        if l > 1e-9 {
            let k = by * std::f64::consts::SQRT_2 / l;
            *p = [p[0] + d[0] * k, p[1] + d[1] * k];
        }
    }
    out
}

pub fn in_quad(q: &[[f64; 2]; 4], p: [f64; 2]) -> bool {
    let (mut pos, mut neg) = (true, true);
    for k in 0..4 {
        let (a, b) = (q[k], q[(k + 1) % 4]);
        let t = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
        if t < 0.0 { pos = false; }
        if t > 0.0 { neg = false; }
    }
    pos || neg
}

fn bbox(q: &[[f64; 2]; 4]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for p in q { b[0] = b[0].min(p[0]); b[1] = b[1].min(p[1]); b[2] = b[2].max(p[0]); b[3] = b[3].max(p[1]); }
    b
}

/// A thread's recently read packs.
pub struct Packs<'a> { pub ix: &'a Index, cache: Vec<(usize, Arc<Pack>)> }

impl<'a> Packs<'a> {
    pub fn new(ix: &'a Index) -> Packs<'a> { Packs { ix, cache: Vec::new() } }
    pub fn get(&mut self, pack: usize) -> Result<Arc<Pack>, String> {
        if let Some(i) = self.cache.iter().position(|c| c.0 == pack) {
            let c = self.cache.remove(i);
            self.cache.push(c.clone());
            return Ok(c.1);
        }
        let p = Arc::new(self.ix.load(pack)?);
        self.cache.push((pack, p.clone()));
        if self.cache.len() > 24 { self.cache.remove(0); }
        Ok(p)
    }
}

/// The tiles around tile ti (within `ring` tiles, itself excluded) with their placement into ti's frame, in key order.
pub fn neighbours(ix: &Index, ti: usize, ring: i64) -> Vec<(usize, Placement)> {
    let t = &ix.tiles[ti];
    let mut out = Vec::new();
    for dy in -ring..=ring {
        for dx in -ring..=ring {
            if dx == 0 && dy == 0 { continue; }
            let key = format!("g17_{}_{}", t.gx + dx, t.gy + dy);
            let Some(&qi) = ix.by_key.get(&key) else { continue };
            let q = &ix.tiles[qi];
            if let Some(pl) = ix.placement(q.pack, t.pack) { out.push((qi, pl)); }
        }
    }
    out
}

pub fn build(packs: &mut Packs, ti: usize, grow_by: f64) -> Result<Built, String> {
    let ix = packs.ix;
    let it = &ix.tiles[ti];
    let pack = packs.get(it.pack)?;
    let own = pack.tiles.get(it.tile).ok_or("tile missing from its pack")?;
    let quad = grow(&own.quad, grow_by);
    let qb = bbox(&quad);
    let near = neighbours(ix, ti, 1);
    let mut others: Vec<(Arc<Pack>, usize, Placement)> = Vec::new();
    for (qi, pl) in &near {
        let q = &ix.tiles[*qi];
        others.push((packs.get(q.pack)?, q.tile, *pl));
    }
    // terrain: the tile's own block, grown with the neighbours' ground where it ends
    let o = &own.ter;
    let (x0, y0) = ((qb[0].floor() as i64 - TER_PAD).min(o.ox), (qb[1].floor() as i64 - TER_PAD).min(o.oy));
    let (x1, y1) = ((qb[2].ceil() as i64 + TER_PAD).max(o.ox + o.n as i64 - 1), (qb[3].ceil() as i64 + TER_PAD).max(o.oy + o.n as i64 - 1));
    let n = ((x1 - x0).max(y1 - y0) + 1) as usize;
    let ter = if n == o.n && x0 == o.ox && y0 == o.oy { o.clone() } else {
        let mut z = vec![f32::NAN; n * n];      // (NaN and a hole: no tile's ground there)
        let mut hole = vec![1u8; n * n];
        let mut paved = vec![0u8; n * n];
        let other_ter: Vec<(Terrain, &Ter, Placement)> = others.iter().map(|(p, t, pl)| {
            let tt = &p.tiles[*t].ter;
            (tt.audit(), tt, *pl)
        }).collect();
        let zguess = o.z.get(o.n * o.n / 2 + o.n / 2).copied().unwrap_or(0.0) as f64;
        for j in 0..n {
            for i in 0..n {
                let (gx, gy) = (x0 + i as i64, y0 + j as i64);
                let (li, lj) = (gx - o.ox, gy - o.oy);
                let k = j * n + i;
                if li >= 0 && lj >= 0 && li < o.n as i64 && lj < o.n as i64 {
                    let ok = lj as usize * o.n + li as usize;
                    z[k] = o.z[ok];
                    hole[k] = o.hole[ok];
                    paved[k] = o.paved[ok];
                    // (the block's last row and column hold no square of their own: their flags, in the grown grid,
                    // are the neighbours' ground's)
                    if li < o.n as i64 - 1 && lj < o.n as i64 - 1 { continue; }
                    hole[k] = 1;
                    for (at, tt, pl) in &other_ter {
                        let p = pl.invert_xy([gx as f64 + 0.5, gy as f64 + 0.5], zguess);
                        if at.at(p[0], p[1]).is_finite() {
                            hole[k] = 0;
                            paved[k] = tt.paved_at(p[0], p[1]).unwrap_or(false) as u8;
                            break;
                        }
                    }
                    continue;
                }
                for (at, tt, pl) in &other_ter {
                    let p = pl.invert_xy([gx as f64, gy as f64], zguess);
                    let zq = at.at(p[0], p[1]);
                    if !zq.is_finite() { continue; }
                    z[k] = pl.apply([p[0], p[1], zq])[2] as f32;
                    hole[k] = 0;
                    paved[k] = tt.paved_at(p[0], p[1]).unwrap_or(false) as u8;
                    break;
                }
            }
        }
        Ter { n, ox: x0, oy: y0, z, hole, paved }
    };
    // triangles: the tile's own entries, then each neighbour's (placed), those near the grown quad
    let mut tris: Vec<Tri> = Vec::new();
    let mut entry = 0u32;
    let keep = |p: &[[f64; 3]; 3]| {
        let (mut a, mut b) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        for v in p { for k in 0..2 { a[k] = a[k].min(v[k]); b[k] = b[k].max(v[k]); } }
        b[0] >= qb[0] - NEAR && a[0] <= qb[2] + NEAR && b[1] >= qb[1] - NEAR && a[1] <= qb[3] + NEAR
    };
    for e in &own.entries {
        for (flags, p) in &e.tris {
            tris.push(Tri { p: *p, entry, kind: e.kind, collides: e.collides, flags: *flags, cls: classify(p) });
        }
        entry += 1;
    }
    let own_entries = entry as usize;
    for (p, t, pl) in &others {
        for e in &p.tiles[*t].entries {
            let mut any = false;
            for (flags, tp) in &e.tris {
                let q = [pl.apply(tp[0]), pl.apply(tp[1]), pl.apply(tp[2])];
                if !keep(&q) { continue; }
                any = true;
                tris.push(Tri { p: q, entry, kind: e.kind, collides: e.collides, flags: *flags, cls: classify(&q) });
            }
            if any { entry += 1; }
        }
    }
    let lines: Vec<Line> = own.lines.iter().map(|l| Line { level: l.level, width: l.width, xy: l.xy.clone() }).collect();
    let terrain = ter.audit();
    Ok(Built { scene: Scene { quad, terrain, tris, lines }, own_quad: own.quad, lines: own.lines.clone(), ter,
               own_entries, aff: it.aff, key: it.key.clone() })
}

impl Built {
    pub fn latlon(&self, x: f64, y: f64) -> (f64, f64) {
        let a = &self.aff;
        (a[0] + a[1] * x + a[2] * y, a[3] + a[4] * x + a[5] * y)
    }
    pub fn owned(&self, x: f64, y: f64) -> bool { in_quad(&self.own_quad, [x, y]) }
}
