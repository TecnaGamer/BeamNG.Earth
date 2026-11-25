//! T0 tunnel checks of the M0 audit (tunnelplan section 5: the tunnel complaint categories as audit metrics, on any
//! built scene: today's output and the road system's alike). Called by audit::audit with what its lane pass found.
//!
//! - `hole_squares` / `hole_open_squares`: terrain lattice squares the engine skips (the hole flag of the square's
//!   south-west sample) inside the tile, and those nothing covers from above: no mesh face at the square's centre
//!   higher than its lowest corner less 1 m (sky or the void under the ground seen through the hole; tunnelscan's
//!   "gap").
//! - `hole_edge_open` (the plan's seam rule, 4.4): edges between a hole square and a kept one that nothing closes:
//!   0.25 m into the hole no surface within 0.3 m of the kept side's lowest edge node (no floor, kit roof or ground
//!   patch going on from the ground) and no steep face along the edge reaching from that surface (or 2 m down) up to
//!   the kept side's height (no headwall or wing). One count per 1 m edge.
//! - `tunnel_mouth_steps_over_10cm`, `tunnel_mouth_step_max_m`, `tunnel_mouth_holes`: at every end of a tunnel lane
//!   (a mapped tunnel range's end inside the tile) its drivable height in the last cell against the drivable height of
//!   the ground or bridge lane cell 1 m out along it (tunnelscan's "mouth_step"); a hole there (nothing to drive on).
//! - Portals (ROADSYS_AUDIT_PORTALS=path, a side file: the metrics stay numbers): `TAGEND x y line` at each mapped
//!   tunnel range's end in the tile, `PORTAL x y line` where a tunnel lane's centre row goes from no vault to a vault
//!   over it or back (the built portal). tools/tunnelcorpus measures their offset against the real portals.
//!
//! The lane pass itself adds `opening_blocked_tunnel_m2` (the physics ground inside a tunnel lane's clearance: terrain
//! across the mouth), `tube_breaks_upper_m2` (a tunnel part inside the envelope of a ground or bridge lane: a tube
//! through the road over it) and `handover_lip_tunnel_*` (the hand-over lips of tunnel floors).
use crate::audit::{Metrics, Scene, Tri, DOWN, STEEP, UP};
use std::collections::BTreeMap;

const COVER_BELOW: f64 = 1.0;   // m: a surface this far under a hole square's lowest corner still covers it
const EDGE_IN: f64 = 0.25;      // m into the hole square the edge's continuation is looked for
const EDGE_TOL: f64 = 0.3;      // m: a surface within this of the kept side's edge height goes on from the ground
const WALL_HALF: f64 = 0.15;    // m: half the strip along an edge a closing face must cross
const WALL_DEEP: f64 = 2.0;     // m: with nothing under the hole, a face reaching this far down closes the edge
const MOUTH_OUT: f64 = 1.0;     // m out of a tunnel lane's end the ground cell is read
const MOUTH_STEP: f64 = 0.10;   // m

/// Every face's z at (x, y) among `ids` (vertical ray through its XY projection).
fn zs_at(tris: &[Tri], ids: &[u32], x: f64, y: f64, ray: &dyn Fn(&Tri, f64, f64) -> Option<f64>) -> Vec<f64> {
    ids.iter().filter_map(|&k| ray(&tris[k as usize], x, y)).collect()
}

/// Lane cell heights from the audit's lane pass: (line, station, across) -> (drive z or NaN, x, y, level).
pub type Heights = BTreeMap<(u32, i64, i64), (f64, f64, f64, u8)>;

pub struct Faces<'a> {
    pub flat_at: &'a dyn Fn(f64, f64) -> Vec<u32>,
    pub steep_around: &'a dyn Fn(f64, f64, f64, f64) -> Vec<u32>,
    pub ray: &'a dyn Fn(&Tri, f64, f64) -> Option<f64>,
    pub clip_z: &'a dyn Fn(&Tri, f64, f64, f64, f64) -> Option<(f64, f64)>,
    pub in_quad: &'a dyn Fn(f64, f64) -> bool,
}

/// The hole checks (hole_squares, hole_open_squares, hole_edge_open).
pub fn holes(sc: &Scene, f: &Faces, m: &mut Metrics) {
    let t = &sc.terrain;
    let n = t.n;
    if n < 3 { return; }
    let tris = &sc.tris;
    let node = |x: usize, y: usize| t.z[y * n + x] as f64;
    let hole = |x: usize, y: usize| t.hole[y * n + x] != 0;
    let dump = std::env::var("ROADSYS_AUDIT_DUMP").is_ok();
    for iy in 0..n - 1 {
        for ix in 0..n - 1 {
            if !hole(ix, iy) { continue; }
            let (x0, y0) = ((t.ox + ix as i64) as f64, (t.oy + iy as i64) as f64);
            let (cx, cy) = (x0 + 0.5, y0 + 0.5);
            if !(f.in_quad)(cx, cy) { continue; }
            m.add("hole_squares", 1.0);
            let cmin = node(ix, iy).min(node(ix + 1, iy)).min(node(ix, iy + 1)).min(node(ix + 1, iy + 1));
            let top = zs_at(tris, &(f.flat_at)(cx, cy), cx, cy, f.ray).into_iter().fold(f64::NEG_INFINITY, f64::max);
            if !(top >= cmin - COVER_BELOW) {
                m.add("hole_open_squares", 1.0);
                if dump { eprintln!("HO {:.1} {:.1} cmin {:.2} top {:.2}", cx, cy, cmin, top); }
            }
            // the four edges: (neighbour square, the edge's two lattice nodes, the inward normal into this square)
            let edges: [(i64, i64, (usize, usize), (usize, usize), [f64; 2]); 4] = [
                (0, -1, (ix, iy), (ix + 1, iy), [0.0, 1.0]),
                (0, 1, (ix, iy + 1), (ix + 1, iy + 1), [0.0, -1.0]),
                (-1, 0, (ix, iy), (ix, iy + 1), [1.0, 0.0]),
                (1, 0, (ix + 1, iy), (ix + 1, iy + 1), [-1.0, 0.0]),
            ];
            for (dx, dy, a, b, inn) in edges {
                let (nx, ny) = (ix as i64 + dx, iy as i64 + dy);
                if nx < 0 || ny < 0 || nx >= (n - 1) as i64 || ny >= (n - 1) as i64 { continue; }
                if hole(nx as usize, ny as usize) { continue; }
                let ze = node(a.0, a.1).min(node(b.0, b.1));
                let (ax, ay) = ((t.ox + a.0 as i64) as f64, (t.oy + a.1 as i64) as f64);
                let (bx, by) = ((t.ox + b.0 as i64) as f64, (t.oy + b.1 as i64) as f64);
                let (mx, my) = ((ax + bx) / 2.0, (ay + by) / 2.0);
                let (px, py) = (mx + inn[0] * EDGE_IN, my + inn[1] * EDGE_IN);
                let zh = zs_at(tris, &(f.flat_at)(px, py), px, py, f.ray).into_iter()
                    .filter(|&z| z <= ze + EDGE_TOL).fold(f64::NEG_INFINITY, f64::max);
                if zh >= ze - EDGE_TOL { continue; }
                // a face along the edge (headwall, wing, trough wall) from the surface under the hole up to the ground
                let foot = if zh.is_finite() { zh + EDGE_TOL } else { ze - WALL_DEEP };
                // (the strip across the edge's middle, its ends kept 0.2 m off the corners: a wall along the next
                // edge does not close this one)
                let along_x = ay == by;
                let (sx0, sx1) = if along_x { (ax.min(bx) + 0.2, ax.max(bx) - 0.2) } else { (ax - WALL_HALF, ax + WALL_HALF) };
                let (sy0, sy1) = if along_x { (ay - WALL_HALF, ay + WALL_HALF) } else { (ay.min(by) + 0.2, ay.max(by) - 0.2) };
                // (the faces' z spans in the strip merged: a wall is two or more triangles, none spanning it alone)
                let mut spans: Vec<(f64, f64)> = (f.steep_around)(sx0, sy0, sx1, sy1).iter()
                    .filter_map(|&k| (f.clip_z)(&tris[k as usize], sx0, sy0, sx1, sy1)).collect();
                spans.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));
                let mut reach = f64::NEG_INFINITY;
                for &(zl, zu) in &spans {
                    if zl > foot && zl > reach + 0.05 { break; }
                    if zl <= foot || zl <= reach + 0.05 { reach = reach.max(zu); }
                }
                let closed = reach >= ze - 0.05;
                if closed { continue; }
                m.add("hole_edge_open", 1.0);
                if dump { eprintln!("HE {:.1} {:.1} ze {:.2} zh {:.2}", mx, my, ze, zh); }
            }
        }
    }
}

/// The mouth steps and the portal side file. `roofed[(line, station, across)]`: a vault 1 m to ROOF_MAX over the
/// lane cell (tunnel lanes only).
pub fn mouths(sc: &Scene, heights: &Heights, roofed: &BTreeMap<(u32, i64, i64), bool>, f: &Faces, m: &mut Metrics) {
    // ground / bridge cells by position (0.5 m buckets) for the cell 1 m out of a tunnel lane's end
    let mut by_pos: BTreeMap<(i64, i64), Vec<f64>> = BTreeMap::new();
    for &(z, x, y, level) in heights.values() {
        if level == 2 { continue; }
        by_pos.entry(((x / 0.5).floor() as i64, (y / 0.5).floor() as i64)).or_default().push(z);
    }
    let mut out = String::new();
    for (li, l) in sc.lines.iter().enumerate() {
        if l.level != 2 || l.xy.len() < 2 { continue; }
        let li = li as u32;
        let len: f64 = l.xy.windows(2).map(|w| ((w[1][0] - w[0][0]).powi(2) + (w[1][1] - w[0][1]).powi(2)).sqrt()).sum();
        let last_station = ((len - 0.25) / 0.5).floor() as i64;
        for (end, p, q, station) in [(0, l.xy[0], l.xy[1], 0i64), (1, l.xy[l.xy.len() - 1], l.xy[l.xy.len() - 2], last_station)] {
            if !(f.in_quad)(p[0], p[1]) { continue; }
            let _ = end;
            out.push_str(&format!("TAGEND {:.3} {:.3} {}\n", p[0], p[1], li));
            let d = [p[0] - q[0], p[1] - q[1]];
            let dl = (d[0] * d[0] + d[1] * d[1]).sqrt();
            if dl < 1e-6 { continue; }
            let o = [p[0] + d[0] / dl * MOUTH_OUT, p[1] + d[1] / dl * MOUTH_OUT];
            // the tunnel lane's last cell at its centre (within a cell of the end)
            let zin = [station, station + if station == 0 { 1 } else { -1 }].iter()
                .filter_map(|&s| heights.get(&(li, s, 0))).map(|v| v.0).find(|z| z.is_finite());
            let Some(zin) = zin else { continue };
            let key = ((o[0] / 0.5).floor() as i64, (o[1] / 0.5).floor() as i64);
            let mut zout: Option<f64> = None;
            let mut seen = false;
            for di in -1..=1 { for dj in -1..=1 {
                if let Some(v) = by_pos.get(&(key.0 + di, key.1 + dj)) {
                    for &z in v {
                        seen = true;
                        if z.is_finite() && zout.map_or(true, |w| (z - zin).abs() < (w - zin).abs()) { zout = Some(z); }
                    }
                }
            } }
            if !seen { continue; }     // (no ground lane mapped on from this end inside the tile)
            match zout {
                None => m.add("tunnel_mouth_holes", 1.0),
                Some(z) => {
                    let step = (z - zin).abs();
                    m.add("tunnel_mouths", 1.0);
                    if step > MOUTH_STEP { m.add("tunnel_mouth_steps_over_10cm", 1.0); }
                    m.max("tunnel_mouth_step_max_m", step);
                }
            }
        }
        // the built portals along the centre row
        let mut row: Vec<(i64, bool, f64, f64)> = roofed.iter().filter(|(k, _)| k.0 == li && k.2 == 0)
            .filter_map(|(k, &r)| heights.get(k).map(|h| (k.1, r, h.1, h.2))).collect();
        row.sort_by(|a, b| a.0.cmp(&b.0));
        // (a row roofed from its first or to its last cell at the range's mapped end in the tile: the portal is that end)
        if let (Some(r0), Some(r1)) = (row.first(), row.last()) {
            let (p0, p1) = (l.xy[0], l.xy[l.xy.len() - 1]);
            if r0.1 && r0.0 <= 1 && (f.in_quad)(p0[0], p0[1]) {
                out.push_str(&format!("PORTAL {:.3} {:.3} {}\n", r0.2, r0.3, li));
            }
            if r1.1 && r1.0 >= last_station - 1 && (f.in_quad)(p1[0], p1[1]) {
                out.push_str(&format!("PORTAL {:.3} {:.3} {}\n", r1.2, r1.3, li));
            }
        }
        for w in row.windows(2) {
            if w[1].0 - w[0].0 <= 2 && w[0].1 != w[1].1 {
                out.push_str(&format!("PORTAL {:.3} {:.3} {}\n", (w[0].2 + w[1].2) / 2.0, (w[0].3 + w[1].3) / 2.0, li));
            }
        }
    }
    if let Ok(path) = std::env::var("ROADSYS_AUDIT_PORTALS") {
        if !out.is_empty() {
            use std::io::Write;
            if let Ok(mut fh) = std::fs::OpenOptions::new().create(true).append(true).open(path) { let _ = fh.write_all(out.as_bytes()); }
        }
    }
}

pub fn is_flat(t: &Tri) -> bool { t.cls == UP || t.cls == DOWN }
pub fn is_steep(t: &Tri) -> bool { t.cls == STEEP }

#[cfg(test)]
mod tests {
    use crate::audit::{audit, classify, Line, Scene, Terrain, Tri, KIND_TUNNEL};

    fn scene_with_hole(tris: Vec<Tri>) -> Scene {
        let n = 40usize;
        let mut t = Terrain { n, ox: -20, oy: -20, z: vec![10.0; n * n], hole: vec![0; n * n] };
        for y in 20..23 { for x in 20..23 { t.hole[y * n + x] = 1; } }       // squares (0..3, 0..3)
        Scene { quad: [[-15.0, -15.0], [15.0, -15.0], [15.0, 15.0], [-15.0, 15.0]], terrain: t, tris,
                lines: vec![Line { level: 0, width: 4.0, xy: vec![[-14.0, -10.0], [14.0, -10.0]] }] }
    }

    fn plate(x0: f64, y0: f64, x1: f64, y1: f64, z: f64) -> Vec<Tri> {
        let mk = |p: [[f64; 3]; 3]| Tri { p, entry: 0, kind: KIND_TUNNEL, collides: true, flags: 0, cls: classify(&p) };
        vec![mk([[x0, y0, z], [x1, y0, z], [x1, y1, z]]), mk([[x0, y0, z], [x1, y1, z], [x0, y1, z]])]
    }

    #[test]
    fn an_uncovered_hole_has_every_edge_open_and_a_patch_at_ground_level_closes_them() {
        let m = audit(&scene_with_hole(vec![]));
        assert_eq!(m.m.get("hole_squares").copied(), Some(9.0), "{:?}", m.m);
        assert_eq!(m.m.get("hole_open_squares").copied(), Some(9.0));
        assert_eq!(m.m.get("hole_edge_open").copied(), Some(12.0));
        // a patch in the hole at the ground's height (a lattice ground patch, a kit roof): nothing open
        let m = audit(&scene_with_hole(plate(-0.5, -0.5, 3.5, 3.5, 10.0)));
        assert_eq!(m.m.get("hole_open_squares"), None, "{:?}", m.m);
        assert_eq!(m.m.get("hole_edge_open"), None, "{:?}", m.m);
        // a floor 3 m down: the hole is seen into from above and every edge is a 3 m drop with no wall
        let m = audit(&scene_with_hole(plate(-0.5, -0.5, 3.5, 3.5, 7.0)));
        assert_eq!(m.m.get("hole_open_squares").copied(), Some(9.0), "{:?}", m.m);
        assert_eq!(m.m.get("hole_edge_open").copied(), Some(12.0));
    }

    #[test]
    fn a_wall_from_the_floor_to_the_ground_closes_its_edge() {
        // floor 3 m down, one wall along the hole's south edge (y = 0) from the floor up to the ground: 3 edges closed
        let mut tris = plate(-0.5, -0.5, 3.5, 3.5, 7.0);
        let mk = |p: [[f64; 3]; 3]| Tri { p, entry: 1, kind: KIND_TUNNEL, collides: true, flags: 0, cls: classify(&p) };
        tris.push(mk([[0.0, 0.0, 6.9], [3.0, 0.0, 6.9], [3.0, 0.0, 10.2]]));
        tris.push(mk([[0.0, 0.0, 6.9], [3.0, 0.0, 10.2], [0.0, 0.0, 10.2]]));
        let m = audit(&scene_with_hole(tris));
        assert_eq!(m.m.get("hole_edge_open").copied(), Some(9.0), "{:?}", m.m);
    }
}
