//! A cached place's far field put back in one call (server/farrestore.py, 2026-11-12): every far tile whose finished
//! shape the revisit cache holds ("farfin" items: the plain shape with a placeholder material name, its height grid
//! and its numbers) read from the revisit packs, its material name stamped in, compressed as the game reads it
//! (cdaez), written into the session folder through the staging folder, linked to its pooled slot path, and its kept
//! texture files linked beside it, on a few native threads with the GIL released. In the server these were one far
//! tile at a time, each a worker's round trip and a shape encoded again in the main interpreter: 30-57 ms a tile,
//! ~10 s for a place's ~470 tiles (2026-10-25 note; 2026-11-12 profile: 50-75 s under load, the far gate 3 wide).
//!
//! The record is revisit.py's (_encode): u32 length of a JSON head {names, sizes, side, extra}, the head, the blobs
//! one after another; it lies in a pack (revisitpack.py) at (offset, stored length), zstd (codec 1) or raw (codec 0),
//! with the crc32 of its stored bytes. Output: per item the shape file's size, the record's extra (JSON text) and
//! the grid blob, or the reason it could not be put back (the caller builds that tile the usual way).

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

/// (pack file, offset of the stored bytes, stored length, codec, raw length, crc32 of the stored bytes)
pub type Loc = (String, u64, u32, u8, u32, u32);
/// (shape's staging tmp, shape's path, slot's staging tmp, slot's path)
pub type Files = (String, String, Option<String>, Option<String>);
/// (kept file, staging tmp, path in the session folder)
pub type Side = (String, String, String);

pub struct Item {
    pub loc: Loc,
    pub placeholder: String,
    pub material: String,
    pub files: Files,
    pub sides: Vec<Side>,
}

pub struct Done {
    pub size: u64,
    pub extra: String,
    pub grid: Vec<u8>,
}

#[cfg(unix)]
fn read_at(f: &File, buf: &mut [u8], off: u64) -> std::io::Result<()> {
    use std::os::unix::fs::FileExt;
    f.read_exact_at(buf, off)
}

#[cfg(windows)]
fn read_at(f: &File, buf: &mut [u8], off: u64) -> std::io::Result<()> {
    use std::os::windows::fs::FileExt;
    let mut done = 0usize;
    while done < buf.len() {
        let n = f.seek_read(&mut buf[done..], off + done as u64)?;
        if n == 0 {
            return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "short read"));
        }
        done += n;
    }
    Ok(())
}

fn crc32(data: &[u8]) -> u32 {
    let mut c = flate2::Crc::new();
    c.update(data);
    c.sum()
}

/// A record's raw bytes from its pack (read handles kept per thread: one open per pack, not per item).
fn read_record(loc: &Loc, files: &mut HashMap<String, File>) -> Result<Vec<u8>, String> {
    let (path, off, n, codec, raw_n, crc) = loc;
    if !files.contains_key(path) {
        let f = File::open(path).map_err(|e| format!("pack {path}: {e}"))?;
        files.insert(path.clone(), f);
    }
    let f = &files[path];
    let mut stored = vec![0u8; *n as usize];
    read_at(f, &mut stored, *off).map_err(|e| format!("pack read: {e}"))?;
    if crc32(&stored) != *crc {
        return Err("record crc".into());
    }
    match codec {
        0 => {
            if stored.len() != *raw_n as usize {
                return Err("raw record of the wrong length".into());
            }
            Ok(stored)
        }
        1 => {
            let out = zstd::bulk::decompress(&stored, *raw_n as usize).map_err(|e| format!("zstd: {e}"))?;
            if out.len() != *raw_n as usize {
                return Err("record of the wrong length".into());
            }
            Ok(out)
        }
        c => Err(format!("unknown codec {c}")),
    }
}

/// (shape blob, grid blob, extra as JSON text) of a farfin record (revisit._encode's layout).
fn split_record(raw: &[u8]) -> Result<(&[u8], &[u8], String), String> {
    if raw.len() < 4 {
        return Err("short record".into());
    }
    let n = u32::from_le_bytes(raw[..4].try_into().unwrap()) as usize;
    let head = raw.get(4..4 + n).ok_or("short record head")?;
    let meta: serde_json::Value = serde_json::from_slice(head).map_err(|e| format!("record head: {e}"))?;
    let names = meta["names"].as_array().ok_or("record names")?;
    let sizes = meta["sizes"].as_array().ok_or("record sizes")?;
    if names.len() != sizes.len() {
        return Err("record names / sizes".into());
    }
    let mut at = 4 + n;
    let (mut shape, mut grid) = (None, None);
    for (name, size) in names.iter().zip(sizes) {
        let size = size.as_u64().ok_or("record size")? as usize;
        let blob = raw.get(at..at + size).ok_or("record blobs short")?;
        match name.as_str() {
            Some("shape") => shape = Some(blob),
            Some("grid") => grid = Some(blob),
            _ => {}
        }
        at += size;
    }
    if at != raw.len() {
        return Err("record blobs do not add up".into());
    }
    let extra = serde_json::to_string(&meta["extra"]).map_err(|e| format!("extra: {e}"))?;
    Ok((shape.ok_or("no shape")?, grid.ok_or("no grid")?, extra))
}

/// data with every `old` replaced by `new` (the same length: the shape's string table keeps its layout); an error
/// when old is not in it (matslots.restamp).
pub fn restamp(data: &[u8], old: &[u8], new: &[u8]) -> Result<Vec<u8>, String> {
    if old.len() != new.len() || old.is_empty() {
        return Err("names differ in length".into());
    }
    let mut out = data.to_vec();
    let mut i = 0usize;
    let mut found = 0usize;
    while i + old.len() <= out.len() {
        if out[i] == old[0] && &out[i..i + old.len()] == old {
            out[i..i + old.len()].copy_from_slice(new);
            i += old.len();
            found += 1;
        } else {
            i += 1;
        }
    }
    if found == 0 {
        return Err("placeholder not in the shape".into());
    }
    Ok(out)
}

/// os.replace(tmp, dst), dst's folder made again if it went (staging._replace).
fn replace(tmp: &str, dst: &str) -> std::io::Result<()> {
    match fs::rename(tmp, dst) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound && Path::new(tmp).exists() => {
            if let Some(d) = Path::new(dst).parent() {
                fs::create_dir_all(d)?;
            }
            fs::rename(tmp, dst)
        }
        r => r,
    }
}

/// dst becomes src's content atomically through tmp: a hard link, a copy where links fail (matslots.place,
/// revisit.place).
fn link_in(src: &str, tmp: &str, dst: &str) -> std::io::Result<()> {
    let _ = fs::remove_file(tmp);
    if fs::hard_link(src, tmp).is_err() {
        fs::copy(src, tmp)?;
    }
    replace(tmp, dst)
}

/// A file just put into the game's folder read once here (staging.warm: Windows scans a new file at its first open,
/// and the game paid that in its frame).
fn warm(path: &str) {
    if let Ok(mut f) = File::open(path) {
        let mut buf = vec![0u8; 1 << 20];
        while let Ok(n) = f.read(&mut buf) {
            if n == 0 {
                break;
            }
        }
    }
}

pub fn restore_one(it: &Item, files: &mut HashMap<String, File>, warm_read: bool) -> Result<Done, String> {
    let raw = read_record(&it.loc, files)?;
    let (shape, grid, extra) = split_record(&raw)?;
    let plain = restamp(shape, it.placeholder.as_bytes(), it.material.as_bytes())?;
    let data = crate::cdaez::packed(&plain, crate::cdaez::LEVEL)?;
    let (tmp, dst, slot_tmp, slot_dst) = &it.files;
    // the texture files first (the shape names its material; the mod sets the texture when the message comes, after
    // all of them are in place anyway)
    for (src, stmp, sdst) in &it.sides {
        link_in(src, stmp, sdst).map_err(|e| format!("side {sdst}: {e}"))?;
    }
    {
        let mut f = File::create(tmp).map_err(|e| format!("shape tmp: {e}"))?;
        f.write_all(&data).map_err(|e| format!("shape write: {e}"))?;
    }
    replace(tmp, dst).map_err(|e| format!("shape replace: {e}"))?;
    if let (Some(st), Some(sd)) = (slot_tmp, slot_dst) {
        link_in(dst, st, sd).map_err(|e| format!("slot: {e}"))?;
    }
    if warm_read {
        warm(dst);
        if let Some(sd) = slot_dst {
            warm(sd);
        }
        for (_, _, sdst) in &it.sides {
            warm(sdst);
        }
    }
    Ok(Done { size: data.len() as u64, extra, grid: grid.to_vec() })
}

/// Every item on up to `threads` threads (each takes the next item: the slow ones do not hold up a fixed share).
pub fn restore_all(items: &[Item], threads: usize, warm_read: bool) -> Vec<Result<Done, String>> {
    let n = items.len();
    let out: Vec<Mutex<Option<Result<Done, String>>>> = (0..n).map(|_| Mutex::new(None)).collect();
    let next = AtomicUsize::new(0);
    let t = threads.clamp(1, 16).min(n.max(1));
    std::thread::scope(|s| {
        for _ in 0..t {
            s.spawn(|| {
                let mut files = HashMap::new();
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= n {
                        break;
                    }
                    let r = restore_one(&items[i], &mut files, warm_read);
                    *out[i].lock().unwrap() = Some(r);
                }
            });
        }
    });
    out.into_iter().map(|m| m.into_inner().unwrap().unwrap_or_else(|| Err("not run".into()))).collect()
}

/// farrestore.restore: items [(loc, placeholder, material, files, sides)] (see Loc, Files, Side) -> per item
/// (True, shape bytes, extra JSON, grid bytes) or (False, 0, reason, b"").
#[pyfunction]
#[pyo3(signature = (items, threads = 4, warm_read = false))]
pub fn far_restore<'py>(
    py: Python<'py>,
    items: Vec<(Loc, String, String, Files, Vec<Side>)>,
    threads: usize,
    warm_read: bool,
) -> PyResult<Vec<(bool, u64, String, Bound<'py, PyBytes>)>> {
    let items: Vec<Item> = items
        .into_iter()
        .map(|(loc, placeholder, material, files, sides)| Item { loc, placeholder, material, files, sides })
        .collect();
    let res = py.detach(|| restore_all(&items, threads, warm_read));
    Ok(res
        .into_iter()
        .map(|r| match r {
            Ok(d) => (true, d.size, d.extra, PyBytes::new(py, &d.grid)),
            Err(e) => (false, 0, e, PyBytes::new(py, b"")),
        })
        .collect())
}

/// One shape written from its plain bytes with a placeholder name (a far tile built in a worker: the main process
/// only stamps the name in and compresses, instead of encoding the mesh again): restamp + cdaez pack -> the file's
/// bytes.
#[pyfunction]
pub fn far_shape_stamp<'py>(py: Python<'py>, plain: &[u8], placeholder: &str, material: &str)
                            -> PyResult<Bound<'py, PyBytes>> {
    let out = py
        .detach(|| restamp(plain, placeholder.as_bytes(), material.as_bytes())
            .and_then(|p| crate::cdaez::packed(&p, crate::cdaez::LEVEL)))
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    Ok(PyBytes::new(py, &out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restamp_all_or_error() {
        assert_eq!(restamp(b"xxABCyyABC", b"ABC", b"DEF").unwrap(), b"xxDEFyyDEF");
        assert!(restamp(b"xxABCyy", b"ABD", b"DEF").is_err());
        assert!(restamp(b"xxABCyy", b"ABC", b"DE").is_err());
    }

    #[test]
    fn record_split() {
        let head = br#"{"names":["shape","grid"],"sizes":[3,2],"side":["tex.dds"],"extra":{"err":1.5}}"#;
        let mut raw = (head.len() as u32).to_le_bytes().to_vec();
        raw.extend_from_slice(head);
        raw.extend_from_slice(b"abcde");
        let (s, g, e) = split_record(&raw).unwrap();
        assert_eq!(s, b"abc");
        assert_eq!(g, b"de");
        assert_eq!(e, r#"{"err":1.5}"#);
        raw.push(0);
        assert!(split_record(&raw).is_err());
    }
}
