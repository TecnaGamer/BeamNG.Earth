//! German Autobahn guide signs (server/deautobahn.py): what each board says (content), where it stands (stations),
//! and the boards, gantries and cantilevers as one render-only mesh. The rules cite StVO Anlage 3, VwV-StVO, RWBA 2000 /
//! 2023 (via ARS 15/2024, BASt dWiSta 2022, the Hessen LWBA) and ZTV-ING; the design is
//! docs/research/2026-11-08-german-autobahn-signs-design.md.
#![allow(dead_code)]
use crate::dm::Dm;

pub(crate) mod content;
pub(crate) mod stations;
pub(crate) mod layout;
pub(crate) mod mesh;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;

use content::{classify, content, RAW_FIELDS};
use layout::{assemble, assemble_fork, fit_posts, Font, Geo, Panel};
use stations::{stations_follow, Board, Mount, Profile};
use crate::speedsigns::Mesh;

pub(crate) const JUNCTION_COLS: &[&str] = &["id", "side", "gore_s", "ref_s", "prev_exit_s", "fork_s", "other_motorway",
    "fork_x", "fork_y", "fork_z", "fork_dx", "fork_dy", "b0_dx", "b0_dy", "b1_dx", "b1_dy",
    "gore_x", "gore_y", "gore_z", "gore_dx", "gore_dy", "follow"];
pub(crate) const PROFILE_COLS: &[&str] = &["s", "x", "y", "dx", "dy", "edge_l", "edge_r", "opp_edge", "lanes",
    "ground_l", "ground_r", "road_z_max", "de", "through"];
pub(crate) const MAX_VERTICES: usize = 60_000;   // per written shape (16-bit indices)

fn col(cols: &[&str], name: &str) -> usize {
    cols.iter().position(|c| *c == name).unwrap_or(0)
}

fn json_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for ch in s.chars() {
        match ch {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn json_list(v: &[String]) -> String {
    format!("[{}]", v.iter().map(|s| json_str(s)).collect::<Vec<_>>().join(","))
}

fn json_opt(v: &Option<String>) -> String {
    v.as_ref().map(|s| json_str(s)).unwrap_or_else(|| "null".into())
}

fn json_panel(p: &Panel) -> String {
    let lines = format!("[{}]", p.lines.iter().map(|l| json_list(l)).collect::<Vec<_>>().join(","));
    format!("{{\"lines\":{},\"plates\":{},\"symbols\":{},\"number\":{},\"printed\":{},\"z0\":{:.3}}}", lines,
            json_list(&p.plates), json_list(&p.symbols), json_opt(&p.number), json_opt(&p.printed), p.z0)
}

fn board_name(b: Board) -> String {
    match b {
        Board::Announce => "announce".into(),
        Board::Advance(d) => format!("advance{d}"),
        Board::Countdown(n) => format!("countdown{n}"),
        Board::Direction => "direction".into(),
        Board::Gore => "gore".into(),
        Board::Fork => "fork".into(),
    }
}

fn mount_name(m: Mount) -> &'static str {
    match m {
        Mount::Portal => "portal",
        Mount::Cantilever => "cantilever",
        Mount::Side => "side",
        Mount::Post => "post",
        Mount::GoreSign => "goresign",
        Mount::Ground => "ground",
    }
}

fn offsets(off: &[i64], n: usize, total: usize, what: &str) -> PyResult<()> {
    if off.len() != n + 1 || off.first() != Some(&0) || off.windows(2).any(|w| w[1] < w[0])
        || off.last().map(|&v| v as usize) != Some(total) {
        return Err(PyValueError::new_err(format!("autobahn_signs: {what} offsets must be {n} + 1 ascending, 0 .. {total}")));
    }
    Ok(())
}

/// The kernel's input columns: (RAW_FIELDS, JUNCTION_COLS, PROFILE_COLS) (server/deautobahn.py mirrors them).
#[pyfunction]
pub fn autobahn_columns() -> (Vec<String>, Vec<String>, Vec<String>) {
    let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect();
    (s(RAW_FIELDS), s(JUNCTION_COLS), s(PROFILE_COLS))
}

/// The German Autobahn boards of a sign tile as one mesh: per junction its raw sign text (RAW_FIELDS), its row of
/// JUNCTION_COLS, its approach samples (PROFILE_COLS, s ascending) and skip ranges (decks, tunnels), the lettering
/// atlas (deautobahnfont.tables(), PICTOGRAMS) and the tile box [x0, y0, x1, y1] (an assembly belongs to the tile its
/// foot stands in). Returns (positions (v, 3) relative to (x0, y0, 0), triangles (t, 3), uvs (v, 2), material ids (t,)
/// into deautobahnfont.MATERIALS, one JSON line per board). Nearest junctions first; at most MAX_VERTICES vertices.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn autobahn_signs<'py>(py: Python<'py>, raw: Vec<String>, junctions: PyReadonlyArray2<'py, f64>,
                           profile: PyReadonlyArray2<'py, f64>, profile_off: PyReadonlyArray1<'py, i64>,
                           skips: PyReadonlyArray2<'py, f64>, skip_off: PyReadonlyArray1<'py, i64>,
                           blocks: PyReadonlyArray2<'py, f64>, block_off: PyReadonlyArray1<'py, i64>,
                           cps: PyReadonlyArray1<'py, i64>, glyphs: PyReadonlyArray2<'py, f64>,
                           faces: PyReadonlyArray1<'py, i64>, face_params: PyReadonlyArray2<'py, f64>,
                           kern: PyReadonlyArray2<'py, f64>, pictograms: Vec<String>,
                           pictogram_cps: PyReadonlyArray1<'py, i64>, tile: [f64; 4])
                           -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                                        Bound<'py, PyArray1<i64>>, Vec<String>)> {
    let (jn, pr, sk, bl) = (junctions.as_array(), profile.as_array(), skips.as_array(), blocks.as_array());
    let (poff, soff, boff) = (profile_off.as_array().to_vec(), skip_off.as_array().to_vec(), block_off.as_array().to_vec());
    let (cp, gl, fc, fp, kn) = (cps.as_array(), glyphs.as_array(), faces.as_array(), face_params.as_array(), kern.as_array());
    let pcp = pictogram_cps.as_array();
    let n = jn.nrows();
    let bad = |m: String| Err(PyValueError::new_err(format!("autobahn_signs: {m}")));
    if jn.ncols() != JUNCTION_COLS.len() { return bad(format!("junctions need {} columns", JUNCTION_COLS.len())); }
    if raw.len() != n * RAW_FIELDS.len() { return bad(format!("raw needs {} strings a junction", RAW_FIELDS.len())); }
    if pr.ncols() != PROFILE_COLS.len() { return bad(format!("profile needs {} columns", PROFILE_COLS.len())); }
    if sk.ncols() != 2 { return bad("skips are (k, 2)".into()); }
    offsets(&poff, n, pr.nrows(), "profile")?;
    offsets(&soff, n, sk.nrows(), "skip")?;
    if bl.ncols() != 2 { return bad("blocks are (b, 2)".into()); }
    offsets(&boff, pr.nrows(), bl.nrows(), "block")?;
    if gl.ncols() != 9 || gl.nrows() != cp.len() || fc.len() != cp.len() {
        return bad("glyphs are (g, 9) with g code points and faces".into());
    }
    if fp.nrows() != 3 || fp.ncols() != 2 { return bad("face_params are (3, 2)".into()); }
    if kn.ncols() != 4 { return bad("kern is (k, 4)".into()); }
    if pictograms.len() != pcp.len() { return bad("pictograms and their code points differ in length".into()); }
    let rows: Vec<[f64; 9]> = (0..gl.nrows()).map(|i| {
        let mut r = [0.0; 9];
        for (c, v) in r.iter_mut().enumerate() { *v = gl[[i, c]]; }
        r
    }).collect();
    let kr: Vec<[f64; 4]> = (0..kn.nrows()).map(|i| [kn[[i, 0]], kn[[i, 1]], kn[[i, 2]], kn[[i, 3]]]).collect();
    let params = [[fp[[0, 0]], fp[[0, 1]]], [fp[[1, 0]], fp[[1, 1]]], [fp[[2, 0]], fp[[2, 1]]]];
    let symbols: HashMap<String, u32> = pictograms.iter().cloned().zip(pcp.iter().map(|&v| v.max(0) as u32)).collect();
    let font = Font::new(&cp.to_vec(), &rows, &fc.to_vec(), params, &kr, symbols);
    let jc = |name: &str| col(JUNCTION_COLS, name);
    let pc = |name: &str| col(PROFILE_COLS, name);
    let (cx, cy) = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0);
    let mut order: Vec<(f64, usize)> = (0..n).filter(|&j| poff[j + 1] > poff[j]).map(|j| {
        let r = poff[j] as usize;
        ((pr[[r, pc("x")]] - cx).dhypot(pr[[r, pc("y")]] - cy), j)
    }).collect();
    order.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1)));
    let mut all = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3],
                         face: [0.0; 2], right: [0.0; 2] };
    let mut boards = Vec::new();
    for (_, j) in order {
        let r: Vec<&str> = raw[j * RAW_FIELDS.len()..(j + 1) * RAW_FIELDS.len()].iter().map(|s| s.as_str()).collect();
        let v = |name: &str| jn[[j, jc(name)]];
        let kind = classify(&r, v("other_motorway") > 0.5);
        let side = if v("side") < 0.0 { -1 } else { 1 };
        let Some(c) = content(&r, kind, side) else { continue };
        let (p0, p1) = (poff[j] as usize, poff[j + 1] as usize);
        let s_vals: Vec<f64> = (p0..p1).map(|i| pr[[i, pc("s")]]).collect();
        // the mounting counts the through lanes (an exit lane is not one); the layout (Geo.lanes) the lanes as drawn.
        // A missing or zero through count: the drawn one
        let lanes: Vec<f64> = (p0..p1).map(|i| {
            let t = pr[[i, pc("through")]];
            if t.is_finite() && t > 0.0 { t } else { pr[[i, pc("lanes")]] }
        }).collect();
        let skip_j: Vec<(f64, f64)> = (soff[j] as usize..soff[j + 1] as usize).map(|i| (sk[[i, 0]], sk[[i, 1]])).collect();
        let (ref_s, gore_s, prev) = (v("ref_s"), v("gore_s"), v("prev_exit_s"));
        if !(ref_s.is_finite() && gore_s.is_finite()) { continue; }
        let st = stations_follow(kind, &c, ref_s, gore_s, prev.is_finite().then_some(prev), v("follow") > 0.5,
                                 &Profile { s: &s_vals, lanes: &lanes }, &skip_j);
        let mut m = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3],
                           face: [0.0; 2], right: [0.0; 2] };
        let mut lines = Vec::new();
        for s in &st {
            let (geo, assembly) = match s.board {
                Board::Gore => {
                    let g = Geo { x: v("gore_x"), y: v("gore_y"), dx: v("gore_dx"), dy: v("gore_dy"), edge_l: -1.0,
                                  edge_r: 1.0, opp_edge: f64::NAN, lanes: 1.0, ground_l: v("gore_z"),
                                  ground_r: v("gore_z"), road_z: v("gore_z") };
                    (g, assemble(s, &c, &g, &font))
                }
                Board::Fork => {
                    let g = Geo { x: v("fork_x"), y: v("fork_y"), dx: v("fork_dx"), dy: v("fork_dy"), edge_l: -1.0,
                                  edge_r: 1.0, opp_edge: f64::NAN, lanes: 1.0, ground_l: v("fork_z"),
                                  ground_r: v("fork_z"), road_z: v("fork_z") };
                    let rel = |bx: f64, by: f64| (bx * g.dx + by * g.dy, bx * g.dy - by * g.dx);
                    let dirs = [rel(v("b0_dx"), v("b0_dy")), rel(v("b1_dx"), v("b1_dy"))];
                    (g, assemble_fork(&c, &g, dirs, &font))
                }
                _ => {
                    // the nearest sample, then the next ones along the road until the posts keep off other pavements
                    let Some(i0) = (p0..p1).min_by(|&a, &b| (pr[[a, pc("s")]] - s.s).abs()
                        .partial_cmp(&(pr[[b, pc("s")]] - s.s).abs()).unwrap_or(std::cmp::Ordering::Equal)) else { continue };
                    let reach: i64 = if matches!(s.board, Board::Announce | Board::Advance(_)) { 8 } else { 3 };
                    let mut found = None;
                    for k in (0..=2 * reach).map(|k| if k % 2 == 0 { k / 2 } else { -(k + 1) / 2 }) {
                        let i = i0 as i64 + k;
                        if i < p0 as i64 || i >= p1 as i64 { continue; }
                        let i = i as usize;
                        if pr[[i, pc("de")]] < 0.5 { continue; }
                        // the sample itself off the decks and tunnels too (the station is; its nearest sample and the
                        // posts' search may not be)
                        let si = pr[[i, pc("s")]];
                        if skip_j.iter().any(|&(a, b)| si > a.min(b) && si < a.max(b)) { continue; }
                        let q = |name: &str| pr[[i, pc(name)]];
                        let g = Geo { x: q("x"), y: q("y"), dx: q("dx"), dy: q("dy"), edge_l: q("edge_l"),
                                      edge_r: q("edge_r"), opp_edge: q("opp_edge"), lanes: q("lanes"),
                                      ground_l: q("ground_l"), ground_r: q("ground_r"), road_z: q("road_z_max") };
                        let Some(mut a) = assemble(s, &c, &g, &font) else { break };
                        let blk: Vec<(f64, f64)> = (boff[i] as usize..boff[i + 1] as usize).map(|b| (bl[[b, 0]], bl[[b, 1]])).collect();
                        if fit_posts(&mut a, &g, &blk, &c, s, &font) {
                            found = Some((g, a));
                            break;
                        }
                    }
                    let Some((g, a)) = found else { continue };
                    (g, Some(a))
                }
            };
            let Some(a) = assembly else { continue };
            let len = geo.dx.dhypot(geo.dy);
            if !(len > 1e-9 && geo.x.is_finite() && geo.y.is_finite() && geo.road_z.is_finite()) { continue; }
            let (dx, dy) = (geo.dx / len, geo.dy / len);
            let (ax, ay) = (geo.x + dy * a.anchor, geo.y - dx * a.anchor);
            if !(ax >= tile[0] && ax < tile[2] && ay >= tile[1] && ay < tile[3]) { continue; }
            m.centre = [geo.x - tile[0], geo.y - tile[1], 0.0];
            m.face = [-dx, -dy];
            m.right = [dy, -dx];
            mesh::draw(&mut m, &font, &a);
            let posts = a.posts.iter().map(|p| format!("[{:.2},{:.2}]", geo.x + dy * p.y, geo.y - dx * p.y))
                .collect::<Vec<_>>().join(",");
            lines.push(format!("{{\"j\":{},\"board\":{},\"mount\":{},\"s\":{},\"at\":[{:.2},{:.2}],\"dir\":[{:.4},{:.4}],\"posts\":[{}],\"panels\":[{}]}}",
                               v("id") as i64, json_str(&board_name(s.board)), json_str(mount_name(a.mount)),
                               if s.s.is_finite() { format!("{:.1}", s.s) } else { "null".into() }, geo.x, geo.y, dx, dy,
                               posts, a.panels.iter().map(json_panel).collect::<Vec<_>>().join(",")));
        }
        if all.p.len() + m.p.len() > MAX_VERTICES {
            continue;
        }
        let base = all.p.len() as i64;
        all.p.extend(m.p);
        all.uv.extend(m.uv);
        all.t.extend(m.t.into_iter().map(|[a, b, c]| [a + base, b + base, c + base]));
        all.mi.extend(m.mi);
        boards.extend(lines);
    }
    let (nv, nt) = (all.p.len(), all.t.len());
    let p = numpy::ndarray::Array2::from_shape_vec((nv, 3), all.p.into_iter().flatten().collect())
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let t = numpy::ndarray::Array2::from_shape_vec((nt, 3), all.t.into_iter().flatten().collect())
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let uv = numpy::ndarray::Array2::from_shape_vec((nv, 2), all.uv.into_iter().flatten().collect())
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok((p.into_pyarray(py), t.into_pyarray(py), uv.into_pyarray(py), all.mi.into_pyarray(py), boards))
}
