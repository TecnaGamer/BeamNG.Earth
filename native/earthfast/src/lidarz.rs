//! The lidar-derived road heights of a map cell as a prebuilt input ("lidarz", server/lidarz.py; built by
//! tools/build_lidarz.py on the project's own machine, 2026-11-15): exactly what server/roadz.py's RoadZ._lidar makes
//! of the USGS 3DEP point clouds for one z14 cell (its decks', elevated stretches' and approaches' measured lines and
//! the stretches' answers), so a player's server reads a few KB from the project's CDN instead of ~50 MB of EPT
//! nodes a cell. The records travel in signed z12 blocks (decisions.rs, domain "lidarz": the builder's key, the same
//! checks as the decision archives).
//!
//! A cell record (little-endian; all lengths checked against the record's own size on decode):
//!
//!     0   b"BLZC", CELL_FORMAT u8, flags u8 (1: the heights are on our DEM's datum: Lines.corrected), 0 u16
//!     8   lines u32, vertices u32, gaps u32, text bytes u32, dem calls u32, dem samples u32
//!     32  dem [32]        dem_digest of every height the builder's measuring read of our DEM (RoadZ._dem_raw), in
//!                         its order: a player whose DEM gives other heights at any of those points would have
//!                         measured otherwise, and reads the lidar itself (2: was a digest at points along the map's
//!                         lines only, which missed the datum's ground points beside them; Codex review of 206afb6f)
//!     64  lonlat          vertices x (lon, lat) f64 bit patterns, as deltas down each column in byte planes
//!                         (decisions::delta_planes: lossless, byte-exact back)
//!         z               vertices f64 (NaN: none), the same
//!         counts          lines u32: each line's vertices (in order)
//!         level           lines i8 (>0 deck, 0 ground)
//!         gaps            gaps x (from lon, from lat, to lon, to lat) f64, delta planes
//!         text            per line: ref, name; per gap: the feature id; each a u16 length and UTF-8 bytes
//!         dem calls       calls u32: each _dem_raw call's samples (in order: replayed call by call)
//!         dem samples     samples x (lat, lon) f64, delta planes: where the builder read the DEM
//!
//! A record's item key in its block is "c/<z>_<x>_<y>@<inputs digest, 16 hex>": inputs_digest of what the build
//! measured along (the cell's bridge parts and gap candidates from the map, their half widths, the 3DEP datasets
//! tried), which a player's server computes from its own map data before it reads anything; another map makes
//! another digest, and the lidar is read as before.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use sha2::{Digest, Sha256};

use crate::decisions::delta_planes;

pub const CELL_FORMAT: u8 = 2;
const CMAGIC: &[u8; 4] = b"BLZC";
const CHEAD: usize = 64;
const MAX_VERTS: usize = 1 << 24;
const MAX_LINES: usize = 1 << 22;
const MAX_GAPS: usize = 1 << 20;
const MAX_CALLS: usize = 1 << 20;
const MAX_SAMPLES: usize = 1 << 24;

fn err<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(format!("lidarz: {e}"))
}

#[derive(Debug, Clone, PartialEq)]
pub struct Cell {
    pub lonlat: Vec<[f64; 2]>,
    pub z: Vec<f64>,
    pub counts: Vec<u32>,
    pub level: Vec<i8>,
    pub refs: Vec<String>,
    pub names: Vec<String>,
    pub corrected: bool,
    pub gaps: Vec<(String, [f64; 4])>,
    pub dem: [u8; 32],
    pub calls: Vec<u32>,
    pub samples: Vec<[f64; 2]>,
}

fn f64_planes(vals: &[f64], rows: usize, cols: usize) -> Vec<u8> {
    let raw: Vec<u8> = vals.iter().flat_map(|v| v.to_bits().to_le_bytes()).collect();
    delta_planes(&raw, rows, cols, true)
}

fn f64_back(b: &[u8], rows: usize, cols: usize) -> Vec<f64> {
    delta_planes(b, rows, cols, false)
        .chunks_exact(8)
        .map(|c| f64::from_bits(u64::from_le_bytes(c.try_into().unwrap())))
        .collect()
}

fn put_text(out: &mut Vec<u8>, s: &str) -> Result<(), String> {
    if s.len() > u16::MAX as usize {
        return Err("a text over 65535 bytes".into());
    }
    out.extend_from_slice(&(s.len() as u16).to_le_bytes());
    out.extend_from_slice(s.as_bytes());
    Ok(())
}

pub fn encode_cell(c: &Cell) -> Result<Vec<u8>, String> {
    let (nv, nl, ng) = (c.lonlat.len(), c.counts.len(), c.gaps.len());
    if c.z.len() != nv {
        return Err(format!("{} heights for {nv} vertices", c.z.len()));
    }
    if c.level.len() != nl || c.refs.len() != nl || c.names.len() != nl {
        return Err(format!("{nl} lines with {} levels, {} refs, {} names", c.level.len(), c.refs.len(), c.names.len()));
    }
    if c.counts.iter().map(|&k| k as u64).sum::<u64>() != nv as u64 {
        return Err("the lines' vertex counts do not add up to the vertices".into());
    }
    let (nc, ns) = (c.calls.len(), c.samples.len());
    if nv > MAX_VERTS || nl > MAX_LINES || ng > MAX_GAPS || nc > MAX_CALLS || ns > MAX_SAMPLES {
        return Err("too large".into());
    }
    if c.calls.iter().map(|&k| k as u64).sum::<u64>() != ns as u64 {
        return Err("the dem calls' counts do not add up to the samples".into());
    }
    let mut text = Vec::new();
    for i in 0..nl {
        put_text(&mut text, &c.refs[i])?;
        put_text(&mut text, &c.names[i])?;
    }
    for (fid, _) in &c.gaps {
        put_text(&mut text, fid)?;
    }
    let mut out = Vec::with_capacity(CHEAD + nv * 24 + nl * 5 + ng * 32 + text.len() + nc * 4 + ns * 16);
    out.extend_from_slice(CMAGIC);
    out.extend_from_slice(&[CELL_FORMAT, u8::from(c.corrected), 0, 0]);
    for v in [nl, nv, ng, text.len(), nc, ns] {
        out.extend_from_slice(&(v as u32).to_le_bytes());
    }
    out.extend_from_slice(&c.dem);
    let flat: Vec<f64> = c.lonlat.iter().flat_map(|p| [p[0], p[1]]).collect();
    out.extend_from_slice(&f64_planes(&flat, nv, 2));
    out.extend_from_slice(&f64_planes(&c.z, nv, 1));
    for k in &c.counts {
        out.extend_from_slice(&k.to_le_bytes());
    }
    out.extend(c.level.iter().map(|&l| l as u8));
    let g: Vec<f64> = c.gaps.iter().flat_map(|(_, v)| *v).collect();
    out.extend_from_slice(&f64_planes(&g, ng, 4));
    out.extend_from_slice(&text);
    for k in &c.calls {
        out.extend_from_slice(&k.to_le_bytes());
    }
    let sf: Vec<f64> = c.samples.iter().flat_map(|p| [p[0], p[1]]).collect();
    out.extend_from_slice(&f64_planes(&sf, ns, 2));
    Ok(out)
}

fn u32_at(b: &[u8], p: usize) -> usize {
    u32::from_le_bytes(b[p..p + 4].try_into().unwrap()) as usize
}

fn get_text(b: &[u8], p: &mut usize) -> Result<String, String> {
    let n = b.get(*p..*p + 2).ok_or("text cut short")?;
    let n = u16::from_le_bytes([n[0], n[1]]) as usize;
    let s = b.get(*p + 2..*p + 2 + n).ok_or("text cut short")?;
    *p += 2 + n;
    String::from_utf8(s.to_vec()).map_err(|_| "a text not UTF-8".to_string())
}

pub fn decode_cell(b: &[u8]) -> Result<Cell, String> {
    if b.len() < CHEAD || &b[..4] != CMAGIC {
        return Err("not a lidar cell record".into());
    }
    if b[4] != CELL_FORMAT {
        return Err(format!("cell format {}, {} wanted", b[4], CELL_FORMAT));
    }
    if b[5] > 1 {
        return Err(format!("flags {}", b[5]));
    }
    let (nl, nv, ng, nt) = (u32_at(b, 8), u32_at(b, 12), u32_at(b, 16), u32_at(b, 20));
    let (nc, ns) = (u32_at(b, 24), u32_at(b, 28));
    if nv > MAX_VERTS || nl > MAX_LINES || ng > MAX_GAPS || nc > MAX_CALLS || ns > MAX_SAMPLES || nt > 1 << 30 {
        return Err("too large".into());
    }
    let want = CHEAD + nv * 24 + nl * 5 + ng * 32 + nt + nc * 4 + ns * 16;
    if b.len() != want {
        return Err(format!("{} bytes, {want} expected", b.len()));
    }
    let dem: [u8; 32] = b[32..64].try_into().unwrap();
    let mut p = CHEAD;
    let flat = f64_back(&b[p..p + nv * 16], nv, 2);
    p += nv * 16;
    let lonlat: Vec<[f64; 2]> = flat.chunks_exact(2).map(|c| [c[0], c[1]]).collect();
    let z = f64_back(&b[p..p + nv * 8], nv, 1);
    p += nv * 8;
    let counts: Vec<u32> = (0..nl).map(|i| u32_at(b, p + 4 * i) as u32).collect();
    p += nl * 4;
    if counts.iter().map(|&k| k as u64).sum::<u64>() != nv as u64 {
        return Err("the lines' vertex counts do not add up to the vertices".into());
    }
    let level: Vec<i8> = b[p..p + nl].iter().map(|&v| v as i8).collect();
    p += nl;
    let g = f64_back(&b[p..p + ng * 32], ng, 4);
    p += ng * 32;
    let tb = &b[p..p + nt];
    let mut q = 0usize;
    let (mut refs, mut names) = (Vec::with_capacity(nl), Vec::with_capacity(nl));
    for _ in 0..nl {
        refs.push(get_text(tb, &mut q)?);
        names.push(get_text(tb, &mut q)?);
    }
    let mut gaps = Vec::with_capacity(ng);
    for i in 0..ng {
        gaps.push((get_text(tb, &mut q)?, [g[4 * i], g[4 * i + 1], g[4 * i + 2], g[4 * i + 3]]));
    }
    if q != nt {
        return Err("text length".into());
    }
    p += nt;
    let calls: Vec<u32> = (0..nc).map(|i| u32_at(b, p + 4 * i) as u32).collect();
    p += nc * 4;
    if calls.iter().map(|&k| k as u64).sum::<u64>() != ns as u64 {
        return Err("the dem calls' counts do not add up to the samples".into());
    }
    let sf = f64_back(&b[p..p + ns * 16], ns, 2);
    let samples: Vec<[f64; 2]> = sf.chunks_exact(2).map(|c| [c[0], c[1]]).collect();
    Ok(Cell { lonlat, z, counts, level, refs, names, corrected: b[5] == 1, gaps, dem, calls, samples })
}

fn put_str(h: &mut Sha256, s: &str) {
    h.update((s.len() as u32).to_le_bytes());
    h.update(s.as_bytes());
}

/// What a cell's lidar read measures along, as one digest: the cell (z, x, y), the bridge parts (the first nb of
/// rings) and gap candidates (the rest, of features fids) with their half widths, the 3DEP datasets tried in order.
#[allow(clippy::too_many_arguments)]
pub fn inputs_digest(z: u32, x: u32, y: u32, rings: &[Vec<[f64; 2]>], halves: &[f64], nb: usize, fids: &[String],
                     names: &[String]) -> Result<[u8; 32], String> {
    if rings.len() != halves.len() || nb > rings.len() || fids.len() != rings.len() - nb {
        return Err(format!("{} parts, {} half widths, {nb} decks, {} candidates", rings.len(), halves.len(),
                           fids.len()));
    }
    let mut h = Sha256::new();
    h.update(b"BNLZ-inputs\x01");
    for v in [z, x, y, nb as u32, rings.len() as u32] {
        h.update(v.to_le_bytes());
    }
    for (ring, half) in rings.iter().zip(halves) {
        h.update((ring.len() as u32).to_le_bytes());
        for p in ring {
            h.update(p[0].to_bits().to_le_bytes());
            h.update(p[1].to_bits().to_le_bytes());
        }
        h.update(half.to_bits().to_le_bytes());
    }
    h.update((fids.len() as u32).to_le_bytes());
    for f in fids {
        put_str(&mut h, f);
    }
    h.update((names.len() as u32).to_le_bytes());
    for n in names {
        put_str(&mut h, n);
    }
    Ok(h.finalize().into())
}

/// The digest of DEM heights (every NaN the same).
pub fn dem_digest(vals: &[f64]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(b"BNLZ-dem\x01");
    h.update((vals.len() as u32).to_le_bytes());
    for v in vals {
        let bits = if v.is_nan() { 0x7ff8_0000_0000_0000u64 } else { v.to_bits() };
        h.update(bits.to_le_bytes());
    }
    h.finalize().into()
}

// --- Python ------------------------------------------------------------------------------------------------------

fn rings_of(rings: &[PyReadonlyArray2<'_, f64>]) -> Result<Vec<Vec<[f64; 2]>>, String> {
    rings
        .iter()
        .map(|r| {
            let a = r.as_array();
            if a.ncols() != 2 {
                return Err(format!("a ring of {} columns", a.ncols()));
            }
            Ok((0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect())
        })
        .collect()
}

type GapIn = (String, (f64, f64), (f64, f64));

/// A cell record (encode_cell) of a Lines' arrays (lonlat (n, 2), z (n,), off (L + 1,), level (L,), refs / names
/// (L,), corrected), its gaps [(fid, (lon, lat) from, (lon, lat) to)], the builder's dem digest (32 bytes) and where
/// it read the DEM: calls (each call's sample count), samples (m, 2) lat / lon.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn lz_encode_cell<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                           off: PyReadonlyArray1<'py, i64>, level: PyReadonlyArray1<'py, i8>, refs: Vec<String>,
                           names: Vec<String>, corrected: bool, gaps: Vec<GapIn>, dem: &[u8], calls: Vec<u32>,
                           samples: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyBytes>> {
    let sa = samples.as_array();
    if sa.ncols() != 2 {
        return Err(err("samples is not (m, 2)"));
    }
    let ll = lonlat.as_array();
    if ll.ncols() != 2 {
        return Err(err("lonlat is not (n, 2)"));
    }
    let off: Vec<i64> = off.as_array().to_vec();
    if off.is_empty() || off[0] != 0 || off.windows(2).any(|w| w[1] < w[0]) {
        return Err(err("off is not the lines' vertex ranges"));
    }
    let cell = Cell {
        lonlat: (0..ll.nrows()).map(|i| [ll[[i, 0]], ll[[i, 1]]]).collect(),
        z: z.as_array().to_vec(),
        counts: off.windows(2).map(|w| (w[1] - w[0]) as u32).collect(),
        level: level.as_array().to_vec(),
        refs,
        names,
        corrected,
        gaps: gaps.into_iter().map(|(f, a, b)| (f, [a.0, a.1, b.0, b.1])).collect(),
        dem: dem.try_into().map_err(|_| err("the dem digest is not 32 bytes"))?,
        calls,
        samples: (0..sa.nrows()).map(|i| [sa[[i, 0]], sa[[i, 1]]]).collect(),
    };
    if *off.last().unwrap() as usize != cell.lonlat.len() {
        return Err(err("off does not end at the vertices"));
    }
    let out = py.detach(|| encode_cell(&cell)).map_err(err)?;
    Ok(PyBytes::new(py, &out))
}

type Decoded<'py> = (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>,
                     Bound<'py, PyArray1<i8>>, Vec<String>, Vec<String>, bool, Vec<GapIn>, Bound<'py, PyBytes>,
                     Vec<u32>, Bound<'py, PyArray2<f64>>);

/// (lonlat, z, off, level, refs, names, corrected, gaps, dem digest, dem calls, dem samples (m, 2) lat / lon) of a
/// cell record; ValueError when damaged.
#[pyfunction]
pub fn lz_decode_cell<'py>(py: Python<'py>, buf: &[u8]) -> PyResult<Decoded<'py>> {
    let c = py.detach(|| decode_cell(buf)).map_err(err)?;
    let n = c.lonlat.len();
    let flat: Vec<f64> = c.lonlat.iter().flat_map(|p| [p[0], p[1]]).collect();
    let mut off = Vec::with_capacity(c.counts.len() + 1);
    off.push(0i64);
    for k in &c.counts {
        off.push(off.last().unwrap() + *k as i64);
    }
    let ns = c.samples.len();
    let sf: Vec<f64> = c.samples.iter().flat_map(|p| [p[0], p[1]]).collect();
    Ok((numpy::ndarray::Array2::from_shape_vec((n, 2), flat).expect("lonlat").into_pyarray(py),
        c.z.into_pyarray(py), off.into_pyarray(py), c.level.into_pyarray(py), c.refs, c.names, c.corrected,
        c.gaps.into_iter().map(|(f, v)| (f, (v[0], v[1]), (v[2], v[3]))).collect(), PyBytes::new(py, &c.dem),
        c.calls, numpy::ndarray::Array2::from_shape_vec((ns, 2), sf).expect("samples").into_pyarray(py)))
}

/// inputs_digest (32 bytes) of a cell's parts: rings [(k, 2) lon / lat], halves, nb decks first, the candidates' fids,
/// the datasets' names.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn lz_inputs_digest<'py>(py: Python<'py>, z: u32, x: u32, y: u32, rings: Vec<PyReadonlyArray2<'py, f64>>,
                             halves: Vec<f64>, nb: usize, fids: Vec<String>, names: Vec<String>)
                             -> PyResult<Bound<'py, PyBytes>> {
    let rings = rings_of(&rings).map_err(err)?;
    let d = inputs_digest(z, x, y, &rings, &halves, nb, &fids, &names).map_err(err)?;
    Ok(PyBytes::new(py, &d))
}

/// dem_digest (32 bytes) of DEM heights.
#[pyfunction]
pub fn lz_dem_digest<'py>(py: Python<'py>, vals: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyBytes> {
    let v = vals.as_array().to_vec();
    PyBytes::new(py, &dem_digest(&v))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(lz_encode_cell, m)?)?;
    m.add_function(wrap_pyfunction!(lz_decode_cell, m)?)?;
    m.add_function(wrap_pyfunction!(lz_inputs_digest, m)?)?;
    m.add_function(wrap_pyfunction!(lz_dem_digest, m)?)?;
    m.add("LZ_CELL_FORMAT", CELL_FORMAT)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Cell {
        Cell {
            lonlat: vec![[-73.99, 40.70], [-73.9899, 40.7001], [-73.9898, 40.7002], [-73.98, 40.71], [-73.9799, 40.7101]],
            z: vec![12.25, f64::NAN, 12.75, 3.0, -0.0],
            counts: vec![3, 2],
            level: vec![1, 0],
            refs: vec![String::new(), "I 78".into()],
            names: vec![String::new(), "Holland Tunnel Approach".into()],
            corrected: true,
            gaps: vec![("w123@4".into(), [-73.99, 40.70, -73.9898, 40.7002])],
            dem: [9u8; 32],
            calls: vec![2, 0, 1],
            samples: vec![[40.70, -73.99], [40.7000123, -73.9899877], [40.71, f64::NAN]],
        }
    }

    fn same(a: &Cell, b: &Cell) {
        let bits = |c: &Cell| c.z.iter().map(|v| v.to_bits()).collect::<Vec<_>>();
        assert_eq!(bits(a), bits(b));
        assert_eq!(a.lonlat.iter().map(|p| (p[0].to_bits(), p[1].to_bits())).collect::<Vec<_>>(),
                   b.lonlat.iter().map(|p| (p[0].to_bits(), p[1].to_bits())).collect::<Vec<_>>());
        assert_eq!((&a.counts, &a.level, &a.refs, &a.names, a.corrected, &a.dem, &a.calls),
                   (&b.counts, &b.level, &b.refs, &b.names, b.corrected, &b.dem, &b.calls));
        assert_eq!(a.samples.iter().map(|p| (p[0].to_bits(), p[1].to_bits())).collect::<Vec<_>>(),
                   b.samples.iter().map(|p| (p[0].to_bits(), p[1].to_bits())).collect::<Vec<_>>());
        assert_eq!(a.gaps.len(), b.gaps.len());
        for (x, y) in a.gaps.iter().zip(&b.gaps) {
            assert_eq!(x.0, y.0);
            assert_eq!(x.1.map(f64::to_bits), y.1.map(f64::to_bits));
        }
    }

    #[test]
    fn round_trip_bit_exact() {
        let c = sample();
        let b = encode_cell(&c).unwrap();
        same(&c, &decode_cell(&b).unwrap());
        assert_eq!(encode_cell(&decode_cell(&b).unwrap()).unwrap(), b, "deterministic");
        let empty = Cell { lonlat: vec![], z: vec![], counts: vec![], level: vec![], refs: vec![], names: vec![],
                           corrected: true, gaps: vec![], dem: [0u8; 32], calls: vec![], samples: vec![] };
        same(&empty, &decode_cell(&encode_cell(&empty).unwrap()).unwrap());
    }

    #[test]
    fn damaged_records_refused() {
        let b = encode_cell(&sample()).unwrap();
        assert!(decode_cell(&b[..b.len() - 1]).is_err(), "cut short");
        let mut longer = b.clone();
        longer.push(0);
        assert!(decode_cell(&longer).is_err(), "trailing bytes");
        let mut bad = b.clone();
        bad[4] = 9;
        assert!(decode_cell(&bad).is_err(), "another format");
        let mut bad = b.clone();
        bad[12..16].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(decode_cell(&bad).is_err(), "a huge count is never allocated");
        let mut bad = b.clone();
        bad[28..32].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(decode_cell(&bad).is_err(), "a huge sample count is never allocated");
        let mut c = sample();
        c.counts = vec![3, 3];
        assert!(encode_cell(&c).is_err());
        let mut c = sample();
        c.calls = vec![2, 2];
        assert!(encode_cell(&c).is_err(), "the calls must cover the samples");
    }

    #[test]
    fn digests() {
        let r = vec![vec![[-73.99, 40.70], [-73.98, 40.71]]];
        let names = vec!["NY_NYC_2014".to_string()];
        let a = inputs_digest(14, 1, 2, &r, &[5.0], 1, &[], &names).unwrap();
        assert_eq!(a, inputs_digest(14, 1, 2, &r, &[5.0], 1, &[], &names).unwrap());
        assert_ne!(a, inputs_digest(14, 1, 3, &r, &[5.0], 1, &[], &names).unwrap(), "another cell");
        assert_ne!(a, inputs_digest(14, 1, 2, &r, &[5.5], 1, &[], &names).unwrap(), "another width");
        assert_ne!(a, inputs_digest(14, 1, 2, &r, &[5.0], 0, &["f".into()], &names).unwrap(), "a candidate");
        let r2 = vec![vec![[-73.99, 40.70], [-73.98, 40.710000001]]];
        assert_ne!(a, inputs_digest(14, 1, 2, &r2, &[5.0], 1, &[], &names).unwrap(), "another line");
        assert_ne!(a, inputs_digest(14, 1, 2, &r, &[5.0], 1, &[], &[]).unwrap(), "other datasets");
        assert_eq!(dem_digest(&[f64::NAN]), dem_digest(&[-f64::NAN]));
        assert_ne!(dem_digest(&[1.0]), dem_digest(&[1.0 + 1e-12]));
    }
}
