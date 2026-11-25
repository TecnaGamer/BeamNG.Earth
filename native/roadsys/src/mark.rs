//! Markings derived from the lanes: runs on the strips' own boundaries (the same row vertices), the country's style
//! (server/markings.py Style, exported with the place): lane lines dashed (motorway / urban pattern), the centre line
//! (double solid where the style says so, else dashed), edge lines on motorway..secondary, solid lines along both
//! sides of a gore. Dash phase from the stroke's station (never jumps at a tile edge or a deck joint). z: the
//! asphalt's along the row (the row edge is a mesh edge) + 5 mm.
use crate::geom::*;
use crate::io::Style;
use crate::net::*;
use crate::part::{Mesh, Row};

#[derive(Default)]
pub struct Marks {
    pub v: Vec<[i64; 3]>,
    pub t: Vec<[u32; 3]>,
    pub colour: Vec<u8>,      // per triangle: 0 white, 1 yellow
    pub piece: Vec<u32>,
    pub pieces: usize,
    pub fragments: usize,
}

#[derive(Clone, Copy, PartialEq)]
enum Kind { Lane, Centre, Edge, EdgeLeft, Gore, Cycle, CycleAdv, Park, Bus }

/// (BNE_ROADSYS_XSEC) a cross-section strip slot (xsec.rs): parking, cycle lane, buffer.
fn strip(k: u8) -> bool { k == crate::xsec::K_PARK || k == crate::xsec::K_CYCLE || k == crate::xsec::K_BUFFER }

fn colour(st: &Style, k: Kind) -> u8 {
    match k {
        Kind::Centre => if st.centre.contains("yellow") { 1 } else { 0 },
        Kind::EdgeLeft => if st.left_edge.contains("yellow") { 1 } else { 0 },
        _ => 0,
    }
}

/// One marking ribbon along boundary k of rows[ra..=rb] (stations s), lateral offset `lat` (m, + left), width w,
/// dashed (dash, gap) or solid.
#[allow(clippy::too_many_arguments)]
fn ribbon(out: &mut Marks, mesh: &Mesh, rows: &[Row], k: usize, ra: usize, rb: usize, lat: f64, w: f64,
          dash: Option<(f64, f64)>, col: u8, piece: u32) {
    let pos = |r: usize| { let v = mesh.v[rows[r].b[k] as usize]; [v[0] as f64 / 1000.0, v[1] as f64 / 1000.0, v[2] as f64 / 1000.0] };
    // per row: point, unit across (left) from the row's own boundaries
    let across = |r: usize| -> P2 {
        let b = &rows[r].b;
        let (p0, p1) = (mesh.v[b[0] as usize], mesh.v[*b.last().unwrap() as usize]);
        unit([(p0[0] - p1[0]) as f64, (p0[1] - p1[1]) as f64]).unwrap_or([0.0, 1.0])
    };
    // pieces: list of (s0, s1) on/off intervals along the stations
    let (s_lo, s_hi) = (rows[ra].s, rows[rb].s);
    let mut ivs: Vec<(f64, f64)> = Vec::new();
    match dash {
        // (a pattern without length is a solid line: never a loop over every dash of the station range)
        None => ivs.push((s_lo, s_hi)),
        Some((d, g)) if !(d > 0.05 && d + g > 0.1) || !((s_hi - s_lo) / (d + g) < 1e6) => ivs.push((s_lo, s_hi)),
        Some((d, g)) => {
            let p = d + g;
            let k0 = (s_lo / p).floor() as i64;
            let k1 = (s_hi / p).ceil() as i64;
            for kk in k0..=k1 {
                let a = kk as f64 * p;
                let (x0, x1) = (a.max(s_lo), (a + d).min(s_hi));
                if x1 > x0 + 0.05 { ivs.push((x0, x1)); }
            }
        }
    }
    let at = |s: f64| -> ([f64; 3], P2) {
        let mut r = ra;
        while r + 1 < rb && rows[r + 1].s < s { r += 1; }
        let r1 = (r + 1).min(rb);
        let (sa, sb) = (rows[r].s, rows[r1].s);
        let u = if sb > sa { clampf((s - sa) / (sb - sa), 0.0, 1.0) } else { 0.0 };
        let (pa, pb) = (pos(r), pos(r1));
        let (na, nb) = (across(r), across(r1));
        let p = [pa[0] + (pb[0] - pa[0]) * u, pa[1] + (pb[1] - pa[1]) * u, pa[2] + (pb[2] - pa[2]) * u];
        (p, unit(lerp(na, nb, u)).unwrap_or(na))
    };
    for (x0, x1) in ivs {
        // the polyline: x0, every row inside, x1
        let mut ss = vec![x0];
        for r in ra..=rb { if rows[r].s > x0 + 0.01 && rows[r].s < x1 - 0.01 { ss.push(rows[r].s); } }
        ss.push(x1);
        let base = out.v.len() as u32;
        let mut area = 0.0;
        for (i, &s) in ss.iter().enumerate() {
            let (p, n) = at(s);
            for side in [1.0, -1.0] {
                let o = lat + side * w / 2.0;
                let q = [p[0] + n[0] * o, p[1] + n[1] * o];
                out.v.push([mm(q[0]), mm(q[1]), mm(p[2] + 0.005)]);
            }
            if i > 0 { area += (s - ss[i - 1]) * w; }
        }
        for i in 0..ss.len() as u32 - 1 {
            let (a, b, c, d) = (base + 2 * i, base + 2 * i + 1, base + 2 * i + 3, base + 2 * i + 2);
            out.t.push([a, b, c]); out.colour.push(col); out.piece.push(piece);
            out.t.push([a, c, d]); out.colour.push(col); out.piece.push(piece);
        }
        out.pieces += 1;
        if area < 0.015 { out.fragments += 1; }
    }
}

/// (BNE_ROADSYS_XSEC) boundary k's marking at station s: its sides the nearest slots with any width (a run of empty
/// strips lies on one line; its leftmost boundary takes it, the others none), today's rules between lanes, shoulders
/// and gores, the strips' lines beside a strip.
fn xsec_kind(st: &Stroke, k: usize, s: f64, edge_cls: bool) -> Option<Kind> {
    let ns = st.slots.len();
    let l = &st.slots[k - 1];
    let wl = l.w(s);
    if strip(l.kind) && wl < 0.01 { return None; }
    let mut j = k;
    while j < ns && strip(st.slots[j].kind) && st.slots[j].w(s) < 0.01 { j += 1; }
    if j >= ns { return None; }
    let r = &st.slots[j];
    let wr = r.w(s);
    if l.kind == K_GORE && wl > 0.3 || r.kind == K_GORE && wr > 0.3 { return Some(Kind::Gore); }
    if !strip(l.kind) && !strip(r.kind) {
        if wl <= 0.5 || wr <= 0.5 { return None; }
        return match (l.kind, r.kind) {
            (K_LANE, K_LANE) if l.group != r.group => Some(Kind::Centre),
            (K_LANE, K_LANE) => Some(if st.over_at(k - 1, s, crate::xsec::K_BUS) != st.over_at(j, s, crate::xsec::K_BUS) { Kind::Bus } else { Kind::Lane }),
            (K_SHOULDER, K_LANE) => if edge_cls { Some(if st.oneway { Kind::EdgeLeft } else { Kind::Edge }) } else { None },
            (K_LANE, K_SHOULDER) => if edge_cls { Some(Kind::Edge) } else { None },
            _ => None,
        };
    }
    if wl <= 0.2 || wr <= 0.2 { return None; }
    use crate::xsec::{K_BUFFER, K_CYCLE, K_PARK};
    let adv = |i: usize| st.slots[i].kind == K_CYCLE && st.over_at(i, s, OVER_ADV);
    match (l.kind, r.kind) {
        (K_LANE, K_CYCLE) | (K_BUFFER, K_CYCLE) => Some(if adv(j) { Kind::CycleAdv } else { Kind::Cycle }),
        (K_CYCLE, K_LANE) | (K_CYCLE, K_BUFFER) => Some(if adv(k - 1) { Kind::CycleAdv } else { Kind::Cycle }),
        (K_LANE, K_BUFFER) | (K_BUFFER, K_LANE) => Some(Kind::Cycle),
        (K_CYCLE, K_PARK) | (K_PARK, K_CYCLE) => Some(Kind::Park),
        (K_LANE, K_PARK) | (K_PARK, K_LANE) => if edge_cls { Some(Kind::Park) } else { None },
        _ => None,
    }
}

pub fn marks(net: &Net, mesh: &Mesh, rows: &[Vec<Row>], style: &Style, sel: Option<&[bool]>) -> (Marks, Vec<f64>) {
    let mut out = Marks::default();
    let mut cpu = vec![0.0; net.strokes.len()];
    for (si, st) in net.strokes.iter().enumerate() {
        if let Some(v) = sel { if !v[si] { continue; } }
        let rs = &rows[si];
        if rs.len() < 2 || st.absorbed { continue; }
        let t0 = crate::cpu_now();
        let mw = st.cls == 1 || st.cls == 2;
        let w = if mw { style.w_motorway } else { style.w };
        let edge_cls = st.cls >= 1 && st.cls <= 4;
        let ns = st.slots.len();
        for k in 1..ns {
            let (l, r) = (&st.slots[k - 1], &st.slots[k]);
            // classify boundary k at each row; emit runs
            let kind_at = |s: f64| -> Option<Kind> {
                if net.xsec_on { return xsec_kind(st, k, s, edge_cls); }
                let (wl, wr) = (l.w(s), r.w(s));
                if l.kind == K_GORE && wl > 0.3 || r.kind == K_GORE && wr > 0.3 { return Some(Kind::Gore); }
                if wl <= 0.5 || wr <= 0.5 { return None; }
                match (l.kind, r.kind) {
                    (K_LANE, K_LANE) => Some(if l.group != r.group { Kind::Centre } else { Kind::Lane }),
                    (K_SHOULDER, K_LANE) => if edge_cls { Some(if st.oneway { Kind::EdgeLeft } else { Kind::Edge }) } else { None },
                    (K_LANE, K_SHOULDER) => if edge_cls { Some(Kind::Edge) } else { None },
                    _ => None,
                }
            };
            let mut r0 = 0;
            while r0 + 1 < rs.len() {
                let kd = kind_at((rs[r0].s + rs[r0 + 1].s) / 2.0);
                let mut r1 = r0 + 1;
                while r1 + 1 < rs.len() && kind_at((rs[r1].s + rs[r1 + 1].s) / 2.0) == kd { r1 += 1; }
                if let Some(kd) = kd {
                    let col = colour(style, kd);
                    match kd {
                        Kind::Lane => ribbon(&mut out, mesh, rs, k, r0, r1, 0.0, w, Some(if mw { style.lane_motorway } else { style.lane_urban }), col, piece_of(st.uid)),
                        Kind::Centre => {
                            if style.double_centre {
                                let g = 0.12;
                                ribbon(&mut out, mesh, rs, k, r0, r1, (w + g) / 2.0, w, None, col, piece_of(st.uid));
                                ribbon(&mut out, mesh, rs, k, r0, r1, -(w + g) / 2.0, w, None, col, piece_of(st.uid));
                            } else {
                                ribbon(&mut out, mesh, rs, k, r0, r1, 0.0, w, Some(style.centre_urban), col, piece_of(st.uid));
                            }
                        }
                        Kind::Edge | Kind::EdgeLeft => {
                            let ew = if mw { style.edge_motorway } else { style.edge };
                            let inward = if l.kind == K_SHOULDER { -ew / 2.0 } else { ew / 2.0 };
                            ribbon(&mut out, mesh, rs, k, r0, r1, inward, ew, None, col, piece_of(st.uid));
                        }
                        Kind::Gore => ribbon(&mut out, mesh, rs, k, r0, r1, 0.0, style.edge.max(0.15), None, col, piece_of(st.uid)),
                        // (BNE_ROADSYS_XSEC) a cycle lane's or bus lane's broad solid line, an advisory lane's dashes,
                        // a parking lane's thin solid line
                        Kind::Cycle | Kind::Bus => ribbon(&mut out, mesh, rs, k, r0, r1, 0.0, (w * 2.0).min(0.3), None, col, piece_of(st.uid)),
                        Kind::CycleAdv => ribbon(&mut out, mesh, rs, k, r0, r1, 0.0, w, Some((1.0, 1.0)), col, piece_of(st.uid)),
                        Kind::Park => ribbon(&mut out, mesh, rs, k, r0, r1, 0.0, w, None, col, piece_of(st.uid)),
                    }
                }
                r0 = r1;
            }
        }
        cpu[si] = crate::cpu_now() - t0;
    }
    (out, cpu)
}
