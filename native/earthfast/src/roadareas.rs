//! OpenStreetMap's mapped road areas (area:highway=* polygons; tools/build_osm_road_areas.py, read by
//! server/osmareas.py): one zoom-14 tile's blob decoded into columns.
//!
//! Blob ("OAH1", then LEB128 varints uv, zig-zag zz): uv ndict, ndict x (uv length, UTF-8); uv n; per polygon: uv key
//! delta (ascending: each after the first at least 1; key = 2 x the OSM id, + 1 for a relation), uv value (dict
//! index), uv name / ref / surface (dict index + 1, 0 none), uv zz(layer), u8 flags, uv nlinks + nlinks x uv way id
//! deltas, uv nrings, per ring u8 role, uv count, count x 2 uv zz(delta of lon / lat x 1e7). Stored as raw deflate
//! (the builder's), or plain (starting with the magic).
//!
//! The tiles come from the network: every count is checked against the bytes left before anything is allocated for
//! it, the inflated size (buildyears::MAX_BLOB), the polygons (MAX_POLYS), rings (MAX_RINGS) and points (MAX_POINTS)
//! are bounded, and a bad tile is an error, never a panic.

use numpy::{IntoPyArray, PyArray1, PyArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub const MAGIC: &[u8; 4] = b"OAH1";
// The planet build of 2026-11-23 (74,481 tiles, 522,779 rows, 54 MB): bounds far past its densest tile (Wroclaw).
pub const MAX_POLYS: u64 = 200_000;
pub const MAX_RINGS: u64 = 400_000;
pub const MAX_POINTS: u64 = 8_000_000;
pub const MAX_DICT: u64 = 400_000;
pub const MAX_LINKS: u64 = 100_000;

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
    fn zz(&mut self) -> Result<i64, String> {
        let v = self.uv()?;
        Ok((v >> 1) as i64 ^ -((v & 1) as i64))
    }
    fn u8(&mut self) -> Result<u8, String> {
        let c = *self.b.get(self.i).ok_or("truncated")?;
        self.i += 1;
        Ok(c)
    }
    fn left(&self) -> usize {
        self.b.len() - self.i
    }
    fn count(&mut self, max: u64, each: usize, what: &str) -> Result<usize, String> {
        let n = self.uv()?;
        if n > max || n.saturating_mul(each as u64) > self.left() as u64 {
            return Err(format!("{n} {what}, past the tile's bytes or the {max} a tile may have"));
        }
        Ok(n as usize)
    }
}

/// A tile's columns.
#[derive(Default, Debug)]
pub struct Tile {
    pub keys: Vec<u64>,
    pub value: Vec<u32>,             // dict index
    pub name: Vec<i64>,              // dict index, -1 none
    pub layer: Vec<i64>,
    pub flags: Vec<u8>,
    pub link_off: Vec<i64>,          // n + 1
    pub links: Vec<u64>,
    pub ring_poly: Vec<i64>,
    pub ring_role: Vec<u8>,
    pub pt_off: Vec<i64>,            // rings + 1
    pub pts: Vec<[f64; 2]>,          // lon, lat (the stored 1e-7 integers / 1e7)
    pub dict: Vec<String>,
}

/// A tile's bytes as stored decoded.
pub fn parse(raw: &[u8]) -> Result<Tile, String> {
    let b = crate::buildyears::inflate_magic(raw, MAGIC)?;
    if !b.starts_with(MAGIC) {
        return Err("not an OAH1 tile".into());
    }
    let mut r = R { b: &b, i: MAGIC.len() };
    let mut t = Tile::default();
    let nd = r.count(MAX_DICT, 1, "texts")?;
    for _ in 0..nd {
        let n = r.count(u64::MAX, 1, "bytes of a text")?;
        let s = std::str::from_utf8(&r.b[r.i..r.i + n]).map_err(|_| "a text not UTF-8")?;
        t.dict.push(s.to_string());
        r.i += n;
    }
    let n = r.count(MAX_POLYS, 9, "polygons")?;
    let mut acc: u64 = 0;
    let (mut nrings, mut npts) = (0u64, 0u64);
    t.link_off.push(0);
    t.pt_off.push(0);
    for k in 0..n {
        let d = r.uv()?;
        if k > 0 && d == 0 {
            return Err("keys not ascending".into());
        }
        acc = acc.checked_add(d).ok_or("key past 64 bits")?;
        t.keys.push(acc);
        let v = r.uv()?;
        if v >= nd as u64 {
            return Err("a value past the texts".into());
        }
        t.value.push(v as u32);
        let mut texts = [0u64; 3];
        for x in texts.iter_mut() {
            *x = r.uv()?;
            if *x > nd as u64 {
                return Err("a text index past the texts".into());
            }
        }
        t.name.push(texts[0] as i64 - 1);
        t.layer.push(r.zz()?);
        t.flags.push(r.u8()?);
        let nl = r.count(MAX_LINKS, 1, "links")?;
        let mut la: u64 = 0;
        for _ in 0..nl {
            la = la.checked_add(r.uv()?).ok_or("link past 64 bits")?;
            t.links.push(la);
        }
        t.link_off.push(t.links.len() as i64);
        let nr = r.count(MAX_RINGS, 2, "rings")?;
        nrings += nr as u64;
        if nrings > MAX_RINGS {
            return Err(format!("{nrings} rings, past the {MAX_RINGS} a tile may have"));
        }
        for _ in 0..nr {
            let role = r.u8()?;
            let c = r.count(MAX_POINTS, 2, "points")?;
            npts += c as u64;
            if npts > MAX_POINTS {
                return Err(format!("{npts} points, past the {MAX_POINTS} a tile may have"));
            }
            let (mut x, mut y) = (0i64, 0i64);
            for _ in 0..c {
                x = x.checked_add(r.zz()?).ok_or("a point out of range")?;
                y = y.checked_add(r.zz()?).ok_or("a point out of range")?;
                if x.abs() > 1_800_000_000 || y.abs() > 900_000_000 {
                    return Err("a point off the globe".into());
                }
                t.pts.push([x as f64 / 1e7, y as f64 / 1e7]);
            }
            t.ring_poly.push(k as i64);
            t.ring_role.push(if role != 0 { 1 } else { 0 });
            t.pt_off.push(t.pts.len() as i64);
        }
    }
    Ok(t)
}

/// One OSM road-area tile (as stored: raw deflate or plain) decoded: (keys uint64 ascending, values [str], names [str
/// or None], layers int64, flags int64, link_off int64 (n + 1), links uint64, ring_poly int64, ring_role int64,
/// pt_off int64 (rings + 1), points float64 (k, 2) lon / lat).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn oah_decode<'py>(py: Python<'py>, data: &[u8])
                       -> PyResult<(Py<PyAny>, Vec<String>, Vec<Option<String>>, Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>,
                                    Py<PyAny>, Py<PyAny>, Py<PyAny>)> {
    if data.len() > crate::buildyears::MAX_STORED {
        return Err(PyValueError::new_err(format!("tile of {} bytes stored, past {}", data.len(), crate::buildyears::MAX_STORED)));
    }
    let raw = data.to_vec();
    let t = py.detach(move || parse(&raw)).map_err(PyValueError::new_err)?;
    let values: Vec<String> = t.value.iter().map(|&v| t.dict[v as usize].clone()).collect();
    let names: Vec<Option<String>> = t.name.iter().map(|&v| if v >= 0 { Some(t.dict[v as usize].clone()) } else { None }).collect();
    let n = t.pts.len();
    let flat: Vec<f64> = t.pts.iter().flat_map(|p| [p[0], p[1]]).collect();
    let pts = PyArray1::from_vec(py, flat).reshape([n, 2])?;
    Ok((t.keys.into_pyarray(py).into_any().unbind(), values, names, t.layer.into_pyarray(py).into_any().unbind(),
        t.flags.iter().map(|&f| f as i64).collect::<Vec<i64>>().into_pyarray(py).into_any().unbind(),
        t.link_off.into_pyarray(py).into_any().unbind(), t.links.into_pyarray(py).into_any().unbind(),
        t.ring_poly.into_pyarray(py).into_any().unbind(),
        t.ring_role.iter().map(|&f| f as i64).collect::<Vec<i64>>().into_pyarray(py).into_any().unbind(),
        t.pt_off.into_pyarray(py).into_any().unbind(), pts.into_any().unbind()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(oah_decode, m)?)?;
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
    fn zz(v: i64, out: &mut Vec<u8>) {
        uv(((v << 1) ^ (v >> 63)) as u64, out)
    }

    fn blob() -> Vec<u8> {
        let mut b = MAGIC.to_vec();
        uv(2, &mut b);
        for s in ["primary", "Main St"] {
            uv(s.len() as u64, &mut b);
            b.extend_from_slice(s.as_bytes());
        }
        uv(1, &mut b);
        uv(2 * 77 + 1, &mut b); // relation 77
        uv(0, &mut b); // value: "primary"
        uv(2, &mut b); // name: "Main St"
        uv(0, &mut b);
        uv(0, &mut b);
        zz(-1, &mut b);
        b.push(1);
        uv(1, &mut b);
        uv(5, &mut b); // a link: way 5
        uv(2, &mut b);
        for (role, pts) in [(0u8, vec![(10i64, 20i64), (30, 20), (30, 40), (10, 20)]), (1, vec![(15, 22), (20, 22), (20, 25), (15, 22)])] {
            b.push(role);
            uv(pts.len() as u64, &mut b);
            let (mut px, mut py) = (0, 0);
            for (x, y) in pts {
                zz(x - px, &mut b);
                zz(y - py, &mut b);
                px = x;
                py = y;
            }
        }
        b
    }

    #[test]
    fn round_trip() {
        let t = parse(&blob()).unwrap();
        assert_eq!(t.keys, vec![155]);
        assert_eq!(t.dict[t.value[0] as usize], "primary");
        assert_eq!(t.name, vec![1]);
        assert_eq!((t.layer[0], t.flags[0]), (-1, 1));
        assert_eq!(t.links, vec![5]);
        assert_eq!(t.ring_role, vec![0, 1]);
        assert_eq!(t.pt_off, vec![0, 4, 8]);
        assert_eq!(t.pts[2], [30.0 / 1e7, 40.0 / 1e7]);
    }

    #[test]
    fn bad_tiles_are_errors() {
        assert!(parse(b"").is_err());
        assert!(parse(b"OAH1").is_err());
        let mut b = blob();
        b.truncate(b.len() - 1);
        assert!(parse(&b).is_err());
        let mut b = MAGIC.to_vec();
        uv(0, &mut b);
        uv(1 << 40, &mut b); // polygons far past the bytes
        assert!(parse(&b).is_err());
        let mut b = MAGIC.to_vec();
        uv(0, &mut b);
        uv(1, &mut b);
        uv(4, &mut b);
        uv(3, &mut b); // a value index past an empty dict
        b.extend_from_slice(&[0; 16]);
        assert!(parse(&b).is_err());
    }
}
