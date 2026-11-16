//! The street-development context of a road tile (server development.py, PR #670), whole: the features' labels
//! (dev_building_bits / dev_land_bits / dev_pois: each attribute value normalised as str().strip().lower() and matched
//! against the label tables here, OSM's JSON tags read where a land use has no class), the tile's bit raster
//! (DevContext: the footprints, land use and pedestrian places of its cells, with a halo) and its samples at the
//! sidewalk stations (DevContext.samples).
//!
//! The numbers are the Python's to the bit (development.py before 2026-11-10): numpy's pairwise summation where it
//! summed (the POI weights, the footprint areas' reduceat), the same operation order everywhere else. Strings arrive
//! as UCS-4 rows (fast.ucs4: no Python string per value; a city cell holds ~100k buildings and ~40k places).
use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

use crate::devraster;
use crate::roadlanes::FxBuild;

const PIXEL_M: f64 = 2.0;
const FRONTAGE_M: f64 = 120.0;

// label flags (development.py's tables)
const F_COM: u8 = 1;
const F_RES: u8 = 2;
const F_OUT: u8 = 4;
const F_IND: u8 = 8;
const F_PED: u8 = 16; // the value itself, or its part before ':', is a pedestrian place, or it ends in "_store"
const F_EMPTY: u8 = 32; // "" once normalised
const F_CLOSED: u8 = 64; // contains "closed"

const COMMERCIAL: &[&str] = &["commercial", "retail", "office", "offices", "shop", "supermarket", "government",
                              "civic", "public", "hotel"];
const RESIDENTIAL: &[&str] = &["residential", "house", "detached", "semidetached_house", "terrace", "apartments",
                               "dormitory", "bungalow"];
const OUTBUILDINGS: &[&str] = &["agricultural", "barn", "farm", "farm_auxiliary", "shed", "garage", "garages",
                                "greenhouse", "roof", "ruins", "construction", "abandoned"];
const INDUSTRIAL: &[&str] = &["industrial", "warehouse", "manufacture", "depot"];
const PEDESTRIAN: &[&str] = &["restaurant", "cafe", "coffee_shop", "bar", "pub", "bakery", "shop", "retail", "office",
    "offices", "bank", "pharmacy", "library", "museum", "theatre", "theater", "cinema", "post_office", "townhall",
    "community_centre", "university", "college", "school", "convenience_store", "grocery_store", "supermarket",
    "clothing_store", "bookstore", "hair_salon", "beauty_salon", "store", "professional_service", "financial_service",
    "technical_service", "corporate_or_business_office", "real_estate_service", "personal_or_beauty_service",
    "attorney_or_law_firm", "casual_eatery", "bank_or_credit_union", "art_gallery", "government_office",
    "educational_facility", "social_or_community_service", "civic_organization", "music_venue", "public_plaza",
    "non_alcoholic_beverage_venue"];

// channel bits: raw footprint, occupied footprint, commercial footprint, commercial / residential / industrial land
const B_RAW: u8 = 1;
const B_OCC: u8 = 2;
const B_COM: u8 = 4;
const L_COM: u8 = 8;
const L_RES: u8 = 16;
const L_IND: u8 = 32;

/// Python's str.isspace (strip's set): Unicode White_Space plus the separators U+001C..U+001F.
fn py_space(c: char) -> bool {
    matches!(c, '\t'..='\r' | '\u{1c}'..='\u{20}' | '\u{85}' | '\u{a0}' | '\u{1680}' | '\u{2000}'..='\u{200a}'
             | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}')
}

/// str(v).strip().lower() (with .replace(" ", "_") when under): a lone surrogate (numpy keeps them) as U+FFFD, which
/// neither strips nor lowers into a label either.
fn normalise(chars: impl Iterator<Item = char>, under: bool) -> String {
    let s: String = chars.collect();
    let t = s.trim_matches(py_space);
    let low = if t.is_ascii() { t.to_ascii_lowercase() } else { t.to_lowercase() };
    if under { low.replace(' ', "_") } else { low }
}

fn row_chars(row: &[u32]) -> impl Iterator<Item = char> + '_ {
    let end = row.iter().rposition(|&c| c != 0).map_or(0, |k| k + 1);     // (numpy drops trailing NULs)
    row[..end].iter().map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
}

fn flags_of(v: &str) -> u8 {
    let mut f = 0u8;
    if v.is_empty() {
        f |= F_EMPTY;
    }
    if COMMERCIAL.contains(&v) {
        f |= F_COM;
    }
    if RESIDENTIAL.contains(&v) {
        f |= F_RES;
    }
    if OUTBUILDINGS.contains(&v) {
        f |= F_OUT;
    }
    if INDUSTRIAL.contains(&v) {
        f |= F_IND;
    }
    let head = v.split(':').next().unwrap_or("");
    if PEDESTRIAN.contains(&v) || PEDESTRIAN.contains(&head) || v.ends_with("_store") {
        f |= F_PED;
    }
    if v.contains("closed") {
        f |= F_CLOSED;
    }
    f
}

/// Per row of a (n, w) UCS-4 column the flags of its normalised value (development._col), each distinct raw value
/// normalised once. None: the attribute is missing ("" for all).
fn column(col: Option<(&[u32], usize)>, n: usize) -> Vec<u8> {
    let Some((data, w)) = col else { return vec![F_EMPTY; n] };
    if w == 0 {
        return vec![F_EMPTY; n];
    }
    let mut memo: HashMap<&[u32], u8, FxBuild> = HashMap::default();
    let mut out: Vec<u8> = data.chunks_exact(w).take(n).map(|row| {
        *memo.entry(row).or_insert_with(|| flags_of(&normalise(row_chars(row), true)))
    }).collect();
    out.resize(n, F_EMPTY);
    out
}

// ---- json.loads, strict, as far as the tags fallback can tell values apart -------------------------------------

enum Json {
    Null,
    Bool(bool),
    Num(bool),          // (truthy: not zero)
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) | Json::Num(b) => *b,
            Json::Str(s) => !s.is_empty(),
            Json::Arr(a) => !a.is_empty(),
            Json::Obj(o) => !o.is_empty(),
        }
    }
    fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(o) => o.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v),     // (the last one wins)
            _ => None,
        }
    }
}

struct Parser<'a> {
    s: &'a [char],
    i: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], ' ' | '\t' | '\n' | '\r') {
            self.i += 1;
        }
    }
    fn peek(&self) -> Option<char> {
        self.s.get(self.i).copied()
    }
    fn lit(&mut self, word: &str) -> bool {
        let w: Vec<char> = word.chars().collect();
        if self.s.len() >= self.i + w.len() && self.s[self.i..self.i + w.len()] == w[..] {
            self.i += w.len();
            true
        } else {
            false
        }
    }
    fn hex4(&mut self) -> Option<u32> {
        if self.i + 4 > self.s.len() {
            return None;
        }
        let mut v = 0u32;
        for k in 0..4 {
            v = v * 16 + self.s[self.i + k].to_digit(16)?;
        }
        self.i += 4;
        Some(v)
    }
    fn string(&mut self) -> Option<String> {
        // (at the opening quote) json's py_scanstring, strict: no control characters, a lone surrogate kept (here
        // U+FFFD: neither a label nor empty)
        self.i += 1;
        let mut out = String::new();
        loop {
            let c = self.peek()?;
            self.i += 1;
            match c {
                '"' => return Some(out),
                '\\' => {
                    let e = self.peek()?;
                    self.i += 1;
                    match e {
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        '/' => out.push('/'),
                        'b' => out.push('\u{8}'),
                        'f' => out.push('\u{c}'),
                        'n' => out.push('\n'),
                        'r' => out.push('\r'),
                        't' => out.push('\t'),
                        'u' => {
                            let u = self.hex4()?;
                            let mut cp = u;
                            if (0xd800..0xdc00).contains(&u) && self.peek() == Some('\\')
                                && self.s.get(self.i + 1) == Some(&'u') {
                                let save = self.i;
                                self.i += 2;
                                match self.hex4() {
                                    Some(l) if (0xdc00..0xe000).contains(&l) => {
                                        cp = 0x10000 + ((u - 0xd800) << 10) + (l - 0xdc00);
                                    }
                                    Some(_) => self.i = save,
                                    None => return None,
                                }
                            }
                            out.push(char::from_u32(cp).unwrap_or('\u{fffd}'));
                        }
                        _ => return None,
                    }
                }
                c if (c as u32) < 0x20 => return None,
                c => out.push(c),
            }
        }
    }
    fn number(&mut self) -> Option<Json> {
        // -?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?; an int is falsy only as 0, a float when it reads as zero
        let start = self.i;
        if self.peek() == Some('-') {
            self.i += 1;
        }
        let d0 = self.i;
        match self.peek() {
            Some('0') => self.i += 1,
            Some('1'..='9') => {
                while matches!(self.peek(), Some('0'..='9')) {
                    self.i += 1;
                }
            }
            _ => return None,
        }
        let int_end = self.i;
        let mut float = false;
        if self.peek() == Some('.') && matches!(self.s.get(self.i + 1), Some('0'..='9')) {
            self.i += 1;
            while matches!(self.peek(), Some('0'..='9')) {
                self.i += 1;
            }
            float = true;
        }
        if matches!(self.peek(), Some('e' | 'E')) {
            let save = self.i;
            self.i += 1;
            if matches!(self.peek(), Some('+' | '-')) {
                self.i += 1;
            }
            if matches!(self.peek(), Some('0'..='9')) {
                while matches!(self.peek(), Some('0'..='9')) {
                    self.i += 1;
                }
                float = true;
            } else {
                self.i = save;
            }
        }
        if !float {
            return Some(Json::Num(self.s[d0..int_end].iter().any(|&c| c != '0')));
        }
        let text: String = self.s[start..self.i].iter().collect();
        let v: f64 = text.parse().ok()?;
        Some(Json::Num(v != 0.0))
    }
    fn value(&mut self) -> Option<Json> {
        match self.peek()? {
            '"' => self.string().map(Json::Str),
            '{' => {
                self.i += 1;
                self.ws();
                let mut o = Vec::new();
                if self.peek() == Some('}') {
                    self.i += 1;
                    return Some(Json::Obj(o));
                }
                loop {
                    if self.peek() != Some('"') {
                        return None;
                    }
                    let k = self.string()?;
                    self.ws();
                    if self.peek() != Some(':') {
                        return None;
                    }
                    self.i += 1;
                    self.ws();
                    let v = self.value()?;
                    o.push((k, v));
                    self.ws();
                    match self.peek()? {
                        ',' => {
                            self.i += 1;
                            self.ws();
                        }
                        '}' => {
                            self.i += 1;
                            return Some(Json::Obj(o));
                        }
                        _ => return None,
                    }
                }
            }
            '[' => {
                self.i += 1;
                self.ws();
                let mut a = Vec::new();
                if self.peek() == Some(']') {
                    self.i += 1;
                    return Some(Json::Arr(a));
                }
                loop {
                    a.push(self.value()?);
                    self.ws();
                    match self.peek()? {
                        ',' => {
                            self.i += 1;
                            self.ws();
                        }
                        ']' => {
                            self.i += 1;
                            return Some(Json::Arr(a));
                        }
                        _ => return None,
                    }
                }
            }
            'n' if self.lit("null") => Some(Json::Null),
            't' if self.lit("true") => Some(Json::Bool(true)),
            'f' if self.lit("false") => Some(Json::Bool(false)),
            'N' if self.lit("NaN") => Some(Json::Num(true)),
            'I' if self.lit("Infinity") => Some(Json::Num(true)),
            '-' if self.lit("-Infinity") => Some(Json::Num(true)),
            '-' | '0'..='9' => self.number(),
            _ => None,
        }
    }
}

/// json.loads(text) (None where it raises).
fn loads(text: &[char]) -> Option<Json> {
    if text.first() == Some(&'\u{feff}') {
        return None;
    }
    let mut p = Parser { s: text, i: 0 };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != text.len() {
        return None;
    }
    Some(v)
}

/// The flags of a land use's tags (str(tags) or "{}" read as JSON): of landuse, building, office, shop the first
/// whose value is not empty once str()-ed, stripped and lowered (no flags for a value that is no label).
fn tag_flags(row: &[u32]) -> u8 {
    let mut text: Vec<char> = row_chars(row).collect();
    if text.is_empty() {
        text = vec!['{', '}'];
    }
    let Some(t) = loads(&text) else { return F_EMPTY };
    // ([{"key", "value"}]: Overture's source tags form) -> {str(key): value}, the last one winning
    let pairs: Vec<(Option<&str>, Option<&Json>)> = match &t {
        Json::Obj(o) => o.iter().map(|(k, v)| (Some(k.as_str()), Some(v))).collect(),
        Json::Arr(a) => a.iter().filter_map(|d| match d {
            Json::Obj(_) => Some((match d.get("key") { Some(Json::Str(s)) => Some(s.as_str()), _ => None },
                                  d.get("value"))),
            _ => None,
        }).collect(),
        _ => return F_EMPTY,
    };
    for k in ["landuse", "building", "office", "shop"] {
        let Some((_, v)) = pairs.iter().rev().find(|(key, _)| *key == Some(k)) else { continue };
        match v {
            None => continue,
            Some(Json::Str(s)) => {
                let s = normalise(s.chars(), false);
                if !s.is_empty() {
                    return flags_of(&s) & !F_EMPTY;
                }
            }
            Some(j) if j.truthy() => return 0,      // (a number, true, a list or an object: no label)
            Some(_) => continue,
        }
    }
    F_EMPTY
}

fn ucs4<'a>(a: &'a Option<PyReadonlyArray2<'_, u32>>) -> PyResult<Option<(&'a [u32], usize)>> {
    match a {
        None => Ok(None),
        Some(a) => Ok(Some((a.as_slice().map_err(|e| PyValueError::new_err(e.to_string()))?, a.shape()[1]))),
    }
}

/// development._bits of a buildings layer: (n,) channel bits (polygons above ground: raw; not an outbuilding:
/// occupied; and commercial).
#[pyfunction]
#[pyo3(signature = (poly, cls, sub, under))]
pub fn dev_building_bits<'py>(py: Python<'py>, poly: PyReadonlyArray1<'py, bool>,
                              cls: Option<PyReadonlyArray2<'py, u32>>,
                              sub: Option<PyReadonlyArray2<'py, u32>>, under: Option<PyReadonlyArray1<'py, bool>>)
                              -> PyResult<Bound<'py, PyArray1<u8>>> {
    let poly = poly.as_slice()?;
    let n = poly.len();
    let (c, s) = (ucs4(&cls)?, ucs4(&sub)?);
    let under = match &under {
        Some(u) => Some(u.as_slice()?),
        None => None,
    };
    let out = py.detach(|| {
        let (fc, fs) = (column(c, n), column(s, n));
        (0..n).map(|i| {
            let ok = poly[i] && !under.is_some_and(|u| u[i]);
            if !ok {
                return 0;
            }
            let has = fc[i] | fs[i];
            let mut b = B_RAW;
            if has & F_OUT == 0 {
                b |= B_OCC;
                if has & F_COM != 0 {
                    b |= B_COM;
                }
            }
            b
        }).collect::<Vec<u8>>()
    });
    Ok(out.into_pyarray(py))
}

/// development._bits of a land use layer: (n,) channel bits (commercial / residential / industrial polygons, by
/// class or subtype; a polygon without a class by its OSM tags).
#[pyfunction]
#[pyo3(signature = (poly, cls, sub, tags))]
pub fn dev_land_bits<'py>(py: Python<'py>, poly: PyReadonlyArray1<'py, bool>, cls: Option<PyReadonlyArray2<'py, u32>>,
                          sub: Option<PyReadonlyArray2<'py, u32>>, tags: Option<PyReadonlyArray2<'py, u32>>)
                          -> PyResult<Bound<'py, PyArray1<u8>>> {
    let poly = poly.as_slice()?;
    let n = poly.len();
    let (c, s, t) = (ucs4(&cls)?, ucs4(&sub)?, ucs4(&tags)?);
    let out = py.detach(|| {
        let (fc, fs) = (column(c, n), column(s, n));
        (0..n).map(|i| {
            if !poly[i] {
                return 0;
            }
            let mut has_c = fc[i];
            if has_c & F_EMPTY != 0 {
                if let Some((data, w)) = t {
                    has_c = tag_flags(data.get(i * w..(i + 1) * w).unwrap_or(&[]));
                }
            }
            let has = has_c | fs[i];
            let mut b = 0u8;
            if has & F_COM != 0 {
                b |= L_COM;
            }
            if has & F_RES != 0 {
                b |= L_RES;
            }
            if has & F_IND != 0 {
                b |= L_IND;
            }
            b
        }).collect::<Vec<u8>>()
    });
    Ok(out.into_pyarray(py))
}

/// development._pois: (m,) indices of the pedestrian places (shops, cafes, offices ...), open (no "closed" in the
/// status), confidence >= 0.5 where it is known.
#[pyfunction]
#[pyo3(signature = (n, status, cat, tax, conf))]
pub fn dev_pois<'py>(py: Python<'py>, n: usize, status: Option<PyReadonlyArray2<'py, u32>>,
                     cat: Option<PyReadonlyArray2<'py, u32>>, tax: Option<PyReadonlyArray2<'py, u32>>,
                     conf: Option<PyReadonlyArray1<'py, f64>>) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (st, ca, ta) = (ucs4(&status)?, ucs4(&cat)?, ucs4(&tax)?);
    let conf = match &conf {
        Some(c) => Some(c.as_slice()?),
        None => None,
    };
    let out = py.detach(|| {
        let (fs, fc, ft) = (column(st, n), column(ca, n), column(ta, n));
        (0..n).filter(|&i| {
            fs[i] & F_CLOSED == 0 && !conf.is_some_and(|c| c[i].is_finite() && c[i] < 0.5)
                && (fc[i] | ft[i]) & F_PED != 0
        }).map(|i| i as i64).collect::<Vec<i64>>()
    });
    Ok(out.into_pyarray(py))
}

// ---- the raster ---------------------------------------------------------------------------------------------

/// numpy's pairwise summation (pairwise_sum_DOUBLE: 8 accumulators up to 128 values, halves beyond), 0.0 first.
pub(crate) fn pairwise(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut r = 0.0;
        for &v in a {
            r += v;
        }
        r
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - n % 8 {
            for j in 0..8 {
                r[j] += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise(&a[..n2]) + pairwise(&a[n2..])
    }
}

/// np.add.reduceat's value of one segment: its first value, then the rest pairwise.
fn reduce_seg(a: &[f64]) -> f64 {
    match a.len() {
        0 => 0.0,
        1 => a[0],
        _ => a[0] + pairwise(&a[1..]),
    }
}

fn smooth(value: f64, lo: f64, hi: f64) -> f64 {
    let t = pmax(0., pmin(1., (value - lo) / (hi - lo)));
    t * t * (3. - 2. * t)
}

/// Python's max(a, b) / min(a, b): the first unless the second is greater / smaller.
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

fn trimmed(row: &[u32]) -> &[u32] {
    &row[..row.iter().rposition(|&c| c != 0).map_or(0, |k| k + 1)]
}

type PolyLayer<'py> = (bool, PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                       PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, u8>, PyReadonlyArray2<'py, f64>,
                       PyReadonlyArray2<'py, u32>);
type PoiLayer<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                      PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray2<'py, u32>);

struct Poly<'a> {
    building: bool,
    xy: &'a [f64],
    part_off: &'a [i64],
    ring_off: &'a [i64],
    coord_off: &'a [i64],
    bits: &'a [u8],
    boxes: &'a [f64],
    ids: (&'a [u32], usize),
}

struct Pois<'a> {
    xy: &'a [f64],
    part_off: &'a [i64],
    ring_off: &'a [i64],
    coord_off: &'a [i64],
    idx: &'a [i64],
    ids: (&'a [u32], usize),
}

fn at(v: &[i64], k: i64) -> Option<usize> {
    if k < 0 {
        return None;
    }
    v.get(k as usize).and_then(|&x| if x < 0 { None } else { Some(x as usize) })
}

fn slice2<'a>(a: &'a PyReadonlyArray2<'_, f64>, cols: usize, what: &str) -> PyResult<&'a [f64]> {
    if a.shape()[1] != cols && a.shape()[0] != 0 {
        return Err(PyValueError::new_err(format!("{what} must be (n, {cols})")));
    }
    a.as_slice().map_err(|e| PyValueError::new_err(e.to_string()))
}

/// A road tile's development raster (development.Context's index): channel prefix sums per row, the pedestrian
/// places, the circle's row spans.
#[pyclass(module = "earthfast")]
pub struct DevContext {
    origin: [f64; 2],
    h: usize,
    w: usize,
    occ: Vec<u8>,
    sums: Vec<i32>,     // (6, h, w + 1)
    pois: Vec<[f64; 2]>,
    dy: Vec<i64>,
    dx: Vec<i64>,
    area: f64,
    r2: f64,
}

impl DevContext {
    fn circle(&self, px: f64, py: f64) -> [f64; 6] {
        let col = ((px - self.origin[0]) / PIXEL_M).floor() as i64;
        let row = ((py - self.origin[1]) / PIXEL_M).floor() as i64;
        let (h, w) = (self.h as i64, self.w as i64);
        let mut acc = [0i64; 6];
        for (k, &dy) in self.dy.iter().enumerate() {
            let rr = row + dy;
            if rr < 0 || rr >= h {
                continue;
            }
            let x0 = (col - self.dx[k]).clamp(0, w) as usize;
            let x1 = (col + self.dx[k] + 1).clamp(0, w) as usize;
            for (c, a) in acc.iter_mut().enumerate() {
                let base = (c * self.h + rr as usize) * (self.w + 1);
                *a += (self.sums[base + x1] - self.sums[base + x0]) as i64;
            }
        }
        acc.map(|v| v as f64 / self.area)
    }

    fn frontage(&self, px: f64, py: f64, ox: f64, oy: f64, half: f64) -> f64 {
        let (ax, ay) = (-oy, ox);
        let offs = [half + 6., half + 14., half + 22., half + 30.];
        let n = 61usize;            // np.arange(-FRONTAGE_M / 2, FRONTAGE_M / 2 + .1, PIXEL_M)
        let mut hit = [false; 61];
        for (i, h) in hit.iter_mut().enumerate() {
            let s = -FRONTAGE_M / 2. + i as f64 * PIXEL_M;
            let (bx, by) = (px + s * ax, py + s * ay);
            *h = offs.iter().any(|&o| {
                let (qx, qy) = (bx + o * ox, by + o * oy);
                let c = ((qx - self.origin[0]) / PIXEL_M).floor() as i64;
                let r = ((qy - self.origin[1]) / PIXEL_M).floor() as i64;
                c >= 0 && (c as usize) < self.w && r >= 0 && (r as usize) < self.h
                    && self.occ[r as usize * self.w + c as usize] > 0
            });
        }
        let a = hit.iter().filter(|&&v| v).count() as f64 / n as f64;
        let b = hit.windows(2).filter(|p| p[0] && p[1]).count() as f64 / (n - 1) as f64;
        0.5 * (a + b)
    }

    /// development.Context.sample's fields: coverage, frontage, commercial, poi, score, tier, limited, urban,
    /// residential.
    fn sample(&self, px: f64, py: f64, ox: f64, oy: f64, half: f64, terms: &mut Vec<f64>) -> [f64; 9] {
        let norm = (ox * ox + oy * oy).sqrt();
        let d = pmax(norm, 1e-9);
        let (ox, oy) = (ox / d, oy / d);
        let [coverage, built, commercial_built, commercial_land, residential, industrial] = self.circle(px, py);
        let frontage = self.frontage(px, py, ox, oy, half);
        terms.clear();
        terms.extend(self.pois.iter().map(|q| {
            let (dx, dy) = (q[0] - px, q[1] - py);
            let v = 1. - (dx * dx + dy * dy) / self.r2;
            if v.is_nan() || v > 0. { v } else { 0. }        // (np.maximum(0., v))
        }));
        let poi = 0.0 + pairwise(terms);
        let commercial = pmin(1., pmax(pmax(commercial_built / pmax(built, 0.04), commercial_land / 0.35), poi / 4.));
        let score = pmin(1., 0.5 * pmin(built / 0.28, 1.) + 0.3 * frontage + 0.15 * commercial
                         + 0.05 * pmin(poi / 4., 1.));
        let limited = smooth(built, 0.025, 0.14) * smooth(score, 0.13, 0.32);
        let mut urban = limited * smooth(built, 0.10, 0.22) * smooth(frontage, 0.25, 0.65)
            * smooth(commercial, 0.15, 0.65) * smooth(score, 0.45, 0.70);
        urban *= 1. - smooth(industrial, 0.3, 0.7) * (1. - smooth(poi, 1., 4.));
        let tier = if urban >= 0.5 { 2. } else if limited > 0. { 1. } else { 0. };
        [coverage, frontage, commercial, poi, score, tier, limited, urban, residential]
    }
}

#[allow(clippy::too_many_arguments)]
fn build(origin: [f64; 2], lo: [f64; 2], hi: [f64; 2], h: usize, w: usize, radius: f64, r2: f64, max_area: f64,
         polys: &[Poly], pois_in: &[Pois]) -> DevContext {
    let mut xs: Vec<[f64; 2]> = Vec::new();
    let mut ring_off: Vec<i64> = Vec::new();
    let mut part_off: Vec<i64> = Vec::new();
    let mut part_bits: Vec<u8> = Vec::new();
    let mut seen: HashSet<&[u32], FxBuild> = HashSet::default();
    let mut cross: Vec<f64> = Vec::new();
    for f in polys {
        let n = f.bits.len().min(f.boxes.len() / 4).min(f.part_off.len().saturating_sub(1));
        let (ids, iw) = f.ids;
        let mut idx: Vec<usize> = (0..n).filter(|&i| {
            let b = &f.boxes[4 * i..4 * i + 4];
            f.bits[i] > 0 && b[0] <= hi[0] && b[2] >= lo[0] && b[1] <= hi[1] && b[3] >= lo[1]
        }).collect();
        if f.building && !idx.is_empty() {          // (a building in two cells: once)
            let id = |i: usize| if iw == 0 { &ids[..0] } else { trimmed(&ids[i * iw..(i + 1) * iw]) };
            idx.retain(|&i| !seen.contains(id(i)));
            for &i in &idx {
                seen.insert(id(i));
            }
        }
        for &i in &idx {
            let (Some(p0), Some(p1)) = (at(f.part_off, i as i64), at(f.part_off, i as i64 + 1)) else { continue };
            for p in p0..p1 {
                let (Some(r0), Some(r1)) = (at(f.ring_off, p as i64), at(f.ring_off, p as i64 + 1)) else { continue };
                let mut bits = f.bits[i];
                let first_ring = ring_off.len();
                part_off.push(first_ring as i64);
                for r in r0..r1 {
                    let (Some(c0), Some(c1)) = (at(f.coord_off, r as i64), at(f.coord_off, r as i64 + 1)) else {
                        ring_off.push(xs.len() as i64);
                        continue;
                    };
                    let start = xs.len();
                    ring_off.push(start as i64);
                    for c in c0..c1.min(f.xy.len() / 2) {
                        xs.push([(f.xy[2 * c] - lo[0]) / PIXEL_M, (f.xy[2 * c + 1] - lo[1]) / PIXEL_M]);
                    }
                    if f.building && r == r0 {       // MapData's oversized-footprint veto (each part's outline)
                        let ring = &xs[start..];
                        let k = ring.len();
                        cross.clear();
                        for j in 0..k {
                            let (a, b) = (ring[j], ring[(j + 1) % k]);
                            let (ax, ay, bx, by) = (a[0] * PIXEL_M, a[1] * PIXEL_M, b[0] * PIXEL_M, b[1] * PIXEL_M);
                            cross.push(ax * by - bx * ay);
                        }
                        if 0.5 * reduce_seg(&cross).abs() > max_area {
                            bits = 0;
                        }
                    }
                }
                part_bits.push(bits);
            }
        }
    }
    ring_off.push(xs.len() as i64);
    part_off.push((ring_off.len() - 1) as i64);
    let grid = if part_bits.is_empty() { vec![0u8; h * w] } else { devraster::fill(h, w, &xs, &ring_off, &part_off,
                                                                                   &part_bits) };
    drop(xs);
    let mut sums = vec![0i32; 6 * h * (w + 1)];
    for c in 0..6 {
        for r in 0..h {
            let base = (c * h + r) * (w + 1);
            let mut acc = 0i32;
            for x in 0..w {
                acc += ((grid[r * w + x] >> c) & 1) as i32;
                sums[base + x + 1] = acc;
            }
        }
    }
    let occ: Vec<u8> = grid.iter().map(|&g| (g >> 1) & 1).collect();
    // the pedestrian places' points (each feature's first coordinate), each id once
    let mut pois = Vec::new();
    let mut seen_poi: HashSet<&[u32], FxBuild> = HashSet::default();
    for f in pois_in {
        let (ids, iw) = f.ids;
        for &i in f.idx {
            let first = at(f.part_off, i).and_then(|p| at(f.ring_off, p as i64))
                .and_then(|r| at(f.coord_off, r as i64));
            let Some(c) = first else { continue };
            if 2 * c + 1 >= f.xy.len() {
                continue;
            }
            let (x, y) = (f.xy[2 * c], f.xy[2 * c + 1]);
            if !(x >= lo[0] && x <= hi[0] && y >= lo[1] && y <= hi[1]) {
                continue;
            }
            let id = if iw == 0 { &ids[..0] } else { trimmed(&ids[i as usize * iw..(i as usize + 1) * iw]) };
            if seen_poi.insert(id) {
                pois.push([x, y]);
            }
        }
    }
    let r = radius / PIXEL_M;
    let ri = r as i64;
    let dy: Vec<i64> = (-ri..=ri).collect();
    let dx: Vec<i64> = dy.iter().map(|&d| pmax(0., r * r - (d * d) as f64).sqrt().floor() as i64).collect();
    let area = dx.iter().map(|&d| 2 * d + 1).sum::<i64>() as f64;
    DevContext { origin, h, w, occ, sums, pois, dy, dx, area, r2 }
}

#[pymethods]
impl DevContext {
    /// The raster of a tile: origin (its lower left, on the PIXEL_M lattice), lo / hi the halo box features must
    /// reach, (h, w) pixels, radius (m), r2 its square (Python's radius ** 2), max_area (MapData's BUILDING_MAX_M2).
    /// polys: per buildings / land use Features (building, xy (N, 2) in the tile frame, part_off, ring_off,
    /// coord_off, bits (dev_*_bits), boxes (n, 4), ids (n, w) UCS-4), in layer order; pois: per places Features
    /// (xy, part_off, ring_off, coord_off, idx (dev_pois), ids).
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(py: Python<'_>, origin: (f64, f64), lo: (f64, f64), hi: (f64, f64), h: usize, w: usize, radius: f64,
           r2: f64, max_area: f64, polys: Vec<PolyLayer<'_>>, pois: Vec<PoiLayer<'_>>) -> PyResult<Self> {
        let mut pl = Vec::with_capacity(polys.len());
        for (building, xy, po, ro, co, bits, boxes, ids) in &polys {
            pl.push(Poly {
                building: *building,
                xy: slice2(xy, 2, "xy")?,
                part_off: po.as_slice()?,
                ring_off: ro.as_slice()?,
                coord_off: co.as_slice()?,
                bits: bits.as_slice()?,
                boxes: slice2(boxes, 4, "boxes")?,
                ids: (ids.as_slice()?, ids.shape()[1]),
            });
        }
        let mut ql = Vec::with_capacity(pois.len());
        for (xy, po, ro, co, idx, ids) in &pois {
            ql.push(Pois {
                xy: slice2(xy, 2, "xy")?,
                part_off: po.as_slice()?,
                ring_off: ro.as_slice()?,
                coord_off: co.as_slice()?,
                idx: idx.as_slice()?,
                ids: (ids.as_slice()?, ids.shape()[1]),
            });
        }
        Ok(py.detach(|| build([origin.0, origin.1], [lo.0, lo.1], [hi.0, hi.1], h, w, radius, r2, max_area, &pl,
                              &ql)))
    }

    /// (n, 9) per point the sample's fields (coverage, frontage, commercial, poi, score, tier, limited, urban,
    /// residential): p (n, 2) road centreline points, out (n, 2) toward the sidewalk's side, half (n,) the road's
    /// half width.
    fn samples<'py>(&self, py: Python<'py>, p: PyReadonlyArray2<'py, f64>, out: PyReadonlyArray2<'py, f64>,
                    half: PyReadonlyArray1<'py, f64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
        let (p, o, hf) = (slice2(&p, 2, "p")?, slice2(&out, 2, "out")?, half.as_slice()?);
        let n = hf.len();
        if p.len() != 2 * n || o.len() != 2 * n {
            return Err(PyValueError::new_err("p, out and half differ in length"));
        }
        let v = py.detach(|| {
            let mut terms = Vec::with_capacity(self.pois.len());
            let mut v = Vec::with_capacity(9 * n);
            for i in 0..n {
                v.extend(self.sample(p[2 * i], p[2 * i + 1], o[2 * i], o[2 * i + 1], hf[i], &mut terms));
            }
            v
        });
        Ok(Array2::from_shape_vec((n, 9), v).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py))
    }

    /// The occupied-footprint channel (h, w) (tests and probes).
    fn occupied<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyArray2<u8>>> {
        Ok(Array2::from_shape_vec((self.h, self.w), self.occ.clone())
            .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py))
    }

    #[getter]
    fn n_pois(&self) -> usize {
        self.pois.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flags(s: &str) -> u8 {
        flags_of(&normalise(s.chars(), true))
    }

    #[test]
    fn labels_normalise_like_python() {
        assert_eq!(flags("  Retail "), F_COM | F_PED);
        assert_eq!(flags("Semidetached House"), F_RES);
        assert_eq!(flags("\u{1c}shed\u{3000}"), F_OUT);
        assert_eq!(flags("\u{212a}iosk"), 0);                   // (KELVIN SIGN lowers to k: "kiosk", no label)
        assert_eq!(flags("Shoe_Store"), F_PED);
        assert_eq!(flags("shop:bakery"), F_PED);
        assert_eq!(flags("Permanently Closed"), F_CLOSED);
        assert_eq!(flags(""), F_EMPTY);
    }

    fn tags(s: &str) -> u8 {
        let row: Vec<u32> = s.chars().map(|c| c as u32).collect();
        tag_flags(&row) & (F_COM | F_RES | F_IND | F_EMPTY)
    }

    #[test]
    fn tags_fallback_reads_json_like_python() {
        assert_eq!(tags(r#"{"landuse":" Retail "}"#), F_COM);
        assert_eq!(tags(r#"{"landuse":"", "shop":"supermarket"}"#), F_COM);
        assert_eq!(tags(r#"{"landuse":0, "building":"house"}"#), F_RES);
        assert_eq!(tags(r#"{"landuse":1, "building":"house"}"#), 0);       // (truthy, no label: the first wins)
        assert_eq!(tags(r#"{"landuse":"farm","landuse":"industrial"}"#), F_IND);     // (the last key wins)
        assert_eq!(tags(r#"[{"key":"shop","value":"retail"},{"key":"x"}]"#), F_COM);
        assert_eq!(tags(r#"{"semidetached house":"x","building":"semidetached house"}"#), 0);  // (no "_" here)
        assert_eq!(tags(r#"{"shop":"retail",}"#), F_EMPTY);               // (invalid JSON: skipped)
        assert_eq!(tags(r#"{"shop":"retail"}"#), F_COM);
        assert_eq!(tags(r#"{"office":NaN, "shop":"retail"}"#), 0);
        assert_eq!(tags(r#"{"office":0.0e5, "shop":"retail"}"#), F_COM);
        assert_eq!(tags(""), F_EMPTY);
        assert_eq!(tags(r#"{"shop":"retail"} x"#), F_EMPTY);
    }

    #[test]
    fn pairwise_matches_numpy_blocks() {
        let a: Vec<f64> = (0..300).map(|k| 1.0 / (k as f64 + 1.0)).collect();
        let s = pairwise(&a);
        let plain: f64 = a.iter().sum();
        assert!((s - plain).abs() < 1e-12);
        assert_eq!(pairwise(&[]), 0.0);
        assert_eq!(reduce_seg(&[2.0]), 2.0);
    }
}
