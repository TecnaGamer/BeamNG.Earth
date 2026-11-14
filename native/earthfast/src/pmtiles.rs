//! Overture's own vector tiles (PMTiles v3 archives of gzip MVT, z14 = one map cell) read into the map cells'
//! columns (server/pmtiles.py: the network, disk cache and mirrors stay in Python's netfetch; the parsing is here).
//!
//! * PMTiles v3: the 127-byte header, directories (varint counts, delta tile ids, run lengths, lengths, offsets with
//!   0 = "right after the previous"), Hilbert tile ids, leaf pointers (run length 0).
//! * MVT: protobuf layers, features, keys / values, geometry commands (MoveTo / LineTo / ClosePath, zig-zag deltas).
//! * A cell's layer the way overture.Reader gives it: every feature whose geometry reaches the cell (its closed box),
//!   WHOLE: a line or polygon that crosses tile edges is rebuilt from its pieces in the neighbour
//!   tiles (each tile's piece clipped to the tile's own square, the pieces joined where one ends on an edge and the next
//!   starts there; Planetiler simplifies a feature once, before slicing it, so the pieces share their vertices). The
//!   tiles a feature still needs are asked of the caller's fetch callback, round after round. Polygons of a clipped
//!   layer (water, land use) are cut to the cell instead. Attributes from the tile's properties (nested values are JSON
//!   strings), the same columns and rule tables as the parquet path's SQL (overture.LAYERS, Reader._road_rules).
//!
//! Coordinates: world units at the tile zoom (tile x * 4096 + tile pixel), y down, as f64; out as lon/lat (Web
//! Mercator inverse). Ring orientation as the parquet cells have it: exteriors counter-clockwise in lon/lat, holes
//! clockwise.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::Read;
use std::sync::{Arc, Mutex};

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use serde_json::Value as J;

const EXT: f64 = 4096.0;
const JOIN_TOL: f64 = 2.0; // world units: a piece's end and the next piece's start (each tile rounds its own cut)
const POINT: u8 = 1;
const LINE: u8 = 2;
const POLYGON: u8 = 3;
const MPH: f64 = 1.609344;

// ------------------------------------------------------------------------------------------------ bytes
fn varint(b: &[u8], i: &mut usize) -> Result<u64, String> {
    let mut v: u64 = 0;
    let mut s = 0u32;
    loop {
        if *i >= b.len() {
            return Err("truncated varint".into());
        }
        let c = b[*i];
        *i += 1;
        if s < 64 {
            v |= ((c & 0x7f) as u64) << s;
        }
        if c < 0x80 {
            return Ok(v);
        }
        s += 7;
        if s > 70 {
            return Err("varint too long".into());
        }
    }
}

/// gzip-decoded bytes (as they are when not gzip).
pub fn gunzip(b: &[u8]) -> Result<Vec<u8>, String> {
    if b.len() >= 2 && b[0] == 0x1f && b[1] == 0x8b {
        let mut out = Vec::with_capacity(b.len() * 4);
        flate2::read::MultiGzDecoder::new(b).read_to_end(&mut out).map_err(|e| format!("gzip: {e}"))?;
        Ok(out)
    } else {
        Ok(b.to_vec())
    }
}

// ------------------------------------------------------------------------------------------------ PMTiles
/// The PMTiles tile id of (z, x, y): the tiles of lower zooms first, then the Hilbert curve's index at z.
pub fn tile_id(z: u8, x: u64, y: u64) -> u64 {
    let mut acc: u64 = 0;
    for t in 0..z {
        acc += 1u64 << (2 * t as u64);
    }
    let n: u64 = 1u64 << z;
    let (mut tx, mut ty) = (x, y);
    let mut d: u64 = 0;
    let mut s = n / 2;
    while s > 0 {
        let rx: u64 = if tx & s > 0 { 1 } else { 0 };
        let ry: u64 = if ty & s > 0 { 1 } else { 0 };
        d += s * s * ((3 * rx) ^ ry);
        if ry == 0 {
            if rx == 1 {
                tx = s.wrapping_sub(1).wrapping_sub(tx);
                ty = s.wrapping_sub(1).wrapping_sub(ty);
            }
            std::mem::swap(&mut tx, &mut ty);
        }
        s /= 2;
    }
    acc + d
}

#[derive(Clone, Copy, Debug)]
pub struct Entry {
    pub tile_id: u64,
    pub offset: u64,
    pub length: u64,
    pub run: u64,
}

pub fn parse_dir(b: &[u8]) -> Result<Vec<Entry>, String> {
    let mut i = 0usize;
    let n = varint(b, &mut i)? as usize;
    if n > b.len() {
        return Err("directory count past its bytes".into());
    }
    let mut e = vec![Entry { tile_id: 0, offset: 0, length: 0, run: 0 }; n];
    let mut last = 0u64;
    for k in 0..n {
        last += varint(b, &mut i)?;
        e[k].tile_id = last;
    }
    for k in 0..n {
        e[k].run = varint(b, &mut i)?;
    }
    for k in 0..n {
        e[k].length = varint(b, &mut i)?;
    }
    for k in 0..n {
        let v = varint(b, &mut i)?;
        e[k].offset = if v == 0 && k > 0 { e[k - 1].offset + e[k - 1].length } else { v.saturating_sub(1) };
    }
    Ok(e)
}

/// The entry for tile id t (an exact tile, a run covering it, or the leaf directory that holds it), as the spec's
/// findTile.
pub fn find(e: &[Entry], t: u64) -> Option<Entry> {
    let (mut lo, mut hi) = (0i64, e.len() as i64 - 1);
    while lo <= hi {
        let m = (lo + hi) / 2;
        let c = e[m as usize].tile_id;
        if c < t {
            lo = m + 1;
        } else if c > t {
            hi = m - 1;
        } else {
            return Some(e[m as usize]);
        }
    }
    if hi >= 0 {
        let x = e[hi as usize];
        if x.run == 0 || t - x.tile_id < x.run {
            return Some(x);
        }
    }
    None
}

fn u64le(b: &[u8], o: usize) -> u64 {
    let mut v = [0u8; 8];
    v.copy_from_slice(&b[o..o + 8]);
    u64::from_le_bytes(v)
}

#[pyfunction]
pub fn pmt_tile_id(z: u8, x: u64, y: u64) -> u64 {
    tile_id(z, x, y)
}

/// The archive header's fields (bytes: at least its first 127 bytes).
#[pyfunction]
pub fn pmt_header<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    if data.len() < 127 || &data[0..7] != b"PMTiles" {
        return Err(PyValueError::new_err("not a PMTiles archive"));
    }
    if data[7] != 3 {
        return Err(PyValueError::new_err(format!("PMTiles version {} (3 expected)", data[7])));
    }
    let d = PyDict::new(py);
    for (k, o) in [("root_offset", 8), ("root_length", 16), ("metadata_offset", 24), ("metadata_length", 32),
                   ("leaf_offset", 40), ("leaf_length", 48), ("data_offset", 56), ("data_length", 64),
                   ("addressed_tiles", 72), ("tile_entries", 80), ("tile_contents", 88)] {
        d.set_item(k, u64le(data, o))?;
    }
    for (k, o) in [("clustered", 96), ("internal_compression", 97), ("tile_compression", 98), ("tile_type", 99),
                   ("min_zoom", 100), ("max_zoom", 101)] {
        d.set_item(k, data[o])?;
    }
    Ok(d)
}

/// Tile id t in a directory (its bytes as stored: gzip or plain): None, or (offset, length, is_leaf) of the tile's
/// data (relative to the data section) or of the leaf directory to look in next (relative to the leaf section).
#[pyfunction]
pub fn pmt_find(py: Python<'_>, dir: &[u8], t: u64) -> PyResult<Option<(u64, u64, bool)>> {
    let raw = dir.to_vec();
    let got = py.detach(move || -> Result<Option<Entry>, String> {
        let b = gunzip(&raw)?;
        Ok(find(&parse_dir(&b)?, t))
    }).map_err(PyValueError::new_err)?;
    Ok(got.map(|e| (e.offset, e.length, e.run == 0)))
}

// ------------------------------------------------------------------------------------------------ MVT
#[derive(Clone, Debug)]
enum V {
    S(String),
    F(f64),
    I(i64),
    B(bool),
}

/// A tile's layer, read lazily: its values and geometries stay bytes of the tile until asked for (a neighbour
/// tile is read for the few features that cross into the cell; its other values were most of the decode).
struct Layer {
    raw: Arc<Vec<u8>>,          // the decompressed tile
    keys: Vec<String>,
    values: Vec<(usize, usize)>, // each Value message's bytes in raw
    feats: Vec<RawFeat>,
}

struct RawFeat {
    gtype: u8,
    geom: (usize, usize),       // the packed geometry commands' bytes in raw
    tags: Vec<u32>,
    id: Option<String>,         // the "id" property (GERS id)
}

impl Layer {
    fn value(&self, i: usize) -> Option<V> {
        let (s, e) = *self.values.get(i)?;
        value(&self.raw[s..e]).ok()
    }
    fn geom(&self, f: &RawFeat) -> Vec<u32> {
        packed(&self.raw[f.geom.0..f.geom.1]).unwrap_or_default()
    }
}

fn field(b: &[u8], i: &mut usize) -> Result<(u64, u8, usize, usize, u64), String> {
    // (field number, wire type, slice start, slice end, varint value)
    let k = varint(b, i)?;
    let (f, w) = (k >> 3, (k & 7) as u8);
    match w {
        0 => {
            let v = varint(b, i)?;
            Ok((f, w, 0, 0, v))
        }
        1 => {
            let s = *i;
            *i += 8;
            if *i > b.len() {
                return Err("truncated fixed64".into());
            }
            Ok((f, w, s, *i, 0))
        }
        2 => {
            let n = varint(b, i)? as usize;
            let s = *i;
            *i = i.checked_add(n).ok_or("bad length")?;
            if *i > b.len() {
                return Err("truncated field".into());
            }
            Ok((f, w, s, *i, 0))
        }
        5 => {
            let s = *i;
            *i += 4;
            if *i > b.len() {
                return Err("truncated fixed32".into());
            }
            Ok((f, w, s, *i, 0))
        }
        _ => Err(format!("wire type {w}")),
    }
}

fn packed(b: &[u8]) -> Result<Vec<u32>, String> {
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        out.push(varint(b, &mut i)? as u32);
    }
    Ok(out)
}

fn value(b: &[u8]) -> Result<V, String> {
    let mut i = 0;
    let mut out = V::S(String::new());
    while i < b.len() {
        let (f, _w, s, e, v) = field(b, &mut i)?;
        out = match f {
            1 => V::S(String::from_utf8_lossy(&b[s..e]).into_owned()),
            2 => V::F(f32::from_le_bytes([b[s], b[s + 1], b[s + 2], b[s + 3]]) as f64),
            3 => {
                let mut a = [0u8; 8];
                a.copy_from_slice(&b[s..e]);
                V::F(f64::from_le_bytes(a))
            }
            4 => V::I(v as i64),
            5 => V::I(v as i64),
            6 => V::I(((v >> 1) as i64) ^ -((v & 1) as i64)),
            7 => V::B(v != 0),
            _ => out,
        };
    }
    Ok(out)
}

/// The tile's layer `name` (None when the tile has none).
fn decode_layer(raw: &Arc<Vec<u8>>, name: &str) -> Result<Option<(Layer, f64)>, String> {
    let tile: &[u8] = raw;
    let mut i = 0;
    while i < tile.len() {
        let (f, w, s, e, _) = field(tile, &mut i)?;
        if f != 3 || w != 2 {
            continue;
        }
        let lb = &tile[s..e];
        // the name first (one pass over the layer's fields to find it)
        let mut j = 0;
        let mut lname: &[u8] = b"";
        while j < lb.len() {
            let (g, _w, s2, e2, _) = field(lb, &mut j)?;
            if g == 1 {
                lname = &lb[s2..e2];
                break;
            }
        }
        if lname != name.as_bytes() {
            continue;
        }
        let mut lay = Layer { raw: raw.clone(), keys: vec![], values: vec![], feats: vec![] };
        let mut extent = EXT;
        let mut j = 0;
        while j < lb.len() {
            let (g, w2, s2, e2, v) = field(lb, &mut j)?;
            match (g, w2) {
                (2, 2) => {
                    let fb = &lb[s2..e2];
                    let mut k = 0;
                    let mut rf = RawFeat { gtype: 0, geom: (0, 0), tags: vec![], id: None };
                    while k < fb.len() {
                        let (h, w3, s3, e3, v3) = field(fb, &mut k)?;
                        match (h, w3) {
                            (2, 2) => rf.tags = packed(&fb[s3..e3])?,
                            (3, 0) => rf.gtype = v3 as u8,
                            (4, 2) => rf.geom = (s + s2 + s3, s + s2 + e3),
                            _ => {}
                        }
                    }
                    lay.feats.push(rf);
                }
                (3, 2) => lay.keys.push(String::from_utf8_lossy(&lb[s2..e2]).into_owned()),
                (4, 2) => lay.values.push((s + s2, s + e2)),
                (5, 0) => extent = v as f64,
                _ => {}
            }
        }
        if let Some(ki) = lay.keys.iter().position(|k| k == "id") {
            let keys_n = ki as u32;
            let mut ids = Vec::with_capacity(lay.feats.len());
            for f in lay.feats.iter() {
                let mut id = None;
                let mut i = 0;
                while i + 1 < f.tags.len() {
                    if f.tags[i] == keys_n {
                        if let Some(V::S(s)) = lay.value(f.tags[i + 1] as usize) {
                            id = Some(s);
                        }
                        break;
                    }
                    i += 2;
                }
                ids.push(id);
            }
            for (f, id) in lay.feats.iter_mut().zip(ids) {
                f.id = id;
            }
        }
        return Ok(Some((lay, extent)));
    }
    Ok(None)
}

// Decompressed tiles kept per process (a cell's fetch rounds, its neighbour cells and the other layers of the same
// tiles gunzip each tile once while it is kept): by the caller's archive key (release, theme, ETag: the bytes never
// change under it) and tile.
type RawKey = (String, u8, i64, i64);
type Dec = Arc<Option<(Layer, f64)>>;
const RAW_BYTES: usize = 64 << 20;
static RAWS: Mutex<Option<(HashMap<RawKey, (Arc<Vec<u8>>, u64)>, u64, usize)>> = Mutex::new(None);

fn raw_tile(ckey: &str, z: u8, x: i64, y: i64, bytes: &[u8]) -> Result<Arc<Vec<u8>>, String> {
    let k: RawKey = (ckey.to_string(), z, x, y);
    if !ckey.is_empty() {
        let mut g = RAWS.lock().unwrap_or_else(|e| e.into_inner());
        let c = g.get_or_insert_with(|| (HashMap::new(), 0, 0));
        c.1 += 1;
        let tick = c.1;
        if let Some(e) = c.0.get_mut(&k) {
            e.1 = tick;
            return Ok(e.0.clone());
        }
    }
    let r = Arc::new(gunzip(bytes)?);
    if !ckey.is_empty() {
        let mut g = RAWS.lock().unwrap_or_else(|e| e.into_inner());
        let c = g.get_or_insert_with(|| (HashMap::new(), 0, 0));
        c.1 += 1;
        let tick = c.1;
        if c.0.insert(k, (r.clone(), tick)).is_none() {
            c.2 += r.len();
        }
        while c.2 > RAW_BYTES && c.0.len() > 1 {
            let old = c.0.iter().min_by_key(|(_, v)| v.1).map(|(k, _)| k.clone()).unwrap();
            if let Some((b, _)) = c.0.remove(&old) {
                c.2 -= b.len();
            }
        }
    }
    Ok(r)
}

/// A feature's geometry commands as rings / lines / points (tile pixels, as f64).
fn geometry(cmds: &[u32]) -> Vec<Vec<[f64; 2]>> {
    let mut out: Vec<Vec<[f64; 2]>> = vec![];
    let (mut x, mut y) = (0i64, 0i64);
    let mut i = 0;
    let zz = |v: u32| -> i64 { ((v >> 1) as i64) ^ -((v & 1) as i64) };
    while i < cmds.len() {
        let c = cmds[i];
        i += 1;
        let (id, count) = (c & 7, (c >> 3) as usize);
        match id {
            1 => {
                for _ in 0..count {
                    if i + 2 > cmds.len() {
                        return out;
                    }
                    x += zz(cmds[i]);
                    y += zz(cmds[i + 1]);
                    i += 2;
                    out.push(vec![[x as f64, y as f64]]);
                }
            }
            2 => {
                for _ in 0..count {
                    if i + 2 > cmds.len() {
                        return out;
                    }
                    x += zz(cmds[i]);
                    y += zz(cmds[i + 1]);
                    i += 2;
                    if let Some(l) = out.last_mut() {
                        l.push([x as f64, y as f64]);
                    }
                }
            }
            7 => {}
            _ => return out,
        }
    }
    out
}

// ------------------------------------------------------------------------------------------------ geometry
type P = [f64; 2];

#[derive(Clone, Copy)]
struct Box2 {
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
}

impl Box2 {
    fn on_edge(&self, p: P) -> bool {
        p[0] == self.x0 || p[0] == self.x1 || p[1] == self.y0 || p[1] == self.y1
    }
    fn inside(&self, p: P) -> bool {
        p[0] >= self.x0 && p[0] <= self.x1 && p[1] >= self.y0 && p[1] <= self.y1
    }
}

fn tile_box(tx: i64, ty: i64) -> Box2 {
    Box2 { x0: tx as f64 * EXT, y0: ty as f64 * EXT, x1: (tx + 1) as f64 * EXT, y1: (ty + 1) as f64 * EXT }
}

/// Liang-Barsky: the part [t0, t1] of a -> b inside the box, the clipped points snapped onto the edge they hit.
fn clip_seg(a: P, b: P, bx: &Box2) -> Option<(P, P, bool, bool)> {
    let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
    let p = [-dx, dx, -dy, dy];
    let q = [a[0] - bx.x0, bx.x1 - a[0], a[1] - bx.y0, bx.y1 - a[1]];
    let (mut t0, mut t1) = (0.0f64, 1.0f64);
    let (mut e0, mut e1) = (usize::MAX, usize::MAX);
    for k in 0..4 {
        if p[k] == 0.0 {
            if q[k] < 0.0 {
                return None;
            }
        } else {
            let r = q[k] / p[k];
            if p[k] < 0.0 {
                if r > t1 {
                    return None;
                }
                if r > t0 {
                    t0 = r;
                    e0 = k;
                }
            } else {
                if r < t0 {
                    return None;
                }
                if r < t1 {
                    t1 = r;
                    e1 = k;
                }
            }
        }
    }
    let snap = |mut pt: P, e: usize| -> P {
        match e {
            0 => pt[0] = bx.x0,
            1 => pt[0] = bx.x1,
            2 => pt[1] = bx.y0,
            3 => pt[1] = bx.y1,
            _ => {}
        }
        pt
    };
    let pa = if e0 == usize::MAX { a } else { snap([a[0] + t0 * dx, a[1] + t0 * dy], e0) };
    let pb = if e1 == usize::MAX { b } else { snap([a[0] + t1 * dx, a[1] + t1 * dy], e1) };
    Some((pa, pb, e0 != usize::MAX, e1 != usize::MAX))
}

fn dedup(v: &mut Vec<P>) {
    v.dedup_by(|a, b| a[0] == b[0] && a[1] == b[1]);
}

/// A polyline's pieces inside the box (closed).
fn clip_line(pts: &[P], bx: &Box2) -> Vec<Vec<P>> {
    let mut out = vec![];
    let mut cur: Vec<P> = vec![];
    if pts.len() == 1 {
        return out;
    }
    for w in pts.windows(2) {
        match clip_seg(w[0], w[1], bx) {
            Some((pa, pb, _c0, c1)) => {
                if let Some(l) = cur.last() {
                    if l[0] != pa[0] || l[1] != pa[1] {
                        out.push(std::mem::take(&mut cur));
                    }
                }
                if cur.is_empty() {
                    cur.push(pa);
                }
                cur.push(pb);
                if c1 {
                    out.push(std::mem::take(&mut cur));
                }
            }
            None => {
                if !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                }
            }
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out.into_iter()
        .map(|mut v| {
            dedup(&mut v);
            v
        })
        .filter(|v| v.len() >= 2)
        .collect()
}

/// Sutherland-Hodgman of an open ring against the box, intersections snapped onto the box edges.
fn clip_ring(ring: &[P], bx: &Box2) -> Vec<P> {
    let mut p: Vec<P> = ring.to_vec();
    for side in 0..4 {
        if p.is_empty() {
            break;
        }
        let ins = |q: P| -> bool {
            match side {
                0 => q[0] >= bx.x0,
                1 => q[0] <= bx.x1,
                2 => q[1] >= bx.y0,
                _ => q[1] <= bx.y1,
            }
        };
        let cut = |a: P, b: P| -> P {
            match side {
                0 | 1 => {
                    let v = if side == 0 { bx.x0 } else { bx.x1 };
                    let t = (v - a[0]) / (b[0] - a[0]);
                    [v, a[1] + t * (b[1] - a[1])]
                }
                _ => {
                    let v = if side == 2 { bx.y0 } else { bx.y1 };
                    let t = (v - a[1]) / (b[1] - a[1]);
                    [a[0] + t * (b[0] - a[0]), v]
                }
            }
        };
        let mut q = Vec::with_capacity(p.len() + 4);
        let n = p.len();
        for k in 0..n {
            let a = p[k];
            let b = p[(k + 1) % n];
            let (ia, ib) = (ins(a), ins(b));
            if ia {
                q.push(a);
                if !ib {
                    q.push(cut(a, b));
                }
            } else if ib {
                q.push(cut(a, b));
            }
        }
        p = q;
    }
    dedup(&mut p);
    while p.len() > 1 && p[0] == p[p.len() - 1] {
        p.pop();
    }
    p
}

fn area2(r: &[P]) -> f64 {
    let n = r.len();
    let mut s = 0.0;
    for k in 0..n {
        let a = r[k];
        let b = r[(k + 1) % n];
        s += a[0] * b[1] - b[0] * a[1];
    }
    s
}

fn point_in_ring(p: P, r: &[P]) -> bool {
    let mut inside = false;
    let n = r.len();
    let mut j = n.wrapping_sub(1);
    for i in 0..n {
        let (a, b) = (r[i], r[j]);
        if (a[1] > p[1]) != (b[1] > p[1]) && p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0] {
            inside = !inside;
        }
        j = i;
    }
    inside
}

/// An open ring (as MVT gives it, its closing point dropped).
fn open_ring(mut r: Vec<P>) -> Vec<P> {
    dedup(&mut r);
    while r.len() > 1 && r[0] == r[r.len() - 1] {
        r.pop();
    }
    r
}

/// A clipped ring's runs between the box's edges: (chains with both ends on the box, or the ring whole when it touches
/// no edge along one).
fn ring_chains(r: &[P], bx: &Box2) -> (Vec<Vec<P>>, Option<Vec<P>>) {
    let n = r.len();
    let along = |a: P, b: P| -> bool {
        (a[0] == bx.x0 && b[0] == bx.x0) || (a[0] == bx.x1 && b[0] == bx.x1) || (a[1] == bx.y0 && b[1] == bx.y0)
            || (a[1] == bx.y1 && b[1] == bx.y1)
    };
    let edge_b: Vec<bool> = (0..n).map(|k| along(r[k], r[(k + 1) % n])).collect();
    if !edge_b.iter().any(|&b| b) {
        return (vec![], Some(r.to_vec()));
    }
    if edge_b.iter().all(|&b| b) {
        return (vec![], None);
    }
    // start at a vertex whose incoming edge is along the box and outgoing one is not
    let s = (0..n).find(|&k| edge_b[(k + n - 1) % n] && !edge_b[k]).unwrap_or(0);
    let mut chains = vec![];
    let mut cur: Vec<P> = vec![];
    for step in 0..n {
        let k = (s + step) % n;
        if !edge_b[k] {
            if cur.is_empty() {
                cur.push(r[k]);
            }
            cur.push(r[(k + 1) % n]);
        } else if !cur.is_empty() {
            chains.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        chains.push(cur);
    }
    (chains, None)
}

struct Piece {
    pts: Vec<P>,
    tile: usize,
    sb: bool,
    eb: bool,
}

/// Pieces joined end to start (a boundary end to a boundary start of another tile's piece within JOIN_TOL): the
/// chains in order, each with whether it closed on itself, and the boundary ends that found no partner.
fn link(pieces: &[Piece], close: bool) -> (Vec<(Vec<P>, bool, bool)>, Vec<P>) {
    let n = pieces.len();
    let mut next = vec![usize::MAX; n];
    let mut prev = vec![usize::MAX; n];
    let mut open_ends = vec![];
    // candidate pairs, nearest first (deterministic: distance, then indices)
    let mut pairs: Vec<(f64, usize, usize)> = vec![];
    for i in 0..n {
        if !pieces[i].eb {
            continue;
        }
        let e = *pieces[i].pts.last().unwrap();
        for j in 0..n {
            if j == i || !pieces[j].sb || pieces[j].tile == pieces[i].tile {
                continue;
            }
            let s = pieces[j].pts[0];
            let d = ((e[0] - s[0]).powi(2) + (e[1] - s[1]).powi(2)).sqrt();
            if d <= JOIN_TOL {
                pairs.push((d, i, j));
            }
        }
    }
    pairs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    for (_, i, j) in pairs {
        if next[i] == usize::MAX && prev[j] == usize::MAX {
            next[i] = j;
            prev[j] = i;
        }
    }
    for i in 0..n {
        if pieces[i].eb && next[i] == usize::MAX {
            open_ends.push(*pieces[i].pts.last().unwrap());
        }
        if pieces[i].sb && prev[i] == usize::MAX {
            open_ends.push(pieces[i].pts[0]);
        }
    }
    let mut seen = vec![false; n];
    let mut out = vec![];
    let mid = |a: P, b: P| -> P { [(a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5] };
    let walk = |start: usize, seen: &mut Vec<bool>| -> (Vec<P>, bool, bool) {
        let open_start = pieces[start].sb && prev[start] == usize::MAX;
        let mut pts: Vec<P> = vec![];
        let mut k = start;
        let mut closed = false;
        loop {
            seen[k] = true;
            let p = &pieces[k].pts;
            if pts.is_empty() {
                pts.extend_from_slice(p);
            } else {
                let l = pts.len() - 1;
                pts[l] = mid(pts[l], p[0]);
                pts.extend_from_slice(&p[1..]);
            }
            let nx = next[k];
            if nx == usize::MAX {
                break;
            }
            if nx == start {
                closed = true;
                break;
            }
            if seen[nx] {
                break;
            }
            k = nx;
        }
        if closed && pts.len() > 1 {
            let l = pts.len() - 1;
            pts[0] = mid(pts[l], pts[0]);
            pts.pop();
        }
        dedup(&mut pts);
        (pts, closed, open_start)
    };
    for i in 0..n {
        if !seen[i] && prev[i] == usize::MAX {
            out.push(walk(i, &mut seen));
        }
    }
    for i in 0..n {
        if !seen[i] {
            out.push(walk(i, &mut seen));
        }
    }
    let _ = close;
    (out, open_ends)
}

// ------------------------------------------------------------------------------------------------ attributes
struct Props<'a> {
    lay: &'a Layer,
    tags: &'a [u32],
}

impl<'a> Props<'a> {
    fn get(&self, k: &str) -> Option<V> {
        let mut i = 0;
        while i + 1 < self.tags.len() {
            let (a, b) = (self.tags[i] as usize, self.tags[i + 1] as usize);
            if a < self.lay.keys.len() && self.lay.keys[a] == k {
                return self.lay.value(b);
            }
            i += 2;
        }
        None
    }
    fn s(&self, k: &str) -> Option<String> {
        match self.get(k)? {
            V::S(s) => Some(s),
            V::F(f) => Some(format!("{f}")),
            V::I(i) => Some(format!("{i}")),
            V::B(b) => Some(if b { "true".into() } else { "false".into() }),
        }
    }
    fn f(&self, k: &str) -> Option<f64> {
        match self.get(k)? {
            V::F(f) => Some(f),
            V::I(i) => Some(i as f64),
            V::B(b) => Some(if b { 1.0 } else { 0.0 }),
            V::S(s) => s.parse().ok(),
        }
    }
    fn b(&self, k: &str) -> bool {
        match self.get(k) {
            Some(V::B(b)) => b,
            Some(V::I(i)) => i != 0,
            Some(V::S(s)) => s == "true",
            _ => false,
        }
    }
    fn json(&self, k: &str) -> J {
        match self.get(k) {
            Some(V::S(s)) => serde_json::from_str(&s).unwrap_or(J::Null),
            _ => J::Null,
        }
    }
}

fn jstr(v: &J) -> Option<String> {
    match v {
        J::String(s) => Some(s.clone()),
        J::Null => None,
        other => Some(other.to_string()),
    }
}

fn jf(v: &J) -> Option<f64> {
    v.as_f64()
}

fn between(v: &J) -> Option<(f64, f64)> {
    let b = v.get("between")?.as_array()?;
    if b.len() < 2 {
        return None;
    }
    Some((b[0].as_f64().unwrap_or(0.0), b[1].as_f64().unwrap_or(1.0)))
}

/// DuckDB's CAST(DOUBLE AS VARCHAR) for the values a route's "between" holds (shortest round trip, "0.0", "1.0").
fn duck_double(v: f64) -> String {
    let s = format!("{v:?}");
    s
}

fn truthy(v: Option<&J>) -> bool {
    match v {
        None | Some(J::Null) => false,
        Some(J::Array(a)) => !a.is_empty(),
        Some(J::String(s)) => !s.is_empty(),
        Some(J::Bool(b)) => *b,
        Some(J::Object(o)) => !o.is_empty(),
        Some(_) => true,
    }
}

struct Rule {
    feat: Vec<i32>,
    text: Vec<String>,
    num: Vec<f32>,
    start: Vec<f32>,
    end: Vec<f32>,
    is_text: bool,
}

impl Rule {
    fn new(is_text: bool) -> Rule {
        Rule { feat: vec![], text: vec![], num: vec![], start: vec![], end: vec![], is_text }
    }
    fn push_t(&mut self, f: usize, v: Option<String>, b: Option<(f64, f64)>) {
        self.feat.push(f as i32);
        self.text.push(v.unwrap_or_default());
        let (s, e) = b.unwrap_or((0.0, 1.0));
        self.start.push(s as f32);
        self.end.push(e as f32);
    }
    fn push_f(&mut self, f: usize, v: Option<f64>, b: Option<(f64, f64)>) {
        self.feat.push(f as i32);
        self.num.push(v.map(|x| x as f32).unwrap_or(f32::NAN));
        let (s, e) = b.unwrap_or((0.0, 1.0));
        self.start.push(s as f32);
        self.end.push(e as f32);
    }
}

fn src_of<'j>(sources: &'j J, own: bool) -> Option<&'j J> {
    let arr = sources.as_array()?;
    if own {
        if let Some(s) = arr.iter().find(|s| s.get("property").and_then(|p| p.as_str()).unwrap_or("") == "") {
            return Some(s);
        }
    }
    arr.first()
}

const ROUTE_CLASSES: [&str; 5] = ["motorway", "trunk", "primary", "secondary", "tertiary"];

fn routes_txt(p: &Props) -> String {
    let r = p.json("routes");
    let mut parts = vec![];
    if let Some(a) = r.as_array() {
        for x in a {
            let rf = match x.get("ref") {
                Some(J::Null) | None => continue,
                Some(v) => jstr(v).unwrap_or_default(),
            };
            let net = x.get("network").and_then(jstr).unwrap_or_default().to_lowercase();
            let b = x.get("between").and_then(|b| b.as_array());
            let b0 = b.and_then(|b| b.first()).and_then(|v| v.as_f64()).unwrap_or(0.0);
            let b1 = b.and_then(|b| b.get(1)).and_then(|v| v.as_f64()).unwrap_or(1.0);
            parts.push(format!("{net}|{rf}|{}|{}", duck_double(b0), duck_double(b1)));
        }
    }
    parts.join(";")
}

fn text_col(layer: &str, col: &str, p: &Props, src: Option<&J>, sources: &J, height: Option<f64>) -> Option<String> {
    match col {
        "name" => p.json("names").get("primary").and_then(jstr),
        "src_dataset" => src.and_then(|s| s.get("dataset")).and_then(jstr),
        "src_record" => src.and_then(|s| s.get("record_id")).and_then(jstr),
        "height_src" => {
            let by_prop = sources.as_array().and_then(|a| {
                a.iter().find(|s| s.get("property").and_then(|v| v.as_str()) == Some("/properties/height"))
            });
            match by_prop {
                Some(s) => s.get("dataset").and_then(jstr),
                None if height.is_some() => sources.as_array().and_then(|a| a.first())
                    .and_then(|s| s.get("dataset")).and_then(jstr),
                None => None,
            }
        }
        "taxonomy" => p.json("taxonomy").get("primary").and_then(jstr),
        "brand_wikidata" => p.json("brand").get("wikidata").and_then(jstr),
        "brand_name" => p.json("brand").get("names").and_then(|n| n.get("primary")).and_then(jstr),
        "tags" => p.s("source_tags"),
        "routes_txt" => Some(routes_txt(p)),
        _ => {
            let _ = layer;
            p.s(col)
        }
    }
}

// ------------------------------------------------------------------------------------------------ the layer
struct Tile {
    z: u8,
    x: i64,
    y: i64,
    dec: Dec,                   // the layer and its extent
    scale: f64,                 // tile pixels -> world units
    conn: Option<Dec>,          // the transportation tiles' connectors (the segments' junction points)
}

impl Tile {
    fn lay(&self) -> Option<&Layer> {
        self.dec.as_ref().as_ref().map(|(l, _)| l)
    }
}

struct Built {
    id: String,
    kind: u8,
    parts: Vec<Vec<Vec<P>>>, // parts -> rings (lines: one ring each; points: one ring of one vertex)
    clipped: bool,
    tile: usize,
    feat: usize,
    rep: Option<P>, // cx / cy in place of the bbox centre (world units): see owner_point
}

fn sorted_tiles(tiles: Vec<(u8, i64, i64, Vec<u8>)>, layer: &str, center: (i64, i64), conn: bool, ckey: &str)
                -> Result<Vec<Tile>, String> {
    let mut out = vec![];
    for (z, x, y, bytes) in tiles {
        let raw = raw_tile(ckey, z, x, y, &bytes)?;
        let dec: Dec = Arc::new(decode_layer(&raw, layer)?);
        let ext = dec.as_ref().as_ref().map_or(EXT, |(_, e)| *e);
        let conn = if conn { Some(Arc::new(decode_layer(&raw, "connector")?)) } else { None };
        out.push(Tile { z, x, y, dec, scale: EXT / if ext > 0.0 { ext } else { EXT }, conn });
    }
    out.sort_by_key(|t| (if (t.x, t.y) == center { 0 } else { 1 }, t.z, t.x, t.y));
    out.dedup_by_key(|t| (t.z, t.x, t.y));
    Ok(out)
}


fn world(t: &Tile, g: Vec<Vec<[f64; 2]>>) -> Vec<Vec<P>> {
    let (ox, oy) = (t.x as f64 * EXT, t.y as f64 * EXT);
    g.into_iter().map(|r| r.into_iter().map(|q| [ox + q[0] * t.scale, oy + q[1] * t.scale]).collect()).collect()
}

/// MVT polygon rings grouped into polygons: each exterior (positive area, y down) with the holes after it.
fn polygons(rings: Vec<Vec<P>>) -> Vec<Vec<Vec<P>>> {
    let mut out: Vec<Vec<Vec<P>>> = vec![];
    for r in rings {
        let r = open_ring(r);
        if r.len() < 3 {
            continue;
        }
        let a = area2(&r);
        if a > 0.0 || out.is_empty() {
            out.push(vec![r]);
        } else if a < 0.0 {
            out.last_mut().unwrap().push(r);
        }
    }
    out
}

struct Need {
    want: BTreeSet<(i64, i64)>,
    have: BTreeSet<(i64, i64)>,
}

const LOOKAHEAD: i64 = 1; // tiles past an unfetched neighbour asked in the same round, straight on (long roads, rails)

impl Need {
    fn add_for(&mut self, p: P, t: &Tile) {
        let bx = tile_box(t.x, t.y);
        let mut dxs = vec![0i64];
        let mut dys = vec![0i64];
        if p[0] == bx.x0 {
            dxs.push(-1);
        }
        if p[0] == bx.x1 {
            dxs.push(1);
        }
        if p[1] == bx.y0 {
            dys.push(-1);
        }
        if p[1] == bx.y1 {
            dys.push(1);
        }
        for &dx in &dxs {
            for &dy in &dys {
                if dx != 0 || dy != 0 {
                    let n = (t.x + dx, t.y + dy);
                    if self.have.contains(&n) {
                        continue;       // (fetched, no piece goes on there: the line ends on the edge)
                    }
                    for k in 1..=1 + LOOKAHEAD {
                        self.want.insert((t.x + k * dx, t.y + k * dy));
                    }
                }
            }
        }
    }
}

fn whole_line(id_tiles: &[(usize, usize)], tiles: &[Tile], parts_of: &HashMap<(usize, usize), Vec<Vec<P>>>,
              need: &mut Need) -> Vec<Vec<Vec<P>>> {
    let mut pieces = vec![];
    for &(ti, fi) in id_tiles {
        let t = &tiles[ti];
        let bx = tile_box(t.x, t.y);
        for line in &parts_of[&(ti, fi)] {
            for pc in clip_line(line, &bx) {
                let (sb, eb) = (bx.on_edge(pc[0]), bx.on_edge(*pc.last().unwrap()));
                pieces.push(Piece { pts: pc, tile: ti, sb, eb });
            }
        }
    }
    let (chains, open) = link(&pieces, false);
    for p in open {
        // the tile the open end touches (any fetched tile whose box has it on its edge)
        for t in tiles.iter() {
            if tile_box(t.x, t.y).on_edge(p) && tile_box(t.x, t.y).inside(p) {
                need.add_for(p, t);
            }
        }
    }
    // the chain from the line's own first vertex first (owner: the cell holding it, see build)
    let mut chains: Vec<(Vec<P>, bool, bool)> = chains.into_iter().filter(|(c, _, _)| c.len() >= 2).collect();
    chains.sort_by_key(|c| c.2);
    chains.into_iter().map(|(c, _, _)| vec![c]).collect()
}

fn whole_polygon(id_tiles: &[(usize, usize)], tiles: &[Tile], parts_of: &HashMap<(usize, usize), Vec<Vec<P>>>,
                 need: &mut Need) -> Vec<Vec<Vec<P>>> {
    if id_tiles.len() == 1 {
        // wholly in one fetched tile? (no ring reaches its edges: the tile's polygon as it is)
        let (ti, fi) = id_tiles[0];
        let t = &tiles[ti];
        let bx = tile_box(t.x, t.y);
        let rings = &parts_of[&(ti, fi)];
        if rings.iter().all(|r| r.iter().all(|&p| bx.inside(p) && !bx.on_edge(p))) {
            return polygons(rings.clone());
        }
    }
    let mut pieces = vec![];
    let mut whole: Vec<Vec<P>> = vec![];
    for &(ti, fi) in id_tiles {
        let t = &tiles[ti];
        let bx = tile_box(t.x, t.y);
        for r in &parts_of[&(ti, fi)] {
            let r = open_ring(r.clone());
            if r.len() < 3 {
                continue;
            }
            let c = clip_ring(&r, &bx);
            if c.len() < 3 {
                continue;
            }
            let (chains, ring) = ring_chains(&c, &bx);
            if let Some(rr) = ring {
                whole.push(rr);
            }
            for ch in chains {
                if ch.len() >= 2 {
                    pieces.push(Piece { pts: ch, tile: ti, sb: true, eb: true });
                }
            }
        }
    }
    let (chains, open) = link(&pieces, true);
    for p in open {
        for t in tiles.iter() {
            if tile_box(t.x, t.y).on_edge(p) && tile_box(t.x, t.y).inside(p) {
                need.add_for(p, t);
            }
        }
    }
    for (c, _closed, _) in chains {
        let r = open_ring(c);
        if r.len() >= 3 {
            whole.push(r);
        }
    }
    // exteriors (positive area, y down) and the holes inside them
    let mut ext: Vec<(Vec<P>, f64)> = vec![];
    let mut holes: Vec<Vec<P>> = vec![];
    for r in whole {
        let a = area2(&r);
        if a > 0.0 {
            ext.push((r, a));
        } else if a < 0.0 {
            holes.push(r);
        }
    }
    let mut out: Vec<Vec<Vec<P>>> = ext.iter().map(|(r, _)| vec![r.clone()]).collect();
    for h in holes {
        let mut best: Option<(usize, f64)> = None;
        for (k, (r, a)) in ext.iter().enumerate() {
            if point_in_ring(h[0], r) && best.map_or(true, |(_, b)| *a < b) {
                best = Some((k, *a));
            }
        }
        if let Some((k, _)) = best {
            out[k].push(h);
        }
    }
    out
}

/// Polygon parts cut to the box (mapgeom.clip_polygon_parts' rules: a part whose exterior is left with no area goes,
/// its holes cut too): (parts, changed).
fn clip_polygons(polys: Vec<Vec<Vec<P>>>, bx: &Box2) -> (Vec<Vec<Vec<P>>>, bool) {
    let mut out = vec![];
    let mut changed = false;
    for rings in polys {
        let ext = &rings[0];
        if ext.iter().all(|&p| bx.inside(p)) {
            out.push(rings);
            continue;
        }
        changed = true;
        let e = clip_ring(ext, bx);
        if e.len() < 3 || area2(&e) == 0.0 {
            continue;
        }
        let mut new = vec![e];
        for h in &rings[1..] {
            let c = clip_ring(h, bx);
            if c.len() >= 3 && area2(&c) != 0.0 {
                new.push(c);
            }
        }
        out.push(new);
    }
    (out, changed)
}

/// The connectors' points by id (world units), each from the tile whose own square holds it.
fn connector_points(tiles: &[Tile]) -> HashMap<String, P> {
    let mut out: HashMap<String, (P, bool)> = HashMap::new();
    for t in tiles {
        let (lay, ext) = match t.conn.as_ref().and_then(|c| c.as_ref().as_ref()) {
            Some((l, e)) => (l, e),
            None => continue,
        };
        let sc = EXT / if *ext > 0.0 { *ext } else { EXT };
        let bx = tile_box(t.x, t.y);
        let (ox, oy) = (t.x as f64 * EXT, t.y as f64 * EXT);
        for f in &lay.feats {
            let p = Props { lay, tags: &f.tags };
            let id = match p.get("id") {
                Some(V::S(s)) => s.clone(),
                _ => continue,
            };
            let g = geometry(&lay.geom(f));
            let q = match g.first().and_then(|r| r.first()) {
                Some(q) => [ox + q[0] * sc, oy + q[1] * sc],
                None => continue,
            };
            let own = bx.inside(q);
            match out.get(&id) {
                Some((_, true)) => {}
                Some((_, false)) if !own => {}
                _ => {
                    out.insert(id, (q, own));
                }
            }
        }
    }
    out.into_iter().map(|(k, (p, _))| (k, p)).collect()
}

/// The segment's junction points put back into its line: Planetiler's simplification drops a vertex where another
/// road meets the segment mid-way when the line runs straight through it (2 of 3 such junctions in Fells Point), the
/// parquet line always has one there. Each connector with 0 < at < 1 goes in at its nearest place along the line
/// (searched from the previous one's, in `at` order), unless a vertex is already there.
fn junction_vertices(lines: &mut [Vec<Vec<P>>], p: &Props, conns: &HashMap<String, P>) {
    let js = p.json("connectors");
    let mut mids: Vec<(f64, P)> = vec![];
    if let Some(a) = js.as_array() {
        for x in a {
            let at = x.get("at").and_then(|v| v.as_f64()).unwrap_or(0.0);
            if at <= 0.0 || at >= 1.0 {
                continue;
            }
            if let Some(q) = x.get("connector_id").and_then(|v| v.as_str()).and_then(|c| conns.get(c)) {
                mids.push((at, *q));
            }
        }
    }
    if mids.is_empty() {
        return;
    }
    mids.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    const NEAR: f64 = 4.0; // world units (~1.8 m at z14): farther, the connector is not on this line
    let mut last = (0usize, 0usize, 0.0f64);
    for (_, q) in mids {
        let mut best: Option<(f64, usize, usize, f64)> = None;
        let mut best_any: Option<(f64, usize, usize, f64)> = None;
        for (pi, part) in lines.iter().enumerate() {
            let l = &part[0];
            for k in 0..l.len().saturating_sub(1) {
                let (a, b) = (l[k], l[k + 1]);
                let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                let ll = dx * dx + dy * dy;
                let t = if ll > 0.0 { (((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / ll).clamp(0.0, 1.0) } else { 0.0 };
                let d = ((a[0] + t * dx - q[0]).powi(2) + (a[1] + t * dy - q[1]).powi(2)).sqrt();
                let c = (d, pi, k, t);
                if best_any.map_or(true, |b| d < b.0) {
                    best_any = Some(c);
                }
                if (pi, k, t) >= last && best.map_or(true, |b| d < b.0) {
                    best = Some(c);
                }
            }
        }
        let pick = match (best, best_any) {
            (Some(b), _) if b.0 <= NEAR => b,
            (_, Some(b)) if b.0 <= NEAR => b,
            _ => continue,
        };
        let (_, pi, k, t) = pick;
        let l = &mut lines[pi][0];
        if l.iter().any(|v| v[0] == q[0] && v[1] == q[1]) {
            last = (pi, k, t);
            continue;
        }
        l.insert(k + 1, q);
        last = (pi, k + 1, 0.0);
    }
}

/// Where a whole feature is owned (mapdata: the cell holding its cx / cy, the bbox centre): the bbox centre, unless
/// the feature does not reach that z14 cell (a road bent round a cell's corner: no cell lists it there, so none would
/// build it); then its first vertex (every cell that lists it has the same whole geometry and picks the same one).
/// None: the bbox centre.
fn owner_point(kind: u8, parts: &[Vec<Vec<P>>], bb: (f64, f64, f64, f64), tz: u8) -> Option<P> {
    let n = (1u64 << tz) as f64 * EXT;
    let a = lonlat([bb.0, bb.3], tz);
    let b = lonlat([bb.2, bb.1], tz);
    let (clon, clat) = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0);
    let wx = (clon + 180.0) / 360.0 * n;
    let wy = (1.0 - clat.to_radians().tan().asinh() / std::f64::consts::PI) / 2.0 * n;
    let cs = EXT / (1u64 << (14 - tz.min(14))) as f64;
    let (cx, cy) = ((wx / cs).floor(), (wy / cs).floor());
    let bx = Box2 { x0: cx * cs, y0: cy * cs, x1: (cx + 1.0) * cs, y1: (cy + 1.0) * cs };
    let outer: Vec<Vec<P>> = parts.iter().filter_map(|p| p.first().cloned()).collect();
    if touches(kind, &outer, &bx) {
        None
    } else {
        outer.first().and_then(|r| r.first()).copied()
    }
}

fn touches(kind: u8, parts: &[Vec<P>], bx: &Box2) -> bool {
    match kind {
        POINT => parts.iter().any(|r| r.iter().any(|&p| bx.inside(p))),
        // (a vertex on the box's edge counts: a road starting on a cell edge is owned by the cell east / south of it
        // (mapcells.owner), which must list it)
        LINE => parts.iter().any(|r| r.iter().any(|&p| bx.inside(p)) || !clip_line(r, bx).is_empty()),
        _ => parts.iter().any(|r| {
            let r = open_ring(r.clone());
            r.len() >= 3 && clip_ring(&r, bx).len() >= 3
        }),
    }
}

fn lonlat(p: P, z: u8) -> [f64; 2] {
    let n = (1u64 << z) as f64 * EXT;
    let lon = p[0] / n * 360.0 - 180.0;
    let lat = (std::f64::consts::PI * (1.0 - 2.0 * p[1] / n)).sinh().atan().to_degrees();
    [lon, lat]
}

struct Spec {
    layer: String,
    text: Vec<String>,
    float: Vec<String>,
    boolean: Vec<String>,
    clip: bool,
    road_rules: bool,
    src_own: bool,
}

#[allow(clippy::type_complexity)]
struct Out {
    need: Vec<(i64, i64)>,
    feats: Vec<Built>,
    partial: usize,
    partial_ids: Vec<String>,
}

fn build(tiles: &[Tile], cell: (u8, i64, i64), tz: u8, spec: &Spec, cap: i64) -> Out {
    let sh = cell.0 as i64 - tz as i64;
    let center = (cell.1 >> sh, cell.2 >> sh);
    let cs = EXT / (1i64 << sh) as f64;
    let cbox = Box2 { x0: cell.1 as f64 * cs, y0: cell.2 as f64 * cs, x1: (cell.1 + 1) as f64 * cs,
                      y1: (cell.2 + 1) as f64 * cs };
    // the features whose geometry reaches the cell (one that only passes within the tile's buffer is the
    // neighbour's: the parquet query lists it when its bbox touches the cell, but no build reads it here, and
    // following it would fetch tiles for nothing)
    let near = cbox;
    let mut parts_of: HashMap<(usize, usize), Vec<Vec<P>>> = HashMap::new();
    let mut by_id: BTreeMap<String, Vec<(usize, usize)>> = BTreeMap::new();
    let mut targets: BTreeSet<String> = BTreeSet::new();
    for (ti, t) in tiles.iter().enumerate() {
        let lay = match t.lay() {
            Some(l) => l,
            None => continue,
        };
        let is_center = (t.x, t.y) == center && t.z == tz;
        for (fi, f) in lay.feats.iter().enumerate() {
            let id = match f.id.clone() {
                Some(s) => s,
                None => continue,
            };
            if !is_center && !targets.contains(&id) {
                continue;               // (a neighbour's feature: the targets' pieces only; the centre tile comes first)
            }
            let g = world(t, geometry(&lay.geom(f)));
            if is_center && (spec.clip && f.gtype == POLYGON || touches(f.gtype, &g, &near)) {
                targets.insert(id.clone());
            }
            parts_of.entry((ti, fi)).or_default().extend(g);
            by_id.entry(id).or_default().push((ti, fi));
        }
    }
    let conns = if tiles.iter().any(|t| t.conn.as_ref().map_or(false, |c| c.is_some())) { Some(connector_points(tiles)) } else { None };
    let mut need = Need { want: BTreeSet::new(), have: tiles.iter().map(|t| (t.x, t.y)).collect() };
    let mut feats = vec![];
    let mut partial = 0usize;
    let mut partial_ids: Vec<String> = vec![];
    for id in targets.iter() {
        let occ = &by_id[id];
        let (ti0, fi0) = occ[0];
        let f0 = &tiles[ti0].lay().unwrap().feats[fi0];
        let kind = f0.gtype;
        let before = need.want.len();
        let (parts, clipped) = match kind {
            POINT => {
                // the point from the tile whose own square holds it (else the centre tile's)
                let mut pts: Option<Vec<Vec<P>>> = None;
                for &(ti, fi) in occ {
                    let t = &tiles[ti];
                    let bx = tile_box(t.x, t.y);
                    let g = &parts_of[&(ti, fi)];
                    if g.iter().all(|r| r.iter().all(|&p| bx.inside(p))) {
                        pts = Some(g.clone());
                        break;
                    }
                }
                let g = pts.unwrap_or_else(|| parts_of[&(ti0, fi0)].clone());
                (g.into_iter().filter(|r| !r.is_empty()).map(|r| vec![vec![r[0]]]).collect(), false)
            }
            LINE => {
                let mut l = whole_line(occ, tiles, &parts_of, &mut need);
                if let Some(cp) = &conns {
                    let lay = tiles[ti0].lay().unwrap();
                    let p = Props { lay, tags: &f0.tags };
                    junction_vertices(&mut l, &p, cp);
                }
                (l, false)
            }
            POLYGON if spec.clip => {
                let polys = polygons(parts_of[&(ti0, fi0)].clone());
                clip_polygons(polys, &cbox)
            }
            POLYGON => (whole_polygon(occ, tiles, &parts_of, &mut need), false),
            _ => continue,
        };
        if need.want.len() > before {
            partial += 1;
            partial_ids.push(id.clone());
        }
        if parts.is_empty() {
            continue;
        }
        // bbox against the cell (closed), as the parquet query's bbox filter
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for part in &parts {
            for r in part {
                for p in r {
                    x0 = x0.min(p[0]);
                    y0 = y0.min(p[1]);
                    x1 = x1.max(p[0]);
                    y1 = y1.max(p[1]);
                }
            }
        }
        if x0 > cbox.x1 || x1 < cbox.x0 || y0 > cbox.y1 || y1 < cbox.y0 {
            continue;
        }
        // a line is owned by the cell holding its first vertex: every cell that lists it agrees, even where a long
        // one is cut at the cap (its first chain then starts on an outer tile edge, outside the cell); a polygon by
        // its bbox centre's (owner_point)
        let rep = if kind == LINE {
            parts.first().and_then(|p| p.first()).and_then(|r| r.first()).copied()
        } else if kind == POINT || clipped {
            None
        } else {
            owner_point(kind, &parts, (x0, y0, x1, y1), tz)
        };
        feats.push(Built { id: id.clone(), kind, parts, clipped, tile: ti0, feat: fi0, rep });
    }
    let have: BTreeSet<(i64, i64)> = tiles.iter().map(|t| (t.x, t.y)).collect();
    let n = 1i64 << tz;
    let nd: Vec<(i64, i64)> = need.want.into_iter()
        .filter(|&(x, y)| x >= 0 && y >= 0 && x < n && y < n && !have.contains(&(x, y))
            && (x - center.0).abs() <= cap && (y - center.1).abs() <= cap)
        .collect();
    Out { need: nd, feats, partial, partial_ids }
}

/// A cell's layer from its PMTiles tiles (see the module docstring). tiles: [(z, x, y, tile bytes)] (absent tiles:
/// empty bytes); cell: the map cell (z, x, y); tz: the tiles' zoom; text / float / bool: the layer's columns
/// (overture.LAYERS); clip: polygons cut to the cell; road_rules: the roads layer's rule tables; src_own: the
/// feature's own source for src_* (the roads'); cap: the farthest ring of tiles (from the cell's) a feature is
/// followed into; fetch: fetch([(z, x, y)]) -> [bytes] (b"": none in the archive), called with the tiles the
/// features still need, at most max_rounds - 1 times (each tile decoded once in the call); cache_key: the archive's
/// identity (release, theme, ETag): its decompressed tiles kept per process (RAWS), none when empty. The result also
/// says which tiles were read ("tiles"), the rounds, and the features still cut at the end ("partial_ids").
#[pyfunction]
#[pyo3(signature = (layer, mvt_layer, tiles, cell, tz, text, float, boolean, clip, road_rules, src_own, cap,
                    fetch=None, max_rounds=8, cache_key=String::new()))]
#[allow(clippy::too_many_arguments)]
pub fn pmt_layer<'py>(py: Python<'py>, layer: String, mvt_layer: String, tiles: Vec<(u8, i64, i64, Bound<'py, PyBytes>)>,
                      cell: (u8, i64, i64), tz: u8, text: Vec<String>, float: Vec<String>, boolean: Vec<String>,
                      clip: bool, road_rules: bool, src_own: bool, cap: i64, fetch: Option<Bound<'py, PyAny>>,
                      max_rounds: usize, cache_key: String) -> PyResult<Bound<'py, PyDict>> {
    if cell.0 < tz {
        return Err(PyValueError::new_err("the cell's zoom is below the tiles'"));
    }
    let spec = Spec { layer, text, float, boolean, clip, road_rules, src_own };
    let sh = cell.0 as i64 - tz as i64;
    let center = (cell.1 >> sh, cell.2 >> sh);
    let conn = mvt_layer == "segment";
    let mut new: Vec<(u8, i64, i64, Vec<u8>)> = tiles.into_iter()
        .map(|(z, x, y, b)| (z, x, y, b.as_bytes().to_vec())).collect();
    let mut ts: Vec<Tile> = vec![];
    let mut rounds = 0usize;
    let out = loop {
        rounds += 1;
        let (got, out) = py.detach(|| -> Result<(Vec<Tile>, Out), String> {
            let mut all = std::mem::take(&mut ts);
            all.extend(sorted_tiles(std::mem::take(&mut new), &mvt_layer, center, conn, &cache_key)?);
            all.sort_by_key(|t| (if (t.x, t.y) == center { 0 } else { 1 }, t.z, t.x, t.y));
            all.dedup_by_key(|t| (t.z, t.x, t.y));
            let out = build(&all, cell, tz, &spec, cap);
            Ok((all, out))
        }).map_err(PyValueError::new_err)?;
        ts = got;
        let f = match &fetch {
            Some(f) if !out.need.is_empty() && rounds < max_rounds => f,
            _ => break out,
        };
        let want: Vec<(u8, i64, i64)> = out.need.iter().map(|&(x, y)| (tz, x, y)).collect();
        let got = f.call1((want.clone(),))?;
        let bodies: Vec<Vec<u8>> = got.extract::<Vec<Bound<'py, PyBytes>>>()?.iter().map(|b| b.as_bytes().to_vec()).collect();
        if bodies.len() != want.len() {
            return Err(PyValueError::new_err("fetch gave another number of tiles than asked"));
        }
        new = want.into_iter().zip(bodies).map(|((z, x, y), b)| (z, x, y, b)).collect();
    };
    let d = PyDict::new(py);
    let need = PyList::empty(py);
    for (x, y) in &out.need {
        need.append((tz, *x, *y))?;
    }
    d.set_item("need", need)?;
    d.set_item("partial", out.partial)?;
    d.set_item("partial_ids", out.partial_ids.clone())?;
    d.set_item("rounds", rounds)?;
    let read = PyList::empty(py);
    for t in &ts {
        read.append((t.z, t.x, t.y))?;
    }
    d.set_item("tiles", read)?;
    columns(py, &d, &ts, &out.feats, tz, &spec)?;
    Ok(d)
}

fn columns(py: Python<'_>, d: &Bound<'_, PyDict>, tiles: &[Tile], feats: &[Built], tz: u8, spec: &Spec)
           -> PyResult<()> {
    let n = feats.len();
    let mut kinds = Vec::with_capacity(n);
    let mut part_off: Vec<i64> = vec![0];
    let mut ring_off: Vec<i64> = vec![0];
    let mut coord_off: Vec<i64> = vec![0];
    let mut ll: Vec<f64> = vec![];
    let mut cx = Vec::with_capacity(n);
    let mut cy = Vec::with_capacity(n);
    let mut clipped = Vec::with_capacity(n);
    let (mut np_, mut nr, mut nc) = (0i64, 0i64, 0i64);
    for b in feats {
        kinds.push(b.kind);
        clipped.push(b.clipped);
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for part in &b.parts {
            for (k, r) in part.iter().enumerate() {
                let mut pts: Vec<[f64; 2]> = r.iter().map(|&p| lonlat(p, tz)).collect();
                if b.kind == POLYGON {
                    // exteriors counter-clockwise in lon/lat, holes clockwise (the parquet cells'), closed
                    let a = area2(&pts);
                    if (k == 0) != (a > 0.0) {
                        pts.reverse();
                    }
                    if let Some(&f) = pts.first() {
                        pts.push(f);
                    }
                }
                for p in &pts {
                    x0 = x0.min(p[0]);
                    x1 = x1.max(p[0]);
                    y0 = y0.min(p[1]);
                    y1 = y1.max(p[1]);
                    ll.push(p[0]);
                    ll.push(p[1]);
                }
                nc += pts.len() as i64;
                coord_off.push(nc);
            }
            nr += part.len() as i64;
            ring_off.push(nr);
        }
        np_ += b.parts.len() as i64;
        part_off.push(np_);
        match b.rep {
            Some(p) => {
                let q = lonlat(p, tz);
                cx.push(q[0]);
                cy.push(q[1]);
            }
            None => {
                cx.push((x0 + x1) / 2.0);
                cy.push((y0 + y1) / 2.0);
            }
        }
    }
    let lonlat = Array2::from_shape_vec((ll.len() / 2, 2), ll).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let ids = PyList::empty(py);
    for b in feats {
        ids.append(&b.id)?;
    }
    d.set_item("ids", ids)?;
    d.set_item("kind", kinds.into_pyarray(py))?;
    d.set_item("part_off", part_off.into_pyarray(py))?;
    d.set_item("ring_off", ring_off.into_pyarray(py))?;
    d.set_item("coord_off", coord_off.into_pyarray(py))?;
    d.set_item("lonlat", lonlat.into_pyarray(py))?;
    d.set_item("clipped", clipped.into_pyarray(py))?;
    d.set_item("cx", cx.into_pyarray(py))?;
    d.set_item("cy", cy.into_pyarray(py))?;

    // attributes and rules
    let mut text: Vec<Vec<String>> = vec![Vec::with_capacity(n); spec.text.len()];
    let mut flt: Vec<Vec<f32>> = vec![Vec::with_capacity(n); spec.float.len()];
    let mut bln: Vec<Vec<bool>> = vec![Vec::with_capacity(n); spec.boolean.len()];
    let mut sources: BTreeMap<(String, String), i64> = BTreeMap::new();
    let rule_names = ["surface", "width", "level", "subclass", "flags", "max_speed_kmh", "connectors", "oneway",
                      "src_ways"];
    let mut rules: Vec<Rule> = rule_names.iter()
        .map(|r| Rule::new(matches!(*r, "surface" | "subclass" | "flags" | "connectors" | "src_ways")))
        .collect();
    let mut keep = vec![true; n];
    for (i, b) in feats.iter().enumerate() {
        let t = &tiles[b.tile];
        let l = t.lay().unwrap();
        let p = Props { lay: l, tags: &l.feats[b.feat].tags };
        let srcs = p.json("sources");
        let src = src_of(&srcs, spec.src_own);
        let height = p.f("height");
        if spec.layer == "road_routes" {
            let cls = p.s("class").unwrap_or_default();
            if p.s("subtype").as_deref() != Some("road") || !ROUTE_CLASSES.contains(&cls.as_str())
                || routes_txt(&p).is_empty() {
                keep[i] = false;
            }
        }
        for (k, c) in spec.text.iter().enumerate() {
            text[k].push(text_col(&spec.layer, c, &p, src, &srcs, height).unwrap_or_default());
        }
        for (k, c) in spec.float.iter().enumerate() {
            flt[k].push(p.f(c).map(|v| v as f32).unwrap_or(f32::NAN));
        }
        for (k, c) in spec.boolean.iter().enumerate() {
            bln[k].push(p.b(c));
        }
        if keep[i] {
            if let Some(a) = srcs.as_array() {
                for s in a {
                    let ds = s.get("dataset").and_then(jstr).unwrap_or_default();
                    let lic = s.get("license").and_then(jstr).unwrap_or_default();
                    *sources.entry((ds, lic)).or_insert(0) += 1;
                }
            }
        }
        if !spec.road_rules {
            continue;
        }
        for (r, (col, fieldn)) in [("road_surface", "value"), ("width_rules", "value"), ("level_rules", "value"),
                                   ("subclass_rules", "value")].iter().enumerate() {
            if let Some(a) = p.json(col).as_array() {
                for x in a {
                    let v = x.get(*fieldn).unwrap_or(&J::Null);
                    if rules[r].is_text {
                        rules[r].push_t(i, jstr(v), between(x));
                    } else {
                        rules[r].push_f(i, jf(v), between(x));
                    }
                }
            }
        }
        for col in ["road_flags", "rail_flags"] {
            if let Some(a) = p.json(col).as_array() {
                for x in a {
                    if let Some(vals) = x.get("values").and_then(|v| v.as_array()) {
                        for f in vals {
                            rules[4].push_t(i, jstr(f), between(x));
                        }
                    }
                }
            }
        }
        if let Some(a) = p.json("speed_limits").as_array() {
            for x in a {
                let ms = x.get("max_speed");
                let v = ms.and_then(|m| m.get("value")).and_then(|v| v.as_f64());
                let u = ms.and_then(|m| m.get("unit")).and_then(|v| v.as_str()).unwrap_or("").to_lowercase();
                let uncond = matches!(x.get("when"), None | Some(J::Null));
                if let (Some(v), true) = (v, uncond) {
                    rules[5].push_f(i, Some(v * if u == "mph" { MPH } else { 1.0 }), between(x));
                }
            }
        }
        if let Some(a) = p.json("connectors").as_array() {
            for x in a {
                let at = x.get("at").and_then(|v| v.as_f64());
                let cid = x.get("connector_id").and_then(jstr);
                let b = at.map(|a| (a, a));
                rules[6].feat.push(i as i32);
                rules[6].text.push(cid.unwrap_or_default());
                let (s, e) = b.unwrap_or((0.0, 1.0));
                rules[6].start.push(s as f32);
                rules[6].end.push(e as f32);
            }
        }
        if let Some(a) = p.json("access_restrictions").as_array() {
            for x in a {
                if x.get("access_type").and_then(|v| v.as_str()) != Some("denied") {
                    continue;
                }
                let w = x.get("when");
                let heading = w.and_then(|w| w.get("heading")).and_then(|v| v.as_str()).unwrap_or("");
                if heading != "forward" && heading != "backward" {
                    continue;
                }
                let g = |k: &str| w.and_then(|w| w.get(k));
                if truthy(g("during")) || truthy(g("using")) || truthy(g("recognized")) || truthy(g("vehicle")) {
                    continue;
                }
                if let Some(J::Array(m)) = g("mode") {
                    if !m.is_empty() && !m.iter().any(|v| matches!(v.as_str(), Some("motor_vehicle" | "vehicle" | "car"))) {
                        continue;
                    }
                }
                rules[7].push_f(i, Some(if heading == "backward" { 1.0 } else { -1.0 }), between(x));
            }
        }
        // the OpenStreetMap ways of the segment (overture.way_sources) and the one covering most of it
        let mut ways: Vec<(String, Option<(f64, f64)>)> = vec![];
        if let Some(a) = srcs.as_array() {
            for s in a {
                let rec = s.get("record_id").and_then(jstr).unwrap_or_default();
                let prop = s.get("property").and_then(|v| v.as_str()).unwrap_or("");
                if rec.starts_with('w') && prop.is_empty() {
                    ways.push((rec, between(s)));
                }
            }
        }
        if !ways.is_empty() {
            let span = |b: &Option<(f64, f64)>| b.map_or(1.0, |(s, e)| e - s);
            let mut best = 0;
            for k in 1..ways.len() {
                if span(&ways[k].1) > span(&ways[best].1) {
                    best = k;
                }
            }
            if let Some(ci) = spec.text.iter().position(|c| c == "src_record") {
                text[ci][i] = ways[best].0.clone();
            }
            if ways.len() > 1 {
                for (r, b) in &ways {
                    rules[8].push_t(i, Some(r.clone()), *b);
                }
            }
        }
    }
    if keep.iter().any(|k| !k) {
        // (road_routes' rows filter: the features and their columns again without the dropped ones)
        let sel: Vec<usize> = (0..n).filter(|&i| keep[i]).collect();
        let sub: Vec<Built> = sel.iter().map(|&i| Built { id: feats[i].id.clone(), kind: feats[i].kind,
            parts: feats[i].parts.clone(), clipped: feats[i].clipped, tile: feats[i].tile, feat: feats[i].feat,
            rep: feats[i].rep }).collect();
        return columns(py, d, tiles, &sub, tz, spec);
    }
    let td = PyDict::new(py);
    for (k, c) in spec.text.iter().enumerate() {
        td.set_item(c, std::mem::take(&mut text[k]))?;
    }
    d.set_item("text", td)?;
    let fd = PyDict::new(py);
    for (k, c) in spec.float.iter().enumerate() {
        fd.set_item(c, std::mem::take(&mut flt[k]).into_pyarray(py))?;
    }
    d.set_item("float", fd)?;
    let bd = PyDict::new(py);
    for (k, c) in spec.boolean.iter().enumerate() {
        bd.set_item(c, std::mem::take(&mut bln[k]).into_pyarray(py))?;
    }
    d.set_item("bool", bd)?;
    let rd = PyDict::new(py);
    if spec.road_rules {
        for (k, name) in rule_names.iter().enumerate() {
            let r = std::mem::replace(&mut rules[k], Rule::new(false));
            let feat: Bound<'_, PyArray1<i32>> = r.feat.into_pyarray(py);
            if r.is_text {
                rd.set_item(name, (feat, r.text, r.start.into_pyarray(py), r.end.into_pyarray(py)))?;
            } else {
                rd.set_item(name, (feat, r.num.into_pyarray(py), r.start.into_pyarray(py), r.end.into_pyarray(py)))?;
            }
        }
    }
    d.set_item("rules", rd)?;
    let sl = PyList::empty(py);
    for ((ds, lic), c) in sources {
        sl.append((ds, lic, c))?;
    }
    d.set_item("sources", sl)?;
    Ok(())
}
