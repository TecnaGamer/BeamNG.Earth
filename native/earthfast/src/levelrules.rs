//! roads.level_ranges' rules after the map's tags, a layer at a time (the road-semantics port's steps 2-4,
//! .claude/work/road_semantics_rust_plan.md): a bridge range lengthened over the roads crossing under it and onto
//! the road joined at its end (_skew_decks, _bridge_reach), a tunnel's portals moved past the roads over them
//! (_tunnel_reach), the short ground gaps between decks and between a portal and a deck made deck (_deck_gaps,
//! _portal_decks), the lidar's elevated stretches (_lidar_gaps), the grade separations' ranges merged into the map's
//! (_level_ranges) and the ground pieces at a deck's end the lidar is read along (lidar_gap_candidates). As ~100k small
//! Python calls a dense cell (dict lookups, small numpy arrays) they were ~30 % of a cold Baltimore road tile's
//! planning (py-spy, 2026-11-10); here a cell's are 3.5-5x quicker. The Python's numbers bit for bit: numpy's pairwise
//! sums and interp, libm's hypot / cos, Python's min / max (the first of equals); checked against it on 225k road
//! features of 198 cells in 20 places and 2k synthetic ones (the commit's message).
//!
//! Kinds: 1 bridge, 2 tunnel (so ranges sort as Python's ("bridge" < "tunnel") tuples), 3 ground (level_extras' input).
use crate::dm::Dm;

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::decks::{deck_gaps_round, fill_gaps};
use crate::gradesep::np_interp;
use crate::levels::{cmp_r, level_rule, level_spans, np_sum, R};
use crate::tunnelcover::tunnel_reach_core;
use crate::{ends_near_core, seg_hits_core};

const BRIDGE: i8 = 1;
const TUNNEL: i8 = 2;
const GROUND: i8 = 3;
const KY: f64 = 111320.0;
/// m: a road met within this of an end of a bridge's stretch is the road joined to it there (the junction at the
/// bridge's end node: the same map point), not a road under the bridge (make_reach). The crossing's place along the
/// bridge and the stretch's end are both sums of hypot()s, the same number only to the last bit, and which side of
/// the end it fell on depended on the C library's hypot (glibc 2.35's and 2.44's differ in the last bit): at
/// Vienna's S1 / Rautenweg a ramp's bridge (203 m, a road joined at its end) took that road for a crossing on
/// Ubuntu 22.04 and reached 1.6 m further back and 18.8 m past its end, not on the PC: other decks there, the
/// release workflow's test_drivability failing on GitHub's runner while the PC's gate passed (2026-11-12). On
/// every system some did by the same chance (Porte Maillot: two bridges of six with a road joined at their end,
/// one of them its own road going on at 3.6 degrees: a 30 m longer deck); a road joined at the start never did
/// (its place is exactly 0). A micrometre: a million times the rounding of a km-long line's metres, far under the
/// map's ~1 cm coordinate step.
const END_EPS_M: f64 = 1e-6;

type P = [f64; 2];

/// Python's max(a, b): a unless b > a.
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's min(a, b): a unless b < a.
fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// numpy's minimum / maximum (NaN wins).
fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a }
}

fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}

/// np.mean of a polyline's latitudes.
fn mean_lat(r: &[P]) -> f64 {
    let v: Vec<f64> = r.iter().map(|p| p[1]).collect();
    np_sum(&v) / r.len() as f64
}

/// m per degree of longitude along lon / lat polyline r as sub_polyline measures it (at its mean latitude): its level
/// ranges' fractions. At its first point instead, a 15 km motorway's crossing came 8.7 m along from where its deck was
/// cut (the A4 at Glaireuse, #1020).
fn ring_kx(r: &[P]) -> f64 {
    111320.0 * mean_lat(r).to_radians().dcos()
}

/// np.r_[0, np.cumsum(np.dhypot(*np.diff(m, axis=0).T))].
fn arcs(m: &[P]) -> Vec<f64> {
    let mut s = vec![0.0f64; m.len().max(1)];
    for k in 1..m.len() {
        s[k] = s[k - 1] + (m[k][0] - m[k - 1][0]).dhypot(m[k][1] - m[k - 1][1]);
    }
    s
}

/// float(np.dhypot(*np.diff(m, axis=0).T).sum()).
fn length_np(m: &[P]) -> f64 {
    let seg: Vec<f64> = (1..m.len()).map(|k| (m[k][0] - m[k - 1][0]).dhypot(m[k][1] - m[k - 1][1])).collect();
    np_sum(&seg)
}

/// float(np.dhypot(*(np.diff(ring, axis=0) * [kx, ky]).T).sum()).
fn length_scaled(r: &[P], kx: f64, ky: f64) -> f64 {
    let seg: Vec<f64> = (1..r.len())
        .map(|k| ((r[k][0] - r[k - 1][0]) * kx).dhypot((r[k][1] - r[k - 1][1]) * ky)).collect();
    np_sum(&seg)
}

/// (ring - ring[0]) * [kx, ky] of points (or of ring itself with o = ring[0]).
fn to_m(r: &[P], o: P, kx: f64, ky: f64) -> Vec<P> {
    r.iter().map(|p| [(p[0] - o[0]) * kx, (p[1] - o[1]) * ky]).collect()
}

fn interp2(x: f64, s: &[f64], xs: &[f64], ys: &[f64]) -> P {
    [np_interp(x, s, xs), np_interp(x, s, ys)]
}

fn cols(r: &[P]) -> (Vec<f64>, Vec<f64>) {
    (r.iter().map(|p| p[0]).collect(), r.iter().map(|p| p[1]).collect())
}

/// roads.sub_polyline: the part of lon / lat polyline r (>= 2 points) between fractions a < b of its length.
pub(crate) fn sub_polyline(r: &[P], a: f64, b: f64) -> Vec<P> {
    let c = mean_lat(r).to_radians().dcos();
    let mut s = vec![0.0f64; r.len()];
    for v in 1..r.len() {
        s[v] = s[v - 1] + ((r[v][0] - r[v - 1][0]) * c).dhypot(r[v][1] - r[v - 1][1]);
    }
    let tot = s[r.len() - 1];
    if tot <= 0.0 {
        return vec![r[0]];
    }
    let (sa, sb) = (a * tot, b * tot);
    let (xs, ys) = cols(r);
    let mut out = vec![interp2(sa, &s, &xs, &ys)];
    for v in 0..r.len() {
        if s[v] > sa && s[v] < sb {
            out.push(r[v]);
        }
    }
    out.push(interp2(sb, &s, &xs, &ys));
    out
}

/// roads.ground_ranges: the on-ground (start, end) fractions of a feature with ranges rngs.
fn ground_pieces(rngs: &[R], min_part: f64) -> Vec<(f64, f64)> {
    let (mut pieces, mut at) = (Vec::new(), 0.0f64);
    for &(a, b, _) in rngs {
        if a - at > min_part {
            pieces.push((at, a));
        }
        at = pmax(at, b);
    }
    if 1.0 - at > min_part {
        pieces.push((at, 1.0));
    }
    pieces
}

/// The pieces (a, b) of each ring of 2 points or more, in order: the ring itself for (0, 1), else sub_polyline.
fn cut_rings(rings: &[&[P]], pieces: &[(f64, f64)], keep_short: bool) -> Vec<Vec<P>> {
    let mut out = Vec::new();
    for r in rings {
        for &(a, b) in pieces {
            let p = if a == 0.0 && b == 1.0 { r.to_vec() } else { sub_polyline(r, a, b) };
            if keep_short || p.len() >= 2 {
                out.push(p);
            }
        }
    }
    out
}

/// roads._seg_dist(p, gm[:-1], gm[1:]).min(): numpy's numbers (NaN wins).
fn seg_dist_min(p: &[P], g: &[P]) -> f64 {
    let mut best = f64::INFINITY;
    let mut first = true;
    for q in p {
        for w in g.windows(2) {
            let (a, b) = (w[0], w[1]);
            let ab = [b[0] - a[0], b[1] - a[1]];
            let l2 = nmax(ab[0] * ab[0] + ab[1] * ab[1], 1e-12);
            let t = ((q[0] - a[0]) * ab[0] + (q[1] - a[1]) * ab[1]) / l2;
            let t = if t.is_nan() { t } else { pmin(pmax(t, 0.0), 1.0) };
            let d = (q[0] - (a[0] + t * ab[0])).dhypot(q[1] - (a[1] + t * ab[1]));
            best = if first { d } else { nmin(best, d) };
            first = false;
        }
    }
    best
}

/// roads._ring_fraction: the fraction of lon / lat polyline r's length (sub_polyline's metric) at its point nearest p.
fn ring_fraction(r: &[P], p: P) -> f64 {
    let c = mean_lat(r).to_radians().dcos();
    let q: Vec<P> = r.iter().map(|v| [v[0] * c, v[1] * 1.0]).collect();
    let pp = [p[0] * c, p[1] * 1.0];
    let n = q.len().saturating_sub(1);
    let (mut l2, mut t, mut d) = (vec![0.0f64; n], vec![0.0f64; n], vec![0.0f64; n]);
    for k in 0..n {
        let (a, b) = (q[k], q[k + 1]);
        let ab = [b[0] - a[0], b[1] - a[1]];
        l2[k] = nmax(ab[0] * ab[0] + ab[1] * ab[1], 1e-30);
        let tk = ((pp[0] - a[0]) * ab[0] + (pp[1] - a[1]) * ab[1]) / l2[k];
        t[k] = if tk.is_nan() { tk } else { pmin(pmax(tk, 0.0), 1.0) };
        d[k] = (a[0] + t[k] * ab[0] - pp[0]).dhypot(a[1] + t[k] * ab[1] - pp[1]);
    }
    // (np.argmin: the first minimum, the first NaN if any)
    let mut k = 0usize;
    for j in 0..n {
        if d[j].is_nan() {
            k = j;
            break;
        }
        if d[j] < d[k] {
            k = j;
        }
    }
    let seg: Vec<f64> = l2.iter().map(|v| v.sqrt()).collect();
    let mut s = vec![0.0f64; n + 1];
    for j in 0..n {
        s[j + 1] = s[j] + seg[j];
    }
    (s[k] + t[k] * seg[k]) / pmax(s[n], 1e-30)
}

/// Sorted ranges joined where one starts within 1e-9 of the last's end (kind: join(last kind, its kind)).
fn join_sorted(mut all: Vec<R>, join: impl Fn(i8, i8) -> i8) -> Vec<R> {
    all.sort_by(cmp_r);
    let mut out: Vec<R> = Vec::with_capacity(all.len());
    for (a, b, k) in all {
        if let Some(last) = out.last_mut() {
            if a <= last.1 + 1e-9 {
                *last = (last.0, pmax(last.1, b), join(last.2, k));
                continue;
            }
        }
        out.push((a, b, k));
    }
    out
}

struct Prm {
    skew_on: bool,
    skew_margin: f64,
    skew_slope: f64,
    skew_max: f64,
    skew_min_sin: f64,
    skew_join: f64,
    gap_on: bool,
    gap_deck: f64,
    gap_junction: f64,
    gap_join: f64,
    portal_deck: f64,
    min_part: f64,
    tunnel: [f64; 7],
    lidar_on: bool,
}

impl Prm {
    fn new(p: &[f64]) -> PyResult<Prm> {
        if p.len() < 20 {
            return Err(pyo3::exceptions::PyValueError::new_err("level_rules: 20 parameters"));
        }
        let mut tunnel = [0.0f64; 7];
        tunnel.copy_from_slice(&p[12..19]);
        Ok(Prm {
            skew_on: p[0] != 0.0, skew_margin: p[1], skew_slope: p[2], skew_max: p[3], skew_min_sin: p[4],
            skew_join: p[5],
            gap_on: p[6] != 0.0, gap_deck: p[7], gap_junction: p[8], gap_join: p[9], portal_deck: p[10],
            min_part: p[11], tunnel, lidar_on: p[19] != 0.0,
        })
    }
}

/// A layer's geometry: lonlat (k, 2) and its offsets (mapgeom.Features).
struct Geo {
    ll: Vec<P>,
    co: Vec<usize>,
    ro: Vec<usize>,
    po: Vec<usize>,
}

impl Geo {
    fn new(lonlat: &PyReadonlyArray2<f64>, co: &PyReadonlyArray1<i64>, ro: &PyReadonlyArray1<i64>,
           po: &PyReadonlyArray1<i64>) -> PyResult<Geo> {
        let ll = lonlat.as_array();
        if ll.ncols() != 2 && ll.nrows() > 0 {
            return Err(pyo3::exceptions::PyValueError::new_err("level rules: lonlat (k, 2)"));
        }
        let us = |a: &PyReadonlyArray1<i64>| a.as_array().iter().map(|&v| v.max(0) as usize).collect::<Vec<_>>();
        Ok(Geo { ll: (0..ll.nrows()).map(|k| [ll[[k, 0]], ll[[k, 1]]]).collect(), co: us(co), ro: us(ro), po: us(po) })
    }

    fn n(&self) -> usize {
        self.po.len().saturating_sub(1)
    }

    /// feats.parts(i)'s rings of 2 points or more, in order.
    fn rings(&self, i: usize) -> Vec<&[P]> {
        let mut out = Vec::new();
        for p in self.po[i]..self.po[i + 1] {
            for r in self.ro[p]..self.ro[p + 1] {
                let (a, b) = (self.co[r], self.co[r + 1]);
                if b >= a + 2 {
                    out.push(&self.ll[a..b]);
                }
            }
        }
        out
    }

    /// roads._feature_boxes: (lon0, lat0, lon1, lat1) per feature, NaN without coordinates.
    fn boxes(&self) -> Vec<[f64; 4]> {
        (0..self.n()).map(|i| {
            let (a, b) = (self.co[self.ro[self.po[i]]], self.co[self.ro[self.po[i + 1]]]);
            if b <= a {
                return [f64::NAN; 4];
            }
            let mut r = [self.ll[a][0], self.ll[a][1], self.ll[a][0], self.ll[a][1]];
            for p in &self.ll[a + 1..b] {
                r = [nmin(r[0], p[0]), nmin(r[1], p[1]), nmax(r[2], p[0]), nmax(r[3], p[1])];
            }
            r
        }).collect()
    }

    /// roads._first_ring_ends: the first part's first ring's two ends (NaN: fewer than 2 points), and whether it has
    /// a part with a ring shorter than that (_near_bridge_ends' exact test).
    fn first_ends(&self) -> (Vec<[f64; 4]>, Vec<bool>) {
        let n = self.n();
        let (mut ends, mut short) = (vec![[f64::NAN; 4]; n], vec![false; n]);
        for i in 0..n {
            if self.po[i + 1] <= self.po[i] {
                continue;
            }
            let r = self.ro[self.po[i]];
            let (a, b) = (self.co[r], self.co[r + 1]);
            if b >= a + 2 {
                ends[i] = [self.ll[a][0], self.ll[a][1], self.ll[b - 1][0], self.ll[b - 1][1]];
            } else {
                short[i] = true;
            }
        }
        (ends, short)
    }
}

/// Ranges per feature from (off (n + 1,), rng (m, 2), kind (m,)).
fn ranges_of(off: &PyReadonlyArray1<i64>, rng: &PyReadonlyArray2<f64>, kind: &PyReadonlyArray1<i8>) -> Vec<Vec<R>> {
    let (off, rng, kind) = (off.as_array(), rng.as_array(), kind.as_array());
    (0..off.len().saturating_sub(1)).map(|i| {
        (off[i] as usize..off[i + 1] as usize).map(|r| (rng[[r, 0]], rng[[r, 1]], kind[r])).collect()
    }).collect()
}

type Out<'py> = (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i8>>);

fn ranges_out<'py>(py: Python<'py>, got: &[Vec<R>]) -> Out<'py> {
    let mut off = vec![0i64; got.len() + 1];
    let (mut flat, mut kinds) = (Vec::new(), Vec::new());
    for (i, g) in got.iter().enumerate() {
        for &(a, b, k) in g {
            flat.push(a);
            flat.push(b);
            kinds.push(k);
        }
        off[i + 1] = kinds.len() as i64;
    }
    let m = kinds.len();
    (off.into_pyarray(py), numpy::ndarray::Array2::from_shape_vec((m, 2), flat).expect("ranges").into_pyarray(py),
     kinds.into_pyarray(py))
}

/// The bridge features (raw ranges with a bridge, a ring of 2 points or more) and their first such ring's ends.
fn bridge_ends(geo: &Geo, raw: &[Vec<R>]) -> (Vec<usize>, Vec<[P; 2]>) {
    let (mut idx, mut ends) = (Vec::new(), Vec::new());
    for (b, rr) in raw.iter().enumerate() {
        if !rr.iter().any(|r| r.2 == BRIDGE) {
            continue;
        }
        if let Some(r) = geo.rings(b).first() {
            idx.push(b);
            ends.push([r[0], r[r.len() - 1]]);
        }
    }
    (idx, ends)
}

/// roads._near_bridge_ends: per feature, may an end of its first ring lie within 0.75 m of a bridge feature's end (a
/// box test in lon / lat, 2 m: ends_near; a first ring shorter than 2 points: yes)? None: no pre-check (past 89 deg).
fn near_ends(geo: &Geo, raw: &[Vec<R>]) -> Option<Vec<bool>> {
    let (_, bends) = bridge_ends(geo, raw);
    let (ends, short) = geo.first_ends();
    let lats: Vec<f64> = ends.iter().flat_map(|e| [e[1].abs(), e[3].abs()]).collect();
    // (np.nanmax(np.abs(lat)) where any is finite, else 0)
    let lat = if lats.iter().any(|v| v.is_finite()) {
        lats.iter().filter(|v| !v.is_nan()).fold(f64::NEG_INFINITY, |m, &v| if v > m { v } else { m })
    } else {
        0.0
    };
    if !lat.is_finite() || lat > 89.0 {
        return None;
    }
    let ty = 2.0 / 111320.0;
    let tx = ty / pmax(pmin(lat + 0.1, 89.0).to_radians().dcos(), 1e-3);
    let pts: Vec<P> = ends.iter().flat_map(|e| [[e[0], e[1]], [e[2], e[3]]]).collect();
    let tgt: Vec<P> = bends.iter().flat_map(|e| [e[0], e[1]]).collect();
    let m = ends_near_core(&pts, &tgt, tx, ty);
    Some((0..geo.n()).map(|i| m[2 * i] || m[2 * i + 1] || short[i]).collect())
}

#[derive(Clone)]
struct Reach {
    ranges: Vec<R>,
    over: [f64; 2],
}

/// One call's view of a layer: its geometry, every feature's raw ranges, the road specs' and structure widths.
struct Layer<'a> {
    geo: &'a Geo,
    raw: &'a [Vec<R>],
    near: Option<&'a [bool]>,
    spec: &'a [u8],
    spec_w: &'a [f64],
    sk: &'a [u8],
    sw: &'a [f64],
    prm: &'a Prm,
    boxes: &'a [[f64; 4]],
    ends: &'a [[f64; 4]],
    bidx: &'a [usize],
    bends: &'a [[P; 2]],
    reach: &'a mut HashMap<usize, Reach>,
}

impl<'a> Layer<'a> {
    /// roads._structure_width(feats, i), as the caller gave it (sk: 0 not given, 1 None, 2 sw[i]).
    fn structure(&self, i: usize) -> Result<Option<f64>, String> {
        match self.sk[i] {
            2 => Ok(Some(self.sw[i])),
            1 => Ok(None),
            _ => Err(format!("level rules: no structure width for feature {i}")),
        }
    }

    /// roads._raw_ground_parts(feats, j): j's on-ground pieces from its raw ranges.
    fn raw_ground(&self, j: usize) -> Vec<Vec<P>> {
        let pieces = ground_pieces(&self.raw[j], self.prm.min_part);
        cut_rings(&self.geo.rings(j), &pieces, true)
    }

    /// roads._bridge_reach(feats, i), remembered.
    fn bridge_reach(&mut self, i: usize) -> Result<Reach, String> {
        if let Some(r) = self.reach.get(&i) {
            return Ok(r.clone());
        }
        let got = self.make_reach(i)?;
        self.reach.insert(i, got.clone());
        Ok(got)
    }

    fn make_reach(&self, i: usize) -> Result<Reach, String> {
        let p = self.prm;
        let rngs = self.raw[i].clone();
        let base = Reach { ranges: rngs.clone(), over: [0.0, 0.0] };
        let up_w = if self.spec[i] != 0 { Some(self.spec_w[i]) } else { self.structure(i)? };
        let rings = self.geo.rings(i);
        let up_w = match up_w {
            Some(w) if !rings.is_empty() && rngs.iter().any(|r| r.2 == BRIDGE) => w,
            _ => return Ok(base),
        };
        let ring = rings[0];
        let (kx, ky) = (ring_kx(ring), KY);
        let m = to_m(ring, ring[0], kx, ky);
        let sm = arcs(&m);
        let l = sm[sm.len() - 1];
        if l <= 0.0 {
            return Ok(base);
        }
        let (mx, my) = cols(&m);
        let (mut out, mut over): (Vec<R>, [f64; 2]) = (Vec::new(), [0.0, 0.0]);
        for (n, &(a, b, k)) in rngs.iter().enumerate() {
            let (mut a, mut b) = (a, b);
            if k == BRIDGE {
                let (a0, b0) = (a, b);
                let (mut lo_m, mut hi_m) = (a0 * l, b0 * l);
                // (only the bridge's own stretch: a crossing counts under it; a 3 km segment's whole line and every
                // road near it cost a rail-yard tile seconds, 2026-10-25)
                let mut sub = vec![interp2(lo_m, &sm, &mx, &my)];
                for v in 0..m.len() {
                    if sm[v] > lo_m && sm[v] < hi_m {
                        sub.push(m[v]);
                    }
                }
                sub.push(interp2(hi_m, &sm, &mx, &my));
                let (mut lo, mut hi) = (sub[0], sub[0]);
                for q in &sub[1..] {
                    lo = [nmin(lo[0], q[0]), nmin(lo[1], q[1])];
                    hi = [nmax(hi[0], q[0]), nmax(hi[1], q[1])];
                }
                let pad = 1.0;
                let blo = [(lo[0] - pad) / kx + ring[0][0], (lo[1] - pad) / ky + ring[0][1]];
                let bhi = [(hi[0] + pad) / kx + ring[0][0], (hi[1] + pad) / ky + ring[0][1]];
                let (mut gl, mut gs, mut gj) = (Vec::new(), vec![0usize], Vec::new());
                for (j, bx) in self.boxes.iter().enumerate() {
                    if !(bx[0] <= bhi[0] && bx[2] >= blo[0] && bx[1] <= bhi[1] && bx[3] >= blo[1]) {
                        continue;
                    }
                    if j == i || self.spec[j] == 0 {
                        continue;
                    }
                    for g in self.raw_ground(j) {
                        gl.extend(to_m(&g, ring[0], kx, ky));
                        gs.push(gl.len());
                        gj.push(j);
                    }
                }
                let (lo0, hi0) = (lo_m, hi_m);
                if !gj.is_empty() {
                    for (g, sc, sin) in seg_hits_core(&sub, &gl, &gs) {
                        let sc = sc + lo0;
                        if !(lo0 + END_EPS_M < sc && sc < hi0 - END_EPS_M) {
                            continue;               // (at the stretch's end: joined there, END_EPS_M)
                        }
                        let j = gj[g];
                        let sin = pmax(sin, p.skew_min_sin);
                        let cos = pmax(0.0, 1.0 - sin * sin).sqrt();
                        let h = (self.spec_w[j] / 2.0 + p.skew_margin + p.skew_slope) / sin + (up_w / 2.0) * cos / sin;
                        lo_m = pmin(lo_m, pmax(a0 * l - p.skew_max, sc - h));
                        hi_m = pmax(hi_m, pmin(b0 * l + p.skew_max, sc + h));
                    }
                }
                // (never into a neighbouring range; past the feature's ends: "over")
                let prev = out.last().map(|r| r.1).unwrap_or(0.0);
                let nxt = if n + 1 < rngs.len() { rngs[n + 1].0 } else { 1.0 };
                if lo_m < 0.0 && a0 <= 1e-9 {
                    over[0] = pmax(over[0], -lo_m);
                }
                if hi_m > l && b0 >= 1.0 - 1e-9 {
                    over[1] = pmax(over[1], hi_m - l);
                }
                a = pmax(pmax(lo_m / l, prev), 0.0);
                b = pmin(pmin(hi_m / l, nxt), 1.0);
            }
            out.push((a, b, k));
        }
        Ok(Reach { ranges: out, over })
    }

    /// roads._tunnel_reach(feats, i, rngs).
    fn tunnel_reach(&self, i: usize, rngs: Vec<R>) -> Vec<R> {
        let rings = self.geo.rings(i);
        if self.spec[i] == 0 || rings.is_empty() {
            return rngs;
        }
        let ring = rings[0];
        let (kx, ky) = (ring_kx(ring), KY);
        let m = to_m(ring, ring[0], kx, ky);
        let sm = arcs(&m);
        let l = sm[sm.len() - 1];
        if l <= 0.0 {
            return rngs;
        }
        let (mx, my) = cols(&m);
        let tun: Vec<usize> = (0..rngs.len()).filter(|&n| rngs[n].2 == TUNNEL).collect();
        let t = &self.prm.tunnel;
        let pd = t[2] + t[3] + 1.0;
        let pad = [pd / kx, pd / ky];
        let mut near = vec![false; self.boxes.len()];
        let mut ports: Vec<P> = Vec::new();         // (the portals in m from ring[0]; the roads near them only)
        for &n in &tun {
            for f in [rngs[n].0, rngs[n].1] {
                let pm = interp2(f * l, &sm, &mx, &my);
                ports.push(pm);
                let q = [pm[0] / kx + ring[0][0], pm[1] / ky + ring[0][1]];
                for (j, bx) in self.boxes.iter().enumerate() {
                    near[j] |= bx[0] <= q[0] + pad[0] && bx[2] >= q[0] - pad[0] && bx[1] <= q[1] + pad[1]
                        && bx[3] >= q[1] - pad[1];
                }
            }
        }
        let (mut gl, mut gs, mut hw) = (Vec::new(), vec![0usize], Vec::new());
        for j in 0..near.len() {
            if !near[j] || j == i || self.spec[j] == 0 {
                continue;
            }
            for g in self.raw_ground(j) {
                let g = to_m(&g, ring[0], kx, ky);
                // a road ending at a portal is the tunnel road's own approach going on (a junction there), neither a
                // road crossing over the portal nor one running alongside it (the I-280 underpass at Millbrae)
                if !ports.is_empty() {
                    let ends = [g[0], g[g.len() - 1]];
                    let mut dmin = f64::INFINITY;
                    let mut first = true;
                    for pp in &ports {
                        for e in &ends {
                            let d = (pp[0] - e[0]).dhypot(pp[1] - e[1]);
                            dmin = if first { d } else { nmin(dmin, d) };
                            first = false;
                        }
                    }
                    if dmin < t[4] {
                        continue;
                    }
                }
                gl.extend(g);
                gs.push(gl.len());
                hw.push(self.spec_w[j] / 2.0);
            }
        }
        if hw.is_empty() {
            return rngs;
        }
        let ranges: Vec<[f64; 2]> = tun.iter().map(|&n| [rngs[n].0 * l, rngs[n].1 * l]).collect();
        let got = tunnel_reach_core(&m, &sm, &ranges, &gl, &gs, &hw, t);
        let mut out = rngs;
        for (q, &n) in tun.iter().enumerate() {
            let prev = if n > 0 { out[n - 1].1 } else { 0.0 };
            let nxt = if n + 1 < out.len() { out[n + 1].0 } else { 1.0 };
            out[n] = (pmax(got[q][0] / l, prev), pmin(got[q][1] / l, nxt), TUNNEL);
        }
        out.retain(|r| r.1 > r.0);      // (a tunnel left shorter than nothing: ground)
        out
    }

    /// roads._skew_decks(feats, i, rngs).
    fn skew_decks(&mut self, i: usize, rngs: Vec<R>) -> Result<Vec<R>, String> {
        let mut rngs = rngs;
        if rngs.iter().any(|r| r.2 == TUNNEL) {
            rngs = self.tunnel_reach(i, rngs);
        }
        if !self.prm.skew_on {
            return Ok(rngs);
        }
        if rngs.iter().any(|r| r.2 == BRIDGE) {
            rngs = self.bridge_reach(i)?.ranges;      // (its raw ranges lengthened, over a tunnel's reach too)
        }
        if self.bidx.is_empty() {
            return Ok(rngs);
        }
        if let Some(near) = self.near {
            if !near[i] {
                return Ok(rngs);                    // (no end of it near a bridge's end: nothing joins it)
            }
        }
        let rings = self.geo.rings(i);
        if rings.is_empty() {
            return Ok(rngs);
        }
        let ring = rings[0];
        let (kx, ky) = (ring_kx(ring), KY);
        let tol = self.prm.skew_join;
        let mut add: Vec<R> = Vec::new();
        let mut l: Option<f64> = None;
        for (end, p) in [(0usize, ring[0]), (1, ring[ring.len() - 1])] {
            if rngs.iter().any(|r| if end == 0 { r.0 <= 1e-9 } else { r.1 >= 1.0 - 1e-9 }) {
                continue;                           // off the ground at that end already
            }
            let mut best = 0.0f64;
            for r in 0..self.bidx.len() {
                for e2 in 0..2 {
                    let q = self.bends[r][e2];
                    let d = ((q[0] - p[0]) * kx).dhypot((q[1] - p[1]) * ky);
                    let b = self.bidx[r];
                    if d < tol && b != i {
                        best = pmax(best, self.bridge_reach(b)?.over[e2]);
                    }
                }
            }
            if best > 0.0 {
                let len = *l.get_or_insert_with(|| length_scaled(ring, kx, ky));
                if len <= 0.0 {
                    break;
                }
                let f = pmin(best, self.prm.skew_max) / len;
                add.push(if end == 0 { (0.0, pmin(f, 1.0), BRIDGE) } else { (pmax(1.0 - f, 0.0), 1.0, BRIDGE) });
            }
        }
        if add.is_empty() {
            return Ok(rngs);
        }
        rngs.extend(add);
        // (overlapping a range already there: joined, as level_ranges does)
        Ok(join_sorted(rngs, |a, b| if a == TUNNEL || b == TUNNEL { TUNNEL } else { BRIDGE }))
    }

    /// roads._portal_decks(feats, i, rngs).
    fn portal_decks(&self, i: usize, rngs: Vec<R>) -> Vec<R> {
        let rings = self.geo.rings(i);
        if rings.is_empty() || rngs.len() < 2 {
            return rngs;
        }
        let ring = rings[0];
        let (kx, ky) = (ring_kx(ring), KY);
        let l = length_scaled(ring, kx, ky);
        if l <= 0.0 {
            return rngs;
        }
        let mut out = rngs;
        for n in 0..out.len() - 1 {
            let ((a0, b0, k0), (a1, b1, k1)) = (out[n], out[n + 1]);
            let pair = (k0 == TUNNEL && k1 == BRIDGE) || (k0 == BRIDGE && k1 == TUNNEL);
            if !pair || !(0.0 < (a1 - b0) * l && (a1 - b0) * l <= self.prm.portal_deck) {
                continue;
            }
            let gap = sub_polyline(ring, b0, a1);
            let pd = self.prm.gap_junction + 1.0;
            let pad = [pd / kx, pd / ky];
            let (mut lo, mut hi) = (gap[0], gap[0]);
            for q in &gap[1..] {
                lo = [nmin(lo[0], q[0]), nmin(lo[1], q[1])];
                hi = [nmax(hi[0], q[0]), nmax(hi[1], q[1])];
            }
            let (lo, hi) = ([lo[0] - pad[0], lo[1] - pad[1]], [hi[0] + pad[0], hi[1] + pad[1]]);
            let gm = to_m(&gap, ring[0], kx, ky);
            let mut pe: Vec<P> = Vec::new();
            for (j, bx) in self.boxes.iter().enumerate() {
                let hit = bx[0] <= hi[0] && bx[2] >= lo[0] && bx[1] <= hi[1] && bx[3] >= lo[1] && self.spec[j] > 0;
                if !hit || j == i {
                    continue;
                }
                let e = self.ends[j];
                for q in [[e[0], e[1]], [e[2], e[3]]] {
                    if q[0].is_finite() && q[1].is_finite() {
                        pe.push([(q[0] - ring[0][0]) * kx, (q[1] - ring[0][1]) * ky]);
                    }
                }
            }
            if !pe.is_empty() && seg_dist_min(&pe, &gm) <= self.prm.gap_junction {
                continue;                           // (another road meets it: a junction on the ground)
            }
            if k0 == BRIDGE {
                out[n] = (a0, a1, BRIDGE);
            } else {
                out[n + 1] = (b0, b1, BRIDGE);
            }
        }
        out
    }

    /// roads._deck_gaps(feats, i, rngs).
    fn deck_gaps(&self, i: usize, rngs: Vec<R>) -> Result<Vec<R>, String> {
        if !self.prm.gap_on {
            return Ok(rngs);
        }
        let has = |k: i8| rngs.iter().any(|r| r.2 == k);
        if has(TUNNEL) {
            return Ok(if has(BRIDGE) { self.portal_decks(i, rngs) } else { rngs });
        }
        if !has(BRIDGE) {
            if let Some(near) = self.near {
                if !near[i] {
                    return Ok(rngs);                // (no bridge of its own, none ending at it)
                }
            }
        }
        if self.structure(i)?.is_none() {
            return Ok(rngs);                        // (railways: their own pieces)
        }
        let rings = self.geo.rings(i);
        if rings.is_empty() {
            return Ok(rngs);
        }
        let ring = rings[0];
        let (kx, ky) = (ring_kx(ring), KY);
        let m = to_m(ring, ring[0], kx, ky);
        let l = length_np(&m);
        if l <= 0.0 {
            return Ok(rngs);
        }
        let (hits, others) = deck_gaps_round(ring, self.ends, self.boxes, self.spec, i as i64, kx, ky,
                                             self.prm.gap_join, self.prm.gap_junction + 1.0);
        let mut end_deck = [false; 2];
        for e in 0..2 {
            let (j, k) = (hits[2 * e], hits[2 * e + 1]);
            end_deck[e] = j >= 0 && self.raw[j as usize].iter()
                .any(|r| r.2 == BRIDGE && if k == 0 { r.0 <= 1e-6 } else { r.1 >= 1.0 - 1e-6 });
        }
        let bridges: Vec<[f64; 2]> = rngs.iter().filter(|r| r.2 == BRIDGE).map(|r| [r.0 * l, r.1 * l]).collect();
        let got = fill_gaps(&m, &bridges, end_deck, &others, self.prm.gap_deck, self.prm.gap_junction);
        // (np.allclose's test, rtol 1e-5 / atol 1e-8, on these few numbers)
        let same = got.len() == bridges.len() && got.iter().zip(&bridges).all(|(g, r)| {
            g.iter().zip(r).all(|(&x, &y)| x == y || (y.is_finite() && (x - y).abs() <= 1e-8 + 1e-5 * y.abs()))
        });
        if same || got.is_empty() {
            return Ok(rngs);
        }
        let f = |v: f64| pmin(pmax(v / l, 0.0), 1.0);
        let mut out: Vec<R> = got.iter().map(|g| (f(g[0]), f(g[1]), BRIDGE)).collect();
        if got[0][0] <= 1e-6 {
            out[0].0 = 0.0;
        }
        let last = out.len() - 1;
        if got[last][1] >= l - 1e-6 {
            out[last].1 = 1.0;
        }
        Ok(out)
    }

    /// roads._lidar_gaps(feats, i, rngs): rngs with the lidar's elevated stretches (lon / lat from, to) made bridge.
    fn lidar_gaps(&self, i: usize, rngs: Vec<R>, got: &[[P; 2]]) -> Vec<R> {
        if !self.prm.lidar_on || rngs.iter().any(|r| r.2 == TUNNEL) || got.is_empty() {
            return rngs;
        }
        let rings = self.geo.rings(i);
        if rings.is_empty() {
            return rngs;
        }
        let mut all = rngs;
        for ab in got {
            let (mut fa, mut fb) = (ring_fraction(rings[0], ab[0]), ring_fraction(rings[0], ab[1]));
            if fb < fa {
                std::mem::swap(&mut fa, &mut fb);
            }
            if fb - fa > self.prm.min_part {
                all.push((if fa < 1e-4 { 0.0 } else { fa }, if fb > 1.0 - 1e-4 { 1.0 } else { fb }, BRIDGE));
            }
        }
        join_sorted(all, |_, _| BRIDGE)
    }
}

const STEP_SKEW: i64 = 1;
const STEP_LIDAR: i64 = 2;
const STEP_GAPS: i64 = 4;
const STEP_PORTAL: i64 = 8;

/// A road layer as roads.level_ranges' rules read it, made once per layer (kept on feats: its geometry, every feature's
/// raw_level_ranges, their boxes, first rings' ends and bridge ends, _near_bridge_ends and the bridges' reach as they
/// are worked out), so that one feature's rules cost that feature's work (a layer's arrays for each were seconds a
/// dense cell when asked feature by feature).
#[pyclass(module = "earthfast", frozen)]
pub struct LevelLayer {
    geo: Geo,
    raw: Vec<Vec<R>>,
    near: Option<Vec<bool>>,
    boxes: Vec<[f64; 4]>,
    ends: Vec<[f64; 4]>,
    bidx: Vec<usize>,
    bends: Vec<[P; 2]>,
    reach: std::sync::Mutex<HashMap<usize, Reach>>,     // (roads._bridge_reach's memo: one per layer, as it was)
}

#[pymethods]
impl LevelLayer {
    /// lonlat (k, 2) and its offsets (mapgeom.Features); raw_off (n + 1,) / raw_rng (m, 2) / raw_kind (m,): every
    /// feature's raw_level_ranges.
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(py: Python<'_>, lonlat: PyReadonlyArray2<'_, f64>, coord_off: PyReadonlyArray1<'_, i64>,
           ring_off: PyReadonlyArray1<'_, i64>, part_off: PyReadonlyArray1<'_, i64>, raw_off: PyReadonlyArray1<'_, i64>,
           raw_rng: PyReadonlyArray2<'_, f64>, raw_kind: PyReadonlyArray1<'_, i8>) -> PyResult<LevelLayer> {
        let geo = Geo::new(&lonlat, &coord_off, &ring_off, &part_off)?;
        let raw = ranges_of(&raw_off, &raw_rng, &raw_kind);
        if raw.len() != geo.n() {
            return Err(pyo3::exceptions::PyValueError::new_err("LevelLayer: a raw range list per feature"));
        }
        Ok(py.detach(|| {
            let near = near_ends(&geo, &raw);
            let (bidx, bends) = bridge_ends(&geo, &raw);
            let (ends, _) = geo.first_ends();
            let boxes = geo.boxes();
            LevelLayer { geo, raw, near, boxes, ends, bidx, bends, reach: std::sync::Mutex::new(HashMap::new()) }
        }))
    }

    /// roads._near_bridge_ends: (n,) per feature, or None (no pre-check: ends past 89 deg).
    fn near<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyArray1<bool>>> {
        self.near.as_ref().map(|v| v.clone().into_pyarray(py))
    }

    /// Features tgt's level ranges after the steps (bits: 1 _skew_decks, 2 _lidar_gaps, 4 _deck_gaps, 8 _portal_decks
    /// alone, in that order) from their ranges tin (tin_off (t + 1,), tin_rng, tin_kind). spec / spec_w: road_specs (1
    /// and its width where a feature has one); sk / sw: _structure_width (0 not made, 1 None, 2 sw); lid_off (t + 1,,
    /// or empty: none) / lid_pts (k, 4): each target's lidar stretches (lon, lat from, lon, lat to); prm: the rules'
    /// constants (roads._level_prm). Returns (off (t + 1,), ranges (m, 2), kinds (m,)).
    #[allow(clippy::too_many_arguments)]
    fn rules<'py>(&self, py: Python<'py>, spec: PyReadonlyArray1<'py, u8>, spec_w: PyReadonlyArray1<'py, f64>,
                  sk: PyReadonlyArray1<'py, u8>, sw: PyReadonlyArray1<'py, f64>, tgt: PyReadonlyArray1<'py, i64>,
                  tin_off: PyReadonlyArray1<'py, i64>, tin_rng: PyReadonlyArray2<'py, f64>,
                  tin_kind: PyReadonlyArray1<'py, i8>, lid_off: PyReadonlyArray1<'py, i64>,
                  lid_pts: PyReadonlyArray2<'py, f64>, steps: i64, prm: PyReadonlyArray1<'py, f64>)
                  -> PyResult<Out<'py>> {
        let n = self.geo.n();
        let tin = ranges_of(&tin_off, &tin_rng, &tin_kind);
        let spec: Vec<u8> = spec.as_array().to_vec();
        let spec_w: Vec<f64> = spec_w.as_array().to_vec();
        let sk: Vec<u8> = sk.as_array().to_vec();
        let sw: Vec<f64> = sw.as_array().to_vec();
        let tgt: Vec<usize> = tgt.as_array().iter().map(|&v| v.max(0) as usize).collect();
        let (lo, lp) = (lid_off.as_array(), lid_pts.as_array());
        let prm: Vec<f64> = prm.as_array().to_vec();
        let prm = Prm::new(&prm)?;
        if spec.len() != n || spec_w.len() != n || sk.len() != n || sw.len() != n || tin.len() != tgt.len()
            || tgt.iter().any(|&i| i >= n) || (!lo.is_empty() && lo.len() != tgt.len() + 1) {
            return Err(pyo3::exceptions::PyValueError::new_err("LevelLayer.rules: array sizes differ"));
        }
        let lid: Vec<Vec<[P; 2]>> = if lo.is_empty() { vec![Vec::new(); tgt.len()] } else {
            (0..tgt.len()).map(|t| (lo[t] as usize..lo[t + 1] as usize)
                .map(|k| [[lp[[k, 0]], lp[[k, 1]]], [lp[[k, 2]], lp[[k, 3]]]]).collect()).collect()
        };
        let got: Result<Vec<Vec<R>>, String> = py.detach(|| {
            let mut reach = self.reach.lock().unwrap_or_else(|e| e.into_inner());
            let mut layer = Layer {
                geo: &self.geo, raw: &self.raw, near: self.near.as_deref(), spec: &spec, spec_w: &spec_w, sk: &sk,
                sw: &sw, prm: &prm, boxes: &self.boxes, ends: &self.ends, bidx: &self.bidx, bends: &self.bends,
                reach: &mut reach,
            };
            let mut out = Vec::with_capacity(tgt.len());
            for (t, &i) in tgt.iter().enumerate() {
                let mut r = tin[t].clone();
                if steps & STEP_SKEW != 0 {
                    r = layer.skew_decks(i, r)?;
                }
                if steps & STEP_LIDAR != 0 {
                    r = layer.lidar_gaps(i, r, &lid[t]);
                }
                if steps & STEP_GAPS != 0 {
                    r = layer.deck_gaps(i, r)?;
                }
                if steps & STEP_PORTAL != 0 {
                    r = layer.portal_decks(i, r);
                }
                out.push(r);
            }
            Ok(out)
        });
        let got = got.map_err(pyo3::exceptions::PyValueError::new_err)?;
        Ok(ranges_out(py, &got))
    }

    /// roads.lidar_gap_candidates' lines: the drawn roads' ground pieces (rule ranges rule_*, have_rule: the features
    /// rule_level_ranges_near holds) with an end within prm[1] m of a bridge piece's end (any road's: its rule ranges'
    /// bridge pieces split at the level rule's local levels, lv_*: feature, start, end, Python's str of the value),
    /// from that end outward, prm[2] m at most; sk / sw: _structure_width as rules takes it; prm[0]: MIN_PART. Returns
    /// (feature (c,), line offsets (c + 1,), lines (k, 2) lon / lat, half widths (c,)).
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn lidar_lines<'py>(&self, py: Python<'py>, rule_off: PyReadonlyArray1<'py, i64>,
                        rule_rng: PyReadonlyArray2<'py, f64>, rule_kind: PyReadonlyArray1<'py, i8>,
                        have_rule: PyReadonlyArray1<'py, u8>, sk: PyReadonlyArray1<'py, u8>,
                        sw: PyReadonlyArray1<'py, f64>, lv_feat: PyReadonlyArray1<'py, i64>,
                        lv_start: PyReadonlyArray1<'py, f64>, lv_end: PyReadonlyArray1<'py, f64>,
                        lv_values: Vec<String>, prm: PyReadonlyArray1<'py, f64>)
                        -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>,
                                     Bound<'py, PyArray1<f64>>)> {
        let geo = &self.geo;
        let n = geo.n();
        let rule = ranges_of(&rule_off, &rule_rng, &rule_kind);
        let have: Vec<u8> = have_rule.as_array().to_vec();
        let (sk, sw): (Vec<u8>, Vec<f64>) = (sk.as_array().to_vec(), sw.as_array().to_vec());
        let prm: Vec<f64> = prm.as_array().to_vec();
        let (lf, ls, le) = (lv_feat.as_array(), lv_start.as_array(), lv_end.as_array());
        if rule.len() != n || have.len() != n || sk.len() != n || sw.len() != n || prm.len() < 3
            || lf.len() != ls.len() || lf.len() != le.len() || lf.len() != lv_values.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("LevelLayer.lidar_lines: array sizes differ"));
        }
        // each feature's level rule rows, in the table's order (mapgeom.RuleTable.rows)
        let mut lv: Vec<Vec<(f64, f64, f64)>> = vec![Vec::new(); n];
        for j in 0..lf.len() {
            let f = lf[j];
            if f >= 0 && (f as usize) < n {
                if let Some(r) = level_rule(ls[j], le[j], &lv_values[j]) {
                    lv[f as usize].push(r);
                }
            }
        }
        let (min_part, touch, reach) = (prm[0], prm[1], prm[2]);
        let got: Result<Vec<(usize, Vec<P>, f64)>, String> = py.detach(|| {
            let mut ends: Vec<P> = Vec::new();
            for i in 0..n {
                if have[i] == 0 {
                    continue;
                }
                let br: Vec<(f64, f64)> = rule[i].iter().filter(|r| r.2 == BRIDGE).map(|r| (r.0, r.1)).collect();
                let pieces: Vec<(f64, f64)> = level_spans(&br, &lv[i], min_part).iter().map(|s| (s.0, s.1)).collect();
                for r in cut_rings(&geo.rings(i), &pieces, false) {
                    ends.push(r[0]);
                    ends.push(r[r.len() - 1]);
                }
            }
            let mut out = Vec::new();
            if ends.is_empty() {
                return Ok(out);
            }
            for i in 0..n {
                if have[i] == 0 || rule[i].iter().any(|r| r.2 == TUNNEL) {
                    continue;
                }
                let w = match sk[i] {
                    2 => sw[i],
                    1 => continue,
                    _ => return Err(format!("lidar_lines: no structure width for feature {i}")),
                };
                for r in cut_rings(&geo.rings(i), &ground_pieces(&rule[i], min_part), false) {
                    let (kx, ky) = (111320.0 * r[0][1].to_radians().dcos(), KY);
                    for at in [0usize, r.len() - 1] {
                        let p = r[at];
                        if !ends.iter().any(|e| ((e[0] - p[0]) * kx).dhypot((e[1] - p[1]) * ky) < touch) {
                            continue;
                        }
                        let mut line: Vec<P> = if at == 0 { r.clone() } else { r.iter().rev().copied().collect() };
                        let s = arcs(&to_m(&line, line[0], kx, ky));
                        if s[s.len() - 1] > reach {
                            let (xs, ys) = cols(&line);
                            let cut = interp2(reach, &s, &xs, &ys);
                            line = (0..line.len()).filter(|&v| s[v] < reach).map(|v| line[v]).collect();
                            line.push(cut);
                        }
                        out.push((i, line, pmax(2.5, w / 2.0)));
                    }
                }
            }
            Ok(out)
        });
        let got = got.map_err(pyo3::exceptions::PyValueError::new_err)?;
        let mut off = vec![0i64; got.len() + 1];
        let (mut feat, mut pts, mut half) = (Vec::with_capacity(got.len()), Vec::new(), Vec::with_capacity(got.len()));
        for (c, (i, line, h)) in got.iter().enumerate() {
            feat.push(*i as i64);
            half.push(*h);
            for p in line {
                pts.push(p[0]);
                pts.push(p[1]);
            }
            off[c + 1] = (pts.len() / 2) as i64;
        }
        let k = pts.len() / 2;
        Ok((feat.into_pyarray(py), off.into_pyarray(py),
            numpy::ndarray::Array2::from_shape_vec((k, 2), pts).expect("lines").into_pyarray(py),
            half.into_pyarray(py)))
    }
}

/// roads._level_ranges' merge for rows of features: the map's ranges (tags: tag_level_ranges) with the grade
/// separations' (extra: 1 bridge, 3 ground: an underpass, its tunnel made ground; others left out) where no range of
/// the map's is. Returns (off, ranges, kinds) per row.
#[pyfunction]
pub fn level_extras<'py>(py: Python<'py>, tag_off: PyReadonlyArray1<'py, i64>, tag_rng: PyReadonlyArray2<'py, f64>,
                         tag_kind: PyReadonlyArray1<'py, i8>, ex_off: PyReadonlyArray1<'py, i64>,
                         ex_rng: PyReadonlyArray2<'py, f64>, ex_kind: PyReadonlyArray1<'py, i8>) -> PyResult<Out<'py>> {
    let tags = ranges_of(&tag_off, &tag_rng, &tag_kind);
    let extra = ranges_of(&ex_off, &ex_rng, &ex_kind);
    if tags.len() != extra.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("level_extras: rows differ"));
    }
    let got: Vec<Vec<R>> = tags.into_iter().zip(extra).map(|(tags, extra)| {
        let mut out = tags;
        for &(a, b, _) in extra.iter().filter(|r| r.2 == GROUND) {
            let mut cut = Vec::with_capacity(out.len());
            for &(fa, fb, k) in &out {
                if k != TUNNEL || fb <= a || fa >= b {
                    cut.push((fa, fb, k));
                    continue;
                }
                if fa < a - 1e-5 {
                    cut.push((fa, a, k));
                }
                if fb > b + 1e-5 {
                    cut.push((b, fb, k));
                }
            }
            out = cut;
        }
        let mut add = Vec::new();
        for &(a0, b, _) in extra.iter().filter(|r| r.2 == BRIDGE) {
            let mut a = a0;
            for &(fa, fb, _) in &out {
                if fb <= a || fa >= b {
                    continue;
                }
                if fa > a {
                    add.push((a, fa, BRIDGE));
                }
                a = pmax(a, fb);
                if a >= b {
                    break;
                }
            }
            if b - a > 1e-5 {
                add.push((a, b, BRIDGE));
            }
        }
        out.extend(add);
        out.sort_by(cmp_r);
        // (touching ranges of a kind joined: the map's fractions are float32s)
        let mut merged: Vec<R> = Vec::with_capacity(out.len());
        for (a, b, k) in out {
            if let Some(last) = merged.last_mut() {
                if k == last.2 && a <= last.1 + 1e-5 {
                    *last = (last.0, pmax(last.1, b), k);
                    continue;
                }
            }
            merged.push((a, b, k));
        }
        merged
    }).collect();
    Ok(ranges_out(py, &got))
}

