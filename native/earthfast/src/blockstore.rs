//! The graded terrain blocks kept on disk (server/blockcache.py, 2026-11-12): a block on a cached place came back only
//! by building it again (heights 3-7 s of DEM sampling, road grading, sea floor and arch raise, then the land cover and
//! the paint: a warm teleport's median graded block 6.7 s on the PC). Two kinds of file, both content-addressed (the
//! 32-byte key names everything the output depends on; blockcache.py makes it):
//!
//! - heights: one 128 m lattice chunk of the block's float64 surface heights (heightmap.ChunkCache's unit). They are
//!   pointwise (heightmap.surface_u), so a chunk serves every block of the anchor that covers it: a block moved 128 m
//!   reads 56 of its 64 chunks. Stored exactly (the land cover's slope and the tunnel holes read them, and a quantum's
//!   rounding would flip samples there): the bits mapped to integers in float order, a 2-D Lorenzo prediction's
//!   residual zig-zagged, the 8 bytes of every value split into planes, one zstd frame with its checksum. ~4.8 bytes a
//!   sample on Fells Point (the float64's low mantissa is noise: nothing lossless does much better).
//! - block: a block's material index per sample (after the layer fold, before the tunnel holes), its tunnel holes and a
//!   small JSON of the rest of its info. Not pointwise (the paint's affine grid, the slope at the block's edge, the cells
//!   under it), so one per block centre; ~20-30 KB.
//!
//! Files are `<dir>/<2 hex>/<64 hex>.<h|b>`: a header (magic, format, kind, side, key) and one zstd frame. Written to a
//! unique temporary name and renamed into place (a crash leaves a whole file or none); an existing file is not written
//! again (same key, same bytes: two processes building the same chunk write it once). A damaged file (bad header, key,
//! size or checksum) is removed and reads as a miss, so the block is built again. A hit marks the file used (its mtime,
//! once a process) for the LRU prune (blockstore_prune).

use numpy::{IntoPyArray, PyArray2, PyArray3, PyReadonlyArray2, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::HashSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::SystemTime;

const MAGIC: &[u8; 4] = b"BNBC";
const FORMAT: u8 = 1;
const KIND_HEIGHTS: u8 = 1;
const KIND_BLOCK: u8 = 2;
const HEADER: usize = 4 + 1 + 1 + 2 + 32 + 8; // magic, format, kind, side (u16), key, raw length (u64)
const SIGN_FREE: i64 = 0x7FFF_FFFF_FFFF_FFFF;
const MAX_THREADS: usize = 8;

static TMP_SEQ: AtomicU64 = AtomicU64::new(0);
static TOUCHED: Mutex<Option<HashSet<[u8; 32]>>> = Mutex::new(None);

fn key32(k: &[u8]) -> Result<[u8; 32], String> {
    k.try_into().map_err(|_| format!("blockstore: a key is 32 bytes, not {}", k.len()))
}

fn hex(key: &[u8; 32]) -> String {
    key.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn path_for(dir: &Path, key: &[u8; 32], kind: u8) -> PathBuf {
    let h = hex(key);
    dir.join(&h[..2]).join(format!("{h}.{}", if kind == KIND_HEIGHTS { "h" } else { "b" }))
}

// ---- the heights' codec ------------------------------------------------------------------------------------------

/// The float's bits as an integer that orders as the floats do (negative ones mirrored): neighbouring heights on either
/// side of 0 m stay neighbouring integers. Its own inverse.
#[inline]
fn ordered(bits: i64) -> i64 {
    if bits < 0 {
        bits ^ SIGN_FREE
    } else {
        bits
    }
}

#[inline]
fn predict(o: &[i64], n: usize, j: usize, i: usize) -> i64 {
    match (j, i) {
        (0, 0) => 0,
        (0, _) => o[i - 1],
        (_, 0) => o[(j - 1) * n],
        _ => o[j * n + i - 1].wrapping_add(o[(j - 1) * n + i]).wrapping_sub(o[(j - 1) * n + i - 1]),
    }
}

/// n x n heights (row-major) -> the residuals' 8 byte planes.
pub fn heights_planes(v: &[f64], n: usize) -> Vec<u8> {
    let m = n * n;
    let o: Vec<i64> = v.iter().map(|x| ordered(x.to_bits() as i64)).collect();
    let mut planes = vec![0u8; 8 * m];
    for j in 0..n {
        for i in 0..n {
            let r = o[j * n + i].wrapping_sub(predict(&o, n, j, i));
            let z = ((r << 1) ^ (r >> 63)) as u64;
            let b = z.to_le_bytes();
            let at = j * n + i;
            for (k, byte) in b.iter().enumerate() {
                planes[k * m + at] = *byte;
            }
        }
    }
    planes
}

/// heights_planes back to the heights, bit for bit.
pub fn planes_heights(planes: &[u8], n: usize) -> Vec<f64> {
    let m = n * n;
    let mut o = vec![0i64; m];
    for j in 0..n {
        for i in 0..n {
            let at = j * n + i;
            let mut b = [0u8; 8];
            for (k, byte) in b.iter_mut().enumerate() {
                *byte = planes[k * m + at];
            }
            let z = u64::from_le_bytes(b);
            let r = ((z >> 1) as i64) ^ -((z & 1) as i64);
            o[at] = predict(&o, n, j, i).wrapping_add(r);
        }
    }
    o.into_iter().map(|x| f64::from_bits(ordered(x) as u64)).collect()
}

// ---- files -----------------------------------------------------------------------------------------------------

fn compress(raw: &[u8], level: i32) -> Result<Vec<u8>, String> {
    let mut c = zstd::bulk::Compressor::new(level).map_err(|e| format!("zstd: {e}"))?;
    c.set_parameter(zstd::zstd_safe::CParameter::ChecksumFlag(true)).map_err(|e| format!("zstd: {e}"))?;
    c.compress(raw).map_err(|e| format!("zstd: {e}"))
}

fn encode(kind: u8, side: usize, key: &[u8; 32], raw: &[u8], level: i32) -> Result<Vec<u8>, String> {
    let body = compress(raw, level)?;
    let mut out = Vec::with_capacity(HEADER + body.len());
    out.extend_from_slice(MAGIC);
    out.push(FORMAT);
    out.push(kind);
    out.extend_from_slice(&(side as u16).to_le_bytes());
    out.extend_from_slice(key);
    out.extend_from_slice(&(raw.len() as u64).to_le_bytes());
    out.extend_from_slice(&body);
    Ok(out)
}

/// A file's raw payload, or Err when it is not a whole, matching file of this kind, side and key.
pub fn decode(data: &[u8], kind: u8, side: usize, key: &[u8; 32], max_raw: usize) -> Result<Vec<u8>, String> {
    if data.len() < HEADER || &data[..4] != MAGIC || data[4] != FORMAT || data[5] != kind {
        return Err("not a block cache file of this kind".into());
    }
    if u16::from_le_bytes([data[6], data[7]]) as usize != side || &data[8..40] != key {
        return Err("another block cache item".into());
    }
    let raw_len = u64::from_le_bytes(data[40..48].try_into().unwrap()) as usize;
    if raw_len > max_raw {
        return Err("block cache item too large".into());
    }
    let raw = zstd::bulk::decompress(&data[HEADER..], raw_len).map_err(|e| format!("zstd: {e}"))?;
    if raw.len() != raw_len {
        return Err("truncated block cache item".into());
    }
    Ok(raw)
}

/// Write data as path atomically (a unique temporary name renamed into place); an existing file is kept as it is.
/// -> bytes written (0: there already, or not written).
fn write_atomic(path: &Path, data: &[u8]) -> usize {
    if path.exists() {
        return 0;
    }
    let Some(parent) = path.parent() else { return 0 };
    if std::fs::create_dir_all(parent).is_err() {
        return 0;
    }
    let tmp = path.with_extension(format!(
        "{}.{}.tmp",
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let ok = (|| -> std::io::Result<()> {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(data)?;
        drop(f);
        std::fs::rename(&tmp, path)
    })();
    if ok.is_err() {
        let _ = std::fs::remove_file(&tmp);
        return 0;
    }
    data.len()
}

/// The file at path read and checked (decode); a damaged one removed. None: missing or damaged.
fn read_checked(path: &Path, kind: u8, side: usize, key: &[u8; 32], max_raw: usize) -> Option<Vec<u8>> {
    let data = std::fs::read(path).ok()?;
    match decode(&data, kind, side, key, max_raw) {
        Ok(raw) => {
            touch(path, key);
            Some(raw)
        }
        Err(_) => {
            let _ = std::fs::remove_file(path);
            None
        }
    }
}

/// Mark a file used now (its mtime: blockstore_prune's order), once per process and key.
fn touch(path: &Path, key: &[u8; 32]) {
    {
        let mut g = TOUCHED.lock().unwrap();
        let set = g.get_or_insert_with(HashSet::new);
        if !set.insert(*key) {
            return;
        }
    }
    if let Ok(f) = std::fs::File::options().write(true).open(path) {
        let _ = f.set_modified(SystemTime::now());
    }
}

/// Run f over 0..n on up to MAX_THREADS threads (the items' order kept).
fn par_map<T: Send, F: Fn(usize) -> T + Sync>(n: usize, f: F) -> Vec<T> {
    let threads = std::thread::available_parallelism().map(|t| t.get()).unwrap_or(1).clamp(1, MAX_THREADS).min(n.max(1));
    if threads <= 1 {
        return (0..n).map(&f).collect();
    }
    let per = n.div_ceil(threads);
    let f = &f;
    std::thread::scope(|sc| {
        let hs: Vec<_> = (0..threads)
            .map(|t| sc.spawn(move || (t * per..((t + 1) * per).min(n)).map(f).collect::<Vec<T>>()))
            .collect();
        hs.into_iter().flat_map(|h| h.join().unwrap()).collect()
    })
}

pub fn put_heights(dir: &Path, keys: &[[u8; 32]], u: &[f64], n: usize, level: i32) -> (usize, usize) {
    let m = n * n;
    let got = par_map(keys.len(), |c| {
        let path = path_for(dir, &keys[c], KIND_HEIGHTS);
        if path.exists() {
            return 0;
        }
        match encode(KIND_HEIGHTS, n, &keys[c], &heights_planes(&u[c * m..(c + 1) * m], n), level) {
            Ok(data) => write_atomic(&path, &data),
            Err(_) => 0,
        }
    });
    (got.iter().filter(|&&b| b > 0).count(), got.iter().sum())
}

pub fn get_heights(dir: &Path, keys: &[[u8; 32]], n: usize) -> Vec<Option<Vec<f64>>> {
    let m = n * n;
    par_map(keys.len(), |c| {
        let raw = read_checked(&path_for(dir, &keys[c], KIND_HEIGHTS), KIND_HEIGHTS, n, &keys[c], 8 * m)?;
        if raw.len() != 8 * m {
            let _ = std::fs::remove_file(path_for(dir, &keys[c], KIND_HEIGHTS));
            return None;
        }
        Some(planes_heights(&raw, n))
    })
}

/// A block's payload: flags (bit 0 material index, bit 1 holes), meta length (u32), meta, index (n²), holes (n² bits).
pub fn block_raw(n: usize, idx: Option<&[u8]>, holes: Option<&[bool]>, meta: &[u8]) -> Vec<u8> {
    let mut raw = Vec::with_capacity(5 + meta.len() + n * n + n * n / 8 + 1);
    raw.push(u8::from(idx.is_some()) | (u8::from(holes.is_some()) << 1));
    raw.extend_from_slice(&(meta.len() as u32).to_le_bytes());
    raw.extend_from_slice(meta);
    if let Some(idx) = idx {
        raw.extend_from_slice(idx);
    }
    if let Some(holes) = holes {
        let mut bits = vec![0u8; (n * n).div_ceil(8)];
        for (i, &h) in holes.iter().enumerate() {
            if h {
                bits[i / 8] |= 1 << (i % 8);
            }
        }
        raw.extend_from_slice(&bits);
    }
    raw
}

pub type BlockParts = (Option<Vec<u8>>, Option<Vec<bool>>, Vec<u8>);

pub fn block_parts(raw: &[u8], n: usize) -> Result<BlockParts, String> {
    let bad = || "damaged block cache item".to_string();
    if raw.len() < 5 {
        return Err(bad());
    }
    let flags = raw[0];
    let ml = u32::from_le_bytes(raw[1..5].try_into().unwrap()) as usize;
    let mut at = 5;
    let meta = raw.get(at..at + ml).ok_or_else(bad)?.to_vec();
    at += ml;
    let idx = if flags & 1 != 0 {
        let v = raw.get(at..at + n * n).ok_or_else(bad)?.to_vec();
        at += n * n;
        Some(v)
    } else {
        None
    };
    let holes = if flags & 2 != 0 {
        let bits = raw.get(at..at + (n * n).div_ceil(8)).ok_or_else(bad)?;
        at += bits.len();
        Some((0..n * n).map(|i| bits[i / 8] >> (i % 8) & 1 == 1).collect())
    } else {
        None
    };
    if at != raw.len() {
        return Err(bad());
    }
    Ok((idx, holes, meta))
}

/// The files under dir (path, size, mtime) and their total size.
fn files(dir: &Path) -> (Vec<(SystemTime, u64, PathBuf)>, u64) {
    let mut out = Vec::new();
    let mut total = 0;
    let Ok(rd) = std::fs::read_dir(dir) else { return (out, 0) };
    for sub in rd.flatten() {
        let Ok(rd2) = std::fs::read_dir(sub.path()) else { continue };
        for e in rd2.flatten() {
            let p = e.path();
            let ext = p.extension().and_then(|x| x.to_str()).unwrap_or("");
            let Ok(md) = e.metadata() else { continue };
            if !md.is_file() {
                continue;
            }
            let age = SystemTime::now().duration_since(md.modified().unwrap_or(SystemTime::now())).unwrap_or_default();
            if ext == "tmp" {
                if age.as_secs() > 3600 {
                    let _ = std::fs::remove_file(&p); // (a process that died mid-write)
                }
                continue;
            }
            if ext != "h" && ext != "b" {
                continue;
            }
            total += md.len();
            out.push((md.modified().unwrap_or(SystemTime::UNIX_EPOCH), md.len(), p));
        }
    }
    (out, total)
}

/// The least recently used files removed until at most low_water x max_bytes are left (once over max_bytes); files
/// used within keep_s stay. -> (files removed, bytes left).
pub fn prune(dir: &Path, max_bytes: u64, low_water: f64, keep_s: f64) -> (usize, u64) {
    let (mut fs, mut total) = files(dir);
    if total <= max_bytes {
        return (0, total);
    }
    fs.sort();
    let now = SystemTime::now();
    let target = (max_bytes as f64 * low_water) as u64;
    let mut removed = 0;
    for (mtime, size, p) in fs {
        if total <= target || now.duration_since(mtime).map(|d| d.as_secs_f64()).unwrap_or(0.0) < keep_s {
            break;
        }
        if std::fs::remove_file(&p).is_ok() {
            total -= size;
            removed += 1;
        }
    }
    (removed, total)
}

// ---- Python ----------------------------------------------------------------------------------------------------

fn keys_of(keys: &[Vec<u8>]) -> PyResult<Vec<[u8; 32]>> {
    keys.iter().map(|k| key32(k).map_err(PyValueError::new_err)).collect()
}

/// blockcache.py: chunks u[c] (k, n, n) float64 written under keys[c] (32 bytes each) in dir. -> (files, bytes written).
#[pyfunction]
#[pyo3(signature = (dir, keys, u, level = 3))]
pub fn blockstore_put_heights(py: Python<'_>, dir: &str, keys: Vec<Vec<u8>>, u: PyReadonlyArray3<'_, f64>,
                              level: i32) -> PyResult<(usize, usize)> {
    let keys = keys_of(&keys)?;
    let sh = u.shape();
    if sh[0] != keys.len() || sh[1] != sh[2] || sh[1] == 0 || sh[1] > u16::MAX as usize {
        return Err(PyValueError::new_err("blockstore: u must be (len(keys), n, n)"));
    }
    let n = sh[1];
    let v = u.as_slice().map_err(|e| PyValueError::new_err(format!("{e}")))?;
    let dir = PathBuf::from(dir);
    Ok(py.detach(|| put_heights(&dir, &keys, v, n, level)))
}

/// blockcache.py: the chunks of keys from dir: ((k, n, n) float64, found per key); a missing or damaged one's rows
/// are zero (a damaged file is removed).
#[pyfunction]
pub fn blockstore_get_heights<'py>(py: Python<'py>, dir: &str, keys: Vec<Vec<u8>>, n: usize)
                                   -> PyResult<(Bound<'py, PyArray3<f64>>, Vec<bool>)> {
    let keys = keys_of(&keys)?;
    let dir = PathBuf::from(dir);
    let got = py.detach(|| get_heights(&dir, &keys, n));
    let m = n * n;
    let mut out = vec![0f64; keys.len() * m];
    let mut found = Vec::with_capacity(keys.len());
    for (c, g) in got.into_iter().enumerate() {
        found.push(g.is_some());
        if let Some(v) = g {
            out[c * m..(c + 1) * m].copy_from_slice(&v);
        }
    }
    let arr = numpy::ndarray::Array3::from_shape_vec((keys.len(), n, n), out).unwrap().into_pyarray(py);
    Ok((arr, found))
}

/// blockcache.py: a block's material index (n, n) uint8 or None, its tunnel holes (n, n) bool or None and meta (JSON
/// bytes) written under key. -> bytes written (0: there already, or not written).
#[pyfunction]
#[pyo3(signature = (dir, key, n, idx, holes, meta, level = 3))]
pub fn blockstore_put_block(py: Python<'_>, dir: &str, key: Vec<u8>, n: usize, idx: Option<PyReadonlyArray2<'_, u8>>,
                            holes: Option<PyReadonlyArray2<'_, bool>>, meta: &[u8], level: i32) -> PyResult<usize> {
    let key = key32(&key).map_err(PyValueError::new_err)?;
    if n == 0 || n > u16::MAX as usize {
        return Err(PyValueError::new_err("blockstore: bad block side"));
    }
    let idx_v = match &idx {
        Some(a) if a.shape() == [n, n] => Some(a.as_slice().map_err(|e| PyValueError::new_err(format!("{e}")))?),
        Some(_) => return Err(PyValueError::new_err("blockstore: idx must be (n, n) contiguous")),
        None => None,
    };
    let holes_v = match &holes {
        Some(a) if a.shape() == [n, n] => Some(a.as_slice().map_err(|e| PyValueError::new_err(format!("{e}")))?),
        Some(_) => return Err(PyValueError::new_err("blockstore: holes must be (n, n) contiguous")),
        None => None,
    };
    let path = path_for(Path::new(dir), &key, KIND_BLOCK);
    let raw = block_raw(n, idx_v, holes_v, meta);
    py.detach(|| match encode(KIND_BLOCK, n, &key, &raw, level) {
        Ok(data) => Ok(write_atomic(&path, &data)),
        Err(e) => Err(PyValueError::new_err(e)),
    })
}

type BlockOut<'py> = (Option<Bound<'py, PyArray2<u8>>>, Option<Bound<'py, PyArray2<bool>>>, Bound<'py, PyBytes>);

/// blockcache.py: (material index or None, holes or None, meta bytes) of key's block, or None (missing or damaged:
/// a damaged file is removed).
#[pyfunction]
pub fn blockstore_get_block<'py>(py: Python<'py>, dir: &str, key: Vec<u8>, n: usize) -> PyResult<Option<BlockOut<'py>>> {
    let key = key32(&key).map_err(PyValueError::new_err)?;
    let path = path_for(Path::new(dir), &key, KIND_BLOCK);
    let max_raw = 5 + (1 << 20) + n * n + (n * n).div_ceil(8);
    let got = py.detach(|| {
        let raw = read_checked(&path, KIND_BLOCK, n, &key, max_raw)?;
        match block_parts(&raw, n) {
            Ok(p) => Some(p),
            Err(_) => {
                let _ = std::fs::remove_file(&path);
                None
            }
        }
    });
    Ok(got.map(|(idx, holes, meta)| {
        (idx.map(|v| numpy::ndarray::Array2::from_shape_vec((n, n), v).unwrap().into_pyarray(py)),
         holes.map(|v| numpy::ndarray::Array2::from_shape_vec((n, n), v).unwrap().into_pyarray(py)),
         PyBytes::new(py, &meta))
    }))
}

/// earthd's start and the stores' own check: dir cut back to max_bytes (least recently used first, to low_water of it;
/// files used within keep_s stay). -> (files removed, bytes left).
#[pyfunction]
#[pyo3(signature = (dir, max_bytes, low_water = 0.9, keep_s = 600.0))]
pub fn blockstore_prune(py: Python<'_>, dir: &str, max_bytes: u64, low_water: f64, keep_s: f64) -> (usize, u64) {
    let dir = PathBuf::from(dir);
    py.detach(|| prune(&dir, max_bytes, low_water, keep_s))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dm::Dm;

    fn sample(n: usize) -> Vec<f64> {
        (0..n * n)
            .map(|k| {
                let (j, i) = ((k / n) as f64, (k % n) as f64);
                (j * 0.013 + i * 0.007).dsin() * 3.0 - 1.0 + (k % 13) as f64 * 1e-9
            })
            .collect()
    }

    #[test]
    fn heights_round_trip_bits() {
        let n = 64;
        let mut v = sample(n);
        v[5] = -0.0;
        v[6] = 0.0;
        v[7] = f64::MIN_POSITIVE;
        v[8] = -1e300;
        v[9] = f64::NAN;
        let back = planes_heights(&heights_planes(&v, n), n);
        for (a, b) in v.iter().zip(back.iter()) {
            assert_eq!(a.to_bits(), b.to_bits());
        }
    }

    #[test]
    fn files_round_trip_and_damage() {
        let dir = std::env::temp_dir().join(format!("bnbc_test_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let n = 32;
        let v = sample(n);
        let keys = [[7u8; 32], [9u8; 32]];
        let mut u = v.clone();
        u.extend(v.iter().map(|x| x + 1.0));
        let (files_, bytes) = put_heights(&dir, &keys, &u, n, 3);
        assert_eq!(files_, 2);
        assert!(bytes > 0);
        assert_eq!(put_heights(&dir, &keys, &u, n, 3).0, 0); // there already
        let got = get_heights(&dir, &keys, n);
        assert_eq!(got[1].as_ref().unwrap()[3].to_bits(), (v[3] + 1.0).to_bits());
        // another key's file under this name, a truncated file: misses, removed
        let p = path_for(&dir, &keys[0], KIND_HEIGHTS);
        let data = std::fs::read(&p).unwrap();
        std::fs::write(&p, &data[..data.len() - 3]).unwrap();
        assert!(get_heights(&dir, &keys[..1], n)[0].is_none());
        assert!(!p.exists());
        let raw = block_raw(n, Some(&vec![3u8; n * n]), Some(&(0..n * n).map(|i| i % 5 == 0).collect::<Vec<_>>()), b"{}");
        let (idx, holes, meta) = block_parts(&raw, n).unwrap();
        assert_eq!(idx.unwrap()[10], 3);
        assert!(holes.as_ref().unwrap()[10] && !holes.unwrap()[11]);
        assert_eq!(meta, b"{}");
        assert!(block_parts(&raw[..raw.len() - 1], n).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
