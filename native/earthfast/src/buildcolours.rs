//! OpenStreetMap's mapped colours of buildings and building parts (tools/build_osm_building_colours.py, read by
//! server/osmcolours.py): one zoom-12 tile's blob decoded into its rows' keys and facade / roof colours.
//!
//! Blob ("OBC1", then LEB128 varints): uv n; n x uv key deltas (the first from 0; each later one at least 1: the keys
//! ascend, one row a key); n flag bytes (FACADE 1, ROOF 2, no other bit); uv m; the palette, m x 3 bytes r, g, b
//! (ascending as 0xRRGGBB, each once); then for every row with FACADE, in key order, uv its palette index, then
//! likewise for ROOF. A key is 2 x the OSM id, + 1 for a relation. Stored as raw deflate (the builder's), or plain
//! (starting with the magic). Bytes after the roof indices are left for later sections.
//!
//! The tiles come from the network: every count is checked against the bytes left, every index against the palette,
//! the inflated size is capped (buildyears' MAX_BLOB), the rows (buildyears' MAX_ROWS) and what they and the palette
//! decode to (MAX_DECODED) are bounded before anything is allocated for them, a row with neither colour is refused,
//! and a bad tile is an error, never a panic (Codex review of 9f1bcf84: a 62 KB deflated tile of 32 M rows of one-byte
//! deltas and no colours passed every check and asked for 512 MB). Manhattan's densest z12 tile has 9,514 rows.

use numpy::IntoPyArray;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::buildyears;

pub const MAGIC: &[u8; 4] = b"OBC1";
const FACADE: u8 = 1;
const ROOF: u8 = 2;
const ROW_BYTES: u64 = 8 + 4 + 4; // a row decoded: its key, facade and roof
const PAL_BYTES: u64 = 4; // a palette colour decoded

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
    fn bytes(&mut self, n: usize) -> Result<&'a [u8], String> {
        if n > self.left() {
            return Err(format!("{n} bytes past the tile's {} left", self.left()));
        }
        let s = &self.b[self.i..self.i + n];
        self.i += n;
        Ok(s)
    }
}

/// (keys ascending, facade, roof: 0xRRGGBB or -1 where none) of a tile's bytes as stored.
pub fn parse(raw: &[u8]) -> Result<(Vec<u64>, Vec<i32>, Vec<i32>), String> {
    let b = buildyears::inflate_magic(raw, MAGIC)?;
    if !b.starts_with(MAGIC) {
        return Err("not an OBC1 tile".into());
    }
    let mut r = R { b: &b, i: MAGIC.len() };
    let n = r.uv()?;
    if n > buildyears::MAX_ROWS || n.saturating_mul(ROW_BYTES) > buildyears::MAX_DECODED {
        return Err(format!("{n} rows, past the {} a tile may have", buildyears::MAX_ROWS));
    }
    // (each row: a key delta byte, a flag byte and a palette index byte at least)
    if n > (r.left() / 3) as u64 {
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
    let flags = r.bytes(n)?;
    if let Some(f) = flags.iter().find(|&&f| f & !(FACADE | ROOF) != 0 || f == 0) {
        return Err(if *f == 0 { "a row with no colour".to_string() } else { format!("unknown flags {f:#x}") });
    }
    let m = r.uv()?;
    // (each colour used by a row at least: 2 a row at most)
    let decoded = (n as u64 * ROW_BYTES).saturating_add(m.saturating_mul(PAL_BYTES));
    if m > (r.left() / 3) as u64 || m > 2 * n as u64 || decoded > buildyears::MAX_DECODED {
        return Err(format!("a palette of {m} past the tile's {} bytes or its {n} rows", r.left()));
    }
    let pal: Vec<i32> = r
        .bytes(3 * m as usize)?
        .chunks_exact(3)
        .map(|c| ((c[0] as i32) << 16) | ((c[1] as i32) << 8) | c[2] as i32)
        .collect();
    let mut col = |bit: u8| -> Result<Vec<i32>, String> {
        let mut out = vec![-1i32; n];
        for (k, &f) in flags.iter().enumerate() {
            if f & bit != 0 {
                let j = r.uv()?;
                out[k] = *pal
                    .get(usize::try_from(j).map_err(|_| "palette index past the platform")?)
                    .ok_or_else(|| format!("palette index {j} of {}", pal.len()))?;
            }
        }
        Ok(out)
    };
    let facade = col(FACADE)?;
    let roof = col(ROOF)?;
    Ok((keys, facade, roof))
}

/// One OSM building-colour tile (as stored: raw deflate or plain) decoded: (keys uint64 ascending, facade int32,
/// roof int32: 0xRRGGBB, -1 where the row has none).
#[pyfunction]
pub fn obc_decode<'py>(py: Python<'py>, data: &[u8]) -> PyResult<(Py<PyAny>, Py<PyAny>, Py<PyAny>)> {
    if data.len() > buildyears::MAX_STORED {
        return Err(PyValueError::new_err(format!(
            "tile of {} bytes stored, past {}",
            data.len(),
            buildyears::MAX_STORED
        )));
    }
    let raw = data.to_vec();
    let (k, f, r) = py.detach(move || parse(&raw)).map_err(PyValueError::new_err)?;
    Ok((
        k.into_pyarray(py).into_any().unbind(),
        f.into_pyarray(py).into_any().unbind(),
        r.into_pyarray(py).into_any().unbind(),
    ))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(obc_decode, m)?)?;
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

    /// rows (key, facade, roof) ascending by key, -1 none
    fn blob(rows: &[(u64, i32, i32)]) -> Vec<u8> {
        let mut pal: Vec<i32> = rows.iter().flat_map(|r| [r.1, r.2]).filter(|&c| c >= 0).collect();
        pal.sort();
        pal.dedup();
        let mut b = MAGIC.to_vec();
        uv(rows.len() as u64, &mut b);
        let mut prev = 0;
        for r in rows {
            uv(r.0 - prev, &mut b);
            prev = r.0;
        }
        for r in rows {
            b.push((r.1 >= 0) as u8 * FACADE + (r.2 >= 0) as u8 * ROOF);
        }
        uv(pal.len() as u64, &mut b);
        for c in &pal {
            b.extend_from_slice(&[(c >> 16) as u8, (c >> 8) as u8, *c as u8]);
        }
        for col in [1, 2] {
            for r in rows {
                let c = if col == 1 { r.1 } else { r.2 };
                if c >= 0 {
                    uv(pal.binary_search(&c).unwrap() as u64, &mut b);
                }
            }
        }
        b
    }

    #[test]
    fn round_trip() {
        let rows = [
            (2 * 703143170, -1, 0x006400),
            (2 * 9771573 + 1, 0xf5f5dc, -1),
            (2 * 34633854, 0xf5f5dc, 0xd3d3d3),
            (u64::MAX / 2, 0x000000, 0xffffff),
        ];
        let mut rows = rows.to_vec();
        rows.sort();
        let (k, f, r) = parse(&blob(&rows)).unwrap();
        assert_eq!(k, rows.iter().map(|r| r.0).collect::<Vec<_>>());
        assert_eq!(f, rows.iter().map(|r| r.1).collect::<Vec<_>>());
        assert_eq!(r, rows.iter().map(|r| r.2).collect::<Vec<_>>());
    }

    #[test]
    fn deflated_and_empty() {
        use flate2::write::DeflateEncoder;
        use std::io::Write;
        let b = blob(&[(4, 0x123456, -1), (10, -1, 0x654321)]);
        let mut e = DeflateEncoder::new(Vec::new(), flate2::Compression::default());
        e.write_all(&b).unwrap();
        let (k, f, r) = parse(&e.finish().unwrap()).unwrap();
        assert_eq!((k, f, r), (vec![4, 10], vec![0x123456, -1], vec![-1, 0x654321]));
        let (k, f, r) = parse(&blob(&[])).unwrap();
        assert!(k.is_empty() && f.is_empty() && r.is_empty());
    }

    fn deflate(b: &[u8]) -> Vec<u8> {
        use flate2::write::DeflateEncoder;
        use std::io::Write;
        let mut e = DeflateEncoder::new(Vec::new(), flate2::Compression::best());
        e.write_all(b).unwrap();
        e.finish().unwrap()
    }

    #[test]
    fn allocation_bombs_are_refused_before_allocating() {
        // the review's bomb: 32 M rows of one-byte deltas, no colours, an empty palette (~62 KB deflated)
        let n: usize = 32 << 20;
        let mut big = MAGIC.to_vec();
        uv(n as u64, &mut big);
        big.resize(big.len() + n, 1);
        big.resize(big.len() + n, 0);
        uv(0, &mut big);
        let z = deflate(&big);
        drop(big);
        assert!(z.len() < 200_000, "{}", z.len());
        assert!(parse(&z).unwrap_err().contains("inflates past"));
        // MAX_ROWS + 1 rows with the bytes for every row, inside MAX_BLOB: refused by the count
        let rows = |n: usize| {
            let mut b = MAGIC.to_vec();
            uv(n as u64, &mut b);
            b.resize(b.len() + n, 1); // (deltas)
            b.resize(b.len() + n, FACADE);
            uv(1, &mut b);
            b.extend_from_slice(&[1, 2, 3]);
            b.resize(b.len() + n, 0); // (palette indices)
            b
        };
        let over = rows(buildyears::MAX_ROWS as usize + 1);
        assert!(over.len() < buildyears::MAX_BLOB);
        assert!(parse(&deflate(&over)).unwrap_err().contains("rows, past"));
        let (k, f, r) = parse(&deflate(&rows(buildyears::MAX_ROWS as usize))).unwrap();
        assert_eq!((k.len(), f[0], r[0]), (buildyears::MAX_ROWS as usize, 0x010203, -1), "MAX_ROWS decodes");
        assert!(buildyears::MAX_ROWS * ROW_BYTES <= buildyears::MAX_DECODED);
        // rows with neither colour
        let mut b = MAGIC.to_vec();
        uv(3, &mut b);
        b.extend_from_slice(&[1, 1, 1, 0, 0, 0]);
        uv(0, &mut b);
        b.extend_from_slice(&[0, 0, 0]);
        assert!(parse(&b).unwrap_err().contains("no colour"));
        // a palette past twice the rows
        let mut b = MAGIC.to_vec();
        uv(1, &mut b);
        b.extend_from_slice(&[1, FACADE]);
        uv(3, &mut b);
        b.extend_from_slice(&[0; 9]);
        b.push(0);
        assert!(parse(&b).unwrap_err().contains("palette of 3"));
    }

    #[test]
    fn bad_tiles_are_errors() {
        assert!(parse(b"").is_err());
        assert!(parse(b"OBC1").is_err());
        assert!(parse(b"OBY1\x00\x00").is_err(), "another dataset's tile");
        let good = blob(&[(4, 0x123456, -1), (10, -1, 0x654321)]);
        for cut in 1..good.len() - MAGIC.len() {
            assert!(parse(&good[..good.len() - cut]).is_err(), "cut by {cut}");
        }
        // a row count far past the bytes
        let mut b = MAGIC.to_vec();
        uv(1 << 40, &mut b);
        assert!(parse(&b).is_err());
        // keys not ascending
        let mut b = good.clone();
        b[6] = 0; // (the second key's delta)
        assert!(parse(&b).is_err());
        // an unknown flag
        let mut b = good.clone();
        b[7] |= 4; // (the first row's flags)
        assert!(parse(&b).unwrap_err().contains("flags"));
        // a palette index past the palette
        let mut b = good.clone();
        let last = b.len() - 1;
        b[last] = 2;
        assert!(parse(&b).unwrap_err().contains("palette index"));
        // a palette longer than the bytes
        let mut b = MAGIC.to_vec();
        uv(1, &mut b);
        uv(4, &mut b);
        b.push(FACADE);
        uv(1 << 30, &mut b);
        assert!(parse(&b).is_err());
    }
}
