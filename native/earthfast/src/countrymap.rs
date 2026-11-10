//! Which country (or region) a point lies in, from polygon borders (server/countrybounds.py is the glue; PR #505's
//! regional scenery picks its poles, delineators, signs and STOP lettering by the country at each object).
//!
//! The file (tools/build_country_bands.py writes it from the source polygons): the rings' edges, cut into thin
//! latitude bands (BANDS_PER_DEG per degree), each band one zstd frame of runs of consecutive edges of one ring. A
//! point's horizontal ray only ever crosses edges whose latitude range holds the point, so a band alone answers
//! "inside which rings" (crossing parity): a process decodes only the bands its points fall in (a drive touches a
//! handful), and keeps at most CACHE_BANDS of them. No other ring data is needed: a ring with no edge in the band
//! cannot hold the point.
//!
//! Layout (little-endian):
//!   b"BNECB\x01\0\0", u32 bands_per_deg, u32 ncodes, u32 nrings, u32 nparts
//!   ncodes x [u8; 8]  (ASCII, NUL-padded: "FR", "CA-QC")
//!   nrings x (u32 part, u8 hole, u8 pad[3], f32 centre_lon)
//!   nparts x u16 code
//!   180 * bands_per_deg x (u64 offset, u32 clen, u32 nruns)   (band k: latitudes [-90 + k/B, -90 + (k+1)/B))
//!   frames: zstd of nruns x (u32 ring, u32 npts, npts x (i32 dlon, i32 dlat) microdegrees, deltas from the
//!           previous point of the run, the first from 0)
//! Longitudes are unwrapped per ring (may pass +-180); a query is moved to within 180 of the ring's centre_lon.
//!
//! The answer: the code of the one part (outer ring minus its holes) holding the point, else 0 ("unknown": outside
//! all, or inside parts of two different codes where the sources overlap).

use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::sync::{Arc, Mutex, OnceLock};

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const MAGIC: &[u8; 8] = b"BNECB\x01\0\0";
const CACHE_BANDS: usize = 96;

struct Run { ring: u32, pts: Vec<(i32, i32)> }

struct Map {
    path: String,
    bpd: u32,
    codes: Vec<String>,
    ring_part: Vec<u32>,
    ring_hole: Vec<bool>,
    ring_centre: Vec<f64>,
    part_code: Vec<u16>,
    bands: Vec<(u64, u32, u32)>,
    cache: Mutex<(HashMap<u32, Arc<Vec<Run>>>, Vec<u32>)>,   // decoded bands, their use order (oldest first)
}

fn rd_u32(b: &[u8], o: &mut usize) -> u32 { let v = u32::from_le_bytes(b[*o..*o + 4].try_into().unwrap()); *o += 4; v }

impl Map {
    fn open(path: &str) -> Result<Map, String> {
        let mut f = File::open(path).map_err(|e| format!("{path}: {e}"))?;
        let mut head = [0u8; 24];
        f.read_exact(&mut head).map_err(|e| e.to_string())?;
        if &head[..8] != MAGIC { return Err(format!("{path}: not a country band file")); }
        let mut o = 8;
        let bpd = rd_u32(&head, &mut o);
        let ncodes = rd_u32(&head, &mut o) as usize;
        let nrings = rd_u32(&head, &mut o) as usize;
        let nparts = rd_u32(&head, &mut o) as usize;
        if bpd == 0 || bpd > 1000 { return Err("bad band count".into()); }
        let nbands = 180 * bpd as usize;
        let mut rest = vec![0u8; ncodes * 8 + nrings * 12 + nparts * 2 + nbands * 16];
        f.read_exact(&mut rest).map_err(|e| e.to_string())?;
        let mut o = 0;
        let codes = (0..ncodes).map(|i| {
            let s = &rest[i * 8..i * 8 + 8];
            String::from_utf8_lossy(&s[..s.iter().position(|&c| c == 0).unwrap_or(8)]).into_owned()
        }).collect();
        o += ncodes * 8;
        let (mut ring_part, mut ring_hole, mut ring_centre) = (Vec::with_capacity(nrings), Vec::with_capacity(nrings), Vec::with_capacity(nrings));
        for _ in 0..nrings {
            ring_part.push(rd_u32(&rest, &mut o));
            ring_hole.push(rest[o] != 0);
            o += 4;
            ring_centre.push(f32::from_le_bytes(rest[o..o + 4].try_into().unwrap()) as f64);
            o += 4;
        }
        let part_code = (0..nparts).map(|i| u16::from_le_bytes(rest[o + i * 2..o + i * 2 + 2].try_into().unwrap())).collect();
        o += nparts * 2;
        let mut bands = Vec::with_capacity(nbands);
        for _ in 0..nbands {
            let off = u64::from_le_bytes(rest[o..o + 8].try_into().unwrap());
            o += 8;
            let clen = rd_u32(&rest, &mut o);
            let nruns = rd_u32(&rest, &mut o);
            bands.push((off, clen, nruns));
        }
        Ok(Map { path: path.to_owned(), bpd, codes, ring_part, ring_hole, ring_centre, part_code, bands,
                 cache: Mutex::new((HashMap::new(), Vec::new())) })
    }

    fn band(&self, k: u32) -> Result<Arc<Vec<Run>>, String> {
        {
            let mut c = self.cache.lock().unwrap();
            if let Some(b) = c.0.get(&k).cloned() {
                c.1.retain(|&x| x != k);
                c.1.push(k);
                return Ok(b);
            }
        }
        let (off, clen, nruns) = self.bands[k as usize];
        let mut runs = Vec::with_capacity(nruns as usize);
        if nruns > 0 {
            let mut f = File::open(&self.path).map_err(|e| e.to_string())?;
            f.seek(SeekFrom::Start(off)).map_err(|e| e.to_string())?;
            let mut z = vec![0u8; clen as usize];
            f.read_exact(&mut z).map_err(|e| e.to_string())?;
            let raw = zstd::stream::decode_all(&z[..]).map_err(|e| e.to_string())?;
            let mut o = 0;
            for _ in 0..nruns {
                if o + 8 > raw.len() { return Err("truncated band".into()); }
                let ring = rd_u32(&raw, &mut o);
                let n = rd_u32(&raw, &mut o) as usize;
                if ring as usize >= self.ring_part.len() || o + n * 8 > raw.len() { return Err("bad band run".into()); }
                let (mut x, mut y) = (0i32, 0i32);
                let mut pts = Vec::with_capacity(n);
                for _ in 0..n {
                    x = x.wrapping_add(i32::from_le_bytes(raw[o..o + 4].try_into().unwrap()));
                    y = y.wrapping_add(i32::from_le_bytes(raw[o + 4..o + 8].try_into().unwrap()));
                    o += 8;
                    pts.push((x, y));
                }
                runs.push(Run { ring, pts });
            }
        }
        let b = Arc::new(runs);
        let mut c = self.cache.lock().unwrap();
        c.0.insert(k, b.clone());
        c.1.push(k);
        while c.1.len() > CACHE_BANDS { let old = c.1.remove(0); c.0.remove(&old); }
        Ok(b)
    }

    /// The code index (1-based into codes; 0 = unknown) at (lat, lon) in degrees.
    fn at(&self, lat: f64, lon: f64) -> Result<u16, String> {
        if !(lat.is_finite() && lon.is_finite()) || !(-90.0..90.0).contains(&lat) { return Ok(0); }
        let k = ((lat + 90.0) * self.bpd as f64).floor() as u32;
        let runs = self.band(k)?;
        let y = lat * 1e6;
        let mut parity: HashMap<u32, bool> = HashMap::new();   // ring -> odd crossings
        for r in runs.iter() {
            let c = self.ring_centre[r.ring as usize];
            let x = (lon + 360.0 * ((c - lon) / 360.0).round()) * 1e6;
            let mut n = 0u32;
            for w in r.pts.windows(2) {
                let (x0, y0) = (w[0].0 as f64, w[0].1 as f64);
                let (x1, y1) = (w[1].0 as f64, w[1].1 as f64);
                if (y0 > y) != (y1 > y) && x0 + (y - y0) * (x1 - x0) / (y1 - y0) > x { n += 1; }
            }
            if n % 2 == 1 { let e = parity.entry(r.ring).or_insert(false); *e = !*e; }
        }
        // per part: inside its outer ring and none of its holes
        let mut part_in: HashMap<u32, (bool, bool)> = HashMap::new();
        for (&ring, &odd) in &parity {
            if !odd { continue; }
            let e = part_in.entry(self.ring_part[ring as usize]).or_insert((false, false));
            if self.ring_hole[ring as usize] { e.1 = true } else { e.0 = true }
        }
        let mut found = 0u16;
        for (&part, &(outer, hole)) in &part_in {
            if !outer || hole { continue; }
            let code = self.part_code[part as usize] + 1;
            if found != 0 && found != code { return Ok(0); }
            found = code;
        }
        Ok(found)
    }
}

fn maps() -> &'static Mutex<HashMap<String, Arc<Map>>> {
    static M: OnceLock<Mutex<HashMap<String, Arc<Map>>>> = OnceLock::new();
    M.get_or_init(|| Mutex::new(HashMap::new()))
}

fn map(path: &str) -> PyResult<Arc<Map>> {
    let mut m = maps().lock().unwrap();
    if let Some(x) = m.get(path) { return Ok(x.clone()); }
    let x = Arc::new(Map::open(path).map_err(PyValueError::new_err)?);
    m.insert(path.to_owned(), x.clone());
    Ok(x)
}

/// The codes of a band file (index i is code i + 1 of country_index; 0 = unknown).
#[pyfunction]
pub fn country_codes(path: &str) -> PyResult<Vec<String>> { Ok(map(path)?.codes.clone()) }

/// Per point (lats, lons in degrees): its code's index + 1 into country_codes(path), 0 where unknown.
#[pyfunction]
pub fn country_index<'py>(py: Python<'py>, path: &str, lats: PyReadonlyArray1<'py, f64>, lons: PyReadonlyArray1<'py, f64>)
        -> PyResult<Bound<'py, PyArray1<u16>>> {
    let m = map(path)?;
    let (la, lo) = (lats.as_array().to_vec(), lons.as_array().to_vec());
    if la.len() != lo.len() { return Err(PyValueError::new_err("lats and lons differ in length")); }
    let out: Result<Vec<u16>, String> = py.allow_threads(|| la.iter().zip(&lo).map(|(&a, &b)| m.at(a, b)).collect());
    Ok(out.map_err(PyValueError::new_err)?.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(path: &str, rings: &[(u32, bool, Vec<(f64, f64)>)], part_codes: &[u16], codes: &[&str], bpd: u32) {
        let nb = 180 * bpd as usize;
        let mut bands: Vec<Vec<(u32, Vec<(i32, i32)>)>> = vec![Vec::new(); nb];
        for (ri, (_, _, pts)) in rings.iter().enumerate() {
            let p: Vec<(i32, i32)> = pts.iter().map(|&(x, y)| ((x * 1e6).round() as i32, (y * 1e6).round() as i32)).collect();
            for e in 0..p.len() - 1 {
                let (lo, hi) = (p[e].1.min(p[e + 1].1) as f64 / 1e6, p[e].1.max(p[e + 1].1) as f64 / 1e6);
                for k in ((lo + 90.0) * bpd as f64).floor() as usize..=((hi + 90.0) * bpd as f64).floor() as usize {
                    bands[k].push((ri as u32, vec![p[e], p[e + 1]]));
                }
            }
        }
        let mut head = MAGIC.to_vec();
        for v in [bpd, codes.len() as u32, rings.len() as u32, part_codes.len() as u32] { head.extend(v.to_le_bytes()); }
        for c in codes { let mut b = [0u8; 8]; b[..c.len()].copy_from_slice(c.as_bytes()); head.extend(b); }
        for (part, hole, pts) in rings {
            head.extend(part.to_le_bytes()); head.extend([*hole as u8, 0, 0, 0]);
            let c = pts.iter().map(|p| p.0).sum::<f64>() / pts.len() as f64;
            head.extend((c as f32).to_le_bytes());
        }
        for c in part_codes { head.extend(c.to_le_bytes()); }
        let table_at = head.len();
        head.extend(vec![0u8; nb * 16]);
        for (k, b) in bands.iter().enumerate() {
            let mut raw = Vec::new();
            for (ring, pts) in b {
                raw.extend(ring.to_le_bytes()); raw.extend((pts.len() as u32).to_le_bytes());
                let (mut px, mut py) = (0i32, 0i32);
                for &(x, y) in pts { raw.extend((x - px).to_le_bytes()); raw.extend((y - py).to_le_bytes()); px = x; py = y; }
            }
            let z = zstd::stream::encode_all(&raw[..], 3).unwrap();
            let off = head.len() as u64;
            head[table_at + k * 16..table_at + k * 16 + 8].copy_from_slice(&off.to_le_bytes());
            head[table_at + k * 16 + 8..table_at + k * 16 + 12].copy_from_slice(&(z.len() as u32).to_le_bytes());
            head[table_at + k * 16 + 12..table_at + k * 16 + 16].copy_from_slice(&(b.len() as u32).to_le_bytes());
            head.extend(z);
        }
        std::fs::write(path, head).unwrap();
    }

    fn sq(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<(f64, f64)> { vec![(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)] }

    #[test]
    fn parts_holes_overlaps_and_the_date_line() {
        let path = std::env::temp_dir().join(format!("cm_test_{}.bin", std::process::id()));
        let path = path.to_str().unwrap();
        // part 0 "AA": a square with a hole; part 1 "BB": the hole's island (an enclave); part 2 "CC" overlapping AA's
        // east edge; part 3 "DD" across the date line (lon 179 .. 181 = -179)
        let rings = vec![
            (0, false, sq(0.0, 0.0, 2.0, 2.0)), (0, true, sq(0.5, 0.5, 1.0, 1.0)),
            (1, false, sq(0.6, 0.6, 0.9, 0.9)),
            (2, false, sq(1.8, 0.0, 3.0, 2.0)),
            (3, false, sq(179.0, 10.0, 181.0, 11.0)),
        ];
        write(path, &rings, &[0, 1, 2, 3], &["AA", "BB", "CC", "DD"], 16);
        let m = Map::open(path).unwrap();
        let code = |lat: f64, lon: f64| { let c = m.at(lat, lon).unwrap(); if c == 0 { "-".to_string() } else { m.codes[c as usize - 1].clone() } };
        assert_eq!(code(0.3, 0.3), "AA");
        assert_eq!(code(0.55, 0.55), "-");      // in the hole, outside the island
        assert_eq!(code(0.7, 0.7), "BB");       // the enclave
        assert_eq!(code(1.0, 1.9), "-");        // AA and CC overlap: unknown
        assert_eq!(code(1.0, 2.5), "CC");
        assert_eq!(code(1.0, 5.0), "-");
        assert_eq!(code(10.5, 179.5), "DD");
        assert_eq!(code(10.5, -179.5), "DD");   // past the date line
        assert_eq!(code(10.5, -178.5), "-");
        assert_eq!(code(f64::NAN, 0.0), "-");
        std::fs::remove_file(path).ok();
    }
}
