//! M0 audit (bridgerewrite/DESIGN.md section 10): the design's drivability checks on any built road scene, today's
//! output (tools/widecorpus/checks.py hands each geotile's emitted meshes, its physics terrain block and the source
//! map's lines) and the shadow product alike. One pass over in-lane sample cells (0.5 m) of the mapped lanes:
//!
//! - `double_owner`: two colliders, or a collider and the terrain, within 5 cm at a lane point (D2);
//! - `envelope_intrusion`: a collider other than the drivable surface inside the drivable envelope (every lane point
//!   plus 4.5 m above it); overhead structure parts (deck / tunnel / rail parts at least 1 m up) count apart as
//!   `envelope_overhead` (low clearance) (D2, D4);
//! - `opening_blocked`: a lower lane point under a structure whose opening is under 2.5 m (fill, wall, a deck lying
//!   on the road) (D4);
//! - `pier_in_face`: a tall steep structure face (pier, abutment, wall) inside a lower lane face or within 1.5 m (D16);
//! - `parapet_in_cw_m`: barrier / parapet walls crossing a lane at its own level, in metres of lane cells (D8);
//! - `handover_lip`: the step between a colliding top's free edge and the surface it hands over to (the terrain, or
//!   another collider at a joint) where a lane crosses it (D1);
//! - clearance under structures against the floors (`clearance_short`: under 4.25 m to the lowest overhead part;
//!   `clearance_excess`: road to road over 7.6 m, the ski-ramp side) (D3, D4);
//! - `tcracks` (a vertex of another piece strictly inside a free edge, in lanes), `folds` (down-facing triangles on
//!   an up-facing road surface in lanes), zero-area triangles, the longest triangle edge (D6, D7).
//!
//! Input (little-endian), written by checks.py's `audit_input`:
//!   "RSAUDIT1"; quad: 8 x f64 (x0 y0 .. x3 y3);
//!   terrain: u32 n, i64 ox, i64 oy, n*n f32 heights (row-major y, x), n*n u8 hole flags;
//!   u32 entries; each: u8 kind (KIND_*), u8 collides, u32 triangles, per triangle u8 flags (FLAG_*) + 9 x f64
//!     (CCW seen from above for an upward face);
//!   u32 lines; each: u8 level (0 ground, 1 bridge, 2 tunnel), f64 width, u32 points, points x 2 f64.
//! Output: a JSON object of metrics (numbers only), stable key order.
use std::collections::BTreeMap;

pub const KIND_ROAD: u8 = 0;
pub const KIND_WALK: u8 = 1;
pub const KIND_WALKCOL: u8 = 2;
pub const KIND_DECK: u8 = 3;
pub const KIND_TUNNEL: u8 = 4;
pub const KIND_BARRIER: u8 = 5;
pub const KIND_BUSCOL: u8 = 6;
pub const KIND_RAIL: u8 = 7;
pub const KIND_RAILCOL: u8 = 8;
pub const KIND_OTHER: u8 = 9;

pub const FLAG_DECK_TOP: u8 = 1;
pub const FLAG_DECK_SIDE: u8 = 2;
pub const FLAG_CURB: u8 = 4;
pub const FLAG_MARK: u8 = 8;

const CELL: f64 = 0.5;          // lane sample cells
const MARGIN: f64 = 0.5;        // lane interior: the mapped half width less this
const GRID: f64 = 2.0;          // triangle buckets
const OWN: f64 = 0.05;          // double owner / own surface tolerance
const ENVELOPE: f64 = 4.5;
const PIER_NEAR: f64 = 1.5;

#[derive(Clone)]
pub struct Terrain { pub n: usize, pub ox: i64, pub oy: i64, pub z: Vec<f32>, pub hole: Vec<u8> }

impl Terrain {
    /// checks.Terrain.sample: Torque's alternating diagonals by lattice parity; NaN in a hole or outside.
    pub fn at(&self, x: f64, y: f64) -> f64 {
        let n = self.n;
        if n < 2 { return f64::NAN; }
        let lx = x - self.ox as f64;
        let ly = y - self.oy as f64;
        let (fx, fy) = (lx.floor(), ly.floor());
        if !(fx >= 0.0 && fy >= 0.0 && fx < (n - 1) as f64 && fy < (n - 1) as f64) { return f64::NAN; }
        let (ix, iy) = (fx as usize, fy as usize);
        if self.hole[iy * n + ix] != 0 { return f64::NAN; }
        let (u, v) = (lx - fx, ly - fy);
        let g = |x: usize, y: usize| self.z[y * n + x] as f64;
        let (a, b, c, d) = (g(ix, iy), g(ix + 1, iy), g(ix, iy + 1), g(ix + 1, iy + 1));
        let odd = ((ix as i64 + iy as i64 + self.ox + self.oy).rem_euclid(2)) == 1;
        if odd {
            if u + v <= 1.0 { a + u * (b - a) + v * (c - a) } else { d + (1.0 - u) * (c - d) + (1.0 - v) * (b - d) }
        } else if v > u { a + v * (c - a) + u * (d - c) } else { a + u * (b - a) + v * (d - b) }
    }
}

#[derive(Clone, Copy)]
pub struct Tri { pub p: [[f64; 3]; 3], pub entry: u32, pub kind: u8, pub collides: bool, pub flags: u8, pub cls: u8 }

pub const UP: u8 = 0;
pub const DOWN: u8 = 1;
pub const STEEP: u8 = 2;
pub const FLAT0: u8 = 3;     // zero area

pub struct Line { pub level: u8, pub width: f64, pub xy: Vec<[f64; 2]> }

pub struct Scene { pub quad: [[f64; 2]; 4], pub terrain: Terrain, pub tris: Vec<Tri>, pub lines: Vec<Line> }

struct Rd<'a> { b: &'a [u8], i: usize }
impl Rd<'_> {
    fn take(&mut self, n: usize) -> Result<&[u8], String> {
        if self.i + n > self.b.len() { return Err("audit input: truncated".into()); }
        let s = &self.b[self.i..self.i + n];
        self.i += n;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, String> { Ok(self.take(1)?[0]) }
    fn u32(&mut self) -> Result<u32, String> { Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap())) }
    fn i64(&mut self) -> Result<i64, String> { Ok(i64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
    fn f64(&mut self) -> Result<f64, String> { Ok(f64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
    fn f32(&mut self) -> Result<f32, String> { Ok(f32::from_le_bytes(self.take(4)?.try_into().unwrap())) }
}

fn classify(p: &[[f64; 3]; 3]) -> u8 {
    let (ux, uy, uz) = (p[1][0] - p[0][0], p[1][1] - p[0][1], p[1][2] - p[0][2]);
    let (vx, vy, vz) = (p[2][0] - p[0][0], p[2][1] - p[0][1], p[2][2] - p[0][2]);
    let n = [uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx];
    let l = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
    if !(l > 1e-9) { return FLAT0; }
    if n[2] > 0.45 * l { UP } else if n[2] < -0.7 * l { DOWN } else { STEEP }
}

pub fn parse(b: &[u8]) -> Result<Scene, String> {
    let mut r = Rd { b, i: 0 };
    if r.take(8)? != b"RSAUDIT1" { return Err("audit input: bad magic".into()); }
    let mut quad = [[0.0; 2]; 4];
    for q in quad.iter_mut() { q[0] = r.f64()?; q[1] = r.f64()?; }
    let n = r.u32()? as usize;
    let (ox, oy) = (r.i64()?, r.i64()?);
    if n > 20000 { return Err("audit input: terrain too large".into()); }
    let mut z = Vec::with_capacity(n * n);
    for _ in 0..n * n { z.push(r.f32()?); }
    let hole = r.take(n * n)?.to_vec();
    let terrain = Terrain { n, ox, oy, z, hole };
    let ne = r.u32()? as usize;
    let mut tris = Vec::new();
    for e in 0..ne {
        let kind = r.u8()?;
        let collides = r.u8()? != 0;
        let nt = r.u32()? as usize;
        for _ in 0..nt {
            let flags = r.u8()?;
            let mut p = [[0.0; 3]; 3];
            for v in p.iter_mut() { for c in v.iter_mut() { *c = r.f64()?; } }
            if p.iter().any(|v| v.iter().any(|c| !c.is_finite())) { return Err("audit input: non-finite vertex".into()); }
            let cls = classify(&p);
            tris.push(Tri { p, entry: e as u32, kind, collides, flags, cls });
        }
    }
    let nl = r.u32()? as usize;
    let mut lines = Vec::with_capacity(nl);
    for _ in 0..nl {
        let level = r.u8()?;
        let width = r.f64()?;
        let np = r.u32()? as usize;
        let mut xy = Vec::with_capacity(np);
        for _ in 0..np { let x = r.f64()?; let y = r.f64()?; xy.push([x, y]); }
        lines.push(Line { level, width, xy });
    }
    Ok(Scene { quad, terrain, tris, lines })
}

fn cross2(a: [f64; 2], b: [f64; 2]) -> f64 { a[0] * b[1] - a[1] * b[0] }

fn in_quad(q: &[[f64; 2]; 4], p: [f64; 2]) -> bool {
    let mut pos = true;
    let mut neg = true;
    for k in 0..4 {
        let (a, b) = (q[k], q[(k + 1) % 4]);
        let t = cross2([b[0] - a[0], b[1] - a[1]], [p[0] - a[0], p[1] - a[1]]);
        if t < 0.0 { pos = false; }
        if t > 0.0 { neg = false; }
    }
    pos || neg
}

/// z of triangle t's plane at (x, y) when (x, y) lies inside its XY projection.
fn ray(t: &Tri, x: f64, y: f64) -> Option<f64> {
    let p = &t.p;
    let d = (p[1][0] - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (p[1][1] - p[0][1]);
    if d.abs() < 1e-12 { return None; }
    let w1 = ((x - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (y - p[0][1])) / d;
    let w2 = ((p[1][0] - p[0][0]) * (y - p[0][1]) - (x - p[0][0]) * (p[1][1] - p[0][1])) / d;
    let e = -1e-9;
    if w1 < e || w2 < e || w1 + w2 > 1.0 - e { return None; }
    Some(p[0][2] + w1 * (p[1][2] - p[0][2]) + w2 * (p[2][2] - p[0][2]))
}

/// The z range of triangle t clipped to the square [x0, x1] x [y0, y1] (exact: Sutherland-Hodgman in 3D).
fn clip_z(t: &Tri, x0: f64, y0: f64, x1: f64, y1: f64) -> Option<(f64, f64)> {
    let mut poly: Vec<[f64; 3]> = t.p.to_vec();
    let mut tmp: Vec<[f64; 3]> = Vec::with_capacity(8);
    for (axis, c, keep_ge) in [(0usize, x0, true), (0, x1, false), (1, y0, true), (1, y1, false)] {
        tmp.clear();
        let n = poly.len();
        for i in 0..n {
            let (a, b) = (poly[i], poly[(i + 1) % n]);
            let ia = if keep_ge { a[axis] >= c } else { a[axis] <= c };
            let ib = if keep_ge { b[axis] >= c } else { b[axis] <= c };
            if ia { tmp.push(a); }
            if ia != ib {
                let u = (c - a[axis]) / (b[axis] - a[axis]);
                tmp.push([a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u]);
            }
        }
        std::mem::swap(&mut poly, &mut tmp);
        if poly.is_empty() { return None; }
    }
    let mut lo = f64::INFINITY;
    let mut hi = f64::NEG_INFINITY;
    for v in &poly { if v[2] < lo { lo = v[2]; } if v[2] > hi { hi = v[2]; } }
    Some((lo, hi))
}

struct Grid { cells: BTreeMap<(i64, i64), Vec<u32>> }

impl Grid {
    fn new(tris: &[Tri], keep: impl Fn(&Tri) -> bool) -> Grid {
        let mut cells: BTreeMap<(i64, i64), Vec<u32>> = BTreeMap::new();
        for (k, t) in tris.iter().enumerate() {
            if !keep(t) { continue; }
            let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
            for v in &t.p { x0 = x0.min(v[0]); y0 = y0.min(v[1]); x1 = x1.max(v[0]); y1 = y1.max(v[1]); }
            let (i0, i1) = ((x0 / GRID).floor() as i64, (x1 / GRID).floor() as i64);
            let (j0, j1) = ((y0 / GRID).floor() as i64, (y1 / GRID).floor() as i64);
            if (i1 - i0 + 1) * (j1 - j0 + 1) > 40000 { continue; }    // (a triangle over 400 m: counted as long below)
            for i in i0..=i1 { for j in j0..=j1 { cells.entry((i, j)).or_default().push(k as u32); } }
        }
        Grid { cells }
    }
    fn at(&self, x: f64, y: f64) -> &[u32] {
        self.cells.get(&((x / GRID).floor() as i64, (y / GRID).floor() as i64)).map(|v| v.as_slice()).unwrap_or(&[])
    }
    /// Triangle ids of every cell meeting the square (deduplicated, ascending).
    fn around(&self, x0: f64, y0: f64, x1: f64, y1: f64, out: &mut Vec<u32>) {
        out.clear();
        for i in (x0 / GRID).floor() as i64..=(x1 / GRID).floor() as i64 {
            for j in (y0 / GRID).floor() as i64..=(y1 / GRID).floor() as i64 {
                if let Some(v) = self.cells.get(&(i, j)) { out.extend_from_slice(v); }
            }
        }
        out.sort_unstable();
        out.dedup();
    }
}

/// A lane sample cell: centre, level, the line it belongs to.
struct Sample { x: f64, y: f64, level: u8 }

fn samples(sc: &Scene) -> Vec<Sample> {
    let q = &sc.quad;
    let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
    for p in q { for k in 0..2 { lo[k] = lo[k].min(p[k]); hi[k] = hi[k].max(p[k]); } }
    let mut out = Vec::new();
    for l in &sc.lines {
        let h = l.width / 2.0 - MARGIN;
        let nlat = if h < CELL / 2.0 { 0 } else { ((h - CELL / 2.0) / CELL).floor() as i64 };
        let mut dist = 0.0;
        for j in 0..l.xy.len().saturating_sub(1) {
            let (a, b) = (l.xy[j], l.xy[j + 1]);
            let seg = ((b[0] - a[0]) * (b[0] - a[0]) + (b[1] - a[1]) * (b[1] - a[1])).sqrt();
            let d0 = dist;
            dist += seg;
            if seg < 1e-3 { continue; }
            let near = a[0].max(b[0]) >= lo[0] - 20.0 && a[0].min(b[0]) <= hi[0] + 20.0
                && a[1].max(b[1]) >= lo[1] - 20.0 && a[1].min(b[1]) <= hi[1] + 20.0;
            if !near { continue; }
            let t = [(b[0] - a[0]) / seg, (b[1] - a[1]) / seg];
            let nrm = [-t[1], t[0]];
            // stations on the line's own 0.5 m grid (from its start), so a bend does not shift them
            let mut s = (d0 / CELL).ceil() * CELL + CELL / 2.0;
            if s - CELL >= d0 { s -= CELL; }
            while s < dist {
                let u = s - d0;
                let c = [a[0] + t[0] * u, a[1] + t[1] * u];
                for k in -nlat..=nlat {
                    let o = k as f64 * CELL;
                    let p = [c[0] + nrm[0] * o, c[1] + nrm[1] * o];
                    if in_quad(q, p) { out.push(Sample { x: p[0], y: p[1], level: l.level }); }
                }
                s += CELL;
            }
        }
    }
    out
}

/// In-lane test for points (edge samples): within the mapped half width less 0.3 m of a line, any level.
struct LaneMask { cells: BTreeMap<(i64, i64), Vec<(usize, usize)>> }

impl LaneMask {
    fn new(lines: &[Line]) -> LaneMask {
        let mut cells: BTreeMap<(i64, i64), Vec<(usize, usize)>> = BTreeMap::new();
        for (li, l) in lines.iter().enumerate() {
            let r = l.width / 2.0;
            for j in 0..l.xy.len().saturating_sub(1) {
                let (a, b) = (l.xy[j], l.xy[j + 1]);
                let (x0, x1) = (a[0].min(b[0]) - r, a[0].max(b[0]) + r);
                let (y0, y1) = (a[1].min(b[1]) - r, a[1].max(b[1]) + r);
                let (i0, i1) = ((x0 / 8.0).floor() as i64, (x1 / 8.0).floor() as i64);
                let (j0, j1) = ((y0 / 8.0).floor() as i64, (y1 / 8.0).floor() as i64);
                if (i1 - i0 + 1) * (j1 - j0 + 1) > 20000 { continue; }
                for i in i0..=i1 { for jj in j0..=j1 { cells.entry((i, jj)).or_default().push((li, j)); } }
            }
        }
        LaneMask { cells }
    }
    fn inside(&self, lines: &[Line], x: f64, y: f64, margin: f64) -> bool {
        let Some(v) = self.cells.get(&((x / 8.0).floor() as i64, (y / 8.0).floor() as i64)) else { return false };
        for &(li, j) in v {
            let l = &lines[li];
            let (a, b) = (l.xy[j], l.xy[j + 1]);
            let ab = [b[0] - a[0], b[1] - a[1]];
            let l2 = ab[0] * ab[0] + ab[1] * ab[1];
            if l2 < 1e-12 { continue; }
            let u = ((x - a[0]) * ab[0] + (y - a[1]) * ab[1]) / l2;
            if !(0.0..=1.0).contains(&u) { continue; }
            let d = cross2(ab, [x - a[0], y - a[1]]).abs() / l2.sqrt();
            if d <= l.width / 2.0 - margin { return true; }
        }
        false
    }
}

fn pct(v: &mut [f64], q: f64) -> f64 {
    if v.is_empty() { return 0.0; }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let k = ((v.len() as f64 - 1.0) * q).round() as usize;
    v[k.min(v.len() - 1)]
}

fn qkey(v: [f64; 3]) -> (i64, i64, i64) {
    ((v[0] * 1000.0).round() as i64, (v[1] * 1000.0).round() as i64, (v[2] * 1000.0).round() as i64)
}

#[derive(Default)]
pub struct Metrics { pub m: BTreeMap<&'static str, f64> }

impl Metrics {
    fn add(&mut self, k: &'static str, v: f64) { *self.m.entry(k).or_insert(0.0) += v; }
    fn max(&mut self, k: &'static str, v: f64) { let e = self.m.entry(k).or_insert(0.0); if v > *e { *e = v; } }
    pub fn json(&self) -> String {
        let mut s = String::from("{");
        for (i, (k, v)) in self.m.iter().enumerate() {
            if i > 0 { s.push(','); }
            let v = if v.is_finite() { *v } else { 0.0 };
            s.push_str(&format!("\"{}\":{}", k, fmt(v)));
        }
        s.push('}');
        s
    }
}

fn fmt(v: f64) -> String {
    if v == v.trunc() && v.abs() < 1e15 { format!("{}", v as i64) } else { format!("{:.6}", v) }
}

/// The audit of one scene.
pub fn audit(sc: &Scene) -> Metrics {
    let mut m = Metrics::default();
    let tris = &sc.tris;
    let ter = &sc.terrain;
    // colliding up / down faces (vertical rays) and every colliding face (column clipping)
    let col_flat = Grid::new(tris, |t| t.collides && (t.cls == UP || t.cls == DOWN));
    let col_steep = Grid::new(tris, |t| t.collides && t.cls == STEEP);
    let deck_top = Grid::new(tris, |t| t.kind == KIND_DECK && t.flags & FLAG_DECK_TOP != 0 && t.cls == UP);
    let floors = Grid::new(tris, |t| (t.kind == KIND_DECK && t.flags & FLAG_DECK_SIDE != 0 || t.kind == KIND_TUNNEL) && t.cls == UP);
    let ss = samples(sc);
    let area = CELL * CELL;
    let mut cand: Vec<u32> = Vec::new();
    let mut r2r: Vec<f64> = Vec::new();
    let mut hits: Vec<(f64, u32)> = Vec::new();
    for s in &ss {
        let (x, y) = (s.x, s.y);
        m.add("lane_cells", 1.0);
        let zt = ter.at(x, y);
        hits.clear();
        for &k in col_flat.at(x, y) {
            let t = &tris[k as usize];
            if t.cls != UP { continue; }
            if let Some(z) = ray(t, x, y) { hits.push((z, t.entry)); }
        }
        // the drivable reference at the line's level
        let zref = match s.level {
            0 => if zt.is_finite() { zt } else {
                // a hole in the terrain (a portal, a cut-out): the highest colliding top there
                hits.iter().map(|h| h.0).fold(f64::NAN, f64::max)
            },
            1 => deck_top.at(x, y).iter().filter_map(|&k| ray(&tris[k as usize], x, y)).fold(f64::NAN, f64::max),
            _ => floors.at(x, y).iter().filter_map(|&k| ray(&tris[k as usize], x, y)).fold(f64::NAN, f64::max),
        };
        if !zref.is_finite() { m.add("hole_cells", 1.0); continue; }
        let win = if s.level == 0 { 0.5 } else { 0.3 };
        let mut drive = if s.level == 0 && zt.is_finite() { zt } else { f64::NEG_INFINITY };
        for &(z, _) in &hits { if z >= zref - win && z <= zref + win && z > drive { drive = z; } }
        if !drive.is_finite() { drive = zref; }
        // double owner: distinct surfaces within 5 cm under the drivable one
        let mut owners: Vec<(u32, f64)> = Vec::new();
        let terrain_own = zt.is_finite() && zt >= drive - OWN && zt <= drive + 1e-9;
        for &(z, e) in &hits {
            if z < drive - OWN || z > drive + 1e-9 { continue; }
            if owners.iter().any(|&(oe, oz)| oe == e && (oz - z).abs() < 0.001) { continue; }
            owners.push((e, z));
        }
        let n_own = owners.len() + terrain_own as usize;
        if n_own >= 2 {
            m.add("double_owner_m2", area);
            if terrain_own { m.add("double_owner_terrain_m2", area); } else { m.add("double_owner_collider_m2", area); }
            if std::env::var("ROADSYS_AUDIT_DUMP").is_ok() { eprintln!("DO {:.1} {:.1} lvl {} ter {} zt {:.2} drive {:.2} n {}", x, y, s.level, terrain_own, zt, drive, n_own); }
        }
        // the envelope above: colliding faces in (drive + 5 cm, drive + 4.5 m]
        let (lo, hi) = (drive + OWN, drive + ENVELOPE);
        let mut over_min = f64::INFINITY;       // lowest overhead structure part above the lane point
        let mut top_above = f64::INFINITY;      // lowest deck top above (road to road)
        let mut intr = false;
        let mut intr_kind = [false; 10];
        for &k in col_flat.at(x, y) {
            let t = &tris[k as usize];
            let Some(z) = ray(t, x, y) else { continue };
            if z <= lo { continue; }
            let overhead = matches!(t.kind, KIND_DECK | KIND_TUNNEL | KIND_RAIL | KIND_RAILCOL) && z >= drive + 1.0;
            if overhead {
                if z < over_min { over_min = z; }
                if t.kind == KIND_DECK && t.cls == UP && t.flags & FLAG_DECK_TOP != 0 && z < top_above && z > drive + 2.0 { top_above = z; }
            } else if z <= hi { intr = true; intr_kind[t.kind.min(9) as usize] = true; }
        }
        // overhead parts further up (above the envelope: road to road)
        if s.level == 0 {
            for &k in deck_top.at(x, y) {
                if let Some(z) = ray(&tris[k as usize], x, y) { if z > drive + 2.0 && z < top_above && z < drive + 15.0 { top_above = z; } }
            }
        }
        let h = CELL / 2.0;
        col_steep.around(x - h, y - h, x + h, y + h, &mut cand);
        let mut parapet = false;
        for &k in &cand {
            let t = &tris[k as usize];
            let Some((zl, zh)) = clip_z(t, x - h, y - h, x + h, y + h) else { continue };
            if zh <= lo || zl > hi { continue; }
            let overhead = matches!(t.kind, KIND_DECK | KIND_TUNNEL | KIND_RAIL | KIND_RAILCOL) && zl >= drive + 1.0;
            if overhead { if zl < over_min { over_min = zl; } continue; }
            intr = true;
            intr_kind[t.kind.min(9) as usize] = true;
            if matches!(t.kind, KIND_BARRIER | KIND_DECK | KIND_TUNNEL | KIND_OTHER) && t.flags & FLAG_CURB == 0
                && zl <= drive + 0.15 && zh >= drive + 0.6 { parapet = true; }
        }
        if intr {
            m.add("envelope_intrusion_m2", area);
            for (kind, name) in [(KIND_BARRIER, "envelope_intrusion_barrier_m2"), (KIND_DECK, "envelope_intrusion_deck_m2"),
                                 (KIND_WALK, "envelope_intrusion_walk_m2"), (KIND_WALKCOL, "envelope_intrusion_walk_m2"),
                                 (KIND_TUNNEL, "envelope_intrusion_tunnel_m2"), (KIND_RAIL, "envelope_intrusion_rail_m2"),
                                 (KIND_RAILCOL, "envelope_intrusion_rail_m2")] {
                if intr_kind[kind as usize] { m.add(name, area); }
            }
        }
        if parapet { m.add("parapet_in_cw_m", CELL); }
        if over_min.is_finite() && over_min <= hi { m.add("envelope_overhead_m2", area); }
        if s.level == 0 && (over_min.is_finite() || top_above.is_finite()) && over_min < drive + 15.0 {
            m.add("under_structure_cells", 1.0);
            let gap = over_min - drive;
            if gap < 2.5 { m.add("opening_blocked_m2", area); }
            if gap < 4.25 { m.add("clearance_short_cells", 1.0); }
            if gap < 4.25 && std::env::var("ROADSYS_AUDIT_DUMP").is_ok() { eprintln!("CS {:.1} {:.1} gap {:.2} drive {:.2} top {:.2}", x, y, gap, drive, top_above); }
            if top_above.is_finite() {
                let rr = top_above - drive;
                r2r.push(rr);
                if rr > 7.6 { m.add("clearance_excess_cells", 1.0); }
            }
            // piers: tall steep structure faces within 1.5 m of the lane cell, standing at this level
            let e = h + PIER_NEAR;
            col_steep.around(x - e, y - e, x + e, y + e, &mut cand);
            let mut pier = false;
            for &k in &cand {
                let t = &tris[k as usize];
                if !matches!(t.kind, KIND_DECK | KIND_RAILCOL | KIND_RAIL | KIND_OTHER) { continue; }
                let Some((zl, zh)) = clip_z(t, x - e, y - e, x + e, y + e) else { continue };
                if zl <= drive + 1.0 && zh >= drive + 2.0 && zh > drive - 0.3 { pier = true; break; }
            }
            if pier { m.add("pier_in_face_m2", area); }
        }
    }
    if !r2r.is_empty() {
        m.max("road_to_road_max_p50_m", pct(&mut r2r, 0.5));
        m.max("road_to_road_max_m", *r2r.last().unwrap());
    }
    // free edges of the colliding tops (hand-overs) and of the road surfaces (cracks), in lanes
    let mask = LaneMask::new(&sc.lines);
    let dump = std::env::var("ROADSYS_AUDIT_DUMP").is_ok();
    let mut lips: Vec<f64> = Vec::new();
    let mut lips_terrain: Vec<f64> = Vec::new();
    let mut by_entry: BTreeMap<(u32, bool), Vec<u32>> = BTreeMap::new();
    for (k, t) in tris.iter().enumerate() {
        if t.cls != UP { continue; }
        let road = t.kind == KIND_ROAD && t.flags & FLAG_MARK == 0;
        if t.collides || road { by_entry.entry((t.entry, t.collides)).or_default().push(k as u32); }
    }
    // vertices of every piece (T-crack candidates), 4 m buckets
    let mut vgrid: BTreeMap<(i64, i64), Vec<([f64; 3], u32)>> = BTreeMap::new();
    for t in tris.iter() {
        if t.cls != UP { continue; }
        if !(t.collides || (t.kind == KIND_ROAD && t.flags & FLAG_MARK == 0)) { continue; }
        for v in &t.p { vgrid.entry(((v[0] / 4.0).floor() as i64, (v[1] / 4.0).floor() as i64)).or_default().push((*v, t.entry)); }
    }
    for v in vgrid.values_mut() {
        v.sort_by(|a, b| qkey(a.0).cmp(&qkey(b.0)).then(a.1.cmp(&b.1)));
        v.dedup_by(|a, b| qkey(a.0) == qkey(b.0) && a.1 == b.1);
    }
    for ((entry, collides), ids) in &by_entry {
        let mut em: BTreeMap<((i64, i64, i64), (i64, i64, i64)), (u32, [f64; 3], [f64; 3], u32)> = BTreeMap::new();
        for &k in ids {
            let t = &tris[k as usize];
            for e in 0..3 {
                let (a, b) = (t.p[e], t.p[(e + 1) % 3]);
                let (ka, kb) = (qkey(a), qkey(b));
                if ka == kb { continue; }
                let key = if ka < kb { (ka, kb) } else { (kb, ka) };
                let ent = em.entry(key).or_insert((0, a, b, k));
                ent.0 += 1;
            }
        }
        for (_key, &(cnt, a, b, owner)) in &em {
            if cnt != 1 { continue; }
            let dx = b[0] - a[0];
            let dy = b[1] - a[1];
            let len = (dx * dx + dy * dy).sqrt();
            if len < 0.01 { continue; }
            let mid = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0];
            if !in_quad(&sc.quad, mid) { continue; }
            // T-cracks: a vertex of another piece strictly inside the free edge (2 mm, 5 cm in z)
            if mask.inside(&sc.lines, mid[0], mid[1], 0.3) {
                let mut crack = false;
                let (gx0, gx1) = ((a[0].min(b[0]) / 4.0).floor() as i64, (a[0].max(b[0]) / 4.0).floor() as i64);
                let (gy0, gy1) = ((a[1].min(b[1]) / 4.0).floor() as i64, (a[1].max(b[1]) / 4.0).floor() as i64);
                'cr: for gx in gx0..=gx1 {
                    for gy in gy0..=gy1 {
                        let Some(vs) = vgrid.get(&(gx, gy)) else { continue };
                        for &(q, _e) in vs {
                            let u = ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / (len * len);
                            if u <= 0.01 || u >= 0.99 { continue; }
                            let d = cross2([dx, dy], [q[0] - a[0], q[1] - a[1]]).abs() / len;
                            if d > 0.002 { continue; }
                            let ze = a[2] + (b[2] - a[2]) * u;
                            if (q[2] - ze).abs() > 0.05 { continue; }
                            crack = true;
                            break 'cr;
                        }
                    }
                }
                if crack { m.add(if *collides { "tcracks_collider" } else { "tcracks_road" }, 1.0); }
            }
            // (kerbs and walks step on purpose: the kerb metrics count them; tops of the drivable products only)
            if !*collides || matches!(tris[owner as usize].kind, KIND_WALK | KIND_WALKCOL) { continue; }
            // hand-over samples every 0.25 m along the free edge, where this top is the drivable surface just inside
            // it (5 cm in: no other surface up to 15 cm above it there; a pad under the road is no hand-over)
            let tri = &tris[owner as usize];
            let c = tri.p.iter().copied().find(|v| qkey(*v) != qkey(a) && qkey(*v) != qkey(b)).unwrap_or(a);
            let mut inn = [-dy / len, dx / len];
            if inn[0] * (c[0] - a[0]) + inn[1] * (c[1] - a[1]) < 0.0 { inn = [-inn[0], -inn[1]]; }
            let n = ((len / 0.25).ceil() as usize).max(1);
            for i in 0..n {
                let u = (i as f64 + 0.5) / n as f64;
                let p = [a[0] + dx * u, a[1] + dy * u];
                if !mask.inside(&sc.lines, p[0], p[1], 0.3) { continue; }
                let ze = a[2] + (b[2] - a[2]) * u;
                let pin = [p[0] + 0.05 * inn[0], p[1] + 0.05 * inn[1]];
                let zin = ray(tri, pin[0], pin[1]).unwrap_or(ze);
                let zt_in = ter.at(pin[0], pin[1]);
                let mut hidden = zt_in.is_finite() && zt_in > zin + 0.001 && zt_in <= zin + 0.15;
                if !hidden {
                    for &k in col_flat.at(pin[0], pin[1]) {
                        let t = &tris[k as usize];
                        if t.cls != UP || t.entry == *entry { continue; }
                        if let Some(z) = ray(t, pin[0], pin[1]) { if z > zin + 0.001 && z <= zin + 0.15 { hidden = true; break; } }
                    }
                }
                if hidden { continue; }
                let mut best = f64::INFINITY;
                let mut terrain = false;
                let zt = ter.at(p[0], p[1]);
                if zt.is_finite() && (zt - ze).abs() <= 0.15 { best = (zt - ze).abs(); terrain = true; }
                for &k in col_flat.at(p[0], p[1]) {
                    let t = &tris[k as usize];
                    if t.cls != UP || t.entry == *entry { continue; }
                    if let Some(z) = ray(t, p[0], p[1]) {
                        let d = (z - ze).abs();
                        if d <= 0.15 && d < best { best = d; terrain = false; }
                    }
                }
                if best.is_finite() {
                    if dump && best > 0.005 { eprintln!("lip {:.3} {:.3} {:.3} {:.1} terrain={} entry={} kind={}", p[0], p[1], ze, best * 1000.0, terrain, entry, tri.kind); }
                    lips.push(best * 1000.0);
                    if terrain { lips_terrain.push(best * 1000.0); }
                }
            }
        }
    }
    m.add("handover_samples", lips.len() as f64);
    m.add("handover_terrain_samples", lips_terrain.len() as f64);
    for (lim, name) in [(1.0, "handover_lip_over_1mm"), (5.0, "handover_lip_over_5mm"), (20.0, "handover_lip_over_20mm")] {
        m.add(name, lips.iter().filter(|&&v| v > lim).count() as f64);
    }
    if !lips.is_empty() {
        m.max("handover_lip_max_mm", lips.iter().cloned().fold(0.0, f64::max));
        m.max("handover_lip_max_p95_mm", pct(&mut lips, 0.95));
    }
    if !lips_terrain.is_empty() { m.max("handover_terrain_lip_max_p95_mm", pct(&mut lips_terrain, 0.95)); }
    // folds, zero-area and long triangles on the drivable / drawn road surfaces
    for t in tris.iter() {
        let road = (t.kind == KIND_ROAD && t.flags & FLAG_MARK == 0) || (t.collides && t.kind == KIND_DECK && t.flags & FLAG_DECK_TOP != 0);
        if !road { continue; }
        let c = [(t.p[0][0] + t.p[1][0] + t.p[2][0]) / 3.0, (t.p[0][1] + t.p[1][1] + t.p[2][1]) / 3.0];
        if !in_quad(&sc.quad, c) { continue; }
        let mut emax: f64 = 0.0;
        for e in 0..3 {
            let (a, b) = (t.p[e], t.p[(e + 1) % 3]);
            let (ex, ey) = (b[0] - a[0], b[1] - a[1]);
            emax = emax.max((ex * ex + ey * ey).sqrt());
        }
        m.max("edge_max_m", emax);
        if emax > 50.0 { m.add("long_edges_over_50m", 1.0); }
        if !mask.inside(&sc.lines, c[0], c[1], 0.3) { continue; }
        if t.cls == FLAT0 { m.add("zero_area_in_lane", 1.0); }
        if t.cls == DOWN { m.add("folds_in_lane", 1.0); }
    }
    m
}

/// The audit of an input blob: a JSON object, or an error.
pub fn run(bytes: &[u8]) -> Result<String, String> {
    let sc = parse(bytes)?;
    Ok(audit(&sc).json())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flat_terrain(n: usize, z: f32) -> Terrain { Terrain { n, ox: -(n as i64) / 2, oy: -(n as i64) / 2, z: vec![z; n * n], hole: vec![0; n * n] } }

    fn quad(h: f64) -> [[f64; 2]; 4] { [[-h, -h], [h, -h], [h, h], [-h, h]] }

    fn rect(entry: u32, kind: u8, collides: bool, flags: u8, x0: f64, y0: f64, x1: f64, y1: f64, z: f64) -> Vec<Tri> {
        let mk = |p: [[f64; 3]; 3]| Tri { p, entry, kind, collides, flags, cls: classify(&p) };
        vec![mk([[x0, y0, z], [x1, y0, z], [x1, y1, z]]), mk([[x0, y0, z], [x1, y1, z], [x0, y1, z]])]
    }

    fn road_line(level: u8) -> Line { Line { level, width: 8.0, xy: vec![[-40.0, 0.0], [40.0, 0.0]] } }

    #[test]
    fn terrain_matches_the_lattice() {
        let mut t = flat_terrain(4, 0.0);
        t.z[1 * 4 + 1] = 1.0;
        // (1, 1) is a lattice node: exact there; midway to (2, 2) along the diagonal of an even cell
        assert_eq!(t.at(-1.0, -1.0), 1.0);
        assert!((t.at(-0.5, -0.5) - 0.5).abs() < 1e-12);
    }

    #[test]
    fn a_pad_under_the_road_is_a_double_owner_and_a_clean_road_is_not() {
        let base = Scene { quad: quad(30.0), terrain: flat_terrain(80, 10.0), tris: vec![], lines: vec![road_line(0)] };
        let m = audit(&base);
        assert!(m.m.get("lane_cells").copied().unwrap_or(0.0) > 100.0);
        assert_eq!(m.m.get("double_owner_m2"), None);
        let mut sc = base;
        sc.tris = rect(0, KIND_DECK, true, 0, -10.0, -4.0, 10.0, 4.0, 9.97);
        let m = audit(&sc);
        let a = m.m["double_owner_terrain_m2"];
        assert!(a > 100.0 && a < 200.0, "{}", a);
    }

    #[test]
    fn a_barrier_across_the_lane_is_a_parapet_in_the_carriageway() {
        let mut sc = Scene { quad: quad(30.0), terrain: flat_terrain(80, 0.0), tris: vec![], lines: vec![road_line(0)] };
        let mk = |p: [[f64; 3]; 3]| Tri { p, entry: 0, kind: KIND_BARRIER, collides: true, flags: 0, cls: classify(&p) };
        sc.tris.push(mk([[0.1, -5.0, -0.1], [0.1, 5.0, -0.1], [0.1, 5.0, 1.0]]));
        sc.tris.push(mk([[0.1, -5.0, -0.1], [0.1, 5.0, 1.0], [0.1, -5.0, 1.0]]));
        let m = audit(&sc);
        let l = m.m["parapet_in_cw_m"];
        assert!(l >= 3.0 && l <= 8.0, "{}", l);
        assert!(m.m["envelope_intrusion_barrier_m2"] > 0.0);
    }

    #[test]
    fn a_deck_end_lip_is_measured_where_the_lane_crosses_it() {
        let mut sc = Scene { quad: quad(30.0), terrain: flat_terrain(80, 5.0), tris: vec![], lines: vec![road_line(1)] };
        sc.tris = rect(0, KIND_DECK, true, FLAG_DECK_TOP, 0.0, -4.5, 30.0, 4.5, 5.012);
        let m = audit(&sc);
        assert!((m.m["handover_lip_max_mm"] - 12.0).abs() < 0.01, "{:?}", m.m);
        assert!(m.m["handover_lip_over_5mm"] > 0.0);
    }

    #[test]
    fn a_low_deck_over_a_road_blocks_the_opening() {
        let mut sc = Scene { quad: quad(30.0), terrain: flat_terrain(80, 0.0), tris: vec![], lines: vec![road_line(0)] };
        sc.tris = rect(0, KIND_DECK, true, FLAG_DECK_TOP, -5.0, -6.0, 5.0, 6.0, 3.0);
        let mut low = rect(0, KIND_DECK, true, 0, -5.0, -6.0, 5.0, 6.0, 2.0);
        for t in low.iter_mut() { t.p.swap(1, 2); t.cls = classify(&t.p); }
        sc.tris.extend(low);
        let m = audit(&sc);
        assert!(m.m["opening_blocked_m2"] > 10.0);
        assert!(m.m["clearance_short_cells"] > 10.0);
        assert!(m.m["envelope_overhead_m2"] > 10.0);
    }
}
