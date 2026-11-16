//! The facade of a post-Soviet block, cell by cell (plan 048 stage C): every wall is cut into bays and storeys, each cell
//! one rectangle of the kit's atlas (the art's `bne_cis_<kit>.cells.json`, 96 px per metre), picked by a hash per cell
//! among the variants of its role: the same patchwork of old and new windows and individually glazed balconies as on the
//! real blocks. The stair columns (entrances below, stair windows above) stand on the entrance side at the verdict's
//! section positions along the long axis, balconies on the other side in stacks, the blank end walls at the ends of a slab.
//! Balcony, entrance and roof-box places are returned as slots for the modules (stage D, modules.rs).
use crate::dm::Dm;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Role {
    Bay,
    BayBalcony,
    BayBalconyFar,
    Stair,
    Entrance,
    Ground,
    Tech,
    Plinth,
    Parapet,
    End,
    Roof,
    Other,
}

fn role_of(s: &str) -> Role {
    match s {
        "bay" => Role::Bay,
        "bay_balcony" => Role::BayBalcony,
        "bay_balcony_far" => Role::BayBalconyFar,
        "stair" => Role::Stair,
        "entrance" => Role::Entrance,
        "ground" => Role::Ground,
        "tech" => Role::Tech,
        "plinth" => Role::Plinth,
        "parapet" => Role::Parapet,
        "end" => Role::End,
        "roof" => Role::Roof,
        _ => Role::Other,
    }
}

pub struct Cell {
    /// A texel of the cell where the colour is plain and the normal flat (tools/build_cis_data.py): what a quad with no
    /// picture of its own (the skirt under the walls, a jog) samples. The cell's centre may lie on a panel joint.
    pub plain: Option<[f64; 2]>,
    pub role: Role,
    pub weight: f64,
    pub px: [f64; 4],
    pub size_m: [f64; 2],
    pub set: Option<usize>,
}

pub struct Atlas {
    pub size: [f64; 2],
    pub bay_m: f64,
    pub storey_m: f64,
    pub cells: Vec<Cell>,
    /// The kit's colour sets (a building keeps one), by their tag.
    pub sets: Vec<String>,
}

/// Tags that name a colour / material set of an atlas (the others describe a cell: old, pvc, far, bars ...).
const SET_TAGS: [&str; 8] = ["tile_beige", "tile_blue", "aggregate", "painted", "silicate", "red", "vent", "plaster"];

impl Atlas {
    pub fn parse(json: &str) -> Result<Atlas, String> {
        let v: Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
        let num = |x: &Value| x.as_f64().ok_or_else(|| "number expected".to_string());
        let atlas = v["atlas"].as_array().ok_or("no atlas size")?;
        let mut sets: Vec<String> = vec![];
        let mut cells = vec![];
        for c in v["cells"].as_array().ok_or("no cells")? {
            let px = c["px"].as_array().ok_or("no px")?;
            let sz = c["size_m"].as_array().ok_or("no size_m")?;
            let mut set = None;
            if let Some(tags) = c["tags"].as_array() {
                for t in tags.iter().filter_map(|t| t.as_str()) {
                    if SET_TAGS.contains(&t) {
                        let k = match sets.iter().position(|s| s == t) {
                            Some(k) => k,
                            None => {
                                sets.push(t.to_string());
                                sets.len() - 1
                            }
                        };
                        set = Some(k);
                    }
                }
            }
            let plain = c["plain"].as_array().and_then(|a| Some([a.first()?.as_f64()?, a.get(1)?.as_f64()?]));
            cells.push(Cell {
                plain,
                role: role_of(c["role"].as_str().unwrap_or("")),
                weight: c["weight"].as_f64().unwrap_or(1.0).max(1e-6),
                px: [num(&px[0])?, num(&px[1])?, num(&px[2])?, num(&px[3])?],
                size_m: [num(&sz[0])?, num(&sz[1])?],
                set,
            });
        }
        Ok(Atlas {
            size: [num(&atlas[0])?, num(&atlas[1])?],
            bay_m: v["bay_m"].as_f64().unwrap_or(3.0),
            storey_m: v["storey_m"].as_f64().unwrap_or(2.8),
            cells,
            sets,
        })
    }

    pub fn has(&self, role: Role) -> bool {
        self.cells.iter().any(|c| c.role == role)
    }

    /// A cell of the role, by the weights, from the building's set (cells without a set are in every one).
    pub fn pick(&self, role: Role, set: Option<usize>, u: f64) -> Option<&Cell> {
        let ok = |c: &&Cell| c.role == role && (c.set.is_none() || set.is_none() || c.set == set);
        let total: f64 = self.cells.iter().filter(ok).map(|c| c.weight).sum();
        if total <= 0.0 {
            return None;
        }
        let mut t = u * total;
        let mut last = None;
        for c in self.cells.iter().filter(ok) {
            last = Some(c);
            t -= c.weight;
            if t < 0.0 {
                return Some(c);
            }
        }
        last
    }

    /// The tallest cell of a role (its own height says how high that storey is).
    pub fn height_of(&self, role: Role) -> Option<f64> {
        self.cells.iter().filter(|c| c.role == role).map(|c| c.size_m[1]).fold(None, |a, h| Some(a.map_or(h, |x: f64| x.max(h))))
    }

    /// UV of a cell's rectangle, inset by 1.5 px; (u0, v_bottom, u1, v_top), v up (Collada).
    fn uv(&self, c: &Cell) -> [f64; 4] {
        let (w, h) = (self.size[0], self.size[1]);
        let i = 1.5;
        [(c.px[0] + i) / w, 1.0 - (c.px[1] + c.px[3] - i) / h, (c.px[0] + c.px[2] - i) / w, 1.0 - (c.px[1] + i) / h]
    }
}

type Cache = Mutex<HashMap<u64, Arc<Atlas>>>;

fn cache() -> &'static Cache {
    static C: OnceLock<Cache> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

fn fnv(s: &str) -> u64 {
    let mut h = 0xcbf29ce484222325u64;
    for b in s.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

/// The atlas of a JSON text, parsed once per distinct text.
pub fn atlas(json: &str) -> Result<Arc<Atlas>, String> {
    let k = fnv(json);
    if let Some(a) = cache().lock().map_err(|e| e.to_string())?.get(&k) {
        return Ok(a.clone());
    }
    let a = Arc::new(Atlas::parse(json)?);
    cache().lock().map_err(|e| e.to_string())?.insert(k, a.clone());
    Ok(a)
}

fn splitmix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9e3779b97f4a7c15);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
    z ^ (z >> 31)
}

/// A number in [0, 1) from the building's seed and three keys.
pub fn h01(seed: u64, a: i64, b: i64, c: i64) -> f64 {
    let z = splitmix(seed ^ splitmix(a as u64 ^ splitmix(b as u64 ^ splitmix(c as u64))));
    (z >> 11) as f64 / (1u64 << 53) as f64
}

/// What a module slot is.
pub const SLOT_BALCONY: f64 = 1.0;
pub const SLOT_ENTRANCE: f64 = 2.0;
pub const SLOT_ROOF_BOX: f64 = 3.0;
pub const SLOT_COLUMNS: usize = 9;

pub struct Params<'a> {
    pub kit: &'a Atlas,
    pub common: &'a Atlas,
    pub kit_name: &'a str,
    pub ring: &'a [[f64; 2]],
    pub z_skirt: f64,
    pub z_base: f64,
    pub z_eave: f64,
    pub floors: i32,
    pub tech: bool,
    pub entrances: i32,
    pub axis: [f64; 2],
    pub center: [f64; 2],
    pub length: f64,
    pub arm: f64,
    pub road_side: f64,
    pub seed: u64,
    pub lite: bool,
    /// The share of the balcony stacks drawn as 3D modules (0: none, the painted cells only) and whether entrances get a 3D
    /// canopy (the user, 2026-11-16, after seeing Vyshenka: "balconies and entrances, maybe remove them").
    pub balcony_3d: f64,
    pub entrance_3d: bool,
}

#[derive(Default)]
pub struct Out {
    /// 12 numbers per quad, 8 per quad, the material slot per quad (0 the kit's atlas, 1 the common atlas).
    pub p: Vec<f64>,
    pub uv: Vec<f64>,
    pub mid: Vec<i32>,
    /// SLOT_COLUMNS numbers each: kind, x, y, z, normal x, normal y, width, storey, top (1: the building's top storey).
    pub slots: Vec<f64>,
}

impl Out {
    #[allow(clippy::too_many_arguments)]
    fn quad(&mut self, a: [f64; 2], b: [f64; 2], z0: f64, z1: f64, uv: [f64; 4], mid: i32) {
        self.p.extend_from_slice(&[a[0], a[1], z0, b[0], b[1], z0, b[0], b[1], z1, a[0], a[1], z1]);
        self.uv.extend_from_slice(&[uv[0], uv[1], uv[2], uv[1], uv[2], uv[3], uv[0], uv[3]]);
        self.mid.push(mid);
    }

    fn slot(&mut self, kind: f64, p: [f64; 3], n: [f64; 2], width: f64, storey: i32, top: bool) {
        self.slots.extend_from_slice(&[kind, p[0], p[1], p[2], n[0], n[1], width, storey as f64, if top { 1.0 } else { 0.0 }]);
    }
}

/// Balconies per kind: the share of the bay columns of the balcony side that have one.
/// The share of the balcony stacks that are real 3D modules (the others use the atlas's painted balcony cells): a block's
/// modules cost ~100 triangles each, 14 000 on a nine-storey slab with all of them, 13 times its walls (Vyshenka, 17 blocks:
/// 160 000 triangles of modules against 12 000 of walls), so only this share of the stacks has them.
pub const MODULE_SHARE: f64 = 0.4;     // (the 3D share when the caller asks for none of its own; the game passes cisbld::BALCONY_3D)

fn balcony_share(kit: &str) -> f64 {
    match kit {
        "panel9" | "late_soviet" => 0.9,
        "panel16" => 0.8,
        "new_mono" => 0.7,
        _ => 0.5,
    }
}

/// The sets' shares: the first colour set is taken by this share of the buildings, the others share the rest.
fn pick_set(a: &Atlas, seed: u64) -> Option<usize> {
    if a.sets.is_empty() {
        return None;
    }
    let u = h01(seed, 7, 0, 0);
    Some(((u * a.sets.len() as f64) as usize).min(a.sets.len() - 1))
}

pub fn build(p: &Params) -> Out {
    let mut out = Out::default();
    let n = p.ring.len();
    if n < 3 || p.floors < 1 || p.z_eave - p.z_base < 2.0 {
        return out;
    }
    let (ax, ay) = (p.axis[0], p.axis[1]);
    let left = [-ay, ax];
    let new_kit = p.kit_name.starts_with("new_");
    let set = pick_set(p.kit, p.seed);
    let h_all = p.z_eave - p.z_base;
    let plinth = if new_kit { None } else { p.common.pick(Role::Plinth, None, 0.0) };
    let mut plinth_h = plinth.map_or(0.0, |c| c.size_m[1]);
    let parapet_cell = |u: f64| p.kit.pick(Role::Parapet, set, u).or_else(|| p.common.pick(Role::Parapet, None, u));
    let parapet_h = parapet_cell(0.0).map_or(0.0, |c| c.size_m[1]);
    let tech = p.tech && p.kit.has(Role::Tech);
    let tech_h = if tech { p.kit.height_of(Role::Tech).unwrap_or(1.8) } else { 0.0 };
    let g_h = p.kit.height_of(Role::Ground).unwrap_or(p.kit.storey_m);
    // the storeys: the ground one by its cells' height, the others share what is left
    let mut nf = p.floors.max(1);
    let nominal_of = |n: i32| g_h + (n - 1).max(0) as f64 * p.kit.storey_m;
    // what is left over the plinth, the parapet and the technical storey is shared by the storeys in proportion to their cells'
    // heights (the plinth stays what its cell is: a leftover of 2 m made it a 3 m dark band, Vyshenka 2026-11-16)
    let avail = (h_all - plinth_h - parapet_h - tech_h).max(1.0);
    let mut k = avail / nominal_of(nf).max(1.0);
    if !(0.75..=1.4).contains(&k) {
        // the storey count does not fit the height (a block of 15 m counted as one storey: the user's Vinnytsia look, 2026-11-16, a
        // blank wall over the first floor): the count the height says
        nf = (((avail - g_h) / p.kit.storey_m).round() as i32 + 1).clamp(1, 120);
        k = avail / nominal_of(nf).max(1.0);
    }
    let k = k.clamp(0.6, 2.2);
    let upper = (nf - 1).max(0) as f64;
    let g_h = g_h * k;
    let s_h = p.kit.storey_m * k;
    let _ = new_kit;
    let z_plinth = p.z_base;
    let z_g0 = z_plinth + plinth_h;
    let z_u0 = z_g0 + g_h;
    let z_tech0 = z_u0 + upper * s_h;
    let z_par0 = z_tech0 + tech_h;
    let z_top = p.z_eave;
    let plain_uv = |c: &Cell, a: &Atlas| {
        let (cu, cv) = match c.plain {
            Some(q) => ((q[0] + 0.5) / a.size[0], 1.0 - (q[1] + 0.5) / a.size[1]),
            None => {
                let u = a.uv(c);
                ((u[0] + u[2]) * 0.5, (u[1] + u[3]) * 0.5)
            }
        };
        [cu, cv, cu + 1e-4, cv + 1e-4]
    };
    let plain_cell = p.kit.pick(Role::End, set, 0.5).map(|c| (c, p.kit, 0)).or_else(|| p.common.pick(Role::Plinth, None, 0.5).map(|c| (c, p.common, 1)));
    let slab = p.length / p.arm.max(1.0) >= 1.8 && !new_kit;
    // the ring in the long axis's frame: a short wall at either end of it is a blind end (the end of an L's other arm too)
    let (mut u0, mut u1, mut v0, mut v1) = (f64::MAX, f64::MIN, f64::MAX, f64::MIN);
    for q in p.ring.iter() {
        let (u, v) = (q[0] * ax + q[1] * ay, q[0] * left[0] + q[1] * left[1]);
        u0 = u0.min(u);
        u1 = u1.max(u);
        v0 = v0.min(v);
        v1 = v1.max(v);
    }
    // the entrance side of the long axis and the stair columns' positions along it
    let side = if p.road_side < 0.0 { -1.0 } else { 1.0 };
    let n_ent = p.entrances.max(1);
    let cols_u: Vec<f64> = (0..n_ent).map(|k| -p.length * 0.5 + (k as f64 + 0.5) * p.length / n_ent as f64).collect();
    // for a block without a long side (a tower) the entrance face is the one facing the road side
    let bal_share = balcony_share(p.kit_name);
    for i in 0..n {
        let (a, b) = (p.ring[i], p.ring[(i + 1) % n]);
        let (ex, ey) = (b[0] - a[0], b[1] - a[1]);
        let le = ex.dhypot(ey);
        if le < 0.05 {
            continue;
        }
        let d = [ex / le, ey / le];
        let nrm = [d[1], -d[0]];
        let along = (d[0] * ax + d[1] * ay).abs();
        let normal_side = nrm[0] * left[0] + nrm[1] * left[1];
        let on_entrance_side = normal_side * side > 0.5;
        // the skirt under the walls: plain concrete down to the lowest ground
        if let Some((c, at, mi)) = plain_cell {
            if z_plinth > p.z_skirt {
                out.quad(a, b, p.z_skirt, z_plinth, plain_uv(c, at), mi);
            }
        }
        if le < 2.5 {
            // a stair-core return or a jog in the outline: plain wall, no cells
            if let Some((c, at, mi)) = plain_cell {
                out.quad(a, b, z_plinth, z_top, plain_uv(c, at), mi);
            }
            continue;
        }
        let (mu, mv) = (((a[0] + b[0]) * 0.5) * ax + ((a[1] + b[1]) * 0.5) * ay, ((a[0] + b[0]) * 0.5) * left[0] + ((a[1] + b[1]) * 0.5) * left[1]);
        let at_end = mu - u0 < 1.5 || u1 - mu < 1.5 || mv - v0 < 1.5 || v1 - mv < 1.5;
        let end_wall = slab && le >= p.arm * 0.6 && le <= p.arm * 1.5 + 2.0 && (along < 0.34 || at_end);
        // the plinth
        if let Some(c) = plinth {
            plinth_row(&mut out, p.common, c, a, b, d, le, z_plinth, z_g0);
        }
        let b0 = p.kit.bay_m.max(1.5);
        let nb = ((le / b0).round() as i32).max(1);
        let bw = le / nb as f64;
        // which bay is a stair column: the bay whose centre is the nearest to each column of the axis (parallel walls),
        // else columns by section length along the wall's own length
        let mut stair = vec![false; nb as usize];
        if along > 0.9 {
            let s0 = (a[0] - p.center[0]) * ax + (a[1] - p.center[1]) * ay;
            let dir = d[0] * ax + d[1] * ay;
            for &cu in &cols_u {
                let t = (cu - s0) * dir.signum(); // metres along this wall
                if t > 0.0 && t < le {
                    let j = ((t / bw) as i32).clamp(0, nb - 1) as usize;
                    stair[j] = true;
                }
            }
        } else if !end_wall && nb >= 4 {
            let sec = p.length / n_ent as f64;
            let k = ((le / sec.max(6.0)).round() as i32).max(1);
            for q in 0..k {
                let t = (q as f64 + 0.5) * le / k as f64;
                stair[((t / bw) as i32).clamp(0, nb - 1) as usize] = true;
            }
        }
        // balcony stacks: a column of the balcony side keeps its balcony on every storey above the ground
        let balcony_side = !on_entrance_side && !end_wall;
        for j in 0..nb {
            let (pa, pb) = ([a[0] + d[0] * bw * j as f64, a[1] + d[1] * bw * j as f64], [a[0] + d[0] * bw * (j + 1) as f64, a[1] + d[1] * bw * (j + 1) as f64]);
            let col_balcony = balcony_side && !stair[j as usize] && h01(p.seed, i as i64, j as i64, 91) < bal_share;
            let col_module = col_balcony && h01(p.seed, i as i64, j as i64, 92) < p.balcony_3d;
            // ground storey
            let role_g = if end_wall {
                Role::End
            } else if stair[j as usize] && on_entrance_side {
                Role::Entrance
            } else if stair[j as usize] {
                Role::Stair
            } else {
                Role::Ground
            };
            let role_g = if p.kit.has(role_g) { role_g } else if p.kit.has(Role::Bay) { Role::Bay } else { Role::End };
            if let Some(c) = p.kit.pick(role_g, set, h01(p.seed, i as i64, j as i64, 1)) {
                let uv = p.kit.uv(c);
                out.quad(pa, pb, z_g0, z_u0, uv, 0);
                if role_g == Role::Entrance && p.entrance_3d {
                    out.slot(SLOT_ENTRANCE, [(pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5, z_g0], nrm, bw, 0, false);
                }
            }
            for s in 1..nf {
                let z_lo = z_u0 + (s - 1) as f64 * s_h;
                let role = if end_wall {
                    Role::End
                } else if stair[j as usize] {
                    Role::Stair
                } else if col_balcony {
                    if p.lite || !col_module {
                        Role::BayBalconyFar
                    } else {
                        Role::BayBalcony
                    }
                } else {
                    Role::Bay
                };
                let role = if p.kit.has(role) { role } else { Role::Bay };
                if let Some(c) = p.kit.pick(role, set, h01(p.seed, i as i64, j as i64, 100 + s as i64)) {
                    let uv = p.kit.uv(c);
                    out.quad(pa, pb, z_lo, z_lo + s_h, uv, 0);
                    if role == Role::BayBalcony && !p.lite {
                        out.slot(SLOT_BALCONY, [(pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5, z_lo], nrm, bw, s, s == nf - 1);
                    }
                }
            }
            if tech {
                if let Some(c) = p.kit.pick(Role::Tech, set, h01(p.seed, i as i64, j as i64, 3)) {
                    out.quad(pa, pb, z_tech0, z_par0, p.kit.uv(c), 0);
                }
            }
            if parapet_h > 0.0 {
                if let Some(c) = parapet_cell(h01(p.seed, i as i64, j as i64, 4)) {
                    let from_kit = p.kit.cells.iter().any(|k| std::ptr::eq(k, c));
                    let a_ = if from_kit { p.kit } else { p.common };
                    out.quad(pa, pb, z_par0, z_top, a_.uv(c), if from_kit { 0 } else { 1 });
                }
            }
        }
    }
    // a machine room over each stair column of a block with lifts (9 storeys and up): on the roof at the column's place
    if nf >= 9 && slab {
        for &cu in &cols_u {
            let (x, y) = (p.center[0] + ax * cu, p.center[1] + ay * cu);
            out.slot(SLOT_ROOF_BOX, [x, y, p.z_eave], [left[0] * side, left[1] * side], 0.0, nf, true);
        }
    } else if nf >= 9 {
        out.slot(SLOT_ROOF_BOX, [p.center[0], p.center[1], p.z_eave], [left[0] * side, left[1] * side], 0.0, nf, true);
    }
    out
}

#[allow(clippy::too_many_arguments)]
fn plinth_row(out: &mut Out, common: &Atlas, c: &Cell, a: [f64; 2], b: [f64; 2], d: [f64; 2], le: f64, z0: f64, z1: f64) {
    let w = c.size_m[0].max(1.0);
    let nb = ((le / w).round() as i32).max(1);
    let bw = le / nb as f64;
    let uv = common.uv(c);
    for j in 0..nb {
        let pa = [a[0] + d[0] * bw * j as f64, a[1] + d[1] * bw * j as f64];
        let pb = [a[0] + d[0] * bw * (j + 1) as f64, a[1] + d[1] * bw * (j + 1) as f64];
        out.quad(pa, pb, z0, z1, uv, 1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn atlas_json(kit: &str, with_tech: bool) -> String {
        let mut cells = vec![];
        let mut x = 4.0;
        let mut add = |role: &str, n: usize, w: f64, h: f64, tags: &str| {
            for k in 0..n {
                cells.push(format!(
                    r#"{{"id":"{role}_{k}","role":"{role}","weight":{},"px":[{x},4,{},{}],"size_m":[{w},{h}],"tags":[{tags}]}}"#,
                    1.0 / n as f64, w * 96.0, h * 96.0
                ));
                x += w * 96.0 + 4.0;
            }
        };
        add("bay", 3, 3.2, 2.7, "\"tile_beige\"");
        add("bay_balcony", 2, 3.2, 2.7, "\"tile_beige\"");
        add("bay_balcony_far", 2, 3.2, 2.7, "\"tile_beige\"");
        add("stair", 1, 3.2, 2.7, "\"tile_beige\"");
        add("entrance", 1, 3.2, 2.7, "\"tile_beige\"");
        add("ground", 2, 3.2, 2.7, "\"tile_beige\"");
        add("end", 1, 3.2, 2.7, "\"tile_beige\"");
        if with_tech {
            add("tech", 1, 3.2, 1.8, "\"tile_beige\"");
        }
        add("plinth", 1, 3.2, 1.0, "\"common\"");
        add("parapet", 1, 3.2, 0.6, "\"common\"");
        format!(r#"{{"kit":"{kit}","version":1,"px_per_m":96,"atlas":[8192,2048],"bay_m":3.2,"storey_m":2.7,"cells":[{}]}}"#, cells.join(","))
    }

    fn slab(l: f64, w: f64) -> Vec<[f64; 2]> {
        vec![[-l / 2.0, -w / 2.0], [l / 2.0, -w / 2.0], [l / 2.0, w / 2.0], [-l / 2.0, w / 2.0]]
    }

    fn params<'a>(kit: &'a Atlas, ring: &'a [[f64; 2]], floors: i32, tech: bool, lite: bool, seed: u64) -> Params<'a> {
        Params {
            kit, common: kit, kit_name: "khrush_panel", ring, z_skirt: -3.0, z_base: 0.0,
            z_eave: floors as f64 * 2.8 + 1.5 + if tech { 1.8 } else { 0.0 }, floors, tech, entrances: 4, axis: [1.0, 0.0],
            center: [0.0, 0.0], length: 62.0, arm: 11.5, road_side: 1.0, seed, lite, balcony_3d: 1.0, entrance_3d: true,
        }
    }

    #[test]
    fn the_atlas_reads_roles_sets_and_uv() {
        let a = Atlas::parse(&atlas_json("khrush_panel", true)).unwrap();
        assert!(a.has(Role::Tech) && a.has(Role::Parapet) && !a.has(Role::Roof));
        assert_eq!(a.sets, vec!["tile_beige".to_string()]);
        let c = a.pick(Role::Bay, Some(0), 0.0).unwrap();
        let uv = a.uv(c);
        assert!(uv[0] < uv[2] && uv[1] < uv[3] && uv[0] > 0.0 && uv[3] < 1.0);
        assert!((a.height_of(Role::Tech).unwrap() - 1.8).abs() < 1e-9);
    }

    #[test]
    fn a_slab_gets_its_walls_cells_and_slots() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(62.0, 11.5);
        let o = build(&params(&a, &ring, 5, false, false, 12345));
        let q = o.mid.len();
        assert_eq!(o.p.len(), q * 12);
        assert_eq!(o.uv.len(), q * 8);
        assert!(q > 100, "{q}");
        // four entrances, and balconies only on the side away from the road, never on the ground storey
        let entr = o.slots.chunks(SLOT_COLUMNS).filter(|s| s[0] == SLOT_ENTRANCE).count();
        assert!(entr >= 3 && entr <= 4, "{entr}");
        let bal: Vec<&[f64]> = o.slots.chunks(SLOT_COLUMNS).filter(|s| s[0] == SLOT_BALCONY).collect();
        assert!(!bal.is_empty());
        assert!(bal.iter().all(|s| s[5] < -0.9), "balconies on the side away from the road (+y is the entrance side)");
        assert!(bal.iter().all(|s| s[7] >= 1.0));
        // every quad faces outward: its normal points away from the centre
        for k in 0..q {
            let v = &o.p[k * 12..k * 12 + 12];
            let (ux, uy, uz) = (v[3] - v[0], v[4] - v[1], v[5] - v[2]);
            let (wx, wy, wz) = (v[9] - v[0], v[10] - v[1], v[11] - v[2]);
            let (nx, ny) = (uy * wz - uz * wy, uz * wx - ux * wz);
            let (mx, my) = ((v[0] + v[3]) * 0.5, (v[1] + v[4]) * 0.5);
            assert!(nx * mx + ny * my > -1e-6, "quad {k} faces inward");
        }
    }

    #[test]
    fn it_is_deterministic_and_the_seed_changes_the_patchwork() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(62.0, 11.5);
        let (x, y, z) = (build(&params(&a, &ring, 5, false, false, 1)), build(&params(&a, &ring, 5, false, false, 1)), build(&params(&a, &ring, 5, false, false, 2)));
        assert_eq!(x.uv, y.uv);
        assert_ne!(x.uv, z.uv);
    }

    #[test]
    fn far_tiles_use_the_painted_balconies_and_no_slots_for_them() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(62.0, 11.5);
        let o = build(&params(&a, &ring, 5, false, true, 7));
        assert!(o.slots.chunks(SLOT_COLUMNS).all(|s| s[0] != SLOT_BALCONY));
    }

    #[test]
    fn nine_storeys_with_a_technical_storey_get_machine_rooms() {
        let a = Atlas::parse(&atlas_json("panel9", true)).unwrap();
        let ring = slab(72.0, 12.5);
        let mut p = params(&a, &ring, 9, true, false, 3);
        p.kit_name = "panel9";
        p.length = 72.0;
        let o = build(&p);
        let roof = o.slots.chunks(SLOT_COLUMNS).filter(|s| s[0] == SLOT_ROOF_BOX).count();
        assert_eq!(roof, 4);
        // the top of the walls is the eave
        let top = (0..o.mid.len()).map(|k| o.p[k * 12 + 8]).fold(f64::MIN, f64::max);
        assert!((top - p.z_eave).abs() < 1e-6, "{top} {}", p.z_eave);
    }

    #[test]
    fn with_the_3d_modules_off_there_are_no_balcony_or_entrance_slots() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(62.0, 11.5);
        let mut p = params(&a, &ring, 5, false, false, 12345);
        p.balcony_3d = 0.0;
        p.entrance_3d = false;
        let o = build(&p);
        assert!(o.mid.len() > 100);
        assert!(o.slots.chunks(SLOT_COLUMNS).all(|s| s[0] != SLOT_BALCONY && s[0] != SLOT_ENTRANCE));
    }

    #[test]
    fn the_leftover_height_goes_to_the_storeys_not_to_the_plinth() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(62.0, 11.5);
        let mut p = params(&a, &ring, 5, false, false, 5);
        p.z_eave = 20.0; // 5 storeys mapped 20 m tall
        let o = build(&p);
        // the plinth (the first row of quads above the skirt) is 1.0 m: the first wall row's top is at z = 1.0
        let mut zs: Vec<f64> = (0..o.mid.len()).filter(|k| o.mid[*k] == 1).map(|k| o.p[k * 12 + 8]).collect();
        zs.sort_by(|a, b| a.partial_cmp(b).unwrap());
        zs.dedup_by(|a, b| (*a - *b).abs() < 1e-9);
        assert!(zs.iter().any(|z| (z - 1.0).abs() < 1e-6), "{zs:?}");
        assert!(!zs.iter().any(|z| *z > 1.2 && *z < 6.0), "no tall plinth: {zs:?}");
    }

    #[test]
    fn the_far_arm_of_an_l_has_a_blind_end() {
        let a = Atlas::parse(&atlas_json("panel9", true)).unwrap();
        // an L of two 12.5 m wide arms: 60 m along x and 40 m along y; the end of the y arm (12.5 m, parallel to the x axis) is blind
        let ring = vec![[0.0, 0.0], [60.0, 0.0], [60.0, 12.5], [12.5, 12.5], [12.5, 40.0], [0.0, 40.0]];
        let mut p = params(&a, &ring, 9, true, false, 9);
        p.kit_name = "panel9";
        p.length = 60.0;
        p.arm = 12.5;
        p.center = [30.0, 6.0];
        let o = build(&p);
        // quads on the wall y = 40 (the y arm's end): all of the End cell's UVs, i.e. one distinct UV rectangle
        let mut uvs: Vec<[f64; 2]> = vec![];
        for k in 0..o.mid.len() {
            let v = &o.p[k * 12..k * 12 + 12];
            if (v[1] - 40.0).abs() < 1e-9 && (v[4] - 40.0).abs() < 1e-9 && o.mid[k] == 0 && v[2] > 1.2 && v[8] < 25.0 {
                let u = &o.uv[k * 8..k * 8 + 8];
                if !uvs.iter().any(|q| (q[0] - u[0]).abs() < 1e-9 && (q[1] - u[1]).abs() < 1e-9) {
                    uvs.push([u[0], u[1]]);
                }
            }
        }
        assert!(!uvs.is_empty() && uvs.len() <= 2, "the end wall's cells are the one blank cell: {}", uvs.len());
    }

    #[test]
    fn a_storey_count_that_does_not_fit_the_height_is_replaced_by_the_height() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(62.0, 11.5);
        let mut p = params(&a, &ring, 1, false, false, 3);
        p.z_eave = 16.0; // one storey counted, 16 m of wall
        let o = build(&p);
        // no wall cell taller than 4 m (a stretched parapet or ground cell was a blank wall): the rows are about 2.8 m
        let tallest = (0..o.mid.len()).filter(|k| o.p[k * 12 + 2] > -0.5).map(|k| o.p[k * 12 + 8] - o.p[k * 12 + 2]).fold(0.0, f64::max);
        assert!(tallest < 4.0, "{tallest}");
        // and the wall reaches the eave
        let top = (0..o.mid.len()).map(|k| o.p[k * 12 + 8]).fold(f64::MIN, f64::max);
        assert!((top - 16.0).abs() < 1e-6, "{top}");
    }

    #[test]
    fn a_degenerate_building_makes_nothing() {
        let a = Atlas::parse(&atlas_json("khrush_panel", false)).unwrap();
        let ring = slab(10.0, 10.0);
        let mut p = params(&a, &ring, 5, false, false, 1);
        p.z_eave = 1.0;
        assert!(build(&p).mid.is_empty());
        p.z_eave = 15.0;
        p.floors = 0;
        assert!(build(&p).mid.is_empty());
    }
}
