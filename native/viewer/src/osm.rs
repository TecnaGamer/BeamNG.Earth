//! The OSM ways of a map-data cell, read straight from earthd's uncompressed mirror (read only):
//! `<cache>/mapdata-mirror/v<N>/overture/<release>/roads.<lv>/<z>/<x>/<y>/{ok, pack.bin}`
//! (server/mapdata.py `_pack` / `_unpack`: the `ok` marker's line 2 is `#pack1`, line 3 a JSON
//! header `[[name, numpy dtype, shape, offset], ...]`; each array is raw little-endian at its
//! offset in pack.bin). Feature geometry as server/mapgeom.py: feature i has parts
//! part_off[i]..part_off[i+1], part p rings ring_off[p]..ring_off[p+1], ring r coordinates
//! coord_off[r]..coord_off[r+1] of `lonlat` (lon first). `attr.*` columns are one value per
//! feature ("" / NaN = missing); `rule.<name>.{feature,value,start,end}` are linear-referenced
//! rules (fractions of the feature's length). The OSM way is `attr.src_record` (`w<id>@<version>`)
//! where `attr.src_dataset` is OpenStreetMap.
//!
//! A cell not in the mirror is reported as missing: earthd builds it when it plans roads there
//! (the viewer never fetches map data itself).

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::App;

pub const LINE: u8 = 2;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Kind {
    F64,
    F32,
    I64,
    I32,
    U8,
    Bool,
    /// numpy unicode: UTF-32LE, this many characters, NUL padded
    Str(usize),
    Other(usize),
}

impl Kind {
    pub fn parse(descr: &str) -> Option<Kind> {
        let (order, rest) = descr.split_at(1);
        if !matches!(order, "<" | "|" | "=") {
            return None; // (big-endian: never written by numpy on these machines)
        }
        let (k, n) = rest.split_at(1);
        let n: usize = n.parse().ok()?;
        Some(match (k, n) {
            ("f", 8) => Kind::F64,
            ("f", 4) => Kind::F32,
            ("i", 8) => Kind::I64,
            ("i", 4) => Kind::I32,
            ("u", 1) => Kind::U8,
            ("b", 1) => Kind::Bool,
            ("U", n) => Kind::Str(n),
            (_, n) => Kind::Other(n),
        })
    }
    fn size(self) -> usize {
        match self {
            Kind::F64 | Kind::I64 => 8,
            Kind::F32 | Kind::I32 => 4,
            Kind::U8 | Kind::Bool => 1,
            Kind::Str(n) => 4 * n,
            Kind::Other(n) => n,
        }
    }
}

pub struct Array {
    pub kind: Kind,
    pub shape: Vec<usize>,
    pub off: usize,
}

impl Array {
    pub fn count(&self) -> usize {
        self.shape.iter().product()
    }
}

/// One mirrored layer cell: its header and pack.bin's bytes.
pub struct Pack {
    pub arrays: BTreeMap<String, Array>,
    pub data: Vec<u8>,
}

impl Pack {
    pub fn read(dir: &Path) -> Result<Pack, String> {
        let ok = std::fs::read_to_string(dir.join("ok")).map_err(|e| format!("ok: {e}"))?;
        let mut lines = ok.lines();
        let _sizes = lines.next();
        if lines.next().map(str::trim) != Some("#pack1") {
            return Err("not a #pack1 cell".into());
        }
        let header: Value = serde_json::from_str(lines.next().ok_or("no header")?).map_err(|e| format!("header: {e}"))?;
        let data = std::fs::read(dir.join("pack.bin")).map_err(|e| format!("pack.bin: {e}"))?;
        let mut arrays = BTreeMap::new();
        for h in header.as_array().ok_or("header is not a list")? {
            let h = h.as_array().ok_or("header entry is not a list")?;
            let name = h.first().and_then(Value::as_str).ok_or("no name")?.to_string();
            let kind = h.get(1).and_then(Value::as_str).and_then(Kind::parse);
            let Some(kind) = kind else { continue }; // (a structured dtype: not used here)
            let shape: Vec<usize> = h
                .get(2)
                .and_then(Value::as_array)
                .ok_or("no shape")?
                .iter()
                .map(|v| v.as_u64().unwrap_or(0) as usize)
                .collect();
            let off = h.get(3).and_then(Value::as_u64).ok_or("no offset")? as usize;
            let a = Array { kind, shape, off };
            if a.off + a.count() * kind.size() > data.len() {
                return Err(format!("{name} past the end of pack.bin"));
            }
            arrays.insert(name, a);
        }
        Ok(Pack { arrays, data })
    }

    /// A layer cell from earthd's compressed cache instead (`<cache>/mapdata/.../<y>.npz`: numpy's
    /// savez_compressed, a zip of `.npy` files). earthd reads it when the mirror has no valid copy:
    /// on Windows a mirror folder can be left without its `ok` marker (its old pack.bin was memory-mapped
    /// and could not be removed), and earthd then reads the .npz on every load.
    pub fn read_npz(path: &Path) -> Result<Pack, String> {
        let zip = std::fs::read(path).map_err(|e| format!("npz: {e}"))?;
        let mut arrays = BTreeMap::new();
        let mut data = Vec::new();
        for (name, raw) in zip_entries(&zip)? {
            let Some(name) = name.strip_suffix(".npy") else { continue };
            let (kind, shape, body) = match parse_npy(&raw) {
                Ok(Some(v)) => v,
                Ok(None) => continue, // (an object or structured array: not used here)
                Err(e) => return Err(format!("{name}: {e}")),
            };
            let a = Array { kind, shape, off: data.len() };
            if body.len() < a.count() * kind.size() {
                return Err(format!("{name}: short data"));
            }
            data.extend_from_slice(&body[..a.count() * kind.size()]);
            arrays.insert(name.to_string(), a);
        }
        Ok(Pack { arrays, data })
    }

    fn bytes(&self, a: &Array) -> &[u8] {
        &self.data[a.off..a.off + a.count() * a.kind.size()]
    }

    pub fn f64s(&self, name: &str) -> Option<Vec<f64>> {
        let a = self.arrays.get(name)?;
        let b = self.bytes(a);
        Some(match a.kind {
            Kind::F64 => b.chunks_exact(8).map(|c| f64::from_le_bytes(c.try_into().unwrap())).collect(),
            Kind::F32 => b.chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap()) as f64).collect(),
            Kind::I64 => b.chunks_exact(8).map(|c| i64::from_le_bytes(c.try_into().unwrap()) as f64).collect(),
            Kind::I32 => b.chunks_exact(4).map(|c| i32::from_le_bytes(c.try_into().unwrap()) as f64).collect(),
            Kind::U8 | Kind::Bool => b.iter().map(|&v| v as f64).collect(),
            _ => return None,
        })
    }

    pub fn i64s(&self, name: &str) -> Option<Vec<i64>> {
        let a = self.arrays.get(name)?;
        let b = self.bytes(a);
        Some(match a.kind {
            Kind::I64 => b.chunks_exact(8).map(|c| i64::from_le_bytes(c.try_into().unwrap())).collect(),
            Kind::I32 => b.chunks_exact(4).map(|c| i32::from_le_bytes(c.try_into().unwrap()) as i64).collect(),
            Kind::U8 | Kind::Bool => b.iter().map(|&v| v as i64).collect(),
            _ => return None,
        })
    }

    pub fn strs(&self, name: &str) -> Option<Vec<String>> {
        let a = self.arrays.get(name)?;
        let Kind::Str(n) = a.kind else { return None };
        let b = self.bytes(a);
        if n == 0 {
            return Some(vec![String::new(); a.count()]);
        }
        Some(
            b.chunks_exact(4 * n)
                .map(|c| {
                    c.chunks_exact(4)
                        .map(|u| u32::from_le_bytes(u.try_into().unwrap()))
                        .take_while(|&u| u != 0)
                        .filter_map(char::from_u32)
                        .collect()
                })
                .collect(),
        )
    }
}

fn le16(b: &[u8], i: usize) -> Option<usize> {
    b.get(i..i + 2).map(|s| u16::from_le_bytes([s[0], s[1]]) as usize)
}
fn le32(b: &[u8], i: usize) -> Option<usize> {
    b.get(i..i + 4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]) as usize)
}

/// The files of a zip (stored or deflated; no zip64, no encryption), from its central directory.
fn zip_entries(z: &[u8]) -> Result<Vec<(String, Vec<u8>)>, String> {
    use std::io::Read;
    let eocd = (0..z.len().saturating_sub(21))
        .rev()
        .take(65557)
        .find(|&i| z[i..].starts_with(b"PK\x05\x06"))
        .ok_or("npz: no zip end record")?;
    let n = le16(z, eocd + 10).ok_or("npz: short")?;
    let mut at = le32(z, eocd + 16).ok_or("npz: short")?;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        if !z.get(at..).is_some_and(|s| s.starts_with(b"PK\x01\x02")) {
            return Err("npz: bad central directory".into());
        }
        let method = le16(z, at + 10).ok_or("npz: short")?;
        let csize = le32(z, at + 20).ok_or("npz: short")?;
        let usize_ = le32(z, at + 24).ok_or("npz: short")?;
        let (nl, xl, cl) = (le16(z, at + 28).unwrap_or(0), le16(z, at + 30).unwrap_or(0), le16(z, at + 32).unwrap_or(0));
        let local = le32(z, at + 42).ok_or("npz: short")?;
        let name = String::from_utf8_lossy(z.get(at + 46..at + 46 + nl).ok_or("npz: short")?).into_owned();
        at += 46 + nl + xl + cl;
        if csize == 0xFFFF_FFFF || usize_ == 0xFFFF_FFFF {
            return Err("npz: zip64 not supported".into());
        }
        let start = local + 30 + le16(z, local + 26).ok_or("npz: short")? + le16(z, local + 28).ok_or("npz: short")?;
        let comp = z.get(start..start + csize).ok_or("npz: entry past the end")?;
        let raw = match method {
            0 => comp.to_vec(),
            8 => {
                let mut v = Vec::with_capacity(usize_);
                flate2::read::DeflateDecoder::new(comp).read_to_end(&mut v).map_err(|e| format!("npz {name}: {e}"))?;
                v
            }
            m => return Err(format!("npz {name}: compression method {m}")),
        };
        out.push((name, raw));
    }
    Ok(out)
}

/// A `.npy` file: (dtype, shape, data), None for a dtype not read here.
fn parse_npy(b: &[u8]) -> Result<Option<(Kind, Vec<usize>, &[u8])>, String> {
    if !b.starts_with(b"\x93NUMPY") || b.len() < 10 {
        return Err("not a .npy".into());
    }
    let (hlen, hstart) = match b[6] {
        1 => (le16(b, 8).unwrap(), 10),
        _ => (le32(b, 8).ok_or("short")?, 12),
    };
    let h = std::str::from_utf8(b.get(hstart..hstart + hlen).ok_or("short header")?).map_err(|e| e.to_string())?;
    let field = |key: &str| -> Option<&str> {
        let i = h.find(&format!("'{key}':"))? + key.len() + 3;
        Some(h[i..].trim_start())
    };
    let descr = field("descr").ok_or("no descr")?;
    if !descr.starts_with('\'') {
        return Ok(None); // (a structured dtype)
    }
    let descr = &descr[1..descr[1..].find('\'').ok_or("bad descr")? + 1];
    if field("fortran_order").is_some_and(|f| f.starts_with("True")) {
        return Err("fortran order".into());
    }
    let shape = field("shape").ok_or("no shape")?;
    let shape = &shape[1..shape.find(')').ok_or("bad shape")?];
    let shape: Vec<usize> = shape.split(',').map(str::trim).filter(|s| !s.is_empty()).filter_map(|s| s.parse().ok()).collect();
    match Kind::parse(descr) {
        Some(Kind::Other(_)) | None => Ok(None),
        Some(k) => Ok(Some((k, shape, &b[hstart + hlen..]))),
    }
}

/// The value of an attribute column at feature i as JSON (None = missing).
fn attr_value(p: &Pack, name: &str, i: usize, cache: &mut BTreeMap<String, Option<Col>>) -> Option<Value> {
    let col = cache.entry(name.to_string()).or_insert_with(|| {
        let a = p.arrays.get(name)?;
        match a.kind {
            Kind::Str(_) => p.strs(name).map(Col::S),
            Kind::Bool => p.i64s(name).map(|v| Col::B(v.into_iter().map(|x| x != 0).collect())),
            Kind::Other(_) => None,
            _ => p.f64s(name).map(Col::N),
        }
    });
    match col.as_ref()? {
        Col::S(v) => v.get(i).filter(|s| !s.is_empty()).map(|s| Value::String(s.clone())),
        Col::N(v) => v.get(i).filter(|x| x.is_finite()).map(|&x| json!(x)),
        Col::B(v) => v.get(i).map(|&b| Value::Bool(b)),
    }
}

enum Col {
    S(Vec<String>),
    N(Vec<f64>),
    B(Vec<bool>),
}

/// Rule tables present in the cell: rule name -> (feature, value as JSON, start, end) rows.
fn rules(p: &Pack) -> BTreeMap<String, Vec<(usize, Value, f64, f64)>> {
    let mut out = BTreeMap::new();
    let names: HashSet<String> = p
        .arrays
        .keys()
        .filter_map(|k| k.strip_prefix("rule.").and_then(|r| r.rsplit_once('.')).map(|(n, _)| n.to_string()))
        .collect();
    for n in names {
        let (Some(feat), Some(start), Some(end)) = (
            p.i64s(&format!("rule.{n}.feature")),
            p.f64s(&format!("rule.{n}.start")),
            p.f64s(&format!("rule.{n}.end")),
        ) else {
            continue;
        };
        let vname = format!("rule.{n}.value");
        let values: Vec<Value> = match p.arrays.get(&vname).map(|a| a.kind) {
            Some(Kind::Str(_)) => p.strs(&vname).unwrap_or_default().into_iter().map(Value::String).collect(),
            Some(_) => p.f64s(&vname).unwrap_or_default().into_iter().map(|x| json!(x)).collect(),
            None => vec![Value::Null; feat.len()],
        };
        let rows = feat
            .iter()
            .zip(values)
            .zip(start.iter().zip(end.iter()))
            .map(|((&f, v), (&s, &e))| (f as usize, v, s, e))
            .collect();
        out.insert(n, rows);
    }
    out
}

/// The point at fraction t of a polyline's length (lengths in metres from `en`, else equirectangular).
fn along(lonlat: &[[f64; 2]], en: Option<&[[f64; 2]]>, t: f64) -> [f64; 2] {
    if lonlat.len() < 2 {
        return lonlat.first().copied().unwrap_or([0.0, 0.0]);
    }
    let seg = |k: usize| -> f64 {
        match en {
            Some(e) => (e[k + 1][0] - e[k][0]).hypot(e[k + 1][1] - e[k][1]),
            None => {
                let c = lonlat[k][1].to_radians().cos();
                ((lonlat[k + 1][0] - lonlat[k][0]) * c).hypot(lonlat[k + 1][1] - lonlat[k][1])
            }
        }
    };
    let lens: Vec<f64> = (0..lonlat.len() - 1).map(seg).collect();
    let total: f64 = lens.iter().sum();
    let mut want = t.clamp(0.0, 1.0) * total;
    for (k, &l) in lens.iter().enumerate() {
        if want <= l || k == lens.len() - 1 {
            let f = if l > 0.0 { (want / l).clamp(0.0, 1.0) } else { 0.0 };
            let (a, b) = (lonlat[k], lonlat[k + 1]);
            return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
        }
        want -= l;
    }
    *lonlat.last().unwrap()
}

/// A roads cell as the page draws it: `{ways: [...], nodes: [...]}`.
pub fn cell_json(p: &Pack) -> Result<Value, String> {
    let kind = p.i64s("kind").ok_or("no kind")?;
    let part_off = p.i64s("part_off").ok_or("no part_off")?;
    let ring_off = p.i64s("ring_off").ok_or("no ring_off")?;
    let coord_off = p.i64s("coord_off").ok_or("no coord_off")?;
    let ll = p.f64s("lonlat").ok_or("no lonlat")?;
    let ll: Vec<[f64; 2]> = ll.chunks_exact(2).map(|c| [c[0], c[1]]).collect();
    let en: Option<Vec<[f64; 2]>> = p
        .f64s("en.local")
        .or_else(|| p.f64s("en"))
        .map(|v| v.chunks_exact(2).map(|c| [c[0], c[1]]).collect())
        .filter(|v: &Vec<[f64; 2]>| v.len() == ll.len());
    let ids = p.strs("ids").unwrap_or_default();
    let own = p.i64s("own");
    let n = kind.len();
    if part_off.len() != n + 1 {
        return Err("part_off does not match the features".into());
    }
    let idx = |v: &[i64], k: usize| -> Result<usize, String> {
        v.get(k).map(|&x| x as usize).ok_or_else(|| "offset out of range".to_string())
    };
    let attr_names: Vec<String> = p
        .arrays
        .iter()
        .filter(|(k, a)| k.starts_with("attr.") && a.shape.len() == 1 && a.shape[0] == n)
        .map(|(k, _)| k.clone())
        .filter(|k| k != "attr.cx" && k != "attr.cy")
        .collect();
    let rules = rules(p);
    let mut cols = BTreeMap::new();
    let mut ways = Vec::new();
    // the coordinates of each feature's first line part (for the connectors' positions)
    let mut first_line: Vec<Option<(usize, usize)>> = vec![None; n];
    for i in 0..n {
        let mut parts = Vec::new();
        for pp in idx(&part_off, i)?..idx(&part_off, i + 1)? {
            for r in idx(&ring_off, pp)?..idx(&ring_off, pp + 1)? {
                let (a, b) = (idx(&coord_off, r)?, idx(&coord_off, r + 1)?);
                if b > ll.len() || a > b {
                    return Err("coord_off out of range".into());
                }
                if first_line[i].is_none() {
                    first_line[i] = Some((a, b));
                }
                let flat: Vec<f64> = ll[a..b].iter().flat_map(|c| [c[0], c[1]]).collect();
                parts.push(flat);
            }
        }
        if kind[i] as u8 != LINE {
            continue;
        }
        let mut tags = Map::new();
        for name in &attr_names {
            if let Some(v) = attr_value(p, name, i, &mut cols) {
                tags.insert(name["attr.".len()..].to_string(), v);
            }
        }
        let mut rmap = Map::new();
        for (rn, rows) in &rules {
            if rn == "connectors" {
                continue;
            }
            let mine: Vec<Value> =
                rows.iter().filter(|r| r.0 == i).map(|(_, v, s, e)| json!([v, s, e])).collect();
            if !mine.is_empty() {
                rmap.insert(rn.clone(), Value::Array(mine));
            }
        }
        let mut w = json!({
            "id": ids.get(i).cloned().unwrap_or_default(),
            "own": own.as_ref().and_then(|o| o.get(i)).map(|&x| x != 0).unwrap_or(true),
            "tags": tags,
            "g": parts,
        });
        if !rmap.is_empty() {
            w["rules"] = Value::Object(rmap);
        }
        ways.push(w);
    }
    let mut nodes = Vec::new();
    let mut seen = HashSet::new();
    if let Some(rows) = rules.get("connectors") {
        for (f, v, s, _) in rows {
            let id = v.as_str().unwrap_or_default().to_string();
            if *f >= n || !seen.insert(id.clone()) {
                continue;
            }
            if let Some((a, b)) = first_line[*f] {
                let pt = along(&ll[a..b], en.as_ref().map(|e| &e[a..b]), *s);
                nodes.push(json!({ "id": id, "p": pt }));
            }
        }
    }
    Ok(json!({ "ways": ways, "nodes": nodes }))
}

/// Folders sorted by name, newest-looking first.
fn subdirs_desc(dir: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
        .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect())
        .unwrap_or_default();
    v.sort();
    v.reverse();
    v
}

pub enum Source {
    /// a mirror folder with its `ok` marker and pack.bin
    Mirror(PathBuf),
    /// a compressed cache file
    Npz(PathBuf),
}

impl Source {
    pub fn read(&self) -> Result<Pack, String> {
        match self {
            Source::Mirror(d) => Pack::read(d),
            Source::Npz(p) => Pack::read_npz(p),
        }
    }
}

/// The roads cell (z, x, y): from the newest mirror version, Overture release and layer version that has a valid
/// copy, else from the compressed cache (`<cache>/mapdata/...`) as earthd falls back to it.
pub fn find_cell(cache_dir: &Path, z: u32, x: u32, y: u32) -> Option<(Source, String)> {
    if let Some((d, rel)) = find_in(&cache_dir.join("mapdata-mirror"), z, x, y, |d| {
        (d.join("ok").is_file() && d.join("pack.bin").is_file()).then(|| d.to_path_buf())
    }) {
        return Some((Source::Mirror(d), rel));
    }
    find_in(&cache_dir.join("mapdata"), z, x, y, |d| {
        let f = d.with_extension("npz");
        f.is_file().then_some(f)
    })
    .map(|(p, rel)| (Source::Npz(p), rel))
}

/// `<root>/v<N>/overture/<release>/roads[.<lv>]/<z>/<x>/<y>` tried newest first; `pick` says what is there.
fn find_in(root: &Path, z: u32, x: u32, y: u32, pick: impl Fn(&Path) -> Option<PathBuf>) -> Option<(PathBuf, String)> {
    for ver in subdirs_desc(root) {
        for rel in subdirs_desc(&ver.join("overture")) {
            let mut layers: Vec<(u32, PathBuf)> = subdirs_desc(&rel)
                .into_iter()
                .filter_map(|p| {
                    let name = p.file_name()?.to_str()?.to_string();
                    if name == "roads" {
                        Some((0, p))
                    } else {
                        name.strip_prefix("roads.")?.parse().ok().map(|v| (v, p))
                    }
                })
                .collect();
            layers.sort_by(|a, b| b.0.cmp(&a.0));
            for (_, l) in layers {
                let d = l.join(z.to_string()).join(x.to_string()).join(y.to_string());
                if let Some(found) = pick(&d) {
                    let release = rel.file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                    return Some((found, release));
                }
            }
        }
    }
    None
}

#[derive(Deserialize)]
pub struct CellQuery {
    z: Option<u32>,
    x: u32,
    y: u32,
}

pub async fn handler(State(app): State<Arc<App>>, Query(q): Query<CellQuery>) -> Response {
    let z = q.z.unwrap_or(14);
    let cache = app.cfg.cache_dir.clone();
    let res = tokio::task::spawn_blocking(move || -> Result<Value, String> {
        let Some((src, release)) = find_cell(&cache, z, q.x, q.y) else {
            return Ok(json!({ "cell": [z, q.x, q.y], "missing": true }));
        };
        let pack = src.read()?;
        let mut v = cell_json(&pack)?;
        v["cell"] = json!([z, q.x, q.y]);
        v["release"] = Value::String(release);
        v["from"] = Value::String(match src { Source::Mirror(_) => "mirror", Source::Npz(_) => "npz" }.into());
        Ok(v)
    })
    .await;
    match res {
        Ok(Ok(v)) => ([(header::CONTENT_TYPE, "application/json")], v.to_string()).into_response(),
        Ok(Err(e)) => (StatusCode::INTERNAL_SERVER_ERROR, e).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dtypes() {
        assert_eq!(Kind::parse("<f8"), Some(Kind::F64));
        assert_eq!(Kind::parse("|u1"), Some(Kind::U8));
        assert_eq!(Kind::parse("|b1"), Some(Kind::Bool));
        assert_eq!(Kind::parse("<U36"), Some(Kind::Str(36)));
        assert_eq!(Kind::parse(">f8"), None);
    }

    #[test]
    fn along_halfway() {
        let ll = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]];
        let en = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]];
        assert_eq!(along(&ll, Some(&en), 0.5), [1.0, 0.0]);
        assert_eq!(along(&ll, Some(&en), 0.75), [1.0, 0.5]);
        assert_eq!(along(&ll, Some(&en), 1.0), [1.0, 1.0]);
    }
}
