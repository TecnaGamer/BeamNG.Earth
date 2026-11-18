//! Street-name signs on the traffic signals' mast arms (server/streetsigns.py).

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::speedsigns::{rounded, Mesh};

const SAME_SIGNAL_M: f64 = 1.5;
const SAME_SIGNAL_DEG: f64 = 15.0;

const CROSS_PROBE: (f64, f64) = (4.0, 8.0);   // (left of the pole, ahead): the junction's middle
const CROSS_MAX_M: f64 = 24.0;
const CROSS_COS: f64 = 0.55;
const OWN_PROBE: (f64, f64) = (2.0, 6.0);     // (left, behind): in the approach's own lanes
const OWN_COS: f64 = 0.8;
const OWN_MAX_M: f64 = 15.0;

// The stock boom's arm (s_trafficlight_boom_sn.dae): (m from the pole, m up)
const ARM_PROFILE: [(f64, f64); 3] = [(0.0, 4.8), (3.7, 6.07), (4.3, 6.17)];
const ARM_REACH: f64 = 0.8;
const ARM_FRONT: f64 = 0.12;

const METAL: i64 = 3;               // signmesh.MATERIALS
const GLYPH: i64 = 5;
const GLYPH_WHITE: i64 = 8;

pub const SIGNAL_ARM: i64 = 0;

fn rows<const N: usize>(a: &numpy::ndarray::ArrayView2<'_, f64>) -> Vec<[f64; N]> {
    a.outer_iter().map(|r| std::array::from_fn(|d| r[d])).collect()
}

/// (drivers' right, travel) of a signal turned yaw degrees.
fn axes(yaw: f64) -> ([f64; 2], [f64; 2]) {
    let (s, c) = yaw.to_radians().sin_cos();
    ([c, s], [-s, c])
}

/// The rows [x, y, z, yaw] to draw: each signal once, those in box [x0, y0, x1, y1).
pub fn signals(rows: &[[f64; 4]], bx: [f64; 4]) -> Vec<i64> {
    let mut kept: Vec<usize> = Vec::new();
    for (i, r) in rows.iter().enumerate() {
        let same = kept.iter().any(|&k| {
            let o = rows[k];
            (r[0] - o[0]).hypot(r[1] - o[1]) < SAME_SIGNAL_M
                && ((r[3] - o[3] + 180.0).rem_euclid(360.0) - 180.0).abs() < SAME_SIGNAL_DEG
        });
        if !same {
            kept.push(i);
        }
    }
    kept.into_iter()
        .filter(|&i| bx[0] <= rows[i][0] && rows[i][0] < bx[2] && bx[1] <= rows[i][1] && rows[i][1] < bx[3])
        .map(|i| i as i64)
        .collect()
}

pub struct Segs {
    a: Vec<[f64; 2]>,
    b: Vec<[f64; 2]>,
    t: Vec<[f64; 2]>,
    line: Vec<usize>,
}

impl Segs {
    /// The lines' segments, but those whose middle is in a level [line, s0, s1] (a bridge or tunnel stretch).
    pub fn new(pts: &[[f64; 2]], off: &[i64], levels: &[[f64; 3]]) -> Segs {
        let mut s = Segs { a: Vec::new(), b: Vec::new(), t: Vec::new(), line: Vec::new() };
        let mut ranges: HashMap<usize, Vec<(f64, f64)>> = HashMap::new();
        for l in levels {
            if l[0] >= 0.0 {
                ranges.entry(l[0] as usize).or_default().push((l[1], l[2]));
            }
        }
        for r in 0..off.len().saturating_sub(1) {
            let (i0, i1) = (off[r].max(0) as usize, (off[r + 1].max(0) as usize).min(pts.len()));
            if i1 < i0 + 2 {
                continue;
            }
            let line = &pts[i0..i1];
            let len: Vec<f64> = line.windows(2).map(|w| (w[1][0] - w[0][0]).hypot(w[1][1] - w[0][1])).collect();
            let total = len.iter().sum::<f64>().max(1e-9);
            let off_r = ranges.get(&r);
            let mut acc = 0.0;
            for (j, w) in line.windows(2).enumerate() {
                let mid = (acc + len[j] / 2.0) / total;
                acc += len[j];
                if off_r.is_some_and(|v| v.iter().any(|&(s0, s1)| mid >= s0 && mid <= s1)) {
                    continue;
                }
                let n = len[j].max(1e-9);
                s.a.push(w[0]);
                s.b.push(w[1]);
                s.t.push([(w[1][0] - w[0][0]) / n, (w[1][1] - w[0][1]) / n]);
                s.line.push(r);
            }
        }
        s
    }

    fn dist(&self, k: usize, q: [f64; 2]) -> f64 {
        let (a, b) = (self.a[k], self.b[k]);
        let d = [b[0] - a[0], b[1] - a[1]];
        let t = ((q[0] - a[0]) * d[0] + (q[1] - a[1]) * d[1]) / (d[0] * d[0] + d[1] * d[1]).max(1e-12);
        let t = t.clamp(0.0, 1.0);
        (a[0] + t * d[0] - q[0]).hypot(a[1] + t * d[1] - q[1])
    }

    /// The line of the nearest segment within max_m of q that keep(line, |cos to dir|) accepts.
    fn nearest(&self, q: [f64; 2], dir: [f64; 2], max_m: f64, keep: impl Fn(usize, f64) -> bool) -> Option<usize> {
        let mut best: Option<(f64, usize)> = None;
        for k in 0..self.a.len() {
            let cos = (self.t[k][0] * dir[0] + self.t[k][1] * dir[1]).abs();
            if !keep(self.line[k], cos) {
                continue;
            }
            let d = self.dist(k, q);
            if d <= max_m && best.is_none_or(|(bd, _)| d < bd) {
                best = Some((d, self.line[k]));
            }
        }
        best.map(|(_, l)| l)
    }
}

/// The line the signal's approach crosses, or -1 (none named near, or the approach's own street).
pub fn cross(segs: &Segs, name_id: &[i64], x: f64, y: f64, yaw: f64) -> i64 {
    let (r, d) = axes(yaw);
    let named = |l: usize| name_id.get(l).is_some_and(|&n| n >= 0);
    let q = [x - r[0] * CROSS_PROBE.0 + d[0] * CROSS_PROBE.1, y - r[1] * CROSS_PROBE.0 + d[1] * CROSS_PROBE.1];
    let Some(line) = segs.nearest(q, d, CROSS_MAX_M, |l, cos| cos <= CROSS_COS && named(l)) else { return -1 };
    let p0 = [x - r[0] * OWN_PROBE.0 - d[0] * OWN_PROBE.1, y - r[1] * OWN_PROBE.0 - d[1] * OWN_PROBE.1];
    if let Some(own) = segs.nearest(p0, d, OWN_MAX_M, |_, cos| cos >= OWN_COS) {
        if name_id.get(own) == name_id.get(line) {
            return -1;
        }
    }
    line as i64
}

/// A name as a style writes it ("East Fayette Street" -> "E FAYETTE ST"); a direction that is the name stays.
pub fn abbrev(name: &str, dirs: &HashMap<String, String>, first: &HashMap<String, String>,
              last: &HashMap<String, String>, upper: bool) -> String {
    let mut words: Vec<String> = name.split_whitespace().map(str::to_string).collect();
    if words.is_empty() {
        return String::new();
    }
    let low: Vec<String> = words.iter().map(|w| w.to_lowercase()).collect();
    let n = words.len();
    if n > 1 && dirs.contains_key(&low[0]) && !dirs.contains_key(&low[1]) && (n > 2 || !last.contains_key(&low[1])) {
        words[0] = dirs[&low[0]].clone();
    } else if n > 1 && first.contains_key(&low[0]) {
        words[0] = first[&low[0]].clone();
    }
    let mut trail = None;
    if n > 1 && dirs.contains_key(&low[n - 1]) && !dirs.contains_key(&low[n - 2]) {
        trail = Some(dirs[&low[n - 1]].clone());
        words.pop();
    }
    if words.len() > 1 {
        if let Some(s) = last.get(&words[words.len() - 1].to_lowercase()) {
            *words.last_mut().unwrap() = s.clone();
        }
    }
    words.extend(trail);
    let text = words.join(" ");
    if upper { text.to_uppercase() } else { text }
}

/// signmesh's font: glyphs [advance, left, bottom, right, top, u0, v0, u1, v1].
pub struct Font {
    pub glyphs: HashMap<char, [f64; 9]>,
    pub missing: [f64; 9],
    pub kern: HashMap<(char, char), f64>,
    pub stretch: f64,
    pub spacing: f64,
}

impl Font {
    fn glyph(&self, c: char) -> &[f64; 9] {
        self.glyphs.get(&c).unwrap_or(&self.missing)
    }

    fn kerning(&self, prev: Option<char>, c: char) -> f64 {
        prev.and_then(|p| self.kern.get(&(p, c))).copied().unwrap_or(0.0)
    }

    /// signmesh.advance
    pub fn advance(&self, text: &str) -> f64 {
        let mut x = 0.0;
        let mut prev = None;
        for c in text.chars() {
            x += self.glyph(c)[0] + self.spacing + self.kerning(prev, c);
            prev = Some(c);
        }
        x * self.stretch
    }

    /// signmesh.measure's ink box [left, bottom, right, top].
    fn ink(&self, text: &str) -> [f64; 4] {
        let (mut x, mut prev, mut ink): (f64, Option<char>, Option<[f64; 4]>) = (0.0, None, None);
        for c in text.chars() {
            x += self.kerning(prev, c) * self.stretch;
            let g = self.glyph(c);
            if g[3] > g[1] && g[4] > g[2] {
                let (l, r) = (x + g[1] * self.stretch, x + g[3] * self.stretch);
                ink = Some(match ink {
                    None => [l, g[2], r, g[4]],
                    Some(b) => [b[0].min(l), b[1].min(g[2]), b[2].max(r), b[3].max(g[4])],
                });
            }
            x += (g[0] + self.spacing) * self.stretch;
            prev = Some(c);
        }
        ink.unwrap_or([0.0; 4])
    }

    /// The ink centred on y = 0, z at depth x: glyphs height high, at most max_w wide (signmesh.Mesh.text centre_ink).
    fn text(&self, m: &mut Mesh, text: &str, x: f64, z: f64, height: f64, max_w: f64, mat: i64) {
        let [l, b, r, t] = self.ink(text);
        let h = height.min(max_w / (r - l).max(0.01));
        let mut y = -(l + r) * h / 2.0;
        let z = z - (b + t) / 2.0 * h;
        let s = self.stretch * h;
        let mut prev = None;
        for c in text.chars() {
            y += self.kerning(prev, c) * s;
            let g = *self.glyph(c);
            if g[3] > g[1] && g[4] > g[2] {
                let (y0, y1, z0, z1) = (y + g[1] * s, y + g[3] * s, z + g[2] * h, z + g[4] * h);
                m.quad([[x, y0, z0], [x, y1, z0], [x, y1, z1], [x, y0, z1]], mat,
                       [[g[5], g[6]], [g[7], g[6]], [g[7], g[8]], [g[5], g[8]]]);
            }
            y += (g[0] + self.spacing) * s;
            prev = Some(c);
        }
    }
}

#[derive(Clone, Copy)]
pub struct Style {
    pub mount: i64,
    pub height: f64,
    pub text: f64,
    pub width: (f64, f64),
    pub border: i64,
    pub bg: i64,
    pub white: bool,
}

/// The arm's height out m from the pole.
pub fn arm_z(out: f64) -> f64 {
    let p = &ARM_PROFILE;
    if out <= p[0].0 {
        return p[0].1;
    }
    for w in p.windows(2) {
        if out <= w[1].0 {
            return w[0].1 + (out - w[0].0) / (w[1].0 - w[0].0) * (w[1].1 - w[0].1);
        }
    }
    p[p.len() - 1].1
}

pub fn blade_width(font: &Font, text: &str, st: &Style) -> f64 {
    (font.advance(text) * st.text + 0.40).clamp(st.width.0, st.width.1)
}

/// A blade on the arm of the boom s [x, y, z, yaw], level, its middle on the rising arm, facing the drivers.
pub(crate) fn blade(m: &mut Mesh, font: &Font, s: [f64; 4], origin: [f64; 3], text: &str, st: &Style) {
    let (r, d) = axes(s[3]);
    let (w, h) = (blade_width(font, text, st), st.height);
    let out = ARM_REACH + w / 2.0;
    let up = arm_z(out);
    m.centre = [s[0] - r[0] * out - d[0] * ARM_FRONT - origin[0], s[1] - r[1] * out - d[1] * ARM_FRONT - origin[1],
                s[2] - origin[2]];
    m.face = [-d[0], -d[1]];
    m.right = r;
    // the plate: rounded, 5 cm thick
    let body = rounded(w + 0.03, up - (h + 0.03) / 2.0, h + 0.03, 0.03);
    m.fill(&body, 0.025, METAL, false);
    m.fill(&body, -0.025, METAL, true);
    for i in 0..body.len() {
        let (a, b) = (body[i], body[(i + 1) % body.len()]);
        m.quad([[0.025, a[0], a[1]], [-0.025, a[0], a[1]], [-0.025, b[0], b[1]], [0.025, b[0], b[1]]], METAL,
               [[0.0; 2]; 4]);
    }
    m.fill(&rounded(w, up - h / 2.0, h, 0.03), 0.027, st.border, false);
    m.fill(&rounded(w - 0.04, up - (h - 0.04) / 2.0, h - 0.04, 0.025), 0.029, st.bg, false);
    font.text(m, text, 0.032, up, st.text, w - 0.22, if st.white { GLYPH_WHITE } else { GLYPH });
    for sy in [-w / 3.0, w / 3.0] {
        let z = arm_z(out - sy);
        m.cuboid([-ARM_FRONT, sy - 0.025, z - 0.06], [0.0, sy + 0.025, z + 0.06], METAL);
    }
}

/// The blades of signals with their texts and styles, relative to origin; none past max_verts vertices.
pub(crate) fn mesh(sig: &[[f64; 4]], texts: &[String], styles: &[Style], font: &Font, origin: [f64; 3],
                   max_verts: usize) -> Mesh {
    let mut m = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3], face: [0.0; 2],
                       right: [0.0; 2] };
    for ((s, text), st) in sig.iter().zip(texts).zip(styles) {
        if st.mount != SIGNAL_ARM || text.is_empty() || m.p.len() > max_verts {
            continue;
        }
        blade(&mut m, font, *s, origin, text, st);
    }
    m
}

/// Python: street_sign_signals(rows (n, 4) [x, y, z, yaw], box) -> (k,) the rows to draw.
#[pyfunction]
pub fn street_sign_signals<'py>(py: Python<'py>, rows: PyReadonlyArray2<'py, f64>, bx: [f64; 4])
                                -> Bound<'py, PyArray1<i64>> {
    let r = self::rows::<4>(&rows.as_array());
    py.detach(|| signals(&r, bx)).into_pyarray(py)
}

/// Python: street_sign_cross(signals (k, 4), pts (n, 2), off (r + 1,), name_id (r,), levels (j, 3)) -> (k,) line or -1.
#[pyfunction]
pub fn street_sign_cross<'py>(py: Python<'py>, signals: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>,
                              off: PyReadonlyArray1<'py, i64>, name_id: PyReadonlyArray1<'py, i64>,
                              levels: PyReadonlyArray2<'py, f64>) -> Bound<'py, PyArray1<i64>> {
    let (s, p, l) = (rows::<4>(&signals.as_array()), rows::<2>(&pts.as_array()), rows::<3>(&levels.as_array()));
    let (o, n) = (off.as_array().to_vec(), name_id.as_array().to_vec());
    py.detach(|| {
        let segs = Segs::new(&p, &o, &l);
        s.iter().map(|r| cross(&segs, &n, r[0], r[1], r[3])).collect::<Vec<i64>>()
    }).into_pyarray(py)
}

/// Python: street_sign_abbrev(names, dirs, first, last, upper) -> names.
#[pyfunction]
pub fn street_sign_abbrev(names: Vec<String>, dirs: HashMap<String, String>, first: HashMap<String, String>,
                          last: HashMap<String, String>, upper: bool) -> Vec<String> {
    names.iter().map(|n| abbrev(n, &dirs, &first, &last, upper)).collect()
}

/// Python: street_sign_mesh(signals (k, 4), texts, styles (k, 8) [mount, height, text, min w, max w, border, bg,
/// white], glyph_cp, glyph (g, 9), missing, kern_cp (q, 2), kern (q,), stretch, spacing, origin, max_verts)
/// -> (p, t, uv, material per triangle).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn street_sign_mesh<'py>(py: Python<'py>, signals: PyReadonlyArray2<'py, f64>, texts: Vec<String>,
                             styles: PyReadonlyArray2<'py, f64>, glyph_cp: PyReadonlyArray1<'py, i64>,
                             glyph: PyReadonlyArray2<'py, f64>, missing: [f64; 9], kern_cp: PyReadonlyArray2<'py, i64>,
                             kern: PyReadonlyArray1<'py, f64>, stretch: f64, spacing: f64, origin: [f64; 3],
                             max_verts: usize)
                             -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                                 Bound<'py, PyArray1<i64>>) {
    let s = rows::<4>(&signals.as_array());
    let st: Vec<Style> = rows::<8>(&styles.as_array()).iter().map(|r| Style {
        mount: r[0] as i64, height: r[1], text: r[2], width: (r[3], r[4]), border: r[5] as i64, bg: r[6] as i64,
        white: r[7] != 0.0 }).collect();
    let ch = |v: i64| char::from_u32(v as u32);
    let (cp, g) = (glyph_cp.as_array(), rows::<9>(&glyph.as_array()));
    let glyphs = cp.iter().zip(g).filter_map(|(&c, row)| ch(c).map(|c| (c, row))).collect();
    let (kc, kv) = (kern_cp.as_array(), kern.as_array());
    let kern = kc.outer_iter().zip(kv.iter())
        .filter_map(|(p, &v)| Some(((ch(p[0])?, ch(p[1])?), v))).collect();
    let font = Font { glyphs, missing, kern, stretch, spacing };
    let m = py.detach(|| mesh(&s, &texts, &st, &font, origin, max_verts));
    let a2 = |v: Vec<f64>, c: usize| numpy::ndarray::Array2::from_shape_vec((v.len() / c, c), v).expect("shape");
    let t: Vec<i64> = m.t.iter().flatten().copied().collect();
    (a2(m.p.iter().flatten().copied().collect(), 3).into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((m.t.len(), 3), t).expect("shape").into_pyarray(py),
     a2(m.uv.iter().flatten().copied().collect(), 2).into_pyarray(py),
     m.mi.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn font() -> Font {
        let mut glyphs: HashMap<char, [f64; 9]> = ('A'..='Z').map(|c| (c, [0.6, 0.05, 0.0, 0.55, 1.0, 0., 0., 0., 0.]))
            .collect();
        glyphs.insert(' ', [0.3, 0.0, 0.0, 0.3, 0.0, 0., 0., 0., 0.]);
        Font { glyphs, missing: [0.6, 0.05, 0.0, 0.55, 1.0, 0., 0., 0., 0.], kern: HashMap::new(), stretch: 0.78,
               spacing: 0.08 }
    }

    const US: Style = Style { mount: SIGNAL_ARM, height: 0.46, text: 0.26, width: (1.0, 2.9), border: 0, bg: 7,
                              white: true };

    fn words(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
    }

    /// North Main Street (line 0) along x = 0, Elm Street (line 1) along y = 20.
    fn grid(names: [i64; 2]) -> (Segs, Vec<i64>) {
        let pts = [[0.0, -200.0], [0.0, 200.0], [-200.0, 20.0], [200.0, 20.0]];
        (Segs::new(&pts, &[0, 2, 4], &[]), names.to_vec())
    }

    fn ink_range(m: &Mesh, axis: usize) -> (f64, f64) {
        m.t.iter().zip(&m.mi).filter(|(_, &mi)| mi == GLYPH_WHITE)
            .flat_map(|(t, _)| t.iter().map(|&i| m.p[i as usize][axis]))
            .fold((f64::MAX, f64::MIN), |(lo, hi), v| (lo.min(v), hi.max(v)))
    }

    #[test]
    fn the_blade_names_the_street_crossed() {
        let (segs, names) = grid([0, 1]);
        assert_eq!(cross(&segs, &names, 8.0, 8.0, 0.0), 1);       // northbound on Main
        assert_eq!(cross(&segs, &names, -10.0, 12.0, 270.0), 0);  // eastbound on Elm
    }

    #[test]
    fn no_blade_without_a_named_cross_street_or_naming_the_own_street() {
        let (segs, names) = grid([0, -1]);
        assert_eq!(cross(&segs, &names, 8.0, 8.0, 0.0), -1);
        let (segs, names) = grid([0, 0]);
        assert_eq!(cross(&segs, &names, 8.0, 8.0, 0.0), -1);
    }

    #[test]
    fn a_bridge_is_no_cross_street() {
        let pts = [[0.0, -200.0], [0.0, 200.0], [-200.0, 20.0], [-60.0, 20.0], [60.0, 20.0], [200.0, 20.0]];
        let segs = Segs::new(&pts, &[0, 2, 6], &[[1.0, 0.35, 0.65]]);
        assert_eq!(cross(&segs, &[0, 1], 8.0, 8.0, 0.0), -1);
    }

    #[test]
    fn one_signal_read_twice_is_drawn_once_and_only_in_its_tile() {
        let rows = [[10.0, 10.0, 0.0, 359.0], [10.5, 10.0, 0.0, 2.0], [10.0, 10.0, 0.0, 90.0], [120.0, 5.0, 0.0, 0.0]];
        assert_eq!(signals(&rows, [0.0, 0.0, 100.0, 100.0]), vec![0, 2]);
    }

    #[test]
    fn us_short_forms() {
        let dirs = words(&[("north", "N"), ("east", "E"), ("west", "W")]);
        let last = words(&[("street", "ST"), ("freeway", "FWY"), ("highway", "HWY")]);
        let first = HashMap::new();
        let f = |n: &str| abbrev(n, &dirs, &first, &last, true);
        assert_eq!(f("West Main Street"), "W MAIN ST");
        assert_eq!(f("North Stemmons Freeway"), "N STEMMONS FWY");
        assert_eq!(f("North Street"), "NORTH ST");
        assert_eq!(f("East West Highway"), "EAST WEST HWY");
        assert_eq!(f("Broadway East"), "BROADWAY E");
        assert_eq!(abbrev("Elm Street", &dirs, &first, &last, false), "Elm ST");
    }

    #[test]
    fn the_blade_hangs_on_the_rising_arm_facing_the_drivers() {
        assert_eq!(arm_z(0.0), 4.8);
        assert!((arm_z(3.7) - 6.07).abs() < 1e-12 && arm_z(9.0) == 6.17);
        let f = font();
        let w = blade_width(&f, "ELM ST", &US);
        // yaw 0: the arm along -x, the drivers coming north (+y), so the face is toward -y
        let m = mesh(&[[0.0; 4]], &["ELM ST".into()], &[US], &f, [0.0; 3], 52000);
        let (lo, hi) = ink_range(&m, 2);
        assert!(((lo + hi) / 2.0 - arm_z(ARM_REACH + w / 2.0)).abs() < 1e-9);
        let xs: Vec<f64> = m.p.iter().map(|p| p[0]).collect();
        let xmax = xs.iter().cloned().fold(f64::MIN, f64::max);
        let xmin = xs.iter().cloned().fold(f64::MAX, f64::min);
        assert!((xmax - (-ARM_REACH + 0.015)).abs() < 1e-9 && (xmin - (-ARM_REACH - w - 0.015)).abs() < 1e-9);
        assert!(ink_range(&m, 1).1 < -ARM_FRONT, "the lettering in front of the arm");
    }

    #[test]
    fn a_long_name_is_capped_and_its_text_fits() {
        let f = font();
        let name = "VERY LONG NAME OF A BOULEVARD THAT GOES ON";
        assert_eq!(blade_width(&f, name, &US), 2.9);
        let m = mesh(&[[0.0; 4]], &[name.into()], &[US], &f, [0.0; 3], 52000);
        let (lo, hi) = ink_range(&m, 0);
        assert!(hi - lo <= 2.9 - 0.22 + 1e-9);
    }

    #[test]
    fn no_blade_for_another_mount_or_past_the_vertex_cap() {
        let f = font();
        let other = Style { mount: 1, ..US };
        assert!(mesh(&[[0.0; 4]], &["ELM ST".into()], &[other], &f, [0.0; 3], 52000).t.is_empty());
        let two = mesh(&[[0.0; 4], [50.0, 0.0, 0.0, 0.0]], &["A".into(), "B".into()], &[US, US], &f, [0.0; 3], 0);
        let one = mesh(&[[0.0; 4]], &["A".into()], &[US], &f, [0.0; 3], 52000);
        assert_eq!(two.p.len(), one.p.len());
    }
}
