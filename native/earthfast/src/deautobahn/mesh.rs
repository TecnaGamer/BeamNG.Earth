//! The German Autobahn boards and what carries them as render-only triangles (speedsigns::Mesh: x toward the driver,
//! y to the driver's right, z up; front faces counter-clockwise as the driver sees them, lettering TEXT_DEPTH in front
//! of the face, UVs v-up). Material ids index server/deautobahnfont.py MATERIALS.
use super::layout::{Arrow, Assembly, Font, Item, Panel, Shape, WHITE_INK_BELOW};
use super::stations::Mount;
use crate::speedsigns::{rounded, Mesh};

pub(crate) const M_LETTERS_WHITE: i64 = 0;
pub(crate) const M_LETTERS_BLACK: i64 = 1;
pub(crate) const M_BLUE: i64 = 2;
pub(crate) const M_WHITE: i64 = 3;
pub(crate) const M_YELLOW: i64 = 4;
pub(crate) const M_GREEN: i64 = 5;
pub(crate) const M_BLACK: i64 = 6;
pub(crate) const M_METAL: i64 = 7;
const TEXT_DEPTH: f64 = 0.02;
const INLAY_DEPTH: f64 = 0.01;
const RIM: f64 = 0.06;
const RADIUS: f64 = 0.12;
// Zeichen 450 (StVO 2017 drawings 450-50/51/52): the bars' left top edges and thickness as fractions of the height,
// rising by 0.228 of the height across the board
const BAKE_BARS: [(&[f64], f64); 3] = [(&[0.61], 0.185), (&[0.515, 0.79], 0.135), (&[0.485, 0.67, 0.85], 0.092)];

fn shift(poly: Vec<[f64; 2]>, dy: f64) -> Vec<[f64; 2]> {
    poly.into_iter().map(|[y, z]| [y + dy, z]).collect()
}

fn rect(y0: f64, y1: f64, z0: f64, z1: f64) -> Vec<[f64; 2]> {
    vec![[y0, z0], [y1, z0], [y1, z1], [y0, z1]]
}

fn round_rect(y0: f64, y1: f64, z0: f64, z1: f64, r: f64) -> Vec<[f64; 2]> {
    let w = y1 - y0;
    shift(rounded(w, z0, z1 - z0, r.min(w / 2.0 - 1e-3).min((z1 - z0) / 2.0 - 1e-3).max(1e-3)), y0 + w / 2.0)
}

/// A convex polygon clipped to an axis-aligned rectangle (Sutherland-Hodgman).
fn clip(poly: &[[f64; 2]], y0: f64, y1: f64, z0: f64, z1: f64) -> Vec<[f64; 2]> {
    let mut out: Vec<[f64; 2]> = poly.to_vec();
    let edges: [(usize, f64, bool); 4] = [(0, y0, true), (0, y1, false), (1, z0, true), (1, z1, false)];
    for (axis, v, keep_above) in edges {
        let inside = |p: &[f64; 2]| if keep_above { p[axis] >= v } else { p[axis] <= v };
        let src = std::mem::take(&mut out);
        for i in 0..src.len() {
            let (a, b) = (src[i], src[(i + 1) % src.len()]);
            let (ia, ib) = (inside(&a), inside(&b));
            if ia {
                out.push(a);
            }
            if ia != ib {
                let t = (v - a[axis]) / (b[axis] - a[axis]);
                out.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]);
            }
        }
        if out.len() < 3 {
            return Vec::new();
        }
    }
    out
}

fn quad_uv(m: &mut Mesh, y0: f64, y1: f64, z0: f64, z1: f64, x: f64, mat: i64, uv: [f64; 4]) {
    m.quad([[x, y0, z0], [x, y1, z0], [x, y1, z1], [x, y0, z1]], mat,
           [[uv[0], uv[1]], [uv[2], uv[1]], [uv[2], uv[3]], [uv[0], uv[3]]]);
}

/// Text left-aligned at y on baseline z, cap height cap (kerning and the face's spacing as the layout measured it).
pub(crate) fn text(m: &mut Mesh, font: &Font, s: &str, face: i64, cap: f64, y: f64, z: f64, mat: i64) {
    let [scale, spacing] = font.params(face);
    let mut cursor = y;
    let mut prev: Option<char> = None;
    for ch in s.chars() {
        let Some(g) = font.glyph(face, ch) else { continue };
        if let Some(p) = prev {
            cursor += font.kerning(face, p, ch) * cap;
        }
        if g[3] > g[1] && g[4] > g[2] {
            quad_uv(m, cursor + g[1] * cap * scale, cursor + g[3] * cap * scale, z + g[2] * cap, z + g[4] * cap,
                    TEXT_DEPTH, mat, [g[5], g[6], g[7], g[8]]);
        }
        cursor += (g[0] * scale + spacing) * cap;
        prev = Some(ch);
    }
}

fn text_centred(m: &mut Mesh, font: &Font, s: &str, cap: f64, cy: f64, cz: f64, mat: i64) {
    let w = font.width(s, 0, cap);
    text(m, font, s, 0, cap, cy - w / 2.0, cz - cap / 2.0, mat);
}

/// An arrow of length l pointing along (ux, uz) from its tail at (y, z): a shaft and a head, as convex parts.
fn arrow_at(m: &mut Mesh, y: f64, z: f64, ux: f64, uz: f64, l: f64, x: f64) {
    let (shaft_w, head_w, head_l) = (0.12 * l, 0.40 * l, 0.34 * l);
    let (ny, nz) = (-uz, ux);
    let neck = l - head_l;
    let p = |a: f64, b: f64| [y + ux * a + ny * b, z + uz * a + nz * b];
    m.fill(&[p(0.0, -shaft_w / 2.0), p(neck, -shaft_w / 2.0), p(neck, shaft_w / 2.0), p(0.0, shaft_w / 2.0)],
           x, M_WHITE, false);
    m.fill(&[p(neck, -head_w / 2.0), p(l, 0.0), p(neck, head_w / 2.0)], x, M_WHITE, false);
}

fn thick_line(m: &mut Mesh, a: [f64; 2], b: [f64; 2], w: f64, x: f64) {
    let (dy, dz) = (b[0] - a[0], b[1] - a[1]);
    let n = (dy * dy + dz * dz).sqrt().max(1e-9);
    let (ny, nz) = (-dz / n * w / 2.0, dy / n * w / 2.0);
    m.fill(&[[a[0] - ny, a[1] - nz], [b[0] - ny, b[1] - nz], [b[0] + ny, b[1] + nz], [a[0] + ny, a[1] + nz]], x,
           M_WHITE, false);
}

fn draw_arrow(m: &mut Mesh, kind: Arrow, cy: f64, cz: f64, size: f64) {
    let x = INLAY_DEPTH;
    match kind {
        Arrow::Up => arrow_at(m, cy, cz - size / 2.0, 0.0, 1.0, size, x),
        Arrow::Diagonal(s) => {
            let (ux, uz) = (s.signum() * std::f64::consts::FRAC_1_SQRT_2, std::f64::consts::FRAC_1_SQRT_2);
            arrow_at(m, cy - ux * size / 2.0, cz - uz * size / 2.0, ux, uz, size, x)
        }
        Arrow::Curve(s) => {
            // a stem up the left column bending to the side at the top, its head pointing to the side
            let w = 0.08 * size.max(1.0);
            let (z0, z1) = (cz - size / 2.0, cz + size / 2.0 - 0.35);
            thick_line(m, [cy, z0], [cy, z1 - 0.25], w, x);
            let pts: Vec<[f64; 2]> = (0..=6).map(|k| {
                let a = std::f64::consts::FRAC_PI_2 * k as f64 / 6.0;
                [cy + s * 0.35 * (1.0 - a.cos()), z1 - 0.25 + 0.35 * a.sin()]
            }).collect();
            for v in pts.windows(2) {
                thick_line(m, v[0], v[1], w, x);
            }
            let end = pts[pts.len() - 1];
            arrow_at(m, end[0], end[1], s.signum(), 0.0, 0.45, x);
        }
        Arrow::Stem => {
            let w = 0.11;
            let (z0, z1) = (cz - size / 2.0, cz + size / 2.0);
            arrow_at(m, cy, z0, 0.0, 1.0, z1 - z0, x);
            let b = [cy, z0 + 0.35 * (z1 - z0)];
            let t = [cy + 0.5, b[1] + 0.5];
            thick_line(m, b, t, w, x);
            arrow_at(m, t[0] - 0.05, t[1] - 0.05, std::f64::consts::FRAC_1_SQRT_2, std::f64::consts::FRAC_1_SQRT_2,
                     0.45, x);
        }
    }
}

fn plate(m: &mut Mesh, font: &Font, kind: char, t: &str, cy: f64, cz: f64, w: f64, h: f64) {
    let x = INLAY_DEPTH;
    let cap = 0.52 * h;
    match kind {
        'B' => {
            let o = round_rect(cy - w / 2.0, cy + w / 2.0, cz - h / 2.0, cz + h / 2.0, 0.04);
            let i = round_rect(cy - w / 2.0 + 0.03, cy + w / 2.0 - 0.03, cz - h / 2.0 + 0.03, cz + h / 2.0 - 0.03, 0.02);
            m.ring(&o, &i, x, M_BLACK);
            m.fill(&i, x, M_YELLOW, false);
            text_centred(m, font, t, cap, cy, cz, M_LETTERS_BLACK);
        }
        'E' => {
            let o = round_rect(cy - w / 2.0, cy + w / 2.0, cz - h / 2.0, cz + h / 2.0, 0.04);
            let i = round_rect(cy - w / 2.0 + 0.03, cy + w / 2.0 - 0.03, cz - h / 2.0 + 0.03, cz + h / 2.0 - 0.03, 0.02);
            m.ring(&o, &i, x, M_WHITE);
            m.fill(&i, x, M_GREEN, false);
            text_centred(m, font, t, cap, cy, cz, M_LETTERS_WHITE);
        }
        _ => {
            // Zeichen 405 on a blue board: the white outline of the gabled plate, white numerals
            let hex = |s: f64| -> Vec<[f64; 2]> {
                [(7.0, 22.0), (100.0, 5.0), (193.0, 22.0), (193.0, 98.0), (100.0, 115.0), (7.0, 98.0)].iter()
                    .map(|(px, pz)| [cy + (px - 100.0) / 186.0 * w * s, cz - (pz - 60.0) / 110.0 * h * s]).collect()
            };
            let (o, i) = (hex(1.0), hex(0.86));
            let o: Vec<[f64; 2]> = o.into_iter().rev().collect();
            let i: Vec<[f64; 2]> = i.into_iter().rev().collect();
            m.ring(&o, &i, x, M_WHITE);
            text_centred(m, font, t, cap, cy, cz, M_LETTERS_WHITE);
        }
    }
}

fn ring_number(m: &mut Mesh, font: &Font, t: &str, cy: f64, cz: f64, h: f64) {
    let x = INLAY_DEPTH;
    let w = if t.chars().count() <= 2 { h } else { h * 1.62 };
    let o = round_rect(cy - w / 2.0, cy + w / 2.0, cz - h / 2.0, cz + h / 2.0, h / 2.0);
    let k = 0.075 * h;
    let i = round_rect(cy - w / 2.0 + k, cy + w / 2.0 - k, cz - h / 2.0 + k, cz + h / 2.0 - k, h / 2.0 - k);
    m.ring(&o, &i, x, M_WHITE);
    text_centred(m, font, t, 0.5 * h, cy, cz, M_LETTERS_WHITE);
}

fn picto(m: &mut Mesh, font: &Font, cp: u32, cy: f64, cz: f64, size: f64) {
    let Some(ch) = char::from_u32(cp) else { return };
    let Some(g) = font.glyph(2, ch) else { return };
    let h = size / 2.0;
    if cp >= WHITE_INK_BELOW {
        m.fill(&rect(cy - h, cy + h, cz - h, cz + h), INLAY_DEPTH, M_WHITE, false);
        let k = 0.1 * size;
        quad_uv(m, cy - h + k, cy + h - k, cz - h + k, cz + h - k, TEXT_DEPTH, M_LETTERS_BLACK, [g[5], g[6], g[7], g[8]]);
    } else {
        quad_uv(m, cy - h, cy + h, cz - h, cz + h, TEXT_DEPTH, M_LETTERS_WHITE, [g[5], g[6], g[7], g[8]]);
    }
}

fn oval(m: &mut Mesh, font: &Font, code: &str, cy: f64, cz: f64, h: f64) {
    let w = (0.9 * h + font.width(code, 0, 0.5 * h)).max(1.4 * h);
    let pts: Vec<[f64; 2]> = (0..24).map(|k| {
        let a = k as f64 * std::f64::consts::TAU / 24.0;
        [cy + w / 2.0 * a.cos(), cz + h / 2.0 * a.sin()]
    }).collect();
    m.fill(&pts, INLAY_DEPTH, M_WHITE, false);
    text_centred(m, font, code, 0.5 * h, cy, cz, M_LETTERS_BLACK);
}

fn panel(m: &mut Mesh, font: &Font, p: &Panel) {
    let (y0, y1, z0, z1) = (p.y0, p.y1, p.z0, p.z1);
    match p.shape {
        Shape::Gore => {
            let tip = 0.35;
            let o = vec![[y0, z0], [y1 - tip, z0], [y1, (z0 + z1) / 2.0], [y1 - tip, z1], [y0, z1]];
            let i = vec![[y0 + RIM, z0 + RIM], [y1 - tip - RIM * 0.4, z0 + RIM], [y1 - RIM * 1.4, (z0 + z1) / 2.0],
                         [y1 - tip - RIM * 0.4, z1 - RIM], [y0 + RIM, z1 - RIM]];
            m.ring(&o, &i, 0.0, M_WHITE);
            m.fill(&i, 0.0, M_BLUE, false);
            m.fill(&o, -0.004, M_METAL, true);
        }
        _ => {
            let r = if p.shape == Shape::Bake { 0.05 } else { RADIUS };
            let o = round_rect(y0, y1, z0, z1, r);
            let i = round_rect(y0 + RIM, y1 - RIM, z0 + RIM, z1 - RIM, r - RIM * 0.5);
            m.ring(&o, &i, 0.0, M_WHITE);
            m.fill(&i, 0.0, M_BLUE, false);
            m.fill(&o, -0.004, M_METAL, true);
        }
    }
    for it in &p.items {
        match it {
            Item::Text { s, face, cap, y, z, black } => {
                text(m, font, s, *face, *cap, *y, *z, if *black { M_LETTERS_BLACK } else { M_LETTERS_WHITE })
            }
            Item::Insert { y0, y1, z0, z1 } => m.fill(&round_rect(*y0, *y1, *z0, *z1, 0.03), INLAY_DEPTH, M_WHITE, false),
            Item::Plate { kind, text: t, cy, cz, w, h } => plate(m, font, *kind, t, *cy, *cz, *w, *h),
            Item::Arrow { kind, cy, cz, size } => draw_arrow(m, *kind, *cy, *cz, *size),
            Item::Ring { text: t, cy, cz, h } => ring_number(m, font, t, *cy, *cz, *h),
            Item::Picto { cp, cy, cz, size } => picto(m, font, *cp, *cy, *cz, *size),
            Item::Oval { code, cy, cz, h } => oval(m, font, code, *cy, *cz, *h),
            Item::Bars { n } => {
                let (fracs, t) = BAKE_BARS[(*n as usize).clamp(1, 3) - 1];
                let h = z1 - z0;
                let rise = 0.228 * h;
                for f in fracs {
                    let (za, zb) = (z1 - f * h, z1 - (f + t) * h);
                    let bar = vec![[y0, zb], [y1, zb + rise], [y1, za + rise], [y0, za]];
                    let c = clip(&bar, y0 + RIM, y1 - RIM, z0 + RIM, z1 - RIM);
                    if c.len() >= 3 {
                        m.fill(&c, INLAY_DEPTH, M_WHITE, false);
                    }
                }
            }
        }
    }
}

/// One assembly: its panels and what carries them (portal: posts on plinths, a box girder with a walkway rail;
/// cantilever: a post and an arm; beside the road, in the gore, on the ramp: slim posts).
pub(crate) fn draw(m: &mut Mesh, font: &Font, a: &Assembly) {
    for p in &a.panels {
        panel(m, font, p);
    }
    let heavy = matches!(a.mount, Mount::Portal | Mount::Cantilever);
    for post in &a.posts {
        if heavy {
            m.cuboid([-0.85, post.y - 0.25, post.z0], [-0.35, post.y + 0.25, post.z1], M_METAL);
            m.cuboid([-1.15, post.y - 0.6, post.z0 - 0.2], [-0.05, post.y + 0.6, post.z0 + 0.9], M_METAL);
        } else {
            m.cuboid([-0.08, post.y - 0.05, post.z0 - 0.2], [-0.02, post.y + 0.05, post.z1], M_METAL);
        }
    }
    if let Some((y0, y1, gz)) = a.girder {
        m.cuboid([-0.85, y0 - 0.25, gz - 0.3], [-0.35, y1 + 0.25, gz + 0.3], M_METAL);
        m.cuboid([-1.35, y0 - 0.25, gz + 0.3], [-0.1, y1 + 0.25, gz + 0.36], M_METAL);
        m.cuboid([-0.14, y0 - 0.25, gz + 1.36], [-0.1, y1 + 0.25, gz + 1.42], M_METAL);
        let n = ((y1 - y0) / 2.0).ceil().max(1.0) as usize;
        for k in 0..=n {
            let y = y0 + (y1 - y0) * k as f64 / n as f64;
            m.cuboid([-0.14, y - 0.025, gz + 0.36], [-0.1, y + 0.025, gz + 1.36], M_METAL);
        }
    }
    if let Some((y0, y1, gz)) = a.arm {
        m.cuboid([-0.8, y0, gz - 0.25], [-0.4, y1, gz + 0.25], M_METAL);
    }
}
