//! Road stages shared by the server's processes (server/plancache.py's stage_*, grading._Planner._road_stage;
//! 2026-11-13): one process makes a map cell's road stage (its roads' plans, before its tunnels), the others wait
//! for it and read its result, instead of each making it again.
//!
//! Measured first (a cold Fells Point first visit, ~/bnge-tests/share286): a cell's tunnels are graded by the road
//! stages of the cells round it, so the process that plans one cell makes the road stages of its neighbours too.
//! One process planning a first block's 4 cells made each stage once (one interpreter lock: 20 s); four processes
//! planning one cell each made the neighbours' stages again, 13.5 CPU-s a cell where 6.5 were its own, and were no
//! faster. With the stages shared each is made once whatever the number of processes.
//!
//! Two parts, both plain files in the run's plan folder (no shared memory to set up, the same on Linux and Windows):
//!
//! - the claim: `<path>.claim`, made with O_EXCL by the process that makes the stage, holding "rank priority pid"
//!   (server/xproc.py's urgency: a process waits only for a holder at least as urgent as itself). A claim whose
//!   process is gone, or older than STALE_MS, is taken over. The wait runs here without the interpreter lock, in
//!   short slices (the caller's cancel check runs between them), looking every POLL_MS: the reader's other threads go
//!   on, and it wakes within milliseconds of the result (xproc.once polls from Python every 50 ms).
//! - the result: `<path>`, one file written under another name and renamed into place: a header, the JSON the
//!   caller gives, then the arrays' bytes as they are in memory (dtype string and shape kept: an exact round trip),
//!   closed by a checksum, all of it one zstd frame (level 1: a third of the bytes written for ~1 ms a cell). A
//!   damaged file reads as missing: the stage is made again.

use std::fs;
use std::io::{Read, Write};
use std::path::Path;
use std::time::{Duration, Instant, SystemTime};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes};

pub const MINE: i32 = 0; // this process makes it (release after the result is written)
pub const READY: i32 = 1; // the result is there
pub const HELD: i32 = 2; // another process is making it: wait
pub const UNLOCKED: i32 = 3; // a less urgent holder, or no claim file can be made: make it without the claim

const MAGIC: &[u8; 8] = b"BNGSTG1\n";
const ZMAGIC: &[u8; 8] = b"BNGSTZ1\n"; // the file: this, the body's length, the body (MAGIC ...) as one zstd frame
const ZLEVEL: i32 = 1; // (a road plan's 828 KB pack to about a third in ~1 ms: packz.rs; a run writes one per cell)
const MAX_BODY: u64 = 1 << 31;
const POLL_MS: u64 = 4;
const STALE_MS: u128 = 300_000; // a claim this old is taken over whatever its process does
const MAX_ARRAYS: usize = 4096;

fn claim_path(path: &str) -> String {
    format!("{path}.claim")
}

/// (rank, priority, pid) of a claim file, None when it is gone or still being written.
fn holder(claim: &str) -> Option<(i64, i64, u32)> {
    let mut s = String::new();
    fs::File::open(claim).ok()?.read_to_string(&mut s).ok()?;
    let mut it = s.split_whitespace();
    let rank = it.next()?.parse().ok()?;
    let prio = it.next()?.parse().ok()?;
    let pid = it.next()?.parse().ok()?;
    Some((rank, prio, pid))
}

#[cfg(target_os = "linux")]
fn alive(pid: u32) -> bool {
    // (gone, or ended and not reaped yet by the server's own process: a zombie)
    match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(s) => match s.rsplit_once(')') {
            Some((_, rest)) => rest.split_whitespace().next() != Some("Z"),
            None => true,
        },
        Err(e) => e.kind() != std::io::ErrorKind::NotFound,
    }
}

#[cfg(windows)]
fn alive(pid: u32) -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut core::ffi::c_void;
        fn GetExitCodeProcess(h: *mut core::ffi::c_void, code: *mut u32) -> i32;
        fn CloseHandle(h: *mut core::ffi::c_void) -> i32;
        fn GetLastError() -> u32;
    }
    // (as xproc._alive_windows: PROCESS_QUERY_LIMITED_INFORMATION; access denied means it exists)
    unsafe {
        let h = OpenProcess(0x1000, 0, pid);
        if h.is_null() {
            return GetLastError() == 5;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(h, &mut code);
        CloseHandle(h);
        ok == 0 || code == 259
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn alive(_pid: u32) -> bool {
    true // (not told: waited out, STALE_MS)
}

fn stale(claim: &str) -> bool {
    match fs::metadata(claim).and_then(|m| m.modified()) {
        Ok(t) => SystemTime::now().duration_since(t).map(|d| d.as_millis() > STALE_MS).unwrap_or(false),
        Err(_) => false,
    }
}

/// One look: the result, else the claim taken, else who holds it.
fn claim_once(path: &str, rank: i64, prio: i64, pid: u32) -> i32 {
    if Path::new(path).exists() {
        return READY;
    }
    let claim = claim_path(path);
    for _ in 0..3 {
        if let Some(dir) = Path::new(&claim).parent() {
            let _ = fs::create_dir_all(dir);
        }
        match fs::OpenOptions::new().write(true).create_new(true).open(&claim) {
            Ok(mut f) => {
                let _ = f.write_all(format!("{rank} {prio} {pid}").as_bytes());
                drop(f);
                if Path::new(path).exists() {
                    // (written by the process whose claim we just followed)
                    let _ = fs::remove_file(&claim);
                    return READY;
                }
                return MINE;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => match holder(&claim) {
                Some((hr, hp, hpid)) => {
                    if hpid == pid {
                        return HELD; // (another thread of this process: its own locks order them)
                    }
                    if !alive(hpid) || stale(&claim) {
                        let _ = fs::remove_file(&claim);
                        continue;
                    }
                    if (hr, hp) > (rank, prio) {
                        return UNLOCKED;
                    }
                    return HELD;
                }
                None => {
                    if stale(&claim) {
                        let _ = fs::remove_file(&claim);
                        continue;
                    }
                    return HELD; // (released meanwhile, or being written: looked at again)
                }
            },
            Err(_) => return UNLOCKED,
        }
    }
    HELD
}

/// Before making the stage whose result is the file `path`: READY (1) when it is there, MINE (0) when this process
/// now holds its claim (stage_release after stage_put), HELD (2) when another process is making it (stage_wait),
/// UNLOCKED (3) when it should be made here without the claim (a less urgent holder; no claim file can be made).
/// rank, prio: this process's urgency (xproc._rank); pid: its id.
#[pyfunction]
pub fn stage_claim(path: &str, rank: i64, prio: i64, pid: u32) -> i32 {
    claim_once(path, rank, prio, pid)
}

/// stage_claim looked at again every few ms for up to timeout_ms, without the interpreter lock: the first answer
/// that is not HELD, or HELD at the end (the caller checks its cancel and calls again).
#[pyfunction]
pub fn stage_wait(py: Python<'_>, path: &str, rank: i64, prio: i64, pid: u32, timeout_ms: u64) -> i32 {
    py.detach(|| {
        let end = Instant::now() + Duration::from_millis(timeout_ms);
        loop {
            let st = claim_once(path, rank, prio, pid);
            if st != HELD || Instant::now() >= end {
                return st;
            }
            std::thread::sleep(Duration::from_millis(POLL_MS));
        }
    })
}

/// Give the claim of `path` back (after stage_put, or when the stage could not be made).
#[pyfunction]
pub fn stage_release(py: Python<'_>, path: &str) {
    py.detach(|| release(path))
}

/// The claim file removed; tried again for a moment while something else holds it open without sharing its removal
/// (Windows: a scanner, another program's read; as xproc.release). A claim that still cannot be removed is waited
/// out by the others (STALE_MS, their own wait's limit).
fn release(path: &str) {
    let claim = claim_path(path);
    for _ in 0..40 {
        match fs::remove_file(&claim) {
            Ok(()) => return,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return,
            Err(_) => std::thread::sleep(Duration::from_millis(5)),
        }
    }
}

/// Is a process making the stage of `path` now (its claim is there)?
#[pyfunction]
pub fn stage_busy(path: &str) -> bool {
    Path::new(&claim_path(path)).exists()
}

fn fnv(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    // (8 bytes at a time: a checksum against damage, not an attacker)
    let mut chunks = data.chunks_exact(8);
    for c in &mut chunks {
        h ^= u64::from_le_bytes(c.try_into().unwrap());
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    for &b in chunks.remainder() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

type ArrayIn<'a> = (String, String, Vec<u64>, &'a [u8]);

fn encode(meta: &[u8], arrays: &[ArrayIn]) -> Result<Vec<u8>, String> {
    if arrays.len() > MAX_ARRAYS {
        return Err("too many arrays".into());
    }
    let total: usize = arrays.iter().map(|a| a.3.len() + a.0.len() + a.1.len() + 8 * a.2.len() + 16).sum();
    let mut out = Vec::with_capacity(32 + meta.len() + total);
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&(meta.len() as u64).to_le_bytes());
    out.extend_from_slice(&(arrays.len() as u32).to_le_bytes());
    for (name, dtype, shape, data) in arrays {
        if name.len() > 255 || dtype.len() > 255 || shape.len() > 32 {
            return Err(format!("array {name}: name, dtype or shape too long"));
        }
        out.push(name.len() as u8);
        out.extend_from_slice(name.as_bytes());
        out.push(dtype.len() as u8);
        out.extend_from_slice(dtype.as_bytes());
        out.push(shape.len() as u8);
        for d in shape {
            out.extend_from_slice(&d.to_le_bytes());
        }
        out.extend_from_slice(&(data.len() as u64).to_le_bytes());
    }
    out.extend_from_slice(meta);
    for (_, _, _, data) in arrays {
        out.extend_from_slice(data);
    }
    let sum = fnv(&out);
    out.extend_from_slice(&sum.to_le_bytes());
    Ok(out)
}

struct Entry {
    name: String,
    dtype: String,
    shape: Vec<u64>,
    at: usize,
    len: usize,
}

/// (meta range, the arrays' entries) of a file's bytes, None when it is not a whole stage file.
fn decode(buf: &[u8]) -> Option<((usize, usize), Vec<Entry>)> {
    if buf.len() < MAGIC.len() + 12 + 8 || &buf[..MAGIC.len()] != MAGIC {
        return None;
    }
    let body = buf.len() - 8;
    if fnv(&buf[..body]) != u64::from_le_bytes(buf[body..].try_into().ok()?) {
        return None;
    }
    let mut p = MAGIC.len();
    let take = |p: &mut usize, n: usize| -> Option<&[u8]> {
        let s = buf.get(*p..p.checked_add(n)?)?;
        *p += n;
        Some(s)
    };
    let meta_len = u64::from_le_bytes(take(&mut p, 8)?.try_into().ok()?) as usize;
    let n = u32::from_le_bytes(take(&mut p, 4)?.try_into().ok()?) as usize;
    if n > MAX_ARRAYS {
        return None;
    }
    let mut heads = Vec::with_capacity(n);
    for _ in 0..n {
        let l = take(&mut p, 1)?[0] as usize;
        let name = std::str::from_utf8(take(&mut p, l)?).ok()?.to_string();
        let l = take(&mut p, 1)?[0] as usize;
        let dtype = std::str::from_utf8(take(&mut p, l)?).ok()?.to_string();
        let nd = take(&mut p, 1)?[0] as usize;
        let mut shape = Vec::with_capacity(nd);
        for _ in 0..nd {
            shape.push(u64::from_le_bytes(take(&mut p, 8)?.try_into().ok()?));
        }
        let len = u64::from_le_bytes(take(&mut p, 8)?.try_into().ok()?) as usize;
        heads.push((name, dtype, shape, len));
    }
    let meta = (p, p.checked_add(meta_len)?);
    let mut at = meta.1;
    let mut out = Vec::with_capacity(n);
    for (name, dtype, shape, len) in heads {
        let end = at.checked_add(len)?;
        if end > body {
            return None;
        }
        out.push(Entry { name, dtype, shape, at, len });
        at = end;
    }
    if meta.1 > body || at != body {
        return None;
    }
    Some((meta, out))
}

/// The body as the file holds it.
fn pack(body: &[u8]) -> Result<Vec<u8>, String> {
    let z = zstd::bulk::compress(body, ZLEVEL).map_err(|e| format!("stage file not packed: {e}"))?;
    let mut out = Vec::with_capacity(16 + z.len());
    out.extend_from_slice(ZMAGIC);
    out.extend_from_slice(&(body.len() as u64).to_le_bytes());
    out.extend_from_slice(&z);
    Ok(out)
}

/// The body of a file's bytes, None when they are not a whole stage file.
fn unpack(file: &[u8]) -> Option<Vec<u8>> {
    if file.len() < 16 || &file[..8] != ZMAGIC {
        return None;
    }
    let n = u64::from_le_bytes(file[8..16].try_into().ok()?);
    if n > MAX_BODY {
        return None;
    }
    let body = zstd::bulk::decompress(&file[16..], n as usize).ok()?;
    (body.len() as u64 == n).then_some(body)
}

fn write_file(path: &str, data: &[u8], pid: u32) -> std::io::Result<()> {
    if let Some(dir) = Path::new(path).parent() {
        fs::create_dir_all(dir)?;
    }
    let tmp = format!("{path}.{pid}.{:?}.tmp", std::thread::current().id());
    let res = fs::File::create(&tmp).and_then(|mut f| f.write_all(data)).and_then(|_| fs::rename(&tmp, path));
    if res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    res
}

/// Write a stage's result to `path` (under another name, renamed into place): meta (the caller's JSON) and arrays
/// [(name, dtype string, shape, bytes)] as they are in memory. True when written.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn stage_put(
    py: Python<'_>,
    path: &str,
    meta: &[u8],
    arrays: Vec<(String, String, Vec<u64>, Bound<'_, PyBytes>)>,
    pid: u32,
) -> PyResult<bool> {
    let refs: Vec<ArrayIn> = arrays.iter().map(|(n, d, s, b)| (n.clone(), d.clone(), s.clone(), b.as_bytes())).collect();
    py.detach(|| {
        let buf = encode(meta, &refs).map_err(PyValueError::new_err)?;
        let packed = pack(&buf).map_err(PyValueError::new_err)?;
        Ok(write_file(path, &packed, pid).is_ok())
    })
}

/// (meta bytes, [(name, dtype string, shape, bytearray)]) of the stage file `path`, None when it is missing or
/// damaged. The arrays' bytes are new, writable buffers (numpy.frombuffer over them gives arrays the caller owns).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn stage_get<'py>(
    py: Python<'py>,
    path: &str,
) -> PyResult<Option<(Bound<'py, PyBytes>, Vec<(String, String, Vec<u64>, Bound<'py, PyByteArray>)>)>> {
    let got = py.detach(|| {
        let buf = unpack(&fs::read(path).ok()?)?;
        let (meta, entries) = decode(&buf)?;
        Some((buf, meta, entries))
    });
    let Some((buf, meta, entries)) = got else {
        return Ok(None);
    };
    let arrays = entries
        .into_iter()
        .map(|e| (e.name, e.dtype, e.shape, PyByteArray::new(py, &buf[e.at..e.at + e.len])))
        .collect();
    Ok(Some((PyBytes::new(py, &buf[meta.0..meta.1]), arrays)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(stage_claim, m)?)?;
    m.add_function(wrap_pyfunction!(stage_wait, m)?)?;
    m.add_function(wrap_pyfunction!(stage_release, m)?)?;
    m.add_function(wrap_pyfunction!(stage_busy, m)?)?;
    m.add_function(wrap_pyfunction!(stage_put, m)?)?;
    m.add_function(wrap_pyfunction!(stage_get, m)?)?;
    m.add("STAGE_MINE", MINE)?;
    m.add("STAGE_READY", READY)?;
    m.add("STAGE_HELD", HELD)?;
    m.add("STAGE_UNLOCKED", UNLOCKED)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> String {
        let d = std::env::temp_dir().join(format!("stageshare_{}_{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d.join("14_1_2.stage").to_string_lossy().into_owned()
    }

    #[test]
    fn round_trip_and_damage() {
        let a: Vec<u8> = (0..4096u32).flat_map(|i| (i as f64 * 0.1).to_le_bytes()).collect();
        let arrays: Vec<ArrayIn> = vec![
            ("p".into(), "<f8".into(), vec![2048, 2], &a),
            ("e".into(), "<i8".into(), vec![0], &[]),
            ("s".into(), "|b1".into(), vec![], &[1]),
        ];
        let buf = encode(b"{\"k\": 1}", &arrays).unwrap();
        let (meta, es) = decode(&buf).unwrap();
        assert_eq!(&buf[meta.0..meta.1], b"{\"k\": 1}");
        assert_eq!(es.len(), 3);
        assert_eq!((es[0].name.as_str(), es[0].dtype.as_str(), es[0].shape.clone()), ("p", "<f8", vec![2048, 2]));
        assert_eq!(&buf[es[0].at..es[0].at + es[0].len], &a[..]);
        assert_eq!(es[1].len, 0);
        assert_eq!(&buf[es[2].at..es[2].at + es[2].len], &[1]);
        // (cut short, a byte changed, another file: missing)
        assert!(decode(&buf[..buf.len() - 9]).is_none());
        let mut bad = buf.clone();
        bad[buf.len() / 2] ^= 1;
        assert!(decode(&bad).is_none());
        assert!(decode(b"PK\x03\x04 not a stage").is_none());
        // (the file's form: packed, and a cut or changed file is missing too)
        let file = pack(&buf).unwrap();
        assert!(file.len() < buf.len() / 2);
        assert_eq!(unpack(&file).unwrap(), buf);
        assert!(unpack(&file[..file.len() - 3]).is_none());
        assert!(unpack(&buf).is_none());
        let mut bad = file.clone();
        bad[file.len() / 2] ^= 0x40;
        assert!(unpack(&bad).and_then(|b| decode(&b).map(|_| ())).is_none());
    }

    #[test]
    fn claims() {
        let path = tmp("claims");
        let me = std::process::id();
        assert_eq!(claim_once(&path, 1, 0, me), MINE);
        // (this process's other thread: held; another live process as urgent: held; a less urgent holder: unlocked)
        assert_eq!(claim_once(&path, 1, 0, me), HELD);
        fs::write(claim_path(&path), format!("1 3 {}", 1)).unwrap(); // (pid 1: alive on Linux, whatever it is)
        if alive(1) {
            assert_eq!(claim_once(&path, 1, 3, me), HELD);
            assert_eq!(claim_once(&path, 1, 0, me), UNLOCKED);
        }
        // (a holder that is gone: taken over)
        fs::write(claim_path(&path), "1 0 4000000000").unwrap();
        if !alive(4_000_000_000) {
            assert_eq!(claim_once(&path, 1, 0, me), MINE);
        }
        // (the result written: ready, with or without a claim)
        write_file(&path, &pack(&encode(b"{}", &[]).unwrap()).unwrap(), me).unwrap();
        assert_eq!(claim_once(&path, 1, 0, me), READY);
        let _ = fs::remove_file(claim_path(&path));
        assert_eq!(claim_once(&path, 1, 0, me), READY);
        assert!(decode(&unpack(&fs::read(&path).unwrap()).unwrap()).is_some());
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }

    #[test]
    fn two_threads_one_maker() {
        let path = tmp("race");
        let me = std::process::id();
        // (eight takers at once: the claim file is made by exactly one)
        let n: usize = (0..8u32)
            .map(|_| {
                let p = path.clone();
                std::thread::spawn(move || claim_once(&p, 1, 0, me) == MINE)
            })
            .collect::<Vec<_>>()
            .into_iter()
            .map(|h| h.join().unwrap() as usize)
            .sum();
        assert_eq!(n, 1);
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }
}
