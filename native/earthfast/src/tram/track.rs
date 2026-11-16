//! Ground tram track (plan 046; ported from server/tramtrack.py and rails._tram, task 018): where it lies in the street,
//! how wide it is, what it is drawn with.
//!
//! A tram within STREET_MARGIN_M of a carriageway's edge lies in the street: a paved strip (asphalt, slabs in the
//! ex-USSR) with two rails on it, no sleepers and no bed. Open track gets a gravel bed and its two rails. The rails'
//! gauge is the line's OSM gauge (inside GAUGE_MIN_M..GAUGE_MAX_M), else 1.435 m.
use super::lines::{clip_polyline, densify, offset_polyline, Segs, P2};
use super::switches;
use crate::dm::Dm;

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

pub const STREET_MARGIN_M: f64 = 3.0; // a track this close to a carriageway's edge is in the street (VGU's 3.5 m, less a tolerance)
pub const GAUGE_DEFAULT_M: f64 = 1.435;
pub const GAUGE_MIN_M: f64 = 0.6;
pub const GAUGE_MAX_M: f64 = 1.7; // a gauge tag outside it is ignored
pub const DECAL_W_M: f64 = 1.8; // the street decal's width for GAUGE_DEFAULT_M
pub const PAVING_EXTRA_M: f64 = 1.0; // the paved strip is the gauge and this much wider
pub const RAIL_W_M: f64 = 0.17; // a rail's ribbon (the head and its dark flange groove either side)
const HEAD_M: f64 = 0.075; // a rail head's width; the rail lines lie half of it outside the gauge
const SAMPLE_M: f64 = 1.0; // m between the points tested for the street
const MIN_RUN_M: f64 = 2.0; // shorter street / open runs join their neighbours
const MARGIN_M: f64 = 4.0; // ribbons run this far past the tile box (the road mesh cuts them at it)
const JOIN_TOL_M: f64 = 0.3; // two part ends this near are one joint
const ON_ROAD_INSET_M: f64 = 0.3; // a track point this far inside a carriageway's edge is on the street

/// A road-mesh material look: material, texture length, priority, layer.
#[derive(Clone, Copy)]
pub struct Look {
    pub m: &'static str,
    pub tl: f64,
    pub p: i64,
    pub layer: &'static str,
}

pub const RAIL: Look = Look { m: "bne_tram_rail", tl: 4.0, p: 1, layer: "mark" };
pub const ASPHALT: Look = Look { m: "road_asphalt_2lane", tl: 11.0, p: 19, layer: "base" }; // the road's own asphalt
pub const SLAB: Look = Look { m: "bne_tram_slab", tl: 3.0, p: 19, layer: "base" }; // concrete track slabs (ex-USSR)
pub const BALLAST: Look = Look { m: "road_gravel", tl: 8.0, p: 19, layer: "base" }; // an open track's bed
// The user's street strips (plan 046 stage B, tools/ingest_tram_textures.py): base-layer paving with the two rails baked in
// (at 10 % and 90 % of the strip's width), so no separate rail ribbon is drawn on them; square images: the texture length
// is the strip's width. The ballast is a 1 m tile for open track.
pub const STREET_ASPHALT: Look = Look { m: "bne_tram_street_asphalt", tl: 1.0, p: 19, layer: "base" };
pub const STREET_SETTS: Look = Look { m: "bne_tram_street_setts", tl: 1.0, p: 19, layer: "base" };
pub const STREET_SLAB: Look = Look { m: "bne_tram_street_slab", tl: 1.0, p: 19, layer: "base" };
pub const STREET_GRASS: Look = Look { m: "bne_tram_street_grass", tl: 1.0, p: 19, layer: "base" };
/// The thin concrete border across a paved strip where it ends against open track, a bridge or the data's end.
pub const BORDER: Look = Look { m: "m_gm_concrete_plain", tl: 2.0, p: 4, layer: "gutter" };
/// (the border is drawn in the strip's own paving, not the bright stock concrete: see border_look)
/// m: the border's width along the track.
pub const BORDER_W_M: f64 = 0.12;
/// m: a plate over the frog of a switch (its length along the branch, its width).
pub const PLATE_LEN_M: f64 = 1.6;
pub const PLATE_W_M: f64 = 0.5;
/// Where the rails stand in each strip's picture, as the share of the strip's width between them (tools/ingest_tram_textures.py
/// STRIPS: the left rail at (1 - span) / 2, the right at (1 + span) / 2): the strip is made as wide as puts those rails a
/// gauge apart, so the baked rails and the 3D track's meet at one gauge.
pub fn strip_span(look: &Look) -> f64 {
    if look.m == STREET_SETTS.m {
        0.8015
    } else if look.m == STREET_SLAB.m {
        0.768
    } else if look.m == STREET_GRASS.m {
        0.7775
    } else {
        0.815
    }
}
/// The width of a strip with its rails baked in, for gauge g: the rails' centres a gauge and a head apart (HEAD_M).
pub fn strip_width(look: &Look, g: f64) -> f64 {
    py_round((g + HEAD_M) / strip_span(look), 3)
}

// The 3D open track's heights where it meets something flush (a paved strip, a bridge, a free end): the ballast comes down to
// the ground over OPEN_RAMP_M, its rails' heads down to the ballast over OPEN_RAIL_RAMP_M, and no sleeper lies in the ramp.
pub const OPEN_BALLAST_H_M: f64 = 0.02; // m the ballast top stands over the ground (a tram's track lies on the ground)
pub const OPEN_FLUSH_M: f64 = 0.003; // m over the ground at the junction (a decal's height there)
pub const OPEN_RAIL_ABOVE_M: f64 = 0.12; // m the rail head stands over the ballast top
pub const OPEN_SLEEPER_ABOVE_M: f64 = 0.04; // m the sleepers' tops stand over the ballast (half sunk)
pub const OPEN_SLEEPER_FREE_M: f64 = 0.4; // m of a ramped end without sleepers
pub const OPEN_RAMP_M: f64 = 1.75; // m the ballast ramps down over, and the sleeper-free length
pub const OPEN_RAIL_RAMP_M: f64 = 3.0; // m the rail head's height ramps over
pub const BALLAST_TEX: Look = Look { m: "bne_tram_ballast", tl: 1.0, p: 19, layer: "base" };
/// Countries whose street tracks lie in setts (granite sets: the historic cores of Central Europe) on a minor street.
pub const SETTS_COUNTRIES: [&str; 6] = ["CZ", "SK", "AT", "DE", "PL", "HU"];
/// Countries whose open tracks beside a road are a grass strip (a green track).
pub const GRASS_COUNTRIES: [&str; 10] = ["DE", "AT", "CH", "NL", "FR", "FI", "DK", "SE", "PL", "CZ"];
/// A carriageway segment's tag for the minor streets (residential, living street, service, unclassified, pedestrian).
pub const TAG_MINOR: u8 = 1;
/// ... and for a street the map says is paved with sets or cobbles (the spec's kind "cobble"): setts in any country.
pub const TAG_COBBLE: u8 = 2;
/// The share of a paved run's points on minor streets from which it lies in setts (in a setts country).
const SETTS_SHARE: f64 = 0.5;

/// What a ground track is drawn with: the new strips (tex) or the old materials; where it lies (country, su).
#[derive(Clone)]
pub struct Style {
    pub su: bool,
    pub country: String,
    pub tex: bool,
    /// open track is built in 3D (railways.tile_mesh, tram_open_runs): the decals leave it out
    pub open3d: bool,
    /// the turnouts of the 3D track are drawn as turnouts (BNE_TRAM_SWITCH3D)
    pub switch3d: bool,
}

impl Style {
    /// Whether the unpaved runs are drawn in 3D: asked for, and not a green track (those stay a grass strip decal).
    pub fn open_is_3d(&self) -> bool {
        self.open3d && !(self.tex && self.in_list(&GRASS_COUNTRIES))
    }

    fn in_list(&self, list: &[&str]) -> bool {
        list.contains(&self.country.as_str())
    }
}

pub fn gauge(osm: Option<f64>) -> f64 {
    match osm {
        Some(g) if g >= GAUGE_MIN_M && g <= GAUGE_MAX_M => g,
        _ => GAUGE_DEFAULT_M,
    }
}

pub fn paving(su: bool) -> Look {
    if su {
        SLAB
    } else {
        ASPHALT
    }
}

/// Python's round(x, nd): the correctly rounded decimal (ties to even on the exact value).
pub fn py_round(x: f64, nd: usize) -> f64 {
    format!("{:.*}", nd, x).parse().unwrap_or(x)
}

pub fn paving_width(g: f64) -> f64 {
    py_round(g + PAVING_EXTRA_M, 3)
}

/// rails._runs: [(start, end, on)] index ranges (end inclusive) of equal values, runs shorter than MIN_RUN_M / SAMPLE_M
/// points joined into the one before; each run starts where the one before ends (no gap in the rails).
pub fn runs(on: &[bool]) -> Vec<(usize, usize, bool)> {
    let mut out: Vec<[usize; 2]> = Vec::new();
    let mut vals: Vec<bool> = Vec::new();
    for (k, &v) in on.iter().enumerate() {
        if let (Some(last), Some(&lv)) = (out.last_mut(), vals.last()) {
            if lv == v {
                last[1] = k;
                continue;
            }
        }
        out.push([k, k]);
        vals.push(v);
    }
    let short = (MIN_RUN_M / SAMPLE_M).ceil() as usize;
    let mut merged: Vec<(usize, usize, bool)> = Vec::new();
    for (r, &v) in out.iter().zip(&vals) {
        if let Some(m) = merged.last_mut() {
            if r[1] - r[0] < short || m.1 - m.0 < short {
                m.1 = r[1];
                continue;
            }
        }
        merged.push((r[0], r[1], v));
    }
    for k in 1..merged.len() {
        merged[k].0 = merged[k - 1].1;
    }
    merged
}

/// Which points lie in the street: within STREET_MARGIN_M of a carriageway's edge.
pub fn in_street(pts: &[P2], ways: &Segs) -> Vec<bool> {
    pts.iter().map(|&p| !ways.is_empty() && ways.near(p, STREET_MARGIN_M)).collect()
}

/// One chunk of the road tile's track: look, width, points (z 0: the road mesh drapes it).
pub struct Chunk {
    look: Look,
    w: f64,
    tl: f64,
    pts: Vec<P2>,
}

fn chunk_dict<'py>(py: Python<'py>, c: &Chunk) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("m", c.look.m)?;
    d.set_item("w", py_round(c.w, 3))?;
    d.set_item("tl", c.tl)?;
    d.set_item("p", c.look.p)?;
    d.set_item("layer", c.look.layer)?;
    let n = PyList::empty(py);
    for p in &c.pts {
        n.append(PyList::new(py, [py_round(p[0], 3), py_round(p[1], 3), 0.0])?)?;
    }
    d.set_item("n", n)?;
    Ok(d)
}

/// rails._tram: a ground tram's chunks inside tile `bx` (x0, y0, x1, y1): in the street (all: the carriageways, within
/// STREET_MARGIN_M, or on one of eligible within -ON_ROAD_INSET_M) a paved strip and two rails on it, on open ground
/// a bed (width bw) and two rails of gauge g. Returns (the bed and paving chunks, the rails).
/// The paving of a whole chain: one material from its first paved point to its last (a short part cannot flip it): the
/// user's slab strips in the ex-USSR, setts where at least half of the chain's paved points lie on a street the map says is
/// cobbled (or, in the setts countries, on minor streets), else asphalt; the old materials without the strips.
fn chain_paving(full: &[P2], street: &[bool], all: &Segs, st: &Style) -> Look {
    if !st.tex {
        return paving(st.su);
    }
    if st.su {
        return STREET_SLAB;
    }
    let tags: Vec<u8> = full.iter().zip(street).filter(|(_, &s)| s).map(|(&p, _)| all.near_tag(p, STREET_MARGIN_M)).collect();
    if tags.is_empty() {
        return STREET_ASPHALT;
    }
    let share = |bit: u8| tags.iter().filter(|&&t| t & bit != 0).count() as f64 / tags.len() as f64;
    if share(TAG_COBBLE) >= SETTS_SHARE || (st.in_list(&SETTS_COUNTRIES) && share(TAG_MINOR) >= SETTS_SHARE) {
        STREET_SETTS
    } else {
        STREET_ASPHALT
    }
}

/// The street flags of a chain's points (1 m apart): within STREET_MARGIN_M of a carriageway (all) or on one of eligible.
fn street_flags(full: &[P2], all: &Segs, eligible: &Segs) -> Vec<bool> {
    let a = in_street(full, all);
    full.iter().zip(&a).map(|(&p, &s)| s || (!eligible.is_empty() && eligible.near(p, -ON_ROAD_INSET_M))).collect()
}

/// rails._tram: a ground tram chain's chunks inside tile `bx` (x0, y0, x1, y1): in the street (all: the carriageways, within
/// STREET_MARGIN_M, or on one of eligible within -ON_ROAD_INSET_M) a paved strip (one material for the whole chain) and two
/// rails on it, on open ground a bed (width bw) and two rails of gauge g. The runs are found on the whole chain, then cut to
/// the tile, so every tile finds the same runs (tram_open_runs the same). Returns (the bed and paving chunks, the rails).
pub fn ground_chunks(xy: &[P2], bx: [f64; 4], g: f64, all: &Segs, eligible: &Segs, bw: f64, st: &Style,
                     free_ends: (bool, bool)) -> (Vec<Chunk>, Vec<Chunk>) {
    let (mut bed, mut rails): (Vec<Chunk>, Vec<Chunk>) = (Vec::new(), Vec::new());
    let mut borders: Vec<Chunk> = Vec::new();
    let off = g / 2.0 + HEAD_M / 2.0;
    let full = densify(xy, SAMPLE_M);
    if full.len() < 2 {
        return (bed, rails);
    }
    let street = street_flags(&full, all, eligible);
    let chain_look = chain_paving(&full, &street, all, st);
    let all_runs = runs(&street);
    let nruns = all_runs.len();
    let (cx0, cy0, cx1, cy1) = (bx[0] - MARGIN_M, bx[1] - MARGIN_M, bx[2] + MARGIN_M, bx[3] + MARGIN_M);
    for (ri, (a, b, paved)) in all_runs.into_iter().enumerate() {
        let whole = &full[a..=b];
        if whole.len() < 2 {
            continue;
        }
        let pw = if st.tex { strip_width(&chain_look, g) } else { paving_width(g) };
        for run in clip_polyline(whole, cx0, cy0, cx1, cy1) {
            let run = &run[..];
            if run.len() < 2 {
                continue;
            }
            if paved {
                // a thin border across the strip where it meets open track, and at the line's own free ends (the run's own
                // ends, not the tile's cut)
                let mut at: Vec<(P2, P2)> = Vec::new();
                let same = |p: P2, q: P2| (p[0] - q[0]).abs() < 1e-6 && (p[1] - q[1]).abs() < 1e-6;
                if (ri > 0 || free_ends.0) && same(run[0], whole[0]) {
                    at.push((run[0], run[1]));
                }
                if (ri + 1 < nruns || free_ends.1) && same(run[run.len() - 1], whole[whole.len() - 1]) {
                    at.push((run[run.len() - 1], run[run.len() - 2]));
                }
                for (c, nb) in at {
                    let l = (nb[0] - c[0]).dhypot(nb[1] - c[1]).max(1e-9);
                    let n = [-(nb[1] - c[1]) / l, (nb[0] - c[0]) / l];
                    let h = pw / 2.0;
                    borders.push(Chunk { look: Look { layer: "gutter", p: 4, ..chain_look }, w: BORDER_W_M, tl: if st.tex { pw } else { chain_look.tl },
                                         pts: vec![[c[0] - n[0] * h, c[1] - n[1] * h], [c[0] + n[0] * h, c[1] + n[1] * h]] });
                }
            }
            // (a strip with its rails baked in has no separate ribbon)
            let mut baked = false;
            if paved {
                baked = st.tex;
                bed.push(Chunk { look: chain_look, w: pw, tl: if st.tex { pw } else { chain_look.tl }, pts: run.to_vec() });
            } else if st.open_is_3d() {
                continue;           // (the 3D track of tile_mesh: tram_open_runs)
            } else if st.tex && st.in_list(&GRASS_COUNTRIES) {
                baked = true;
                let w = strip_width(&STREET_GRASS, g);
                bed.push(Chunk { look: STREET_GRASS, w, tl: w, pts: run.to_vec() });
            } else if st.tex {
                bed.push(Chunk { look: BALLAST_TEX, w: bw, tl: BALLAST_TEX.tl, pts: run.to_vec() });
            } else {
                bed.push(Chunk { look: BALLAST, w: bw, tl: BALLAST.tl, pts: run.to_vec() });
            }
            if !baked {
                for side in [off, -off] {
                    rails.push(Chunk { look: RAIL, w: RAIL_W_M, tl: RAIL.tl, pts: offset_polyline(run, side) });
                }
            }
        }
    }
    rails.extend(borders);
    (bed, rails)
}

/// Parts of one gauge that meet end to end where nothing else meets (an OSM way's joint: its end and the next way's
/// start) as one line, so the paving and the rails run on through the joint (mitred offsets: each way's own ribbon ended
/// square, a wedge open on the outside of a bend). A switch (three ends or more at a point) keeps its parts apart.
/// Returns (the line, its gauge) per chain.
pub fn chain_parts(parts: &[(Vec<P2>, f64)]) -> Vec<(Vec<P2>, f64)> {
    let n = parts.len();
    let end_pt = |k: usize, e: usize| if e == 0 { parts[k].0[0] } else { parts[k].0[parts[k].0.len() - 1] };
    let near = |a: P2, b: P2| (a[0] - b[0]).dhypot(a[1] - b[1]) <= JOIN_TOL_M;
    let mut link: Vec<[Option<(usize, usize)>; 2]> = vec![[None, None]; n];
    for k in 0..n {
        if parts[k].0.len() < 2 {
            continue;
        }
        for e in 0..2 {
            // the ends within JOIN_TOL_M of this one (its own part's other end too: a closed loop)
            let mut meets: Vec<(usize, usize)> = Vec::new();
            for k2 in 0..n {
                if parts[k2].0.len() < 2 {
                    continue;
                }
                for e2 in 0..2 {
                    if (k2, e2) != (k, e) && near(end_pt(k, e), end_pt(k2, e2)) {
                        meets.push((k2, e2));
                    }
                }
            }
            if meets.len() == 1 && meets[0].0 != k && (parts[k].1 - parts[meets[0].0].1).abs() < 1e-6 {
                link[k][e] = Some(meets[0]);
            }
        }
    }
    let mut seen = vec![false; n];
    let mut out = Vec::new();
    // chains start at a part with an unlinked end; what is left is loops
    let order: Vec<usize> = (0..n).filter(|&k| link[k][0].is_none() || link[k][1].is_none()).chain(0..n).collect();
    for k0 in order {
        if seen[k0] || parts[k0].0.len() < 2 {
            continue;
        }
        let mut e_in = if link[k0][0].is_none() { 0 } else { 1 };
        let (mut k, mut pts) = (k0, Vec::<P2>::new());
        loop {
            seen[k] = true;
            let mut seg = parts[k].0.clone();
            if e_in == 1 {
                seg.reverse();
            }
            let skip = if pts.is_empty() { 0 } else { 1 };
            pts.extend_from_slice(&seg[skip..]);
            match link[k][1 - e_in] {
                Some((k2, e2)) if !seen[k2] => {
                    k = k2;
                    e_in = e2;
                }
                _ => break,
            }
        }
        out.push((pts, parts[k0].1));
    }
    out
}

/// ground_chunks of every chain of the parts [(line, gauge)] (chain_parts): (the bed and paving chunks, the rails).
pub fn ground_chunks_many(parts: &[(Vec<P2>, f64)], bx: [f64; 4], all: &Segs, eligible: &Segs, bw: f64, st: &Style)
                          -> (Vec<Chunk>, Vec<Chunk>) {
    let (chains, _sws) = chains_refit(parts, st.switch3d);
    let lines: Vec<Vec<P2>> = chains.iter().map(|c| c.0.clone()).collect();
    let (mut pre, mut bed, mut rails) = (Vec::new(), Vec::new(), Vec::new());
    let free = switches::free_ends(&lines);
    let mut cut: Vec<Option<(Vec<P2>, Vec<P2>)>> = vec![None; chains.len()];
    if st.tex {
        // the user's strips have their rails in the picture: at a switch the branch's first metres, beside the through
        // track, are plain paving (under the through strip) with the branch's own rails and a plate on the frog
        for sw in switches::find(&lines) {
            let (g, pw) = (chains[sw.branch].1, strip_width(&STREET_ASPHALT, chains[sw.branch].1));
            if cut[sw.branch].is_none() {
                if let Some((zone, rest)) = switches::cut(&lines[sw.branch], sw.end, &lines[sw.through], pw) {
                    let plain = Style { su: st.su, country: st.country.clone(), tex: false, open3d: st.open3d, switch3d: st.switch3d };
                    let (zb, zr) = ground_chunks(&zone, bx, g, all, eligible, bw, &plain, (false, false));
                    pre.extend(zb);
                    rails.extend(zr);
                    // (a frog plate only where the zone is paved: on open ground the 3D track has its own frog)
                    if street_flags(&zone[..1], all, eligible)[0] {
                        let n = zone.len().min((PLATE_LEN_M as usize).max(2) + 1);
                        rails.push(Chunk { look: RAIL, w: PLATE_W_M, tl: RAIL.tl, pts: zone[..n].to_vec() });
                    }
                    cut[sw.branch] = Some((zone, rest));
                }
            }
        }
    }
    for (ci, (line, g)) in chains.iter().enumerate() {
        let line = match &cut[ci] {
            Some((_z, rest)) => rest,
            None => line,
        };
        let is_free = |e: usize| free.contains(&(ci, e));
        let (b, r) = ground_chunks(line, bx, *g, all, eligible, bw, st, (is_free(0), is_free(1)));
        bed.extend(b);
        rails.extend(r);
    }
    pre.extend(bed);
    (pre, rails)
}

/// The chains of parts (chain_parts) with every turnout's branch refitted to leave its through track tangent
/// (switches::refit), and the switches found: what the strips, the 3D track and the physics all draw from, so they agree.
pub fn chains_refit(parts: &[(Vec<P2>, f64)], refit: bool) -> (Vec<(Vec<P2>, f64)>, Vec<switches::Switch>) {
    let mut chains = chain_parts(parts);
    let lines: Vec<Vec<P2>> = chains.iter().map(|c| c.0.clone()).collect();
    let sws = switches::find(&lines);
    if refit {
        for sw in &sws {
            if sw.branch != sw.through {
                chains[sw.branch].0 = switches::refit(&lines[sw.branch], sw.end, &lines[sw.through]);
            }
        }
    }
    (chains, sws)
}

/// What a 3D branch run needs of its turnout: the arc (m from the heel) before which it has no sleepers of its own
/// (the through track's sleepers reach across to it), the arc from which its rails have parted from the stock rails, and the
/// frog (its inner rail crossing the stock rail on its side).
pub struct BranchInfo {
    pub zone: f64,
    pub div: f64,
    pub frog: Option<P2>,
    pub side: f64,
}

/// What a 3D through run needs of a turnout on it: the arcs (on this run) the switch lies over, the frog and its arc, and the
/// branch's line from the heel (the through sleepers reach across to it, the check rails and the collider's gaps follow it).
pub struct Mark {
    pub s0: f64,
    pub s1: f64,
    pub frog: Option<P2>,
    pub frog_s: f64,
    pub branch_line: Vec<P2>,
}

/// A run of the 3D track: its points, its gauge and whether each end meets something flush (ramp: a paved strip, a bridge's
/// or a line's free end; not a switch's node, where the track goes on).
pub struct OpenRun {
    pub line: Vec<P2>,
    pub gauge: f64,
    pub ramp_start: bool,
    pub ramp_end: bool,
    pub branch: Option<BranchInfo>,
    pub marks: Vec<Mark>,
}

/// The unpaved runs of the chains of parts [(line, gauge)] (chain_parts) that a tile's 3D track draws (not a street's, not a
/// green track's: Style::open_is_3d): each run in the chain's own order, found on the whole chain (so every tile finds
/// the same runs, the decals' street runs the same) at 1 m, those within `near` (x0, y0, x1, y1) only. Empty where the style
/// draws open track as decals.
pub fn open_runs(parts: &[(Vec<P2>, f64)], near: [f64; 4], all: &Segs, eligible: &Segs, st: &Style) -> Vec<OpenRun> {
    let mut out = Vec::new();
    if !st.open_is_3d() {
        return out;
    }
    let (chains, sws) = chains_refit(parts, st.switch3d);
    let lines: Vec<Vec<P2>> = chains.iter().map(|c| c.0.clone()).collect();
    let free = switches::free_ends(&lines);
    let mut found: Vec<(usize, OpenRun)> = Vec::new();
    for (ci, (line, g)) in chains.iter().enumerate() {
        let full = densify(line, SAMPLE_M);
        if full.len() < 2 {
            continue;
        }
        let street = street_flags(&full, all, eligible);
        let all_runs = runs(&street);
        let nruns = all_runs.len();
        for (ri, (i, j, paved)) in all_runs.into_iter().enumerate() {
            if paved || j <= i {
                continue;
            }
            let run = &full[i..=j];
            let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
            for p in run {
                x0 = x0.min(p[0]);
                x1 = x1.max(p[0]);
                y0 = y0.min(p[1]);
                y1 = y1.max(p[1]);
            }
            if x1 >= near[0] && x0 <= near[2] && y1 >= near[1] && y0 <= near[3] {
                found.push((ci, OpenRun {
                    line: run.to_vec(),
                    gauge: *g,
                    ramp_start: ri > 0 || free.contains(&(ci, 0)),
                    ramp_end: ri + 1 < nruns || free.contains(&(ci, 1)),
                    branch: None,
                    marks: Vec::new(),
                }));
            }
        }
    }
    // the turnouts (BNE_TRAM_SWITCH3D): each branch run is oriented from its heel and told its zone, its divergence and its
    // frog; the through run it leaves is told where the switch lies on it
    for sw in &sws {
        if !st.switch3d || sw.branch == sw.through {
            continue;
        }
        let Some(bi) = found.iter().position(|(ci, r)| {
            *ci == sw.branch && {
                let heel = if sw.end == 0 { lines[sw.branch][0] } else { lines[sw.branch][lines[sw.branch].len() - 1] };
                let (a, b) = (r.line[0], r.line[r.line.len() - 1]);
                let d = if sw.end == 0 { a } else { b };
                (d[0] - heel[0]).dhypot(d[1] - heel[1]) < 0.6
            }
        }) else { continue };
        let g = found[bi].1.gauge;
        if sw.end == 1 {
            let r = &mut found[bi].1;
            r.line.reverse();
            std::mem::swap(&mut r.ramp_start, &mut r.ramp_end);
        }
        let branch_line = found[bi].1.line.clone();
        let heel = branch_line[0];
        let through_line = &lines[sw.through];
        let t = switches::turnout(&branch_line, through_line, g, HEAD_M);
        found[bi].1.ramp_start = false;       // (its start is the switch: the track goes on there)
        found[bi].1.branch = Some(BranchInfo { zone: t.zone_len, div: t.div_len, frog: t.frog.map(|f| f.0), side: t.side });
        // the through run that holds the heel
        let ti = found.iter().position(|(ci, r)| *ci == sw.through && switches::near_line(heel, &r.line).0 < 0.6);
        if let Some(ti) = ti {
            let tl = found[ti].1.line.clone();
            let ta = {
                let mut a = vec![0.0f64; tl.len()];
                for i in 1..tl.len() {
                    a[i] = a[i - 1] + (tl[i][0] - tl[i - 1][0]).dhypot(tl[i][1] - tl[i - 1][1]);
                }
                a
            };
            let arc_of = |p: P2| -> f64 {
                let mut best = (f64::INFINITY, 0.0);
                for i in 0..tl.len() - 1 {
                    let (a, b) = (tl[i], tl[i + 1]);
                    let d = [b[0] - a[0], b[1] - a[1]];
                    let l2 = (d[0] * d[0] + d[1] * d[1]).max(1e-12);
                    let u = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
                    let dist = (p[0] - (a[0] + u * d[0])).dhypot(p[1] - (a[1] + u * d[1]));
                    if dist < best.0 {
                        best = (dist, ta[i] + u * (ta[i + 1] - ta[i]));
                    }
                }
                best.1
            };
            let bl = densify(&branch_line, 1.0);
            let ba: Vec<f64> = {
                let mut a = vec![0.0f64; bl.len()];
                for i in 1..bl.len() {
                    a[i] = a[i - 1] + (bl[i][0] - bl[i - 1][0]).dhypot(bl[i][1] - bl[i - 1][1]);
                }
                a
            };
            let zone_pt = bl.iter().zip(&ba).find(|(_, &s)| s >= t.zone_len).map(|(p, _)| *p).unwrap_or(bl[bl.len() - 1]);
            let (s_heel, s_zone) = (arc_of(heel), arc_of(zone_pt));
            let (s0, s1) = (s_heel.min(s_zone) - 1.0, s_heel.max(s_zone) + 1.0);
            let keep: Vec<P2> = bl.iter().zip(&ba).filter(|(_, &s)| s <= t.zone_len + 6.0).map(|(p, _)| *p).collect();
            found[ti].1.marks.push(Mark { s0, s1, frog: t.frog.map(|f| f.0), frog_s: t.frog.map_or(s_heel, |f| arc_of(f.0)),
                                        branch_line: keep });
        }
    }
    found.into_iter().map(|(_, r)| r).collect()
}

fn smooth(t: f64) -> f64 {
    let t = t.clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

/// The heights of an open run's track at arcs s (m from its start) over the ground there zg and the ground smoothed along
/// the run zs: (the ballast top, the rail head's top, the sleepers' top) per point. The track lies on the ground: the ballast
/// a plain strip OPEN_BALLAST_H_M proud of it, the sleepers half sunk (their tops OPEN_SLEEPER_ABOVE_M over the ballast), the
/// rail heads OPEN_RAIL_ABOVE_M over it. Where an end meets something flush (ramp_start / ramp_end) the ballast comes
/// down to the ground (OPEN_FLUSH_M over it) over OPEN_RAMP_M, the sleepers sink into it and the rail head comes down to the
/// ballast over OPEN_RAIL_RAMP_M: the same height as the strip at the junction, no bar of rail where its flat rails go on.
pub fn open_heights(s: &[f64], zg: &[f64], zs: &[f64], ramp_start: bool, ramp_end: bool) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let n = s.len();
    let total = if n > 0 { s[n - 1] } else { 0.0 };
    let (mut top, mut rail, mut sleeper) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
    for i in 0..n {
        let d = f64::min(if ramp_start { s[i] } else { f64::INFINITY }, if ramp_end { total - s[i] } else { f64::INFINITY });
        let (rb, rr) = (smooth(d / OPEN_RAMP_M), smooth(d / OPEN_RAIL_RAMP_M));
        let t = (1.0 - rb) * (zg[i] + OPEN_FLUSH_M) + rb * (zs[i] + OPEN_BALLAST_H_M);
        top.push(t);
        rail.push(t + OPEN_RAIL_ABOVE_M * rr);
        sleeper.push(t - 0.01 + (OPEN_SLEEPER_ABOVE_M + 0.01) * rb);
    }
    (top, rail, sleeper)
}

fn pts2(a: &PyReadonlyArray2<f64>) -> PyResult<Vec<P2>> {
    let v = a.as_array();
    if v.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("tram: points need two columns"));
    }
    Ok((0..v.nrows()).map(|i| [v[[i, 0]], v[[i, 1]]]).collect())
}

fn segs_of(a: &PyReadonlyArray2<f64>, b: &PyReadonlyArray2<f64>, hw: &PyReadonlyArray1<f64>) -> PyResult<Segs> {
    let (pa, pb) = (pts2(a)?, pts2(b)?);
    let h = hw.as_array();
    if pa.len() != pb.len() || pa.len() != h.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram: segment tables differ in length"));
    }
    Ok(Segs::new(&pa, &pb, &h.to_vec()))
}

fn look_dict<'py>(py: Python<'py>, l: &Look) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("m", l.m)?;
    d.set_item("tl", l.tl)?;
    d.set_item("p", l.p)?;
    d.set_item("layer", l.layer)?;
    Ok(d)
}

/// The constants the Python glue and its tests read.
#[pyfunction]
pub fn tram_track_consts<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("STREET_MARGIN_M", STREET_MARGIN_M)?;
    d.set_item("GAUGE_DEFAULT_M", GAUGE_DEFAULT_M)?;
    d.set_item("GAUGE_MIN_M", GAUGE_MIN_M)?;
    d.set_item("GAUGE_MAX_M", GAUGE_MAX_M)?;
    d.set_item("DECAL_W_M", DECAL_W_M)?;
    d.set_item("PAVING_EXTRA_M", PAVING_EXTRA_M)?;
    d.set_item("RAIL_W_M", RAIL_W_M)?;
    d.set_item("RAIL", look_dict(py, &RAIL)?)?;
    d.set_item("ASPHALT", look_dict(py, &ASPHALT)?)?;
    d.set_item("SLAB", look_dict(py, &SLAB)?)?;
    d.set_item("TAG_MINOR", TAG_MINOR)?;
    d.set_item("TAG_COBBLE", TAG_COBBLE)?;
    d.set_item("OPEN_BALLAST_H_M", OPEN_BALLAST_H_M)?;
    d.set_item("OPEN_RAMP_M", OPEN_RAMP_M)?;
    d.set_item("OPEN_RAIL_RAMP_M", OPEN_RAIL_RAMP_M)?;
    d.set_item("OPEN_RAIL_ABOVE_M", OPEN_RAIL_ABOVE_M)?;
    for (k, l) in [("STREET_ASPHALT", &STREET_ASPHALT), ("STREET_SETTS", &STREET_SETTS), ("STREET_SLAB", &STREET_SLAB),
                   ("STREET_GRASS", &STREET_GRASS), ("BALLAST_TEX", &BALLAST_TEX)] {
        d.set_item(k, look_dict(py, l)?)?;
    }
    Ok(d)
}

/// The gauge in m: osm (railways.osm_gauge's, None or NaN: untagged) inside GAUGE_MIN_M..GAUGE_MAX_M, else the default.
#[pyfunction]
pub fn tram_gauge(osm: Option<f64>) -> f64 {
    gauge(osm)
}

/// What a track in the street lies in (su: the ex-USSR's concrete slabs).
#[pyfunction]
pub fn tram_paving<'py>(py: Python<'py>, su: bool) -> PyResult<Bound<'py, PyDict>> {
    look_dict(py, &paving(su))
}

#[pyfunction]
pub fn tram_paving_width(g: f64) -> f64 {
    paving_width(g)
}

/// Per point of pts (k, 2): in the street, given the carriageways' segments a -> b with half widths hw.
#[pyfunction]
pub fn tram_in_street<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray2<'py, f64>,
                           b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>) -> PyResult<Vec<bool>> {
    let (p, ways) = (pts2(&pts)?, segs_of(&a, &b, &hw)?);
    Ok(py.detach(|| in_street(&p, &ways)))
}

/// A tile's ground tram parts' chunks (see ground_chunks_many): the parts end to end (xy (n, 2), part_off (p + 1,), gauges
/// (p,)) in road tile `bx`; (the bed and paving chunks, the rails) as the road mesh's chunk dicts.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tram_ground_chunks<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, part_off: PyReadonlyArray1<'py, i64>,
                               gauges: PyReadonlyArray1<'py, f64>, bx: [f64; 4], a: PyReadonlyArray2<'py, f64>,
                               b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>,
                               ea: PyReadonlyArray2<'py, f64>, eb: PyReadonlyArray2<'py, f64>,
                               ehw: PyReadonlyArray1<'py, f64>, tags: PyReadonlyArray1<'py, u8>, bw: f64, su: bool,
                               country: &str, tex: bool, open3d: bool, switch3d: bool)
                               -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
    let p = pts2(&xy)?;
    let (off, g) = (part_off.as_array().to_vec(), gauges.as_array().to_vec());
    if off.is_empty() || off.len() != g.len() + 1 || off[off.len() - 1] as usize != p.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_ground_chunks: parts and offsets differ"));
    }
    let parts: Vec<(Vec<P2>, f64)> = (0..g.len()).map(|k| (p[off[k] as usize..off[k + 1] as usize].to_vec(), g[k])).collect();
    let (all, eligible) = (segs_of(&a, &b, &hw)?.with_tags(&tags.as_array().to_vec()), segs_of(&ea, &eb, &ehw)?);
    let st = Style { su, country: country.to_uppercase(), tex, open3d, switch3d };
    let (bed, rails) = py.detach(|| ground_chunks_many(&parts, bx, &all, &eligible, bw, &st));
    let to_list = |v: &[Chunk]| -> PyResult<Bound<'py, PyList>> {
        let l = PyList::empty(py);
        for c in v {
            l.append(chunk_dict(py, c)?)?;
        }
        Ok(l)
    };
    Ok((to_list(&bed)?, to_list(&rails)?))
}

/// The 3D track's runs of a tile's ground tram parts (see open_runs): ([(run (k, 2), gauge, ramp_start, ramp_end)]).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tram_open_runs<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, part_off: PyReadonlyArray1<'py, i64>,
                           gauges: PyReadonlyArray1<'py, f64>, near: [f64; 4], a: PyReadonlyArray2<'py, f64>,
                           b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>, ea: PyReadonlyArray2<'py, f64>,
                           eb: PyReadonlyArray2<'py, f64>, ehw: PyReadonlyArray1<'py, f64>, su: bool, country: &str,
                           tex: bool, open3d: bool, switch3d: bool)
                           -> PyResult<Vec<(Bound<'py, numpy::PyArray2<f64>>, f64, bool, bool, Bound<'py, PyDict>)>> {
    use numpy::IntoPyArray;
    let p = pts2(&xy)?;
    let (off, g) = (part_off.as_array().to_vec(), gauges.as_array().to_vec());
    if off.is_empty() || off.len() != g.len() + 1 || off[off.len() - 1] as usize != p.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_open_runs: parts and offsets differ"));
    }
    let parts: Vec<(Vec<P2>, f64)> = (0..g.len()).map(|k| (p[off[k] as usize..off[k + 1] as usize].to_vec(), g[k])).collect();
    let (all, eligible) = (segs_of(&a, &b, &hw)?, segs_of(&ea, &eb, &ehw)?);
    let st = Style { su, country: country.to_uppercase(), tex, open3d, switch3d };
    let out = py.detach(|| open_runs(&parts, near, &all, &eligible, &st));
    Ok(out
        .into_iter()
        .map(|r| {
            let flat: Vec<f64> = r.line.iter().flat_map(|q| [q[0], q[1]]).collect();
            let info = PyDict::new(py);
            if let Some(b) = &r.branch {
                let d = PyDict::new(py);
                let _ = d.set_item("zone", b.zone);
                let _ = d.set_item("div", b.div);
                let _ = d.set_item("frog", b.frog.map(|f| (f[0], f[1])));
                let _ = d.set_item("side", b.side);
                let _ = info.set_item("branch", d);
            }
            let marks = PyList::empty(py);
            for m in &r.marks {
                let d = PyDict::new(py);
                let _ = d.set_item("s0", m.s0);
                let _ = d.set_item("s1", m.s1);
                let _ = d.set_item("frog", m.frog.map(|f| (f[0], f[1])));
                let _ = d.set_item("frog_s", m.frog_s);
                let bf: Vec<f64> = m.branch_line.iter().flat_map(|q| [q[0], q[1]]).collect();
                let _ = d.set_item("branch", numpy::ndarray::Array2::from_shape_vec((m.branch_line.len(), 2), bf).unwrap().into_pyarray(py));
                let _ = marks.append(d);
            }
            let _ = info.set_item("marks", marks);
            (numpy::ndarray::Array2::from_shape_vec((r.line.len(), 2), flat).unwrap().into_pyarray(py), r.gauge,
             r.ramp_start, r.ramp_end, info)
        })
        .collect())
}

/// How far each sleeper (centres (n, 2), unit long axes (n, 2): across the track) must reach to a branch's axis: the signed
/// distance along its axis to the nearest crossing within `reach`, 0 where none (switches::ray_reach).
#[pyfunction]
pub fn tram_sleeper_reach<'py>(py: Python<'py>, centres: PyReadonlyArray2<'py, f64>, axes: PyReadonlyArray2<'py, f64>,
                               branch: PyReadonlyArray2<'py, f64>, reach: f64) -> PyResult<Bound<'py, numpy::PyArray1<f64>>> {
    use numpy::IntoPyArray;
    let (c, a, b) = (pts2(&centres)?, pts2(&axes)?, pts2(&branch)?);
    if c.len() != a.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_sleeper_reach: a centre and an axis each"));
    }
    let out: Vec<f64> = py.detach(|| c.iter().zip(&a).map(|(p, d)| switches::ray_reach(*p, *d, &b, reach)).collect());
    Ok(numpy::ndarray::Array1::from(out).into_pyarray(py))
}

/// An open run's ballast tops, rail heads' tops and sleepers' tops (see open_heights) at arcs s over the ground zg and its
/// smoothing zs, and the arcs from which and to which sleepers may lie: (top (n,), rail (n,), sleeper (n,), first, last).
#[pyfunction]
pub fn tram_open_heights<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, zg: PyReadonlyArray1<'py, f64>,
                              zs: PyReadonlyArray1<'py, f64>, ramp_start: bool, ramp_end: bool)
                              -> PyResult<(Bound<'py, numpy::PyArray1<f64>>, Bound<'py, numpy::PyArray1<f64>>,
                                           Bound<'py, numpy::PyArray1<f64>>, f64, f64)> {
    use numpy::IntoPyArray;
    let (sv, g, z) = (s.as_array().to_vec(), zg.as_array().to_vec(), zs.as_array().to_vec());
    if sv.is_empty() || g.len() != sv.len() || z.len() != sv.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_open_heights: arrays differ"));
    }
    let (top, rail, sleeper) = open_heights(&sv, &g, &z, ramp_start, ramp_end);
    let total = sv[sv.len() - 1];
    Ok((numpy::ndarray::Array1::from(top).into_pyarray(py), numpy::ndarray::Array1::from(rail).into_pyarray(py),
        numpy::ndarray::Array1::from(sleeper).into_pyarray(py),
        if ramp_start { OPEN_SLEEPER_FREE_M } else { 0.0 }, if ramp_end { total - OPEN_SLEEPER_FREE_M } else { total }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn old() -> Style {
        Style { su: false, country: String::new(), tex: false, open3d: false, switch3d: false }
    }

    fn new_tex(country: &str, su: bool) -> Style {
        Style { su, country: country.into(), tex: true, open3d: false, switch3d: false }
    }

    #[test]
    fn gauge_inside_bounds_else_default() {
        assert_eq!(gauge(Some(1.0)), 1.0);
        assert_eq!(gauge(Some(1.524)), 1.524);
        for bad in [None, Some(0.2), Some(3.0), Some(f64::NAN)] {
            assert_eq!(gauge(bad), GAUGE_DEFAULT_M);
        }
    }

    #[test]
    fn runs_join_short_stretches_into_the_one_before() {
        let on = [true, true, true, false, true, true, true, true];
        // the single `false` is shorter than 2 points: it joins the first run (rails._runs: which does not merge the
        // two runs of one kind it leaves, they just share a point)
        assert_eq!(runs(&on), vec![(0, 3, true), (3, 7, true)]);
        let on = [false, false, false, true, true, true, true];
        assert_eq!(runs(&on), vec![(0, 2, false), (2, 6, true)], "each run starts where the one before ends");
        assert!(runs(&[]).is_empty());
    }

    #[test]
    fn python_round_is_half_to_even_on_the_exact_value() {
        assert_eq!(py_round(0.0625, 3), 0.062);
        assert_eq!(py_round(2.675, 2), 2.67); // 2.675 is 2.67499999...
        assert_eq!(py_round(1.0005, 3), 1.0); // 1.000499999999999989...
        assert_eq!(py_round(-0.0004, 3).to_bits(), (-0.0f64).to_bits());
    }

    #[test]
    fn ways_joined_end_to_end_run_as_one_line_and_a_switch_does_not() {
        let a: Vec<P2> = vec![[0.0, 0.0], [10.0, 0.0]];
        let b: Vec<P2> = vec![[20.0, 5.0], [10.0, 0.0]]; // (stored the other way round)
        let c: Vec<P2> = vec![[20.0, 5.0], [30.0, 5.0]];
        let chains = chain_parts(&[(a.clone(), 1.435), (b, 1.435), (c, 1.435)]);
        assert_eq!(chains.len(), 1, "three ways end to end: one chain");
        assert_eq!(chains[0].0.len(), 4);
        assert_eq!(chains[0].0[0], [0.0, 0.0]);
        assert_eq!(chains[0].0[3], [30.0, 5.0]);
        // a third way ending at the joint is a switch: nothing is chained through it
        let d: Vec<P2> = vec![[10.0, 0.0], [20.0, -5.0]];
        let b2: Vec<P2> = vec![[10.0, 0.0], [20.0, 5.0]];
        assert_eq!(chain_parts(&[(a.clone(), 1.435), (b2.clone(), 1.435), (d, 1.435)]).len(), 3);
        // another gauge is not joined
        assert_eq!(chain_parts(&[(a, 1.435), (b2, 1.0)]).len(), 2);
    }

    #[test]
    fn the_rails_run_on_through_a_joint() {
        let ways = Segs::default();
        let a: Vec<P2> = (0..=50).map(|i| [i as f64, 0.0]).collect();
        let b: Vec<P2> = (0..=50).map(|i| [50.0 + i as f64 * 0.8, i as f64 * 0.6]).collect(); // a bend at the joint
        let (_bed, rails) = ground_chunks_many(&[(a, 1.435), (b, 1.435)], [0.0, 0.0, 256.0, 256.0], &ways, &ways, 2.6, &old());
        assert_eq!(rails.len(), 2, "two rails of one chain, not four");
    }

    #[test]
    fn a_track_beside_a_street_is_paved_and_a_far_one_has_a_bed() {
        // a street along y = 0, half width 4 m; track A at y = 6 (inside the 3 m margin), track B at y = 60
        let (a, b) = ([[-50.0, 0.0]], [[150.0, 0.0]]);
        let ways = Segs::new(&a, &b, &[4.0]);
        let none = Segs::default();
        let near: Vec<P2> = (-20..=100).map(|x| [x as f64, 6.0]).collect();
        let (bed, rails) = ground_chunks(&near, [0.0, 0.0, 128.0, 128.0], 1.0, &ways, &none, 2.6, &old(), (false, false));
        assert_eq!(bed.len(), 1);
        assert_eq!(bed[0].look.m, ASPHALT.m);
        assert_eq!(bed[0].w, 2.0);
        assert_eq!(rails.len(), 2);
        let far: Vec<P2> = (-20..=100).map(|x| [x as f64, 60.0]).collect();
        let (bed, rails) = ground_chunks(&far, [0.0, 0.0, 128.0, 128.0], 1.0, &ways, &none, 2.6, &old(), (false, false));
        assert_eq!(bed[0].look.m, BALLAST.m);
        assert_eq!(rails.len(), 2);
        let (yl, yr) = (rails[0].pts[10][1], rails[1].pts[10][1]);
        assert!((yl - yr - (1.0 + HEAD_M)).abs() < 1e-9, "rails a gauge and a head apart");
        let (bed, _r) = ground_chunks(&near, [0.0, 0.0, 128.0, 128.0], 1.0, &ways, &none, 2.6, &Style { su: true, country: String::new(), tex: false, open3d: false, switch3d: false }, (false, false));
        assert_eq!(bed[0].look.m, SLAB.m);
    }

    #[test]
    fn the_new_strips_have_their_rails_baked_in_and_the_rule_picks_by_country_and_street() {
        let (a, b) = ([[-50.0, 0.0]], [[150.0, 0.0]]);
        let minor = Segs::new(&a, &b, &[4.0]).with_tags(&[TAG_MINOR]);
        let major = Segs::new(&a, &b, &[4.0]);
        let none = Segs::default();
        let near: Vec<P2> = (-20..=100).map(|x| [x as f64, 6.0]).collect();
        let far: Vec<P2> = (-20..=100).map(|x| [x as f64, 60.0]).collect();
        let bx = [0.0, 0.0, 128.0, 128.0];
        let street = |ways: &Segs, st: &Style| ground_chunks(&near, bx, 1.435, ways, &none, 2.6, st, (false, false));
        let (bed, rails) = street(&minor, &new_tex("CZ", false));
        assert_eq!(bed[0].look.m, STREET_SETTS.m, "a minor street in Czechia: setts");
        assert!(rails.is_empty(), "the rails are in the picture");
        assert!((bed[0].tl - bed[0].w).abs() < 1e-9, "the square image's length is the strip's width");
        assert_eq!(street(&major, &new_tex("CZ", false)).0[0].look.m, STREET_ASPHALT.m, "a main street: asphalt");
        assert_eq!(street(&minor, &new_tex("FR", false)).0[0].look.m, STREET_ASPHALT.m, "not a setts country");
        let cobble = Segs::new(&a, &b, &[4.0]).with_tags(&[TAG_COBBLE]);
        assert_eq!(street(&cobble, &new_tex("FR", false)).0[0].look.m, STREET_SETTS.m, "a street the map says is cobbled: setts anywhere");
        assert_eq!(street(&minor, &new_tex("UA", true)).0[0].look.m, STREET_SLAB.m, "the ex-USSR: slabs");
        let open = |st: &Style| ground_chunks(&far, bx, 1.435, &major, &none, 2.6, st, (false, false));
        assert_eq!(open(&new_tex("DE", false)).0[0].look.m, STREET_GRASS.m, "open track in Germany: a green track");
        let (bed, rails) = open(&new_tex("US", false));
        assert_eq!(bed[0].look.m, BALLAST_TEX.m);
        assert_eq!(rails.len(), 2, "ballast has its rails drawn");
        let (bed, rails) = street(&minor, &old());
        assert_eq!(bed[0].look.m, ASPHALT.m);
        assert_eq!(rails.len(), 2, "the old materials: separate rails");
    }

    #[test]
    fn a_switch_has_one_strip_over_a_plain_paving_with_the_branchs_own_rails() {
        // a street along y = 0 (half width 4) with the through tram at y = 2 in two ways and a branch leaving at 10 degrees
        let (a, b) = ([[-50.0, 0.0]], [[250.0, 0.0]]);
        let street = Segs::new(&a, &b, &[4.0]);
        let none = Segs::default();
        let line = |x0: f64, x1: f64| -> Vec<P2> { (0..=((x1 - x0) as usize)).map(|i| [x0 + i as f64, 2.0]).collect() };
        let br: Vec<P2> = (0..=60).map(|i| [100.0 + i as f64 * 0.985, 2.0 + i as f64 * 0.174]).collect();
        let parts = vec![(line(20.0, 100.0), 1.435), (line(100.0, 180.0), 1.435), (br, 1.435)];
        let st = new_tex("DE", false);
        let (bed, rails) = ground_chunks_many(&parts, [0.0, 0.0, 256.0, 256.0], &street, &none, 2.6, &st);
        let plain = bed.iter().filter(|c| c.look.m == ASPHALT.m).count();
        assert!(plain >= 1, "the branch's start is plain paving");
        assert!(bed.iter().position(|c| c.look.m == ASPHALT.m) < bed.iter().position(|c| c.look.m == STREET_ASPHALT.m),
                "the plain paving is drawn first: the through strip lies over it");
        assert!(rails.iter().any(|c| c.look.m == RAIL.m && c.w == PLATE_W_M), "a plate on the frog");
        assert!(rails.iter().filter(|c| c.look.m == RAIL.m && c.w == RAIL_W_M).count() >= 2, "the branch's two rails");
        // the strips stop where they end: a border across each free end (the through track's two ends)
        assert!(rails.iter().filter(|c| c.w == BORDER_W_M && c.look.m != BORDER.m).count() >= 2);
    }

    #[test]
    fn a_strip_meeting_open_track_gets_a_thin_border() {
        let (a, b) = ([[-50.0, 0.0]], [[40.0, 0.0]]);
        let street = Segs::new(&a, &b, &[4.0]); // the street ends at x = 40: the tram leaves it there
        let none = Segs::default();
        let line: Vec<P2> = (0..=100).map(|i| [i as f64, 2.0]).collect();
        let (_bed, rails) = ground_chunks(&line, [0.0, 0.0, 128.0, 128.0], 1.435, &street, &none, 2.6, &new_tex("US", false),
                                          (false, false));
        let borders: Vec<&Chunk> = rails.iter().filter(|c| c.w == BORDER_W_M).collect();
        assert_eq!(borders.len(), 1, "one border where the strip meets the open track");
        assert!(borders[0].pts[0][0] > 35.0 && borders[0].pts[0][0] < 50.0);
        let across = (borders[0].pts[1][1] - borders[0].pts[0][1]).abs();
        assert!((across - strip_width(&STREET_ASPHALT, 1.435)).abs() < 0.2, "across the whole strip: {across}");
    }

    #[test]
    fn open_track_in_3d_is_left_out_of_the_decals_and_found_as_runs_except_a_green_track() {
        let (a, b) = ([[-50.0, 0.0]], [[40.0, 0.0]]);
        let street = Segs::new(&a, &b, &[4.0]);
        let none = Segs::default();
        let line: Vec<P2> = (0..=100).map(|i| [i as f64, 2.0]).collect();
        let st = |country: &str| Style { su: false, country: country.into(), tex: true, open3d: true, switch3d: false };
        let bx = [0.0, 0.0, 128.0, 128.0];
        let (bed, rails) = ground_chunks(&line, bx, 1.435, &street, &none, 2.6, &st("US"), (false, false));
        assert!(bed.iter().all(|c| c.look.m != BALLAST_TEX.m && c.look.m != BALLAST.m), "no ballast decal: {}", bed.len());
        assert!(rails.iter().all(|c| c.look.m != RAIL.m || c.w != RAIL_W_M), "no open rails");
        let runs3 = open_runs(&[(line.clone(), 1.435)], bx, &street, &none, &st("US"));
        assert_eq!(runs3.len(), 1);
        assert!(runs3[0].ramp_start && runs3[0].ramp_end, "ramps at the strip and at the line's free end");
        assert!(runs3[0].line[0][0] > 35.0 && runs3[0].line[0][0] < 50.0, "starts where the street run ends: {:?}", runs3[0].line[0]);
        assert!(open_runs(&[(line.clone(), 1.435)], bx, &street, &none, &st("DE")).is_empty(), "a green track stays a decal");
        let (bed, _r) = ground_chunks(&line, bx, 1.435, &street, &none, 2.6, &st("DE"), (false, false));
        assert!(bed.iter().any(|c| c.look.m == STREET_GRASS.m));
    }

    #[test]
    fn the_3d_track_comes_down_to_the_strips_height_at_a_junction_with_one_gauge() {
        // an open run 40 m long meeting a strip at its start: ground 5.0, smoothed 5.02
        let s: Vec<f64> = (0..=40).map(|i| i as f64).collect();
        let (zg, zs) = (vec![5.0; 41], vec![5.02; 41]);
        let (top, rail, sleeper) = open_heights(&s, &zg, &zs, true, false);
        assert!(top[0] - 5.0 <= 0.01 + 1e-9, "the ballast is flush at the junction: {}", top[0] - 5.0);
        assert!(rail[0] - top[0] < 1e-9, "the rail head is at the ballast: {}", rail[0] - top[0]);
        assert!(sleeper[0] <= top[0], "the sleepers are sunk in the ballast at the junction");
        assert!((top[40] - (5.02 + OPEN_BALLAST_H_M)).abs() < 1e-9, "on the ground away from it, a plain strip");
        assert!(top[40] - 5.02 <= 0.03, "no embankment");
        assert!((rail[40] - top[40] - OPEN_RAIL_ABOVE_M).abs() < 1e-9);
        assert!((sleeper[40] - top[40] - OPEN_SLEEPER_ABOVE_M).abs() < 1e-9, "sleepers half sunk");
        assert!(rail.iter().zip(&sleeper).all(|(r, s)| r - s > 0.0), "a rail is always over its sleeper"); 
        // no step over 8 cm between neighbouring metres in the ramp (the ballast, the rails)
        assert!(top.windows(2).all(|w| (w[1] - w[0]).abs() < 0.08) && rail.windows(2).all(|w| (w[1] - w[0]).abs() < 0.08));
        // the rails of the strip are a gauge and a head apart, as the 3D rails are
        for look in [&STREET_ASPHALT, &STREET_SETTS, &STREET_SLAB, &STREET_GRASS] {
            for g in [1.0, 1.435] {
                let apart = strip_width(look, g) * strip_span(look);
                assert!((apart - (g + HEAD_M)).abs() < 0.002, "{} {g}: {apart}", look.m);
            }
        }
    }

    #[test]
    fn one_chain_has_one_paving_whatever_its_parts_are() {
        // a street along y = 0 whose first half is a cobbled street and second half a main one; a track along it in two ways
        let a: Vec<P2> = (0..=10).map(|i| [-50.0 + i as f64 * 10.0, 0.0]).collect();
        let b: Vec<P2> = (0..=10).map(|i| [50.0 + i as f64 * 10.0, 0.0]).collect();
        let (pa, pb): (Vec<P2>, Vec<P2>) = (a.windows(2).map(|w| w[0]).collect(), b.windows(2).map(|w| w[0]).collect());
        let (ea, eb): (Vec<P2>, Vec<P2>) = (a.windows(2).map(|w| w[1]).collect(), b.windows(2).map(|w| w[1]).collect());
        let mut starts = pa.clone();
        starts.extend(pb.clone());
        let mut ends = ea.clone();
        ends.extend(eb.clone());
        let mut tags = vec![TAG_COBBLE; pa.len()];
        tags.extend(vec![0u8; pb.len()]);
        let street = Segs::new(&starts, &ends, &vec![4.0; starts.len()]).with_tags(&tags);
        let none = Segs::default();
        let way: Vec<P2> = (-50..=150).map(|i| [i as f64, 3.0]).collect();
        let (bed, _r) = ground_chunks(&way, [0.0, -50.0, 128.0, 50.0], 1.435, &street, &none, 2.6, &new_tex("FR", false), (false, false));
        let mats: std::collections::HashSet<&str> = bed.iter().map(|c| c.look.m).collect();
        assert_eq!(mats.len(), 1, "one material along the whole track: {mats:?}");
    }
}
