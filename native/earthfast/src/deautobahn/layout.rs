//! What a German Autobahn board looks like and what carries it (spec D, E; the mockups): panels in metres across the
//! carriageway (y, + to the driver's right, from the centreline) and up (z, absolute), their lines of destinations,
//! plates, arrows and pictograms, and the portal, cantilever or posts. Lettering: DIN 1451 Mittelschrift cap 420 mm
//! overhead, 350 mm beside the road, line pitch cap x 11/7 (BASt dWiSta 2022 Abb. 22-24; the RWB unit E = cap / 7).
//! Structures: clear height >= 5.0 m to the sign (ZTV-ING Teil 8 Abschnitt 3), cantilever reach <= 12 m ("soll"),
//! posts behind the barrier on concrete plinths; side boards on two posts 1.5 m from the edge, bottom 2.0 m (VwV-StVO).
use super::content::{Block, Content, Entry, Junction};
use super::stations::{Board, Mount, Station};

use std::collections::HashMap;

pub(crate) const CAP_OVER: f64 = 0.42;
pub(crate) const CAP_SIDE: f64 = 0.35;
pub(crate) const CLEARANCE_M: f64 = 5.2;     // panel bottom over the highest carriageway point (>= 5.0, ZTV-ING)
pub(crate) const SIDE_BOTTOM_M: f64 = 2.0;
pub(crate) const SIDE_OFFSET_M: f64 = 1.5;
pub(crate) const REACH_M: f64 = 12.0;
pub(crate) const POST_OUT_M: f64 = 1.5;      // posts this far outside the pavement edge, behind the barrier
pub(crate) const MEDIAN_CLEAR_M: f64 = 0.5;  // a median post keeps this from both carriageways' edges
pub(crate) const POST_W: f64 = 0.5;
pub(crate) const MAX_EXIT_W: f64 = 7.0;      // an exit panel this wide at most before its lines go condensed
pub(crate) const POST_CLEAR_M: f64 = 0.5;    // a post foot keeps this from any other pavement
pub(crate) const POST_OUT_MAX_M: f64 = 20.0;  // a gantry's post at most this far outside its carriageway's edge
pub(crate) const PICTO_EXIT: u32 = 0xE000;
pub(crate) const PICTO_INTERCHANGE: u32 = 0xE001;
pub(crate) const WHITE_INK_BELOW: u32 = 0xE010;

/// The lettering atlas (server/deautobahnfont.py tables): glyph rows [advance, left, bottom, right, top, u0, v0, u1, v1]
/// in cap units (y up) by (face, code point), the faces' [horizontal scale, letter spacing], kerning by (face, a, b).
pub(crate) struct Font {
    glyphs: HashMap<(i64, u32), [f64; 9]>,
    kern: HashMap<(i64, u32, u32), f64>,
    params: [[f64; 2]; 3],
    uniform: Option<f64>,
    symbols: HashMap<String, u32>,
}

/// deautobahnfont.PICTOGRAMS (the tests' default).
const SYMBOLS: &[(&str, u32)] = &[("motorway_junction", 0xE000), ("interchange", 0xE001), ("airport", 0xE010),
    ("harbour", 0xE011), ("ferry", 0xE011), ("industrial", 0xE012), ("train_station", 0xE013),
    ("railroad_freight_terminal", 0xE014), ("stadium", 0xE015), ("park_and_ride", 0xE016)];

impl Font {
    pub(crate) fn new(cps: &[i64], rows: &[[f64; 9]], faces: &[i64], params: [[f64; 2]; 3], kern: &[[f64; 4]],
                      symbols: HashMap<String, u32>) -> Font {
        let mut glyphs = HashMap::new();
        for ((cp, row), face) in cps.iter().zip(rows).zip(faces) {
            glyphs.insert((*face, *cp as u32), *row);
        }
        let kern = kern.iter().map(|k| ((k[0] as i64, k[1] as u32, k[2] as u32), k[3])).collect();
        Font { glyphs, kern, params, uniform: None, symbols }
    }

    /// Every character `advance` cap wide (tests).
    pub(crate) fn uniform(advance: f64) -> Font {
        Font { glyphs: HashMap::new(), kern: HashMap::new(), params: [[1.0, 0.0]; 3], uniform: Some(advance),
               symbols: SYMBOLS.iter().map(|(k, v)| (k.to_string(), *v)).collect() }
    }

    /// A destination:symbol's pictogram code point (0: none, left out).
    pub(crate) fn symbol(&self, sym: &str) -> u32 {
        self.symbols.get(sym).copied().unwrap_or(0)
    }

    pub(crate) fn glyph(&self, face: i64, ch: char) -> Option<[f64; 9]> {
        match self.uniform {
            Some(a) => Some([a, 0.04, 0.0, a - 0.04, 1.0, 0.0, 0.0, 1.0, 1.0]),
            None => self.glyphs.get(&(face, ch as u32)).copied(),
        }
    }

    pub(crate) fn kerning(&self, face: i64, a: char, b: char) -> f64 {
        self.kern.get(&(face, a as u32, b as u32)).copied().unwrap_or(0.0)
    }

    pub(crate) fn params(&self, face: i64) -> [f64; 2] {
        self.params.get(face.clamp(0, 2) as usize).copied().unwrap_or([1.0, 0.0])
    }

    /// The width of s in metres at cap height cap (missing characters count nothing).
    pub(crate) fn width(&self, s: &str, face: i64, cap: f64) -> f64 {
        let [scale, spacing] = self.params(face);
        let mut w = 0.0;
        let mut prev: Option<char> = None;
        for ch in s.chars() {
            if let Some(g) = self.glyph(face, ch) {
                if let Some(p) = prev {
                    w += self.kerning(face, p, ch);
                }
                w += g[0] * scale + spacing;
                prev = Some(ch);
            }
        }
        (w - spacing).max(0.0) * cap
    }
}

/// The carriageway at a station: its point and travel direction, the pavement edges (offsets, + right of travel), the
/// opposite carriageway's inner edge (NaN: none near), the main lanes, the ground at the post spots and the highest
/// carriageway z under a span (absolute).
#[derive(Clone, Copy, Debug)]
pub(crate) struct Geo {
    pub x: f64,
    pub y: f64,
    pub dx: f64,
    pub dy: f64,
    pub edge_l: f64,
    pub edge_r: f64,
    pub opp_edge: f64,
    pub lanes: f64,
    pub ground_l: f64,
    pub ground_r: f64,
    pub road_z: f64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Arrow {
    Up,
    Diagonal(f64), // +1 up-right, -1 up-left
    Curve(f64),    // a long arrow bending to the side (a Kreuz advance board)
    Stem,          // a side advance board's straight stem with a branch to the right
}

/// What is drawn on a panel face (y across, z up, absolute).
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Item {
    Text { s: String, face: i64, cap: f64, y: f64, z: f64, black: bool },
    Insert { y0: f64, y1: f64, z0: f64, z1: f64 },
    Plate { kind: char, text: String, cy: f64, cz: f64, w: f64, h: f64 },   // kind 'A', 'B', 'E' (else as 'A')
    Arrow { kind: Arrow, cy: f64, cz: f64, size: f64 },
    Ring { text: String, cy: f64, cz: f64, h: f64 },
    Picto { cp: u32, cy: f64, cz: f64, size: f64 },
    Oval { code: String, cy: f64, cz: f64, h: f64 },
    Bars { n: u8 },
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Shape {
    Board,
    Bake,
    Gore,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Panel {
    pub shape: Shape,
    pub y0: f64,
    pub y1: f64,
    pub z0: f64,
    pub z1: f64,
    pub lines: Vec<Vec<String>>,
    pub plates: Vec<String>,
    pub symbols: Vec<String>,
    pub number: Option<String>,
    pub printed: Option<String>,
    pub items: Vec<Item>,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Post {
    pub y: f64,
    pub z0: f64,
    pub z1: f64,
}

/// One board's assembly at its station: panels and what carries them; y relative to the station's centreline point.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Assembly {
    pub mount: Mount,
    pub panels: Vec<Panel>,
    pub posts: Vec<Post>,
    pub girder: Option<(f64, f64, f64)>, // y0, y1, centre z of a portal's girder
    pub arm: Option<(f64, f64, f64)>,    // y0, y1, centre z of a cantilever's arm
    pub anchor: f64,                     // y of the assembly's foot on the right verge (tile ownership)
}

/// The drawn lanes (roads.py: motorway lanes 4.0 m wide, the shoulder s = clip((W - n x 4) / 2, 0, 3) taken 0.5 s
/// inside and 1.5 s outside, edge_margin / motorway_shoulder): (first lane's left edge, lane width, lanes).
fn lane_layout(g: &Geo) -> (f64, f64, usize) {
    // (caller data: an absurd or missing count saturates, never sizes an allocation)
    let n = if g.lanes.is_finite() { g.lanes.round().clamp(1.0, 8.0) as usize } else { 3 };
    let w = (g.edge_r - g.edge_l).max(1.0);
    let s = ((w - n as f64 * 4.0) / 2.0).clamp(0.0, 3.0);
    let (start, end) = (g.edge_l + 0.5 * s, g.edge_r - 1.5 * s);
    (start, ((end - start) / n as f64).max(0.5), n)
}

fn lane_centre(g: &Geo, i: usize) -> f64 {
    let (start, lw, _) = lane_layout(g);
    start + (i as f64 + 0.5) * lw
}

fn right_edge_line(g: &Geo) -> f64 {
    let (start, lw, n) = lane_layout(g);
    start + n as f64 * lw
}

/// A route number's plate: 'A' (Zeichen 405), 'B' (401) or 'E' (410).
pub(crate) fn plate_kind(r: &str) -> char {
    match r.trim().chars().next() {
        Some('B') => 'B',
        Some('E') => 'E',
        _ => 'A',
    }
}

/// A plate's displayed text: the A-number's digits, a B-number's, an E-number as it is.
pub(crate) fn plate_text(r: &str) -> String {
    let t = r.trim();
    for p in ["A ", "B ", "A", "B"] {
        if let Some(rest) = t.strip_prefix(p) {
            if rest.trim_start().chars().next().is_some_and(|c| c.is_ascii_digit()) {
                return rest.trim().to_string();
            }
        }
    }
    t.to_string()
}

fn entry_label(e: &Entry) -> String {
    let mut s = if e.text.is_empty() { format!("[{}]", e.symbols.join(",")) } else { e.text.clone() };
    if let Some(c) = &e.country {
        s = format!("{s} ({c})");
    }
    s
}

fn short(e: &Entry) -> bool {
    !e.text.is_empty() && e.text.chars().count() <= 7 && !e.white
}

fn entry_width(e: &Entry, font: &Font, cap: f64) -> f64 {
    entry_width_face(e, font, cap, 0)
}

fn entry_width_face(e: &Entry, font: &Font, cap: f64, face: i64) -> f64 {
    let mut w = font.width(&e.text, face, cap);
    if e.white {
        w += 0.6 * cap;
    }
    if e.country.is_some() {
        w += 1.9 * cap;
    }
    w += e.symbols.len() as f64 * 1.8 * cap;
    w
}

const GAP: f64 = 1.2; // between two entries on one line, in cap

/// A block's entries as lines: two short names share a line ("Hof  Weiden", "Linz (A) Passau"), a pictogram-only or
/// white-insert entry joins the line before it when there is room; otherwise one entry a line, in sign order.
pub(crate) fn pack(b: &Block, font: &Font, cap: f64, max_w: f64) -> Vec<Vec<Entry>> {
    let mut lines: Vec<Vec<Entry>> = Vec::new();
    for e in &b.entries {
        if let Some(line) = lines.last_mut() {
            let w: f64 = line.iter().map(|x| entry_width(x, font, cap)).sum::<f64>()
                + GAP * cap * line.len() as f64 + entry_width(e, font, cap);
            let attach = (e.text.is_empty() || e.white) && w <= max_w;
            let pair = line.len() == 1 && short(&line[0]) && short(e) && w <= max_w;
            if attach || pair {
                line.push(e.clone());
                continue;
            }
        }
        lines.push(vec![e.clone()]);
    }
    lines
}

fn line_width(line: &[Entry], font: &Font, cap: f64) -> f64 {
    line_width_face(line, font, cap, 0)
}

fn line_width_face(line: &[Entry], font: &Font, cap: f64, face: i64) -> f64 {
    line.iter().map(|x| entry_width_face(x, font, cap, face)).sum::<f64>() + GAP * cap * (line.len().max(1) - 1) as f64
}

/// The announcement board's name lines: a district suffix takes its own line with a leading hyphen
/// ("Regensburg" / "-Ost"; StVO Anlage 3 Zeichen 448, VzKat 2017 "Düsseldorf / -Benrath").
pub(crate) fn announce_lines(name: &str) -> Vec<String> {
    if !name.contains(' ') {
        if let Some(k) = name.rfind('-') {
            let (a, b) = (&name[..k], &name[k + 1..]);
            if a.chars().count() >= 3 && b.chars().next().is_some_and(|c| c.is_uppercase()) {
                return vec![a.to_string(), format!("-{b}")];
            }
        }
    }
    vec![name.to_string()]
}

/// The items of packed lines left-aligned from y0, the first baseline at z_top - cap (white text, white inserts with
/// black text, country ovals, pictograms after their entry).
fn draw_lines(items: &mut Vec<Item>, lines: &[Vec<Entry>], font: &Font, cap: f64, y0: f64, z_top: f64) {
    draw_lines_face(items, lines, font, cap, y0, z_top, 0)
}

/// draw_lines in a face (1: the condensed one, for lines too long for their panel).
fn draw_lines_face(items: &mut Vec<Item>, lines: &[Vec<Entry>], font: &Font, cap: f64, y0: f64, z_top: f64, face: i64) {
    let pitch = cap * 11.0 / 7.0;
    for (i, line) in lines.iter().enumerate() {
        let z = z_top - cap - i as f64 * pitch;
        let mut y = y0;
        for e in line {
            if !e.text.is_empty() {
                let w = font.width(&e.text, face, cap);
                if e.white {
                    items.push(Item::Insert { y0: y, y1: y + w + 0.6 * cap, z0: z - 0.35 * cap, z1: z + 1.35 * cap });
                    y += 0.3 * cap;
                }
                items.push(Item::Text { s: e.text.clone(), face, cap, y, z, black: e.white });
                y += w + if e.white { 0.3 * cap } else { 0.0 };
            }
            if let Some(c) = &e.country {
                y += 0.4 * cap;
                items.push(Item::Oval { code: c.clone(), cy: y + 0.75 * cap, cz: z + 0.5 * cap, h: 1.1 * cap });
                y += 1.5 * cap;
            }
            for sym in &e.symbols {
                let cp = font.symbol(sym);
                if cp == 0 {
                    continue;
                }
                y += 0.3 * cap;
                items.push(Item::Picto { cp, cy: y + 0.75 * cap, cz: z + 0.5 * cap, size: 1.5 * cap });
                y += 1.5 * cap;
            }
            y += GAP * cap;
        }
    }
}

fn panel(shape: Shape, y0: f64, w: f64, z0: f64, h: f64) -> Panel {
    Panel { shape, y0, y1: y0 + w, z0, z1: z0 + h, lines: vec![], plates: vec![], symbols: vec![], number: None,
            printed: None, items: vec![] }
}

fn labels(lines: &[Vec<Entry>]) -> Vec<Vec<String>> {
    lines.iter().map(|l| l.iter().map(entry_label).collect()).collect()
}

fn symbols_of(lines: &[Vec<Entry>]) -> Vec<String> {
    lines.iter().flatten().flat_map(|e| e.symbols.iter().cloned()).collect()
}

fn kreuz(c: &Content) -> bool {
    matches!(c.kind, Junction::Kreuz | Junction::Dreieck)
}

/// The announcement board (Zeichen 448): pictogram and number ring on top, the name, the distance.
fn announce(st: &Station, c: &Content, font: &Font, cap: f64, z0: f64) -> Panel {
    let e = cap / 7.0;
    let margin = 5.0 * e;
    let pitch = cap * 11.0 / 7.0;
    let names = announce_lines(c.name.as_deref().unwrap_or(""));
    let printed = format!("{} m", st.printed_m.unwrap_or(0));
    let row_h = 2.0 * cap;
    let number = c.number.clone();
    let ring_w = number.as_ref().map(|n| if n.chars().count() <= 2 { row_h * 0.8 } else { row_h * 1.3 }).unwrap_or(0.0);
    let text_w = names.iter().chain(std::iter::once(&printed)).map(|l| font.width(l, 0, cap)).fold(0.0, f64::max);
    let w = text_w.max(row_h + 0.6 * cap + ring_w) + 2.0 * margin;
    let h = margin + row_h + 0.8 * cap + (names.len() as f64 + 1.0) * pitch - (pitch - cap) + margin;
    let mut p = panel(Shape::Board, 0.0, w, z0, h);
    let top = z0 + h - margin;
    let row_w = row_h + 0.6 * cap + ring_w;
    let rx = (w - row_w) / 2.0;
    let cp = if kreuz(c) { PICTO_INTERCHANGE } else { PICTO_EXIT };
    p.items.push(Item::Picto { cp, cy: rx + row_h / 2.0, cz: top - row_h / 2.0, size: row_h });
    if let Some(n) = &number {
        p.items.push(Item::Ring { text: n.clone(), cy: rx + row_h + 0.6 * cap + ring_w / 2.0, cz: top - row_h / 2.0,
                                  h: row_h * 0.8 });
    }
    let mut z = top - row_h - 0.8 * cap - cap;
    for l in names.iter().chain(std::iter::once(&printed)) {
        let lw = font.width(l, 0, cap);
        p.items.push(Item::Text { s: l.clone(), face: 0, cap, y: (w - lw) / 2.0, z, black: false });
        z -= pitch;
    }
    p.lines = names.iter().map(|n| vec![n.clone()]).collect();
    p.number = number;
    p.printed = Some(printed);
    p
}

/// A block's panel over the lanes it serves (overhead): its lines on top; through: an up-arrow over each lane and the
/// route plates between the arrows; exit: the arrow, plates and the distance in a bottom row (on a Kreuz advance board
/// a curved arrow in a left column; at a Kreuz split the diagonal arrow left of the lines and the plates centred in
/// the bottom row, as on the A93 gantries at Kreuz Regensburg).
pub(crate) fn block_panel(b: &Block, through: bool, st: &Station, c: &Content, font: &Font, cap: f64, z0: f64,
               lanes: &[f64]) -> Panel {
    let e = cap / 7.0;
    let margin = 5.0 * e;
    let pitch = cap * 11.0 / 7.0;
    let advance = matches!(st.board, Board::Advance(_));
    let curve = !through && advance && kreuz(c);
    let split = !through && !advance && kreuz(c);
    let left_col = if curve || split { 2.4 * cap } else { 0.0 };
    let lines = pack(b, font, cap, 8.0);
    let lane_span = match (lanes.first(), lanes.last()) {
        (Some(a), Some(z)) if through => (z - a) + 2.6,
        _ => 0.0,
    };
    // too long for the panel (the lanes' span on a through panel, MAX_EXIT_W on an exit panel): the condensed face
    let limit = if through { lane_span.max(4.0) - 2.0 * margin } else { MAX_EXIT_W - left_col - 2.0 * margin };
    let mut face = 0;
    let mut text_w = lines.iter().map(|l| line_width(l, font, cap)).fold(0.0, f64::max);
    if text_w > limit {
        face = 1;
        text_w = lines.iter().map(|l| line_width_face(l, font, cap, 1)).fold(0.0, f64::max);
    }
    let raw_plates: Vec<String> = b.plates.clone();
    let plates: Vec<String> = b.plates.iter().map(|r| plate_text(r)).collect();
    let printed = if advance && !through { st.printed_m.map(|m| format!("{m} m")) } else { None };
    let row = if through || !curve || printed.is_some() || !plates.is_empty() { 1.9 * cap } else { 0.0 };
    let mut row_w = 0.0;
    if !through {
        row_w = 2.0 * cap + plates.len() as f64 * 2.6 * cap + printed.as_ref().map(|p| font.width(p, 0, cap) + cap)
            .unwrap_or(0.0);
    }
    let w = (left_col + text_w.max(row_w) + 2.0 * margin).max(lane_span).max(2.5);
    let h = margin + (lines.len() as f64) * pitch - (pitch - cap) + 0.8 * cap + row + margin;
    let mut p = panel(Shape::Board, 0.0, w, z0, h);
    let top = z0 + h - margin;
    let x_text = if through { (w - text_w) / 2.0 } else { margin + left_col };
    draw_lines_face(&mut p.items, &lines, font, cap, x_text, top, face);
    let row_z = z0 + margin + 0.75 * cap;
    if through {
        let centres: Vec<f64> = if lanes.is_empty() { vec![w / 2.0] } else {
            let mid = (lanes[0] + lanes[lanes.len() - 1]) / 2.0;
            lanes.iter().map(|y| y - mid + w / 2.0).collect()
        };
        for cy in &centres {
            p.items.push(Item::Arrow { kind: Arrow::Up, cy: *cy, cz: row_z, size: 1.5 * cap });
        }
        if let Some(t) = raw_plates.first() {
            let spots: Vec<f64> = if centres.len() >= 2 {
                centres.windows(2).map(|v| (v[0] + v[1]) / 2.0).collect()
            } else {
                vec![centres[0] + 2.0 * cap]
            };
            for cy in spots {
                p.items.push(Item::Plate { kind: plate_kind(t), text: plate_text(t), cy, cz: row_z, w: 2.3 * cap, h: 1.38 * cap });
            }
        }
    } else {
        let side = 1.0;
        let mut y = margin;
        if curve {
            p.items.push(Item::Arrow { kind: Arrow::Curve(side), cy: margin + 1.0 * cap, cz: (z0 + top) / 2.0,
                                       size: (top - z0) * 0.8 });
            y = margin + left_col;
        } else if split {
            // the arrow beside the lines (centred between the first cap top and the last baseline), the plates side by
            // side in the bottom row, centred on the panel
            let last = top - cap - (lines.len().max(1) - 1) as f64 * pitch;
            p.items.push(Item::Arrow { kind: Arrow::Diagonal(side), cy: margin + 1.1 * cap, cz: (top + last) / 2.0,
                                       size: 1.6 * cap });
            let n = raw_plates.len() as f64;
            for (i, t) in raw_plates.iter().enumerate() {
                p.items.push(Item::Plate { kind: plate_kind(t), text: plate_text(t),
                                           cy: w / 2.0 + (i as f64 - (n - 1.0) / 2.0) * 2.6 * cap, cz: row_z,
                                           w: 2.3 * cap, h: 1.38 * cap });
            }
        } else {
            p.items.push(Item::Arrow { kind: Arrow::Diagonal(side), cy: y + 0.8 * cap, cz: row_z, size: 1.6 * cap });
            y += 2.0 * cap;
        }
        if !split {
            for t in &raw_plates {
                p.items.push(Item::Plate { kind: plate_kind(t), text: plate_text(t), cy: y + 1.15 * cap, cz: row_z, w: 2.3 * cap, h: 1.38 * cap });
                y += 2.6 * cap;
            }
        }
        if let Some(d) = &printed {
            p.items.push(Item::Text { s: d.clone(), face: 0, cap, y: y + 0.4 * cap, z: row_z - 0.5 * cap, black: false });
        }
    }
    p.lines = labels(&lines);
    p.plates = plates;
    p.symbols = symbols_of(&lines);
    p.printed = printed;
    p
}

/// The side advance board on two lanes (Zeichen 449, diagram style): the through destinations at the top beside the
/// route plate, a stem arrow, the exit destinations on its branch, the distance at the bottom.
fn side_advance(st: &Station, c: &Content, font: &Font, cap: f64, z0: f64) -> Panel {
    let e = cap / 7.0;
    let margin = 5.0 * e;
    let pitch = cap * 11.0 / 7.0;
    let col = 2.4 * cap;
    let mut blocks: Vec<(bool, Vec<Vec<Entry>>, Vec<String>)> = Vec::new();
    if let Some(t) = &c.through {
        blocks.push((true, pack(t, font, cap, 5.0), t.plates.clone()));
    }
    for b in &c.exits {
        blocks.push((false, pack(b, font, cap, 5.0), b.plates.clone()));
    }
    let printed = st.printed_m.map(|m| format!("{m} m"));
    let n_lines: usize = blocks.iter().map(|b| b.1.len()).sum();
    let text_w = blocks.iter().flat_map(|b| b.1.iter()).map(|l| line_width(l, font, cap)).fold(0.0, f64::max);
    let w = col + text_w + 2.6 * cap + 2.0 * margin;
    let h = margin + n_lines as f64 * pitch + blocks.len() as f64 * 0.8 * cap + pitch + margin;
    let mut p = panel(Shape::Board, 0.0, w, z0, h);
    let mut top = z0 + h - margin;
    p.items.push(Item::Arrow { kind: Arrow::Stem, cy: margin + 0.8 * cap, cz: (z0 + top) / 2.0, size: h - 2.0 * margin });
    let mut all = Vec::new();
    for (through, lines, plates) in &blocks {
        draw_lines(&mut p.items, lines, font, cap, margin + col, top);
        if let Some(t) = plates.first() {
            let y = margin + col + lines.first().map(|l| line_width(l, font, cap)).unwrap_or(0.0) + 1.5 * cap;
            p.items.push(Item::Plate { kind: plate_kind(t), text: plate_text(t), cy: y, cz: top - 0.5 * cap, w: 2.3 * cap, h: 1.38 * cap });
        }
        top -= lines.len() as f64 * pitch + 0.8 * cap;
        if !through {
            all.extend(plates.iter().map(|r| plate_text(r)));
        }
        p.lines.extend(labels(lines));
        p.symbols.extend(symbols_of(lines));
    }
    if let Some(d) = &printed {
        p.items.push(Item::Text { s: d.clone(), face: 0, cap, y: margin + col, z: z0 + margin, black: false });
    }
    p.plates = all;
    p.printed = printed;
    p
}

fn place(p: &mut Panel, y0: f64) {
    let w = p.y1 - p.y0;
    let dy = y0 - p.y0;
    p.y0 = y0;
    p.y1 = y0 + w;
    for it in p.items.iter_mut() {
        match it {
            Item::Text { y, .. } => *y += dy,
            Item::Insert { y0, y1, .. } => {
                *y0 += dy;
                *y1 += dy;
            }
            Item::Plate { cy, .. } | Item::Arrow { cy, .. } | Item::Ring { cy, .. } | Item::Picto { cy, .. }
            | Item::Oval { cy, .. } => *cy += dy,
            Item::Bars { .. } => {}
        }
    }
}

/// The portal's posts: right outside the pavement (or the panels); left outside it, in the median when the median
/// keeps MEDIAN_CLEAR_M to both carriageways, else outside the opposite carriageway too (one span over both).
fn portal(g: &Geo, panels: &[Panel]) -> (Vec<Post>, (f64, f64, f64)) {
    let right = (g.edge_r + POST_OUT_M).max(panels.iter().map(|p| p.y1 + 0.5).fold(f64::MIN, f64::max));
    let left = if g.opp_edge.is_finite() {
        let gap = g.edge_l - g.opp_edge;        // negative: the carriageways overlap
        if gap >= 2.0 * MEDIAN_CLEAR_M + POST_W {
            (g.edge_l + g.opp_edge) / 2.0
        } else {
            g.opp_edge - (g.edge_r - g.edge_l) - POST_OUT_M
        }
    } else {
        g.edge_l - POST_OUT_M
    }
    .min(panels.iter().map(|p| p.y0 - 0.5).fold(f64::MAX, f64::min));
    let top = panels.iter().map(|p| p.z1).fold(f64::MIN, f64::max);
    let z0 = panels.iter().map(|p| p.z0).fold(f64::MAX, f64::min);
    let gz = z0 + 0.6 * (top - z0);
    (vec![Post { y: left, z0: g.ground_l, z1: gz + 0.3 }, Post { y: right, z0: g.ground_r, z1: gz + 0.3 }],
     (left, right, gz))
}

/// The assembly of one station, or None (nothing to show, a fork: assemble_fork).
pub(crate) fn assemble(st: &Station, c: &Content, g: &Geo, font: &Font) -> Option<Assembly> {
    let overhead = matches!(st.mount, Mount::Portal | Mount::Cantilever);
    let cap = if overhead { CAP_OVER } else { CAP_SIDE };
    let z_over = g.road_z + CLEARANCE_M;
    let z_side = g.ground_r + SIDE_BOTTOM_M;
    let mut out = Assembly { mount: st.mount, panels: vec![], posts: vec![], girder: None, arm: None,
                             anchor: g.edge_r + POST_OUT_M };
    match st.board {
        Board::Announce => {
            let mut p = announce(st, c, font, cap, if overhead { z_over } else { z_side });
            let w = p.y1 - p.y0;
            let (start, lw, n) = lane_layout(g);
            let centre = match st.mount {
                Mount::Portal => start + n as f64 * lw / 2.0,
                Mount::Cantilever => lane_centre(g, n - 1),
                _ => g.edge_r + SIDE_OFFSET_M + w / 2.0,
            };
            place(&mut p, centre - w / 2.0);
            out.panels.push(p);
        }
        Board::Advance(_) | Board::Direction if overhead => {
            let (_, _, n) = lane_layout(g);
            let exit_lanes: Vec<usize> = c.exits.iter().flat_map(|b| b.lanes.iter().copied()).collect();
            let mut y_next = f64::MIN;
            if let Some(t) = &c.through {
                let mut idx: Vec<usize> = if t.lanes.is_empty() { (0..n).collect() } else { t.lanes.clone() };
                idx.retain(|&i| i < n && (!t.lanes.is_empty() || !exit_lanes.contains(&i) || exit_lanes.len() >= n));
                if idx.is_empty() {
                    idx = (0..n).collect();
                }
                let centres: Vec<f64> = idx.iter().map(|&i| lane_centre(g, i)).collect();
                let mut p = block_panel(t, true, st, c, font, cap, z_over, &centres);
                let mid = (centres[0] + centres[centres.len() - 1]) / 2.0;
                let w = p.y1 - p.y0;
                place(&mut p, mid - w / 2.0);
                y_next = p.y1 + 0.3;
                out.panels.push(p);
            }
            for b in &c.exits {
                let mut p = block_panel(b, false, st, c, font, cap, z_over, &[]);
                let w = p.y1 - p.y0;
                let want = if kreuz(c) && !b.lanes.is_empty() {
                    let cs: Vec<f64> = b.lanes.iter().map(|&i| lane_centre(g, i.min(n.max(1) - 1))).collect();
                    (cs[0] + cs[cs.len() - 1]) / 2.0 - w / 2.0
                } else if matches!(st.board, Board::Advance(_)) {
                    right_edge_line(g) + 0.5
                } else {
                    right_edge_line(g) + 0.2
                };
                place(&mut p, want.max(y_next));
                y_next = p.y1 + 0.3;
                out.panels.push(p);
            }
        }
        Board::Advance(_) => {
            let mut p = side_advance(st, c, font, cap, z_side);
            place(&mut p, g.edge_r + SIDE_OFFSET_M);
            out.panels.push(p);
        }
        Board::Direction => {
            let mut y = g.edge_r + SIDE_OFFSET_M;
            for b in &c.exits {
                let mut p = block_panel(b, false, st, c, font, cap, z_side, &[]);
                place(&mut p, y);
                y = p.y1 + 0.3;
                out.panels.push(p);
            }
        }
        Board::Countdown(n) => {
            let (w, h) = (0.65, 1.5);
            let mut p = panel(Shape::Bake, g.edge_r + SIDE_OFFSET_M, w, g.ground_r + 1.0, h);
            p.items.push(Item::Bars { n });
            let d = format!("{}", 100 * n as u32);
            p.items.push(Item::Text { s: d.clone(), face: 0, cap: 0.165, y: p.y0 + (w - font.width(&d, 0, 0.165)) / 2.0,
                                      z: p.z1 - 0.27 * h + 0.0, black: false });
            p.items.push(Item::Text { s: "m".into(), face: 0, cap: 0.165, y: p.y0 + (w - font.width("m", 0, 0.165)) / 2.0,
                                      z: p.z1 - 0.40 * h, black: false });
            p.printed = Some(format!("{d} m"));
            out.posts.push(Post { y: p.y0 + w / 2.0, z0: g.ground_r, z1: p.z0 + 0.2 });
            out.anchor = p.y0 + w / 2.0;
            out.panels.push(p);
            return Some(out);
        }
        Board::Gore => {
            let (w, h) = (1.9, 0.6);
            let mut p = panel(Shape::Gore, -w / 2.0, w, g.ground_r + 0.7, h);
            let tw = font.width("Ausfahrt", 0, 0.24);
            p.items.push(Item::Text { s: "Ausfahrt".into(), face: 0, cap: 0.24, y: -w / 2.0 + (w - 0.35 - tw) / 2.0 + 0.05,
                                      z: p.z0 + (h - 0.24) / 2.0, black: false });
            p.lines = vec![vec!["Ausfahrt".into()]];
            out.posts = vec![Post { y: -0.6, z0: g.ground_r, z1: p.z1 - 0.05 }, Post { y: 0.5, z0: g.ground_r, z1: p.z1 - 0.05 }];
            out.anchor = 0.0;
            out.panels.push(p);
            return Some(out);
        }
        Board::Fork => return None,
    }
    if out.panels.is_empty() {
        return None;
    }
    match out.mount {
        Mount::Cantilever => {
            let post = g.edge_r + POST_OUT_M;
            let y0 = out.panels.iter().map(|p| p.y0).fold(f64::MAX, f64::min) - 0.3;
            let top = out.panels.iter().map(|p| p.z1).fold(f64::MIN, f64::max);
            let z0 = out.panels.iter().map(|p| p.z0).fold(f64::MAX, f64::min);
            let gz = z0 + 0.6 * (top - z0);
            if post - y0 <= REACH_M {
                out.arm = Some((y0, post, gz));
                out.posts = vec![Post { y: post, z0: g.ground_r, z1: gz + 0.3 }];
            } else {
                out.mount = Mount::Portal;
                let (posts, girder) = portal(g, &out.panels);
                out.posts = posts;
                out.girder = Some(girder);
            }
        }
        Mount::Portal => {
            let (posts, girder) = portal(g, &out.panels);
            out.posts = posts;
            out.girder = Some(girder);
        }
        _ => {
            for p in &out.panels {
                let w = p.y1 - p.y0;
                for f in [0.22, 0.78] {
                    out.posts.push(Post { y: p.y0 + f * w, z0: g.ground_r, z1: p.z1 - 0.1 });
                }
            }
            out.anchor = out.panels[0].y0;
        }
    }
    Some(out)
}

/// Keep the assembly's posts off other pavements (blocks: occupied lateral intervals at the station, the opposite
/// carriageway, ramps, service roads and tracks): a portal's or cantilever's post goes outward past them (a
/// cantilever out of its 12 m reach becomes a portal); posts beside the road or in the gore cannot move, so false.
pub(crate) fn fit_posts(a: &mut Assembly, g: &Geo, blocks: &[(f64, f64)], _c: &Content, _st: &Station, _font: &Font)
                        -> bool {
    let half = POST_W / 2.0 + POST_CLEAR_M;
    let hit = |y: f64| blocks.iter().find(|(b0, b1)| y > b0.min(*b1) - half && y < b0.max(*b1) + half).copied();
    let push = |mut y: f64, outward: f64| -> Option<f64> {
        for _ in 0..32 {
            match hit(y) {
                None => return Some(y),
                Some((b0, b1)) => y = if outward > 0.0 { b0.max(b1) + half + 0.05 } else { b0.min(b1) - half - 0.05 },
            }
        }
        None
    };
    if a.mount == Mount::Cantilever {
        let Some(post) = a.posts.first().copied() else { return false };
        match push(post.y, 1.0) {
            Some(y) if a.arm.map(|(y0, _, _)| y - y0 <= REACH_M).unwrap_or(false) => {
                if let Some((y0, _, gz)) = a.arm {
                    a.arm = Some((y0, y, gz));
                }
                a.posts[0].y = y;
                return true;
            }
            _ => {
                a.mount = Mount::Portal;
                a.arm = None;
                let (posts, girder) = portal(g, &a.panels);
                a.posts = posts;
                a.girder = Some(girder);
            }
        }
    }
    if a.mount == Mount::Portal {
        let mut ys = Vec::new();
        for p in a.posts.iter_mut() {
            let outward = if p.y >= 0.0 { 1.0 } else { -1.0 };
            let edge = if outward > 0.0 { g.edge_r } else { g.edge_l };
            // a post pushed past ramps into the fields is no gantry: try another spot. On the left a span over the
            // opposite carriageway (its width about this one's, and the median) is room too.
            let room = if outward < 0.0 && g.opp_edge.is_finite() {
                POST_OUT_MAX_M + (g.edge_r - g.edge_l) + (g.edge_l - g.opp_edge).max(0.0)
            } else {
                POST_OUT_MAX_M
            };
            let start = p.y;
            match push(p.y, outward) {
                Some(y) if (y - start).abs() < 1e-9 || (y - edge).abs() <= room => p.y = y,
                _ => return false,
            }
            ys.push(p.y);
        }
        if let Some((_, _, gz)) = a.girder {
            let lo = ys.iter().copied().fold(f64::MAX, f64::min);
            let hi = ys.iter().copied().fold(f64::MIN, f64::max);
            a.girder = Some((lo, hi, gz));
        }
        return true;
    }
    a.posts.iter().all(|p| hit(p.y).is_none())
}

/// The fork boards on a Kreuz ramp (Gabelungswegweiser): one ground board per branch, side by side at the fork, the
/// arrow by the branch's direction (dirs: the branches' unit directions relative to the ramp, + to the right).
pub(crate) fn assemble_fork(c: &Content, g: &Geo, dirs: [(f64, f64); 2], font: &Font) -> Option<Assembly> {
    if c.forks.len() < 2 {
        return None;
    }
    let cap = CAP_SIDE;
    let e = cap / 7.0;
    let margin = 5.0 * e;
    let pitch = cap * 11.0 / 7.0;
    let mut out = Assembly { mount: Mount::Ground, panels: vec![], posts: vec![], girder: None, arm: None, anchor: 0.0 };
    let mut boards = Vec::new();
    for (b, (fwd, lat)) in c.forks.iter().zip(dirs) {
        let lines = pack(b, font, cap, 4.5);
        let text_w = lines.iter().map(|l| line_width(l, font, cap)).fold(0.0, f64::max);
        let plates: Vec<String> = b.plates.iter().map(|r| plate_text(r)).collect();
        let w = (text_w + 2.0 * margin).max(2.6 * cap + 2.0 * cap + 2.0 * margin);
        let h = margin + lines.len() as f64 * pitch - (pitch - cap) + 0.8 * cap + 1.9 * cap + margin;
        let z0 = g.ground_r + 0.9;
        let mut p = panel(Shape::Board, 0.0, w, z0, h);
        draw_lines(&mut p.items, &lines, font, cap, margin, z0 + h - margin);
        let row_z = z0 + margin + 0.75 * cap;
        if let Some(t) = b.plates.first() {
            p.items.push(Item::Plate { kind: plate_kind(t), text: plate_text(t), cy: margin + 1.15 * cap, cz: row_z, w: 2.3 * cap, h: 1.38 * cap });
        }
        let ang = lat.atan2(fwd).to_degrees();
        let kind = if ang > 15.0 { Arrow::Diagonal(1.0) } else if ang < -15.0 { Arrow::Diagonal(-1.0) } else { Arrow::Up };
        p.items.push(Item::Arrow { kind, cy: w - margin - 0.9 * cap, cz: row_z, size: 1.6 * cap });
        p.lines = labels(&lines);
        p.plates = plates;
        boards.push((p, lat));
    }
    boards.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
    let total: f64 = boards.iter().map(|(p, _)| p.y1 - p.y0).sum::<f64>() + 1.0;
    let mut y = -total / 2.0;
    for (mut p, _) in boards {
        let w = p.y1 - p.y0;
        place(&mut p, y);
        for f in [0.25, 0.75] {
            out.posts.push(Post { y: y + f * w, z0: g.ground_r, z1: p.z1 - 0.1 });
        }
        y += w + 1.0;
        out.panels.push(p);
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::deautobahn::content::{content, RAW_FIELDS};
    use crate::deautobahn::stations::{stations, Profile};

    fn font() -> Font {
        Font::uniform(0.6)
    }

    fn geo(lanes: f64, opp: f64) -> Geo {
        Geo { x: 0.0, y: 0.0, dx: 1.0, dy: 0.0, edge_l: -7.0, edge_r: 7.0, opp_edge: opp, lanes,
              ground_l: 100.0, ground_r: 100.0, road_z: 100.2 }
    }

    fn raw(pairs: &[(&str, &str)]) -> Vec<String> {
        RAW_FIELDS.iter()
            .map(|f| pairs.iter().find(|(k, _)| k == f).map(|(_, v)| v.to_string()).unwrap_or_default()).collect()
    }

    fn neutraubling() -> Content {
        let r = raw(&[
            ("node_name", "Neutraubling"), ("node_ref", "102"), ("main_ref", "A 3"),
            ("lanes_destination", "Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Neutraubling;Donaustauf;Barbing"),
            ("lanes_destination_country", "A;;|A;;|A;;|"), ("lanes_turn", "none|none|none|slight_right")]);
        let v: Vec<&str> = r.iter().map(|s| s.as_str()).collect();
        content(&v, Junction::Exit, 1).unwrap()
    }

    fn at(st: &[Station], b: Board) -> Station {
        *st.iter().find(|x| x.board == b).unwrap()
    }

    fn chain(c: &Content, lanes: f64) -> Vec<Station> {
        let s: Vec<f64> = (0..300).map(|i| i as f64 * 10.0).collect();
        let l = vec![lanes; s.len()];
        stations(c.kind, c, 125.0, 0.0, None, &Profile { s: &s, lanes: &l }, &[])
    }

    #[test]
    fn layout_neutraubling_eastbound() {
        let c = neutraubling();
        let st = chain(&c, 3.0);
        let a = assemble(&at(&st, Board::Advance(500)), &c, &geo(3.0, f64::NAN), &font()).unwrap();
        assert_eq!(a.mount, Mount::Portal);
        assert_eq!(a.panels.len(), 2);
        assert_eq!(a.panels[0].lines, vec![vec!["Linz (A)".to_string(), "Passau".to_string()], vec!["Rosenhof".to_string()]]);
        assert_eq!(a.panels[0].plates, vec!["3".to_string()]);
        assert_eq!(a.panels[1].lines, vec![vec!["Neutraubling".to_string()], vec!["Donaustauf".to_string()],
                                           vec!["Barbing".to_string()]]);
        assert_eq!(a.panels[1].printed.as_deref(), Some("500 m"));
        assert!(a.panels[1].y0 >= a.panels[0].y1, "the exit panel stands right of the through panel");
        let n = assemble(&at(&st, Board::Announce), &c, &geo(3.0, f64::NAN), &font()).unwrap();
        assert_eq!(n.mount, Mount::Cantilever);
        assert_eq!(n.panels[0].lines, vec![vec!["Neutraubling".to_string()]]);
        assert_eq!((n.panels[0].number.as_deref(), n.panels[0].printed.as_deref()), (Some("102"), Some("1000 m")));
    }

    #[test]
    fn line_packing_hof_weiden() {
        let names = ["Hof", "Weiden", "Regensburg", "München", "Ingolstadt"];
        let b = Block { entries: names.iter().map(|n| Entry { text: n.to_string(), ..Default::default() }).collect(),
                        plates: vec![], lanes: vec![] };
        let lines: Vec<Vec<String>> = pack(&b, &font(), 0.42, 20.0).iter()
            .map(|l| l.iter().map(|e| e.text.clone()).collect()).collect();
        assert_eq!(lines, vec![vec!["Hof".to_string(), "Weiden".to_string()], vec!["Regensburg".to_string()],
                               vec!["München".to_string()], vec!["Ingolstadt".to_string()]]);
    }

    #[test]
    fn district_suffix_own_line() {
        assert_eq!(announce_lines("Regensburg-Ost"), vec!["Regensburg".to_string(), "-Ost".to_string()]);
        assert_eq!(announce_lines("Neutraubling"), vec!["Neutraubling".to_string()]);
        assert_eq!(announce_lines("Kreuz Regensburg"), vec!["Kreuz Regensburg".to_string()]);
        assert_eq!(announce_lines("Ingolstadt-Nord"), vec!["Ingolstadt".to_string(), "-Nord".to_string()]);
    }

    #[test]
    fn portal_median_post_or_full_span() {
        let c = neutraubling();
        let st = at(&chain(&c, 3.0), Board::Advance(500));
        let wide = assemble(&st, &c, &geo(3.0, -9.0), &font()).unwrap();     // 2 m median: a post in its middle
        let left = wide.posts.iter().map(|p| p.y).fold(f64::INFINITY, f64::min);
        assert!((left + 8.0).abs() < 1e-6, "{left}");
        let tight = assemble(&st, &c, &geo(3.0, -7.4), &font()).unwrap();   // 0.4 m: over both carriageways
        let left = tight.posts.iter().map(|p| p.y).fold(f64::INFINITY, f64::min);
        assert!(left < -7.4 - 14.0, "{left}");
    }

    #[test]
    fn cantilever_reach_limit() {
        let c = neutraubling();
        let st = at(&chain(&c, 3.0), Board::Announce);
        let a = assemble(&st, &c, &geo(3.0, f64::NAN), &font()).unwrap();
        assert_eq!(a.mount, Mount::Cantilever);
        let (y0, y1, _) = a.arm.unwrap();
        assert!(y1 - y0 <= 12.0 && y1 > 7.0);
        let mut far = geo(3.0, f64::NAN);
        far.edge_r = 26.0;                 // a very wide carriageway: the right lane is out of a 12 m reach
        let b = assemble(&st, &c, &far, &font()).unwrap();
        assert_eq!(b.mount, Mount::Portal);
        assert!(b.arm.is_none() && b.girder.is_some());
    }

    #[test]
    fn lanes_follow_the_drawn_road() {
        // roads.py: motorway lanes 4.0 m, shoulder s = clip((W - n x 4) / 2, 0, 3), 0.5 s inside, 1.5 s outside
        let mut g = geo(3.0, f64::NAN);
        g.edge_l = -9.0;
        g.edge_r = 9.0;
        let c: Vec<f64> = (0..3).map(|i| lane_centre(&g, i)).collect();
        assert!(c.iter().zip([-5.5, -1.5, 2.5]).all(|(a, b)| (a - b).abs() < 1e-9), "{c:?}");
        assert!((right_edge_line(&g) - 4.5).abs() < 1e-9);
    }

    #[test]
    fn portal_over_overlapping_carriageways() {
        // the generated A3 carriageways overlap by 1.5 m: no median to stand a post in
        let c = neutraubling();
        let st = at(&chain(&c, 3.0), Board::Advance(500));
        let a = assemble(&st, &c, &geo(3.0, -5.5), &font()).unwrap();
        let left = a.posts.iter().map(|p| p.y).fold(f64::INFINITY, f64::min);
        assert!(left < -5.5 - 14.0, "{left}");
    }

    #[test]
    fn side_board_blocked_by_a_parallel_ramp() {
        // a link 1 m outside the edge, 8 m wide: the side board's posts would stand on it
        let c = neutraubling();
        let st = at(&chain(&c, 2.0), Board::Advance(500));
        let mut a = assemble(&st, &c, &geo(2.0, f64::NAN), &font()).unwrap();
        assert!(!fit_posts(&mut a, &geo(2.0, f64::NAN), &[(8.0, 16.0)], &c, &st, &font()));
    }

    #[test]
    fn portal_post_pushed_past_a_parallel_ramp() {
        let c = neutraubling();
        let st = at(&chain(&c, 3.0), Board::Advance(500));
        let g = geo(3.0, -7.6);
        let mut a = assemble(&st, &c, &g, &font()).unwrap();
        let before = a.posts.iter().map(|p| p.y).fold(f64::MIN, f64::max);
        // a ramp covering the right post's spot (8.5 .. 16), and the opposite carriageway over the left one
        assert!(fit_posts(&mut a, &g, &[(before - 1.6, 16.0), (-30.0, -7.6)], &c, &st, &font()));
        let right = a.posts.iter().map(|p| p.y).fold(f64::MIN, f64::max);
        let left = a.posts.iter().map(|p| p.y).fold(f64::MAX, f64::min);
        assert!(right >= 16.0 + 0.75, "{right}");
        assert!(left <= -30.0 - 0.75, "{left}");
        assert!(a.girder.unwrap().1 >= right - 1e-9 && a.girder.unwrap().0 <= left + 1e-9);
    }

    #[test]
    fn portal_post_cannot_go_far_out() {
        // ramps one after another to 60 m right of the edge: a post out there is no gantry, so no fit here
        let c = neutraubling();
        let st = at(&chain(&c, 3.0), Board::Advance(500));
        let g = geo(3.0, f64::NAN);
        let mut a = assemble(&st, &c, &g, &font()).unwrap();
        assert!(!fit_posts(&mut a, &g, &[(8.0, 30.0), (30.5, 67.0)], &c, &st, &font()));
    }

    #[test]
    fn cantilever_past_reach_becomes_a_portal() {
        let c = neutraubling();
        let st = at(&chain(&c, 3.0), Board::Announce);
        let g = geo(3.0, f64::NAN);
        let mut a = assemble(&st, &c, &g, &font()).unwrap();
        assert_eq!(a.mount, Mount::Cantilever);
        assert!(fit_posts(&mut a, &g, &[(8.0, 22.0)], &c, &st, &font()));
        assert_eq!(a.mount, Mount::Portal);
        assert!(a.posts.iter().all(|p| !(p.y > 7.5 && p.y < 22.5)));
    }

    #[test]
    fn long_names_use_the_condensed_face() {
        let b = Block { entries: vec![Entry { text: "Regensburg-Burgweinting Universitätsklinikum".into(), ..Default::default() }],
                        plates: vec![], lanes: vec![] };
        let c = Content { kind: Junction::Exit, number: None, name: None, through: None, exits: vec![b], forks: vec![] };
        let st = Station { board: Board::Direction, s: 100.0, printed_m: None, mount: Mount::Portal };
        let p = block_panel(&c.exits[0], false, &st, &c, &font(), CAP_OVER, 5.0, &[]);
        assert!(p.items.iter().any(|i| matches!(i, Item::Text { face: 1, .. })));
    }

    #[test]
    fn kreuz_exit_panel_plate_below_the_names() {
        // the A93 gantries at Kreuz Regensburg (photos 2023): the diagonal arrow left of the names, the A-plate centred
        // in a bottom row under them, clear of every line
        let b = Block { entries: ["Passau", "R.-Ost"].iter().map(|n| Entry { text: n.to_string(), ..Default::default() }).collect(),
                        plates: vec!["A 3".into()], lanes: vec![] };
        let c = Content { kind: Junction::Kreuz, number: None, name: None, through: None, exits: vec![b], forks: vec![] };
        let st = Station { board: Board::Direction, s: 100.0, printed_m: None, mount: Mount::Portal };
        let (f, cap) = (font(), CAP_OVER);
        let p = block_panel(&c.exits[0], false, &st, &c, &f, cap, 5.0, &[]);
        let text: Vec<(f64, f64)> = p.items.iter()
            .filter_map(|i| match i { Item::Text { y, z, .. } => Some((*y, *z)), _ => None }).collect();
        let lowest = text.iter().map(|t| t.1).fold(f64::INFINITY, f64::min);     // the last line's baseline
        let left = text.iter().map(|t| t.0).fold(f64::INFINITY, f64::min);
        let (pcy, pcz, ph) = p.items.iter()
            .find_map(|i| match i { Item::Plate { cy, cz, h, .. } => Some((*cy, *cz, *h)), _ => None }).unwrap();
        assert!(pcz + ph / 2.0 < lowest - 0.3 * cap, "the plate sits below the last line: {pcz} {lowest}");
        assert!((pcy - (p.y1 - p.y0) / 2.0).abs() < 1e-6, "the plate is centred on the panel: {pcy}");
        let (acy, acz, asz) = p.items.iter().find_map(|i| match i {
            Item::Arrow { kind: Arrow::Diagonal(_), cy, cz, size } => Some((*cy, *cz, *size)), _ => None }).unwrap();
        assert!(acy + asz / 2.0 < left, "the arrow stands left of the names");
        assert!(acz > lowest - 0.5 * cap, "the arrow stands beside the names, not in the bottom row: {acz} {lowest}");
    }

    #[test]
    fn absurd_lane_counts_saturate() {
        // no destination:lanes: the through panel spans all the lanes there are
        let r = raw(&[("node_name", "Neutraubling"), ("node_ref", "102"), ("main_ref", "A 3"),
                      ("link_destination", "Neutraubling;Barbing"), ("through_destination", "Linz;Passau")]);
        let v: Vec<&str> = r.iter().map(|s| s.as_str()).collect();
        let c = content(&v, Junction::Exit, 1).unwrap();
        let st = at(&chain(&c, 3.0), Board::Advance(500));
        for lanes in [1e12, f64::INFINITY, f64::NAN, -3.0] {
            assert!(assemble(&st, &c, &geo(lanes, f64::NAN), &font()).is_some());
        }
    }

    #[test]
    fn clearance_five_metres() {
        let c = neutraubling();
        for b in [Board::Advance(500), Board::Direction, Board::Announce] {
            let a = assemble(&at(&chain(&c, 3.0), b), &c, &geo(3.0, f64::NAN), &font()).unwrap();
            for p in &a.panels {
                assert!(p.z0 >= 100.2 + 5.0, "{b:?} {}", p.z0);
            }
        }
        let side = assemble(&at(&chain(&c, 2.0), Board::Advance(500)), &c, &geo(2.0, f64::NAN), &font()).unwrap();
        assert_eq!(side.mount, Mount::Side);
        assert!((side.panels[0].z0 - 102.0).abs() < 1e-6);
        assert!(side.panels[0].y0 >= 7.0 + 1.5 - 1e-6);
    }
}
