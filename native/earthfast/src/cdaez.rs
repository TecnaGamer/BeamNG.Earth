//! Compressed shapes (.cdae): the game reads a shape whose header says {compression: true} and whose body is one zstd
//! frame (bodysize: that frame's length), as its own vehicle shapes are stored (temp/vehicles/.../*.cdae, frame magic
//! 28 b5 2f fd); checked loading one of ours, 2026-11-04. Ours were written plain: a far tile's shape ~210 KB, the
//! session's shapes ~1.5 GB of a warm 8 min drive at 150 mph (Discord, 2026-11-04: "hard on the drive"). zstd level 3:
//! a far tile's 62 %, a road / building tile's 25-28 %.
//!
//! The header is a msgpack map {info: str, compression: bool, bodysize: int, objectNames: [str]} (server cdae.py);
//! cdae_pack / cdae_unpack rewrite its compression and bodysize and leave the rest as it was.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::borrow::Cow;

pub const LEVEL: i32 = 3;

fn be(b: &[u8], n: usize) -> u64 {
    b[..n].iter().fold(0u64, |v, &x| (v << 8) | x as u64)
}

fn uint(out: &mut Vec<u8>, v: u64) {
    if v < 128 {
        out.push(v as u8);
    } else if v <= 0xff {
        out.extend_from_slice(&[0xcc, v as u8]);
    } else if v <= 0xffff {
        out.push(0xcd);
        out.extend_from_slice(&(v as u16).to_be_bytes());
    } else if v <= 0xffff_ffff {
        out.push(0xce);
        out.extend_from_slice(&(v as u32).to_be_bytes());
    } else {
        out.push(0xcf);
        out.extend_from_slice(&v.to_be_bytes());
    }
}

/// The end of the msgpack value at i (any type the header holds).
fn skip(b: &[u8], i: usize) -> Result<usize, String> {
    let t = *b.get(i).ok_or("truncated header")?;
    let i = i + 1;
    let need = |n: usize| if i + n <= b.len() { Ok(()) } else { Err("truncated header".to_string()) };
    Ok(match t {
        0x00..=0x7f | 0xe0..=0xff | 0xc0 | 0xc2 | 0xc3 => i,
        0xcc | 0xd0 => { need(1)?; i + 1 }
        0xcd | 0xd1 => { need(2)?; i + 2 }
        0xce | 0xd2 | 0xca => { need(4)?; i + 4 }
        0xcf | 0xd3 | 0xcb => { need(8)?; i + 8 }
        0xa0..=0xbf => i + (t & 31) as usize,
        0xd9 | 0xc4 => { need(1)?; i + 1 + be(&b[i..], 1) as usize }
        0xda | 0xc5 => { need(2)?; i + 2 + be(&b[i..], 2) as usize }
        0xdb | 0xc6 => { need(4)?; i + 4 + be(&b[i..], 4) as usize }
        0x90..=0x9f | 0xdc | 0xdd => {
            let (n, mut j) = match t {
                0x90..=0x9f => ((t & 15) as usize, i),
                0xdc => { need(2)?; (be(&b[i..], 2) as usize, i + 2) }
                _ => { need(4)?; (be(&b[i..], 4) as usize, i + 4) }
            };
            for _ in 0..n {
                j = skip(b, j)?;
            }
            j
        }
        _ => return Err(format!("unsupported msgpack type 0x{t:02x} in the header")),
    })
}

/// (version word, compressed?, header bytes' range, body's start) of a shape file.
struct Head {
    compressed: bool,
    map_at: usize,
    body_at: usize,
}

fn head(data: &[u8]) -> Result<Head, String> {
    if data.len() < 9 {
        return Err("file too short".into());
    }
    let hsize = u32::from_le_bytes(data[4..8].try_into().unwrap()) as usize;
    let body_at = 8 + hsize;
    if body_at > data.len() {
        return Err("bad header size".into());
    }
    let t = data[8];
    if !(0x80..=0x8f).contains(&t) {
        return Err("header is not a small map".into());
    }
    let mut i = 9;
    let mut compressed = false;
    for _ in 0..(t & 15) {
        let k0 = i;
        i = skip(data, i)?;
        let key = &data[k0..i];
        let v0 = i;
        i = skip(data, i)?;
        if key == b"\xabcompression" {
            compressed = data[v0] == 0xc3;
        }
    }
    if i != body_at {
        return Err("header size mismatch".into());
    }
    Ok(Head { compressed, map_at: 8, body_at })
}

/// The file with its header's compression and bodysize set, and body.
fn rebuild(data: &[u8], h: &Head, compressed: bool, body: &[u8]) -> Result<Vec<u8>, String> {
    let t = data[h.map_at];
    let mut map = vec![t];
    let mut i = h.map_at + 1;
    for _ in 0..(t & 15) {
        let k0 = i;
        i = skip(data, i)?;
        let key = &data[k0..i];
        let v0 = i;
        i = skip(data, i)?;
        map.extend_from_slice(key);
        if key == b"\xabcompression" {
            map.push(if compressed { 0xc3 } else { 0xc2 });
        } else if key == b"\xa8bodysize" {
            uint(&mut map, body.len() as u64);
        } else {
            map.extend_from_slice(&data[v0..i]);
        }
    }
    let mut out = Vec::with_capacity(8 + map.len() + body.len());
    out.extend_from_slice(&data[..4]);
    out.extend_from_slice(&(map.len() as u32).to_le_bytes());
    out.extend_from_slice(&map);
    out.extend_from_slice(body);
    Ok(out)
}

/// The plain file of a shape (compressed or not).
pub fn plain(data: &[u8]) -> Result<Cow<'_, [u8]>, String> {
    let h = head(data)?;
    if !h.compressed {
        return Ok(Cow::Borrowed(data));
    }
    let body = zstd::stream::decode_all(&data[h.body_at..]).map_err(|e| format!("zstd: {e}"))?;
    Ok(Cow::Owned(rebuild(data, &h, false, &body)?))
}

/// The compressed file of a shape (level: zstd's; one compressed already as it is).
pub fn packed(data: &[u8], level: i32) -> Result<Vec<u8>, String> {
    let h = head(data)?;
    if h.compressed {
        return Ok(data.to_vec());
    }
    let body = zstd::bulk::compress(&data[h.body_at..], level).map_err(|e| format!("zstd: {e}"))?;
    rebuild(data, &h, true, &body)
}

/// Is the shape file's body compressed?
pub fn is_packed(data: &[u8]) -> Result<bool, String> {
    Ok(head(data)?.compressed)
}

/// cdae.py: a shape file with its body as one zstd frame (level LEVEL unless given), as the game reads it.
#[pyfunction]
#[pyo3(signature = (data, level = LEVEL))]
pub fn cdae_pack<'py>(py: Python<'py>, data: &[u8], level: i32) -> PyResult<Bound<'py, PyBytes>> {
    let out = py.detach(|| packed(data, level)).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &out))
}

/// cdae.py: a shape file with a plain body (one compressed or not).
#[pyfunction]
pub fn cdae_unpack<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let out = py.detach(|| plain(data).map(|c| c.into_owned())).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shape(body: &[u8]) -> Vec<u8> {
        // {info: "x", compression: false, bodysize: n, objectNames: ["o"]}
        let mut map = vec![0x84, 0xa4];
        map.extend_from_slice(b"info");
        map.extend_from_slice(&[0xa1, b'x', 0xab]);
        map.extend_from_slice(b"compression");
        map.push(0xc2);
        map.push(0xa8);
        map.extend_from_slice(b"bodysize");
        uint(&mut map, body.len() as u64);
        map.push(0xab);
        map.extend_from_slice(b"objectNames");
        map.extend_from_slice(&[0x91, 0xa1, b'o']);
        let mut out = vec![0x1f, 0, 0x7c, 0];
        out.extend_from_slice(&(map.len() as u32).to_le_bytes());
        out.extend_from_slice(&map);
        out.extend_from_slice(body);
        out
    }

    #[test]
    fn pack_and_back() {
        let body: Vec<u8> = (0..5000u32).flat_map(|i| ((i % 97) as u8).to_le_bytes()).collect();
        let f = shape(&body);
        let p = packed(&f, LEVEL).unwrap();
        assert!(p.len() < f.len());
        assert!(is_packed(&p).unwrap() && !is_packed(&f).unwrap());
        assert_eq!(&p[p.len() - (p.len() - head(&p).unwrap().body_at)..][..4], &[0x28, 0xb5, 0x2f, 0xfd]);
        assert_eq!(plain(&p).unwrap().as_ref(), f.as_slice());
        assert_eq!(plain(&f).unwrap().as_ref(), f.as_slice());
        assert_eq!(packed(&p, LEVEL).unwrap(), p);
    }
}
