//! The in-game reports' privacy scrub and upload packing (the user, 2026-11-12: "update the in game reporter to send
//! reports directly to our mini pc server" and "update the reporter with any info and data that would be useful to
//! us"; reports are private: sent to the developers only, never published).
//!
//! report_scrub(text, names, coarse)        home folders (C:\Users\<name>, /home/<name>, /Users/<name>, ...), the
//!                                          given user names, e-mail and IP addresses replaced; coarse: every number
//!                                          with 3+ decimals up to 180 rounded to 2 decimals (lat / lon to ~1 km)
//! report_scrub_json(text, names, coarse)   the same inside a JSON document's strings and keys; coarse also rounds
//!                                          the numbers under lat / lon keys and drops FINE_KEYS (exact-spot data)
//! report_log_tail(text, kind, lines, s)    a log's useful end: "server" its last lines (and the last perf hitch lines
//!                                          before them), "beamng" the warnings and errors of its last s seconds
//! report_logs_pack(json, names, coarse, max)  {"server.log": .., "beamng.log": ..} scrubbed and gzipped under max
//!                                          bytes (the oldest lines dropped until it fits)
//! report_body(...)                         the collector's POST /report body (native/serverlist reports.rs): each
//!                                          JSON part scrubbed and kept under the part cap (its largest keys dropped,
//!                                          listed in "_dropped"), binary parts base64, the whole under the cap
use crate::dm::Dm;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use serde_json::{Map, Value};
use std::io::Write;

pub const MAX_JSON: usize = 250 * 1024; // (the collector's cap is 256 KB: some room)
pub const MAX_SHOT: usize = 1_500_000;
pub const MAX_TOTAL: usize = 1_990_000;
pub const MAX_LOGS: usize = 250 * 1024;
/// Keys whose values pin the exact spot (dropped when the player leaves the exact location out).
pub const FINE_KEYS: [&str; 7] = ["spot", "pick", "nearby", "street", "address", "where", "anchorKey"];
/// User names too common to replace as words (they are also everyday words or generic accounts).
const GENERIC: [&str; 14] = ["deck", "user", "users", "root", "admin", "administrator", "steam", "steamuser", "owner",
                             "home", "public", "default", "guest", "pc"];

fn sep(c: char) -> bool {
    c == '/' || c == '\\'
}

fn name_char(c: char) -> bool {
    !(sep(c) || c.is_whitespace() || matches!(c, '"' | '\'' | ':' | ';' | ',' | '|' | '<' | '>' | ')' | ']' | '}' | '`'))
}

/// Case-insensitive ASCII prefix at chars[i..].
fn at(chars: &[char], i: usize, word: &str) -> bool {
    let w: Vec<char> = word.chars().collect();
    i + w.len() <= chars.len() && chars[i..i + w.len()].iter().zip(&w).all(|(a, b)| a.eq_ignore_ascii_case(b))
}

/// After a run of one or more separators at chars[i..]: the index past them (None: none there).
fn seps(chars: &[char], i: usize) -> Option<usize> {
    let mut j = i;
    while j < chars.len() && sep(chars[j]) {
        j += 1;
    }
    (j > i).then_some(j)
}

/// A home folder starting at chars[i]: the index past its user-name part.
fn home_at(chars: &[char], i: usize) -> Option<usize> {
    let prev = if i > 0 { Some(chars[i - 1]) } else { None };
    let mut j;
    if chars[i].is_ascii_alphabetic() && i + 1 < chars.len() && chars[i + 1] == ':'
        && prev.is_none_or(|p| !p.is_ascii_alphanumeric())
    {
        // C:\Users\<name>, C:/Documents and Settings/<name>
        j = seps(chars, i + 2)?;
        if at(chars, j, "users") {
            j += 5;
        } else if at(chars, j, "documents and settings") {
            j += 22;
        } else {
            return None;
        }
    } else if sep(chars[i]) && prev.is_none_or(|p| !p.is_ascii_alphanumeric() && !sep(p)) {
        // /home/<name>, /var/home/<name>, /Users/<name>, /media/<name>, /run/media/<name>, /mnt/c/Users/<name>
        j = seps(chars, i)?;
        let mut hit = false;
        for w in ["var/home", "run/media", "home", "users", "media"] {
            if at(chars, j, w) {
                j += w.len();
                hit = true;
                break;
            }
        }
        if !hit && at(chars, j, "mnt") {
            let k = seps(chars, j + 3)?;
            if k < chars.len() && chars[k].is_ascii_alphabetic() {
                let k = seps(chars, k + 1)?;
                if at(chars, k, "users") {
                    j = k + 5;
                    hit = true;
                }
            }
        }
        if !hit {
            return None;
        }
    } else {
        return None;
    }
    let k = seps(chars, j)?;
    let mut e = k;
    while e < chars.len() && name_char(chars[e]) {
        e += 1;
    }
    // (a name with spaces, "C:\Users\Jane Doe\...": taken whole when a separator follows it)
    let mut e2 = e;
    while e2 < chars.len() && (name_char(chars[e2]) || chars[e2] == ' ') {
        e2 += 1;
    }
    if e2 > e && e2 < chars.len() && sep(chars[e2]) && chars[e2 - 1] != ' ' {
        e = e2;
    }
    (e > k).then_some(e)
}

fn ipv4_at(chars: &[char], i: usize) -> Option<usize> {
    if i > 0 && (chars[i - 1].is_ascii_alphanumeric() || chars[i - 1] == '.') {
        return None;
    }
    let mut j = i;
    let mut first = 0u32;
    for part in 0..4 {
        let s = j;
        let mut n = 0u32;
        while j < chars.len() && chars[j].is_ascii_digit() && j - s < 4 {
            n = n * 10 + chars[j] as u32 - '0' as u32;
            j += 1;
        }
        if j == s || j - s > 3 || n > 255 {
            return None;
        }
        if part == 0 {
            first = n;
        }
        if part < 3 {
            if j >= chars.len() || chars[j] != '.' {
                return None;
            }
            j += 1;
        }
    }
    // (not part of a longer dotted number: a game build "0.40.4.0.12345"; 0.x and 127.x are no one's address)
    if j < chars.len() && (chars[j].is_ascii_alphanumeric() || (chars[j] == '.' && chars.get(j + 1).is_some_and(|c| c.is_ascii_digit()))) {
        return None;
    }
    (first != 0 && first != 127).then_some(j)
}

fn ipv6_at(chars: &[char], i: usize) -> Option<usize> {
    if i > 0 && (chars[i - 1].is_ascii_alphanumeric() || chars[i - 1] == ':') {
        return None;
    }
    let mut j = i;
    while j < chars.len() && (chars[j].is_ascii_hexdigit() || chars[j] == ':') {
        j += 1;
    }
    if j < chars.len() && (chars[j].is_ascii_alphanumeric() || chars[j] == '_') {
        return None;
    }
    let s: String = chars[i..j].iter().collect();
    let colons = s.matches(':').count();
    let groups: Vec<&str> = s.split(':').collect();
    let ok = colons >= 2
        && (s.contains("::") || colons >= 7)
        && !s.contains(":::")
        && groups.iter().all(|g| g.len() <= 4)
        && groups.iter().filter(|g| !g.is_empty()).count() >= 2
        && s.chars().any(|c| c.is_ascii_hexdigit());
    ok.then_some(j)
}

fn email_at(chars: &[char], i: usize) -> Option<(usize, usize)> {
    // i at '@': the local part before it, a dotted domain after
    let local = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '%' | '+' | '-');
    let mut s = i;
    while s > 0 && local(chars[s - 1]) {
        s -= 1;
    }
    let mut e = i + 1;
    while e < chars.len() && (chars[e].is_ascii_alphanumeric() || matches!(chars[e], '.' | '-')) {
        e += 1;
    }
    while e > i + 1 && chars[e - 1] == '.' {
        e -= 1;
    }
    let dom: String = chars[i + 1..e].iter().collect();
    (s < i && dom.contains('.') && !dom.starts_with('.')).then_some((s, e))
}

/// A decimal number of 3+ decimals at chars[i] (|x| <= 180): (end, rounded text).
fn coord_at(chars: &[char], i: usize) -> Option<(usize, String)> {
    let word = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '.';
    if i > 0 && (word(chars[i - 1]) || chars[i - 1] == '-') {
        return None;
    }
    let mut j = i;
    if chars[j] == '-' {
        j += 1;
    }
    let ds = j;
    while j < chars.len() && chars[j].is_ascii_digit() {
        j += 1;
    }
    if j == ds || j - ds > 3 || j >= chars.len() || chars[j] != '.' {
        return None;
    }
    j += 1;
    let fs = j;
    while j < chars.len() && chars[j].is_ascii_digit() {
        j += 1;
    }
    if j - fs < 3 || (j < chars.len() && (word(chars[j]))) {
        return None;
    }
    let s: String = chars[i..j].iter().collect();
    let x: f64 = s.parse().ok()?;
    (x.abs() <= 180.0).then(|| (j, format!("{:.2}", x)))
}

/// A map tile key "g<z>_<x>_<y>" (geotiles.key) finer than COARSE_Z at chars[i]: (end, the key of its COARSE_Z tile).
const COARSE_Z: u32 = 14; // (~2.4 km at the equator, finer towards the poles)
fn tile_key_at(chars: &[char], i: usize) -> Option<(usize, String)> {
    if chars[i] != 'g' || (i > 0 && (chars[i - 1].is_ascii_alphanumeric())) {
        return None;
    }
    let mut j = i + 1;
    let mut nums = [0u64; 3];
    for (k, n) in nums.iter_mut().enumerate() {
        let s = j;
        while j < chars.len() && chars[j].is_ascii_digit() && j - s < 10 {
            *n = *n * 10 + (chars[j] as u64 - '0' as u64);
            j += 1;
        }
        if j == s {
            return None;
        }
        if k < 2 {
            if j >= chars.len() || chars[j] != '_' {
                return None;
            }
            j += 1;
        }
    }
    if j < chars.len() && chars[j].is_ascii_alphanumeric() {
        return None;
    }
    let z = nums[0] as u32;
    if z <= COARSE_Z || z > 30 {
        return None;
    }
    let sh = z - COARSE_Z;
    Some((j, format!("g{COARSE_Z}_{}_{}", nums[1] >> sh, nums[2] >> sh)))
}

fn word_eq(chars: &[char], i: usize, name: &[char]) -> bool {
    let n = name.len();
    if i + n > chars.len() || (i > 0 && chars[i - 1].is_alphanumeric()) {
        return false;
    }
    if i + n < chars.len() && chars[i + n].is_alphanumeric() {
        return false;
    }
    chars[i..i + n].iter().zip(name).all(|(a, b)| a.to_lowercase().eq(b.to_lowercase()))
}

pub fn clean_names(names: &[String]) -> Vec<Vec<char>> {
    let mut out: Vec<Vec<char>> = names
        .iter()
        .map(|n| n.trim())
        .filter(|n| n.chars().count() >= 3 && !GENERIC.contains(&n.to_lowercase().as_str()))
        .map(|n| n.chars().collect())
        .collect();
    out.sort_by_key(|n| std::cmp::Reverse(n.len()));
    out.dedup();
    out
}

/// See the module's notes.
pub fn scrub(text: &str, names: &[Vec<char>], coarse: bool) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    'outer: while i < chars.len() {
        let c = chars[i];
        if c.is_ascii_alphabetic() || sep(c) {
            if let Some(e) = home_at(&chars, i) {
                out.push_str("<home>");
                i = e;
                continue;
            }
        }
        if c == '@' {
            if let Some((s, e)) = email_at(&chars, i) {
                // (the local part already went out: take it back)
                let back: usize = chars[s..i].iter().map(|c| c.len_utf8()).sum();
                out.truncate(out.len() - back);
                out.push_str("<email>");
                i = e;
                continue;
            }
        }
        if c.is_ascii_digit() {
            if let Some(e) = ipv4_at(&chars, i) {
                out.push_str("<ip>");
                i = e;
                continue;
            }
        }
        if c.is_ascii_hexdigit() || c == ':' {
            if let Some(e) = ipv6_at(&chars, i) {
                out.push_str("<ip>");
                i = e;
                continue;
            }
        }
        if coarse && c == 'g' {
            if let Some((e, k)) = tile_key_at(&chars, i) {
                out.push_str(&k);
                i = e;
                continue;
            }
        }
        if coarse && (c.is_ascii_digit() || c == '-') {
            if let Some((e, r)) = coord_at(&chars, i) {
                out.push_str(&r);
                i = e;
                continue;
            }
        }
        if c.is_alphanumeric() {
            for n in names {
                if word_eq(&chars, i, n) {
                    out.push_str("<user>");
                    i += n.len();
                    continue 'outer;
                }
            }
            // (the rest of the word as is: a name only matches a whole word)
            while i < chars.len() && chars[i].is_alphanumeric() {
                out.push(chars[i]);
                i += 1;
            }
            continue;
        }
        out.push(c);
        i += 1;
    }
    out
}

fn latlon_key(k: &str) -> bool {
    let l = k.to_ascii_lowercase();
    matches!(l.as_str(), "lat" | "lon" | "lng" | "latitude" | "longitude")
        || k.ends_with("Lat") || k.ends_with("Lon") || k.ends_with("Lng")
        || l.starts_with("lat_") || l.starts_with("lon_")
}

fn latlon_list_key(k: &str) -> bool {
    let l = k.to_ascii_lowercase();
    l.contains("latlon") || l.contains("lonlat") || l == "ll"
}

fn round2(v: &Value) -> Value {
    match v {
        Value::Number(n) => match n.as_f64() {
            Some(x) if n.is_f64() => serde_json::Number::from_f64((x * 100.0).round() / 100.0).map(Value::Number).unwrap_or(Value::Null),
            _ => v.clone(),
        },
        Value::Array(a) => Value::Array(a.iter().map(round2).collect()),
        _ => v.clone(),
    }
}

pub fn scrub_value(v: &Value, names: &[Vec<char>], coarse: bool) -> Value {
    match v {
        Value::String(s) => Value::String(scrub(s, names, coarse)),
        Value::Array(a) => Value::Array(a.iter().map(|x| scrub_value(x, names, coarse)).collect()),
        Value::Object(o) => {
            let mut m = Map::new();
            for (k, x) in o {
                if coarse && FINE_KEYS.contains(&k.as_str()) {
                    continue;
                }
                let x = if coarse && (latlon_key(k) || latlon_list_key(k)) { round2(x) } else { x.clone() };
                m.insert(scrub(k, names, false), scrub_value(&x, names, coarse));
            }
            Value::Object(m)
        }
        _ => v.clone(),
    }
}

/// A JSON object kept under max bytes: its largest top-level values dropped (named in "_dropped") until it fits.
pub fn shrink(mut v: Value, max: usize) -> Value {
    let mut dropped: Vec<String> = Vec::new();
    loop {
        let n = serde_json::to_vec(&v).map(|b| b.len()).unwrap_or(usize::MAX);
        if n <= max {
            break;
        }
        let Some(o) = v.as_object_mut() else { return Value::Object(Map::new()) };
        let big = o
            .iter()
            .filter(|(k, _)| k.as_str() != "_dropped" && !matches!(k.as_str(), "kind" | "title" | "note"))
            .map(|(k, x)| (serde_json::to_vec(x).map(|b| b.len()).unwrap_or(0), k.clone()))
            .max();
        match big {
            Some((_, k)) => {
                o.remove(&k);
                dropped.push(k);
                o.insert("_dropped".into(), Value::from(dropped.clone()));
            }
            None => {
                // (only the text is left and it is over: cut the note)
                if let Some(Value::String(s)) = o.get_mut("note") {
                    let keep: String = s.chars().take(max / 2).collect();
                    *s = keep;
                } else {
                    return Value::Object(Map::new());
                }
            }
        }
    }
    v
}

/// The end of a log worth sending: see the module's notes.
pub fn log_tail(text: &str, kind: &str, lines: usize, seconds: f64) -> String {
    let cut = |l: &str| -> String { l.chars().take(1000).collect() };
    let all: Vec<&str> = text.lines().collect();
    if kind == "beamng" {
        // "   123.45678|E|GELua.x| text": warnings and errors of the last `seconds` (by the log's own clock)
        let stamp = |l: &str| -> Option<(f64, char)> {
            let mut f = l.splitn(3, '|');
            let t = f.next()?.trim().parse::<f64>().ok()?;
            let lv = f.next()?.trim().chars().next()?;
            Some((t, lv))
        };
        let last = all.iter().rev().find_map(|l| stamp(l)).map(|s| s.0).unwrap_or(0.0);
        let mut out: Vec<String> = Vec::new();
        let mut keep = false;
        for l in &all {
            match stamp(l) {
                Some((t, lv)) => {
                    keep = t >= last - seconds && matches!(lv, 'W' | 'E');
                    if keep {
                        out.push(cut(l));
                    }
                }
                None if keep => out.push(cut(l)),
                None => {}
            }
        }
        let s = out.len().saturating_sub(lines);
        return out[s..].join("\n");
    }
    let s = all.len().saturating_sub(lines);
    let mut out: Vec<String> = Vec::new();
    let hitches: Vec<String> = all[..s]
        .iter()
        .filter(|l| l.contains("perf hitch") || l.contains("roads unit") || l.contains("perf drop"))
        .rev()
        .take(30)
        .map(|l| cut(l))
        .collect();
    if !hitches.is_empty() {
        out.push("-- earlier perf lines --".into());
        out.extend(hitches.into_iter().rev());
        out.push(format!("-- the last {} lines --", all.len() - s));
    }
    out.extend(all[s..].iter().map(|l| cut(l)));
    out.join("\n")
}

fn gzip(b: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::best());
    let _ = e.write_all(b);
    e.finish().unwrap_or_default()
}

/// {name: text} scrubbed, gzipped under max (each round drops the older half of the longest log's lines).
pub fn logs_pack(json: &str, names: &[Vec<char>], coarse: bool, max: usize) -> Result<Vec<u8>, String> {
    let v: Value = serde_json::from_str(json).map_err(|e| format!("logs: {e}"))?;
    let Value::Object(o) = v else { return Err("logs: not an object".into()) };
    let mut logs: Vec<(String, Vec<String>)> = o
        .iter()
        .map(|(k, x)| (k.clone(), scrub(x.as_str().unwrap_or(""), names, coarse).lines().map(String::from).collect()))
        .collect();
    loop {
        let m: Map<String, Value> = logs.iter().map(|(k, l)| (k.clone(), Value::String(l.join("\n")))).collect();
        let gz = gzip(&serde_json::to_vec(&Value::Object(m)).unwrap_or_default());
        if gz.len() <= max {
            return Ok(gz);
        }
        let longest = logs.iter_mut().max_by_key(|(_, l)| l.iter().map(|x| x.len()).sum::<usize>()).unwrap();
        if longest.1.len() <= 1 {
            return Err("logs: cannot fit".into());
        }
        let half = longest.1.len() / 2;
        longest.1.drain(..half);
    }
}

fn b64(data: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut s = String::with_capacity(data.len() / 3 * 4 + 4);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            s.push(if i <= c.len() { A[(n >> (18 - 6 * i) & 63) as usize] as char } else { '=' });
        }
    }
    s
}

pub struct Parts<'a> {
    pub install: &'a str,
    pub v: &'a str,
    pub report: &'a str,
    pub hardware: Option<&'a str>,
    pub server: Option<&'a str>,
    pub screenshot: Option<&'a [u8]>,
    pub logs: Option<&'a [u8]>,
}

/// The upload body (see the module's notes): (body, notes on what was shortened or left out).
pub fn body(p: &Parts, names: &[Vec<char>], coarse: bool) -> Result<(Vec<u8>, Vec<String>), String> {
    let mut notes = Vec::new();
    let mut part = |name: &str, text: &str| -> Result<Value, String> {
        let v: Value = serde_json::from_str(text).map_err(|e| format!("{name}: {e}"))?;
        if !v.is_object() {
            return Err(format!("{name}: not an object"));
        }
        let v = shrink(scrub_value(&v, names, coarse), MAX_JSON);
        if let Some(d) = v.get("_dropped") {
            notes.push(format!("{name}: left out (too big): {d}"));
        }
        Ok(v)
    };
    let mut b = Map::new();
    b.insert("install".into(), Value::from(p.install));
    b.insert("v".into(), Value::from(p.v));
    b.insert("report".into(), part("report", p.report)?);
    if let Some(h) = p.hardware {
        b.insert("hardware".into(), part("hardware", h)?);
    }
    if let Some(s) = p.server {
        b.insert("server".into(), part("server", s)?);
    }
    let mut total: usize = b.values().map(|v| serde_json::to_vec(v).map(|x| x.len()).unwrap_or(0)).sum();
    let mut sent = Map::new();
    sent.insert("exactLocation".into(), Value::Bool(!coarse));
    if let Some(l) = p.logs {
        if l.len() <= MAX_LOGS && total + l.len() <= MAX_TOTAL {
            b.insert("logs".into(), Value::String(b64(l)));
            total += l.len();
            sent.insert("logs".into(), Value::Bool(true));
        } else {
            notes.push(format!("logs: left out ({} bytes)", l.len()));
        }
    }
    if let Some(s) = p.screenshot {
        if s.len() < 4 || s[..3] != [0xFF, 0xD8, 0xFF] {
            notes.push("screenshot: left out (not a JPEG)".into());
        } else if s.len() > MAX_SHOT || total + s.len() > MAX_TOTAL {
            notes.push(format!("screenshot: left out ({} bytes)", s.len()));
        } else {
            b.insert("screenshot".into(), Value::String(b64(s)));
            sent.insert("screenshot".into(), Value::Bool(true));
        }
    }
    b.insert("sent".into(), Value::Object(sent));
    Ok((serde_json::to_vec(&Value::Object(b)).map_err(|e| e.to_string())?, notes))
}

/// The gzip at b decompressed (at most max bytes; the tests).
#[cfg(test)]
pub fn gunzip(b: &[u8], max: u64) -> Result<String, String> {
    let mut s = String::new();
    use std::io::Read;
    flate2::read::MultiGzDecoder::new(b).take(max).read_to_string(&mut s).map_err(|e| e.to_string())?;
    Ok(s)
}

#[pyfunction]
#[pyo3(signature = (text, names, coarse = false))]
pub fn report_scrub(py: Python<'_>, text: &str, names: Vec<String>, coarse: bool) -> String {
    py.detach(|| scrub(text, &clean_names(&names), coarse))
}

#[pyfunction]
#[pyo3(signature = (text, names, coarse = false))]
pub fn report_scrub_json(py: Python<'_>, text: &str, names: Vec<String>, coarse: bool) -> PyResult<String> {
    py.detach(|| {
        let v: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
        serde_json::to_string_pretty(&scrub_value(&v, &clean_names(&names), coarse)).map_err(|e| e.to_string())
    })
    .map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (text, kind, lines = 300, seconds = 600.0))]
pub fn report_log_tail(py: Python<'_>, text: &str, kind: &str, lines: usize, seconds: f64) -> String {
    py.detach(|| log_tail(text, kind, lines, seconds))
}

#[pyfunction]
#[pyo3(signature = (json, names, coarse = false, max = MAX_LOGS))]
pub fn report_logs_pack<'py>(py: Python<'py>, json: &str, names: Vec<String>, coarse: bool, max: usize)
                             -> PyResult<Bound<'py, PyBytes>> {
    let gz = py.detach(|| logs_pack(json, &clean_names(&names), coarse, max)).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &gz))
}

#[pyfunction]
#[pyo3(signature = (install, v, report, hardware = None, server = None, screenshot = None, logs = None, names = vec![],
                    coarse = false))]
#[allow(clippy::too_many_arguments)]
pub fn report_body<'py>(py: Python<'py>, install: &str, v: &str, report: &str, hardware: Option<&str>,
                        server: Option<&str>, screenshot: Option<&[u8]>, logs: Option<&[u8]>, names: Vec<String>,
                        coarse: bool) -> PyResult<(Bound<'py, PyBytes>, Vec<String>)> {
    let p = Parts { install, v, report, hardware, server, screenshot, logs };
    let (b, notes) = py.detach(|| body(&p, &clean_names(&names), coarse)).map_err(PyValueError::new_err)?;
    Ok((PyBytes::new(py, &b), notes))
}

/// The features near (lat, lon): rings [(feature, start, end)] of lonlat (start..end, a closed ring a polygon); the
/// nearest distance in metres of each feature within max_m (0 inside a polygon), nearest first, at most limit.
pub fn nearby(lonlat: &[[f64; 2]], rings: &[(usize, usize, usize)], lat: f64, lon: f64, max_m: f64, limit: usize)
              -> Vec<(usize, f64)> {
    let mx = 111_320.0 * lat.to_radians().dcos();
    let my = 110_540.0;
    let xy = |i: usize| ((lonlat[i][0] - lon) * mx, (lonlat[i][1] - lat) * my);
    let mut best: std::collections::HashMap<usize, f64> = std::collections::HashMap::new();
    for &(f, a, b) in rings {
        if b > lonlat.len() || a >= b {
            continue;
        }
        let mut d = f64::INFINITY;
        if b - a == 1 {
            let (x, y) = xy(a);
            d = x.dhypot(y);
        }
        let mut inside = false;
        for i in a..b - 1 {
            let (px, py) = xy(i);
            let (qx, qy) = xy(i + 1);
            let (dx, dy) = (qx - px, qy - py);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (-(px * dx + py * dy) / l2).clamp(0.0, 1.0);
            d = d.min((px + dx * t).dhypot(py + dy * t));
            if (py > 0.0) != (qy > 0.0) && 0.0 < px + (0.0 - py) * dx / (dy + if dy == 0.0 { 1e-12 } else { 0.0 }) {
                inside = !inside;
            }
        }
        let closed = b - a >= 4 && lonlat[a] == lonlat[b - 1];
        if closed && inside {
            d = 0.0;
        }
        if d <= max_m {
            let e = best.entry(f).or_insert(f64::INFINITY);
            *e = e.min(d);
        }
    }
    let mut out: Vec<(usize, f64)> = best.into_iter().collect();
    out.sort_by(|a, b| a.1.total_cmp(&b.1).then(a.0.cmp(&b.0)));
    out.truncate(limit);
    out
}

/// report_nearby(lonlat (N x 2), rings [(feature, start, end)], lat, lon, max_m=30, limit=12) -> [(feature, metres)].
#[pyfunction]
#[pyo3(signature = (lonlat, rings, lat, lon, max_m = 30.0, limit = 12))]
pub fn report_nearby(py: Python<'_>, lonlat: numpy::PyReadonlyArray2<'_, f64>, rings: Vec<(usize, usize, usize)>,
                     lat: f64, lon: f64, max_m: f64, limit: usize) -> PyResult<Vec<(usize, f64)>> {
    let a = lonlat.as_array();
    if a.ncols() != 2 {
        return Err(PyValueError::new_err("lonlat: N x 2"));
    }
    let pts: Vec<[f64; 2]> = a.rows().into_iter().map(|r| [r[0], r[1]]).collect();
    Ok(py.detach(|| nearby(&pts, &rings, lat, lon, max_m, limit)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(t: &str) -> String {
        scrub(t, &clean_names(&["tecnagamer".into(), "Doeri".into(), "deck".into()]), false)
    }

    #[test]
    fn home_folders_and_names() {
        assert_eq!(s(r"C:\Users\Doeri\AppData\Local\BeamNG"), r"<home>\AppData\Local\BeamNG");
        assert_eq!(s(r#"{"p": "C:\\Users\\Someone Else\\x"}"#), r#"{"p": "<home>\\x"}"#);
        assert_eq!(s(r"C:\Users\bob and then text"), "<home> and then text");
        assert_eq!(s("c:/users/bob/Documents"), "<home>/Documents");
        assert_eq!(s("/home/tecnagamer/.local/share/BeamNG"), "<home>/.local/share/BeamNG");
        assert_eq!(s("/var/home/alice/x and /Users/carol/y"), "<home>/x and <home>/y");
        assert_eq!(s("/run/media/deck/sdcard/steamapps"), "<home>/sdcard/steamapps");
        assert_eq!(s("/mnt/c/Users/dave/z"), "<home>/z");
        assert_eq!(s(r"D:\Documents and Settings\eve\q"), r"<home>\q");
        assert_eq!(s("/usr/home/x"), "/usr/home/x", "only a home folder at the path's start");
        assert_eq!(s("logged in as tecnagamer, TecnaGamer's car"), "logged in as <user>, <user>'s car");
        assert_eq!(s("the bridge deck height"), "the bridge deck height", "generic names are kept");
        assert_eq!(s("tecnagamers"), "tecnagamers", "whole words only");
        assert_eq!(s("mail bob.smith+x@example.co.uk now"), "mail <email> now");
    }

    #[test]
    fn addresses() {
        assert_eq!(s("peer 10.0.0.5:30815 and 203.0.113.9."), "peer <ip>:30815 and <ip>.");
        assert_eq!(s("game 0.40.4.0, build 0.38.3.0.12345, local 127.0.0.1"), "game 0.40.4.0, build 0.38.3.0.12345, local 127.0.0.1");
        assert_eq!(s("v1.9.1.2 and 1.2.3.4.5 and 256.1.1.1"), "v1.9.1.2 and 1.2.3.4.5 and 256.1.1.1");
        assert_eq!(s("v6 2001:db8::5 and fe80::1%3 at 12:34:56"), "v6 <ip> and <ip>%3 at 12:34:56");
        assert_eq!(s("Foo::Bar::baz() 1:2"), "Foo::Bar::baz() 1:2");
    }

    #[test]
    fn coarse_rounds_coordinates() {
        let c = |t: &str| scrub(t, &[], true);
        assert_eq!(c("at 40.712776, -74.005974 (z 12.5, x 1234.5678)"), "at 40.71, -74.01 (z 12.5, x 1234.5678)");
        assert_eq!(c("v1.9.123 and 1e-5 and 3.14159e2"), "v1.9.123 and 1e-5 and 3.14159e2");
        assert_eq!(scrub("40.712776", &[], false), "40.712776");
        assert_eq!(c("tile g17_38597_49262 and g14_1_2, bg17_1_1"), "tile g14_4824_6157 and g14_1_2, bg17_1_1");
        assert_eq!(scrub("g17_38597_49262", &[], false), "g17_38597_49262");
        let v: Value = serde_json::from_str(r#"{"lat": 40.712776, "player": {"lon": -74.005974, "camLat": 1.23456,
            "x": 33.123456}, "spot": {"cell": "a"}, "pick": {"name": "road_123"}, "latLon": [40.7127, -74.0059],
            "where": "Main St", "note": "near 40.712776, -74.005974"}"#).unwrap();
        let o = scrub_value(&v, &[], true);
        assert_eq!(o["lat"], 40.71);
        assert_eq!(o["player"]["lon"], -74.01);
        assert_eq!(o["player"]["camLat"], 1.23);
        assert_eq!(o["player"]["x"], 33.123456, "not a lat / lon key: kept");
        assert_eq!(o["latLon"], serde_json::json!([40.71, -74.01]));
        assert!(o.get("spot").is_none() && o.get("pick").is_none() && o.get("where").is_none());
        assert_eq!(o["note"], "near 40.71, -74.01");
        let exact = scrub_value(&v, &[], false);
        assert_eq!(exact["lat"], 40.712776);
        assert!(exact.get("spot").is_some());
    }

    #[test]
    fn json_parts_shrink_under_the_cap() {
        let v = serde_json::json!({"kind": "map", "title": "t", "big": "x".repeat(400_000), "small": 1});
        let o = shrink(v, MAX_JSON);
        assert!(serde_json::to_vec(&o).unwrap().len() <= MAX_JSON);
        assert_eq!(o["_dropped"], serde_json::json!(["big"]));
        assert_eq!(o["small"], 1);
        let v = serde_json::json!({"note": "y".repeat(600_000)});
        assert!(serde_json::to_vec(&shrink(v, MAX_JSON)).unwrap().len() <= MAX_JSON);
    }

    #[test]
    fn nearby_features() {
        // a road 10 m north of the point (east-west), a building square around it, a far road
        let d = 10.0 / 110_540.0;
        let e = 20.0 / (111_320.0 * 40f64.to_radians().dcos());
        let pts = vec![[-74.0 - e, 40.0 + d], [-74.0 + e, 40.0 + d],
                       [-74.0 - e, 40.0 - d / 2.0], [-74.0 + e, 40.0 - d / 2.0], [-74.0 + e, 40.0 + d / 2.0],
                       [-74.0 - e, 40.0 + d / 2.0], [-74.0 - e, 40.0 - d / 2.0],
                       [-73.99, 40.0], [-73.98, 40.0]];
        let rings = vec![(0, 0, 2), (1, 2, 7), (2, 7, 9)];
        let n = nearby(&pts, &rings, 40.0, -74.0, 30.0, 10);
        assert_eq!(n.len(), 2);
        assert_eq!(n[0], (1, 0.0), "inside the building");
        assert_eq!(n[1].0, 0);
        assert!((n[1].1 - 10.0).abs() < 0.01, "{}", n[1].1);
        assert!(nearby(&pts, &[(0, 5, 99)], 40.0, -74.0, 30.0, 10).is_empty(), "a bad range is skipped");
    }

    #[test]
    fn log_tails() {
        let log = "  1.000|I|a|start\n 300.0|W|GELua|old warning\n 950.5|I|b|info\n 951.0|E|c|boom /home/tecnagamer/x\n\
                   trace line\n 960.0|W|d|late\n";
        let t = log_tail(log, "beamng", 100, 600.0);
        assert_eq!(t, " 951.0|E|c|boom /home/tecnagamer/x\ntrace line\n 960.0|W|d|late");
        assert_eq!(log_tail(log, "beamng", 1, 600.0), " 960.0|W|d|late");
        let srv: String = (0..1000).map(|i| if i == 10 { "game: perf hitch 300 ms\n".into() } else { format!("line {i}\n") }).collect();
        let t = log_tail(&srv, "server", 300, 0.0);
        assert!(t.starts_with("-- earlier perf lines --\ngame: perf hitch 300 ms\n-- the last 300 lines --\nline 700"), "{}", &t[..120]);
        assert!(t.ends_with("line 999"));
    }

    #[test]
    fn logs_pack_scrubs_and_fits() {
        let names = clean_names(&["tecnagamer".into()]);
        let big: String = (0..200_000).map(|i| format!("{i} /home/tecnagamer/x {:x}\n", i * 7919)).collect();
        let json = serde_json::to_string(&serde_json::json!({"server.log": big, "beamng.log": "e at 10.0.0.5"})).unwrap();
        let gz = logs_pack(&json, &names, false, 100_000).unwrap();
        assert!(gz.len() <= 100_000);
        let v: Value = serde_json::from_str(&gunzip(&gz, 1 << 26).unwrap()).unwrap();
        assert!(!v["server.log"].as_str().unwrap().contains("tecnagamer"));
        assert!(v["server.log"].as_str().unwrap().ends_with(&format!("199999 <home>/x {:x}", 199_999 * 7919)), "the newest kept");
        assert_eq!(v["beamng.log"], "e at <ip>");
    }

    #[test]
    fn upload_body_caps_and_scrubs() {
        let names = clean_names(&["tecnagamer".into()]);
        let jpg = [&[0xFF, 0xD8, 0xFF, 0xE0][..], &vec![1u8; 10_000][..]].concat();
        let p = Parts { install: "ab", v: "1.9.1", report: r#"{"kind": "map", "lat": 40.712776, "folder": "/home/tecnagamer/r"}"#,
                        hardware: Some(r#"{"cpu": "x", "big": "z"}"#), server: None, screenshot: Some(&jpg), logs: None };
        let (b, notes) = body(&p, &names, true).unwrap();
        assert!(notes.is_empty(), "{notes:?}");
        let v: Value = serde_json::from_slice(&b).unwrap();
        assert_eq!(v["report"]["lat"], 40.71);
        assert_eq!(v["report"]["folder"], "<home>/r");
        assert_eq!(v["sent"], serde_json::json!({"exactLocation": false, "screenshot": true}));
        assert_eq!(v["screenshot"].as_str().unwrap().len(), (jpg.len() + 2) / 3 * 4);
        // a screenshot over the cap and a non-JPEG are left out (with a note), the report still goes
        let huge = [&[0xFF, 0xD8, 0xFF][..], &vec![0u8; MAX_SHOT + 1][..]].concat();
        let (b, notes) = body(&Parts { screenshot: Some(&huge), ..p }, &names, false).unwrap();
        assert!(b.len() < 10_000 && notes[0].starts_with("screenshot: left out"));
        let (_, notes) = body(&Parts { screenshot: Some(b"\x89PNG...."), ..p }, &names, false).unwrap();
        assert_eq!(notes, ["screenshot: left out (not a JPEG)"]);
        // a huge JSON part shrinks under the collector's cap
        let r = serde_json::json!({"kind": "perf", "track": "t".repeat(900_000)}).to_string();
        let (b, notes) = body(&Parts { report: &r, screenshot: None, ..p }, &names, false).unwrap();
        assert!(b.len() < MAX_JSON + 1000 && notes[0].contains("track"));
        assert!(body(&Parts { report: "[1]", ..p }, &names, false).is_err());
    }
}
