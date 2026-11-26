//! Our packed photo copies in the game's terrain texture cache: what we wrote, what goes, and a one-time clean-up
//! (server/terrainpacks.py is the glue; earthd.Server._prune_packs / _record_pack / _base_cache_files call it).
//!
//! The server writes a BC7 pack of every block photo into <user>/temp/art/terrainmaterialcache/<key>.dds
//! (terraincache.base_entry; key = xxh64 of the photo's and the AO map's paths), so the game takes it instead of packing
//! the photo on its main thread. Those packs went only with a photo still on disk (earthd._base_cache_files): a photo
//! deleted any other way left its pack for good. 2026-11-25: 965 such packs (5.4 GB) on the dev PC, 687 (3.9 GB) on the
//! Steam Deck, after the one-time wipe of 2026-11-03.
//!
//! - Ledger: one line per pack we write ("<key> <session id> <unix time>") in the server's cache folder, recorded
//!   before the write. A session's packs go with the session (earthd._base_cache_files adds them), and at a start with
//!   no game running (earthd's guard) every recorded pack of a session that is gone goes (prune), whether or not its
//!   photo is still there; then the recorded packs over CAP_BYTES, oldest first (cap).
//! - wipe_v2: once (a marker), with no game running: the packs already leaked, recorded nowhere. Only files that are
//!   ours beyond doubt: a <16 hex>.dds file of a 2048 or 4096 px photo's exact size, our writer's exact DDS header
//!   (BC7 sRGB, the full mip chain: bc7.header), and every sampled block BC7 mode 6 (our encoders' only mode) with the
//!   flat AO 251 in its alpha endpoints (BASE_AO: the game's own packs of stock textures fail it, 37 of 37 tried), and
//!   no photo on disk (any session) or live ledger entry with its key. A folder holding anything else is left alone.
//!
//! Ported from Python (2026-11-25, earthfast 635), the same files kept and deleted and the same ledger bytes (the
//! last line of a key wins at its first line's place; times compared as the whole numbers they are, any length; the
//! platform's text line ends, "\r\n" on Windows, as Python's text files wrote them; "\r", "\n" and "\r\n" all read).
//! One deliberate change: an entry of the pack folder is never followed through a link. A symbolic link (or anything
//! else that is no regular file) at a pack's name counts as no pack: prune and cap neither measure nor delete it, and
//! is_ours rejects it (Python measured and read the link's target, then unlinked the link). The folder itself may be
//! a link (the game follows it too). Sizes and times are integers throughout; no hash-map order reaches any output.

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;

pub const LEDGER: &str = "terrainpacks.log"; // in the server's cache folder
pub const WIPE_MARK: &str = "terrainpacks_wiped_v2";
pub const CAP_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const BASE_AO: i64 = 251; // the alpha of our photo packs (earthd.BASE_AO, the level's flat AO map)
const AO_TOL: i64 = 2; // (mode 6 alpha endpoints of a flat 251 come out 251 or 252)
const SAMPLE_BLOCKS: u64 = 256;
const TAIL_BLOCKS: u64 = 32; // the last mips' blocks, every one
const OURS_PX: [u64; 2] = [2048, 4096];
const HEADER_LEN: usize = 148;
const DXGI_BC7_UNORM_SRGB: u32 = 99;
const NL: &str = if cfg!(windows) { "\r\n" } else { "\n" }; // (Python's text files: os.linesep)

/// The ledger's file is read and written by one thread at a time (Python: Ledger.lock; every Server has one ledger).
static LEDGER_LOCK: Mutex<()> = Mutex::new(());

fn ledger_lock() -> std::sync::MutexGuard<'static, ()> {
    LEDGER_LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

/// 16 hex digits (Python: [0-9a-fA-F]{16}).
fn is_key(s: &str) -> bool {
    s.len() == 16 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

/// "<16 hex>.dds", exactly (PACK_NAME.fullmatch: the extension lower case).
fn is_pack_name(name: &str) -> bool {
    name.len() == 20 && name.ends_with(".dds") && is_key(&name[..16])
}

/// Python's str.split() whitespace among the characters an ASCII decode can give.
fn py_space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r' | '\x1c' | '\x1d' | '\x1e' | '\x1f')
}

/// A ledger time: a whole number of any length as its digits without leading zeros (Python's int of the line's
/// digits; ordered as numbers).
#[derive(Clone, Debug, PartialEq, Eq)]
struct Time(String);

impl Time {
    fn parse(digits: &str) -> Option<Time> {
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let t = digits.trim_start_matches('0');
        Some(Time(if t.is_empty() { "0".to_string() } else { t.to_string() }))
    }
}

impl Ord for Time {
    fn cmp(&self, o: &Self) -> Ordering {
        self.0.len().cmp(&o.0.len()).then_with(|| self.0.cmp(&o.0))
    }
}

impl PartialOrd for Time {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Entry {
    key: String, // lower case
    sid: String,
    t: Time,
}

/// The ledger's entries in Python dict order: a key at its first line's place with its last line's session and time.
/// Bytes over 0x7F read as U+FFFD (Python's ASCII decode, errors="replace"); "\r\n", "\r" and "\n" end lines
/// (universal newlines); a line counts with exactly 3 fields, a key and a time of digits.
fn parse_ledger(bytes: &[u8]) -> Vec<Entry> {
    let text: String = bytes.iter().map(|&b| if b < 0x80 { b as char } else { '\u{FFFD}' }).collect();
    let mut out: Vec<Entry> = Vec::new();
    let mut at: BTreeMap<String, usize> = BTreeMap::new();
    let text = text.replace("\r\n", "\n");
    for line in text.split(['\n', '\r']) {
        let parts: Vec<&str> = line.split(py_space).filter(|p| !p.is_empty()).collect();
        if parts.len() != 3 || !is_key(parts[0]) {
            continue;
        }
        let Some(t) = Time::parse(parts[2]) else { continue };
        let e = Entry { key: parts[0].to_ascii_lowercase(), sid: parts[1].to_string(), t };
        match at.get(&e.key) {
            Some(&i) => out[i] = e,
            None => {
                at.insert(e.key.clone(), out.len());
                out.push(e);
            }
        }
    }
    out
}

/// The entries (none for a ledger that cannot be read). Takes the lock.
fn read_entries(path: &Path) -> Vec<Entry> {
    let _g = ledger_lock();
    fs::read(path).map(|b| parse_ledger(&b)).unwrap_or_default()
}

enum WriteError {
    Io(String),
    NotAscii(String),
}

fn line(k: &str, s: &str, t: &str) -> String {
    format!("{k} {s} {t}{NL}")
}

/// The ledger as `entries`, sorted by time (stable: ties in entry order), through a temporary file renamed into
/// place (a crash keeps the old one). A session that is not ASCII stops it as Python's ASCII encoder did: the lines
/// before it in the temporary file, the ledger untouched, an error. Takes the lock.
fn rewrite(path: &Path, entries: &[Entry]) -> Result<(), WriteError> {
    let _g = ledger_lock();
    let mut tmp = OsString::from(path.as_os_str());
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let mut sorted: Vec<&Entry> = entries.iter().collect();
    sorted.sort_by(|a, b| a.t.cmp(&b.t));
    let mut body = String::new();
    let mut bad = None;
    for e in sorted {
        if !e.sid.is_ascii() {
            bad = Some(e.sid.clone());
            break;
        }
        body.push_str(&line(&e.key, &e.sid, &e.t.0));
    }
    let wrote = fs::File::create(&tmp).and_then(|mut f| f.write_all(body.as_bytes()));
    if let Err(e) = wrote {
        return Err(WriteError::Io(e.to_string()));
    }
    if let Some(s) = bad {
        return Err(WriteError::NotAscii(s));
    }
    fs::rename(&tmp, path).map_err(|e| WriteError::Io(e.to_string()))
}

/// One line appended (the directory made first). Python's order: the file opened (made) before the ASCII encoder
/// refused a line, which then wrote nothing.
fn append(path: &Path, text: &str) -> Result<(), WriteError> {
    let _g = ledger_lock();
    let opened = (|| -> io::Result<fs::File> {
        if let Some(d) = path.parent() {
            if d.as_os_str().is_empty() {
                return Err(io::Error::new(io::ErrorKind::NotFound, "no folder"));
            }
            fs::create_dir_all(d)?;
        }
        fs::OpenOptions::new().append(true).create(true).open(path)
    })();
    let mut f = opened.map_err(|e| WriteError::Io(e.to_string()))?;
    if !text.is_ascii() {
        return Err(WriteError::NotAscii(text.trim_end().to_string()));
    }
    f.write_all(text.as_bytes()).map_err(|e| WriteError::Io(e.to_string()))
}

/// The size of the regular file at path; None for none, or a link, a folder or anything else (never followed).
fn pack_size(path: &Path) -> Option<u64> {
    match fs::symlink_metadata(path) {
        Ok(m) if m.file_type().is_file() => Some(m.len()),
        _ => None,
    }
}

/// path deleted (a link itself, never what it points at: remove_file does not follow); false for one gone already,
/// other errors noted.
fn remove(path: &Path, notes: &mut Vec<String>) -> bool {
    match fs::remove_file(path) {
        Ok(()) => true,
        Err(e) if e.kind() == io::ErrorKind::NotFound => false,
        Err(e) => {
            notes.push(format!("terrain pack {}: {e}", path.display()));
            false
        }
    }
}

fn pack_path(pdir: &Path, key: &str) -> PathBuf {
    pdir.join(format!("{key}.dds"))
}

fn ledger_note(notes: &mut Vec<String>, r: Result<(), WriteError>) -> Result<(), String> {
    match r {
        Ok(()) => Ok(()),
        Err(WriteError::Io(e)) => {
            notes.push(format!("terrain pack ledger: {e}"));
            Ok(())
        }
        Err(WriteError::NotAscii(s)) => Err(format!("the terrain pack ledger takes ASCII only: {s:?}")),
    }
}

struct Outcome {
    n: u64,
    freed: u64,
    notes: Vec<String>,
}

/// Every recorded pack of a session not in `live` deleted, its line dropped; lines of packs gone dropped too.
fn prune(ledger: &Path, pdir: &Path, live: &BTreeSet<String>) -> Result<Outcome, String> {
    let entries = read_entries(ledger);
    let mut keep = Vec::with_capacity(entries.len());
    let mut o = Outcome { n: 0, freed: 0, notes: Vec::new() };
    for e in &entries {
        let path = pack_path(pdir, &e.key);
        let size = pack_size(&path);
        if live.contains(&e.sid) {
            keep.push(e.clone());
        } else if let Some(size) = size {
            if remove(&path, &mut o.notes) {
                o.n += 1;
                o.freed += size;
            }
        }
    }
    if keep.len() != entries.len() {
        let r = rewrite(ledger, &keep);
        ledger_note(&mut o.notes, r)?;
    }
    Ok(o)
}

/// The recorded packs over max_bytes in all deleted, oldest first (ties by key; keys in protect kept: the session in
/// use).
fn cap(ledger: &Path, pdir: &Path, max_bytes: i64, protect: &BTreeSet<String>) -> Result<Outcome, String> {
    let mut entries = read_entries(ledger);
    let mut live: Vec<(Time, String, u64)> = entries
        .iter()
        .filter_map(|e| pack_size(&pack_path(pdir, &e.key)).map(|s| (e.t.clone(), e.key.clone(), s)))
        .collect();
    let mut total: i128 = live.iter().map(|l| l.2 as i128).sum();
    live.sort();
    let mut o = Outcome { n: 0, freed: 0, notes: Vec::new() };
    let mut gone = BTreeSet::new();
    for (_t, key, size) in live {
        if total <= max_bytes as i128 {
            break;
        }
        if protect.contains(&key) {
            continue;
        }
        if remove(&pack_path(pdir, &key), &mut o.notes) {
            total -= size as i128;
            o.n += 1;
            o.freed += size;
            gone.insert(key);
        }
    }
    if o.n > 0 {
        entries.retain(|e| !gone.contains(&e.key));
        let r = rewrite(ledger, &entries);
        ledger_note(&mut o.notes, r)?;
    }
    Ok(o)
}

/// The file size of our pack of a px x px photo: the header and every mip's BC7 blocks (px >= 1).
pub fn ours_size(px: u64) -> u64 {
    let (mut total, mut w) = (0u64, px);
    loop {
        let n = w.div_ceil(4).max(1); // (BC7 blocks per side)
        total += n * n * 16;
        if w <= 1 {
            break;
        }
        w /= 2;
    }
    HEADER_LEN as u64 + total
}

fn ours_px(size: u64) -> Option<u64> {
    OURS_PX.iter().copied().find(|&px| ours_size(px) == size)
}

/// bc7.header(px, px, px.bit_length()): our writer's 148 bytes (DX10, BC7 sRGB, the full mip chain).
fn header(px: u64) -> Vec<u8> {
    let w = px as u32;
    let levels = 64 - px.leading_zeros();
    let mut o = Vec::with_capacity(HEADER_LEN);
    let mut u = |v: u32| o.extend_from_slice(&v.to_le_bytes());
    let blocks = w.div_ceil(4).max(1);
    let (dds, dx10) = (u32::from_le_bytes(*b"DDS "), u32::from_le_bytes(*b"DX10"));
    for v in [dds, 124, 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000, w, w, blocks * blocks * 16, 1, levels] {
        u(v);
    }
    for _ in 0..11 {
        u(0); // reserved (44 bytes)
    }
    for v in [32, 0x4, dx10, 0, 0, 0, 0, 0] {
        u(v); // pixel format: FOURCC DX10
    }
    for v in [0x401008, 0, 0, 0, 0] {
        u(v); // caps: COMPLEX TEXTURE MIPMAP; reserved2
    }
    for v in [DXGI_BC7_UNORM_SRGB, 3, 0, 1, 0] {
        u(v); // format, TEXTURE2D, -, 1, -
    }
    o
}

/// The blocks sampled of n: SAMPLE_BLOCKS spread from the first to the last (numpy's linspace(0, n - 1, 256) cut to
/// integers, in integers: the same indices for both pack sizes, the unit test checks it) and the last TAIL_BLOCKS.
fn sample_blocks(n: u64) -> BTreeSet<u64> {
    let mut idx = BTreeSet::new();
    if n == 0 {
        return idx;
    }
    for i in 0..SAMPLE_BLOCKS {
        idx.insert(i * (n - 1) / (SAMPLE_BLOCKS - 1));
    }
    for i in n.saturating_sub(TAIL_BLOCKS)..n {
        idx.insert(i);
    }
    idx
}

/// A BC7 mode 6 block with both alpha endpoints within AO_TOL of BASE_AO.
fn ours_block(b: &[u8]) -> bool {
    if b[0] & 0x7F != 0x40 {
        return false; // mode 6: the mode bits 0000001
    }
    let lo = u64::from_le_bytes(b[..8].try_into().unwrap());
    let hi = u64::from_le_bytes(b[8..16].try_into().unwrap());
    let a0 = ((((lo >> 49) & 0x7F) << 1) | ((lo >> 63) & 1)) as i64;
    let a1 = ((((lo >> 56) & 0x7F) << 1) | (hi & 1)) as i64;
    (a0 - BASE_AO).abs() <= AO_TOL && (a1 - BASE_AO).abs() <= AO_TOL
}

/// Is this a photo pack our writer made, beyond doubt? (a regular file, never a link; its name, size, header, and
/// mode 6 with the flat AO in every sampled block, from the top mip to the last)
fn is_ours(path: &Path) -> bool {
    let Some(size) = pack_size(path) else { return false };
    let Some(px) = ours_px(size) else { return false };
    if !path.file_name().and_then(|n| n.to_str()).is_some_and(is_pack_name) {
        return false;
    }
    let Ok(mut f) = fs::File::open(path) else { return false };
    let mut head = Vec::with_capacity(HEADER_LEN);
    if (&mut f).take(HEADER_LEN as u64).read_to_end(&mut head).is_err() || head != header(px) {
        return false;
    }
    let mut body = Vec::new();
    if f.read_to_end(&mut body).is_err() {
        return false;
    }
    let n = (body.len() / 16) as u64;
    if n * 16 + HEADER_LEN as u64 != size {
        return false;
    }
    sample_blocks(n).iter().all(|&i| ours_block(&body[i as usize * 16..i as usize * 16 + 16]))
}

/// The last 3 parts of os.path.normpath(p) lower case (separators "/", and "\\" on Windows; "." dropped, ".." taking
/// the part before it, at the root dropped, at the front of a relative path kept).
fn tail_parts(p: &str) -> Vec<String> {
    let seps: &[char] = if cfg!(windows) { &['/', '\\'] } else { &['/'] };
    let absolute = p.starts_with(seps);
    let mut parts: Vec<&str> = Vec::new();
    for c in p.split(seps) {
        match c {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|l| *l != "..") {
                    parts.pop();
                } else if !absolute {
                    parts.push("..");
                }
            }
            _ => parts.push(c),
        }
    }
    parts[parts.len().saturating_sub(3)..].iter().map(|s| s.to_lowercase()).collect()
}

/// Does pdir look like the game's terrain texture cache and nothing else? (named so, only <16 hex>.dds regular files:
/// no link, folder or other file)
fn familiar(pdir: &Path) -> bool {
    if tail_parts(&pdir.to_string_lossy()) != ["temp", "art", "terrainmaterialcache"] {
        return false;
    }
    let Ok(rd) = fs::read_dir(pdir) else { return false };
    for e in rd {
        let Ok(e) = e else { return false };
        let file = e.file_type().is_ok_and(|t| t.is_file());
        if !(file && e.file_name().to_str().is_some_and(is_pack_name)) {
            return false;
        }
    }
    true
}

enum Wipe {
    Marked,     // done before
    NoFolder,   // no such folder
    Unfamiliar, // a folder holding anything else: left alone
    Done(Outcome),
}

/// Once (the mark written after it ran): our photo packs no photo on disk or live ledger entry has (keep) deleted.
fn wipe_v2(pdir: &Path, keep: &BTreeSet<String>, mark: &Path) -> Result<Wipe, String> {
    if mark.exists() {
        return Ok(Wipe::Marked);
    }
    if !pdir.is_dir() {
        return Ok(Wipe::NoFolder);
    }
    if !familiar(pdir) {
        return Ok(Wipe::Unfamiliar);
    }
    let keep: BTreeSet<String> = keep.iter().map(|k| k.to_lowercase()).collect();
    let mut names = Vec::new();
    for e in fs::read_dir(pdir).map_err(|e| format!("{}: {e}", pdir.display()))? {
        names.push(e.map_err(|e| format!("{}: {e}", pdir.display()))?.file_name());
    }
    let mut o = Outcome { n: 0, freed: 0, notes: Vec::new() };
    for name in names {
        let path = pdir.join(&name);
        let Some(name) = name.to_str() else { continue }; // (no pack name: is_ours says no)
        let key = name.get(..name.len().saturating_sub(4)).unwrap_or("").to_lowercase();
        if keep.contains(&key) || !is_ours(&path) {
            continue;
        }
        let size = pack_size(&path).unwrap_or(0);
        if remove(&path, &mut o.notes) {
            o.n += 1;
            o.freed += size;
        }
    }
    // (Python's os.makedirs("") failed for a mark without a folder: no mark then)
    if let Some(d) = mark.parent().filter(|d| !d.as_os_str().is_empty()) {
        if fs::create_dir_all(d).is_ok() {
            let _ = fs::File::create(mark);
        }
    }
    Ok(Wipe::Done(o))
}

// ---- Python ----

fn set_of(v: Vec<String>) -> BTreeSet<String> {
    v.into_iter().collect()
}

/// terrainpacks.Ledger.record: "<key> <sid> <t>" appended to the ledger at path. Returns None, or the error to log
/// (debug); ValueError for a line that is not ASCII (Python's UnicodeEncodeError).
#[pyfunction]
fn tpacks_record(py: Python<'_>, path: PathBuf, key: String, sid: String, t: String) -> PyResult<Option<String>> {
    let text = line(&key, &sid, &t);
    match py.detach(|| append(&path, &text)) {
        Ok(()) => Ok(None),
        Err(WriteError::Io(e)) => Ok(Some(e)),
        Err(WriteError::NotAscii(s)) => Err(PyValueError::new_err(format!("the terrain pack ledger takes ASCII only: {s:?}"))),
    }
}

fn py_int(py: Python<'_>, t: &Time) -> PyResult<Py<PyAny>> {
    match t.0.parse::<u64>() {
        Ok(v) => Ok(v.into_pyobject(py)?.into_any().unbind()),
        Err(_) => Ok(py.import("builtins")?.getattr("int")?.call1((t.0.as_str(),))?.unbind()), // (longer than u64)
    }
}

/// terrainpacks.Ledger.entries: [(key, sid, unix time)] in the dict's order.
#[pyfunction]
fn tpacks_entries(py: Python<'_>, path: PathBuf) -> PyResult<Vec<(String, String, Py<PyAny>)>> {
    let entries = py.detach(|| read_entries(&path));
    entries.into_iter().map(|e| Ok((e.key, e.sid, py_int(py, &e.t)?))).collect()
}

/// terrainpacks.Ledger.keys_of: the keys recorded for session sid, in the ledger's order.
#[pyfunction]
fn tpacks_keys_of(py: Python<'_>, path: PathBuf, sid: String) -> Vec<String> {
    py.detach(|| read_entries(&path).into_iter().filter(|e| e.sid == sid).map(|e| e.key).collect())
}

type Counted = (u64, u64, Vec<String>);

/// terrainpacks.prune: (files deleted, bytes, notes to log at debug).
#[pyfunction]
fn tpacks_prune(py: Python<'_>, ledger: Option<PathBuf>, pdir: PathBuf, live: Vec<String>) -> PyResult<Counted> {
    let Some(ledger) = ledger else { return Ok((0, 0, Vec::new())) };
    let live = set_of(live);
    let o = py.detach(|| prune(&ledger, &pdir, &live)).map_err(PyValueError::new_err)?;
    Ok((o.n, o.freed, o.notes))
}

/// terrainpacks.cap: (files deleted, bytes, notes); max_bytes None: CAP_BYTES.
#[pyfunction]
#[pyo3(signature = (ledger, pdir, max_bytes, protect))]
fn tpacks_cap(
    py: Python<'_>,
    ledger: Option<PathBuf>,
    pdir: PathBuf,
    max_bytes: Option<i64>,
    protect: Vec<String>,
) -> PyResult<Counted> {
    let Some(ledger) = ledger else { return Ok((0, 0, Vec::new())) };
    let protect = set_of(protect);
    let max = max_bytes.unwrap_or(CAP_BYTES as i64);
    let o = py.detach(|| cap(&ledger, &pdir, max, &protect)).map_err(PyValueError::new_err)?;
    Ok((o.n, o.freed, o.notes))
}

#[pyfunction]
fn tpacks_ours_size(px: i64) -> PyResult<u64> {
    if px < 1 {
        return Err(PyValueError::new_err(format!("terrainpacks.ours_size: {px} px")));
    }
    Ok(ours_size(px as u64))
}

#[pyfunction]
fn tpacks_is_ours(py: Python<'_>, path: PathBuf) -> bool {
    py.detach(|| is_ours(&path))
}

#[pyfunction]
fn tpacks_familiar(py: Python<'_>, pdir: PathBuf) -> bool {
    py.detach(|| familiar(&pdir))
}

/// terrainpacks.wipe_v2: (what happened: "done" / "marked" / "nofolder" / "unfamiliar", files deleted, bytes, notes).
#[pyfunction]
fn tpacks_wipe_v2(py: Python<'_>, pdir: PathBuf, keep: Vec<String>, mark: PathBuf) -> PyResult<(&'static str, u64, u64, Vec<String>)> {
    let keep = set_of(keep);
    let w = py.detach(|| wipe_v2(&pdir, &keep, &mark)).map_err(PyOSError::new_err)?;
    Ok(match w {
        Wipe::Marked => ("marked", 0, 0, Vec::new()),
        Wipe::NoFolder => ("nofolder", 0, 0, Vec::new()),
        Wipe::Unfamiliar => ("unfamiliar", 0, 0, Vec::new()),
        Wipe::Done(o) => ("done", o.n, o.freed, o.notes),
    })
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tpacks_record, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_entries, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_keys_of, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_prune, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_cap, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_ours_size, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_is_ours, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_familiar, m)?)?;
    m.add_function(wrap_pyfunction!(tpacks_wipe_v2, m)?)?;
    m.add("TPACKS_LEDGER", LEDGER)?;
    m.add("TPACKS_WIPE_MARK", WIPE_MARK)?;
    m.add("TPACKS_CAP_BYTES", CAP_BYTES)?;
    m.add("TPACKS_BASE_AO", BASE_AO)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const A: &str = "0123456789abcdef";
    const B: &str = "fedcba9876543210";
    const C: &str = "00000000000000aa";
    const D: &str = "1111111111111111";

    /// A fresh folder of its own (under TMPDIR), removed at the end.
    struct Tmp(PathBuf);
    impl Tmp {
        fn new(name: &str) -> Tmp {
            let d = std::env::temp_dir().join(format!("tpacks_{}_{name}", std::process::id()));
            let _ = fs::remove_dir_all(&d);
            fs::create_dir_all(&d).unwrap();
            Tmp(d)
        }
        fn pdir(&self) -> PathBuf {
            self.0.join("user").join("temp").join("art").join("terrainmaterialcache")
        }
        fn ledger(&self) -> PathBuf {
            self.0.join("cache").join(LEDGER)
        }
    }
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn write(path: &Path, data: &[u8]) -> PathBuf {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, data).unwrap();
        path.to_path_buf()
    }

    fn set(v: &[&str]) -> BTreeSet<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    fn record(led: &Path, key: &str, sid: &str, t: u64) {
        assert!(append(led, &line(key, sid, &t.to_string())).is_ok());
    }

    fn keys(led: &Path) -> Vec<(String, String, String)> {
        read_entries(led).into_iter().map(|e| (e.key, e.sid, e.t.0)).collect()
    }

    fn key_list(led: &Path) -> Vec<String> {
        keys(led).into_iter().map(|e| e.0).collect()
    }

    /// A pack as our writer makes it: the header, every block mode 6 with both alpha endpoints `alpha`.
    fn pack(px: u64, alpha: u64) -> Vec<u8> {
        let mut o = header(px);
        let lo: u64 = 0x40 | (((alpha >> 1) & 0x7F) << 49) | (((alpha >> 1) & 0x7F) << 56) | ((alpha & 1) << 63);
        let hi: u64 = alpha & 1;
        let n = (ours_size(px) as usize - HEADER_LEN) / 16;
        for _ in 0..n {
            o.extend_from_slice(&lo.to_le_bytes());
            o.extend_from_slice(&hi.to_le_bytes());
        }
        o
    }

    #[test]
    fn sizes_and_header() {
        assert_eq!(ours_size(2048), 5592580);
        assert_eq!(ours_size(4096), 22369796);
        assert_eq!(ours_size(1), 148 + 16);
        assert_eq!(ours_px(5592580), Some(2048));
        assert_eq!(ours_px(ours_size(1024)), None, "a stock 1024 pack's size is no pack of ours");
        let h = header(2048);
        assert_eq!(h.len(), HEADER_LEN);
        assert_eq!(&h[..4], b"DDS ");
        assert_eq!(u32::from_le_bytes(h[28..32].try_into().unwrap()), 12, "the full mip chain");
        assert_eq!(&h[84..88], b"DX10");
        assert_eq!(u32::from_le_bytes(h[128..132].try_into().unwrap()), DXGI_BC7_UNORM_SRGB);
        assert_eq!(pack(2048, 251).len() as u64, ours_size(2048));
    }

    #[test]
    fn samples_are_numpys_linspace() {
        // numpy.linspace(0, n - 1, 256).astype(int64): i * ((n - 1) / 255.0), the last n - 1; the integer formula
        // gives the same indices for both pack sizes (not for every n: 156 differs)
        for px in OURS_PX {
            let n = (ours_size(px) - HEADER_LEN as u64) / 16;
            let step = (n - 1) as f64 / 255.0;
            let mut np: BTreeSet<u64> = (0..255).map(|i| (i as f64 * step) as u64).collect();
            np.insert(n - 1);
            np.extend(n - 32..n);
            assert_eq!(sample_blocks(n), np, "{px} px");
        }
    }

    #[test]
    fn ledger_reads_python_files() {
        let t = Tmp::new("ledger");
        let led = t.ledger();
        record(&led, A, "s1", 100);
        record(&led, B, "s2", 200);
        record(&led, &A.to_uppercase(), "s3", 300); // (the last line of a key wins at its first place; lower case)
        let mut f = fs::OpenOptions::new().append(true).open(&led).unwrap();
        // garbage, a short key, a time not digits, 4 fields, Windows / old Mac line ends, a byte over 0x7F, a long time
        f.write_all(b"garbage line\nzz s 1\n1111111111111111 s -5\n1111111111111111 s 5 x\r\n").unwrap();
        f.write_all(b"00000000000000aa a\xffb 0007\r\n2222222222222222 q 123456789012345678901234567890\r").unwrap();
        f.write_all(b"3333333333333333\tw\x0b9\n4444444444444444 w\r9\n").unwrap();
        drop(f);
        let want: Vec<(String, String, String)> = vec![
            (A.into(), "s3".into(), "300".into()),
            (B.into(), "s2".into(), "200".into()),
            (C.into(), "a\u{FFFD}b".into(), "7".into()),
            ("2222222222222222".into(), "q".into(), "123456789012345678901234567890".into()),
            ("3333333333333333".into(), "w".into(), "9".into()),
        ];
        assert_eq!(keys(&led), want);
        assert!(read_entries(&t.0.join("nowhere")).is_empty());
    }

    #[test]
    fn record_refuses_non_ascii_and_writes_nothing() {
        let t = Tmp::new("ascii");
        let led = t.ledger();
        assert!(matches!(append(&led, &line(A, "sé", "1")), Err(WriteError::NotAscii(_))));
        assert_eq!(fs::read(&led).unwrap(), b"", "opened (made), nothing written");
        assert!(matches!(append(Path::new(LEDGER), &line(A, "s", "1")), Err(WriteError::Io(_))), "no folder");
    }

    #[test]
    fn rewrite_sorted_by_time_ties_in_order() {
        let t = Tmp::new("rewrite");
        let led = t.ledger();
        for (k, s, tt) in [(A, "x", 5), (B, "y", 1), (C, "z", 5), (D, "w", 3)] {
            record(&led, k, s, tt);
        }
        assert!(rewrite(&led, &read_entries(&led)).is_ok());
        let want = format!("{B} y 1{NL}{D} w 3{NL}{A} x 5{NL}{C} z 5{NL}");
        assert_eq!(String::from_utf8(fs::read(&led).unwrap()).unwrap(), want);
        // a session the ASCII encoder refused: the ledger kept, the lines before it in the temporary file
        let mut e = read_entries(&led);
        e[3].sid = "\u{FFFD}".into(); // (C, time 5: the last of the rewritten ledger)
        assert!(matches!(rewrite(&led, &e), Err(WriteError::NotAscii(_))));
        assert_eq!(String::from_utf8(fs::read(&led).unwrap()).unwrap(), want);
        let tmp = format!("{}.tmp", led.display());
        assert_eq!(fs::read_to_string(tmp).unwrap(), format!("{B} y 1{NL}{D} w 3{NL}{A} x 5{NL}"));
    }

    #[test]
    fn prune_by_session_not_photo() {
        let t = Tmp::new("prune");
        let (led, pdir) = (t.ledger(), t.pdir());
        for (k, s, tt) in [(A, "gone", 1), (B, "live", 2), (C, "gone", 3)] {
            record(&led, k, s, tt);
        }
        let a = write(&pack_path(&pdir, A), &[b'x'; 10]);
        let b = write(&pack_path(&pdir, B), &[b'y'; 20]);
        // (C's pack never written: its line goes too)
        let o = prune(&led, &pdir, &set(&["live"])).ok().unwrap();
        assert_eq!((o.n, o.freed), (1, 10));
        assert!(!a.exists() && b.exists());
        assert_eq!(keys(&led), vec![(B.to_string(), "live".to_string(), "2".to_string())]);
        let before = fs::read(&led).unwrap();
        let o = prune(&led, &pdir, &set(&["live"])).ok().unwrap();
        assert_eq!((o.n, o.freed), (0, 0));
        assert_eq!(fs::read(&led).unwrap(), before, "nothing dropped: the ledger as it was");
    }

    #[test]
    fn cap_oldest_first_and_protected() {
        let t = Tmp::new("cap");
        let (led, pdir) = (t.ledger(), t.pdir());
        for (i, k) in [A, B, C, D].iter().enumerate() {
            record(&led, k, "s", 10 + i as u64);
            write(&pack_path(&pdir, k), &[b'z'; 100]);
        }
        let o = cap(&led, &pdir, 250, &set(&[A])).ok().unwrap();
        assert_eq!((o.n, o.freed), (2, 200));
        let left: Vec<&str> = [A, B, C, D].into_iter().filter(|k| pack_path(&pdir, k).exists()).collect();
        assert_eq!(left, vec![A, D], "the oldest unprotected went (B, C); A protected, D newest");
        assert_eq!(key_list(&led), vec![A.to_string(), D.to_string()]);
        let o = cap(&led, &pdir, 250, &BTreeSet::new()).ok().unwrap();
        assert_eq!((o.n, o.freed), (0, 0), "under the cap: nothing");
        // equal times: by key; a line of a pack gone stays (cap drops only what it deleted)
        record(&led, C, "s", 13);
        write(&pack_path(&pdir, C), &[b'z'; 100]);
        record(&led, B, "s", 13);
        let o = cap(&led, &pdir, 250, &BTreeSet::new()).ok().unwrap();
        assert_eq!((o.n, o.freed), (1, 100));
        assert!(!pack_path(&pdir, A).exists(), "the oldest (A, 10)");
        assert_eq!(key_list(&led), vec![D.to_string(), C.to_string(), B.to_string()]);
    }

    /// Guard: a link at a pack's name is never followed (no size through it, not deleted, nor what it points at).
    #[cfg(unix)]
    #[test]
    fn prune_and_cap_never_follow_links() {
        use std::os::unix::fs::symlink;
        let t = Tmp::new("links");
        let (led, pdir) = (t.ledger(), t.pdir());
        let outside = write(&t.0.join("elsewhere").join("precious.dds"), &[b'p'; 1000]);
        fs::create_dir_all(&pdir).unwrap();
        symlink(&outside, pack_path(&pdir, A)).unwrap();
        fs::create_dir_all(pack_path(&pdir, B)).unwrap(); // (a folder at a pack's name)
        record(&led, A, "gone", 1);
        record(&led, B, "gone", 2);
        let o = prune(&led, &pdir, &BTreeSet::new()).ok().unwrap();
        assert_eq!((o.n, o.freed), (0, 0));
        assert!(outside.exists() && fs::symlink_metadata(pack_path(&pdir, A)).is_ok() && pack_path(&pdir, B).is_dir());
        assert!(keys(&led).is_empty(), "their lines dropped as for packs gone");
        record(&led, A, "s", 1);
        let o = cap(&led, &pdir, 0, &BTreeSet::new()).ok().unwrap();
        assert_eq!((o.n, o.freed), (0, 0), "a link counts nothing and goes nowhere");
        assert!(outside.exists() && fs::symlink_metadata(pack_path(&pdir, A)).is_ok());
    }

    #[test]
    fn strict_matching() {
        let t = Tmp::new("ours");
        let pdir = t.pdir();
        assert!(is_ours(&write(&pack_path(&pdir, A), &pack(2048, 251))));
        assert!(is_ours(&write(&pack_path(&pdir, "fffffffffffffff0"), &pack(4096, 252))), "4096 px, 252 in AO_TOL");
        assert!(!is_ours(&write(&pack_path(&pdir, B), &pack(2048, 255))), "the game's own alpha");
        assert!(!is_ours(&write(&pack_path(&pdir, "2222222222222222"), &pack(2048, 248))), "251 - 3");
        let mut body = pack(2048, 251);
        body[HEADER_LEN + 16 * 349526] = 0x20; // (mode 5 in the last block)
        assert!(!is_ours(&write(&pack_path(&pdir, C), &body)), "another mode among the sampled blocks");
        let mut body = pack(2048, 251);
        body[HEADER_LEN + 16 * 1000] = 0x20; // (a block no sample reads: ours still, as numpy's samples said)
        assert!(is_ours(&write(&pack_path(&pdir, "3333333333333333"), &body)));
        let mut lin = pack(2048, 251);
        lin[128] = 98; // (DXGI_BC7_UNORM: a linear header)
        assert!(!is_ours(&write(&pack_path(&pdir, D), &lin)));
        let cut = pack(2048, 251);
        assert!(!is_ours(&write(&pack_path(&pdir, "4444444444444444"), &cut[..cut.len() - 16])), "a cut file");
        assert!(!is_ours(&write(&pack_path(&pdir, "5555555555555555"), &pack(1024, 251))), "another size");
        assert!(!is_ours(&write(&t.0.join("notakey.dds"), &pack(2048, 251))), "another name");
        assert!(!is_ours(&write(&pdir.join("6666666666666666.DDS"), &pack(2048, 251))), "the extension's case");
        assert!(!is_ours(&pdir.join("7777777777777777.dds")), "none there");
    }

    /// Guard: a link to one of our packs is no pack of ours (never read through).
    #[cfg(unix)]
    #[test]
    fn is_ours_rejects_links() {
        let t = Tmp::new("ourslink");
        let pdir = t.pdir();
        let real = write(&t.0.join("elsewhere").join(format!("{A}.dds")), &pack(2048, 251));
        fs::create_dir_all(&pdir).unwrap();
        std::os::unix::fs::symlink(&real, pack_path(&pdir, A)).unwrap();
        assert!(is_ours(&real) && !is_ours(&pack_path(&pdir, A)));
    }

    #[test]
    fn familiar_folders() {
        let t = Tmp::new("familiar");
        let pdir = t.pdir();
        write(&pack_path(&pdir, A), b"x");
        assert!(familiar(&pdir));
        assert!(!familiar(pdir.parent().unwrap()), "not named so");
        let odd = PathBuf::from(format!("{}/./", pdir.display()));
        assert!(familiar(&odd), "normpath: . and the trailing separator");
        assert_eq!(tail_parts("/a/TEMP/Art/TerrainMaterialCache/."), ["temp", "art", "terrainmaterialcache"]);
        assert_eq!(tail_parts("art/terrainmaterialcache"), ["art", "terrainmaterialcache"]);
        assert_eq!(tail_parts("../../x"), ["..", "..", "x"]);
        assert_eq!(tail_parts("/../../x"), ["x"]);
        fs::create_dir_all(pdir.join("sub")).unwrap();
        assert!(!familiar(&pdir), "a folder in it");
        fs::remove_dir(pdir.join("sub")).unwrap();
        write(&pdir.join("readme.txt"), b"x");
        assert!(!familiar(&pdir), "a file that is no pack");
        assert!(!familiar(&t.0.join("nowhere").join("temp").join("art").join("terrainmaterialcache")));
    }

    /// Guard: a link among the packs makes the folder unfamiliar (no wipe at all).
    #[cfg(unix)]
    #[test]
    fn familiar_rejects_links() {
        let t = Tmp::new("famlink");
        let pdir = t.pdir();
        let real = write(&t.0.join("elsewhere").join(format!("{A}.dds")), &pack(2048, 251));
        write(&pack_path(&pdir, B), b"x");
        std::os::unix::fs::symlink(&real, pack_path(&pdir, A)).unwrap();
        assert!(!familiar(&pdir));
        let mark = t.0.join("cache").join(WIPE_MARK);
        assert!(matches!(wipe_v2(&pdir, &BTreeSet::new(), &mark), Ok(Wipe::Unfamiliar)));
        assert!(real.exists() && !mark.exists());
    }

    #[test]
    fn wipe_only_ours_without_a_photo_once() {
        let t = Tmp::new("wipe");
        let pdir = t.pdir();
        let mark = t.0.join("cache").join(WIPE_MARK);
        let leaked = write(&pack_path(&pdir, A), &pack(2048, 251));
        let kept = write(&pack_path(&pdir, B), &pack(2048, 251)); // its photo is on disk
        let decoy = write(&pack_path(&pdir, C), &pack(2048, 255)); // the game's own
        let small = write(&pack_path(&pdir, D), &pack(1024, 251)); // a stock 1024 pack size
        let Ok(Wipe::Done(o)) = wipe_v2(&pdir, &set(&[&B.to_uppercase()]), &mark) else { panic!("did not run") };
        assert_eq!((o.n, o.freed), (1, 5592580));
        assert!(!leaked.exists() && kept.exists() && decoy.exists() && small.exists());
        assert!(mark.exists());
        let again = write(&pack_path(&pdir, "3333333333333333"), &pack(2048, 251));
        assert!(matches!(wipe_v2(&pdir, &BTreeSet::new(), &mark), Ok(Wipe::Marked)), "once only");
        assert!(again.exists());
    }

    #[test]
    fn wipe_leaves_an_unfamiliar_folder_alone() {
        let t = Tmp::new("wipeodd");
        let pdir = t.pdir();
        let mark = t.0.join("cache").join(WIPE_MARK);
        let leaked = write(&pack_path(&pdir, A), &pack(2048, 251));
        write(&pdir.join("notes.txt"), b"mine");
        assert!(matches!(wipe_v2(&pdir, &BTreeSet::new(), &mark), Ok(Wipe::Unfamiliar)));
        assert!(leaked.exists() && !mark.exists(), "tried again at a later start");
        assert!(matches!(wipe_v2(&t.0.join("nowhere"), &BTreeSet::new(), &mark), Ok(Wipe::NoFolder)));
        // our packs in a folder named otherwise: unfamiliar too
        let other = t.0.join("user").join("temp").join("art").join("othercache");
        let theirs = write(&pack_path(&other, A), &pack(2048, 251));
        assert!(matches!(wipe_v2(&other, &BTreeSet::new(), &mark), Ok(Wipe::Unfamiliar)));
        assert!(theirs.exists());
    }
}
