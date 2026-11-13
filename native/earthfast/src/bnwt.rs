//! BNWT ("BeamNG.Earth world input tile"): our own prebaked z14 input tiles (server/bnwt.py, tools/build_bnwt.py).
//!
//! A tile is one map cell (z14) and holds, per layer, exactly the Features the generator's own source path gives for
//! that cell (overture.Reader.query or pmtiles.Reader.query: ids, geometry, attribute columns, rule tables, the
//! sources' counts), LOSSLESS: the decoder hands back the same arrays bit for bit, so a cell read from a tile is the
//! cell its source gives (no plan or cell-facts format bump, no cache key of its own).
//!
//! Tile bytes:
//! ```text
//! "BNWT" u16 FORMAT u16 nsec      (no cell in it: the same content in two cells is the same bytes)
//! nsec x (u8 name length, name, u32 raw offset, u32 raw length)
//! one zstd frame: the sections' raw bytes, concatenated
//! ```
//! Section (one layer):
//! ```text
//! u32 n; ids (text column); kind, parts a feature, rings a part, coords a ring (integer streams)
//! i64 x0, i64 y0; x and y deltas (integer streams) of the coordinates on a 1e-7 degree grid (q = round(v * 1e7),
//!   value = q / 1e7, which is the parquet double itself for OpenStreetMap's 1e-7 coordinates); the values that are
//!   not (a clip's cut points on the cell edge, other sources' doubles): index deltas, a table of their other
//!   repeated values, a code each (1-4: the cell's west / south / east / north edge, given at decoding; 5..: the
//!   table; 0: its own exact f64 bits follow); their grid values repeat the one before (delta 0). A cell whose
//!   coordinates are all its edges (open sea: the ocean pieces cut to the cell) is the same bytes in every cell
//!   with the same features: the archive keeps it once (run-length directory entries)
//! u8 has clipped [+ numeric column]
//! u16 columns x (name, column); u16 rules x (name, 4 columns: feature, value, start, end)
//! u16 sources x (u16 dataset, u16 licence, u64 count)
//! ```
//! Columns: 'T' text (u32 numpy width, then 'U' all values canonical UUIDs or empty: 16 bytes each; 'S' all
//! distinct: the words alone; 'D' a dictionary in first-appearance order + codes), 'N' numeric (the numpy dtype string + the raw bytes, byte planes
//! split). Integer stream = u32 n, u8 width (1/2/4/8), zig-zag values at that width split into byte planes (the
//! planes compress far better than interleaved bytes; no varints, so a decode is a few vector loops).
//!
//! The archive is a PMTiles v3 container of these tiles (tile type unknown, tile compression none: each tile is its own
//! zstd frame), Hilbert-ordered and clustered, so every quadtree square is one contiguous byte range (region packs;
//! a deduplicated tile points back at its first copy).
//! bnwt_pmtiles_head writes everything before the tile data (header, root directory, metadata, leaf directories).
use std::collections::HashMap;
use std::io::{Read, Write};

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};

pub const FORMAT: u16 = 2;
const MAGIC: &[u8; 4] = b"BNWT";
const Q: f64 = 1e7;

// ------------------------------------------------------------------------------------------------ bytes
struct W(Vec<u8>);

impl W {
    fn u8(&mut self, v: u8) {
        self.0.push(v);
    }
    fn u16(&mut self, v: u16) {
        self.0.extend_from_slice(&v.to_le_bytes());
    }
    fn u32(&mut self, v: u32) {
        self.0.extend_from_slice(&v.to_le_bytes());
    }
    fn u64(&mut self, v: u64) {
        self.0.extend_from_slice(&v.to_le_bytes());
    }
    fn str8(&mut self, s: &str) -> Result<(), String> {
        if s.len() > 255 {
            return Err(format!("name too long: {s}"));
        }
        self.u8(s.len() as u8);
        self.0.extend_from_slice(s.as_bytes());
        Ok(())
    }
    fn str16(&mut self, s: &str) -> Result<(), String> {
        if s.len() > 65535 {
            return Err("string too long".into());
        }
        self.u16(s.len() as u16);
        self.0.extend_from_slice(s.as_bytes());
        Ok(())
    }
}

struct R<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> R<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], String> {
        if n > self.b.len() - self.i {
            return Err("truncated section".into());
        }
        let s = &self.b[self.i..self.i + n];
        self.i += n;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, String> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16, String> {
        Ok(u16::from_le_bytes(self.take(2)?.try_into().unwrap()))
    }
    fn u32(&mut self) -> Result<u32, String> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn u64(&mut self) -> Result<u64, String> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }
    fn str8(&mut self) -> Result<String, String> {
        let n = self.u8()? as usize;
        String::from_utf8(self.take(n)?.to_vec()).map_err(|e| e.to_string())
    }
    fn str16(&mut self) -> Result<String, String> {
        let n = self.u16()? as usize;
        String::from_utf8(self.take(n)?.to_vec()).map_err(|e| e.to_string())
    }
}

fn zigzag(v: i64) -> u64 {
    ((v << 1) ^ (v >> 63)) as u64
}

fn unzig(u: u64) -> i64 {
    ((u >> 1) as i64) ^ -((u & 1) as i64)
}

/// An integer stream: n, the width, the zig-zag values' byte planes.
fn put_ints(w: &mut W, a: &[i64]) {
    let mx = a.iter().map(|&v| zigzag(v)).max().unwrap_or(0);
    let width: usize = if mx < 1 << 8 { 1 } else if mx < 1 << 16 { 2 } else if mx < 1 << 32 { 4 } else { 8 };
    w.u32(a.len() as u32);
    w.u8(width as u8);
    w.0.reserve(a.len() * width);
    for p in 0..width {
        w.0.extend(a.iter().map(|&v| (zigzag(v) >> (8 * p)) as u8));
    }
}

fn get_ints(r: &mut R) -> Result<Vec<i64>, String> {
    let n = r.u32()? as usize;
    let width = r.u8()? as usize;
    if !matches!(width, 1 | 2 | 4 | 8) {
        return Err(format!("integer width {width}"));
    }
    let b = r.take(n.checked_mul(width).ok_or("integer stream too long")?)?;
    let mut z = vec![0u64; n];
    for p in 0..width {
        let plane = &b[p * n..(p + 1) * n];
        for (v, &x) in z.iter_mut().zip(plane) {
            *v |= (x as u64) << (8 * p);
        }
    }
    Ok(z.into_iter().map(unzig).collect())
}

/// Raw fixed-size items as byte planes (n, item size, planes).
fn put_raw(w: &mut W, item: usize, b: &[u8]) -> Result<(), String> {
    if item == 0 || item > 255 || b.len() % item != 0 {
        return Err(format!("raw column: {} bytes of {item}-byte items", b.len()));
    }
    let n = b.len() / item;
    w.u32(n as u32);
    w.u8(item as u8);
    w.0.reserve(b.len());
    for p in 0..item {
        w.0.extend((0..n).map(|i| b[i * item + p]));
    }
    Ok(())
}

fn get_raw(r: &mut R) -> Result<(usize, Vec<u8>), String> {
    let n = r.u32()? as usize;
    let item = r.u8()? as usize;
    let b = r.take(n.checked_mul(item).ok_or("raw column too long")?)?;
    let mut out = vec![0u8; n * item];
    for p in 0..item {
        let plane = &b[p * n..(p + 1) * n];
        for (i, &x) in plane.iter().enumerate() {
            out[i * item + p] = x;
        }
    }
    Ok((item, out))
}

// ------------------------------------------------------------------------------------------------ text
fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, &c)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        })
}

fn hexval(c: u8) -> u8 {
    if c.is_ascii_digit() {
        c - b'0'
    } else {
        c - b'a' + 10
    }
}

fn put_text(w: &mut W, vals: &[String], width: u32) {
    w.u32(width);
    let n = vals.len();
    let ne = vals.iter().filter(|s| !s.is_empty()).count();
    if ne > 0 && vals.iter().all(|s| s.is_empty() || is_uuid(s)) {
        w.u8(b'U');
        w.u32(n as u32);
        let mut bits = vec![0u8; (n + 7) / 8];
        for (i, s) in vals.iter().enumerate() {
            if !s.is_empty() {
                bits[i / 8] |= 1 << (i % 8);
            }
        }
        w.0.extend_from_slice(&bits);
        for s in vals.iter().filter(|s| !s.is_empty()) {
            let hex: Vec<u8> = s.bytes().filter(|&c| c != b'-').collect();
            for k in 0..16 {
                w.u8(hexval(hex[2 * k]) << 4 | hexval(hex[2 * k + 1]));
            }
        }
        return;
    }
    let mut seen: HashMap<&str, i64> = HashMap::with_capacity(n);
    let mut words: Vec<&str> = Vec::new();
    let mut codes: Vec<i64> = Vec::with_capacity(n);
    for s in vals {
        let c = *seen.entry(s.as_str()).or_insert_with(|| {
            words.push(s.as_str());
            words.len() as i64 - 1
        });
        codes.push(c);
    }
    let distinct = words.len() == n;
    w.u8(if distinct { b'S' } else { b'D' });
    let lens: Vec<i64> = words.iter().map(|s| s.len() as i64).collect();
    put_ints(w, &lens);
    for s in &words {
        w.0.extend_from_slice(s.as_bytes());
    }
    if !distinct {
        put_ints(w, &codes);
    }
}

/// A text column decoded straight to numpy's fixed-width unicode layout: (width in characters, at least 1 as
/// mapgeom.text_column makes it; n x width UCS-4 code points, little-endian).
struct Text {
    width: usize,
    ucs4: Vec<u8>,
}

fn ucs4(words: &[Vec<char>], codes: &[usize], width: usize) -> Result<Text, String> {
    if codes.iter().any(|&c| words[c].len() > width) {
        return Err("text longer than its column's width".into());
    }
    let mut out = vec![0u8; codes.len() * width * 4];
    for (i, &c) in codes.iter().enumerate() {
        for (k, ch) in words[c].iter().enumerate() {
            let o = (i * width + k) * 4;
            out[o..o + 4].copy_from_slice(&(*ch as u32).to_le_bytes());
        }
    }
    Ok(Text { width, ucs4: out })
}

fn get_text(r: &mut R) -> Result<Text, String> {
    let width = r.u32()? as usize;
    if width == 0 || width > 1 << 20 {
        return Err(format!("text width {width}"));
    }
    let tag = r.u8()?;
    match tag {
        b'U' => {
            let n = r.u32()? as usize;
            let bits = r.take((n + 7) / 8)?.to_vec();
            let present: Vec<bool> = (0..n).map(|i| bits[i / 8] >> (i % 8) & 1 == 1).collect();
            let k = present.iter().filter(|&&p| p).count();
            let raw = r.take(k * 16)?;
            if k > 0 && width < 36 {
                return Err("uuid column narrower than 36".into());
            }
            let mut out = vec![0u8; n * width * 4];
            let hex = b"0123456789abcdef";
            let mut j = 0;
            for (i, &p) in present.iter().enumerate() {
                if !p {
                    continue;
                }
                let u = &raw[j * 16..j * 16 + 16];
                j += 1;
                let mut pos = 0;
                for (bi, &byte) in u.iter().enumerate() {
                    if matches!(bi, 4 | 6 | 8 | 10) {
                        out[(i * width + pos) * 4] = b'-';
                        pos += 1;
                    }
                    out[(i * width + pos) * 4] = hex[(byte >> 4) as usize];
                    out[(i * width + pos + 1) * 4] = hex[(byte & 15) as usize];
                    pos += 2;
                }
            }
            Ok(Text { width, ucs4: out })
        }
        b'S' | b'D' => {
            let lens = get_ints(r)?;
            let mut words = Vec::with_capacity(lens.len());
            for &ln in &lens {
                if ln < 0 {
                    return Err("negative word length".into());
                }
                let s = std::str::from_utf8(r.take(ln as usize)?).map_err(|e| e.to_string())?;
                words.push(s.chars().collect::<Vec<char>>());
            }
            let codes: Vec<usize> = if tag == b'S' {
                (0..words.len()).collect()
            } else {
                let c = get_ints(r)?;
                if c.iter().any(|&v| v < 0 || v as usize >= words.len()) {
                    return Err("text code out of range".into());
                }
                c.into_iter().map(|v| v as usize).collect()
            };
            ucs4(&words, &codes, width)
        }
        _ => Err(format!("text column tag {tag}")),
    }
}

// ------------------------------------------------------------------------------------------------ columns
enum Col {
    Text(Vec<String>, u32), // values, numpy width (characters)
    Num(String, usize, Vec<u8>), // numpy dtype string, item size, bytes
}

enum ColOut {
    Text(Text),
    Num(String, Vec<u8>),
}

fn put_col(w: &mut W, c: &Col) -> Result<(), String> {
    match c {
        Col::Text(v, width) => {
            w.u8(b'T');
            put_text(w, v, *width);
        }
        Col::Num(dtype, item, b) => {
            w.u8(b'N');
            w.str8(dtype)?;
            put_raw(w, *item, b)?;
        }
    }
    Ok(())
}

fn get_col(r: &mut R) -> Result<ColOut, String> {
    match r.u8()? {
        b'T' => Ok(ColOut::Text(get_text(r)?)),
        b'N' => {
            let dtype = r.str8()?;
            let (_, b) = get_raw(r)?;
            Ok(ColOut::Num(dtype, b))
        }
        t => Err(format!("column tag {t}")),
    }
}

fn col_from_py(o: &Bound<'_, PyAny>) -> PyResult<Col> {
    let t = o.cast::<PyTuple>()?;
    let tag: String = t.get_item(0)?.extract()?;
    match tag.as_str() {
        "T" => Ok(Col::Text(t.get_item(1)?.extract()?, t.get_item(2)?.extract()?)),
        "N" => {
            let dtype: String = t.get_item(1)?.extract()?;
            let item: usize = t.get_item(2)?.extract()?;
            let b: Vec<u8> = t.get_item(3)?.cast::<PyBytes>()?.as_bytes().to_vec();
            Ok(Col::Num(dtype, item, b))
        }
        _ => Err(PyValueError::new_err(format!("column kind {tag}"))),
    }
}

fn col_to_py<'py>(py: Python<'py>, c: ColOut) -> PyResult<Bound<'py, PyTuple>> {
    match c {
        ColOut::Text(t) => PyTuple::new(py, [
            "T".into_pyobject(py)?.into_any(),
            t.width.into_pyobject(py)?.into_any(),
            PyBytes::new(py, &t.ucs4).into_any(),
        ]),
        ColOut::Num(d, b) => PyTuple::new(py, [
            "N".into_pyobject(py)?.into_any(),
            d.into_pyobject(py)?.into_any(),
            PyBytes::new(py, &b).into_any(),
        ]),
    }
}

// ------------------------------------------------------------------------------------------------ sections
struct Section {
    name: String,
    ids: Col,
    kind: Vec<i64>,
    part_off: Vec<i64>,
    ring_off: Vec<i64>,
    coord_off: Vec<i64>,
    lonlat: Vec<f64>, // x0 y0 x1 y1 ...
    clipped: Option<Col>,
    attrs: Vec<(String, Col)>,
    rules: Vec<(String, [Col; 4])>,
    sources: Vec<(String, String, u64)>,
}

struct SectionOut {
    ids: Text,
    kind: Vec<i64>,
    part_off: Vec<i64>,
    ring_off: Vec<i64>,
    coord_off: Vec<i64>,
    lonlat: Vec<f64>,
    clipped: Option<ColOut>,
    attrs: Vec<(String, ColOut)>,
    rules: Vec<(String, Vec<ColOut>)>,
    sources: Vec<(String, String, u64)>,
}

fn counts(off: &[i64], what: &str) -> Result<Vec<i64>, String> {
    if off.is_empty() || off[0] != 0 {
        return Err(format!("{what}: offsets must start at 0"));
    }
    let c: Vec<i64> = off.windows(2).map(|w| w[1] - w[0]).collect();
    if c.iter().any(|&v| v < 0) {
        return Err(format!("{what}: offsets must not decrease"));
    }
    Ok(c)
}

fn offsets(c: &[i64]) -> Vec<i64> {
    let mut out = Vec::with_capacity(c.len() + 1);
    let mut acc = 0i64;
    out.push(0);
    for &v in c {
        acc += v;
        out.push(acc);
    }
    out
}

fn encode_section(s: &Section, bounds: &[f64; 4]) -> Result<(Vec<u8>, bool), String> {
    let Col::Text(ids, _) = &s.ids else { return Err("ids must be text".into()) };
    let n = ids.len();
    if s.kind.len() != n || s.part_off.len() != n + 1 {
        return Err(format!("{}: {} ids, {} kinds, {} part offsets", s.name, n, s.kind.len(), s.part_off.len()));
    }
    let parts = counts(&s.part_off, "part_off")?;
    let rings = counts(&s.ring_off, "ring_off")?;
    let coords = counts(&s.coord_off, "coord_off")?;
    let np = *s.part_off.last().unwrap() as usize;
    let nr = *s.ring_off.last().unwrap() as usize;
    let nc = *s.coord_off.last().unwrap() as usize;
    if rings.len() != np || coords.len() != nr || s.lonlat.len() != 2 * nc {
        return Err(format!("{}: geometry offsets do not match ({} parts, {} rings, {} coords)", s.name, np, nr, nc));
    }
    let mut w = W(Vec::new());
    w.u32(n as u32);
    put_col(&mut w, &s.ids)?;
    put_ints(&mut w, &s.kind);
    put_ints(&mut w, &parts);
    put_ints(&mut w, &rings);
    put_ints(&mut w, &coords);
    // coordinates on the 1e-7 grid; the exceptions exactly
    let mut q = vec![0i64; 2 * nc];
    let mut exc = vec![false; 2 * nc];
    let mut exc_idx: Vec<i64> = Vec::new();
    let mut exc_bits: Vec<u64> = Vec::new();
    let mut last = 0i64;
    for (i, &v) in s.lonlat.iter().enumerate() {
        let r = (v * Q).round();
        let qi = if r.is_finite() && r.abs() < 4.0e18 { r as i64 } else { 0 };
        q[i] = qi;
        if ((qi as f64) / Q).to_bits() != v.to_bits() {
            exc[i] = true;
            exc_idx.push(i as i64 - last);
            last = i as i64;
            exc_bits.push(v.to_bits());
        }
    }
    // an exception's grid value is the one before it (delta 0): a cell whose coordinates are all its own edges (an
    // open-sea cell's squares) has the same bytes wherever it is, and the archive keeps those bytes once
    let first = |c: usize| (0..nc).map(|k| 2 * k + c).find(|&i| !exc[i]).map(|i| q[i]).unwrap_or(0);
    let (x0, y0) = (first(0), first(1));
    let mut prev = [x0, y0];
    for i in 0..2 * nc {
        if exc[i] {
            q[i] = prev[i % 2];
        } else {
            prev[i % 2] = q[i];
        }
    }
    // each exception a code: 1-4 the cell's edges (west, south, east, north: where a clip cuts), 5.. a table of the
    // other repeated values (first appearance order), 0 its own value follows
    let edge: Vec<u64> = bounds.iter().map(|v| v.to_bits()).collect();
    let mut count: HashMap<u64, u32> = HashMap::new();
    for &b in &exc_bits {
        *count.entry(b).or_insert(0) += 1;
    }
    let mut table: Vec<u64> = Vec::new();
    let mut slot: HashMap<u64, i64> = HashMap::new();
    for (k, &b) in edge.iter().enumerate() {
        slot.entry(b).or_insert(k as i64 + 1);
    }
    for &b in &exc_bits {
        if count[&b] > 1 && !slot.contains_key(&b) && table.len() < 65535 {
            table.push(b);
            slot.insert(b, table.len() as i64 + 4);
        }
    }
    let codes: Vec<i64> = exc_bits.iter().map(|b| *slot.get(b).unwrap_or(&0)).collect();
    let independent = !exc.contains(&false) && codes.iter().all(|&c| (1..=4).contains(&c));
    let tbytes: Vec<u8> = table.iter().flat_map(|b| b.to_le_bytes()).collect();
    let own: Vec<u8> = exc_bits.iter().zip(&codes).filter(|(_, &c)| c == 0).flat_map(|(b, _)| b.to_le_bytes())
        .collect();
    w.u64(x0 as u64);
    w.u64(y0 as u64);
    for c in 0..2 {
        let mut prev = if c == 0 { x0 } else { y0 };
        let d: Vec<i64> = (0..nc).map(|k| {
            let v = q[2 * k + c];
            let dv = v.wrapping_sub(prev);
            prev = v;
            dv
        }).collect();
        put_ints(&mut w, &d);
    }
    put_ints(&mut w, &exc_idx);
    put_raw(&mut w, 8, &tbytes)?;
    put_ints(&mut w, &codes);
    put_raw(&mut w, 8, &own)?;
    match &s.clipped {
        Some(c) => {
            w.u8(1);
            put_col(&mut w, c)?;
        }
        None => w.u8(0),
    }
    w.u16(s.attrs.len() as u16);
    for (name, c) in &s.attrs {
        w.str8(name)?;
        put_col(&mut w, c)?;
    }
    w.u16(s.rules.len() as u16);
    for (name, cols) in &s.rules {
        w.str8(name)?;
        for c in cols {
            put_col(&mut w, c)?;
        }
    }
    w.u16(s.sources.len() as u16);
    for (ds, lic, k) in &s.sources {
        w.str16(ds)?;
        w.str16(lic)?;
        w.u64(*k);
    }
    Ok((w.0, independent))
}

fn decode_section(b: &[u8], bounds: &[f64; 4]) -> Result<SectionOut, String> {
    let mut r = R { b, i: 0 };
    let n = r.u32()? as usize;
    let ids = match get_col(&mut r)? {
        ColOut::Text(t) => t,
        _ => return Err("ids must be text".into()),
    };
    let kind = get_ints(&mut r)?;
    let parts = get_ints(&mut r)?;
    let rings = get_ints(&mut r)?;
    let coords = get_ints(&mut r)?;
    let part_off = offsets(&parts);
    let ring_off = offsets(&rings);
    let coord_off = offsets(&coords);
    if ids.ucs4.len() != n * ids.width * 4 || kind.len() != n || parts.len() != n
        || rings.len() as i64 != *part_off.last().unwrap() || coords.len() as i64 != *ring_off.last().unwrap()
        || parts.iter().chain(&rings).chain(&coords).any(|&v| v < 0) {
        return Err("geometry counts do not match".into());
    }
    let nc = *coord_off.last().unwrap() as usize;
    let x0 = r.u64()? as i64;
    let y0 = r.u64()? as i64;
    let dx = get_ints(&mut r)?;
    let dy = get_ints(&mut r)?;
    if dx.len() != nc || dy.len() != nc {
        return Err("coordinate streams do not match".into());
    }
    let mut lonlat = vec![0f64; 2 * nc];
    let (mut x, mut y) = (x0, y0);
    for k in 0..nc {
        x = x.wrapping_add(dx[k]);
        y = y.wrapping_add(dy[k]);
        lonlat[2 * k] = (x as f64) / Q;
        lonlat[2 * k + 1] = (y as f64) / Q;
    }
    let exc_idx = get_ints(&mut r)?;
    let (_, table) = get_raw(&mut r)?;
    let codes = get_ints(&mut r)?;
    let (_, own) = get_raw(&mut r)?;
    let ntab = table.len() / 8;
    if codes.len() != exc_idx.len() || codes.iter().any(|&c| c < 0 || c as usize > ntab + 4)
        || own.len() != 8 * codes.iter().filter(|&&c| c == 0).count() {
        return Err("coordinate exceptions do not match".into());
    }
    let f = |b: &[u8], j: usize| f64::from_le_bytes(b[8 * j..8 * j + 8].try_into().unwrap());
    let (mut at, mut k) = (0i64, 0usize);
    for (j, &d) in exc_idx.iter().enumerate() {
        at += d;
        if at < 0 || at as usize >= lonlat.len() {
            return Err("coordinate exception out of range".into());
        }
        lonlat[at as usize] = match codes[j] {
            0 => {
                k += 1;
                f(&own, k - 1)
            }
            c @ 1..=4 => bounds[c as usize - 1],
            c => f(&table, c as usize - 5),
        };
    }
    let clipped = if r.u8()? == 1 { Some(get_col(&mut r)?) } else { None };
    let na = r.u16()? as usize;
    let mut attrs = Vec::with_capacity(na);
    for _ in 0..na {
        let name = r.str8()?;
        attrs.push((name, get_col(&mut r)?));
    }
    let nr = r.u16()? as usize;
    let mut rules = Vec::with_capacity(nr);
    for _ in 0..nr {
        let name = r.str8()?;
        let mut cols = Vec::with_capacity(4);
        for _ in 0..4 {
            cols.push(get_col(&mut r)?);
        }
        rules.push((name, cols));
    }
    let ns = r.u16()? as usize;
    let mut sources = Vec::with_capacity(ns);
    for _ in 0..ns {
        let ds = r.str16()?;
        let lic = r.str16()?;
        sources.push((ds, lic, r.u64()?));
    }
    if r.i != b.len() {
        return Err(format!("{} bytes left after the section", b.len() - r.i));
    }
    Ok(SectionOut { ids, kind, part_off, ring_off, coord_off, lonlat, clipped, attrs, rules, sources })
}

fn section_from_py(d: &Bound<'_, PyDict>) -> PyResult<Section> {
    let get = |k: &str| -> PyResult<Bound<'_, PyAny>> {
        d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("section without {k}")))
    };
    let i64s = |k: &str| -> PyResult<Vec<i64>> {
        let a: PyReadonlyArray1<i64> = get(k)?.extract()?;
        Ok(a.as_array().to_vec())
    };
    let ll: PyReadonlyArray2<f64> = get("lonlat")?.extract()?;
    let lla = ll.as_array();
    if lla.ncols() != 2 && lla.nrows() > 0 {
        return Err(PyValueError::new_err("lonlat must be (n, 2)"));
    }
    let lonlat: Vec<f64> = lla.iter().copied().collect();
    let clipped = match d.get_item("clipped")? {
        Some(c) if !c.is_none() => Some(col_from_py(&c)?),
        _ => None,
    };
    let mut attrs = Vec::new();
    for item in get("attrs")?.cast::<PyList>()?.iter() {
        let t = item.cast::<PyTuple>()?;
        attrs.push((t.get_item(0)?.extract::<String>()?, col_from_py(&t.get_item(1)?)?));
    }
    let mut rules = Vec::new();
    for item in get("rules")?.cast::<PyList>()?.iter() {
        let t = item.cast::<PyTuple>()?;
        let cols = t.get_item(1)?;
        let cl = cols.cast::<PyList>()?;
        if cl.len() != 4 {
            return Err(PyValueError::new_err("a rule table has 4 columns"));
        }
        rules.push((t.get_item(0)?.extract::<String>()?,
                    [col_from_py(&cl.get_item(0)?)?, col_from_py(&cl.get_item(1)?)?,
                     col_from_py(&cl.get_item(2)?)?, col_from_py(&cl.get_item(3)?)?]));
    }
    Ok(Section {
        name: get("name")?.extract()?,
        ids: col_from_py(&get("ids")?)?,
        kind: i64s("kind")?,
        part_off: i64s("part_off")?,
        ring_off: i64s("ring_off")?,
        coord_off: i64s("coord_off")?,
        lonlat,
        clipped,
        attrs,
        rules,
        sources: get("sources")?.extract()?,
    })
}

// ------------------------------------------------------------------------------------------------ tiles
struct Head {
    format: u16,
    secs: Vec<(String, u32, u32)>,
    body: usize, // where the zstd frame starts
}

fn parse_head(t: &[u8]) -> Result<Head, String> {
    if t.len() < 8 || &t[0..4] != MAGIC {
        return Err("not a BNWT tile".into());
    }
    let mut r = R { b: t, i: 4 };
    let format = r.u16()?;
    if format != FORMAT {
        return Err(format!("BNWT format {} ({} expected)", format, FORMAT));
    }
    let ns = r.u16()? as usize;
    let mut secs = Vec::with_capacity(ns);
    for _ in 0..ns {
        let name = r.str8()?;
        let off = r.u32()?;
        let len = r.u32()?;
        secs.push((name, off, len));
    }
    Ok(Head { format, secs, body: r.i })
}

fn encode_tile(secs: &[Section], bounds: &[f64; 4], level: i32) -> Result<(Vec<u8>, bool), String> {
    let mut raw = Vec::new();
    let mut dir = Vec::new();
    let mut independent = true;
    for s in secs {
        let (b, ind) = encode_section(s, bounds)?;
        independent &= ind;
        dir.push((s.name.clone(), raw.len() as u32, b.len() as u32));
        raw.extend_from_slice(&b);
    }
    let mut w = W(Vec::new());
    w.0.extend_from_slice(MAGIC);
    w.u16(FORMAT);
    w.u16(dir.len() as u16);
    for (name, off, len) in &dir {
        w.str8(name)?;
        w.u32(*off);
        w.u32(*len);
    }
    let body = zstd::bulk::compress(&raw, level).map_err(|e| format!("zstd: {e}"))?;
    w.0.extend_from_slice(&body);
    Ok((w.0, independent))
}

/// The raw bytes of one section (the frame decoded only as far as that section's end).
fn section_bytes(t: &[u8], h: &Head, name: &str) -> Result<Option<Vec<u8>>, String> {
    let Some((_, off, len)) = h.secs.iter().find(|s| s.0 == name) else {
        return Ok(None);
    };
    let (off, len) = (*off as usize, *len as usize);
    let mut dec = zstd::stream::read::Decoder::new(&t[h.body..]).map_err(|e| format!("zstd: {e}"))?;
    let mut skip = vec![0u8; off.min(1 << 20)];
    let mut left = off;
    while left > 0 {
        let k = left.min(skip.len());
        dec.read_exact(&mut skip[..k]).map_err(|e| format!("zstd: {e}"))?;
        left -= k;
    }
    let mut out = vec![0u8; len];
    dec.read_exact(&mut out).map_err(|e| format!("zstd: {e}"))?;
    Ok(Some(out))
}

/// (bytes, independent) of one cell's tile from its sections: [{"name", "ids": text column, "kind": int64,
/// "part_off" / "ring_off" / "coord_off": int64, "lonlat": (n, 2) float64, "clipped": column or None, "attrs": [(name,
/// column)], "rules": [(name, [4 columns])], "sources": [(dataset, licence, count)]}]; a column is ("T", [str], numpy
/// width) or ("N", dtype str, item size, bytes). bounds: the cell's (west, south, east, north), the values a clip cuts
/// at. independent: every coordinate is one of them (the same bytes in every cell with these features: an open-sea
/// cell). level: zstd's.
#[pyfunction]
#[pyo3(signature = (sections, bounds, level=19))]
pub fn bnwt_encode_tile<'py>(py: Python<'py>, sections: Vec<Bound<'py, PyDict>>, bounds: [f64; 4], level: i32)
                             -> PyResult<(Bound<'py, PyBytes>, bool)> {
    let secs: Vec<Section> = sections.iter().map(section_from_py).collect::<PyResult<_>>()?;
    let (out, ind) = py.detach(move || encode_tile(&secs, &bounds, level)).map_err(PyValueError::new_err)?;
    Ok((PyBytes::new(py, &out), ind))
}

/// (format, [(layer, raw length)]) of a tile.
#[pyfunction]
pub fn bnwt_tile_info(tile: &[u8]) -> PyResult<(u16, Vec<(String, u32)>)> {
    let h = parse_head(tile).map_err(PyValueError::new_err)?;
    Ok((h.format, h.secs.iter().map(|s| (s.0.clone(), s.2)).collect()))
}

/// One layer of a cell's tile (bounds: the cell's, as at encoding), None when the tile has no such section: {"ids":
/// ("T", width, ucs4 bytes), "kind", "part_off", "ring_off", "coord_off": int64 arrays, "lonlat": (n, 2) float64,
/// "clipped": column or None, "attrs", "rules", "sources"}; a column out is ("T", width, ucs4 bytes) or ("N", dtype
/// str, bytes).
#[pyfunction]
pub fn bnwt_decode_layer<'py>(py: Python<'py>, tile: Vec<u8>, layer: String, bounds: [f64; 4])
                              -> PyResult<Option<Bound<'py, PyDict>>> {
    let got = py.detach(move || -> Result<Option<SectionOut>, String> {
        let h = parse_head(&tile)?;
        match section_bytes(&tile, &h, &layer)? {
            None => Ok(None),
            Some(b) => Ok(Some(decode_section(&b, &bounds)?)),
        }
    }).map_err(PyValueError::new_err)?;
    let Some(s) = got else { return Ok(None) };
    let d = PyDict::new(py);
    d.set_item("ids", col_to_py(py, ColOut::Text(s.ids))?)?;
    d.set_item("kind", s.kind.into_pyarray(py))?;
    d.set_item("part_off", s.part_off.into_pyarray(py))?;
    d.set_item("ring_off", s.ring_off.into_pyarray(py))?;
    d.set_item("coord_off", s.coord_off.into_pyarray(py))?;
    let nc = s.lonlat.len() / 2;
    let ll = Array2::from_shape_vec((nc, 2), s.lonlat).map_err(|e| PyValueError::new_err(e.to_string()))?;
    d.set_item("lonlat", ll.into_pyarray(py))?;
    match s.clipped {
        Some(c) => d.set_item("clipped", col_to_py(py, c)?)?,
        None => d.set_item("clipped", py.None())?,
    }
    let attrs = PyList::empty(py);
    for (name, c) in s.attrs {
        attrs.append((name, col_to_py(py, c)?))?;
    }
    d.set_item("attrs", attrs)?;
    let rules = PyList::empty(py);
    for (name, cols) in s.rules {
        let l = PyList::empty(py);
        for c in cols {
            l.append(col_to_py(py, c)?)?;
        }
        rules.append((name, l))?;
    }
    d.set_item("rules", rules)?;
    d.set_item("sources", s.sources)?;
    Ok(Some(d))
}

// ------------------------------------------------------------------------------------------------ archive
/// [start, end) of the tile ids at zoom z under the square (zr, x, y), zr <= z: one contiguous run of the Hilbert
/// order (a region pack is one byte range of a clustered archive).
#[pyfunction]
pub fn bnwt_square_range(zr: u8, x: u64, y: u64, z: u8) -> PyResult<(u64, u64)> {
    if zr > z || z > 26 {
        return Err(PyValueError::new_err("zoom out of range"));
    }
    let acc = |zz: u8| -> u64 { (0..zz).map(|t| 1u64 << (2 * t as u64)).sum() };
    let d = crate::pmtiles::tile_id(zr, x, y) - acc(zr);
    let k = 1u64 << (2 * (z - zr) as u64);
    Ok((acc(z) + d * k, acc(z) + (d + 1) * k))
}

fn varint_put(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

/// A PMTiles directory's bytes (gzip; deterministic: no name, mtime 0).
fn dir_bytes(e: &[(u64, u64, u64, u64)]) -> Result<Vec<u8>, String> {
    // (tile id, offset, length, run length)
    let mut raw = Vec::with_capacity(e.len() * 8);
    varint_put(&mut raw, e.len() as u64);
    let mut last = 0u64;
    for x in e {
        varint_put(&mut raw, x.0 - last);
        last = x.0;
    }
    for x in e {
        varint_put(&mut raw, x.3);
    }
    for x in e {
        varint_put(&mut raw, x.2);
    }
    for (k, x) in e.iter().enumerate() {
        if k > 0 && x.1 == e[k - 1].1 + e[k - 1].2 {
            varint_put(&mut raw, 0);
        } else {
            varint_put(&mut raw, x.1 + 1);
        }
    }
    let mut g = flate2::GzBuilder::new().mtime(0).write(Vec::new(), flate2::Compression::new(9));
    g.write_all(&raw).map_err(|e| e.to_string())?;
    g.finish().map_err(|e| e.to_string())
}

const ROOT_MAX: usize = 16384 - 127;

/// Everything of a clustered PMTiles v3 archive before its tile data: header, root directory, metadata (gzip JSON),
/// leaf directories. tile_ids ascending; offsets / lengths: each tile's bytes in the data section (tiles with the same
/// content share theirs: consecutive ids with the same bytes become one run-length entry); data_len: the data
/// section's size. bounds: (min lon, min lat, max lon, max lat) in 1e-7 degrees; center: (zoom, lon e7, lat e7).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bnwt_pmtiles_head<'py>(py: Python<'py>, tile_ids: PyReadonlyArray1<u64>, offsets: PyReadonlyArray1<u64>,
                              lengths: PyReadonlyArray1<u64>, data_len: u64, metadata: &[u8], min_zoom: u8,
                              max_zoom: u8, bounds: (i32, i32, i32, i32), center: (u8, i32, i32))
                              -> PyResult<Bound<'py, PyBytes>> {
    let ids = tile_ids.as_array().to_vec();
    let offs = offsets.as_array().to_vec();
    let lens = lengths.as_array().to_vec();
    let meta = metadata.to_vec();
    let out = py.detach(move || -> Result<Vec<u8>, String> {
        if ids.len() != lens.len() || ids.len() != offs.len() {
            return Err("tile ids, offsets and lengths differ".into());
        }
        if ids.windows(2).any(|w| w[1] <= w[0]) {
            return Err("tile ids must ascend".into());
        }
        let mut entries: Vec<(u64, u64, u64, u64)> = Vec::with_capacity(ids.len());
        let mut contents = std::collections::HashSet::new();
        for k in 0..ids.len() {
            if offs[k] + lens[k] > data_len {
                return Err("a tile past the data section".into());
            }
            contents.insert(offs[k]);
            if let Some(e) = entries.last_mut() {
                if e.0 + e.3 == ids[k] && e.1 == offs[k] && e.2 == lens[k] {
                    e.3 += 1;
                    continue;
                }
            }
            entries.push((ids[k], offs[k], lens[k], 1u64));
        }
        let n_entries = entries.len() as u64;
        let n_contents = contents.len() as u64;
        // root alone, else leaves of `leaf` entries (doubled until the root fits)
        let mut root = dir_bytes(&entries)?;
        let mut leaves: Vec<u8> = Vec::new();
        if root.len() > ROOT_MAX {
            let mut leaf = 4096usize;
            loop {
                leaves.clear();
                let mut ptrs = Vec::new();
                for chunk in entries.chunks(leaf) {
                    let b = dir_bytes(chunk)?;
                    ptrs.push((chunk[0].0, leaves.len() as u64, b.len() as u64, 0u64));
                    leaves.extend_from_slice(&b);
                }
                root = dir_bytes(&ptrs)?;
                if root.len() <= ROOT_MAX {
                    break;
                }
                leaf *= 2;
            }
        }
        let mut g = flate2::GzBuilder::new().mtime(0).write(Vec::new(), flate2::Compression::new(9));
        g.write_all(&meta).map_err(|e| e.to_string())?;
        let meta_gz = g.finish().map_err(|e| e.to_string())?;
        let root_off = 127u64;
        let meta_off = root_off + root.len() as u64;
        let leaf_off = meta_off + meta_gz.len() as u64;
        let data_off = leaf_off + leaves.len() as u64;
        let mut h = W(Vec::with_capacity(data_off as usize));
        h.0.extend_from_slice(b"PMTiles");
        h.u8(3);
        for v in [root_off, root.len() as u64, meta_off, meta_gz.len() as u64, leaf_off, leaves.len() as u64,
                  data_off, data_len, ids.len() as u64, n_entries, n_contents] {
            h.u64(v);
        }
        h.u8(1); // clustered
        h.u8(2); // internal compression: gzip
        h.u8(1); // tile compression: none (each tile is its own zstd frame)
        h.u8(0); // tile type: unknown
        h.u8(min_zoom);
        h.u8(max_zoom);
        for v in [bounds.0, bounds.1, bounds.2, bounds.3] {
            h.0.extend_from_slice(&v.to_le_bytes());
        }
        h.u8(center.0);
        h.0.extend_from_slice(&center.1.to_le_bytes());
        h.0.extend_from_slice(&center.2.to_le_bytes());
        debug_assert_eq!(h.0.len(), 127);
        h.0.extend_from_slice(&root);
        h.0.extend_from_slice(&meta_gz);
        h.0.extend_from_slice(&leaves);
        Ok(h.0)
    }).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &out))
}

/// The tiles (tile id, offset, length) of a directory (gzip or plain) with ids in [start, end) (a run-length entry
/// gives each of its ids), and the leaf directories (first tile id, offset, length) that may hold some: a region's
/// byte ranges from the directories.
#[pyfunction]
pub fn bnwt_dir_range(py: Python<'_>, dir: &[u8], start: u64, end: u64)
                      -> PyResult<(Vec<(u64, u64, u64)>, Vec<(u64, u64, u64)>)> {
    let raw = dir.to_vec();
    py.detach(move || -> Result<_, String> {
        let e = crate::pmtiles::parse_dir(&crate::pmtiles::gunzip(&raw)?)?;
        let mut tiles = Vec::new();
        let mut leaves = Vec::new();
        for (k, x) in e.iter().enumerate() {
            if x.run == 0 {
                // a leaf covers [its id, the next entry's id)
                let next = e.get(k + 1).map(|n| n.tile_id).unwrap_or(u64::MAX);
                if x.tile_id < end && next > start {
                    leaves.push((x.tile_id, x.offset, x.length));
                }
            } else if x.tile_id < end && x.tile_id + x.run > start {
                for t in x.tile_id.max(start)..(x.tile_id + x.run).min(end) {
                    tiles.push((t, x.offset, x.length));
                }
            }
        }
        Ok((tiles, leaves))
    }).map_err(PyValueError::new_err)
}

/// The rows each cell of a block of cells takes (the parquet path's bbox test, xmin <= east, xmax >= west, ymin <= north,
/// ymax >= south, in the bbox columns' own type as DuckDB compares them: f32 casts both sides to float32, else double):
/// x0 / x1 / y0 / y1 the rows' boxes; lon_edges the block's nx + 1 column edges west to east, lat_edges its ny + 1 row
/// edges north to south (the cells' bounds() values). Returns (offsets, rows): cell (ix, iy) is index ix * ny + iy, its
/// rows rows[offsets[i]:offsets[i + 1]], ascending.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bnwt_assign<'py>(py: Python<'py>, x0: PyReadonlyArray1<f64>, x1: PyReadonlyArray1<f64>,
                        y0: PyReadonlyArray1<f64>, y1: PyReadonlyArray1<f64>, lon_edges: Vec<f64>, lat_edges: Vec<f64>,
                        f32: bool) -> PyResult<(Bound<'py, numpy::PyArray1<i64>>, Bound<'py, numpy::PyArray1<i64>>)> {
    let (a, b, c, d) = (x0.as_array().to_vec(), x1.as_array().to_vec(), y0.as_array().to_vec(), y1.as_array().to_vec());
    if a.len() != b.len() || a.len() != c.len() || a.len() != d.len() || lon_edges.len() < 2 || lat_edges.len() < 2 {
        return Err(PyValueError::new_err("bnwt_assign: mismatched inputs"));
    }
    let (offs, rows) = py.detach(move || {
        let q = |v: f64| if f32 { v as f32 as f64 } else { v };
        let (a, b, c, d): (Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>) =
            (a.iter().map(|&v| q(v)).collect(), b.iter().map(|&v| q(v)).collect(),
             c.iter().map(|&v| q(v)).collect(), d.iter().map(|&v| q(v)).collect());
        let lon: Vec<f64> = lon_edges.iter().map(|&v| q(v)).collect();
        let lat: Vec<f64> = lat_edges.iter().map(|&v| q(v)).collect();
        let (nx, ny) = (lon.len() - 1, lat.len() - 1);
        let mut per: Vec<Vec<i64>> = vec![Vec::new(); nx * ny];
        for r in 0..a.len() {
            for ix in 0..nx {
                if !(a[r] <= lon[ix + 1] && b[r] >= lon[ix]) {
                    continue;
                }
                for iy in 0..ny {
                    if c[r] <= lat[iy] && d[r] >= lat[iy + 1] {
                        per[ix * ny + iy].push(r as i64);
                    }
                }
            }
        }
        let mut offs = Vec::with_capacity(per.len() + 1);
        let mut rows = Vec::new();
        offs.push(0i64);
        for p in per {
            rows.extend(p);
            offs.push(rows.len() as i64);
        }
        (offs, rows)
    });
    Ok((offs.into_pyarray(py), rows.into_pyarray(py)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bnwt_assign, m)?)?;
    m.add_function(wrap_pyfunction!(bnwt_encode_tile, m)?)?;
    m.add_function(wrap_pyfunction!(bnwt_tile_info, m)?)?;
    m.add_function(wrap_pyfunction!(bnwt_decode_layer, m)?)?;
    m.add_function(wrap_pyfunction!(bnwt_square_range, m)?)?;
    m.add_function(wrap_pyfunction!(bnwt_pmtiles_head, m)?)?;
    m.add_function(wrap_pyfunction!(bnwt_dir_range, m)?)?;
    m.add("BNWT_FORMAT", FORMAT)?;
    Ok(())
}
