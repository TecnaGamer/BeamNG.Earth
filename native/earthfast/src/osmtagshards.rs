//! The OSM road tag shards (tools/build_osm_tag_shards.py, read by server/osmtagshards.py): one zoom-13 tile's blob
//! ("OTG1", the layout in the tool's docstring) decoded into the table osmtags.OsmTags._decode makes of a Layercake
//! row group: {"id", "race", "bus", "frn", per column its values (float32 / float64, NaN where missing; a text column
//! as UTF-8 bytes, "<c>_off" (n + 1 offsets) and "<c>_ok")}, plus "lines" [(file row, way id, {column: text}, (k, 2)
//! lon/lat)], "inside" {file row: [file rows]} and "geom" {file row: (k, 2) lon/lat}.

use numpy::ndarray::Array2;
use numpy::IntoPyArray;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::io::Read;

use crate::pmtiles::gunzip;

struct R<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> R<'a> {
    fn uv(&mut self) -> Result<u64, String> {
        let mut v: u64 = 0;
        let mut s = 0u32;
        loop {
            if self.i >= self.b.len() {
                return Err("truncated varint".into());
            }
            let c = self.b[self.i];
            self.i += 1;
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
    fn zz(&mut self) -> Result<i64, String> {
        let v = self.uv()?;
        Ok(((v >> 1) as i64) ^ -((v & 1) as i64))
    }
    fn n(&mut self, limit: usize) -> Result<usize, String> {
        let v = self.uv()? as usize;
        if v > limit {
            return Err(format!("count {v} past the blob's {limit} bytes"));
        }
        Ok(v)
    }
    fn bytes(&mut self, k: usize) -> Result<&'a [u8], String> {
        if self.i + k > self.b.len() {
            return Err("truncated blob".into());
        }
        let s = &self.b[self.i..self.i + k];
        self.i += k;
        Ok(s)
    }
    fn u8(&mut self) -> Result<u8, String> {
        Ok(self.bytes(1)?[0])
    }
    fn coords(&mut self) -> Result<Vec<f64>, String> {
        let mode = self.u8()?;
        let k = self.n(self.b.len())?;
        let mut out = Vec::with_capacity(2 * k);
        match mode {
            0 => {
                let raw = self.bytes(16 * k)?;
                for c in raw.chunks_exact(8) {
                    out.push(f64::from_le_bytes(c.try_into().unwrap()));
                }
            }
            1 => {
                let (mut x, mut y) = (0i64, 0i64);
                for _ in 0..k {
                    x += self.zz()?;
                    y += self.zz()?;
                    out.push(x as f64 / 1e7);
                    out.push(y as f64 / 1e7);
                }
            }
            m => return Err(format!("coords mode {m}")),
        }
        Ok(out)
    }
}

enum Col {
    F32(Vec<f32>),
    F64(Vec<f64>),
    Text(Vec<u8>, Vec<i32>, Vec<bool>),
}

struct Tile {
    frn: Vec<i64>,
    id: Vec<i64>,
    race: Vec<bool>,
    bus: Vec<bool>,
    cols: Vec<(String, Col)>,
    lines: Vec<(i64, i64, Vec<(String, String)>, Vec<f64>)>,
    inside: Vec<(i64, Vec<i64>)>,
    geom: Vec<(i64, Vec<f64>)>,
}

fn ordered_f32(k: i64) -> f32 {
    let k = k as i32;
    let bits = if k < 0 { k ^ 0x7FFF_FFFF } else { k };
    f32::from_bits(bits as u32)
}

/// The tile's bytes as stored: raw deflate (the builder's), gzip, or plain (they start with the magic).
fn inflate(raw: &[u8]) -> Result<Vec<u8>, String> {
    if raw.starts_with(b"OTG1") || (raw.len() >= 2 && raw[0] == 0x1f && raw[1] == 0x8b) {
        return gunzip(raw);
    }
    let mut out = Vec::with_capacity(raw.len() * 4);
    flate2::read::DeflateDecoder::new(raw).read_to_end(&mut out).map_err(|e| format!("deflate: {e}"))?;
    Ok(out)
}

fn parse(raw: &[u8], num: &[(String, u8)], texts: &[String], line_cols: &[String]) -> Result<Tile, String> {
    let b = inflate(raw)?;
    let mut r = R { b: &b, i: 0 };
    if r.bytes(4)? != b"OTG1" {
        return Err("not an OTG1 tile".into());
    }
    let lim = b.len();
    let n = r.n(lim)?;
    for (nm, w) in num {
        if *w != 4 && *w != 8 {
            return Err(format!("column {nm}: width {w}"));
        }
    }
    let mut frn = Vec::with_capacity(n);
    let mut last = 0i64;
    for _ in 0..n {
        last += r.uv()? as i64;
        frn.push(last);
    }
    let mut id = Vec::with_capacity(n);
    last = 0;
    for _ in 0..n {
        last += r.zz()?;
        id.push(last);
    }
    let flags = r.bytes(n)?;
    let race = flags.iter().map(|f| f & 1 != 0).collect();
    let bus = flags.iter().map(|f| f & 2 != 0).collect();
    let mut cols: Vec<(String, Col)> = Vec::new();
    for c in ["bx0", "by0", "bx1", "by1"] {
        let mut v = Vec::with_capacity(n);
        last = 0;
        for _ in 0..n {
            last += r.zz()?;
            v.push(ordered_f32(last));
        }
        cols.push((c.to_string(), Col::F32(v)));
    }
    let rows = |r: &mut R, nnz: usize| -> Result<Vec<usize>, String> {
        let mut at = Vec::with_capacity(nnz);
        let mut p = 0u64;
        for _ in 0..nnz {
            p += r.uv()?;
            if p as usize >= n {
                return Err("row index past the rows".into());
            }
            at.push(p as usize);
        }
        Ok(at)
    };
    for (nm, w) in num {
        let (nm, w) = (nm.clone(), *w);
        let nnz = r.n(n)?;
        let at = rows(&mut r, nnz)?;
        let raw = r.bytes(nnz * w as usize)?;
        if w == 4 {
            let mut v = vec![f32::NAN; n];
            for (k, c) in raw.chunks_exact(4).enumerate() {
                v[at[k]] = f32::from_le_bytes(c.try_into().unwrap());
            }
            cols.push((nm, Col::F32(v)));
        } else {
            let mut v = vec![f64::NAN; n];
            for (k, c) in raw.chunks_exact(8).enumerate() {
                v[at[k]] = f64::from_le_bytes(c.try_into().unwrap());
            }
            cols.push((nm, Col::F64(v)));
        }
    }
    for nm in texts {
        let nm = nm.clone();
        let nd = r.n(lim)?;
        let mut words: Vec<&[u8]> = Vec::with_capacity(nd);
        for _ in 0..nd {
            let k = r.n(lim)?;
            words.push(r.bytes(k)?);
        }
        let nnz = r.n(n)?;
        let at = rows(&mut r, nnz)?;
        let mut val: Vec<Option<usize>> = vec![None; n];
        for k in 0..nnz {
            let d = r.uv()? as usize;
            if d >= nd {
                return Err(format!("column {nm}: word {d} of {nd}"));
            }
            val[at[k]] = Some(d);
        }
        let mut blob = Vec::new();
        let mut off = Vec::with_capacity(n + 1);
        let mut ok = Vec::with_capacity(n);
        off.push(0i32);
        for v in val {
            if let Some(d) = v {
                blob.extend_from_slice(words[d]);
            }
            ok.push(v.is_some());
            off.push(blob.len() as i32);
        }
        cols.push((nm, Col::Text(blob, off, ok)));
    }
    let mut lines = Vec::new();
    for _ in 0..r.n(lim)? {
        let f = r.uv()? as i64;
        let wid = r.zz()?;
        let mut vals = Vec::new();
        for c in line_cols {
            let k = r.n(lim)?;
            if k > 0 {
                let s = String::from_utf8(r.bytes(k - 1)?.to_vec()).map_err(|e| format!("utf-8: {e}"))?;
                vals.push((c.clone(), s));
            }
        }
        lines.push((f, wid, vals, r.coords()?));
    }
    let mut inside = Vec::new();
    for _ in 0..r.n(lim)? {
        let f = r.uv()? as i64;
        let m = r.n(lim)?;
        let mut v = Vec::with_capacity(m);
        for _ in 0..m {
            v.push(r.uv()? as i64);
        }
        inside.push((f, v));
    }
    let mut geom = Vec::new();
    for _ in 0..r.n(lim)? {
        let f = r.uv()? as i64;
        geom.push((f, r.coords()?));
    }
    Ok(Tile { frn, id, race, bus, cols, lines, inside, geom })
}

fn pairs(py: Python<'_>, v: Vec<f64>) -> PyResult<Py<PyAny>> {
    let a = Array2::from_shape_vec((v.len() / 2, 2), v).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(a.into_pyarray(py).into_any().unbind())
}

/// One OSM tag shard tile (as stored: raw deflate, gzip or plain) decoded; see the module's comment. The blob's
/// columns (the manifest's "schema"): numeric [(name, 4 | 8 bytes)], text [name], lines [name] (the raceways' text
/// columns after the id).
#[pyfunction]
pub fn otg_decode<'py>(py: Python<'py>, data: &[u8], numeric: Vec<(String, u8)>, text: Vec<String>,
                       lines: Vec<String>) -> PyResult<Bound<'py, PyDict>> {
    let raw = data.to_vec();
    let t = py.detach(move || parse(&raw, &numeric, &text, &lines)).map_err(PyValueError::new_err)?;
    let d = PyDict::new(py);
    d.set_item("frn", t.frn.into_pyarray(py))?;
    d.set_item("id", t.id.into_pyarray(py))?;
    d.set_item("race", t.race.into_pyarray(py))?;
    d.set_item("bus", t.bus.into_pyarray(py))?;
    for (nm, c) in t.cols {
        match c {
            Col::F32(v) => d.set_item(nm, v.into_pyarray(py))?,
            Col::F64(v) => d.set_item(nm, v.into_pyarray(py))?,
            Col::Text(blob, off, ok) => {
                d.set_item(format!("{nm}_off"), off.into_pyarray(py))?;
                d.set_item(format!("{nm}_ok"), ok.into_pyarray(py))?;
                d.set_item(nm, blob.into_pyarray(py))?;
            }
        }
    }
    let lines = PyList::empty(py);
    for (f, wid, vals, xy) in t.lines {
        let cols = PyDict::new(py);
        for (k, v) in vals {
            cols.set_item(k, v)?;
        }
        lines.append((f, wid, cols, pairs(py, xy)?))?;
    }
    d.set_item("lines", lines)?;
    let inside = PyDict::new(py);
    for (f, v) in t.inside {
        inside.set_item(f, v)?;
    }
    d.set_item("inside", inside)?;
    let geom = PyDict::new(py);
    for (f, xy) in t.geom {
        geom.set_item(f, pairs(py, xy)?)?;
    }
    d.set_item("geom", geom)?;
    Ok(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ordered_keys_round_trip() {
        for v in [0.0f32, -0.0, 1.5, -1.5, -111.6513, 35.1983, 180.0, -180.0] {
            let b = v.to_bits() as i32;
            let k = if b < 0 { b ^ 0x7FFF_FFFF } else { b };
            assert_eq!(ordered_f32(k as i64).to_bits(), v.to_bits());
        }
    }
}
