//! The shadow product (BNE_ROADSYS=shadow, M1): what a geotile's road surface needs, built from its declared read
//! list, never from a tile window (PT3 change 1).
//!
//! `reads` takes the segments read so far and the tile's quad and returns the read list the tile's pieces need:
//! every stroke whose faces reach the tile (D, strokes within NEAR_M of it), the arms of their junction clusters (two
//! rings: the zones' rows and the clusters' merges), their snap trunks, the parallel carriageways that cap them
//! (caps), the members of every vertical component (prof::plan) touching them and those components' crossing
//! partners; each as its whole stroke plus HALO_M round it (the lane graph's walks: 900 m reach + 20 m). The caller
//! reads those cells (and every copy of their segments) and asks again until nothing is missing: the read list is a
//! function of the map data alone (no visit order, no process history), and so is everything built from it.
//!
//! `build` builds the tile from a complete read: the network, the layout, the profiles of the strokes it needs, the
//! surface and markings of D, clipped to the quad (tile::tile_quad: canonical bytes in piece order), the sheet's
//! checks inside the quad, and the keys: per delivered piece the digest of its own read list (the segments in its
//! own dependency boxes, PIECE_FORMAT, the frame) and of its profile (prof's problem keys); per zone the digest of
//! its arms' keys.
use crate::check;
use crate::geom::*;
use crate::io::{Place, Seg};
use crate::mark;
use crate::net::{self, Net, Opts};
use crate::part;
use crate::prof;
use crate::tile;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

pub const PIECE_FORMAT: u64 = 2;       // 2 (M2): the profiles of QP_FORMAT 2, whole components read
pub const HALO_M: f64 = 920.0;
pub const NEAR_M: f64 = 60.0;
pub const PROF_HALO_M: f64 = 30.0;
/// m round a tile its strokes' knots take the profile squares' heights (the rows it meshes; farther knots its own)
pub const FIELD_REACH_M: f64 = 300.0;

/// FNV-1a 64 of a segment's whole record (its identity in the read lists' keys).
pub fn seg_digest(s: &Seg) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    let mut b = |x: &[u8]| { for &c in x { h ^= c as u64; h = h.wrapping_mul(0x100000001b3); } };
    for t in [&s.fid, &s.code, &s.cls_s, &s.turn, &s.turn_bw, &s.place] { b(&(t.len() as u64).to_le_bytes()); b(t.as_bytes()); }
    for v in [s.pave, s.drive, s.kmh, s.lane_w] { b(&v.to_bits().to_le_bytes()); }
    for v in [s.inferred, s.cls, s.dir, s.lanes, s.lfw, s.lbw, s.region, s.urban as i64, s.link as i64] { b(&v.to_le_bytes()); }
    b(&(s.pts.len() as u64).to_le_bytes());
    for p in &s.pts { b(&p[0].to_bits().to_le_bytes()); b(&p[1].to_bits().to_le_bytes()); }
    b(&(s.cons.len() as u64).to_le_bytes());
    for (c, v) in &s.cons { b(&(c.len() as u64).to_le_bytes()); b(c.as_bytes()); b(&(*v as u64).to_le_bytes()); }
    for r in [&s.bridge, &s.tunnel] { b(&(r.len() as u64).to_le_bytes()); for &(x, y) in r.iter() { b(&x.to_bits().to_le_bytes()); b(&y.to_bits().to_le_bytes()); } }
    b(&(s.level.len() as u64).to_le_bytes());
    for &(x, y, v) in &s.level { b(&x.to_bits().to_le_bytes()); b(&y.to_bits().to_le_bytes()); b(&v.to_bits().to_le_bytes()); }
    // (BNE_ROADSYS_XSEC: its cross-section tags and the model's version; nothing without them, the keys as before)
    if !s.xsec.is_empty() {
        b(b"xsec"); b(&crate::xsec::XSEC_FORMAT.to_le_bytes()); b(&(s.xsec.len() as u64).to_le_bytes()); b(s.xsec.as_bytes());
    }
    h
}

fn bbox_of(p: &[P2]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for q in p { b[0] = b[0].min(q[0]); b[1] = b[1].min(q[1]); b[2] = b[2].max(q[0]); b[3] = b[3].max(q[1]); }
    b
}

fn grow(b: [f64; 4], d: f64) -> [f64; 4] { [b[0] - d, b[1] - d, b[2] + d, b[3] + d] }

fn meets(a: [f64; 4], b: [f64; 4]) -> bool { a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3] }

/// Does the polyline come within the box?
fn line_meets(p: &[P2], b: [f64; 4]) -> bool {
    if !meets(bbox_of(p), b) { return false; }
    for w in p.windows(2) {
        let (a, c) = (w[0], w[1]);
        let inside = |q: P2| q[0] >= b[0] && q[0] <= b[2] && q[1] >= b[1] && q[1] <= b[3];
        if inside(a) || inside(c) { return true; }
        let cs = [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]];
        if (0..4).any(|k| seg_x(a, c, cs[k], cs[(k + 1) % 4]).is_some()) { return true; }
    }
    false
}

/// Does a segment's line enter one of the boxes? (the read list's own test: such a segment lies in a cell the read
/// covers)
pub fn seg_in(s: &Seg, boxes: &[[f64; 4]]) -> bool {
    let sb = bbox_of(&s.pts);
    boxes.iter().any(|b| meets(*b, sb) && line_meets(&s.pts, *b))
}

/// A stroke's read boxes: its line in pieces of 1 km, each piece's box grown by halo.
pub fn stroke_boxes(line: &[P2], halo: f64) -> Vec<[f64; 4]> {
    let mut out = Vec::new();
    let mut piece: Vec<P2> = Vec::new();
    let mut run = 0.0;
    for (k, &p) in line.iter().enumerate() {
        if k > 0 { run += norm(sub(p, line[k - 1])); }
        piece.push(p);
        if run >= 1000.0 && piece.len() >= 2 {
            out.push(grow(bbox_of(&piece), halo));
            piece = vec![p];
            run = 0.0;
        }
    }
    if piece.len() >= 2 || out.is_empty() { out.push(grow(bbox_of(&piece), halo)); }
    out
}

/// The strokes a set of delivered strokes needs: full (rows built), prog (their lane programs read), prof (their
/// profiles solved).
pub struct Deps { pub d: Vec<bool>, pub full: Vec<bool>, pub prog: Vec<bool>, pub prof: Vec<bool> }

pub fn deps(net: &Net, lay: &part::Layout, plan: &prof::Plan, d: &[bool]) -> Deps { deps_f(net, lay, plan, d, false) }

/// deps(); field: the profiles come from the profile squares (square.rs), so no component is read: prof stays empty.
pub fn deps_f(net: &Net, lay: &part::Layout, plan: &prof::Plan, d: &[bool], field: bool) -> Deps {
    let ns = net.strokes.len();
    let mut ends: Vec<Vec<usize>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() { ends[st.n0].push(si); ends[st.n1].push(si); }
    let ring = |set: &[bool]| -> Vec<bool> {
        let mut out = set.to_vec();
        for si in 0..ns {
            if !set[si] { continue; }
            let st = &net.strokes[si];
            for node in [st.n0, st.n1] {
                let c = lay.cluster_of[node];
                let nodes: Vec<usize> = if c == usize::MAX { vec![node] } else { lay.clusters[c].clone() };
                for nd in nodes { for &o in &ends[nd] { out[o] = true; } }
            }
        }
        out
    };
    let snaps = |set: &mut Vec<bool>| {
        for _ in 0..8 {
            let mut more = false;
            for si in 0..ns {
                if !set[si] { continue; }
                for sn in [net.strokes[si].snap0, net.strokes[si].snap1].into_iter().flatten() {
                    if !set[sn.trunk] { set[sn.trunk] = true; more = true; }
                }
            }
            if !more { break; }
        }
    };
    // full: the delivered strokes and the arms of their clusters (their end rows are the zones' rings), then the
    // trunks they snap to (rows at the noses and joints; never ringed: only their own rows are read)
    let mut full = ring(d);
    // a cluster merges with its neighbour only through a stroke too short for both cut-backs (part.rs layout,
    // <= 100 m, three passes): those strokes' far clusters' arms decide the merges and the cuts
    let mut prog = vec![false; ns];
    let mut front: Vec<bool> = full.clone();
    for _hop in 0..3 {
        let short: Vec<bool> = (0..ns).map(|si| front[si] && net.strokes[si].len() <= 100.0).collect();
        if !short.iter().any(|&x| x) { break; }
        let r = ring(&short);
        let mut next = vec![false; ns];
        for si in 0..ns { if r[si] && !full[si] && !prog[si] { prog[si] = true; next[si] = true; } }
        front = next;
    }
    snaps(&mut full);
    for si in 0..ns { if full[si] { prog[si] = false; } }
    if field {
        let base: Vec<bool> = (0..ns).map(|si| full[si] || prog[si]).collect();
        for si in 0..ns { if base[si] { for &o in &net.strokes[si].cap_nbrs { if !full[o] { prog[o] = true; } } } }
        return Deps { d: d.to_vec(), full, prog, prof: vec![false; ns] };
    }
    let base: Vec<bool> = (0..ns).map(|si| full[si] || prog[si]).collect();
    for si in 0..ns { if base[si] { for &o in &net.strokes[si].cap_nbrs { if !full[o] { prog[o] = true; } } } }
    // vertical components: every member of a component with a section of a full stroke, their crossing partners'
    // components, and the snap trunks of all of them (Tier A pins)
    // (whole components, before their split: the split is a rule on all of a component, prof::split_components)
    let comps_of = |set: &[bool]| -> BTreeSet<usize> {
        let mut c = BTreeSet::new();
        for si in 0..ns { if set[si] { for &k in &plan.sec_of[si] { c.insert(plan.full_root[k]); } } }
        c
    };
    let mut pr = full.clone();
    let cs = comps_of(&pr);
    for (k, sec) in plan.secs.iter().enumerate() { if cs.contains(&plan.full_root[k]) { pr[sec.si] = true; } }
    // a floor whose coupling the cap left out is enforced against the lower road's Tier A: that road too
    let mut partners = Vec::new();
    for (k, c) in plan.cpl.iter().enumerate() {
        if c.what != 0 || plan.joined[k] { continue; }
        let cr = &net.crossings[c.idx];
        if pr[cr.a] { partners.push(cr.b); }
        if pr[cr.b] { partners.push(cr.a); }
    }
    for p in partners { pr[p] = true; }
    snaps(&mut pr);
    let prof: Vec<bool> = (0..ns).map(|si| pr[si] && !full[si]).collect();
    Deps { d: d.to_vec(), full, prog, prof }
}

/// Strokes whose faces may reach the quad's box (NEAR_M round it).
pub fn delivered(net: &Net, bx: [f64; 4]) -> Vec<bool> {
    let b = grow(bx, NEAR_M);
    net.strokes.iter().map(|st| line_meets(&st.line.p, b)).collect()
}

pub struct Reads { pub boxes: Vec<[f64; 4]>, pub fids: Vec<String>, pub counts: [usize; 4], pub cpu: f64 }

/// The read list of the tile whose box (in the frame) is bx, from the segments read so far.
pub fn reads(place: &Place, bx: [f64; 4], opts: Opts) -> Reads { reads_f(place, bx, opts, false) }

/// reads() with the profiles from the squares (deps_f field): the tile's lane graph alone.
pub fn reads_f(place: &Place, bx: [f64; 4], opts: Opts, field: bool) -> Reads {
    let t0 = crate::cpu_now();
    let order: Vec<usize> = (0..place.segs.len()).collect();
    let mut net = net::build_opts(place, &order, opts);
    let lay = part::layout(&mut net);
    let plan = prof::plan(&net, None);
    let d = delivered(&net, bx);
    let dp = deps_f(&net, &lay, &plan, &d, field);
    let mut boxes = Vec::new();
    let mut fids: BTreeSet<String> = BTreeSet::new();
    let mut counts = [0usize; 4];
    let debug = std::env::var("ROADSYS_DEBUG_READS").is_ok();       // (stderr only: the read list's strokes)
    for si in 0..net.strokes.len() {
        if dp.d[si] { counts[0] += 1; }
        if dp.full[si] { counts[1] += 1; }
        if dp.prog[si] { counts[2] += 1; }
        if dp.prof[si] { counts[3] += 1; }
        if !(dp.full[si] || dp.prog[si] || dp.prof[si]) { continue; }
        let st = &net.strokes[si];
        // (a stroke read for its profile alone: itself and the arms at its ends, no lane-graph walks); a long stroke
        // as boxes of 1 km pieces of its line, not one box round all of it
        let halo = if dp.full[si] || dp.prog[si] { st.reach.max(PROF_HALO_M) } else { PROF_HALO_M };
        boxes.extend(stroke_boxes(&st.line.p, halo));
        if debug {
            let b = bbox_of(&st.line.p);
            eprintln!("read {} cls {} len {:.0} box {:.0},{:.0},{:.0},{:.0} {}", if dp.d[si] { "D" } else if dp.full[si] { "full" } else if dp.prog[si] { "prog" } else { "prof" },
                      st.cls, st.len(), b[0], b[1], b[2], b[3], net.segs[net.edges[st.parts[0].edge].seg].fid);
        }
        for p in &st.parts { fids.insert(net.segs[net.edges[p.edge].seg].fid.clone()); }
    }
    // the tile itself (its own cells, also where it has no road)
    boxes.push(grow(bx, NEAR_M));
    Reads { boxes, fids: fids.into_iter().collect(), counts, cpu: crate::cpu_now() - t0 }
}

pub struct Out {
    pub json: String,
    pub bytes: Vec<u8>,
    pub hash: u64,
    pub piece_keys: BTreeMap<u32, u64>,
}

fn hex(v: u64) -> String { format!("{:016x}", v) }

/// The tile's shadow build from a complete read (reads() found nothing missing). quad: the tile's corners in the
/// frame, counter-clockwise; id: its label in the bytes; frame: the frame's identity (in every key); g_of: ground
/// heights at points.
pub fn build(place: &Place, quad: [[f64; 2]; 4], id: (i32, i32), frame: u64, g_of: &dyn Fn(&[P2]) -> Vec<f64>, opts: Opts) -> Out {
    build_f(place, quad, id, frame, g_of, None, opts)
}

/// The profile squares' field for a tile: (z_of(points, tangents, map features) -> heights, NaN where no square holds
/// the road; the digest of the squares it reads).
pub type Field<'a> = (&'a (dyn Fn(&[P2], &[P2], &[String]) -> Vec<f64> + Sync), u64);

/// build() with its profiles from the squares where they hold the road (every stroke with a Tier B section), the
/// rest its own Tier A (reads_f: no component read).
pub fn build_f(place: &Place, quad: [[f64; 2]; 4], id: (i32, i32), frame: u64, g_of: &dyn Fn(&[P2]) -> Vec<f64>,
               field: Option<Field>, opts: Opts) -> Out {
    let t0 = crate::cpu_now();
    let order: Vec<usize> = (0..place.segs.len()).collect();
    let mut net = net::build_opts(place, &order, opts);
    let t1 = crate::cpu_now();
    let lay = part::layout(&mut net);
    let t2 = crate::cpu_now();
    let plan = prof::plan(&net, None);
    let q = tile::Quad { c: [mm2(quad[0]), mm2(quad[1]), mm2(quad[2]), mm2(quad[3])] };
    let bxf = bbox_of(&quad);
    let d = delivered(&net, bxf);
    let dp = deps_f(&net, &lay, &plan, &d, field.is_some());
    let ns = net.strokes.len();
    let need: Vec<bool> = (0..ns).map(|si| dp.full[si] || dp.prof[si]).collect();
    let t3 = crate::cpu_now();
    let mut pr = prof::solve(&net, g_of, Some(&need));
    if let Some((z_of, fkey)) = field {
        // every needed stroke's knots from the squares where they hold it (their Tier B and Tier A), else its own
        let (mut pts, mut tans, mut feats, mut at): (Vec<P2>, Vec<P2>, Vec<String>, Vec<(usize, usize)>) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
        let near = grow(bxf, FIELD_REACH_M);
        for si in 0..ns {
            if !need[si] || pr.z[si].is_empty() { continue; }
            let st = &net.strokes[si];
            for i in 0..pr.z[si].len() {
                let s = (i as f64 * pr.h[si]).min(st.len());
                let (p, t) = st.line.at(s);
                if p[0] < near[0] || p[0] > near[2] || p[1] < near[1] || p[1] > near[3] { continue; }
                let part = st.parts.iter().find(|q| s >= q.s0 - 1e-6 && s <= q.s1 + 1e-6).unwrap_or(&st.parts[0]);
                pts.push(p);
                tans.push(t);
                feats.push(crate::square::feature_of(&net.segs[net.edges[part.edge].seg].fid).to_string());
                at.push((si, i));
            }
        }
        let z = z_of(&pts, &tans, &feats);
        let mut from: BTreeSet<usize> = BTreeSet::new();
        for (k, &(si, i)) in at.iter().enumerate() { if z.get(k).map(|v| v.is_finite()).unwrap_or(false) { pr.z[si][i] = z[k]; from.insert(si); } }
        for si in from { pr.keys[si] = net::uid_of("field", &[pr.keys[si], fkey]); }
    }
    let t4 = crate::cpu_now();
    let built = part::mesh(&net, &lay, &pr, Some(&d));
    let t5 = crate::cpu_now();
    let (marks, _mc) = mark::marks(&net, &built.mesh, &built.rows, &place.style, Some(&d));
    let t6 = crate::cpu_now();
    let all: Vec<u32> = (0..built.mesh.t.len() as u32).collect();
    let allm: Vec<u32> = (0..marks.t.len() as u32).collect();
    let to = tile::tile_quad(&built.mesh, &all, &marks, &allm, &q, id);
    let t7 = crate::cpu_now();
    let inside = |p: P2| q.contains(mm2(p));
    let chk = check::run_in(&net, &built.mesh, &inside, Some(&d), &[], 0);
    let t8 = crate::cpu_now();
    // keys: each delivered stroke's own read list (its deps' boxes) and profile; zones by their arms
    let digests: Vec<u64> = net.segs.iter().map(seg_digest).collect();
    let area_on = opts.areas && !place.areas.is_empty();
    let mut area_order: Vec<usize> = (0..place.areas.len()).collect();
    area_order.sort_by_key(|&i| (place.areas[i].key, crate::areas::area_digest(&place.areas[i])));
    let area_boxes: Vec<[f64; 4]> = if area_on { area_order.iter().map(|&i| {
        let a = &place.areas[i];
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for (_r, r) in &a.rings { let q = bbox_of(r); b = [b[0].min(q[0]), b[1].min(q[1]), b[2].max(q[2]), b[3].max(q[3])]; }
        b
    }).collect() } else { Vec::new() };
    let area_keys: Vec<u64> = if area_on { area_order.iter().map(|&i| crate::areas::area_digest(&place.areas[i])).collect() } else { Vec::new() };
    let seg_boxes: Vec<[f64; 4]> = net.segs.iter().map(|s| bbox_of(&s.pts)).collect();
    let mut piece_keys: BTreeMap<u32, u64> = BTreeMap::new();
    let mut stroke_key = vec![0u64; ns];
    for si in 0..ns {
        if !d[si] || net.strokes[si].absorbed { continue; }
        let mut one = vec![false; ns];
        one[si] = true;
        let ds = deps_f(&net, &lay, &plan, &one, field.is_some());
        let mut bxs: Vec<[f64; 4]> = Vec::new();
        let mut pk: Vec<u64> = Vec::new();
        for o in 0..ns {
            if ds.full[o] || ds.prog[o] { bxs.extend(stroke_boxes(&net.strokes[o].line.p, net.strokes[o].reach.max(PROF_HALO_M))); }
            else if ds.prof[o] { bxs.extend(stroke_boxes(&net.strokes[o].line.p, PROF_HALO_M)); }
            if (ds.full[o] || ds.prof[o]) && need[o] { pk.push(pr.keys[o]); }
        }
        let mut items: Vec<(&str, u64)> = Vec::new();
        // (a segment whose line enters a box: it lies in a cell the read covers, so every complete read has it)
        for (g, s) in net.segs.iter().enumerate() {
            if bxs.iter().any(|b| meets(*b, seg_boxes[g]) && line_meets(&s.pts, *b)) { items.push((s.fid.as_str(), digests[g])); }
        }
        items.sort();
        let mut h = net::uid_of("piece", &[PIECE_FORMAT, frame, net.strokes[si].uid]);
        for (f, dg) in &items { h = net::uid_of(f, &[h, *dg]); }
        if area_on {
            // (BNE_ROADSYS_AREAS=on: the mapped areas within the fit's reach of any stroke it depends on, their
            // dataset's build and the fit's format)
            h = net::uid_of("areas", &[h, crate::areas::AREA_FORMAT, place.area_build]);
            let bg: Vec<[f64; 4]> = bxs.iter().map(|b| grow(*b, crate::areas::RAY_M + 5.0)).collect();
            for (k, ab) in area_boxes.iter().enumerate() { if bg.iter().any(|b| meets(*b, *ab)) { h = net::uid_of("area", &[h, area_keys[k]]); } }
        }
        pk.sort();
        for k in pk { h = net::uid_of("prof", &[h, k]); }
        stroke_key[si] = h;
        piece_keys.insert(net::piece_of(net.strokes[si].uid), h);
    }
    for (c, arms) in lay.arms.iter().enumerate() {
        if arms.len() < 2 || !arms.iter().any(|a| d[a.stroke]) { continue; }
        let label = built.zone_piece[c];
        if label == 0 { continue; }
        let mut ks: Vec<u64> = arms.iter().map(|a| if stroke_key[a.stroke] != 0 { stroke_key[a.stroke] } else { pr.keys[a.stroke] ^ net.strokes[a.stroke].uid }).collect();
        ks.sort();
        let mut h = net::uid_of("zone", &[PIECE_FORMAT, frame, label as u64]);
        for k in ks { h = net::uid_of("arm", &[h, k]); }
        piece_keys.insert(label, h);
    }
    let ps = &pr.stats;
    let bs = &built.stats;
    let st = &net.stats;
    let mut keys_json = String::new();
    for (i, (p, k)) in piece_keys.iter().enumerate() { let _ = write!(keys_json, "{}\"{:08x}\":\"{}\"", if i > 0 { "," } else { "" }, p, hex(*k)); }
    let cnt = |v: &[bool]| v.iter().filter(|&&x| x).count();
    let json = format!(
        "{{\"format\":{},\"tile_hash\":\"{}\",\"tris\":{},\"verts\":{},\"mark_tris\":{},\
         \"cpu_ms\":{{\"network\":{:.3},\"layout\":{:.3},\"deps\":{:.3},\"profile\":{:.3},\"surface\":{:.3},\"markings\":{:.3},\"clip\":{:.3},\"checks\":{:.3},\"total\":{:.3}}},\
         \"read\":{{\"segments\":{},\"strokes\":{},\"delivered\":{},\"full\":{},\"program\":{},\"profile\":{}}},\
         \"net\":{{\"forks\":{},\"adds\":{},\"drops\":{},\"structures\":{},\"tunnels\":{},\"crossings\":{},\"unbridged\":{},\"cuts\":{},\"cut_unjoined\":{}}},\
         \"profile\":{{\"tier_a\":{},\"components\":{},\"sections\":{},\"secondary\":{},\"knots\":{},\"biggest_knots\":{},\"biggest_km\":{:.2},\"cap_cuts\":{},\"solves\":{},\"reused\":{},\"factor_fail\":{},\"floors\":{},\"floors_met\":{},\"floor_short_max_m\":{:.3},\"grade_viol\":{},\"comfort_viol\":{},\"couple_max_mm\":{:.2}}},\
         \"surface\":{{\"zones\":{},\"zone_union\":{},\"zone_union_fail\":{},\"zone_fallback_straight\":{},\"zone_fail\":{},\"arm_order_fallback\":{},\"snap_ok\":{},\"snap_mismatch\":{},\"fold_guard\":{},\"folds\":{},\"twin_capped\":{},\"twin_lane_overlap\":{}}},\
         \"checks\":{{\"paved_m2\":{:.1},\"lane_m2\":{:.1},\"overlap_m2\":{:.2},\"overlap_lane_m2\":{:.2},\"overlap_same_piece_m2\":{:.2},\"overlap_strip_strip_m2\":{:.2},\"overlap_zone_strip_m2\":{:.2},\"overlap_zone_zone_m2\":{:.2},\"hole_samples\":{},\"hole_uncovered\":{},\"nonmanifold_edges\":{},\"flipped_edges\":{},\"tjunctions\":{},\"folded\":{},\"zero_area\":{}}},\
         \"seams\":[{},{},{},{}],\"keys\":{{{}}}}}",
        PIECE_FORMAT, hex(to.hash), to.tris, to.verts, to.mark_tris,
        (t1 - t0) * 1e3, (t2 - t1) * 1e3, (t3 - t2) * 1e3, (t4 - t3) * 1e3, (t5 - t4) * 1e3, (t6 - t5) * 1e3, (t7 - t6) * 1e3, (t8 - t7) * 1e3, (t8 - t0) * 1e3,
        place.segs.len(), ns, cnt(&dp.d), cnt(&dp.full), cnt(&dp.prog), cnt(&dp.prof),
        st.forks, st.adds, st.drops, st.structures, st.tunnels, st.crossings, st.unbridged, st.cuts, st.cut_unjoined,
        ps.tier_a, ps.components, ps.sections, ps.secondary, ps.knots, ps.biggest_component_knots, ps.biggest_component_km, ps.cap_cuts, ps.solves, ps.reused, ps.factor_fail, ps.crossings, ps.floors_met, ps.floor_short_max_m, ps.grade_viol, ps.comfort_viol, ps.couple_max_mm,
        bs.zones, bs.zone_union, bs.zone_union_fail, bs.zone_fallback_straight, bs.zone_fail, bs.arm_order_fallback, bs.snap_ok, bs.snap_mismatch, bs.fold_guard, bs.folds, bs.twin_capped, bs.twin_lane_overlap,
        chk.paved_m2, chk.lane_m2, chk.overlap_m2, chk.overlap_lane_m2, chk.overlap_fold_m2, chk.overlap_strip_strip_m2, chk.overlap_zone_strip_m2, chk.overlap_zone_zone_m2, chk.hole_samples, chk.hole_uncovered, chk.nonmanifold_edges, chk.flipped_edges, chk.tjunctions, chk.folded, chk.zero_area,
        seam_hash(&to.sides[0]), seam_hash(&to.sides[1]), seam_hash(&to.sides[2]), seam_hash(&to.sides[3]),
        keys_json);
    // the mapped road areas: the audit of the tile's faces against them, and the fit's counts (areas.rs)
    let mut json = json;
    if !place.areas.is_empty() {
        let prep = crate::areas::prepare(&place.areas);
        let tris = crate::areas::faces_of(&built.mesh, &quad);
        let lines = crate::areas::lines_of(&net, Some(&d));
        let a = crate::areas::audit(&prep, quad, &tris, &lines);
        let f = &net.area_fit;
        json.pop();
        let _ = write!(json, ",\"areas_on\":{},\"areas\":{},\"areas_fit\":{{\"strokes\":{},\"fitted\":{},\"stations\":{},\"valid\":{},\"no_area\":{},\"junction\":{},\"fork\":{},\"no_edge\":{},\"implausible\":{},\"outlier\":{},\"few\":{},\"clamped\":{},\"knots\":{},\"failed\":{}}}}}",
                       area_on, a.json(), f.strokes, f.fitted, f.stations, f.valid, f.no_area, f.junction, f.fork, f.no_edge, f.implausible, f.outlier, f.few, f.clamped, f.knots, f.failed);
    }
    Out { json, hash: to.hash, bytes: to.bytes, piece_keys }
}

/// A seam's edges as a hash (two tiles sharing a side must give the same; quoted for JSON).
fn seam_hash(e: &[(tile::V3, tile::V3)]) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for (a, b) in e { for v in a.iter().chain(b.iter()) { for c in v.to_le_bytes() { h ^= c as u64; h = h.wrapping_mul(0x100000001b3); } } }
    format!("\"{}:{}\"", e.len(), hex(h))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(fid: &str, pts: &[[f64; 2]], cls: i64, oneway: i64, kmh: f64, cons: &[(&str, usize)], bridge: Option<(f64, f64)>) -> Seg {
        Seg { fid: fid.into(), code: "US".into(), cls_s: "secondary".into(), pave: 7.0, drive: 7.0, inferred: 2, cls, dir: oneway,
              lanes: 2, lfw: -1, lbw: -1, kmh, lane_w: 3.5, pts: pts.to_vec(), cons: cons.iter().map(|(c, v)| (c.to_string(), *v)).collect(),
              bridge: bridge.into_iter().collect(), level: bridge.map(|b| vec![(b.0, b.1, 1.0)]).unwrap_or_default(), ..Default::default() }
    }

    fn place(rev: bool) -> Place {
        let mut segs = vec![
            seg("a", &[[-400.0, 0.0], [0.0, 0.0]], 4, 0, 50.0, &[("n0", 1)], None),
            seg("b", &[[0.0, 0.0], [400.0, 0.0]], 4, 0, 50.0, &[("n0", 0), ("n1", 1)], None),
            seg("c", &[[0.0, -400.0], [0.0, 0.0]], 4, 0, 50.0, &[("n0", 1)], None),
            seg("d", &[[0.0, 0.0], [0.0, 400.0]], 4, 0, 50.0, &[("n0", 0)], None),
            seg("e", &[[400.0, 0.0], [400.0, 300.0]], 4, 0, 50.0, &[("n1", 0)], None),
            seg("m", &[[-300.0, -600.0], [-300.0, 600.0]], 1, 1, 100.0, &[], Some((0.45, 0.55))),
        ];
        if rev { segs.reverse(); }
        Place { segs, ..Default::default() }
    }

    fn g(p: &[P2]) -> Vec<f64> { p.iter().map(|q| 10.0 + 0.01 * q[0] + 0.02 * q[1]).collect() }

    const Q: [[f64; 2]; 4] = [[-128.0, -128.0], [128.0, -128.0], [128.0, 128.0], [-128.0, 128.0]];

    #[test]
    fn the_same_bytes_whatever_the_input_order() {
        prof::store_clear();
        let a = build(&place(false), Q, (0, 0), 1, &g, Opts::default());
        prof::store_clear();
        let b = build(&place(true), Q, (0, 0), 1, &g, Opts::default());
        assert!(a.bytes.len() > 1000);
        assert_eq!(a.bytes, b.bytes);
        assert_eq!(a.piece_keys, b.piece_keys);
    }

    #[test]
    fn neighbours_cut_the_same_seam() {
        let a = build(&place(false), Q, (0, 0), 1, &g, Opts::default());
        let east = [[128.0, -128.0], [384.0, -128.0], [384.0, 128.0], [128.0, 128.0]];
        let b = build(&place(false), east, (1, 0), 1, &g, Opts::default());
        let seam = |j: &str, k: usize| j.split("\"seams\":[").nth(1).unwrap().split(']').next().unwrap().split(',').nth(k).unwrap().to_string();
        assert_eq!(seam(&a.json, 1), seam(&b.json, 3));
    }

    #[test]
    fn the_input_dump_rebuilds_the_tile() {
        let p = place(false);
        let asked = std::cell::RefCell::new(Vec::new());
        let gr = |pts: &[P2]| { let v = g(pts); asked.borrow_mut().extend_from_slice(&v); v };
        let a = build(&p, Q, (0, 0), 7, &gr, Opts::default());
        let t = crate::io::TileInput { place: p, quad: Q, id: (0, 0), frame: 7, cut: (4000.0, 2000.0), ground: asked.borrow().clone(), areas_on: false };
        let back = crate::io::read_tile(&crate::io::write_tile(&t)).unwrap();
        let ground = back.ground.clone();
        let b = build(&back.place, back.quad, back.id, back.frame, &|_p: &[P2]| ground.clone(), Opts { cut_grid: back.cut.0, cut_offset: back.cut.1, ..Default::default() });
        assert_eq!(a.bytes, b.bytes);
    }
}
