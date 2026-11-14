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
//! Two parts, both plain files in the run's plan folder (the same on Linux and Windows):
//!
//! - the claim: `<path>.claim`, holding "rank priority pid start token born": the holder's urgency (server/xproc.py:
//!   a process waits only for a holder at least as urgent as itself), its process (the pid AND the process's start
//!   time: a pid given to another process is not the holder), the owner's token (a random number: only the owner
//!   whose token is still in the claim may publish or release, stage_put / stage_release) and when it was taken on
//!   the machine's uptime clock (not the wall clock: a claim LEASE_MS old is taken over whatever its process does,
//!   and a clock set forward or back changes nothing). Every look that may change a claim (take it, take over a dead
//!   or expired holder's, release it, publish under it) runs under the folder's lock (`.lock`: an OS file lock, gone
//!   with its process), so two processes never both take over one claim, and a release never removes a successor's.
//!   The wait runs here without the interpreter lock, in short slices (the caller's cancel check runs between them),
//!   looking every POLL_MS at the files and under the lock only when they changed.
//! - the result: `<path>`, one file written under another name and renamed into place: a header, the JSON the
//!   caller gives, then the arrays' bytes as they are in memory (dtype string and shape kept: an exact round trip),
//!   closed by a checksum, all of it one zstd frame (level 1: a third of the bytes written for ~1 ms a cell). A
//!   damaged file reads as missing; a reader that found one asks again with `repair`: the file is then checked under
//!   the lock, and a damaged one counts as no result, so one process takes the claim and writes it again (the
//!   others wait for that); a valid file published meanwhile is read, never removed.

use std::fs;
use std::io::Write;
use std::path::Path;
use std::time::{Duration, Instant, SystemTime};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyByteArray, PyBytes};

pub const MINE: i32 = 0; // this process makes it (stage_put, then stage_release, with the token given)
pub const READY: i32 = 1; // the result is there
pub const HELD: i32 = 2; // another process is making it: wait
pub const UNLOCKED: i32 = 3; // a less urgent holder, or the folder cannot be locked: make it without the claim

const MAGIC: &[u8; 8] = b"BNGSTG1\n";
const ZMAGIC: &[u8; 8] = b"BNGSTZ1\n"; // the file: this, the body's length, the body (MAGIC ...) as one zstd frame
const ZLEVEL: i32 = 1; // (a road plan's 828 KB pack to about a third in ~1 ms: packz.rs; a run writes one per cell)
const MAX_BODY: u64 = 1 << 31;
const POLL_MS: u64 = 4;
const LEASE_MS: u64 = 300_000; // a claim this old (uptime clock) is taken over whatever its process does
const RECHECK_POLLS: u32 = 64; // an unchanged claim's holder looked at again this often (alive, its lease)
const MAX_ARRAYS: usize = 4096;

fn claim_path(path: &str) -> String {
    format!("{path}.claim")
}

/// A claim's holder, as its file says.
#[derive(Clone, Copy, PartialEq, Debug)]
struct Holder {
    rank: i64,
    prio: i64,
    pid: u32,
    start: u64, // the process's start time (0: not known on this system)
    token: u64,
    born: u64, // ms on the uptime clock when the claim was taken
}

impl Holder {
    fn parse(s: &str) -> Option<Holder> {
        let mut it = s.split_whitespace();
        let h = Holder {
            rank: it.next()?.parse().ok()?,
            prio: it.next()?.parse().ok()?,
            pid: it.next()?.parse().ok()?,
            start: it.next()?.parse().ok()?,
            token: it.next()?.parse().ok()?,
            born: it.next()?.parse().ok()?,
        };
        it.next().is_none().then_some(h)
    }

    fn text(&self) -> String {
        format!("{} {} {} {} {} {}", self.rank, self.prio, self.pid, self.start, self.token, self.born)
    }
}

/// The claim file's holder, None when there is none or it cannot be read whole.
fn holder(claim: &str) -> Option<Holder> {
    Holder::parse(&fs::read_to_string(claim).ok()?)
}

/// The start time of process pid (clock ticks since boot), None when it is gone (or a zombie).
#[cfg(target_os = "linux")]
fn process_start(pid: u32) -> Option<u64> {
    let s = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = s.rsplit_once(')')?.1;
    let mut it = rest.split_whitespace();
    if it.next()? == "Z" {
        return None; // (ended and not reaped yet by the server's own process)
    }
    it.nth(18)?.parse().ok() // (field 22 of the line: starttime)
}

#[cfg(windows)]
mod win {
    #[link(name = "kernel32")]
    extern "system" {
        pub fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut core::ffi::c_void;
        pub fn GetExitCodeProcess(h: *mut core::ffi::c_void, code: *mut u32) -> i32;
        pub fn GetProcessTimes(h: *mut core::ffi::c_void, created: *mut u64, exited: *mut u64, kernel: *mut u64, user: *mut u64) -> i32;
        pub fn CloseHandle(h: *mut core::ffi::c_void) -> i32;
        pub fn GetLastError() -> u32;
        pub fn GetTickCount64() -> u64;
    }
}

/// The creation time of process pid (FILETIME), None when it is gone; Some(0) when it is there and cannot be asked.
#[cfg(windows)]
fn process_start(pid: u32) -> Option<u64> {
    // (PROCESS_QUERY_LIMITED_INFORMATION; access denied means it exists: xproc._alive_windows)
    unsafe {
        let h = win::OpenProcess(0x1000, 0, pid);
        if h.is_null() {
            return (win::GetLastError() == 5).then_some(0);
        }
        let mut code: u32 = 0;
        let running = win::GetExitCodeProcess(h, &mut code) == 0 || code == 259;
        let (mut c, mut e, mut k, mut u) = (0u64, 0u64, 0u64, 0u64);
        let ok = win::GetProcessTimes(h, &mut c, &mut e, &mut k, &mut u) != 0;
        win::CloseHandle(h);
        running.then_some(if ok { c } else { 0 })
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn process_start(_pid: u32) -> Option<u64> {
    Some(0) // (not told: the lease alone ends its claims)
}

/// ms on a clock all the machine's processes share that never jumps: its uptime.
#[cfg(target_os = "linux")]
fn uptime_ms() -> u64 {
    fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|s| s.split_whitespace().next().and_then(|v| v.parse::<f64>().ok()))
        .map(|s| (s * 1000.0) as u64)
        .unwrap_or_else(wall_ms)
}

#[cfg(windows)]
fn uptime_ms() -> u64 {
    unsafe { win::GetTickCount64() }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn uptime_ms() -> u64 {
    wall_ms()
}

#[allow(dead_code)]
fn wall_ms() -> u64 {
    SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

/// Is the holder's process still the one that took the claim (the same pid AND start; a start not known on either
/// side counts as the same)?
fn holder_alive(h: &Holder) -> bool {
    match process_start(h.pid) {
        None => false,
        Some(start) => start == 0 || h.start == 0 || start == h.start,
    }
}

fn expired(h: &Holder, now: u64) -> bool {
    // (a claim from before a restart of the machine: born after now on the new uptime; its process is gone anyway)
    now.saturating_sub(h.born) > LEASE_MS || h.born > now.saturating_add(LEASE_MS)
}

fn new_token() -> u64 {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut h = RandomState::new().build_hasher();
    h.write_u64(uptime_ms());
    h.write_u32(std::process::id());
    h.finish() | 1 // (never 0: "no token")
}

/// This asker: its urgency and its process.
#[derive(Clone, Copy)]
struct Me {
    rank: i64,
    prio: i64,
    pid: u32,
    start: u64,
}

impl Me {
    fn new(rank: i64, prio: i64, pid: u32) -> Me {
        Me { rank, prio, pid, start: process_start(pid).unwrap_or(0) }
    }
}

/// The folder's lock, held while a claim is looked at and changed; None when it cannot be had.
fn dir_lock(path: &str) -> Option<fs::File> {
    let dir = Path::new(path).parent()?;
    fs::create_dir_all(dir).ok()?;
    let f = fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(dir.join(".lock")).ok()?;
    f.lock().ok()?;
    Some(f)
}

/// (length, modification time) of a file: which copy of it was looked at.
type Sig = (u64, Option<SystemTime>);

fn sig(path: &str) -> Option<Sig> {
    let m = fs::metadata(path).ok()?;
    Some((m.len(), m.modified().ok()))
}

fn valid_file(path: &str) -> bool {
    fs::read(path).ok().and_then(|f| unpack(&f)).and_then(|b| decode(&b).map(|_| ())).is_some()
}

/// One look under the folder's lock: (the state, this asker's token when the claim is now its own). repair: a result
/// file counts only when it is whole (bad: the damaged copy seen last, not read again).
fn look(path: &str, me: Me, repair: bool, bad: &mut Option<Sig>) -> (i32, u64) {
    let Some(lock) = dir_lock(path) else {
        return (if Path::new(path).exists() && !repair { READY } else { UNLOCKED }, 0);
    };
    let got = look_locked(path, me, repair, bad);
    drop(lock);
    got
}

fn look_locked(path: &str, me: Me, repair: bool, bad: &mut Option<Sig>) -> (i32, u64) {
    if let Some(s) = sig(path) {
        if !repair {
            return (READY, 0);
        }
        if *bad != Some(s) {
            if valid_file(path) {
                return (READY, 0);
            }
            *bad = Some(s);
        }
    }
    let claim = claim_path(path);
    let now = uptime_ms();
    if let Some(h) = holder(&claim) {
        let gone = expired(&h, now) || !holder_alive(&h);
        if !gone {
            if h.pid == me.pid && (h.start == me.start || h.start == 0 || me.start == 0) {
                return (HELD, 0); // (another thread of this process: its own locks order them)
            }
            if (h.rank, h.prio) > (me.rank, me.prio) {
                return (UNLOCKED, 0);
            }
            return (HELD, 0);
        }
    }
    // (none, one not written whole, or a holder that is gone or past its lease: this asker's now)
    let mine = Holder { rank: me.rank, prio: me.prio, pid: me.pid, start: me.start, token: new_token(), born: now };
    match fs::write(&claim, mine.text()) {
        Ok(()) => (MINE, mine.token),
        Err(_) => (UNLOCKED, 0),
    }
}

/// Before making the stage whose result is the file `path`: (state, token). READY (1) when the result is there,
/// MINE (0) when this process now holds its claim (stage_put and stage_release with the token), HELD (2) when
/// another process is making it (stage_wait), UNLOCKED (3) when it should be made here without the claim (a less
/// urgent holder; the folder cannot be locked). rank, prio: this process's urgency (xproc._rank); pid: its id.
/// repair: the caller found the result damaged: a damaged file counts as none (the claim is taken to write it again).
#[pyfunction]
#[pyo3(signature = (path, rank, prio, pid, repair = false))]
pub fn stage_claim(py: Python<'_>, path: &str, rank: i64, prio: i64, pid: u32, repair: bool) -> (i32, u64) {
    py.detach(|| look(path, Me::new(rank, prio, pid), repair, &mut None))
}

/// stage_claim looked at again for up to timeout_ms, without the interpreter lock: the first answer that is not
/// HELD, or HELD at the end (the caller checks its cancel and calls again).
#[pyfunction]
#[pyo3(signature = (path, rank, prio, pid, timeout_ms, repair = false))]
pub fn stage_wait(py: Python<'_>, path: &str, rank: i64, prio: i64, pid: u32, timeout_ms: u64, repair: bool) -> (i32, u64) {
    py.detach(|| wait(path, Me::new(rank, prio, pid), timeout_ms, repair))
}

fn wait(path: &str, me: Me, timeout_ms: u64, repair: bool) -> (i32, u64) {
    let end = Instant::now() + Duration::from_millis(timeout_ms);
    let claim = claim_path(path);
    let mut bad = None;
    loop {
        let got = look(path, me, repair, &mut bad);
        if got.0 != HELD || Instant::now() >= end {
            return got;
        }
        // (the files looked at without the lock until something changes: the claim, the result; the holder itself
        // looked at again now and then)
        let seen = (fs::read(&claim).ok(), sig(path));
        for _ in 0..RECHECK_POLLS {
            std::thread::sleep(Duration::from_millis(POLL_MS));
            if Instant::now() >= end || (fs::read(&claim).ok(), sig(path)) != seen {
                break;
            }
        }
    }
}

/// Give the claim of `path` back (after stage_put, or when the stage could not be made): only when token is still
/// its owner's (a claim taken over meanwhile is its new owner's). True when it was this owner's.
#[pyfunction]
pub fn stage_release(py: Python<'_>, path: &str, token: u64) -> bool {
    py.detach(|| release(path, token))
}

fn release(path: &str, token: u64) -> bool {
    let claim = claim_path(path);
    let Some(lock) = dir_lock(path) else {
        return false;
    };
    let mine = token != 0 && holder(&claim).is_some_and(|h| h.token == token);
    if mine {
        // (tried again for a moment while something else holds the file open without sharing its removal: Windows,
        // a scanner; as xproc.release. Left behind it is taken over when its lease ends, or at once when this
        // process is gone)
        for _ in 0..40 {
            match fs::remove_file(&claim) {
                Ok(()) => break,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => break,
                Err(_) => std::thread::sleep(Duration::from_millis(5)),
            }
        }
    }
    drop(lock);
    mine
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

/// data as the file `path`: written under another name, then renamed into place under the folder's lock, and only
/// while token is still the claim's owner's (0: whoever holds it). False when it is not (another owner took the
/// claim over: its result is the stage's) or the file could not be written.
fn write_file(path: &str, data: &[u8], pid: u32, token: u64) -> bool {
    if let Some(dir) = Path::new(path).parent() {
        if fs::create_dir_all(dir).is_err() {
            return false;
        }
    }
    let tmp = format!("{path}.{pid}.{:?}.tmp", std::thread::current().id());
    if fs::File::create(&tmp).and_then(|mut f| f.write_all(data)).is_err() {
        let _ = fs::remove_file(&tmp);
        return false;
    }
    let lock = dir_lock(path);
    let owner = token == 0 || holder(&claim_path(path)).is_some_and(|h| h.token == token);
    let ok = lock.is_some() && owner && fs::rename(&tmp, path).is_ok();
    drop(lock);
    if !ok {
        let _ = fs::remove_file(&tmp);
    }
    ok
}

/// Write a stage's result to `path` (under another name, renamed into place): meta (the caller's JSON) and arrays
/// [(name, dtype string, shape, bytes)] as they are in memory; token: the claim's, as stage_claim / stage_wait gave
/// it (a claim taken over by another owner meanwhile: not written). True when written.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn stage_put(
    py: Python<'_>,
    path: &str,
    meta: &[u8],
    arrays: Vec<(String, String, Vec<u64>, Bound<'_, PyBytes>)>,
    pid: u32,
    token: u64,
) -> PyResult<bool> {
    let refs: Vec<ArrayIn> = arrays.iter().map(|(n, d, s, b)| (n.clone(), d.clone(), s.clone(), b.as_bytes())).collect();
    py.detach(|| {
        let buf = encode(meta, &refs).map_err(PyValueError::new_err)?;
        let packed = pack(&buf).map_err(PyValueError::new_err)?;
        Ok(write_file(path, &packed, pid, token))
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
        let me = Me::new(1, 0, std::process::id());
        assert!(me.start != 0 || !cfg!(any(target_os = "linux", windows)));
        let (st, token) = look(&path, me, false, &mut None);
        assert_eq!(st, MINE);
        assert_ne!(token, 0);
        // (this process's other thread: held)
        assert_eq!(look(&path, me, false, &mut None), (HELD, 0));
        // (another live process as urgent: held; a less urgent holder: unlocked. pid 1 is alive on Linux)
        if let Some(start) = process_start(1) {
            let other = Holder { rank: 1, prio: 3, pid: 1, start, token: 77, born: uptime_ms() };
            fs::write(claim_path(&path), other.text()).unwrap();
            assert_eq!(look(&path, Me { prio: 3, ..me }, false, &mut None).0, HELD);
            assert_eq!(look(&path, me, false, &mut None).0, UNLOCKED);
            // (pid 1 as another process than the one that took the claim, a reused pid: gone, taken over)
            if start != 0 {
                fs::write(claim_path(&path), Holder { start: start + 12345, ..other }.text()).unwrap();
                assert_eq!(look(&path, me, false, &mut None).0, MINE);
            }
            // (a live holder past its lease: taken over)
            fs::write(claim_path(&path), Holder { born: uptime_ms().saturating_sub(LEASE_MS + 1000), ..other }.text()).unwrap();
            assert_eq!(look(&path, Me { prio: 3, ..me }, false, &mut None).0, MINE);
        }
        // (this process's own pid in a claim of an earlier process, its start another: not "this process", taken over;
        // and its own claim past the lease is taken over too, the same-process answer comes after the expiry)
        if me.start != 0 {
            let old = Holder { rank: 1, prio: 0, pid: me.pid, start: me.start + 1, token: 5, born: uptime_ms() };
            fs::write(claim_path(&path), old.text()).unwrap();
            assert_eq!(look(&path, me, false, &mut None).0, MINE);
        }
        let own_old = Holder { rank: 1, prio: 0, pid: me.pid, start: me.start, token: 6, born: uptime_ms().saturating_sub(LEASE_MS + 1) };
        fs::write(claim_path(&path), own_old.text()).unwrap();
        assert_eq!(look(&path, me, false, &mut None).0, MINE);
        // (a holder that is gone: taken over; a claim not written whole: taken)
        fs::write(claim_path(&path), "1 0 4000000000 1 9 0").unwrap();
        assert_eq!(look(&path, me, false, &mut None).0, MINE);
        fs::write(claim_path(&path), "1 0").unwrap();
        let (st, token) = look(&path, me, false, &mut None);
        assert_eq!(st, MINE);
        // (the result written under the claim: ready, with or without the claim)
        assert!(write_file(&path, &pack(&encode(b"{}", &[]).unwrap()).unwrap(), me.pid, token));
        assert_eq!(look(&path, me, false, &mut None).0, READY);
        assert!(release(&path, token));
        assert!(!Path::new(&claim_path(&path)).exists());
        assert_eq!(look(&path, me, false, &mut None).0, READY);
        assert!(decode(&unpack(&fs::read(&path).unwrap()).unwrap()).is_some());
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }

    #[test]
    fn only_the_owner_releases_and_publishes() {
        let path = tmp("owner");
        let me = Me::new(1, 0, std::process::id());
        let (_, old) = look(&path, me, false, &mut None);
        // (the claim taken over by another owner while the first still works: a holder past its lease)
        let mut h = holder(&claim_path(&path)).unwrap();
        h.born = uptime_ms().saturating_sub(LEASE_MS + 1);
        fs::write(claim_path(&path), h.text()).unwrap();
        let (st, new) = look(&path, me, false, &mut None);
        assert_eq!(st, MINE);
        assert_ne!(old, new);
        // (the first owner's late result and release change nothing of its successor's)
        let body = pack(&encode(b"{\"by\": \"old\"}", &[]).unwrap()).unwrap();
        assert!(!write_file(&path, &body, me.pid, old));
        assert!(!Path::new(&path).exists());
        assert!(!release(&path, old));
        assert_eq!(holder(&claim_path(&path)).unwrap().token, new);
        assert!(!release(&path, 0));
        assert!(write_file(&path, &pack(&encode(b"{\"by\": \"new\"}", &[]).unwrap()).unwrap(), me.pid, new));
        assert!(release(&path, new));
        assert!(holder(&claim_path(&path)).is_none());
        // (no temporary file left by the refused write)
        let left: Vec<_> = fs::read_dir(Path::new(&path).parent().unwrap()).unwrap().map(|e| e.unwrap().file_name()).collect();
        assert!(left.iter().all(|n| !n.to_string_lossy().ends_with(".tmp")), "{left:?}");
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }

    #[test]
    fn a_dead_holder_is_taken_over_once() {
        // (many askers see one dead holder's claim at once: exactly one takes it over, the others wait for that one)
        let path = tmp("takeover");
        if let Some(dir) = Path::new(&path).parent() {
            fs::create_dir_all(dir).unwrap();
        }
        let pid = std::process::id();
        for round in 0..50 {
            fs::write(claim_path(&path), format!("1 0 4000000000 1 {} 0", round + 1)).unwrap();
            let got: Vec<(i32, u64)> = (0..8)
                .map(|_| {
                    let p = path.clone();
                    std::thread::spawn(move || look(&p, Me::new(1, 0, pid), false, &mut None))
                })
                .collect::<Vec<_>>()
                .into_iter()
                .map(|h| h.join().unwrap())
                .collect();
            let owners: Vec<u64> = got.iter().filter(|g| g.0 == MINE).map(|g| g.1).collect();
            assert_eq!(owners.len(), 1, "round {round}: {got:?}");
            assert!(got.iter().all(|g| g.0 == MINE || g.0 == HELD));
            assert_eq!(holder(&claim_path(&path)).unwrap().token, owners[0]);
            assert!(release(&path, owners[0]));
        }
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }

    #[test]
    fn a_damaged_result_is_written_again() {
        let path = tmp("repair");
        let me = Me::new(1, 0, std::process::id());
        let (_, token) = look(&path, me, false, &mut None);
        let good = pack(&encode(b"{\"n\": 1}", &[]).unwrap()).unwrap();
        assert!(write_file(&path, &good, me.pid, token));
        assert!(release(&path, token));
        fs::write(&path, &good[..good.len() - 4]).unwrap(); // (cut short)
        // (an ordinary look still says ready; a reader that found it damaged asks with repair: its claim)
        assert_eq!(look(&path, me, false, &mut None).0, READY);
        let mut bad = None;
        let (st, token) = look(&path, me, true, &mut bad);
        assert_eq!(st, MINE);
        assert!(bad.is_some());
        // (the others wait for the repair, the damaged file not read again; nothing removed it)
        assert_eq!(look(&path, me, true, &mut bad).0, HELD);
        assert_eq!(wait(&path, me, 30, true).0, HELD);
        assert!(Path::new(&path).exists());
        assert!(write_file(&path, &good, me.pid, token));
        assert!(release(&path, token));
        assert_eq!(wait(&path, me, 30, true).0, READY);
        assert!(valid_file(&path));
        // (a valid file published meanwhile is read by a repair look, not taken for damaged)
        assert_eq!(look(&path, me, true, &mut None).0, READY);
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }

    #[test]
    fn two_threads_one_maker() {
        let path = tmp("race");
        let pid = std::process::id();
        // (eight takers at once: the claim is taken by exactly one)
        let n: usize = (0..8u32)
            .map(|_| {
                let p = path.clone();
                std::thread::spawn(move || look(&p, Me::new(1, 0, pid), false, &mut None).0 == MINE)
            })
            .collect::<Vec<_>>()
            .into_iter()
            .map(|h| h.join().unwrap() as usize)
            .sum();
        assert_eq!(n, 1);
        let _ = fs::remove_dir_all(Path::new(&path).parent().unwrap());
    }

    #[test]
    fn claim_text_round_trip() {
        let h = Holder { rank: 1, prio: -2, pid: 77, start: u64::MAX, token: 9, born: 123 };
        assert_eq!(Holder::parse(&h.text()), Some(h));
        assert_eq!(Holder::parse("1 0 77"), None); // (the claim of an older module, or cut short)
        assert_eq!(Holder::parse("1 0 77 1 2 3 4"), None);
        assert!(expired(&Holder { born: 0, ..h }, LEASE_MS + 1));
        assert!(!expired(&Holder { born: 1000, ..h }, 2000));
        assert!(expired(&Holder { born: 10 * LEASE_MS, ..h }, 5), "from before a restart");
    }
}
