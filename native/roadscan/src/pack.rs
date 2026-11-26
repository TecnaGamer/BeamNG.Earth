//! The scanner's inputs, written by tools/roadscan/export.py (little-endian throughout).
//!
//! Pack (one build folder: one anchor frame, one or more geotiles), "RSCANPK1":
//!   f64 lat0, lon0 (the frame); u32 tiles; per tile:
//!     str key; quad 8 x f64 (the tile's corners in the frame);
//!     terrain: u32 n, i64 ox, i64 oy, n*n f32 heights (row-major y, x), n*n u8 hole flags, n*n u8 paved flags;
//!     3 x f64 vertex origin; u32 entries; each: u8 kind (roadsys audit KIND_*), u8 collides, u32 triangles, per
//!       triangle u8 flags + 9 x f32 (offsets from the vertex origin: 0.03 mm steps within the tile's 600 m);
//!     u32 lines; each: str id, u8 level (0 ground, 1 bridge, 2 tunnel), f64 width, f64 drive width, u8 lanes,
//!       u8 oneway, u8 paved, str class, u32 points, points x 2 f64.
//! Index (a scanned state: every pack), "RSCANIX1":
//!   u32 packs; per pack: str path (relative to the index), f64 lat0, lon0;
//!   u32 tiles; per tile: str key, u32 pack, u32 tile (its place in the pack), 6 x f64 affine (lat = a0 + a1 x + a2 y,
//!     lon = a3 + a4 x + a5 y in its pack's frame, fitted over the tile by the server's own world_to_geodetic);
//!   u32 placements; each: u32 src pack, u32 dst pack, 9 x f64 R, 3 x f64 t, 3 x f64 c, f64 pivot z
//!     (geotiles.placement: p' = R p + t + c (p.z - pivot.z), the mod's own way of placing a tile in another world).
//! Route, "RSROUTE1": u32 points, points x (lat, lon) f64.
//! str: u16 length + UTF-8 bytes.

pub struct Rd<'a> { pub b: &'a [u8], pub i: usize }

impl<'a> Rd<'a> {
    pub fn new(b: &'a [u8]) -> Rd<'a> { Rd { b, i: 0 } }
    pub fn take(&mut self, n: usize) -> Result<&'a [u8], String> {
        if n > self.b.len() - self.i { return Err("truncated input".into()); }
        let s = &self.b[self.i..self.i + n];
        self.i += n;
        Ok(s)
    }
    pub fn u8(&mut self) -> Result<u8, String> { Ok(self.take(1)?[0]) }
    pub fn u16(&mut self) -> Result<u16, String> { Ok(u16::from_le_bytes(self.take(2)?.try_into().unwrap())) }
    pub fn u32(&mut self) -> Result<u32, String> { Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap())) }
    pub fn i64(&mut self) -> Result<i64, String> { Ok(i64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
    pub fn f64(&mut self) -> Result<f64, String> { Ok(f64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
    pub fn fin(&mut self) -> Result<f64, String> {
        let v = self.f64()?;
        if v.is_finite() { Ok(v) } else { Err("non-finite number".into()) }
    }
    pub fn str(&mut self) -> Result<String, String> {
        let n = self.u16()? as usize;
        String::from_utf8(self.take(n)?.to_vec()).map_err(|_| "bad UTF-8".to_string())
    }
    pub fn magic(&mut self, m: &[u8; 8]) -> Result<(), String> {
        if self.take(8)? != m { return Err(format!("bad magic, expected {}", String::from_utf8_lossy(m))); }
        Ok(())
    }
    /// A count of items of at least `each` bytes that the rest of the input can still hold.
    pub fn count(&mut self, each: usize) -> Result<usize, String> {
        let n = self.u32()? as usize;
        if n.saturating_mul(each.max(1)) > self.b.len() - self.i { return Err("count past the end of the input".into()); }
        Ok(n)
    }
}

#[derive(Clone)]
pub struct LineMeta { pub id: String, pub level: u8, pub width: f64, pub drive: f64, pub lanes: u8, pub oneway: bool,
                      pub paved: bool, pub cls: String, pub xy: Vec<[f64; 2]> }

#[derive(Clone)]
pub struct Entry { pub kind: u8, pub collides: bool, pub tris: Vec<(u8, [[f64; 3]; 3])> }

#[derive(Clone)]
pub struct Ter { pub n: usize, pub ox: i64, pub oy: i64, pub z: Vec<f32>, pub hole: Vec<u8>, pub paved: Vec<u8> }

impl Ter {
    /// The audit's terrain (alternating diagonals, NaN in a hole or outside).
    pub fn audit(&self) -> roadsys::audit::Terrain {
        roadsys::audit::Terrain { n: self.n, ox: self.ox, oy: self.oy, z: self.z.clone(), hole: self.hole.clone() }
    }
    /// Paved flag of the lattice square holding (x, y) (its south-west node), None outside.
    pub fn paved_at(&self, x: f64, y: f64) -> Option<bool> {
        let (fx, fy) = ((x - self.ox as f64).floor(), (y - self.oy as f64).floor());
        if !(fx >= 0.0 && fy >= 0.0 && fx < self.n as f64 && fy < self.n as f64) { return None; }
        Some(self.paved[fy as usize * self.n + fx as usize] != 0)
    }
}

#[derive(Clone)]
pub struct Tile { pub key: String, pub quad: [[f64; 2]; 4], pub ter: Ter, pub entries: Vec<Entry>, pub lines: Vec<LineMeta> }

pub struct Pack { pub lat0: f64, pub lon0: f64, pub tiles: Vec<Tile> }

pub fn read_pack(b: &[u8]) -> Result<Pack, String> {
    let mut r = Rd::new(b);
    r.magic(b"RSCANPK1")?;
    let (lat0, lon0) = (r.fin()?, r.fin()?);
    let nt = r.count(16)?;
    let mut tiles = Vec::with_capacity(nt);
    for _ in 0..nt {
        let key = r.str()?;
        let mut quad = [[0.0; 2]; 4];
        for q in quad.iter_mut() { q[0] = r.fin()?; q[1] = r.fin()?; }
        let n = r.u32()? as usize;
        let (ox, oy) = (r.i64()?, r.i64()?);
        if n > 20000 || n * n * 6 > r.b.len() - r.i { return Err("terrain too large".into()); }
        let zb = r.take(n * n * 4)?;
        let z: Vec<f32> = zb.chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect();
        let mut hole = r.take(n * n)?.to_vec();
        let paved = r.take(n * n)?.to_vec();
        clear_border(&mut hole, n);
        let org = [r.fin()?, r.fin()?, r.fin()?];
        let ne = r.count(6)?;
        let mut entries = Vec::with_capacity(ne);
        for _ in 0..ne {
            let kind = r.u8()?;
            let collides = r.u8()? != 0;
            let k = r.count(37)?;
            let mut tris = Vec::with_capacity(k);
            let raw = r.take(k * 37)?;
            for t in raw.chunks_exact(37) {
                let mut p = [[0.0; 3]; 3];
                for (j, v) in p.iter_mut().enumerate() {
                    for (c, x) in v.iter_mut().enumerate() {
                        let o = 1 + (j * 3 + c) * 4;
                        let f = f32::from_le_bytes(t[o..o + 4].try_into().unwrap()) as f64;
                        if !f.is_finite() { return Err("non-finite vertex".into()); }
                        *x = org[c] + f;
                    }
                }
                tris.push((t[0], p));
            }
            entries.push(Entry { kind, collides, tris });
        }
        let nl = r.count(30)?;
        let mut lines = Vec::with_capacity(nl);
        for _ in 0..nl {
            let id = r.str()?;
            let level = r.u8()?;
            let width = r.fin()?;
            let drive = r.fin()?;
            let lanes = r.u8()?;
            let oneway = r.u8()? != 0;
            let paved = r.u8()? != 0;
            let cls = r.str()?;
            let np = r.count(16)?;
            let mut xy = Vec::with_capacity(np);
            for _ in 0..np { xy.push([r.fin()?, r.fin()?]); }
            lines.push(LineMeta { id, level, width, drive, lanes, oneway, paved, cls, xy });
        }
        tiles.push(Tile { key, quad, ter: Ter { n, ox, oy, z, hole, paved }, entries, lines });
    }
    Ok(Pack { lat0, lon0, tiles })
}

/// A corpus block's one-node border is all holes (build.py cuts its block there; the game's blocks are larger and go
/// on): cleared, so that two tiles' ground meets at their seam instead of a ditch of holes.
fn clear_border(hole: &mut [u8], n: usize) {
    if n < 3 { return; }
    let ring: Vec<usize> = (0..n).flat_map(|i| [i, (n - 1) * n + i, i * n, i * n + n - 1]).collect();
    let holes = ring.iter().filter(|&&k| hole[k] != 0).count();
    if holes * 100 >= ring.len() * 95 { for k in ring { hole[k] = 0; } }
}

#[derive(Clone, Copy)]
pub struct Placement { pub r: [f64; 9], pub t: [f64; 3], pub c: [f64; 3], pub pz: f64 }

impl Placement {
    pub const IDENTITY: Placement = Placement { r: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], t: [0.0; 3], c: [0.0; 3], pz: 0.0 };
    pub fn apply(&self, p: [f64; 3]) -> [f64; 3] {
        let r = &self.r;
        let dz = p[2] - self.pz;
        [r[0] * p[0] + r[1] * p[1] + r[2] * p[2] + self.t[0] + self.c[0] * dz,
         r[3] * p[0] + r[4] * p[1] + r[5] * p[2] + self.t[1] + self.c[1] * dz,
         r[6] * p[0] + r[7] * p[1] + r[8] * p[2] + self.t[2] + self.c[2] * dz]
    }
    /// The source point placed at q's x, y (z given): two Newton steps on the near-identity map (exact to well under
    /// a millimetre for neighbouring tiles).
    pub fn invert_xy(&self, q: [f64; 2], z: f64) -> [f64; 2] {
        let mut p = [q[0] - self.t[0], q[1] - self.t[1]];
        for _ in 0..3 {
            let f = self.apply([p[0], p[1], z]);
            p = [p[0] - (f[0] - q[0]), p[1] - (f[1] - q[1])];
        }
        p
    }
}

pub struct IndexTile { pub key: String, pub gx: i64, pub gy: i64, pub pack: usize, pub tile: usize, pub aff: [f64; 6] }

pub struct Index {
    pub dir: std::path::PathBuf,
    pub packs: Vec<(String, f64, f64)>,
    pub tiles: Vec<IndexTile>,
    pub place: std::collections::BTreeMap<(usize, usize), Placement>,
    pub by_key: std::collections::BTreeMap<String, usize>,
}

/// (gx, gy) of a geotile key "g17_X_Y".
pub fn key_xy(key: &str) -> Option<(i64, i64)> {
    let mut it = key.split('_');
    let _z = it.next()?;
    let x = it.next()?.parse().ok()?;
    let y = it.next()?.parse().ok()?;
    Some((x, y))
}

pub fn read_index(path: &std::path::Path) -> Result<Index, String> {
    let b = std::fs::read(path).map_err(|e| format!("{}: {}", path.display(), e))?;
    let mut r = Rd::new(&b);
    r.magic(b"RSCANIX1")?;
    let np = r.count(18)?;
    let mut packs = Vec::with_capacity(np);
    for _ in 0..np {
        let p = r.str()?;
        if p.contains("..") || p.starts_with('/') || p.contains('\\') { return Err(format!("pack path outside the index: {}", p)); }
        packs.push((p, r.fin()?, r.fin()?));
    }
    let nt = r.count(58)?;
    let mut tiles = Vec::with_capacity(nt);
    let mut by_key = std::collections::BTreeMap::new();
    for i in 0..nt {
        let key = r.str()?;
        let pack = r.u32()? as usize;
        let tile = r.u32()? as usize;
        if pack >= np { return Err("tile of a missing pack".into()); }
        let mut aff = [0.0; 6];
        for a in aff.iter_mut() { *a = r.fin()?; }
        let (gx, gy) = key_xy(&key).ok_or_else(|| format!("bad tile key {}", key))?;
        by_key.insert(key.clone(), i);
        tiles.push(IndexTile { key, gx, gy, pack, tile, aff });
    }
    let nq = r.count(8 + 16 * 8)?;
    let mut place = std::collections::BTreeMap::new();
    for _ in 0..nq {
        let (s, d) = (r.u32()? as usize, r.u32()? as usize);
        let mut pl = Placement::IDENTITY;
        for v in pl.r.iter_mut() { *v = r.fin()?; }
        for v in pl.t.iter_mut() { *v = r.fin()?; }
        for v in pl.c.iter_mut() { *v = r.fin()?; }
        pl.pz = r.fin()?;
        place.insert((s, d), pl);
    }
    Ok(Index { dir: path.parent().unwrap_or(std::path::Path::new(".")).to_path_buf(), packs, tiles, place, by_key })
}

pub fn read_route(path: &std::path::Path) -> Result<Vec<[f64; 2]>, String> {
    let b = std::fs::read(path).map_err(|e| format!("{}: {}", path.display(), e))?;
    let mut r = Rd::new(&b);
    r.magic(b"RSROUTE1")?;
    let n = r.count(16)?;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n { out.push([r.fin()?, r.fin()?]); }
    Ok(out)
}

impl Index {
    pub fn placement(&self, src_pack: usize, dst_pack: usize) -> Option<Placement> {
        if src_pack == dst_pack { return Some(Placement::IDENTITY); }
        self.place.get(&(src_pack, dst_pack)).copied()
    }
    pub fn load(&self, pack: usize) -> Result<Pack, String> {
        let p = self.dir.join(&self.packs[pack].0);
        let b = std::fs::read(&p).map_err(|e| format!("{}: {}", p.display(), e))?;
        read_pack(&b).map_err(|e| format!("{}: {}", p.display(), e))
    }
}
