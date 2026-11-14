//! OpenStreetMap's construction years of buildings (tools/build_osm_building_years.py, read by server/osmyears.py):
//! one zoom-12 tile's blob decoded into its buildings' keys and years.
//!
//! Blob ("OBY1", then LEB128 varints): uv n; n x uv key deltas (the first from 0; each later one at least 1: the keys
//! ascend, one row a key); n x uv zz(year - YEAR_BASE). A key is 2 x the OSM id, + 1 for a relation. Stored as raw
//! deflate (the builder's), or plain (starting with the magic). Bytes after the years are left for later sections.
//!
//! The tiles come from the network: every count is checked against the bytes left, the inflated size is capped
//! (MAX_BLOB), the rows (MAX_ROWS) and what they decode to (MAX_DECODED) are bounded before anything is allocated for
//! them, and a bad tile is an error, never a panic (Codex review: a 62 KB deflated tile of 32 M one-byte rows passed
//! the bytes-left check inside the old 64 MiB MAX_BLOB and asked for ~300 MB of keys and years).

use numpy::IntoPyArray;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::io::Read;

pub const MAGIC: &[u8; 4] = b"OBY1";
pub const YEAR_BASE: i64 = 1900;
// The published build of 2026-11-14 (59,521 tiles, 17.5 M rows): its largest tile 105,421 rows, 219 KB inflated,
// 104 KB stored. The bounds leave ~19x (rows) and ~75x (bytes) over it.
const MAX_BLOB: usize = 16 << 20; // a tile inflated (or plain) at most
const MAX_STORED: usize = 16 << 20; // a tile as stored at most (oby_decode refuses a longer one before copying it)
pub const MAX_ROWS: u64 = 2_000_000; // a tile's rows at most
const MAX_DECODED: u64 = 32 << 20; // bytes a tile's decoded rows may take (keys u64 + years i16: 10 a row)
const ROW_BYTES: u64 = 8 + 2;

fn inflate(raw: &[u8]) -> Result<Vec<u8>, String> {
    if raw.len() > MAX_STORED {
        return Err(format!("tile of {} bytes stored, past {MAX_STORED}", raw.len()));
    }
    if raw.starts_with(MAGIC) {
        if raw.len() > MAX_BLOB {
            return Err(format!("plain tile past {MAX_BLOB} bytes"));
        }
        return Ok(raw.to_vec());
    }
    let mut out = Vec::with_capacity(raw.len().saturating_mul(3).min(MAX_BLOB));
    flate2::read::DeflateDecoder::new(raw)
        .take(MAX_BLOB as u64 + 1)
        .read_to_end(&mut out)
        .map_err(|e| format!("deflate: {e}"))?;
    if out.len() > MAX_BLOB {
        return Err(format!("tile inflates past {MAX_BLOB} bytes"));
    }
    Ok(out)
}

struct R<'a> {
    b: &'a [u8],
    i: usize,
}

impl<'a> R<'a> {
    fn uv(&mut self) -> Result<u64, String> {
        let mut v: u64 = 0;
        let mut s = 0u32;
        loop {
            let c = *self.b.get(self.i).ok_or("truncated varint")?;
            self.i += 1;
            if s == 63 && c > 1 {
                return Err("varint past 64 bits".into());
            }
            v |= ((c & 0x7f) as u64) << s;
            if c < 0x80 {
                return Ok(v);
            }
            s += 7;
            if s > 63 {
                return Err("varint too long".into());
            }
        }
    }
    fn left(&self) -> usize {
        self.b.len() - self.i
    }
}

/// (keys ascending, years) of a tile's bytes as stored.
pub fn parse(raw: &[u8]) -> Result<(Vec<u64>, Vec<i16>), String> {
    let b = inflate(raw)?;
    if !b.starts_with(MAGIC) {
        return Err("not an OBY1 tile".into());
    }
    let mut r = R { b: &b, i: MAGIC.len() };
    let n = r.uv()?;
    if n > MAX_ROWS || n.saturating_mul(ROW_BYTES) > MAX_DECODED {
        return Err(format!("{n} rows, past the {MAX_ROWS} a tile may have"));
    }
    if n > (r.left() / 2) as u64 {
        return Err(format!("{n} rows past the tile's {} bytes", r.left()));
    }
    let n = n as usize;
    let mut keys = Vec::with_capacity(n);
    let mut acc: u64 = 0;
    for k in 0..n {
        let d = r.uv()?;
        if k > 0 && d == 0 {
            return Err("keys not ascending".into());
        }
        acc = acc.checked_add(d).ok_or("key past 64 bits")?;
        keys.push(acc);
    }
    let mut years = Vec::with_capacity(n);
    for _ in 0..n {
        let v = r.uv()?;
        let y = ((v >> 1) as i64 ^ -((v & 1) as i64)).checked_add(YEAR_BASE).ok_or("year out of range")?;
        if !(i16::MIN as i64..=i16::MAX as i64).contains(&y) {
            return Err(format!("year {y} out of range"));
        }
        years.push(y as i16);
    }
    Ok((keys, years))
}

/// One OSM building-year tile (as stored: raw deflate or plain) decoded: (keys uint64 ascending, years int16).
#[pyfunction]
pub fn oby_decode<'py>(py: Python<'py>, data: &[u8]) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
    if data.len() > MAX_STORED {
        return Err(PyValueError::new_err(format!("tile of {} bytes stored, past {MAX_STORED}", data.len())));
    }
    let raw = data.to_vec();
    let (k, y) = py.detach(move || parse(&raw)).map_err(PyValueError::new_err)?;
    Ok((k.into_pyarray(py).into_any().unbind(), y.into_pyarray(py).into_any().unbind()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(oby_decode, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn uv(mut v: u64, out: &mut Vec<u8>) {
        while v >= 0x80 {
            out.push((v as u8 & 0x7f) | 0x80);
            v >>= 7;
        }
        out.push(v as u8);
    }

    fn blob(keys: &[u64], years: &[i64]) -> Vec<u8> {
        let mut b = MAGIC.to_vec();
        uv(keys.len() as u64, &mut b);
        let mut prev = 0;
        for &k in keys {
            uv(k - prev, &mut b);
            prev = k;
        }
        for &y in years {
            let d = y - YEAR_BASE;
            uv(((d << 1) ^ (d >> 63)) as u64, &mut b);
        }
        b
    }

    #[test]
    fn round_trip() {
        let keys = [2 * 3565284 + 1, 2 * 34633854, 2 * 42500770, u64::MAX / 2];
        let years = [1850, 1931, 1930, 2024];
        let (k, y) = parse(&blob(&keys, &years)).unwrap();
        assert_eq!(k, keys.to_vec());
        assert_eq!(y, vec![1850i16, 1931, 1930, 2024]);
    }

    #[test]
    fn deflated() {
        use flate2::write::DeflateEncoder;
        use std::io::Write;
        let b = blob(&[4, 10], &[1913, 1999]);
        let mut e = DeflateEncoder::new(Vec::new(), flate2::Compression::default());
        e.write_all(&b).unwrap();
        let (k, y) = parse(&e.finish().unwrap()).unwrap();
        assert_eq!(k, vec![4, 10]);
        assert_eq!(y, vec![1913, 1999]);
    }

    #[test]
    fn bad_tiles_are_errors() {
        assert!(parse(b"").is_err());
        assert!(parse(b"OBY1").is_err());
        let mut b = MAGIC.to_vec();
        uv(1 << 40, &mut b); // a row count far past the bytes
        assert!(parse(&b).is_err());
        let mut b = blob(&[4, 10], &[1913, 1999]);
        b.truncate(b.len() - 1);
        assert!(parse(&b).is_err());
        // keys not ascending
        let mut b = MAGIC.to_vec();
        uv(2, &mut b);
        uv(5, &mut b);
        uv(0, &mut b);
        uv(0, &mut b);
        uv(0, &mut b);
        assert!(parse(&b).is_err());
        // a key sum past 64 bits
        let mut b = MAGIC.to_vec();
        uv(2, &mut b);
        uv(u64::MAX, &mut b);
        uv(5, &mut b);
        uv(0, &mut b);
        uv(0, &mut b);
        assert!(parse(&b).is_err());
        // a varint past 64 bits
        let mut b = MAGIC.to_vec();
        b.extend_from_slice(&[0xff; 10]);
        b.push(0x01);
        assert!(parse(&b).is_err());
    }

    #[test]
    fn allocation_bombs_are_refused_before_allocating() {
        use flate2::write::DeflateEncoder;
        use std::io::Write;
        let deflate = |b: &[u8]| {
            let mut e = DeflateEncoder::new(Vec::new(), flate2::Compression::best());
            e.write_all(b).unwrap();
            e.finish().unwrap()
        };
        // (the review's bomb: 32 M rows of one-byte deltas and years, ~62 KB deflated; it inflates past MAX_BLOB)
        let mut big = MAGIC.to_vec();
        uv(32 << 20, &mut big);
        big.resize(big.len() + (64 << 20) - 16, 1);
        let z = deflate(&big);
        assert!(z.len() < 200_000, "{}", z.len());
        drop(big);
        assert!(parse(&z).unwrap_err().contains("inflates past"));
        // one row past MAX_ROWS with the bytes for every row, inside MAX_BLOB: refused by its count
        let mut b = MAGIC.to_vec();
        uv(MAX_ROWS + 1, &mut b);
        b.resize(b.len() + 2 * (MAX_ROWS as usize + 1), 1);
        assert!(b.len() < MAX_BLOB);
        let z = deflate(&b);
        assert!(parse(&z).unwrap_err().contains("past the"), "refused by the row count");
        // MAX_ROWS itself decodes
        let mut b = MAGIC.to_vec();
        uv(MAX_ROWS, &mut b);
        b.resize(b.len() + 2 * MAX_ROWS as usize, 1);
        let (k, y) = parse(&deflate(&b)).unwrap();
        assert_eq!((k.len(), y.len(), k[k.len() - 1]), (MAX_ROWS as usize, MAX_ROWS as usize, MAX_ROWS));
        assert!(MAX_ROWS * ROW_BYTES <= MAX_DECODED);
    }

    #[test]
    fn oversized_tiles_are_errors() {
        // a plain tile past MAX_STORED (and so past what a plain tile may be), refused before any copy or parse
        let mut b = blob(&[4], &[1913]);
        b.resize(MAX_STORED + 1, 0);
        assert!(parse(&b).unwrap_err().contains("stored"));
        // a deflate stream inflating past MAX_BLOB
        use flate2::write::DeflateEncoder;
        use std::io::Write;
        let mut e = DeflateEncoder::new(Vec::new(), flate2::Compression::best());
        let mut big = MAGIC.to_vec();
        big.resize(MAX_BLOB + 4096, 0);
        e.write_all(&big).unwrap();
        let z = e.finish().unwrap();
        assert!(z.len() < MAX_STORED);
        assert!(parse(&z).unwrap_err().contains("inflates past"));
    }
}
