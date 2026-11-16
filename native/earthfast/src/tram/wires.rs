//! Wires and prop rows of the tram overhead line (plan 046; tramcat.py's `rows_and_wires`, then stage G): a row for each
//! pole and mast, the contact wire between the nodes of a part (following the track's axis between its pull-offs, not
//! an arc in the air), the joints of the line (parts that meet, a branch that starts on the main wire, a switch's
//! fitting, wires that cross, a line's end anchored), and the span wires across a street (the contact wire hung from
//! them) with their insulators and tensioner springs. Every item carries its owner point: the tile that holds it draws
//! it (the glue filters).
use super::geom::*;
use super::poles::*;
use crate::dm::Dm;

pub const MODEL_H: f64 = 9.0; // every tram pole model's height (the rows' height: scale 1)
pub const SPAN_Z: f64 = 7.4;
pub const WALL_Z: f64 = 6.8;
pub const CONTACT_Z: f64 = 5.6;
pub const HANGER_M: f64 = 0.5;
pub const SPAN_R: f64 = 0.009;
// the contact wire and a cantilever's tip over the pole's base (tools/blender/build_tram_poles.py ARM_Z)
pub const Z_ARM: f64 = CONTACT_Z;
pub const MAST_Z_BAND: (f64, f64) = (-0.5, 0.2);
pub const CONTACT_R: f64 = 0.016;
pub const DROPPER_R: f64 = 0.010;
pub const MAX_SPAN_DRAWN_M: f64 = 200.0;
// stage G: the wire follows the axis (a pull-off wherever the chord would leave it by more than PULL_TOL_M) ...
pub const PULL_TOL_M: f64 = 0.10;
pub const PULL_MAX_M: f64 = 12.0;
// ... joints: part ends this close are one line (catenary.JOINT_M), a branch's end this close to another's side starts on it
pub const JOINT_M: f64 = 1.5;
pub const LINK_DZ_MAX_M: f64 = 3.0; // ends this far apart in height are not one line (a bridge's wire at its deck, the ground's under it)
pub const SWITCH_CLAMP_M: f64 = 0.9;
pub const CROSS_DZ_M: f64 = 0.3;
pub const CROSS_END_M: f64 = 4.0;
pub const ANCHOR_REACH_M: f64 = 25.0;
// ... fittings (the bars are drawn four-sided in the wire mesh: catenary.wire_solid; the materials by half width)
pub const INS_R: f64 = 0.035;
pub const CLAMP_R: f64 = 0.030;
pub const HANGER_R: f64 = 0.022;
pub const SPRING_R: f64 = 0.019;
pub const INS_L: f64 = 0.35;
pub const SPRING_L: f64 = 0.8;
pub const SPRING_AMP: f64 = 0.06;
pub const SPRING_SEGS: usize = 10;
pub const FITTING_MIN_SPAN_M: f64 = 3.0;
// pull-off cables: from a vertex of the wire on a curve out to the nearest pole or rosette (at most PULL_REACH_M off), one
// every PULL_SPACING_M of a piece at most
pub const PULL_REACH_M: f64 = 22.0;
pub const PULL_SPACING_M: f64 = 6.0;
pub const PULL_MIN_BEND_M: f64 = 0.04;
// a tram wire over a railway track: an insulated section of this length with a clamp
pub const RAIL_INSULATED_M: f64 = 4.0;

/// A prop row as the tile's props list takes it: [group, x, y, z, yaw, scale, height, 0].
pub struct Row {
    pub group: &'static str,
    pub x: f64,
    pub y: f64,
    pub z: f64,
    pub yaw: f64,
    pub scale: f64,
    pub height: f64,
    pub owner: P2,
}

pub struct Wire {
    pub pts: Vec<[f64; 3]>,
    pub r: f64,
    pub owner: P2,
}

/// The ground heights of the plan's points (the order of `ground_points`).
pub struct Heights {
    pub poles: Vec<f64>,
    pub p: Vec<Vec<f64>>,
    pub q: Vec<Vec<f64>>,
}

/// The points whose ground height the build needs: the poles' feet, then each node's axis point and (a mast) its foot.
pub fn ground_points(plan: &Plan) -> Vec<P2> {
    let mut pts: Vec<P2> = plan.poles.iter().map(|p| p.q).collect();
    for pp in &plan.parts {
        for nd in &pp.nodes {
            pts.push(nd.p);
            if nd.kind == Kind::Mast {
                pts.push(nd.q);
            }
        }
    }
    pts
}

pub fn split_heights(plan: &Plan, z: &[f64]) -> Heights {
    let mut k = 0;
    let poles = z[..plan.poles.len()].to_vec();
    k += plan.poles.len();
    let (mut p, mut q) = (Vec::new(), Vec::new());
    for pp in &plan.parts {
        let (mut pv, mut qv) = (Vec::new(), Vec::new());
        for nd in &pp.nodes {
            pv.push(z[k]);
            k += 1;
            if nd.kind == Kind::Mast {
                qv.push(z[k]);
                k += 1;
            } else {
                qv.push(f64::NAN);
            }
        }
        p.push(pv);
        q.push(qv);
    }
    Heights { poles, p, q }
}

/// The prop group of a pole model: tram_<family>_<span | arm | centre> (trees.lua's catalog, tools/blender/build_tram_poles.py).
pub fn group(family: Family, kind: &str) -> &'static str {
    match (family, kind) {
        (Family::Tube, "span") => "tram_tube_span",
        (Family::Tube, "arm") => "tram_tube_arm",
        (Family::Tube, _) => "tram_tube_centre",
        (Family::Square, "span") => "tram_square_span",
        (Family::Square, "arm") => "tram_square_arm",
        (Family::Square, _) => "tram_square_centre",
        (Family::Lattice, "span") => "tram_lattice_span",
        (Family::Lattice, "arm") => "tram_lattice_arm",
        (Family::Lattice, _) => "tram_lattice_centre",
        (Family::Concrete, "span") => "tram_concrete_span",
        (Family::Concrete, "arm") => "tram_concrete_arm",
        (Family::Concrete, _) => "tram_concrete_centre",
    }
}

pub const G_ROSETTE: &str = "tram_rosette";
pub const G_LAMP_GALV: &str = "tram_lamp_g";
pub const G_LAMP_BLACK: &str = "tram_lamp_k";
pub const G_LAMP_T: &str = "tram_lamp_t";

/// The lamp bracket of a family's street pole: the galvanised arm (tube, concrete), the black arm (lattice), Helsinki's T head (square).
pub fn lamp_group(family: Family) -> &'static str {
    match family {
        Family::Tube | Family::Concrete => G_LAMP_GALV,
        Family::Lattice => G_LAMP_BLACK,
        Family::Square => G_LAMP_T,
    }
}

fn yaw_to(from: P2, to: P2) -> f64 {
    (to[1] - from[1]).datan2(to[0] - from[0]).to_degrees().rem_euclid(360.0)
}

fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    x.max(lo).min(hi)
}

type P3 = [f64; 3];

fn sub3(a: P3, b: P3) -> P3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn len3(a: P3) -> f64 {
    (a[0] * a[0] + a[1] * a[1] + a[2] * a[2]).sqrt()
}

fn at3(a: P3, u: P3, t: f64) -> P3 {
    [a[0] + u[0] * t, a[1] + u[1] * t, a[2] + u[2] * t]
}

fn unit3(a: P3) -> P3 {
    let l = len3(a).max(1e-9);
    [a[0] / l, a[1] / l, a[2] / l]
}

/// A drawn piece of a part's contact wire: between two of its nodes, along the axis.
struct Piece {
    pts: Vec<P3>,
    /// The indices in pts of the pull-off vertices (a bend of the axis the wire follows).
    pulls: Vec<usize>,
    sa: f64,
    sb: f64,
    za: f64,
    zb: f64,
}

/// A part's contact wire: its pieces, the axis it follows, and the heights at its nodes.
struct Line<'a> {
    xy: &'a [P2],
    arc: Vec<f64>,
    pieces: Vec<Piece>,
    ends: [(P2, f64, P2); 2], // (point, wire height, unit direction pointing out of the part)
}

impl Line<'_> {
    /// The contact wire's height at arc s of the axis (None where no piece covers it).
    fn z_at(&self, s: f64) -> Option<f64> {
        self.pieces.iter().find(|pc| s >= pc.sa - 1e-9 && s <= pc.sb + 1e-9).map(|pc| {
            let w = (pc.sb - pc.sa).max(1e-9);
            pc.za + (pc.zb - pc.za) * ((s - pc.sa) / w)
        })
    }
}

/// The wire between nodes a and b of a part: along the axis, a vertex where the chord would leave it by PULL_TOL_M (a
/// pull-off) and at most PULL_MAX_M apart; the height linear along the arc.
fn piece(xy: &[P2], arc: &[f64], a: &Node, b: &Node, za: f64, zb: f64) -> Option<Piece> {
    let l = hyp(b.p[0] - a.p[0], b.p[1] - a.p[1]);
    if !(1.0 <= l && l <= MAX_SPAN_DRAWN_M) {
        return None;
    }
    let (sa, sb) = (a.s, b.s);
    let mut flat: Vec<P2> = if sb > sa + 1e-6 && sb - sa <= MAX_SPAN_DRAWN_M * 1.3 { between(xy, arc, sa, sb) } else { vec![a.p, b.p] };
    // (the nodes' own points: a bridge or a mast keeps them exactly)
    flat[0] = a.p;
    let last = flat.len() - 1;
    flat[last] = b.p;
    let keep = simplify(&flat, PULL_TOL_M);
    // the arc fraction of each kept vertex along the polyline
    let mut run = vec![0.0; flat.len()];
    for i in 1..flat.len() {
        run[i] = run[i - 1] + hyp(flat[i][0] - flat[i - 1][0], flat[i][1] - flat[i - 1][1]);
    }
    let whole = run[flat.len() - 1].max(1e-9);
    let mut pts: Vec<P3> = Vec::new();
    let mut pulls: Vec<usize> = Vec::new();
    for w in keep.windows(2) {
        let (i, j) = (w[0], w[1]);
        let seg = run[j] - run[i];
        let k = ((seg / PULL_MAX_M).ceil() as usize).max(1);
        for m in 0..k {
            if m == 0 && i != keep[0] {
                pulls.push(pts.len());
            }
            let t = m as f64 / k as f64;
            let r = run[i] + seg * t;
            let (x, y) = (flat[i][0] + (flat[j][0] - flat[i][0]) * t, flat[i][1] + (flat[j][1] - flat[i][1]) * t);
            pts.push([x, y, za + (zb - za) * (r / whole)]);
        }
    }
    let lp = flat[last];
    pts.push([lp[0], lp[1], zb]);
    Some(Piece { pts, pulls, sa, sb, za, zb })
}

fn tangent_out(xy: &[P2], arc: &[f64], start: bool) -> P2 {
    let n = xy.len();
    if start {
        let (_, u) = frame_at(xy, arc, 0.5_f64.min(arc[n - 1] / 2.0));
        [-u[0], -u[1]]
    } else {
        let (_, u) = frame_at(xy, arc, (arc[n - 1] - 0.5).max(arc[n - 1] / 2.0));
        u
    }
}

/// A bar (a four-sided wire of half width r) of length len, centred on p, along unit direction u (horizontal).
fn bar(p: P3, u: P2, len: f64, r: f64, owner: P2) -> Wire {
    let h = len / 2.0;
    Wire { pts: vec![[p[0] - u[0] * h, p[1] - u[1] * h, p[2]], [p[0] + u[0] * h, p[1] + u[1] * h, p[2]]], r, owner }
}

/// The span wire's end: an insulator bar at the fixing, the tensioner spring (`spring`) after it, then the wire on to
/// mid; returns the point where the plain wire starts.
fn span_end(a: P3, mid: P3, spring: bool, owner: P2, out: &mut Vec<Wire>) -> P3 {
    let d = sub3(mid, a);
    if len3(d) < FITTING_MIN_SPAN_M + INS_L + SPRING_L {
        return a;
    }
    let u = unit3(d);
    let ins_end = at3(a, u, INS_L);
    out.push(Wire { pts: vec![a, ins_end], r: INS_R, owner });
    if !spring {
        return ins_end;
    }
    let (sx, sy) = (u[1], -u[0]);
    let sl = (sx * sx + sy * sy).sqrt();
    let side = if sl > 1e-6 { [sx / sl, sy / sl, 0.0] } else { [1.0, 0.0, 0.0] };
    let mut pts = Vec::with_capacity(SPRING_SEGS + 1);
    for k in 0..=SPRING_SEGS {
        let off = if k == 0 || k == SPRING_SEGS { 0.0 } else if k % 2 == 1 { SPRING_AMP } else { -SPRING_AMP };
        let c = at3(ins_end, u, SPRING_L * k as f64 / SPRING_SEGS as f64);
        pts.push(at3(c, side, off));
    }
    let end = at3(ins_end, u, SPRING_L);
    out.push(Wire { pts, r: SPRING_R, owner });
    end
}

/// Proper crossing of segments p-q and r-s in 2D: (point, t along p-q, u along r-s), both strictly inside.
fn cross2(p: P2, q: P2, r: P2, s: P2) -> Option<(P2, f64, f64)> {
    let (d1, d2) = ([q[0] - p[0], q[1] - p[1]], [s[0] - r[0], s[1] - r[1]]);
    let den = d1[0] * d2[1] - d1[1] * d2[0];
    if den.abs() < 1e-9 {
        return None;
    }
    let t = ((r[0] - p[0]) * d2[1] - (r[1] - p[1]) * d2[0]) / den;
    let u = ((r[0] - p[0]) * d1[1] - (r[1] - p[1]) * d1[0]) / den;
    if t > 0.0 && t < 1.0 && u > 0.0 && u < 1.0 {
        Some(([p[0] + d1[0] * t, p[1] + d1[1] * t], t, u))
    } else {
        None
    }
}

/// tramcat.rows_and_wires and the joints of stage G: the rows and wires of the plan.
pub fn build(sc: &Scene, plan: &Plan, h: &Heights) -> (Vec<Row>, Vec<Wire>) {
    let mut rows: Vec<Row> = Vec::new();
    let mut wires: Vec<Wire> = Vec::new();
    for (i, pl) in plan.poles.iter().enumerate() {
        let zg = pl.z.unwrap_or(h.poles[i]);
        if pl.wall {
            // a rosette on the wall's face, facing the street
            rows.push(Row { group: G_ROSETTE, x: round_to(pl.fix[0], 2), y: round_to(pl.fix[1], 2), z: round_to(zg + WALL_Z, 2),
                            yaw: round_to(pl.normal[1].datan2(pl.normal[0]).to_degrees().rem_euclid(360.0), 1), scale: 1.0,
                            height: 1.0, owner: pl.fix });
            continue;
        }
        let yaw = round_to(yaw_to(pl.q, pl.toward), 1);
        let kind = if pl.kind == PoleKind::Centre { "centre" } else { "span" };
        rows.push(Row { group: group(pl.family, kind), x: round_to(pl.q[0], 2), y: round_to(pl.q[1], 2), z: round_to(zg, 2), yaw,
                        scale: 1.0, height: MODEL_H, owner: pl.q });
        if pl.kind == PoleKind::Street {
            // the street's lamp is on the pole (the lamps within LAMP_CLEAR_M of it are left out: tram_near)
            rows.push(Row { group: lamp_group(pl.family), x: round_to(pl.q[0], 2),
                            y: round_to(pl.q[1], 2), z: round_to(zg, 2), yaw, scale: 1.0, height: MODEL_H, owner: pl.q });
        }
    }
    let mut lines: Vec<Line> = Vec::new();
    for (pi, pp) in plan.parts.iter().enumerate() {
        let nodes = &pp.nodes;
        let xy = sc.parts[pp.part].xy.as_slice();
        let arcs = arc(xy);
        let zc: Vec<f64> = nodes.iter().enumerate().map(|(k, nd)| {
            let za = nd.z.unwrap_or(h.p[pi][k]);
            if nd.kind == Kind::Mast {
                clip(h.q[pi][k], za + MAST_Z_BAND.0, za + MAST_Z_BAND.1) + Z_ARM
            } else {
                za + CONTACT_Z
            }
        }).collect();
        for (k, nd) in nodes.iter().enumerate() {
            if nd.kind == Kind::Mast && nd.centre.is_none() {
                let yaw = nd.dir[1].datan2(nd.dir[0]).to_degrees().rem_euclid(360.0);
                let zb = clip(h.q[pi][k], h.p[pi][k] + MAST_Z_BAND.0, h.p[pi][k] + MAST_Z_BAND.1);
                rows.push(Row { group: group(nd.family, "arm"), x: round_to(nd.q[0], 2), y: round_to(nd.q[1], 2), z: round_to(zb, 2),
                                yaw: round_to(yaw, 1), scale: 1.0, height: MODEL_H, owner: nd.q });
            }
        }
        let mut pieces = Vec::new();
        for k in 0..nodes.len() - 1 {
            if let Some(pc) = piece(xy, &arcs, &nodes[k], &nodes[k + 1], zc[k], zc[k + 1]) {
                wires.push(Wire { pts: pc.pts.clone(), r: CONTACT_R, owner: nodes[k].p });
                pieces.push(pc);
            }
        }
        let n = nodes.len();
        let ends = [(nodes[0].p, zc[0], tangent_out(xy, &arcs, true)), (nodes[n - 1].p, zc[n - 1], tangent_out(xy, &arcs, false))];
        lines.push(Line { xy, arc: arcs, pieces, ends });
        // the span wires: the fixing's insulator, the tensioner spring at one end, the wire on to the hanger
        for (k, nd) in nodes.iter().enumerate() {
            if nd.kind != Kind::Span || nd.bay.is_some() {
                continue;
            }
            let mid = [nd.p[0], nd.p[1], zc[k] + HANGER_M];
            let fix: Vec<(P3, bool)> = nd.anchors.iter().map(|&i| {
                let pl = &plan.poles[i];
                let zf = pl.z.unwrap_or(h.poles[i]) + if pl.wall { WALL_Z } else { SPAN_Z };
                ([pl.fix[0], pl.fix[1], zf], pl.wall)
            }).collect();
            // the spring on the end hashed from the span's point (a wall end has none: the spring is on the pole's)
            let pick = if fix.len() == 2 {
                let key = format!("{:.1},{:.1}", nd.p[0], nd.p[1]);
                let mut c = if h01(&key, "spring") < 0.5 { 0 } else { 1 };
                if fix[c].1 && !fix[1 - c].1 {
                    c = 1 - c;
                }
                c
            } else {
                0
            };
            let mut ends = Vec::new();
            for (e, (a, wall)) in fix.iter().enumerate() {
                ends.push(span_end(*a, mid, e == pick && !*wall, nd.p, &mut wires));
            }
            let pts = if ends.len() == 2 { vec![ends[0], mid, ends[1]] } else { vec![ends[0], mid] };
            wires.push(Wire { pts, r: SPAN_R, owner: nd.p });
            wires.push(Wire { pts: vec![[nd.p[0], nd.p[1], zc[k]], mid], r: DROPPER_R, owner: nd.p });
        }
    }
    bays(plan, h, &lines, &mut wires);
    pull_offs(sc, plan, h, &lines, &mut wires);
    joints(sc, plan, h, &lines, &mut rows, &mut wires);
    crossings(&lines, &mut wires);
    rail_crossings(sc, &lines, &mut wires);
    (rows, wires)
}

/// The fixings a pull-off cable may run to: each pole's span fixing (or rosette), a mast's or a centre pole's shaft.
fn fixings(plan: &Plan, h: &Heights) -> Vec<P3> {
    let mut out = Vec::new();
    for (i, pl) in plan.poles.iter().enumerate() {
        let zg = pl.z.unwrap_or(h.poles[i]);
        out.push(if pl.wall {
            [pl.fix[0], pl.fix[1], zg + WALL_Z]
        } else if pl.kind == PoleKind::Centre {
            [pl.q[0], pl.q[1], zg + 7.0]
        } else {
            [pl.fix[0], pl.fix[1], zg + SPAN_Z]
        });
    }
    for (pi, pp) in plan.parts.iter().enumerate() {
        for (k, nd) in pp.nodes.iter().enumerate() {
            if nd.kind == Kind::Mast && nd.centre.is_none() {
                out.push([nd.q[0], nd.q[1], h.q[pi][k] + 7.0]);
            }
        }
    }
    out
}

/// Pull-offs: at a bend of the axis the wire follows, a thin cable from the wire's vertex outward to the nearest pole or
/// rosette on the outer side (within PULL_REACH_M), at most one every PULL_SPACING_M of a piece.
fn pull_offs(sc: &Scene, plan: &Plan, h: &Heights, lines: &[Line], wires: &mut Vec<Wire>) {
    let fix = fixings(plan, h);
    if fix.is_empty() {
        return;
    }
    for ln in lines {
        for pc in &ln.pieces {
            let mut last_run = f64::NEG_INFINITY;
            let mut run = 0.0;
            let mut runs = vec![0.0; pc.pts.len()];
            for i in 1..pc.pts.len() {
                run += hyp(pc.pts[i][0] - pc.pts[i - 1][0], pc.pts[i][1] - pc.pts[i - 1][1]);
                runs[i] = run;
            }
            for &i in &pc.pulls {
                if i == 0 || i + 1 >= pc.pts.len() || runs[i] - last_run < PULL_SPACING_M {
                    continue;
                }
                let (a, v, b) = (pc.pts[i - 1], pc.pts[i], pc.pts[i + 1]);
                // (outward: the normal of the chord a-b on the vertex's side, how far the vertex is off the chord the bend)
                let t = [b[0] - a[0], b[1] - a[1]];
                let tl = hyp(t[0], t[1]).max(1e-9);
                let nrm = [-t[1] / tl, t[0] / tl];
                let off = (v[0] - a[0]) * nrm[0] + (v[1] - a[1]) * nrm[1];
                if off.abs() < PULL_MIN_BEND_M {
                    continue;
                }
                let u = [nrm[0] * off.signum(), nrm[1] * off.signum()];
                let mut best: Option<(f64, P3)> = None;
                for f in &fix {
                    let (dx, dy) = (f[0] - v[0], f[1] - v[1]);
                    let d = hyp(dx, dy);
                    if d >= 1.5 && d <= PULL_REACH_M && dx * u[0] + dy * u[1] > 0.0 && best.map_or(true, |b| d < b.0)
                        && !crosses_a_track(sc, [v[0], v[1]], [f[0], f[1]]) {
                        best = Some((d, *f));
                    }
                }
                if let Some((_d, f)) = best {
                    wires.push(Wire { pts: vec![v, f], r: SPAN_R, owner: [v[0], v[1]] });
                    last_run = runs[i];
                }
            }
        }
    }
}

/// Whether the segment a-b crosses a ground tram track other than the one it starts on (a cable across a yard's tracks).
fn crosses_a_track(sc: &Scene, a: P2, b: P2) -> bool {
    let len = hyp(b[0] - a[0], b[1] - a[1]);
    sc.tracks.iter().any(|l| l.windows(2).any(|w| cross2(a, b, w[0], w[1]).map_or(false, |(_c, t, _u)| t * len > 0.6)))
}

/// The cross-spans of the portals: for each bay, one wire from its first pole's fixing through a hanger over every track it
/// carries to its last pole's, with the insulators and the spring, and a dropper to each contact wire.
fn bays(plan: &Plan, h: &Heights, lines: &[Line], wires: &mut Vec<Wire>) {
    let _ = lines;
    for (bi, bay) in plan.bays.iter().enumerate() {
        let (pa, pb) = (&plan.poles[bay.a], &plan.poles[bay.b]);
        let fix = |i: usize, pl: &Pole| [pl.fix[0], pl.fix[1], pl.z.unwrap_or(h.poles[i]) + SPAN_Z];
        let (fa, fb) = (fix(bay.a, pa), fix(bay.b, pb));
        let axis = [fb[0] - fa[0], fb[1] - fa[1]];
        let al = hyp(axis[0], axis[1]).max(1e-9);
        let mut hang: Vec<(f64, P3, P3)> = Vec::new(); // (along the bay, the hanger point, the contact wire's point)
        for (pi, pp) in plan.parts.iter().enumerate() {
            for (k, nd) in pp.nodes.iter().enumerate() {
                if nd.bay != Some(bi) {
                    continue;
                }
                let za = nd.z.unwrap_or(h.p[pi][k]) + CONTACT_Z;
                let t = ((nd.p[0] - fa[0]) * axis[0] + (nd.p[1] - fa[1]) * axis[1]) / al;
                hang.push((t, [nd.p[0], nd.p[1], za + HANGER_M], [nd.p[0], nd.p[1], za]));
            }
        }
        if hang.is_empty() {
            continue;
        }
        hang.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap_or(std::cmp::Ordering::Equal));
        let owner = [fa[0], fa[1]];
        let key = format!("{:.1},{:.1}", fa[0], fa[1]);
        let spring_a = h01(&key, "spring") < 0.5;
        let first = hang[0].1;
        let last = hang[hang.len() - 1].1;
        let ea = span_end(fa, first, spring_a, owner, wires);
        let eb = span_end(fb, last, !spring_a, owner, wires);
        let mut pts = vec![ea];
        pts.extend(hang.iter().map(|x| x.1));
        pts.push(eb);
        wires.push(Wire { pts, r: SPAN_R, owner });
        for (_t, top, bot) in &hang {
            wires.push(Wire { pts: vec![*bot, *top], r: DROPPER_R, owner: [bot[0], bot[1]] });
        }
    }
}

/// A tram wire over a railway track's axis: an insulated section (the line's wires are another voltage) and a clamp.
fn rail_crossings(sc: &Scene, lines: &[Line], wires: &mut Vec<Wire>) {
    let mut found: Vec<P2> = Vec::new();
    for ln in lines {
        for pc in &ln.pieces {
            for s in pc.pts.windows(2) {
                for rail in &sc.rails {
                    for r in rail.windows(2) {
                        let Some((c, t, _u)) = cross2([s[0][0], s[0][1]], [s[1][0], s[1][1]], r[0], r[1]) else { continue };
                        if found.iter().any(|f| hyp(f[0] - c[0], f[1] - c[1]) < 3.0) {
                            continue;
                        }
                        found.push(c);
                        let d = [s[1][0] - s[0][0], s[1][1] - s[0][1]];
                        let dl = hyp(d[0], d[1]).max(1e-9);
                        let u = [d[0] / dl, d[1] / dl];
                        let p = [c[0], c[1], s[0][2] + (s[1][2] - s[0][2]) * t];
                        wires.push(bar(p, u, RAIL_INSULATED_M, INS_R, c));
                        wires.push(bar(p, u, SWITCH_CLAMP_M, CLAMP_R, c));
                    }
                }
            }
        }
    }
}

/// An end of a part's wire.
struct End {
    part: usize,
    p: P3,
    dir: P2,
    which: usize,
}

/// Where the line's parts meet: ends within JOINT_M are one line (a link where they are not the same point, a clamp
/// where three or more meet: a switch), an end near another's side starts on its wire (a branch, with the clamp), an end
/// that meets nothing is anchored (an insulator, a guy to the nearest support).
fn joints(sc: &Scene, plan: &Plan, h: &Heights, lines: &[Line], rows: &mut Vec<Row>, wires: &mut Vec<Wire>) {
    let mut ends: Vec<End> = Vec::new();
    for (pi, ln) in lines.iter().enumerate() {
        if ln.pieces.is_empty() {
            continue;
        }
        for w in 0..2 {
            let (p, z, dir) = ln.ends[w];
            ends.push(End { part: pi, p: [p[0], p[1], z], dir, which: w });
        }
    }
    let n = ends.len();
    // union-find over the ends of different parts within JOINT_M
    let mut root: Vec<usize> = (0..n).collect();
    fn find(root: &mut Vec<usize>, i: usize) -> usize {
        let mut r = i;
        while root[r] != r {
            r = root[r];
        }
        let mut c = i;
        while root[c] != r {
            let nx = root[c];
            root[c] = r;
            c = nx;
        }
        r
    }
    let mut linked = vec![false; n];
    for i in 0..n {
        for j in i + 1..n {
            if ends[i].part != ends[j].part && hyp(ends[i].p[0] - ends[j].p[0], ends[i].p[1] - ends[j].p[1]) <= JOINT_M
                && (ends[i].p[2] - ends[j].p[2]).abs() <= LINK_DZ_MAX_M {
                linked[i] = true;
                linked[j] = true;
                let (a, b) = (find(&mut root, i), find(&mut root, j));
                root[a.max(b)] = a.min(b);
                if hyp(ends[i].p[0] - ends[j].p[0], ends[i].p[1] - ends[j].p[1]) > 0.02 || (ends[i].p[2] - ends[j].p[2]).abs() > 0.02 {
                    wires.push(Wire { pts: vec![ends[i].p, ends[j].p], r: CONTACT_R, owner: [ends[i].p[0], ends[i].p[1]] });
                }
            }
        }
    }
    // a switch: three or more ends meet at one place
    for i in 0..n {
        let r = find(&mut root, i);
        if r != i {
            continue;
        }
        let members: Vec<usize> = (0..n).filter(|&j| find(&mut root, j) == r).collect();
        if members.len() >= 3 {
            let (mut cx, mut cy, mut cz) = (0.0, 0.0, 0.0);
            for &m in &members {
                cx += ends[m].p[0];
                cy += ends[m].p[1];
                cz += ends[m].p[2];
            }
            let k = members.len() as f64;
            let c = [cx / k, cy / k, cz / k];
            let u = ends[members[0]].dir;
            wires.push(bar(c, [-u[0], -u[1]], SWITCH_CLAMP_M, CLAMP_R, [c[0], c[1]]));
        }
    }
    // a branch: an end near another part's axis (not near its ends) starts on that part's wire
    for i in 0..n {
        if linked[i] {
            continue;
        }
        let e = &ends[i];
        let mut best: Option<(f64, usize, f64, P2, P2)> = None;
        for (pj, other) in lines.iter().enumerate() {
            if pj == e.part || other.pieces.is_empty() {
                continue;
            }
            let (s, q, u, d) = nearest(other.xy, &other.arc, [e.p[0], e.p[1]]);
            if d <= JOINT_M * 2.0 && s > 1.0 && s < other.arc[other.arc.len() - 1] - 1.0 && best.map_or(true, |b| d < b.0) {
                best = Some((d, pj, s, q, u));
            }
        }
        if let Some((_d, pj, s, q, u)) = best {
            if let Some(z) = lines[pj].z_at(s) {
                let qz = [q[0], q[1], z];
                wires.push(Wire { pts: vec![e.p, qz], r: CONTACT_R, owner: [e.p[0], e.p[1]] });
                wires.push(bar(qz, u, SWITCH_CLAMP_M, CLAMP_R, [e.p[0], e.p[1]]));
                linked[i] = true;
            }
        }
    }
    // an end that meets nothing: anchored (an insulator ends the wire, a steel guy runs on to the nearest support)
    for i in 0..n {
        if linked[i] {
            continue;
        }
        let e = &ends[i];
        let pp = &plan.parts[e.part];
        let node_ix = if e.which == 0 { 1 } else { pp.nodes.len().saturating_sub(2) };
        let out3 = [e.dir[0], e.dir[1], 0.0];
        let ins_end = at3(e.p, out3, INS_L);
        let owner = [e.p[0], e.p[1]];
        wires.push(Wire { pts: vec![e.p, ins_end], r: INS_R, owner });
        let target: Option<P3> = pp.nodes.get(node_ix).and_then(|nd| match nd.kind {
            Kind::Span => nd.anchors.first().map(|&a| {
                let pl = &plan.poles[a];
                [pl.fix[0], pl.fix[1], pl.z.unwrap_or(h.poles[a]) + if pl.wall { WALL_Z } else { SPAN_Z }]
            }),
            Kind::Mast => Some([nd.q[0], nd.q[1], h.q[e.part][node_ix] + MODEL_H - 0.6]),
            Kind::End => None,
        });
        let near = target.filter(|t| {
            let d = len3(sub3(*t, ins_end));
            d > 1.0 && d <= ANCHOR_REACH_M
        });
        if let Some(t) = near {
            wires.push(Wire { pts: vec![ins_end, t], r: HANGER_R, owner });
        } else if let Some((q, base)) = end_pole(sc, pp.part, [e.p[0], e.p[1]], e.dir) {
            // nothing near to hold it: a pole of its own at the line's end (the cantilever mast's model, its arm to the wire)
            let zb = e.p[2] - Z_ARM;
            let fam = family_of(&sc.country, &sc.parts[pp.part].fid);
            rows.push(Row { group: group(fam, "arm"), x: round_to(q[0], 2), y: round_to(q[1], 2), z: round_to(zb, 2),
                            yaw: round_to(yaw_to(q, base), 1), scale: 1.0, height: MODEL_H, owner: q });
            wires.push(Wire { pts: vec![ins_end, [q[0], q[1], zb + MODEL_H - 0.6]], r: HANGER_R, owner });
        }
    }
}

/// Contact wires of different parts that cross at (nearly) one height away from the parts' ends: a crossing clamp.
fn crossings(lines: &[Line], wires: &mut Vec<Wire>) {
    let mut found: Vec<P2> = Vec::new();
    for a in 0..lines.len() {
        for b in a + 1..lines.len() {
            let (la, lb) = (&lines[a], &lines[b]);
            for pa in &la.pieces {
                for sa in pa.pts.windows(2) {
                    for pb in &lb.pieces {
                        for sb in pb.pts.windows(2) {
                            let Some((c, t, u)) = cross2([sa[0][0], sa[0][1]], [sa[1][0], sa[1][1]], [sb[0][0], sb[0][1]], [sb[1][0], sb[1][1]]) else {
                                continue;
                            };
                            let (za, zb) = (sa[0][2] + (sa[1][2] - sa[0][2]) * t, sb[0][2] + (sb[1][2] - sb[0][2]) * u);
                            if (za - zb).abs() > CROSS_DZ_M {
                                continue;
                            }
                            // (not at a joint: the ends of the parts are where they meet, not cross)
                            let near_end = [la, lb].iter().any(|ln| ln.ends.iter().any(|e| hyp(e.0[0] - c[0], e.0[1] - c[1]) < CROSS_END_M));
                            if near_end || found.iter().any(|f| hyp(f[0] - c[0], f[1] - c[1]) < 1.0) {
                                continue;
                            }
                            found.push(c);
                            let d = [sb[1][0] - sb[0][0], sb[1][1] - sb[0][1]];
                            let dl = (d[0] * d[0] + d[1] * d[1]).sqrt().max(1e-9);
                            wires.push(bar([c[0], c[1], (za + zb) / 2.0], [d[0] / dl, d[1] / dl], SWITCH_CLAMP_M, CLAMP_R, c));
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn straight(y: f64, x0: f64, x1: f64) -> Vec<P2> {
        let n = ((x1 - x0) / 2.0) as usize;
        (0..=n).map(|i| [x0 + 2.0 * i as f64, y]).collect()
    }

    fn scene(parts: Vec<Vec<P2>>, roads: Vec<Road>) -> Scene {
        Scene {
            parts: parts.into_iter().enumerate().map(|(i, xy)| Part { fid: format!("t{i}"), bridge: false, xy }).collect(),
            roads,
            rings: vec![],
            decks: vec![],
            country: "DE".into(),
            rails: vec![],
            tracks: vec![],
        }
    }

    fn run(sc: &Scene) -> (Vec<Row>, Vec<Wire>) {
        let plan = plan(sc);
        let z = vec![100.0; ground_points(&plan).len()];
        let h = split_heights(&plan, &z);
        build(sc, &plan, &h)
    }

    fn count(w: &[Wire], r: f64) -> usize {
        w.iter().filter(|x| x.r == r).count()
    }

    #[test]
    fn a_curve_is_followed_by_the_wire_not_cut_across() {
        // a quarter circle of R 30 m as a street tram's axis: the contact wire stays within the pull-off tolerance of it
        let xy: Vec<P2> = (0..=50).map(|i| { let t = i as f64 * 0.0314; [30.0 * t.dsin() - 150.0, 30.0 * (1.0 - t.dcos())] }).collect();
        let sc = scene(vec![xy.clone()], vec![]);
        let (_r, w) = run(&sc);
        let contact: Vec<&Wire> = w.iter().filter(|x| x.r == CONTACT_R).collect();
        assert!(!contact.is_empty());
        for wi in contact {
            for p in &wi.pts {
                assert!(dist_to_line([p[0], p[1]], &xy) < 0.15, "off the axis by {}", dist_to_line([p[0], p[1]], &xy));
            }
        }
    }

    #[test]
    fn two_parts_meeting_with_a_gap_are_linked_and_an_end_alone_is_anchored() {
        let a = straight(0.0, -150.0, 100.0);
        let b = straight(0.0, 101.0, 350.0); // a gap of ~1 m at x = 100..101
        let sc = scene(vec![a, b], vec![]);
        let (_r, w) = run(&sc);
        let links: Vec<&Wire> = w.iter().filter(|x| x.r == CONTACT_R && x.pts.len() == 2 && (x.pts[1][0] - x.pts[0][0]).abs() < 1.6 && x.pts[0][0] > 99.0 && x.pts[0][0] < 102.0).collect();
        assert_eq!(links.len(), 1, "one link over the gap");
        // two true ends (x = -150, 350): an insulator each
        assert_eq!(count(&w, INS_R), 2);
    }

    #[test]
    fn a_branch_starts_on_the_main_wire_with_a_clamp() {
        let main = straight(0.0, -150.0, 150.0);
        let mut branch: Vec<P2> = vec![[0.5, 0.2]];
        for i in 1..=40 {
            let t = i as f64 * 2.0;
            branch.push([0.5 + t, 0.2 + 0.002 * t * t]);
        }
        let sc = scene(vec![main, branch], vec![]);
        let (_r, w) = run(&sc);
        // the branch's start is joined to the main wire: a short wire to its axis point and a clamp there
        assert!(w.iter().any(|x| x.r == CLAMP_R && x.pts[0][0] > -2.0 && x.pts[0][0] < 3.0), "a clamp at the switch");
        assert!(w.iter().any(|x| x.r == CONTACT_R && x.pts.len() == 2 && hyp(x.pts[0][0] - x.pts[1][0], x.pts[0][1] - x.pts[1][1]) < 0.6 + 1e-9 && x.pts[0][0].abs() < 3.0));
    }

    #[test]
    fn crossing_tracks_get_a_clamp_where_the_wires_cross() {
        let a = straight(0.0, -150.0, 150.0);
        let b: Vec<P2> = (0..=100).map(|i| [-20.0 + 0.4 * i as f64, -50.0 + 1.0 * i as f64]).collect();
        let sc = scene(vec![a, b], vec![]);
        let (_r, w) = run(&sc);
        let clamps: Vec<&Wire> = w.iter().filter(|x| x.r == CLAMP_R).collect();
        assert_eq!(clamps.len(), 1);
        assert!(clamps[0].owner[1].abs() < 0.5);
    }

    #[test]
    fn span_wires_have_insulators_and_one_spring() {
        let road = Road { pts: vec![[-150.0, 0.0], [350.0, 0.0]], hw: 4.0 };
        let sc = scene(vec![straight(0.0, -100.0, 300.0)], vec![road]);
        let (rows, w) = run(&sc);
        assert!(!rows.is_empty());
        let spans = count(&w, SPAN_R);
        assert!(spans >= 8);
        assert_eq!(count(&w, INS_R), 2 * spans + 2, "two insulators a span (and the two ends'), none missing");
        assert_eq!(count(&w, SPRING_R), spans, "one spring a span");
        for x in w.iter().filter(|x| x.r == SPRING_R) {
            assert_eq!(x.pts.len(), SPRING_SEGS + 1);
        }
    }

    #[test]
    fn the_contact_wire_is_continuous_and_at_the_pantographs_height() {
        let sc = scene(vec![straight(0.0, -100.0, 300.0)], vec![Road { pts: vec![[-150.0, 0.0], [350.0, 0.0]], hw: 4.0 }]);
        let (_r, w) = run(&sc);
        let mut c: Vec<&Wire> = w.iter().filter(|x| x.r == CONTACT_R).collect();
        c.sort_by(|a, b| a.pts[0][0].partial_cmp(&b.pts[0][0]).unwrap());
        for pair in c.windows(2) {
            let (e, s) = (pair[0].pts.last().unwrap(), pair[1].pts[0]);
            assert!(hyp(e[0] - s[0], e[1] - s[1]) < 1e-9 && (e[2] - s[2]).abs() < 1e-9);
        }
        for x in c {
            for p in &x.pts {
                assert!((p[2] - (100.0 + CONTACT_Z)).abs() < 1e-9);
            }
        }
    }

    #[test]
    fn rows_name_the_models_of_the_family_and_a_street_pole_carries_a_lamp() {
        let road = Road { pts: vec![[-150.0, 0.0], [350.0, 0.0]], hw: 4.0 };
        let sc = scene(vec![straight(0.0, -100.0, 300.0)], vec![road]);
        let (rows, _w) = run(&sc);
        let poles = rows.iter().filter(|r| r.group == "tram_tube_span").count();
        assert!(poles >= 8);
        assert_eq!(rows.iter().filter(|r| r.group == G_LAMP_GALV).count(), poles, "a lamp bracket on each");
        assert!(rows.iter().all(|r| r.group.starts_with("tram_")));
        assert!(rows.iter().all(|r| r.height == MODEL_H));
    }

    #[test]
    fn a_centre_pole_is_one_row_and_both_wires_reach_its_arms() {
        let sc = scene(vec![straight(0.0, -100.0, 300.0), straight(4.0, -100.0, 300.0)], vec![]);
        let (rows, w) = run(&sc);
        let centre = rows.iter().filter(|r| r.group == "tram_tube_centre").count();
        assert!(centre >= 8);
        assert!(rows.iter().filter(|r| r.group == "tram_tube_arm").all(|r| r.x < -80.0 || r.x > 280.0), "a lone arm only at a line's end (its end pole)");
        let c = w.iter().filter(|x| x.r == CONTACT_R).count();
        assert!(c >= 2 * centre);
    }

    #[test]
    fn a_curve_gets_pull_off_cables_to_poles_on_its_outer_side() {
        // a street tram on a curve of R 40 m, the road along it: pull-offs run from the wire out to the outer poles
        let xy: Vec<P2> = (0..=80).map(|i| { let t = i as f64 * 0.0196; [40.0 * t.dsin() - 150.0, 40.0 * (1.0 - t.dcos())] }).collect();
        let road = Road { pts: xy.clone(), hw: 4.0 };
        let sc = scene(vec![xy.clone()], vec![road]);
        let (_r, w) = run(&sc);
        let pulls: Vec<&Wire> = w.iter().filter(|x| x.r == SPAN_R && x.pts.len() == 2 && x.pts[0][2] == 100.0 + CONTACT_Z).collect();
        assert!(!pulls.is_empty(), "pull-off cables");
        for p in pulls {
            // outward from the curve's centre (0, 40 - 150..): the fixing is farther from the centre than the wire
            let c = [-150.0, 40.0];
            let u = [p.pts[0][0] - c[0], p.pts[0][1] - c[1]];
            assert!((p.pts[1][0] - p.pts[0][0]) * u[0] + (p.pts[1][1] - p.pts[0][1]) * u[1] > 0.0, "outward: {:?} {:?}", p.pts[0], p.pts[1]);
            assert!(hyp(p.pts[1][0] - p.pts[0][0], p.pts[1][1] - p.pts[0][1]) <= PULL_REACH_M);
        }
    }

    #[test]
    fn a_line_end_with_no_support_near_gets_a_pole_of_its_own() {
        // a track of its own, nothing but a mast far down it: both ends are anchored to poles at the ends
        let xy = straight(0.0, 0.0, 12.0);
        let a = arc(&xy);
        let fid = (0..200).map(|i| format!("e{i}")).find(|f| stations(&xy, &a, f).is_empty()).unwrap();
        let mut sc = scene(vec![xy], vec![]);
        sc.parts[0].fid = fid;
        let (rows, w) = run(&sc);
        let arms: Vec<&Row> = rows.iter().filter(|r| r.group == "tram_tube_arm").collect();
        assert_eq!(arms.len(), 2, "an end pole each end");
        assert!(arms.iter().any(|r| r.x < 4.0) && arms.iter().any(|r| r.x > 8.0));
        assert!(w.iter().any(|x| x.r == HANGER_R), "the guy to it");
    }

    #[test]
    fn a_tram_wire_over_a_railway_is_insulated_and_no_pole_stands_on_the_rails() {
        let mut sc = scene(vec![straight(0.0, -100.0, 300.0)], vec![Road { pts: vec![[-150.0, 0.0], [350.0, 0.0]], hw: 4.0 }]);
        sc.rails = vec![vec![[100.0, -40.0], [100.0, 40.0]]];
        let (rows, w) = run(&sc);
        let ins: Vec<&Wire> = w.iter().filter(|x| x.r == INS_R && x.pts.len() == 2 && (x.pts[1][0] - x.pts[0][0] - RAIL_INSULATED_M).abs() < 1e-6).collect();
        assert_eq!(ins.len(), 1);
        assert!((ins[0].owner[0] - 100.0).abs() < 1e-6);
        for r in rows.iter().filter(|r| r.group.starts_with("tram_") && !r.group.starts_with("tram_lamp")) {
            assert!((r.x - 100.0).abs() >= RAIL_CLEAR_M - 0.01, "off the railway: {}", r.x);
        }
    }

    #[test]
    fn a_portal_has_one_cross_span_per_bay_with_a_dropper_for_every_track() {
        let sc = Scene {
            parts: (0..6).map(|i| Part { fid: format!("y{i}"), bridge: false, xy: straight(i as f64 * 4.2, -100.0, 300.0) }).collect(),
            roads: vec![],
            rings: vec![],
            decks: vec![],
            country: "DE".into(),
            rails: vec![],
            tracks: (0..6).map(|i| straight(i as f64 * 4.2, -100.0, 300.0)).collect(),
        };
        let (rows, w) = run(&sc);
        let plan = plan(&sc);
        assert!(!plan.bays.is_empty());
        let spans: Vec<&Wire> = w.iter().filter(|x| x.r == SPAN_R && x.pts.len() >= 3).collect();
        assert!(spans.len() >= plan.bays.len().min(1));
        // every track hangs from a cross-span or a centre pole's arm; a span's points: its two ends and its hangers
        for s in &spans {
            for p in &s.pts[1..s.pts.len() - 1] {
                assert!((p[2] - (100.0 + CONTACT_Z + HANGER_M)).abs() < 1e-9);
            }
        }
        assert!(w.iter().filter(|x| x.r == DROPPER_R).count() >= 6);
        assert!(rows.iter().all(|r| !r.group.starts_with("tram_tube_span") || !r.group.contains("lamp")));
        // portal poles carry no lamp
        assert_eq!(rows.iter().filter(|r| r.group.starts_with("tram_lamp")).count(), 0);
    }

    #[test]
    fn a_pull_off_cable_does_not_run_across_another_track() {
        let sc = Scene {
            parts: vec![Part { fid: "t".into(), bridge: false, xy: (0..=80).map(|i| { let t = i as f64 * 0.0196; [40.0 * t.dsin() - 150.0, 40.0 * (1.0 - t.dcos())] }).collect() }],
            roads: vec![],
            rings: vec![],
            decks: vec![],
            country: "DE".into(),
            rails: vec![],
            tracks: vec![
                (0..=80).map(|i| { let t = i as f64 * 0.0196; [40.0 * t.dsin() - 150.0, 40.0 * (1.0 - t.dcos())] }).collect(),
                (0..=80).map(|i| { let t = i as f64 * 0.0196; [46.0 * t.dsin() - 150.0, 40.0 - 46.0 * t.dcos()] }).collect(),
            ],
        };
        let (_r, w) = run(&sc);
        for x in w.iter().filter(|x| x.r == SPAN_R && x.pts.len() == 2 && x.pts[0][2] == 100.0 + CONTACT_Z) {
            assert!(!crosses_a_track(&sc, [x.pts[0][0], x.pts[0][1]], [x.pts[1][0], x.pts[1][1]]));
        }
    }
}
