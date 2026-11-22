//! CIP ("compact input product", architecture v2 phase V2): one map cell's (z14) world-generation inputs in one file,
//! prebuilt per input release (tools/build_cip.py) and read in place of the sources (server/cip.py, BNE_V2_CIP=on).
//!
//! A cell file holds named sections, each the exact bytes or arrays a source path of the server gives for that cell
//! (a map layer's cell file arrays as mapdata.MapData._build makes them, a lidar answer file, a tag shard's rows...),
//! LOSSLESS: every array comes back with its dtype, shape and bytes, so what is built from it is built from the
//! source's own data, bit for bit.
//!
//! Cell bytes (no cell key in them: two cells with the same content are the same bytes, an archive keeps them once):
//! ```text
//! "BCIP" u16 FORMAT u16 nsec
//! nsec x (u8 name length, name, u8 codec, u32 raw offset, u32 raw length)
//! one zstd frame: the sections' raw bytes, concatenated (sections a reader wants first come first: a partial
//!   decode stops at the end of the section it asked for)
//! ```
//! Codecs: 'B' a blob (the bytes as they are); 'A' a dictionary of numpy arrays in their order:
//! ```text
//! u16 n; n x (name, dtype string (numpy .str), u8 ndim, ndim x u64 dims, u8 encoding, payload)
//! ```
//! Encodings (chosen by the encoder from the array alone, deterministic):
//! - 'T' a unicode column ('<U<w>'): the BNWT text column (UUIDs as 16 bytes, all-distinct words, or a dictionary in
//!   first-appearance order + codes); values keep inner NULs, numpy's trailing padding is restored from the width;
//! - 'C' coordinate pairs ('<f8' (n, 2), names ending "lonlat") and 'G' other '<f8' values when most sit on the 1e-7
//!   grid: the BNWT coordinate coding (x and y delta streams on the 1e-7 degree grid; the values that are not on it
//!   coded as the cell's edges (given at decoding), a table of repeated values, or their own f64 bits);
//! - 'F' '<f8' values that are all exact float32s: float32 byte planes;
//! - 'I' integers (any signed / unsigned width; bool) as a zig-zag integer stream; 'J' a non-decreasing 1-D integer
//!   array as its first differences (offsets);
//! - 'D' attr.cx / attr.cy that are their features' bbox centres ((min + max) / 2 of the section's own geometry, made
//!   again at decoding), the few that are not as exceptions;
//! - 'V' a UUID column with repeats: its distinct UUIDs (16 bytes) and a code each; 'X' a rule's value column
//!   "rule.<layer>.value" as references into an earlier section "m:<layer>"'s ids (a road's connectors: the cell's
//!   connectors section, which comes before the roads), the UUIDs not there after them;
//! - 'R' anything else: raw byte planes of the items.
//!
//! Integer stream and byte planes are BNWT's (bnwt.rs): u32 n, u8 width, the planes (zstd finds the structure).
use std::collections::HashMap;
use std::io::Read;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyTuple};

use crate::bnwt::{get_ints, get_raw, get_text, put_ints, put_raw, put_text, R, W};

pub const FORMAT: u16 = 1;
const MAGIC: &[u8; 4] = b"BCIP";
const Q: f64 = 1e7;
const ALIGN: usize = 16;

/// One array of an 'A' section: name, numpy dtype string, shape, its bytes (C order).
pub struct Arr {
    pub name: String,
    pub dtype: String,
    pub shape: Vec<u64>,
    pub data: Vec<u8>,
}

pub enum Sec {
    Arrays(Vec<Arr>),
    Blob(Vec<u8>),
}

// ------------------------------------------------------------------------------------------------ dtypes
/// (kind, item size) of a numpy dtype string: kind 'i' signed, 'u' unsigned, 'b' bool, 'f' float, 'U' unicode (item
/// size in bytes, 4 a character), other kinds as they are. None when the string is not one this codec knows the
/// size of (encoded raw only if the caller says the item size: never; such arrays are refused).
fn dtype_info(dt: &str) -> Option<(char, usize, bool)> {
    let b = dt.as_bytes();
    if b.len() < 3 {
        return None;
    }
    let order = b[0] as char; // '<' little, '>' big, '|' not applicable, '=' native
    let kind = b[1] as char;
    let digits: String = dt[2..].chars().take_while(|c| c.is_ascii_digit()).collect();
    let n: usize = digits.parse().ok()?;
    let little = order == '<' || order == '|' || (order == '=' && cfg!(target_endian = "little"));
    match kind {
        'U' => Some(('U', n.checked_mul(4)?, little)),
        'i' | 'u' | 'f' | 'b' | 'S' | 'V' | 'c' | 'm' | 'M' => Some((kind, n, little)),
        _ => None,
    }
}

fn count(shape: &[u64]) -> Option<usize> {
    let mut n: usize = 1;
    for &d in shape {
        n = n.checked_mul(usize::try_from(d).ok()?)?;
    }
    Some(n)
}

// ------------------------------------------------------------------------------------------------ grid coding
/// Values on the 1e-7 grid as delta streams (stride 2: x and y streams of interleaved pairs; 1: one stream), the
/// others as exceptions: index deltas, a table of repeated values, a code each (1-4 the cell's edges, 5.. the table,
/// 0 its own bits follow); an exception's grid value repeats the one before it in its stream (delta 0).
fn put_grid(w: &mut W, v: &[f64], stride: usize, bounds: &[f64; 4]) -> Result<(), String> {
    let n = v.len();
    let mut q = vec![0i64; n];
    let mut exc = vec![false; n];
    let mut exc_idx: Vec<i64> = Vec::new();
    let mut exc_bits: Vec<u64> = Vec::new();
    let mut last = 0i64;
    for (i, &x) in v.iter().enumerate() {
        let r = (x * Q).round();
        let qi = if r.is_finite() && r.abs() < 4.0e18 { r as i64 } else { 0 };
        q[i] = qi;
        if ((qi as f64) / Q).to_bits() != x.to_bits() {
            exc[i] = true;
            exc_idx.push(i as i64 - last);
            last = i as i64;
            exc_bits.push(x.to_bits());
        }
    }
    let mut starts = vec![0i64; stride];
    for (c, s) in starts.iter_mut().enumerate() {
        *s = (c..n).step_by(stride).find(|&i| !exc[i]).map(|i| q[i]).unwrap_or(0);
    }
    let mut prev = starts.clone();
    for i in 0..n {
        if exc[i] {
            q[i] = prev[i % stride];
        } else {
            prev[i % stride] = q[i];
        }
    }
    let edge: Vec<u64> = bounds.iter().map(|v| v.to_bits()).collect();
    let mut seen: HashMap<u64, u32> = HashMap::new();
    for &b in &exc_bits {
        *seen.entry(b).or_insert(0) += 1;
    }
    let mut table: Vec<u64> = Vec::new();
    let mut slot: HashMap<u64, i64> = HashMap::new();
    for (k, &b) in edge.iter().enumerate() {
        slot.entry(b).or_insert(k as i64 + 1);
    }
    for &b in &exc_bits {
        if seen[&b] > 1 && !slot.contains_key(&b) && table.len() < 65535 {
            table.push(b);
            slot.insert(b, table.len() as i64 + 4);
        }
    }
    let codes: Vec<i64> = exc_bits.iter().map(|b| *slot.get(b).unwrap_or(&0)).collect();
    let tbytes: Vec<u8> = table.iter().flat_map(|b| b.to_le_bytes()).collect();
    let own: Vec<u8> = exc_bits.iter().zip(&codes).filter(|(_, &c)| c == 0).flat_map(|(b, _)| b.to_le_bytes())
        .collect();
    w.u8(stride as u8);
    for &s in &starts {
        w.u64(s as u64);
    }
    for (c, &s) in starts.iter().enumerate() {
        let mut p = s;
        let d: Vec<i64> = (c..n).step_by(stride).map(|i| {
            let dv = q[i].wrapping_sub(p);
            p = q[i];
            dv
        }).collect();
        put_ints(w, &d);
    }
    put_ints(w, &exc_idx);
    put_raw(w, 8, &tbytes)?;
    put_ints(w, &codes);
    put_raw(w, 8, &own)?;
    Ok(())
}

fn get_grid(r: &mut R, n: usize, bounds: &[f64; 4]) -> Result<Vec<f64>, String> {
    let stride = r.u8()? as usize;
    if stride == 0 || stride > 8 || n % stride != 0 {
        return Err(format!("grid stride {stride} for {n} values"));
    }
    let mut starts = Vec::with_capacity(stride);
    for _ in 0..stride {
        starts.push(r.u64()? as i64);
    }
    let mut out = vec![0f64; n];
    for (c, &s) in starts.iter().enumerate() {
        let d = get_ints(r)?;
        if d.len() != n / stride {
            return Err("grid stream length".into());
        }
        let mut x = s;
        for (k, &dv) in d.iter().enumerate() {
            x = x.wrapping_add(dv);
            out[c + k * stride] = (x as f64) / Q;
        }
    }
    let exc_idx = get_ints(r)?;
    let (_, table) = get_raw(r)?;
    let codes = get_ints(r)?;
    let (_, own) = get_raw(r)?;
    let ntab = table.len() / 8;
    if codes.len() != exc_idx.len() || codes.iter().any(|&c| c < 0 || c as usize > ntab + 4)
        || own.len() != 8 * codes.iter().filter(|&&c| c == 0).count() {
        return Err("grid exceptions do not match".into());
    }
    let f = |b: &[u8], j: usize| f64::from_le_bytes(b[8 * j..8 * j + 8].try_into().unwrap());
    let (mut at, mut k) = (0i64, 0usize);
    for (j, &d) in exc_idx.iter().enumerate() {
        at += d;
        if at < 0 || at as usize >= n {
            return Err("grid exception out of range".into());
        }
        out[at as usize] = match codes[j] {
            0 => {
                k += 1;
                f(&own, k - 1)
            }
            c @ 1..=4 => bounds[c as usize - 1],
            c => f(&table, c as usize - 5),
        };
    }
    Ok(out)
}

// ------------------------------------------------------------------------------------------------ arrays
fn f64s(b: &[u8]) -> Vec<f64> {
    b.chunks_exact(8).map(|c| f64::from_le_bytes(c.try_into().unwrap())).collect()
}

/// The integers of a little-endian integer / bool array as i64 (unsigned 64-bit bit-cast).
fn ints_of(kind: char, item: usize, b: &[u8]) -> Vec<i64> {
    b.chunks_exact(item).map(|c| {
        let mut x = [0u8; 8];
        x[..item].copy_from_slice(c);
        let u = u64::from_le_bytes(x);
        if kind == 'i' && item < 8 {
            let sh = 64 - 8 * item as u32;
            ((u << sh) as i64) >> sh
        } else {
            u as i64
        }
    }).collect()
}

fn bytes_of_ints(item: usize, v: &[i64]) -> Vec<u8> {
    let mut out = Vec::with_capacity(v.len() * item);
    for &x in v {
        out.extend_from_slice(&x.to_le_bytes()[..item]);
    }
    out
}

/// A unicode column's values (None when a code point is not a char: the array goes raw).
fn texts(b: &[u8], width: usize) -> Option<Vec<String>> {
    let mut out = Vec::with_capacity(if width == 0 { 0 } else { b.len() / (4 * width) });
    for row in b.chunks_exact(4 * width) {
        let cps: Vec<u32> = row.chunks_exact(4).map(|c| u32::from_le_bytes(c.try_into().unwrap())).collect();
        let end = cps.iter().rposition(|&c| c != 0).map(|p| p + 1).unwrap_or(0);
        let mut s = String::with_capacity(end);
        for &c in &cps[..end] {
            s.push(char::from_u32(c)?);
        }
        out.push(s);
    }
    Some(out)
}

/// 'V': a column of canonical UUIDs (or empty) that repeat (a rule's connector references): the distinct ones as 16
/// bytes each in first-appearance order, then a code per value (0 empty, k the k-th). False (nothing written) when
/// the column is not all UUIDs or nothing repeats (the BNWT text column's 'U' is as small then).
fn put_uuid_dict(w: &mut W, vals: &[String]) -> bool {
    if vals.is_empty() || !vals.iter().all(|s| s.is_empty() || crate::bnwt::is_uuid(s)) {
        return false;
    }
    let mut seen: HashMap<&str, i64> = HashMap::with_capacity(vals.len());
    let mut words: Vec<&str> = Vec::new();
    let mut codes: Vec<i64> = Vec::with_capacity(vals.len());
    for s in vals {
        if s.is_empty() {
            codes.push(0);
            continue;
        }
        let c = *seen.entry(s.as_str()).or_insert_with(|| {
            words.push(s.as_str());
            words.len() as i64
        });
        codes.push(c);
    }
    if words.len() == vals.iter().filter(|s| !s.is_empty()).count() {
        return false;
    }
    w.u8(b'V');
    w.u32(words.len() as u32);
    for s in &words {
        let hex: Vec<u8> = s.bytes().filter(|&c| c != b'-').collect();
        for k in 0..16 {
            w.u8(crate::bnwt::hexval(hex[2 * k]) << 4 | crate::bnwt::hexval(hex[2 * k + 1]));
        }
    }
    put_ints(w, &codes);
    true
}

fn get_uuid_dict(r: &mut R, n: usize, width: usize) -> Result<Vec<u8>, String> {
    let k = r.u32()? as usize;
    let raw = r.take(k.checked_mul(16).ok_or("uuid table too long")?)?;
    let codes = get_ints(r)?;
    if codes.len() != n || width < 36 || codes.iter().any(|&c| c < 0 || c as usize > k) {
        return Err("uuid codes do not match".into());
    }
    let mut out = vec![0u8; n * width * 4];
    for (i, &c) in codes.iter().enumerate() {
        if c != 0 {
            uuid_ucs4(&raw[(c as usize - 1) * 16..(c as usize) * 16], &mut out, i, width);
        }
    }
    Ok(out)
}

/// Row i of a UCS-4 column (width characters) set to the canonical text of a 16-byte UUID.
fn uuid_ucs4(u: &[u8], out: &mut [u8], i: usize, width: usize) {
    let hex = b"0123456789abcdef";
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

fn uuid_bytes(s: &str) -> [u8; 16] {
    let hex: Vec<u8> = s.bytes().filter(|&c| c != b'-').collect();
    let mut out = [0u8; 16];
    for (k, o) in out.iter_mut().enumerate() {
        *o = crate::bnwt::hexval(hex[2 * k]) << 4 | crate::bnwt::hexval(hex[2 * k + 1]);
    }
    out
}

// ------------------------------------------------------------------------------------------------ context codings
/// What an array may be coded against: the arrays before it in its section, and the sections before its own.
pub struct Ctx<'a> {
    pub own: &'a [Arr],
    pub cell: &'a [(String, &'a [Arr])],
}

fn find<'a>(arrs: &'a [Arr], name: &str) -> Option<&'a Arr> {
    arrs.iter().find(|a| a.name == name)
}

fn i64s(a: &Arr) -> Option<Vec<i64>> {
    if a.dtype != "<i8" || a.shape.len() != 1 {
        return None;
    }
    Some(a.data.chunks_exact(8).map(|c| i64::from_le_bytes(c.try_into().unwrap())).collect())
}

/// The bbox centres ((min + max) / 2, as numpy makes them) of every feature's coordinates on one axis, from the
/// section's geometry arrays; None where a feature has no coordinate or a NaN (or the geometry is not there).
fn centres(own: &[Arr], n: usize, axis: usize) -> Option<Vec<Option<f64>>> {
    let po = i64s(find(own, "part_off")?)?;
    let ro = i64s(find(own, "ring_off")?)?;
    let co = i64s(find(own, "coord_off")?)?;
    let ll = find(own, "lonlat")?;
    if ll.dtype != "<f8" || ll.shape.len() != 2 || ll.shape[1] != 2 || po.len() != n + 1 {
        return None;
    }
    let v = f64s(&ll.data);
    let m = v.len() / 2;
    let at = |o: &[i64], i: i64| -> Option<i64> { o.get(usize::try_from(i).ok()?).copied() };
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let c0 = at(&co, at(&ro, po[i])?)?;
        let c1 = at(&co, at(&ro, po[i + 1])?)?;
        let (c0, c1) = (usize::try_from(c0).ok()?, usize::try_from(c1).ok()?);
        if c1 > m || c0 >= c1 {
            out.push(None);
            continue;
        }
        let xs = (c0..c1).map(|k| v[2 * k + axis]);
        if (c0..c1).any(|k| v[2 * k + axis].is_nan()) {
            out.push(None);
            continue;
        }
        let (mn, mx) = xs.fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), x| (a.min(x), b.max(x)));
        out.push(Some((mn + mx) / 2.0));
    }
    Some(out)
}

/// 'D': attr.cx / attr.cy that are their features' bbox centres (all but a few: those as exceptions, own bits).
fn put_centres(w: &mut W, a: &Arr, ctx: &Ctx) -> Result<bool, String> {
    let axis = match a.name.as_str() {
        "attr.cx" => 0,
        "attr.cy" => 1,
        _ => return Ok(false),
    };
    if a.dtype != "<f8" || a.shape.len() != 1 {
        return Ok(false);
    }
    let v = f64s(&a.data);
    let Some(c) = centres(ctx.own, v.len(), axis) else { return Ok(false) };
    let exc: Vec<usize> = (0..v.len()).filter(|&i| c[i].map(|x| x.to_bits()) != Some(v[i].to_bits())).collect();
    if v.is_empty() || exc.len() * 8 > v.len() {
        return Ok(false);
    }
    w.u8(b'D');
    w.u8(axis as u8);
    let mut last = 0i64;
    let d: Vec<i64> = exc.iter().map(|&i| {
        let dv = i as i64 - last;
        last = i as i64;
        dv
    }).collect();
    put_ints(w, &d);
    let bits: Vec<u8> = exc.iter().flat_map(|&i| v[i].to_le_bytes()).collect();
    put_raw(w, 8, &bits)?;
    Ok(true)
}

fn get_centres(r: &mut R, n: usize, own: &[Arr]) -> Result<Vec<u8>, String> {
    let axis = r.u8()? as usize;
    let d = get_ints(r)?;
    let (_, bits) = get_raw(r)?;
    if axis > 1 || bits.len() != 8 * d.len() {
        return Err("centre exceptions do not match".into());
    }
    let c = centres(own, n, axis).ok_or("bbox centres without their geometry")?;
    let mut out: Vec<f64> = c.iter().map(|x| x.unwrap_or(0.0)).collect();
    let mut at = 0i64;
    for (j, &dv) in d.iter().enumerate() {
        at += dv;
        let i = usize::try_from(at).map_err(|_| "centre exception out of range")?;
        if i >= n {
            return Err("centre exception out of range".into());
        }
        out[i] = f64::from_le_bytes(bits[8 * j..8 * j + 8].try_into().unwrap());
    }
    Ok(out.iter().flat_map(|x| x.to_le_bytes()).collect())
}

/// The referenced section of a rule's value column: "rule.<layer>.value" names an earlier "m:<layer>" section whose
/// "ids" it mostly holds (a road's connector references: the cell's connectors).
fn ref_section(name: &str) -> Option<String> {
    let rest = name.strip_prefix("rule.")?.strip_suffix(".value")?;
    Some(format!("m:{rest}"))
}

/// 'X': a UUID column as references into an earlier section's ids (k + 1: its k-th id; -(j + 1): the j-th of the
/// UUIDs not there, 16 bytes each; 0: empty).
fn put_refs(w: &mut W, a: &Arr, vals: &[String], ctx: &Ctx) -> Result<bool, String> {
    let Some(sec) = ref_section(&a.name) else { return Ok(false) };
    let Some((_, arrs)) = ctx.cell.iter().find(|(n, _)| n == &sec || n.split('.').next() == Some(sec.as_str()))
        else { return Ok(false) };
    let Some(ids) = find(arrs, "ids") else { return Ok(false) };
    let Some((kind, item, _)) = dtype_info(&ids.dtype) else { return Ok(false) };
    if kind != 'U' || item < 4 {
        return Ok(false);
    }
    let Some(table) = texts(&ids.data, item / 4) else { return Ok(false) };
    let mut at: HashMap<&str, i64> = HashMap::with_capacity(table.len());
    for (k, s) in table.iter().enumerate() {
        at.entry(s.as_str()).or_insert(k as i64 + 1);
    }
    let mut codes = Vec::with_capacity(vals.len());
    let mut rest: Vec<&str> = Vec::new();
    let mut hits = 0usize;
    for s in vals {
        if s.is_empty() {
            codes.push(0);
        } else if let Some(&k) = at.get(s.as_str()) {
            hits += 1;
            codes.push(k);
        } else if crate::bnwt::is_uuid(s) {
            rest.push(s);
            codes.push(-(rest.len() as i64));
        } else {
            return Ok(false);
        }
    }
    if hits * 2 < vals.len() {
        return Ok(false);
    }
    let sec_name = ctx.cell.iter().find(|(n, _)| n == &sec || n.split('.').next() == Some(sec.as_str())).unwrap().0.clone();
    w.u8(b'X');
    w.str8(&sec_name)?;
    put_ints(w, &codes);
    w.u32(rest.len() as u32);
    for s in rest {
        w.0.extend_from_slice(&uuid_bytes(s));
    }
    Ok(true)
}

fn get_refs(r: &mut R, n: usize, width: usize, refs: &mut dyn FnMut(&str) -> Result<Vec<String>, String>)
            -> Result<Vec<u8>, String> {
    let sec = r.str8()?;
    let codes = get_ints(r)?;
    let k = r.u32()? as usize;
    let rest = r.take(k.checked_mul(16).ok_or("reference residue too long")?)?;
    let table = refs(&sec)?;
    if codes.len() != n {
        return Err("reference codes do not match".into());
    }
    let mut out = vec![0u8; n * width * 4];
    for (i, &c) in codes.iter().enumerate() {
        if c > 0 {
            let s = table.get(c as usize - 1).ok_or("reference out of range")?;
            let cs: Vec<char> = s.chars().collect();
            if cs.len() > width {
                return Err("reference wider than its column".into());
            }
            for (j, ch) in cs.iter().enumerate() {
                out[(i * width + j) * 4..(i * width + j) * 4 + 4].copy_from_slice(&(*ch as u32).to_le_bytes());
            }
        } else if c < 0 {
            let j = (-c) as usize - 1;
            if j >= k || width < 36 {
                return Err("reference residue out of range".into());
            }
            uuid_ucs4(&rest[16 * j..16 * j + 16], &mut out, i, width);
        }
    }
    Ok(out)
}

fn put_array(w: &mut W, a: &Arr, bounds: &[f64; 4], ctx: &Ctx) -> Result<(), String> {
    let (kind, item, little) = dtype_info(&a.dtype).ok_or_else(|| format!("{}: dtype {}", a.name, a.dtype))?;
    let n = count(&a.shape).ok_or_else(|| format!("{}: shape", a.name))?;
    if n.checked_mul(item) != Some(a.data.len()) {
        return Err(format!("{}: {} bytes for {} items of {} bytes", a.name, a.data.len(), n, item));
    }
    w.str8(&a.name)?;
    w.str8(&a.dtype)?;
    w.u8(a.shape.len() as u8);
    for &d in &a.shape {
        w.u64(d);
    }
    let raw = |w: &mut W| -> Result<(), String> {
        w.u8(b'R');
        if item == 0 {
            w.u32(0);
            return Ok(());
        }
        w.u32(1);
        // (items wider than 255 bytes: one plane of the bytes as they are)
        put_raw(w, if item > 255 { 1 } else { item }, &a.data)
    };
    if !little && item > 1 {
        return raw(w);
    }
    match kind {
        'U' => {
            let width = item / 4;
            if width > 0 {
                if let Some(vals) = texts(&a.data, width) {
                    if width >= 36 && put_refs(w, a, &vals, ctx)? {
                        return Ok(());
                    }
                    if width >= 36 && put_uuid_dict(w, &vals) {
                        return Ok(());
                    }
                    w.u8(b'T');
                    put_text(w, &vals, width as u32);
                    return Ok(());
                }
            }
            raw(w)
        }
        'f' if item == 8 => {
            if put_centres(w, a, ctx)? {
                return Ok(());
            }
            let v = f64s(&a.data);
            let pairs = a.shape.len() == 2 && a.shape[1] == 2 && a.name.ends_with("lonlat");
            if pairs {
                w.u8(b'C');
                return put_grid(w, &v, 2, bounds);
            }
            if !v.is_empty() && v.iter().all(|&x| (x as f32 as f64).to_bits() == x.to_bits()) {
                w.u8(b'F');
                let b: Vec<u8> = v.iter().flat_map(|&x| (x as f32).to_le_bytes()).collect();
                return put_raw(w, 4, &b);
            }
            let on = v.iter().filter(|&&x| {
                let r = (x * Q).round();
                r.is_finite() && r.abs() < 4.0e18 && ((r as i64 as f64) / Q).to_bits() == x.to_bits()
            }).count();
            if !v.is_empty() && on * 2 > v.len() {
                w.u8(b'G');
                return put_grid(w, &v, 1, bounds);
            }
            raw(w)
        }
        'i' | 'u' | 'b' if matches!(item, 1 | 2 | 4 | 8) => {
            let v = ints_of(kind, item, &a.data);
            if a.shape.len() == 1 && v.len() > 1 && v.windows(2).all(|p| p[1] >= p[0]) {
                w.u8(b'J');
                let mut prev = 0i64;
                let d: Vec<i64> = v.iter().map(|&x| {
                    let dv = x.wrapping_sub(prev);
                    prev = x;
                    dv
                }).collect();
                put_ints(w, &d);
            } else {
                w.u8(b'I');
                put_ints(w, &v);
            }
            Ok(())
        }
        _ => raw(w),
    }
}

fn get_array(r: &mut R, bounds: &[f64; 4], own: &[Arr], refs: &mut dyn FnMut(&str) -> Result<Vec<String>, String>)
             -> Result<Arr, String> {
    let name = r.str8()?;
    let dtype = r.str8()?;
    let nd = r.u8()? as usize;
    let mut shape = Vec::with_capacity(nd);
    for _ in 0..nd {
        shape.push(r.u64()?);
    }
    let (kind, item, _little) = dtype_info(&dtype).ok_or_else(|| format!("{name}: dtype {dtype}"))?;
    let n = count(&shape).ok_or_else(|| format!("{name}: shape"))?;
    let want = n.checked_mul(item).ok_or("array too large")?;
    let enc = r.u8()?;
    let data = match enc {
        b'R' => {
            if r.u32()? == 0 {
                Vec::new()
            } else {
                get_raw(r)?.1
            }
        }
        b'T' => {
            let t = get_text(r)?;
            if t.width * 4 != item {
                return Err(format!("{name}: text width {} for {dtype}", t.width));
            }
            t.ucs4
        }
        b'V' => get_uuid_dict(r, n, item / 4)?,
        b'X' => get_refs(r, n, item / 4, refs)?,
        b'D' => get_centres(r, n, own)?,
        b'C' | b'G' => get_grid(r, n, bounds)?.iter().flat_map(|x| x.to_le_bytes()).collect(),
        b'F' => {
            let (_, b) = get_raw(r)?;
            b.chunks_exact(4).flat_map(|c| (f32::from_le_bytes(c.try_into().unwrap()) as f64).to_le_bytes()).collect()
        }
        b'I' => bytes_of_ints(item, &get_ints(r)?),
        b'J' => {
            let d = get_ints(r)?;
            let mut acc = 0i64;
            let v: Vec<i64> = d.iter().map(|&x| {
                acc = acc.wrapping_add(x);
                acc
            }).collect();
            bytes_of_ints(item, &v)
        }
        e => return Err(format!("{name}: encoding {e}")),
    };
    let _ = kind;
    if data.len() != want {
        return Err(format!("{name}: {} bytes decoded, {want} expected", data.len()));
    }
    Ok(Arr { name, dtype, shape, data })
}

fn encode_arrays(arrs: &[Arr], bounds: &[f64; 4], cell: &[(String, &[Arr])]) -> Result<Vec<u8>, String> {
    if arrs.len() > 65535 {
        return Err("too many arrays".into());
    }
    let mut w = W(Vec::new());
    w.u16(arrs.len() as u16);
    for (i, a) in arrs.iter().enumerate() {
        put_array(&mut w, a, bounds, &Ctx { own: &arrs[..i], cell })?;
    }
    Ok(w.0)
}

fn decode_arrays(b: &[u8], bounds: &[f64; 4], refs: &mut dyn FnMut(&str) -> Result<Vec<String>, String>)
                 -> Result<Vec<Arr>, String> {
    let mut r = R { b, i: 0 };
    let n = r.u16()? as usize;
    let mut out: Vec<Arr> = Vec::with_capacity(n);
    for _ in 0..n {
        let a = get_array(&mut r, bounds, &out, refs)?;
        out.push(a);
    }
    if r.i != b.len() {
        return Err(format!("{} bytes left after the arrays", b.len() - r.i));
    }
    Ok(out)
}

// ------------------------------------------------------------------------------------------------ cells
pub struct Head {
    pub format: u16,
    pub secs: Vec<(String, u8, u32, u32)>, // name, codec, raw offset, raw length
    pub body: usize,
}

pub fn parse_head(t: &[u8]) -> Result<Head, String> {
    if t.len() < 8 || &t[0..4] != MAGIC {
        return Err("not a CIP cell".into());
    }
    let mut r = R { b: t, i: 4 };
    let format = r.u16()?;
    if format != FORMAT {
        return Err(format!("CIP format {format} ({FORMAT} expected)"));
    }
    let ns = r.u16()? as usize;
    let mut secs = Vec::with_capacity(ns);
    let mut end = 0u64;
    for _ in 0..ns {
        let name = r.str8()?;
        let codec = r.u8()?;
        let off = r.u32()?;
        let len = r.u32()?;
        if !matches!(codec, b'A' | b'B') || off as u64 != end {
            return Err(format!("CIP section {name}: codec {codec}, offset {off}"));
        }
        end += len as u64;
        secs.push((name, codec, off, len));
    }
    Ok(Head { format, secs, body: r.i })
}

pub fn encode_cell(secs: &[(String, Sec)], bounds: &[f64; 4], level: i32) -> Result<Vec<u8>, String> {
    if secs.len() > 65535 {
        return Err("too many sections".into());
    }
    let mut raw = Vec::new();
    let mut dir = Vec::new();
    let mut before: Vec<(String, &[Arr])> = Vec::new();
    for (name, s) in secs {
        let (codec, b) = match s {
            Sec::Arrays(a) => {
                let got = encode_arrays(a, bounds, &before)?;
                before.push((name.clone(), a.as_slice()));
                (b'A', got)
            }
            Sec::Blob(b) => (b'B', b.clone()),
        };
        if raw.len() + b.len() > u32::MAX as usize {
            return Err("cell too large".into());
        }
        dir.push((name.clone(), codec, raw.len() as u32, b.len() as u32));
        raw.extend_from_slice(&b);
    }
    let mut w = W(Vec::new());
    w.0.extend_from_slice(MAGIC);
    w.u16(FORMAT);
    w.u16(dir.len() as u16);
    for (name, codec, off, len) in &dir {
        w.str8(name)?;
        w.u8(*codec);
        w.u32(*off);
        w.u32(*len);
    }
    let body = zstd::bulk::compress(&raw, level).map_err(|e| format!("zstd: {e}"))?;
    w.0.extend_from_slice(&body);
    Ok(w.0)
}

/// The raw bytes of the frame up to `upto` (the end of the last section wanted): the frame is decoded only that far.
fn raw_upto(t: &[u8], h: &Head, upto: usize) -> Result<Vec<u8>, String> {
    let mut dec = zstd::stream::read::Decoder::new(&t[h.body..]).map_err(|e| format!("zstd: {e}"))?;
    let mut out = vec![0u8; upto];
    dec.read_exact(&mut out).map_err(|e| format!("zstd: {e}"))?;
    Ok(out)
}

pub fn decode_sections(t: &[u8], names: Option<&[String]>, bounds: &[f64; 4]) -> Result<Vec<(String, Sec)>, String> {
    let h = parse_head(t)?;
    let want: Vec<&(String, u8, u32, u32)> = h.secs.iter()
        .filter(|s| names.map(|n| n.iter().any(|x| x == &s.0)).unwrap_or(true)).collect();
    let upto = want.iter().map(|s| (s.2 + s.3) as usize).max().unwrap_or(0);
    let raw = if upto > 0 { raw_upto(t, &h, upto)? } else { Vec::new() };
    let mut out = Vec::with_capacity(want.len());
    let mut ids: HashMap<String, Vec<String>> = HashMap::new();
    for (name, codec, off, len) in want {
        let b = &raw[*off as usize..(*off + *len) as usize];
        let sec = if *codec == b'A' {
            let mut refs = |sec: &str| -> Result<Vec<String>, String> {
                if let Some(v) = ids.get(sec) {
                    return Ok(v.clone());
                }
                // (a section before this one, in the part of the frame already decoded)
                let (_, c, o, l) = h.secs.iter().find(|s| s.0 == sec).ok_or("referenced section missing")?;
                if *c != b'A' || (*o + *l) as usize > raw.len() || *o >= *off {
                    return Err("referenced section after its reference".into());
                }
                let arrs = decode_arrays(&raw[*o as usize..(*o + *l) as usize], bounds,
                                         &mut |_s: &str| Err("references nest".into()))?;
                let a = find(&arrs, "ids").ok_or("referenced section without ids")?;
                let (_, item, _) = dtype_info(&a.dtype).ok_or("referenced ids dtype")?;
                let v = texts(&a.data, item / 4).ok_or("referenced ids not text")?;
                ids.insert(sec.to_string(), v.clone());
                Ok(v)
            };
            Sec::Arrays(decode_arrays(b, bounds, &mut refs)?)
        } else {
            Sec::Blob(b.to_vec())
        };
        out.push((name.clone(), sec));
    }
    Ok(out)
}

// ------------------------------------------------------------------------------------------------ Python
fn sec_from_py(o: &Bound<'_, PyAny>) -> PyResult<(String, Sec)> {
    let t = o.cast::<PyTuple>()?;
    let name: String = t.get_item(0)?.extract()?;
    let codec: String = t.get_item(1)?.extract()?;
    let p = t.get_item(2)?;
    match codec.as_str() {
        "B" => Ok((name, Sec::Blob(p.cast::<PyBytes>()?.as_bytes().to_vec()))),
        "A" => {
            let mut arrs = Vec::new();
            for item in p.cast::<PyList>()?.iter() {
                let a = item.cast::<PyTuple>()?;
                arrs.push(Arr {
                    name: a.get_item(0)?.extract()?,
                    dtype: a.get_item(1)?.extract()?,
                    shape: a.get_item(2)?.extract()?,
                    data: a.get_item(3)?.cast::<PyBytes>()?.as_bytes().to_vec(),
                });
            }
            Ok((name, Sec::Arrays(arrs)))
        }
        _ => Err(PyValueError::new_err(format!("section codec {codec}"))),
    }
}

/// The bytes of one cell's file from its sections [(name, "A", [(array name, dtype str, shape, bytes)]) | (name, "B",
/// bytes)] in their order; bounds: the cell's (west, south, east, north), the values a clip cuts at (coded, never
/// stored); level: zstd's.
#[pyfunction]
#[pyo3(signature = (sections, bounds, level=19))]
pub fn cip_encode_cell<'py>(py: Python<'py>, sections: Vec<Bound<'py, PyAny>>, bounds: [f64; 4], level: i32)
                            -> PyResult<Bound<'py, PyBytes>> {
    let secs: Vec<(String, Sec)> = sections.iter().map(sec_from_py).collect::<PyResult<_>>()?;
    let out = py.detach(move || encode_cell(&secs, &bounds, level)).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &out))
}

/// (format, [(name, codec, raw length)]) of a cell file.
#[pyfunction]
pub fn cip_cell_info(cell: &[u8]) -> PyResult<(u16, Vec<(String, String, u32)>)> {
    let h = parse_head(cell).map_err(PyValueError::new_err)?;
    Ok((h.format, h.secs.iter().map(|s| (s.0.clone(), (s.1 as char).to_string(), s.3)).collect()))
}

/// The sections of a cell file (names: those only; None: all), decoded: [(name, "B", bytes) | (name, "A", data,
/// [(array name, dtype str, shape, offset, nbytes)])]: an 'A' section's arrays are views into one buffer `data`,
/// each at an offset aligned to 16 bytes (np.frombuffer(data, dtype, count, offset)).
#[pyfunction]
#[pyo3(signature = (cell, bounds, names=None))]
pub fn cip_decode<'py>(py: Python<'py>, cell: &[u8], bounds: [f64; 4], names: Option<Vec<String>>)
                       -> PyResult<Bound<'py, PyList>> {
    let got = py.detach(|| decode_sections(cell, names.as_deref(), &bounds)).map_err(PyValueError::new_err)?;
    let out = PyList::empty(py);
    for (name, s) in got {
        match s {
            Sec::Blob(b) => {
                out.append((name, "B", PyBytes::new(py, &b)))?;
            }
            Sec::Arrays(arrs) => {
                let mut total = 0usize;
                for a in &arrs {
                    total = (total + ALIGN - 1) / ALIGN * ALIGN + a.data.len();
                }
                let mut meta = Vec::with_capacity(arrs.len());
                let data = PyBytes::new_with(py, total, |buf: &mut [u8]| {
                    let mut at = 0usize;
                    for a in &arrs {
                        at = (at + ALIGN - 1) / ALIGN * ALIGN;
                        buf[at..at + a.data.len()].copy_from_slice(&a.data);
                        meta.push((a.name.clone(), a.dtype.clone(), a.shape.clone(), at, a.data.len()));
                        at += a.data.len();
                    }
                    Ok(())
                })?;
                out.append((name, "A", data, meta))?;
            }
        }
    }
    Ok(out)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cip_encode_cell, m)?)?;
    m.add_function(wrap_pyfunction!(cip_cell_info, m)?)?;
    m.add_function(wrap_pyfunction!(cip_decode, m)?)?;
    m.add("CIP_FORMAT", FORMAT)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn arr(name: &str, dtype: &str, shape: &[u64], data: Vec<u8>) -> Arr {
        Arr { name: name.into(), dtype: dtype.into(), shape: shape.to_vec(), data }
    }

    fn ucs4(vals: &[&str], width: usize) -> Vec<u8> {
        let mut out = vec![0u8; vals.len() * width * 4];
        for (i, s) in vals.iter().enumerate() {
            for (k, c) in s.chars().enumerate() {
                out[(i * width + k) * 4..(i * width + k) * 4 + 4].copy_from_slice(&(c as u32).to_le_bytes());
            }
        }
        out
    }

    #[test]
    fn round_trip_every_encoding() {
        let b = [-76.6, 39.27, -76.58, 39.29];
        let ll: Vec<f64> = vec![-76.5934567, 39.2834567, -76.6, 39.28, 1.0 / 3.0, 39.2801, -76.6, 39.29];
        let mut secs = vec![("roads".to_string(), Sec::Arrays(vec![
            arr("ids", "<U36", &[3], ucs4(&["0a1b2c3d-0000-4000-8000-00000000000a", "", "x\u{0}y"], 36)),
            arr("lonlat", "<f8", &[4, 2], ll.iter().flat_map(|x| x.to_le_bytes()).collect()),
            arr("coord_off", "<i8", &[3], [0i64, 2, 4].iter().flat_map(|x| x.to_le_bytes()).collect()),
            arr("kind", "|u1", &[3], vec![2, 1, 2]),
            arr("neg", "<i4", &[3], [-5i32, 7, -1].iter().flat_map(|x| x.to_le_bytes()).collect()),
            arr("own", "|b1", &[3], vec![1, 0, 1]),
            arr("w", "<f8", &[2], [3.5f64, 0.25].iter().flat_map(|x| x.to_le_bytes()).collect()),
            arr("h", "<f8", &[3], [0.1f64, f64::NAN, 1e300].iter().flat_map(|x| x.to_le_bytes()).collect()),
            arr("f4", "<f4", &[2], [0.1f32, 2.0].iter().flat_map(|x| x.to_le_bytes()).collect()),
            arr("prov", "<U5", &[], ucs4(&["{\"a\"}"], 5)),
            arr("conn", "<U36", &[4], ucs4(&["0a1b2c3d-0000-4000-8000-00000000000a", "",
                                             "0a1b2c3d-0000-4000-8000-00000000000a",
                                             "ffffffff-0000-4000-8000-00000000000b"], 36)),
            arr("empty", "<i8", &[0], vec![]),
            arr("grid", "|u1", &[2, 3], vec![1, 2, 3, 4, 5, 6]),
        ]))];
        secs.insert(0, ("m:connectors".to_string(), Sec::Arrays(vec![
            arr("ids", "<U36", &[2], ucs4(&["0a1b2c3d-0000-4000-8000-00000000000a",
                                            "ffffffff-0000-4000-8000-00000000000b"], 36)),
        ])));
        secs[1].0 = "m:roads.9".to_string();
        if let Sec::Arrays(a) = &mut secs[1].1 {
            a.push(arr("rule.connectors.value", "<U36", &[4], ucs4(&["0a1b2c3d-0000-4000-8000-00000000000a", "",
                "eeeeeeee-0000-4000-8000-00000000000c", "ffffffff-0000-4000-8000-00000000000b"], 36)));
            let po = [0i64, 1, 2, 3, 4];
            let ro = [0i64, 1, 2, 3, 4];
            let co = [0i64, 1, 2, 3, 4];
            a.push(arr("part_off", "<i8", &[5], po.iter().flat_map(|x| x.to_le_bytes()).collect()));
            a.push(arr("ring_off", "<i8", &[5], ro.iter().flat_map(|x| x.to_le_bytes()).collect()));
            a.push(arr("coord_off", "<i8", &[5], co.iter().flat_map(|x| x.to_le_bytes()).collect()));
            let cx: Vec<f64> = ll.chunks(2).map(|p| (p[0] + p[0]) / 2.0).collect();
            a.push(arr("attr.cx", "<f8", &[4], cx.iter().flat_map(|x| x.to_le_bytes()).collect()));
        }
        secs.push(("lidar".to_string(), Sec::Blob(b"answers".to_vec())));
        let cell = encode_cell(&secs, &b, 19).unwrap();
        let back = decode_sections(&cell, None, &b).unwrap();
        assert_eq!(back.len(), 3);
        let roads_only = decode_sections(&cell, Some(&["m:roads.9".to_string()]), &b).unwrap();
        assert_eq!(roads_only.len(), 1);
        let (Sec::Arrays(x), Sec::Arrays(y)) = (&secs[1].1, &back[1].1) else { panic!() };
        let Sec::Arrays(z) = &roads_only[0].1 else { panic!() };
        for (a, c) in x.iter().zip(z) {
            assert_eq!(a.data, c.data, "{}", a.name);
        }
        for (a, c) in x.iter().zip(y) {
            assert_eq!(a.name, c.name);
            assert_eq!(a.dtype, c.dtype);
            assert_eq!(a.shape, c.shape);
            assert_eq!(a.data, c.data, "{}", a.name);
        }
        let Sec::Blob(bl) = &back[2].1 else { panic!() };
        assert_eq!(bl, b"answers");
        let only = decode_sections(&cell, Some(&["lidar".to_string()]), &b).unwrap();
        assert_eq!(only.len(), 1);
        // deterministic bytes
        assert_eq!(cell, encode_cell(&secs, &b, 19).unwrap());
    }
}
