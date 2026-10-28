//! The forest ambience of a vegetation tile (2026-10-26, the user: "Start with 1"): the stock maps' wind in the
//! trees and room tone (lua/ge/extensions/core/audioForest.lua) fed from our trees instead of a level's Forest. The
//! stock module thins a level's Forest items into nodes ~20 m apart (a node: its ground z, half its tree's height,
//! the mean size class of the items it took, how many); here a tile's items are binned into squares of the anchor
//! frame, which gives the same kind of node without the greedy search. The ambience set (wind and room tone) is
//! the stock maps' by climate: temperate (ConiferousDeciduous), Mediterranean, desert, tropical.

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1};
use pyo3::prelude::*;
use std::collections::BTreeMap;

pub const TEMPERATE: u8 = 0;
pub const MEDITERRANEAN: u8 = 1;
pub const DESERT: u8 = 2;
pub const TROPICAL: u8 = 3;

/// The sound nodes of a tile's items (group, x, y in the anchor frame, ground z, height m): the items whose group is
/// in `sound` binned into cell x cell squares (from the frame's origin); per square with any, a row [x, y (the
/// items' centroid), z (their mean ground), depth (half the tallest one's height: the stock nodes' lift), size (the
/// mean class: 0 below h_mid, 0.5 below h_large, else 1), count], in (row, column) order of the squares.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn audio_nodes<'py>(py: Python<'py>, group: PyReadonlyArray1<'py, u8>, x: PyReadonlyArray1<'py, f64>,
                        y: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                        height: PyReadonlyArray1<'py, f64>, sound: Vec<u8>, cell: f64, h_mid: f64, h_large: f64)
                        -> Bound<'py, PyArray2<f64>> {
    let (group, x, y, z, height) = (group.as_array(), x.as_array(), y.as_array(), z.as_array(), height.as_array());
    let mut take = [false; 256];
    for g in sound {
        take[g as usize] = true;
    }
    // (count, sum x, sum y, sum z, tallest, sum class)
    let mut cells: BTreeMap<(i64, i64), (f64, f64, f64, f64, f64, f64)> = BTreeMap::new();
    for i in 0..group.len() {
        if !take[group[i] as usize] {
            continue;
        }
        let h = height[i];
        let class = if h < h_mid { 0.0 } else if h < h_large { 0.5 } else { 1.0 };
        let key = ((y[i] / cell).floor() as i64, (x[i] / cell).floor() as i64);
        let a = cells.entry(key).or_insert((0.0, 0.0, 0.0, 0.0, 0.0, 0.0));
        a.0 += 1.0;
        a.1 += x[i];
        a.2 += y[i];
        a.3 += z[i];
        a.4 = a.4.max(h);
        a.5 += class;
    }
    let mut out = Vec::with_capacity(cells.len() * 6);
    for (n, sx, sy, sz, top, sc) in cells.into_values() {
        out.extend_from_slice(&[sx / n, sy / n, sz / n, 0.5 * top, sc / n, n]);
    }
    let k = out.len() / 6;
    numpy::ndarray::Array2::from_shape_vec((k, 6), out).unwrap().into_pyarray(py)
}

/// The ambience set of a tile at (lat, lon) with these items' groups: (set, variant), the set TEMPERATE,
/// MEDITERRANEAN, DESERT or TROPICAL, the variant the stock _01 (0) or _02 (1). koppen: the tile's Koppen-Geiger
/// class (Beck et al. 2023, 1991-2020: 1-30 as their legend, 0 none: the sea) by koppen_biome; without one, a coarse
/// climate from the position and the groups (ids: desert_shrub, desert_rock, juniper) by biome.
#[pyfunction]
pub fn audio_biome(group: PyReadonlyArray1<'_, u8>, lat: f64, lon: f64, ids: [u8; 3], koppen: u8) -> (u8, u8) {
    if let Some(b) = koppen_biome(koppen) {
        return b;
    }
    let group = group.as_array();
    let dry = group.iter().filter(|g| ids.contains(g)).count();
    biome(group.len(), dry, lat, lon)
}

/// The stock set of a Koppen-Geiger class: tropical (A: Jungle Rock Island's), desert (B arid and E polar: hot BWh /
/// BSh as Johnson Valley's _02, cold as Utah's _01), Mediterranean (Cs: hot-summer Csa as Italy's _01, the cooler
/// Csb / Csc as Small Island's and Cliff's _02), temperate (the other C and D: oceanic Cfb / Cfc as West Coast USA's
/// _02, the rest as East Coast USA's _01). None for 0 (no class) or an unknown one.
fn koppen_biome(k: u8) -> Option<(u8, u8)> {
    Some(match k {
        1..=3 => (TROPICAL, 0),
        4 | 6 => (DESERT, 1),
        5 | 7 | 29 | 30 => (DESERT, 0),
        8 => (MEDITERRANEAN, 0),
        9 | 10 => (MEDITERRANEAN, 1),
        15 | 16 => (TEMPERATE, 1),
        11..=14 | 17..=28 => (TEMPERATE, 0),
        _ => return None,
    })
}

fn biome(total: usize, dry: usize, lat: f64, lon: f64) -> (u8, u8) {
    if total > 0 && 2 * dry >= total {
        return (DESERT, (lon < -114.0) as u8);
    }
    if lat.abs() < 23.5 {
        return (TROPICAL, 0);
    }
    if lat > 30.0 && lat < 46.0 && lon > -10.0 && lon < 40.0 {
        return (MEDITERRANEAN, 0);
    }
    let west_coast = lon < -114.0 && lat > 32.0 && lat < 49.5;
    (TEMPERATE, west_coast as u8)
}

/// The sound spaces along lines (the stock maps' SFXSpace boxes: a tunnel's reverb in its tube, the one under a bridge
/// below its deck): lines given as points (n, 3) with offsets (m + 1); per line its kind (returned as is), width, and
/// the box's reach below its lowest and above its highest point (negative: under it). Consecutive segments go into one
/// box while it stays within max_len, its heading within max_turn (radians) of its first segment's and its heights
/// within max_dz; each box reaches `extend` past its ends (boxes overlap at their joints). Rows [kind, x, y, z (the
/// box's centre), length, width, height, yaw (radians, its length along (cos, sin))].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn sound_boxes<'py>(py: Python<'py>, pts: numpy::PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                        kind: PyReadonlyArray1<'py, f64>, width: PyReadonlyArray1<'py, f64>,
                        below: PyReadonlyArray1<'py, f64>, above: PyReadonlyArray1<'py, f64>, max_len: f64,
                        max_turn: f64, max_dz: f64, extend: f64) -> Bound<'py, PyArray2<f64>> {
    let (pts, off, kind, width, below, above) =
        (pts.as_array(), off.as_array(), kind.as_array(), width.as_array(), below.as_array(), above.as_array());
    let mut out: Vec<f64> = Vec::new();
    for l in 0..kind.len() {
        let (a, b) = (off[l] as usize, off[l + 1] as usize);
        let mut i = a;
        while i + 1 < b {
            // a run of segments from point i to point j
            let (x0, y0) = (pts[[i, 0]], pts[[i, 1]]);
            let h0 = (pts[[i + 1, 1]] - y0).atan2(pts[[i + 1, 0]] - x0);
            let (mut zlo, mut zhi) = (pts[[i, 2]].min(pts[[i + 1, 2]]), pts[[i, 2]].max(pts[[i + 1, 2]]));
            let mut j = i + 1;
            while j + 1 < b {
                let (dx, dy) = (pts[[j + 1, 0]] - pts[[j, 0]], pts[[j + 1, 1]] - pts[[j, 1]]);
                let mut turn = dy.atan2(dx) - h0;
                while turn > std::f64::consts::PI { turn -= 2.0 * std::f64::consts::PI; }
                while turn < -std::f64::consts::PI { turn += 2.0 * std::f64::consts::PI; }
                let z = pts[[j + 1, 2]];
                let len = (pts[[j + 1, 0]] - x0).hypot(pts[[j + 1, 1]] - y0);
                if turn.abs() > max_turn || len > max_len || zhi.max(z) - zlo.min(z) > max_dz {
                    break;
                }
                zlo = zlo.min(z);
                zhi = zhi.max(z);
                j += 1;
            }
            let (x1, y1) = (pts[[j, 0]], pts[[j, 1]]);
            let len = (x1 - x0).hypot(y1 - y0);
            if len > 1e-6 {
                let (z0, z1) = (zlo - below[l], zhi + above[l]);
                out.extend_from_slice(&[kind[l], (x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0, len + 2.0 * extend,
                                        width[l], z1 - z0, (y1 - y0).atan2(x1 - x0)]);
            }
            i = j;
        }
    }
    let k = out.len() / 8;
    numpy::ndarray::Array2::from_shape_vec((k, 8), out).unwrap().into_pyarray(py)
}

/// Shorelines of a water raster for the shore sound (the mod's wave ribbons): kind [j, i] 0 land, 1 lake, 2 sea; the
/// 4-connected water bodies of one kind with at least min_px pixels (every sea body) are kept; the edges between a
/// kept body's pixels and the rest, on pixel corners (x = i, y = j: pixel [j, i] spans x i..i + 1), those whose middle
/// lies in [i0, i1) x [j0, j1) (a cell's own pixels: its neighbour's raster has the next ones), chained into lines
/// and simplified (Douglas-Peucker, eps px); lines under min_len px dropped. (points (n, 2), offsets (m + 1), per line
/// one of its water pixels (m, 2) [j, i] and its kind).
#[pyfunction]
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
pub fn shore_lines<'py>(py: Python<'py>, kind: numpy::PyReadonlyArray2<'py, u8>, min_px: usize, eps: f64,
                        min_len: f64, i0: i64, i1: i64, j0: i64, j1: i64)
                        -> (Bound<'py, PyArray2<f64>>, Bound<'py, numpy::PyArray1<i64>>, Bound<'py, PyArray2<i64>>,
                            Bound<'py, numpy::PyArray1<u8>>) {
    let k = kind.as_array();
    let (h, w) = (k.nrows(), k.ncols());
    // bodies: 4-connected pixels of the same water kind
    let mut label = vec![u32::MAX; h * w];
    let mut keep: Vec<bool> = Vec::new();
    let mut body_kind: Vec<u8> = Vec::new();
    let mut body_px: Vec<(usize, usize)> = Vec::new();
    let mut stack: Vec<usize> = Vec::new();
    for s in 0..h * w {
        let kk = k[[s / w, s % w]];
        if kk == 0 || label[s] != u32::MAX {
            continue;
        }
        let id = keep.len() as u32;
        let mut n = 0usize;
        label[s] = id;
        stack.push(s);
        while let Some(p) = stack.pop() {
            n += 1;
            let (j, i) = (p / w, p % w);
            let mut visit = |q: usize| {
                if label[q] == u32::MAX && k[[q / w, q % w]] == kk {
                    label[q] = id;
                    stack.push(q);
                }
            };
            if i > 0 { visit(p - 1); }
            if i + 1 < w { visit(p + 1); }
            if j > 0 { visit(p - w); }
            if j + 1 < h { visit(p + w); }
        }
        keep.push(kk == 2 || n >= min_px);
        body_kind.push(kk);
        body_px.push((s / w, s % w));
    }
    let wet = |j: usize, i: usize| -> Option<u32> {
        let l = label[j * w + i];
        if l != u32::MAX && keep[l as usize] { Some(l) } else { None }
    };
    // edges between corners (x, y), each with the body on its water side
    let inside = |mx: f64, my: f64| mx >= i0 as f64 && mx < i1 as f64 && my >= j0 as f64 && my < j1 as f64;
    let mut edges: Vec<((i64, i64), (i64, i64), u32)> = Vec::new();
    for j in 0..h {
        for i in 0..w {
            let a = wet(j, i);
            if i + 1 < w {
                let b = wet(j, i + 1);
                if a != b && inside((i + 1) as f64, j as f64 + 0.5) {
                    let x = (i + 1) as i64;
                    edges.push(((x, j as i64), (x, j as i64 + 1), a.or(b).unwrap()));
                }
            }
            if j + 1 < h {
                let b = wet(j + 1, i);
                if a != b && inside(i as f64 + 0.5, (j + 1) as f64) {
                    let y = (j + 1) as i64;
                    edges.push(((i as i64, y), (i as i64 + 1, y), a.or(b).unwrap()));
                }
            }
        }
    }
    // chained: open lines from corners with one edge first, then the loops
    let mut at: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
    for (e, (p, q, _)) in edges.iter().enumerate() {
        at.entry(*p).or_default().push(e);
        at.entry(*q).or_default().push(e);
    }
    let mut used = vec![false; edges.len()];
    let mut starts: Vec<(i64, i64)> = at.iter().filter(|(_, v)| v.len() == 1).map(|(c, _)| *c).collect();
    starts.sort();
    let mut loops: Vec<(i64, i64)> = edges.iter().map(|e| e.0).collect();
    loops.sort();
    let (mut pts, mut off, mut px, mut kinds) = (Vec::new(), vec![0i64], Vec::new(), Vec::new());
    for c0 in starts.into_iter().chain(loops) {
        let mut line = vec![c0];
        let mut body = None;
        let mut c = c0;
        loop {
            let next = at[&c].iter().copied().find(|&e| !used[e]);
            let Some(e) = next else { break };
            used[e] = true;
            let (p, q, b) = edges[e];
            body.get_or_insert(b);
            c = if p == c { q } else { p };
            line.push(c);
            if c == c0 { break; }
        }
        if line.len() < 2 {
            continue;
        }
        let fl: Vec<(f64, f64)> = line.iter().map(|&(x, y)| (x as f64, y as f64)).collect();
        let len: f64 = fl.windows(2).map(|s| (s[1].0 - s[0].0).hypot(s[1].1 - s[0].1)).sum();
        if len < min_len {
            continue;
        }
        let simple = douglas_peucker(&fl, eps);
        for (x, y) in &simple {
            pts.push(*x);
            pts.push(*y);
        }
        off.push((pts.len() / 2) as i64);
        let b = body.unwrap() as usize;
        px.push(body_px[b].0 as i64);
        px.push(body_px[b].1 as i64);
        kinds.push(body_kind[b]);
    }
    let n = pts.len() / 2;
    let m = kinds.len();
    (numpy::ndarray::Array2::from_shape_vec((n, 2), pts).unwrap().into_pyarray(py), off.into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((m, 2), px).unwrap().into_pyarray(py), kinds.into_pyarray(py))
}

/// Douglas-Peucker: the points of line within eps of it kept (its ends always).
fn douglas_peucker(line: &[(f64, f64)], eps: f64) -> Vec<(f64, f64)> {
    let n = line.len();
    if n < 3 {
        return line.to_vec();
    }
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[n - 1] = true;
    let mut stack = vec![(0usize, n - 1)];
    while let Some((a, b)) = stack.pop() {
        let (ax, ay) = line[a];
        let (bx, by) = line[b];
        let (dx, dy) = (bx - ax, by - ay);
        let l = dx.hypot(dy);
        let (mut best, mut bd) = (0usize, -1.0f64);
        for (k, &(x, y)) in line.iter().enumerate().take(b).skip(a + 1) {
            let d = if l > 1e-12 { ((x - ax) * dy - (y - ay) * dx).abs() / l } else { (x - ax).hypot(y - ay) };
            if d > bd {
                bd = d;
                best = k;
            }
        }
        if bd > eps {
            keep[best] = true;
            stack.push((a, best));
            stack.push((best, b));
        }
    }
    line.iter().zip(keep).filter(|(_, k)| *k).map(|(p, _)| *p).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn koppen() {
        assert_eq!(koppen_biome(4), Some((DESERT, 1)));             // BWh: Mojave
        assert_eq!(koppen_biome(7), Some((DESERT, 0)));             // BSk: Utah, Denver
        assert_eq!(koppen_biome(9), Some((MEDITERRANEAN, 1)));      // Csb: San Francisco, Cape Town
        assert_eq!(koppen_biome(14), Some((TEMPERATE, 0)));         // Cfa: Baltimore
        assert_eq!(koppen_biome(15), Some((TEMPERATE, 1)));         // Cfb: Seattle, London
        assert_eq!(koppen_biome(1), Some((TROPICAL, 0)));
        assert_eq!(koppen_biome(0), None);
    }

    #[test]
    fn simplify() {
        let l = [(0.0, 0.0), (1.0, 0.1), (2.0, 0.0), (3.0, 5.0)];
        assert_eq!(douglas_peucker(&l, 0.5), vec![(0.0, 0.0), (2.0, 0.0), (3.0, 5.0)]);
    }

    #[test]
    fn biomes() {
        assert_eq!(biome(10, 8, 38.6, -109.5), (DESERT, 0));        // Moab
        assert_eq!(biome(10, 8, 34.4, -116.5), (DESERT, 1));        // Johnson Valley
        assert_eq!(biome(10, 1, 39.3, -76.6), (TEMPERATE, 0));      // Baltimore
        assert_eq!(biome(10, 1, 37.8, -122.3), (TEMPERATE, 1));     // Oakland
        assert_eq!(biome(0, 0, 41.9, 12.5), (MEDITERRANEAN, 0));    // Rome, no items
        assert_eq!(biome(4, 0, -3.1, -60.0), (TROPICAL, 0));        // Manaus
    }
}
