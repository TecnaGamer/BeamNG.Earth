//! Street furniture assemblies culled as one (server furniture.assemblies / under_decks, racedig.clear_props; the
//! user's reports 56, 59, 66, 70 at Daytona and Atlanta, 2026-11-17: a utility pole culled, its cobra-head arm 7 m up
//! and the wire ending 0.8 m farther out left in the air). A row mounted on another goes or stays with that row, never
//! by its own place or height, and a culled support takes the wires ending within its reach.
//!
//! - prop_assemblies: the row each row is mounted on (the root of a chain), -1 for none: a pole lamp ("polelamp") on
//!   the wire support at its x, y (both rounded to MOUNT_M); a lens or a glow on the lamp whose head it is (the heads
//!   given: furniture.lamp_lenses' rows of each row, rounded to 1 cm, matched exactly).
//! - assembly_cull: an assembly culled as one when any structural member is (its support or post, a pole lamp on it);
//!   lenses and glows follow.
//! - wire_cull: a wire end goes with its own support (culled in this tile or the next) or a culled support within
//!   support_reach.
//!
//! Rounding and the distances as the Python they replace: round(x / MOUNT_M) to even, the libm hypot (Dm), compared
//! with <=: the same rows and wires as before, bit for bit.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

/// m: a pole lamp is on the support at its x, y (both rows rounded to 1 cm)
pub const MOUNT_M: f64 = 0.05;
/// m from a support's x, y its wires end (furniture.WIRE_ARMS 0.8, the regional poles' 1.1, powerlines' wooden pole
/// 1.07)
pub const SUPPORT_REACH_M: f64 = 1.5;
/// m: a lattice pylon's (powerlines.ARMS 4.12)
pub const PYLON_REACH_M: f64 = 5.0;

const POLELAMP: &str = "polelamp";
/// the rows mounted on another: the pole lamp (on its pole), the lenses and glows (at a lamp's head)
const MOUNTED: [&str; 5] = [POLELAMP, "lamplens", "lampglow", "high_mast_light", "fr_lantern_light"];

/// Does a row of this group carry wires: a utility pole of any style (furniture's "pole", poleprofiles' pole_*,
/// eu_pole_*, *_pole), a power line's pole or pylon (powerlines G_POLE, G_PYLON)? Not the pole lamp.
pub fn wire_support(group: &str) -> bool {
    group != POLELAMP
        && (group == "pole" || group.starts_with("pole_") || group.starts_with("eu_pole_") || group.ends_with("_pole")
            || group == "hvpole" || group == "pylon")
}

/// m from a support's x, y within which its wires end.
pub fn support_reach(group: &str) -> f64 {
    if group == "pylon" { PYLON_REACH_M } else { SUPPORT_REACH_M }
}

fn mounted(group: &str) -> bool {
    MOUNTED.contains(&group)
}

/// Python's round(v / MOUNT_M): to the nearest integer, ties to even
fn cell(v: f64) -> i64 {
    (v / MOUNT_M).round_ties_even() as i64
}

/// The parents (prop_assemblies' doc) of rows: names[group[i]] each row's group, xy (n, 2) its place, keys (n, 3) its
/// x, y, z rounded to 1 cm; heads (h, 5) [row, group, x, y, z]: the lens rows of each row (furniture.lamp_lenses),
/// rounded alike.
pub fn assemblies(names: &[String], group: &[i64], xy: &[[f64; 2]], keys: &[[f64; 3]], heads: &[[f64; 5]]) -> Vec<i64> {
    let n = group.len();
    let mut parent = vec![-1i64; n];
    let name = |i: usize| names.get(group[i] as usize).map(String::as_str).unwrap_or("");
    if !(0..n).any(|i| mounted(name(i))) {
        return parent;
    }
    let mut supports: HashMap<(i64, i64), usize> = HashMap::new();
    for i in 0..n {
        if wire_support(name(i)) {
            supports.entry((cell(xy[i][0]), cell(xy[i][1]))).or_insert(i);
        }
    }
    // (a head's key: its group and its 1 cm x, y, z as bits; -0.0 and 0.0 one key, as Python's tuple equality)
    let bits = |v: f64| if v == 0.0 { 0u64 } else { v.to_bits() };
    let mut by_head: HashMap<(i64, u64, u64, u64), usize> = HashMap::new();
    for h in heads {
        let row = h[0] as usize;
        if row >= n {
            continue;
        }
        let g = name(row);
        if mounted(g) && g != POLELAMP {        // (a pole lamp has a head too; a lens has none)
            continue;
        }
        by_head.entry((h[1] as i64, bits(h[2]), bits(h[3]), bits(h[4]))).or_insert(row);
    }
    for i in 0..n {
        let g = name(i);
        if !mounted(g) {
            continue;
        }
        if g == POLELAMP {
            let (kx, ky) = (cell(xy[i][0]), cell(xy[i][1]));
            'found: for dx in [0i64, -1, 1] {           // (a rounding at a cell's edge)
                for dy in [0i64, -1, 1] {
                    if let Some(&j) = supports.get(&(kx + dx, ky + dy)) {
                        parent[i] = j as i64;
                        break 'found;
                    }
                }
            }
        } else if let Some(&j) = by_head.get(&(group[i], bits(keys[i][0]), bits(keys[i][1]), bits(keys[i][2]))) {
            parent[i] = j as i64;
        }
    }
    for i in 0..n {
        let mut j = parent[i];
        while j >= 0 && parent[j as usize] >= 0 {
            j = parent[j as usize];
        }
        parent[i] = j;
    }
    parent
}

/// The assemblies' verdicts: (n,) culled per row from hit (n,) (each row judged alone), an assembly culled as one when
/// any of its structural members is (its support or post and a pole lamp on it: a clear pole whose arm reaches into a
/// deck goes with its arm; Codex's review of 1bc9f8cb), its lenses and glows following (never culling it: a post beside
/// a deck whose head reaches under the deck's side keeps its lens). names, group, xy, keys, heads: assemblies'.
pub fn cull(names: &[String], group: &[i64], xy: &[[f64; 2]], keys: &[[f64; 3]], heads: &[[f64; 5]], hit: &[bool])
            -> Vec<bool> {
    let n = group.len().min(hit.len());
    let parent = assemblies(names, &group[..n], &xy[..n], &keys[..n], heads);
    let name = |i: usize| names.get(group[i] as usize).map(String::as_str).unwrap_or("");
    let root = |i: usize| if parent[i] >= 0 { parent[i] as usize } else { i };
    let mut out = vec![false; n];
    for i in 0..n {
        if hit[i] && (parent[i] < 0 || name(i) == POLELAMP) {
            out[root(i)] = true;
        }
    }
    (0..n).map(|i| if parent[i] >= 0 { out[root(i)] } else { out[i] || hit[i] }).collect()
}

/// cull's kernel (furniture.assembly_drop): names (the groups' names), group (n,) each row's index into them, xy (n, 2)
/// the rows' x, y, keys (n, 3) their x, y, z rounded to 1 cm, heads (h, 5) [row, group index, x, y, z (1 cm)]:
/// furniture.lamp_lenses' rows of each row; hit (n,) each row's own verdict.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn assembly_cull<'py>(py: Python<'py>, names: Vec<String>, group: PyReadonlyArray1<'py, i64>,
                          xy: PyReadonlyArray2<'py, f64>, keys: PyReadonlyArray2<'py, f64>,
                          heads: PyReadonlyArray2<'py, f64>, hit: PyReadonlyArray1<'py, bool>)
                          -> Bound<'py, PyArray1<bool>> {
    let (g, xv, kv, hv, h) = (group.as_array(), xy.as_array(), keys.as_array(), heads.as_array(), hit.as_array());
    let n = g.len();
    let gs: Vec<i64> = g.iter().copied().collect();
    let xs: Vec<[f64; 2]> = (0..n).map(|i| [xv[[i, 0]], xv[[i, 1]]]).collect();
    let ks: Vec<[f64; 3]> = (0..n).map(|i| [kv[[i, 0]], kv[[i, 1]], kv[[i, 2]]]).collect();
    let hs: Vec<[f64; 5]> = (0..hv.shape()[0]).map(|k| [hv[[k, 0]], hv[[k, 1]], hv[[k, 2]], hv[[k, 3]], hv[[k, 4]]])
        .collect();
    let hits: Vec<bool> = h.iter().copied().collect();
    cull(&names, &gs, &xs, &ks, &hs, &hits).into_pyarray(py)
}

/// (m,) for wire ends (m, 2): does the wire go there? Its own support culled (cut (m,): the support at that end, in
/// this tile or the next, judged by the same rules: a span is drawn by the tile of its first pole, so the culled pole
/// at its other end was no row here; Codex's review of 1bc9f8cb), or within support_reach of one of the culled rows
/// (names[group[k]], xy (k, 2)) that carries wires (wire_support).
#[pyfunction]
pub fn wire_cull<'py>(py: Python<'py>, ends: PyReadonlyArray2<'py, f64>, cut: PyReadonlyArray1<'py, bool>,
                      names: Vec<String>, group: PyReadonlyArray1<'py, i64>, xy: PyReadonlyArray2<'py, f64>)
                      -> Bound<'py, PyArray1<bool>> {
    let (e, c, g, xv) = (ends.as_array(), cut.as_array(), group.as_array(), xy.as_array());
    let sup: Vec<(f64, f64, f64)> = (0..g.len()).filter_map(|k| {
        let nm = names.get(g[k] as usize).map(String::as_str).unwrap_or("");
        wire_support(nm).then(|| (xv[[k, 0]], xv[[k, 1]], support_reach(nm)))
    }).collect();
    let out: Vec<bool> = (0..e.shape()[0]).map(|i| {
        let (x, y) = (e[[i, 0]], e[[i, 1]]);
        c.get(i).copied().unwrap_or(false) || sup.iter().any(|&(sx, sy, r)| (x - sx).dhypot(y - sy) <= r)
    }).collect();
    out.into_pyarray(py)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn supports() {
        for g in ["pole", "pole_concrete_recessed", "eu_pole_wood_a", "fr_wood_pole", "hvpole", "pylon"] {
            assert!(wire_support(g), "{g}");
        }
        for g in ["polelamp", "streetlight", "lamplens", "hydrant"] {
            assert!(!wire_support(g), "{g}");
        }
        assert_eq!(support_reach("pylon"), PYLON_REACH_M);
        assert_eq!(support_reach("pole"), SUPPORT_REACH_M);
    }

    #[test]
    fn a_pole_lamp_and_its_lens_on_the_pole() {
        let nm = names(&["pole", "polelamp", "lamplens", "hydrant"]);
        let group = [0, 1, 2, 3];
        let xy = [[1.0, 2.0], [1.0, 2.0], [-1.68, 2.0], [5.0, 5.0]];
        let keys = [[1.0, 2.0, 0.0], [1.0, 2.0, 7.0], [-1.68, 2.0, 7.4], [5.0, 5.0, 0.0]];
        let heads = [[1.0, 2.0, -1.68, 2.0, 7.4]];
        assert_eq!(assemblies(&nm, &group, &xy, &keys, &heads), vec![-1, 0, 0, -1]);
        // (no mounted row: nothing)
        assert_eq!(assemblies(&nm, &[0, 3], &xy[..2], &keys[..2], &[]), vec![-1, -1]);
    }
}
