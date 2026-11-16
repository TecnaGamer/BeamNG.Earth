//! The relief of a post-Soviet block (plan 048 stage D): the art's small modules (balconies, loggia fronts, entrance
//! canopies, lift machine rooms, roof clutter) stamped onto the slots the facade cutter left (facade.rs). The module
//! table is baked from the art's DAEs by tools/build_cis_data.py (server/data/cis_modules.json): per module its kind,
//! the kinds of block it fits, a weight, and its triangles per material, in the module's own frame (origin on the wall
//! plane or the roof, the front toward -Y, Z up, metres).
use super::facade::{h01, SLOT_BALCONY, SLOT_COLUMNS, SLOT_ENTRANCE, SLOT_ROOF_BOX};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

pub struct Geo {
    pub mat: usize,
    /// 9 numbers per triangle (three corners), 6 uv numbers per triangle.
    pub p: Vec<f64>,
    pub uv: Vec<f64>,
}

pub struct Module {
    pub id: String,
    pub kind: String,
    pub fits: Vec<String>,
    pub weight: f64,
    pub width: f64,
    pub top_only: bool,
    pub geo: Vec<Geo>,
}

pub struct Table {
    pub mats: Vec<String>,
    pub modules: Vec<Module>,
}

impl Table {
    pub fn parse(json: &str) -> Result<Table, String> {
        let v: Value = serde_json::from_str(json).map_err(|e| e.to_string())?;
        let mut mats: Vec<String> = vec![];
        let mut modules = vec![];
        let nums = |a: &Value| -> Vec<f64> { a.as_array().map(|x| x.iter().filter_map(|n| n.as_f64()).collect()).unwrap_or_default() };
        for m in v["modules"].as_array().ok_or("no modules")? {
            let mut geo = vec![];
            for g in m["geo"].as_array().ok_or("no geo")? {
                let name = g["mat"].as_str().ok_or("no mat")?.to_string();
                let mat = match mats.iter().position(|s| *s == name) {
                    Some(k) => k,
                    None => {
                        mats.push(name);
                        mats.len() - 1
                    }
                };
                geo.push(Geo { mat, p: nums(&g["p"]), uv: nums(&g["uv"]) });
            }
            modules.push(Module {
                id: m["id"].as_str().unwrap_or("").to_string(),
                kind: m["kind"].as_str().unwrap_or("").to_string(),
                fits: m["fits"].as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default(),
                weight: m["weight"].as_f64().unwrap_or(1.0),
                width: m["width_m"].as_f64().unwrap_or(1.0),
                top_only: m["top_only"].as_bool().unwrap_or(false),
                geo,
            });
        }
        Ok(Table { mats, modules })
    }

    fn pick(&self, kinds: &[&str], kit: &str, top: bool, u: f64, max_w: f64) -> Option<&Module> {
        let ok = |m: &&Module| kinds.contains(&m.kind.as_str()) && m.fits.iter().any(|f| f == kit) && !m.top_only && m.width <= max_w && m.weight > 0.0;
        let total: f64 = self.modules.iter().filter(ok).map(|m| m.weight).sum();
        if total <= 0.0 {
            return None;
        }
        let mut t = u * total;
        let mut chosen = None;
        for m in self.modules.iter().filter(ok) {
            chosen = Some(m);
            t -= m.weight;
            if t < 0.0 {
                break;
            }
        }
        let m = chosen?;
        if top {
            // the top storey's glazed balcony has a tin roof of its own: the module `<id>_top` (the brick ones: `..._top_brick`)
            let (base, tail) = match m.id.strip_suffix("_brick") {
                Some(b) => (b.to_string(), "_brick"),
                None => (m.id.clone(), ""),
            };
            let want = format!("{base}_top{tail}");
            if let Some(t) = self.modules.iter().find(|x| x.id == want && x.top_only) {
                return Some(t);
            }
        }
        Some(m)
    }

    fn by_id(&self, id: &str) -> Option<&Module> {
        self.modules.iter().find(|m| m.id == id)
    }
}

type Cache = Mutex<HashMap<u64, Arc<Table>>>;

pub fn table(json: &str) -> Result<Arc<Table>, String> {
    static C: OnceLock<Cache> = OnceLock::new();
    let c = C.get_or_init(|| Mutex::new(HashMap::new()));
    let mut k = 0xcbf29ce484222325u64;
    for b in json.bytes() {
        k = (k ^ b as u64).wrapping_mul(0x100000001b3);
    }
    if let Some(t) = c.lock().map_err(|e| e.to_string())?.get(&k) {
        return Ok(t.clone());
    }
    let t = Arc::new(Table::parse(json)?);
    c.lock().map_err(|e| e.to_string())?.insert(k, t.clone());
    Ok(t)
}

#[derive(Default)]
pub struct Placed {
    /// 9 numbers per triangle, 6 uv numbers, the table's material index per triangle.
    pub p: Vec<f64>,
    pub uv: Vec<f64>,
    pub mat: Vec<i32>,
}

/// A module's triangles in the frame of a place: the module's x along the wall's right (as seen from outside), -y out
/// along the normal, z up; `pos` the origin.
fn stamp(out: &mut Placed, m: &Module, pos: [f64; 3], n: [f64; 2]) {
    let t = [-n[1], n[0]];
    for g in &m.geo {
        let tris = g.p.len() / 9;
        for k in 0..tris {
            for c in 0..3 {
                let (x, y, z) = (g.p[k * 9 + c * 3], g.p[k * 9 + c * 3 + 1], g.p[k * 9 + c * 3 + 2]);
                out.p.extend_from_slice(&[pos[0] + x * t[0] - y * n[0], pos[1] + x * t[1] - y * n[1], pos[2] + z]);
            }
            if g.uv.len() >= (k + 1) * 6 {
                out.uv.extend_from_slice(&g.uv[k * 6..k * 6 + 6]);
            } else {
                out.uv.extend_from_slice(&[0.0; 6]);
            }
            out.mat.push(g.mat as i32);
        }
    }
}

/// The modules for a building's slots. `slots` is facade.rs's (SLOT_COLUMNS numbers each).
pub fn place(t: &Table, kit: &str, slots: &[f64], seed: u64, floors: i32) -> Placed {
    let mut out = Placed::default();
    let mut roofs = 0usize;
    for s in slots.chunks(SLOT_COLUMNS) {
        if s.len() < SLOT_COLUMNS {
            break;
        }
        let (kind, pos, n, width, top) = (s[0], [s[1], s[2], s[3]], [s[4], s[5]], s[6], s[8] > 0.5);
        // a column's choice is the same on every storey: keyed on where it stands
        let (ix, iy) = ((pos[0] * 10.0).round() as i64, (pos[1] * 10.0).round() as i64);
        if kind == SLOT_BALCONY {
            if let Some(m) = t.pick(&["balcony", "loggia"], kit, top, h01(seed, ix, iy, 77), width + 0.15) {
                stamp(&mut out, m, pos, n);
            }
        } else if kind == SLOT_ENTRANCE {
            if let Some(m) = t.pick(&["entrance"], kit, false, h01(seed, ix, iy, 78), width + 0.6) {
                stamp(&mut out, m, pos, n);
            }
        } else if kind == SLOT_ROOF_BOX {
            let tang = [-n[1], n[0]];
            if let Some(m) = t.pick(&["machine_room"], kit, false, h01(seed, ix, iy, 79), 99.0) {
                stamp(&mut out, m, pos, n);
            }
            if roofs == 0 && floors >= 9 {
                if let Some(m) = t.by_id("roof_exit") {
                    stamp(&mut out, m, [pos[0] + tang[0] * 5.0, pos[1] + tang[1] * 5.0, pos[2]], n);
                }
            }
            // roof clutter: a vent, an old TV mast or a dish, off the box on either side
            let u = h01(seed, ix, iy, 80);
            let (id, side) = if u < 0.35 { ("roof_antenna_old", 1.0) } else if u < 0.6 { ("roof_dish", -1.0) } else if u < 0.85 { ("roof_vent", 1.0) } else { ("", 0.0) };
            if let Some(m) = t.by_id(id) {
                if m.fits.iter().any(|f| f == kit) {
                    stamp(&mut out, m, [pos[0] + n[0] * 3.0 * side, pos[1] + n[1] * 3.0 * side, pos[2]], n);
                }
            }
            roofs += 1;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const TABLE: &str = r#"{"modules":[
      {"id":"b1","kind":"balcony","fits":["khrush_panel"],"weight":1.0,"width_m":2.6,"top_only":false,
       "geo":[{"mat":"bne_cis_common","p":[-1,0,0, 1,0,0, 1,-1,0],"uv":[0,0,1,0,1,1]}]},
      {"id":"b1_top","kind":"balcony","fits":["khrush_panel"],"weight":0.0,"width_m":2.7,"top_only":true,
       "geo":[{"mat":"bne_cis_common","p":[-1,0,2, 1,0,2, 1,-1,2],"uv":[0,0,1,0,1,1]}]},
      {"id":"e1","kind":"entrance","fits":["khrush_panel"],"weight":1.0,"width_m":2.4,"top_only":false,
       "geo":[{"mat":"bne_cis_concrete","p":[0,0,0, 1,0,0, 1,0,1],"uv":[0,0,1,0,1,1]}]},
      {"id":"m1","kind":"machine_room","fits":["panel9"],"weight":1.0,"width_m":4.0,"top_only":false,
       "geo":[{"mat":"bne_cis_common","p":[0,0,0, 1,0,0, 1,0,1],"uv":[0,0,1,0,1,1]}]}
    ]}"#;

    #[test]
    fn the_table_reads_modules_and_materials() {
        let t = Table::parse(TABLE).unwrap();
        assert_eq!(t.modules.len(), 4);
        assert_eq!(t.mats, vec!["bne_cis_common".to_string(), "bne_cis_concrete".to_string()]);
    }

    #[test]
    fn a_balcony_stands_out_from_the_wall_on_the_normal() {
        let t = Table::parse(TABLE).unwrap();
        // a wall facing +x (outward normal (1, 0)), the module's -y goes out along it
        let slots = [SLOT_BALCONY, 10.0, 5.0, 3.0, 1.0, 0.0, 3.2, 1.0, 0.0];
        let o = place(&t, "khrush_panel", &slots, 1, 5);
        assert_eq!(o.mat.len(), 1);
        // local (1, -1, 0): right = (-n.y, n.x) = (0, 1), out = n => (10 + 1, 5 + 1)
        assert!((o.p[6] - 11.0).abs() < 1e-9 && (o.p[7] - 6.0).abs() < 1e-9 && (o.p[8] - 3.0).abs() < 1e-9, "{:?}", &o.p[6..9]);
    }

    #[test]
    fn the_top_storey_takes_the_top_variant_and_other_storeys_never_do() {
        let t = Table::parse(TABLE).unwrap();
        let mid = place(&t, "khrush_panel", &[SLOT_BALCONY, 0.0, 0.0, 3.0, 0.0, 1.0, 3.2, 2.0, 0.0], 1, 5);
        let top = place(&t, "khrush_panel", &[SLOT_BALCONY, 0.0, 0.0, 14.0, 0.0, 1.0, 3.2, 5.0, 1.0], 1, 5);
        assert!((mid.p[2] - 3.0).abs() < 1e-9);
        assert!((top.p[2] - 16.0).abs() < 1e-9, "the top variant sits 2 m higher in its own frame: {}", top.p[2]);
    }

    #[test]
    fn modules_for_other_kinds_and_narrow_bays_are_not_placed() {
        let t = Table::parse(TABLE).unwrap();
        assert!(place(&t, "panel16", &[SLOT_BALCONY, 0.0, 0.0, 3.0, 0.0, 1.0, 3.2, 2.0, 0.0], 1, 5).mat.is_empty());
        assert!(place(&t, "khrush_panel", &[SLOT_BALCONY, 0.0, 0.0, 3.0, 0.0, 1.0, 2.0, 2.0, 0.0], 1, 5).mat.is_empty());
    }

    #[test]
    fn a_roof_box_goes_on_the_roof() {
        let t = Table::parse(TABLE).unwrap();
        let o = place(&t, "panel9", &[SLOT_ROOF_BOX, 3.0, 4.0, 30.0, 0.0, 1.0, 0.0, 9.0, 1.0], 1, 9);
        assert_eq!(o.mat.len(), 1);
        assert!((o.p[2] - 30.0).abs() < 1e-9);
    }
}
