//! Shared decision archives ("BNGD", server/decisions.py; built by tools/build_decisions.py on the project's own
//! machine): a z12 block's road plans and cell facts as the server's own planning makes them, for exactly one version
//! key (the plans' and facts' formats, the frames, the build switches, the map data's versions), signed by the
//! builder's ed25519 key. Players only ever download them: nothing a player's machine made is served.
//!
//! A block file (little-endian):
//!
//!     0   b"BNGD", format u8 (FORMAT), 0 u8, 0 u16
//!     8   z u32, x u32, y u32                       the block (z12)
//!     20  vkey [32]                                 sha256 of the version key's canonical JSON (decisions.vkey)
//!     52  body sha256 [32]                          of everything after the header (the index, then the data)
//!     84  items u32, index bytes u32
//!     92  signature [64]                            ed25519 over bytes 0 .. 92 (the magic gives the domain)
//!     156 index: per item (sorted by key) key length u16, key (UTF-8), codec u8, offset u64 (from the data's start),
//!         stored length u32, raw length u32; then the data
//!
//! Codecs: 1 zstd; 2 zstd of an npz whose 8-byte numeric arrays were turned into per-column deltas in byte planes
//! (npz_transform: lossless, length-preserving, byte-exact back; float64 road profiles pack ~1.8x smaller).
//!
//! A manifest file (one per version key: which blocks exist, for the clients and region downloads):
//!
//!     0   b"BNGM", format u8, 0 u8, 0 u16
//!     8   signature [64]                            ed25519 over b"BNGM" + format byte + the JSON's raw bytes
//!     72  JSON raw length u32, then the JSON as one zstd frame
//!
//! Verification takes the keys compiled in here (TRUSTED: the project's builder key, added with the user's go; empty
//! until then, so nothing verifies) unless the caller hands its own (tests, a local builder).

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use sha2::{Digest, Sha256};

pub const FORMAT: u8 = 1;
const MAGIC: &[u8; 4] = b"BNGD";
const MMAGIC: &[u8; 4] = b"BNGM";
const SIGNED: usize = 92;
const HEAD: usize = 156;
const MHEAD: usize = 76;
const CODEC_ZSTD: u8 = 1;
const CODEC_NPZ: u8 = 2;
const MAX_RAW: usize = 256 << 20; // an item's or a manifest's raw bytes at most (a damaged length is never allocated)
const MAX_ITEMS: usize = 1 << 20;

/// The builder keys whose signatures a client accepts (32-byte ed25519 public keys). Empty until the project's
/// signing key exists (made on the builder machine with the user's go): an empty list verifies nothing.
// The project's builder key (made on the mini PC 2026-11-13 with the user's go; its private seed never leaves it):
// ed25519 public key 33255e3e9e9182e0edcf1d528f347b520ae6fd17e7eb9f145349fc2b97aee57e
const TRUSTED: &[[u8; 32]] = &[[0x33, 0x25, 0x5e, 0x3e, 0x9e, 0x91, 0x82, 0xe0, 0xed, 0xcf, 0x1d, 0x52, 0x8f, 0x34, 0x7b, 0x52, 0x0a, 0xe6, 0xfd, 0x17, 0xe7, 0xeb, 0x9f, 0x14, 0x53, 0x49, 0xfc, 0x2b, 0x97, 0xae, 0xe5, 0x7e]];

fn err<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(format!("decisions: {e}"))
}

fn u16_at(b: &[u8], p: usize) -> Option<u16> {
    b.get(p..p + 2).map(|s| u16::from_le_bytes([s[0], s[1]]))
}

fn u32_at(b: &[u8], p: usize) -> Option<u32> {
    b.get(p..p + 4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
}

fn u64_at(b: &[u8], p: usize) -> Option<u64> {
    b.get(p..p + 8).map(|s| u64::from_le_bytes(s.try_into().unwrap()))
}

// --- the npz transform -------------------------------------------------------------------------------------------

/// shape and whether the .npy header describes a C-ordered array of 8-byte little-endian numbers.
fn npy_shape(text: &str) -> Option<Vec<u64>> {
    let descr = text.split("'descr':").nth(1)?.trim_start();
    let descr = descr.strip_prefix('\'')?.split('\'').next()?;
    if !matches!(descr, "<f8" | "<i8" | "<u8") {
        return None;
    }
    let fo = text.split("'fortran_order':").nth(1)?.trim_start();
    if !fo.starts_with("False") {
        return None;
    }
    let shape = text.split("'shape':").nth(1)?.trim_start().strip_prefix('(')?.split(')').next()?;
    let mut out = Vec::new();
    for part in shape.split(',') {
        let p = part.trim();
        if p.is_empty() {
            continue;
        }
        out.push(p.parse::<u64>().ok()?);
    }
    Some(out)
}

/// One .npy member's data in place: forward = to deltas in byte planes, else back. Anything it does not recognise
/// is left as it is (the decision rests on the untouched headers only, so both directions agree).
fn npy_transform(d: &mut [u8], forward: bool) {
    if d.len() < 10 || &d[..6] != b"\x93NUMPY" {
        return;
    }
    let (hlen, hstart) = match d[6] {
        1 => (u16_at(d, 8).unwrap_or(0) as usize, 10),
        2 | 3 => (u32_at(d, 8).unwrap_or(0) as usize, 12),
        _ => return,
    };
    let Some(htext) = d.get(hstart..hstart + hlen) else { return };
    let Ok(text) = std::str::from_utf8(htext) else { return };
    let Some(shape) = npy_shape(text) else { return };
    let n: u64 = shape.iter().product();
    let data = &mut d[hstart + hlen..];
    if n < 2 || data.len() as u64 != n * 8 {
        return;
    }
    let n = n as usize;
    let rows = if shape.is_empty() { 1 } else { shape[0] as usize };
    if rows == 0 || n % rows != 0 {
        return;
    }
    let cols = n / rows;
    let mut out = vec![0u8; data.len()];
    if forward {
        // value i of column-major order (col c, row r: i = c * rows + r), delta down its column, byte b at b * n + i
        for c in 0..cols {
            let mut prev = 0u64;
            for r in 0..rows {
                let at = (r * cols + c) * 8;
                let v = u64::from_le_bytes(data[at..at + 8].try_into().unwrap());
                let dv = v.wrapping_sub(prev);
                prev = v;
                let i = c * rows + r;
                for (b, byte) in dv.to_le_bytes().iter().enumerate() {
                    out[b * n + i] = *byte;
                }
            }
        }
    } else {
        for c in 0..cols {
            let mut prev = 0u64;
            for r in 0..rows {
                let i = c * rows + r;
                let mut bytes = [0u8; 8];
                for (b, byte) in bytes.iter_mut().enumerate() {
                    *byte = data[b * n + i];
                }
                let v = u64::from_le_bytes(bytes).wrapping_add(prev);
                prev = v;
                let at = (r * cols + c) * 8;
                out[at..at + 8].copy_from_slice(&v.to_le_bytes());
            }
        }
    }
    data.copy_from_slice(&out);
}

/// The 8-byte numeric arrays of an npz (numpy's savez: stored zip members) transformed in place; other members, and
/// anything after a member it cannot read, left as they are.
pub fn npz_transform(buf: &mut [u8], forward: bool) {
    let mut p = 0usize;
    while p + 30 <= buf.len() && &buf[p..p + 4] == b"PK\x03\x04" {
        let flags = u16_at(buf, p + 6).unwrap();
        let method = u16_at(buf, p + 8).unwrap();
        let csize = u32_at(buf, p + 18).unwrap();
        let nlen = u16_at(buf, p + 26).unwrap() as usize;
        let xlen = u16_at(buf, p + 28).unwrap() as usize;
        let xstart = p + 30 + nlen;
        let dstart = xstart + xlen;
        if flags & 8 != 0 || method != 0 || dstart > buf.len() {
            return;
        }
        let mut size = csize as u64;
        if csize == u32::MAX {
            // (zip64: the sizes in the extra field, id 1: uncompressed u64, compressed u64)
            let mut q = xstart;
            let mut found = None;
            while q + 4 <= dstart {
                let id = u16_at(buf, q).unwrap();
                let len = u16_at(buf, q + 2).unwrap() as usize;
                if id == 1 && len >= 16 && q + 4 + 16 <= dstart {
                    found = u64_at(buf, q + 12);
                    break;
                }
                q += 4 + len;
            }
            match found {
                Some(s) => size = s,
                None => return,
            }
        }
        let end = match (dstart as u64).checked_add(size) {
            Some(e) if e <= buf.len() as u64 => e as usize,
            _ => return,
        };
        npy_transform(&mut buf[dstart..end], forward);
        p = end;
    }
}

// --- blocks ------------------------------------------------------------------------------------------------------

pub struct Entry {
    pub key: String,
    pub codec: u8,
    pub off: u64,
    pub stored: u32,
    pub raw: u32,
}

pub struct Head {
    pub z: u32,
    pub x: u32,
    pub y: u32,
    pub vkey: [u8; 32],
    pub entries: Vec<Entry>,
    pub data: usize, // where the data starts
}

fn parse(buf: &[u8]) -> Result<Head, String> {
    if buf.len() < HEAD || &buf[..4] != MAGIC {
        return Err("not a decision block".into());
    }
    if buf[4] != FORMAT {
        return Err(format!("block format {}, {} wanted", buf[4], FORMAT));
    }
    let (z, x, y) = (u32_at(buf, 8).unwrap(), u32_at(buf, 12).unwrap(), u32_at(buf, 16).unwrap());
    let vkey: [u8; 32] = buf[20..52].try_into().unwrap();
    let n = u32_at(buf, 84).unwrap() as usize;
    let ilen = u32_at(buf, 88).unwrap() as usize;
    if n > MAX_ITEMS || HEAD + ilen > buf.len() {
        return Err("index past the end".into());
    }
    let idx = &buf[HEAD..HEAD + ilen];
    let data = HEAD + ilen;
    let dlen = (buf.len() - data) as u64;
    let mut entries = Vec::with_capacity(n);
    let mut p = 0usize;
    for _ in 0..n {
        let kl = u16_at(idx, p).ok_or("index cut short")? as usize;
        let key = idx.get(p + 2..p + 2 + kl).ok_or("index cut short")?;
        let key = std::str::from_utf8(key).map_err(|_| "a key not UTF-8")?.to_string();
        p += 2 + kl;
        let codec = *idx.get(p).ok_or("index cut short")?;
        let off = u64_at(idx, p + 1).ok_or("index cut short")?;
        let stored = u32_at(idx, p + 9).ok_or("index cut short")?;
        let raw = u32_at(idx, p + 13).ok_or("index cut short")?;
        p += 17;
        if off.checked_add(stored as u64).map_or(true, |e| e > dlen) || raw as usize > MAX_RAW {
            return Err(format!("item {key} past the end"));
        }
        if !matches!(codec, CODEC_ZSTD | CODEC_NPZ) {
            return Err(format!("item {key}: codec {codec}"));
        }
        entries.push(Entry { key, codec, off, stored, raw });
    }
    if p != ilen {
        return Err("index length".into());
    }
    if entries.windows(2).any(|w| w[0].key >= w[1].key) {
        return Err("index not sorted".into());
    }
    Ok(Head { z, x, y, vkey, entries, data })
}

fn keys_of(pubkeys: Option<Vec<Vec<u8>>>) -> Result<Vec<VerifyingKey>, String> {
    let raw: Vec<[u8; 32]> = match pubkeys {
        Some(v) => v
            .into_iter()
            .map(|k| <[u8; 32]>::try_from(k.as_slice()).map_err(|_| "a public key is not 32 bytes".to_string()))
            .collect::<Result<_, _>>()?,
        None => TRUSTED.to_vec(),
    };
    raw.iter().map(|k| VerifyingKey::from_bytes(k).map_err(|e| format!("public key: {e}"))).collect()
}

fn verify_any(keys: &[VerifyingKey], msg: &[u8], sig: &[u8]) -> Result<(), String> {
    let sig = Signature::from_slice(sig).map_err(|e| format!("signature: {e}"))?;
    if keys.is_empty() {
        return Err("no trusted builder key".into());
    }
    if keys.iter().any(|k| k.verify_strict(msg, &sig).is_ok()) {
        Ok(())
    } else {
        Err("bad signature".into())
    }
}

fn seed_key(seed: &[u8]) -> Result<SigningKey, String> {
    let s: [u8; 32] = seed.try_into().map_err(|_| "the signing seed is not 32 bytes".to_string())?;
    Ok(SigningKey::from_bytes(&s))
}

/// A block file of items [(key, bytes, npz)] (npz: the bytes are numpy's npz, packed with the npz transform),
/// signed with the builder's key (its 32-byte seed). Items in key order; a key twice is an error.
pub fn pack_block(z: u32, x: u32, y: u32, vkey: &[u8], items: &[(String, Vec<u8>, bool)], level: i32, seed: &[u8])
    -> Result<Vec<u8>, String> {
    let sk = seed_key(seed)?;
    let vkey: [u8; 32] = vkey.try_into().map_err(|_| "vkey is not 32 bytes".to_string())?;
    let mut order: Vec<usize> = (0..items.len()).collect();
    order.sort_by(|&a, &b| items[a].0.cmp(&items[b].0));
    if order.windows(2).any(|w| items[w[0]].0 == items[w[1]].0) {
        return Err("a key twice".into());
    }
    let mut index = Vec::new();
    let mut data = Vec::new();
    for &i in &order {
        let (key, raw, npz) = &items[i];
        if key.len() > u16::MAX as usize || raw.len() > MAX_RAW {
            return Err(format!("item {key} too large"));
        }
        let (codec, packed) = if *npz {
            let mut t = raw.clone();
            npz_transform(&mut t, true);
            (CODEC_NPZ, zstd::bulk::compress(&t, level).map_err(|e| e.to_string())?)
        } else {
            (CODEC_ZSTD, zstd::bulk::compress(raw, level).map_err(|e| e.to_string())?)
        };
        index.extend_from_slice(&(key.len() as u16).to_le_bytes());
        index.extend_from_slice(key.as_bytes());
        index.push(codec);
        index.extend_from_slice(&(data.len() as u64).to_le_bytes());
        index.extend_from_slice(&(packed.len() as u32).to_le_bytes());
        index.extend_from_slice(&(raw.len() as u32).to_le_bytes());
        data.extend_from_slice(&packed);
    }
    let mut h = Sha256::new();
    h.update(&index);
    h.update(&data);
    let body: [u8; 32] = h.finalize().into();
    let mut out = Vec::with_capacity(HEAD + index.len() + data.len());
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&[FORMAT, 0, 0, 0]);
    for v in [z, x, y] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.extend_from_slice(&vkey);
    out.extend_from_slice(&body);
    out.extend_from_slice(&(order.len() as u32).to_le_bytes());
    out.extend_from_slice(&(index.len() as u32).to_le_bytes());
    debug_assert_eq!(out.len(), SIGNED);
    let sig = sk.sign(&out[..SIGNED]);
    out.extend_from_slice(&sig.to_bytes());
    out.extend_from_slice(&index);
    out.extend_from_slice(&data);
    Ok(out)
}

/// Checks a downloaded block whole: its signature by a trusted key, its body's hash, that it is the block and version
/// asked for, every item inside the file. Returns the item count.
pub fn verify_block(buf: &[u8], z: u32, x: u32, y: u32, vkey: &[u8], keys: &[VerifyingKey]) -> Result<usize, String> {
    if buf.len() < HEAD || &buf[..4] != MAGIC {
        return Err("not a decision block".into());
    }
    verify_any(keys, &buf[..SIGNED], &buf[SIGNED..HEAD])?;
    let mut h = Sha256::new();
    h.update(&buf[HEAD..]);
    let body: [u8; 32] = h.finalize().into();
    if body[..] != buf[52..84] {
        return Err("body hash".into());
    }
    let head = parse(buf)?;
    if (head.z, head.x, head.y) != (z, x, y) {
        return Err(format!("block {}/{}/{}, {z}/{x}/{y} asked", head.z, head.x, head.y));
    }
    if head.vkey[..] != *vkey {
        return Err("another version key".into());
    }
    Ok(head.entries.len())
}

fn unpack(buf: &[u8], head: &Head, e: &Entry) -> Result<Vec<u8>, String> {
    let at = head.data + e.off as usize;
    let stored = &buf[at..at + e.stored as usize];
    let mut out = zstd::bulk::Decompressor::new()
        .and_then(|mut d| d.decompress(stored, e.raw as usize))
        .map_err(|err| format!("item {}: zstd: {err}", e.key))?;
    if out.len() != e.raw as usize {
        return Err(format!("item {}: {} bytes, {} expected", e.key, out.len(), e.raw));
    }
    if e.codec == CODEC_NPZ {
        npz_transform(&mut out, false);
    }
    Ok(out)
}

/// The raw bytes of one item of a block (a file already verified when it was downloaded), or None.
pub fn block_item(buf: &[u8], key: &str) -> Result<Option<Vec<u8>>, String> {
    let head = parse(buf)?;
    match head.entries.binary_search_by(|e| e.key.as_str().cmp(key)) {
        Ok(i) => unpack(buf, &head, &head.entries[i]).map(Some),
        Err(_) => Ok(None),
    }
}

pub fn make_manifest(json: &[u8], seed: &[u8], level: i32) -> Result<Vec<u8>, String> {
    let sk = seed_key(seed)?;
    let mut msg = Vec::with_capacity(json.len() + 5);
    msg.extend_from_slice(MMAGIC);
    msg.push(FORMAT);
    msg.extend_from_slice(json);
    let sig = sk.sign(&msg);
    let packed = zstd::bulk::compress(json, level).map_err(|e| e.to_string())?;
    let mut out = Vec::with_capacity(MHEAD + packed.len());
    out.extend_from_slice(MMAGIC);
    out.extend_from_slice(&[FORMAT, 0, 0, 0]);
    out.extend_from_slice(&sig.to_bytes());
    out.extend_from_slice(&(json.len() as u32).to_le_bytes());
    out.extend_from_slice(&packed);
    Ok(out)
}

pub fn open_manifest(buf: &[u8], keys: &[VerifyingKey]) -> Result<Vec<u8>, String> {
    if buf.len() < MHEAD || &buf[..4] != MMAGIC {
        return Err("not a decision manifest".into());
    }
    if buf[4] != FORMAT {
        return Err(format!("manifest format {}, {} wanted", buf[4], FORMAT));
    }
    let n = u32_at(buf, 72).unwrap() as usize;
    if n > MAX_RAW {
        return Err("manifest too large".into());
    }
    let json = zstd::bulk::Decompressor::new()
        .and_then(|mut d| d.decompress(&buf[MHEAD..], n))
        .map_err(|e| format!("manifest: zstd: {e}"))?;
    if json.len() != n {
        return Err("manifest length".into());
    }
    let mut msg = Vec::with_capacity(n + 5);
    msg.extend_from_slice(MMAGIC);
    msg.push(FORMAT);
    msg.extend_from_slice(&json);
    verify_any(keys, &msg, &buf[8..72])?;
    Ok(json)
}

// --- Python ------------------------------------------------------------------------------------------------------

/// A signed block file (the builder: tools/build_decisions.py). items: [(key, bytes, npz)].
#[pyfunction]
#[pyo3(signature = (z, x, y, vkey, items, seed, level = 19))]
pub fn dec_pack_block<'py>(py: Python<'py>, z: u32, x: u32, y: u32, vkey: &[u8],
                           items: Vec<(String, Bound<'py, PyBytes>, bool)>, seed: &[u8], level: i32)
    -> PyResult<Bound<'py, PyBytes>> {
    let owned: Vec<(String, Vec<u8>, bool)> = items.iter().map(|(k, b, n)| (k.clone(), b.as_bytes().to_vec(), *n)).collect();
    let vkey = vkey.to_vec();
    let seed = seed.to_vec();
    let out = py.detach(|| pack_block(z, x, y, &vkey, &owned, level, &seed)).map_err(err)?;
    Ok(PyBytes::new(py, &out))
}

/// The item count of a downloaded block, after its checks (verify_block); ValueError when any fails. pubkeys: the
/// keys to accept (None: the ones compiled in).
#[pyfunction]
#[pyo3(signature = (buf, z, x, y, vkey, pubkeys = None))]
pub fn dec_verify_block(py: Python<'_>, buf: &[u8], z: u32, x: u32, y: u32, vkey: &[u8], pubkeys: Option<Vec<Vec<u8>>>)
    -> PyResult<usize> {
    let keys = keys_of(pubkeys).map_err(err)?;
    py.detach(|| verify_block(buf, z, x, y, vkey, &keys)).map_err(err)
}

/// One item's raw bytes from a block file, or None.
#[pyfunction]
pub fn dec_block_item<'py>(py: Python<'py>, buf: &[u8], key: &str) -> PyResult<Option<Bound<'py, PyBytes>>> {
    let got = py.detach(|| block_item(buf, key)).map_err(err)?;
    Ok(got.map(|b| PyBytes::new(py, &b)))
}

/// (z, x, y, vkey, [keys]) of a block file (no signature check).
#[pyfunction]
pub fn dec_block_info<'py>(py: Python<'py>, buf: &[u8]) -> PyResult<(u32, u32, u32, Bound<'py, PyBytes>, Vec<String>)> {
    let h = parse(buf).map_err(err)?;
    Ok((h.z, h.x, h.y, PyBytes::new(py, &h.vkey), h.entries.into_iter().map(|e| e.key).collect()))
}

#[pyfunction]
#[pyo3(signature = (json, seed, level = 19))]
pub fn dec_make_manifest<'py>(py: Python<'py>, json: &[u8], seed: &[u8], level: i32) -> PyResult<Bound<'py, PyBytes>> {
    let out = make_manifest(json, seed, level).map_err(err)?;
    Ok(PyBytes::new(py, &out))
}

/// The JSON bytes of a manifest whose signature a trusted key made; ValueError otherwise.
#[pyfunction]
#[pyo3(signature = (buf, pubkeys = None))]
pub fn dec_open_manifest<'py>(py: Python<'py>, buf: &[u8], pubkeys: Option<Vec<Vec<u8>>>) -> PyResult<Bound<'py, PyBytes>> {
    let keys = keys_of(pubkeys).map_err(err)?;
    let out = open_manifest(buf, &keys).map_err(err)?;
    Ok(PyBytes::new(py, &out))
}

/// The public key of a 32-byte signing seed.
#[pyfunction]
pub fn dec_public_key<'py>(py: Python<'py>, seed: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    let sk = seed_key(seed).map_err(err)?;
    Ok(PyBytes::new(py, sk.verifying_key().as_bytes()))
}

/// The builder keys compiled in.
#[pyfunction]
pub fn dec_trusted_keys<'py>(py: Python<'py>) -> Vec<Bound<'py, PyBytes>> {
    TRUSTED.iter().map(|k| PyBytes::new(py, k)).collect()
}

/// The npz transform alone (tests).
#[pyfunction]
pub fn dec_npz_transform<'py>(py: Python<'py>, buf: &[u8], forward: bool) -> Bound<'py, PyBytes> {
    let mut b = buf.to_vec();
    npz_transform(&mut b, forward);
    PyBytes::new(py, &b)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dec_pack_block, m)?)?;
    m.add_function(wrap_pyfunction!(dec_verify_block, m)?)?;
    m.add_function(wrap_pyfunction!(dec_block_item, m)?)?;
    m.add_function(wrap_pyfunction!(dec_block_info, m)?)?;
    m.add_function(wrap_pyfunction!(dec_make_manifest, m)?)?;
    m.add_function(wrap_pyfunction!(dec_open_manifest, m)?)?;
    m.add_function(wrap_pyfunction!(dec_public_key, m)?)?;
    m.add_function(wrap_pyfunction!(dec_trusted_keys, m)?)?;
    m.add_function(wrap_pyfunction!(dec_npz_transform, m)?)?;
    m.add("DEC_FORMAT", FORMAT)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn npy(shape: &str, descr: &str, data: &[u8]) -> Vec<u8> {
        let mut h = format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': {shape}, }}");
        while (10 + h.len() + 1) % 64 != 0 {
            h.push(' ');
        }
        h.push('\n');
        let mut out = b"\x93NUMPY\x01\x00".to_vec();
        out.extend_from_slice(&(h.len() as u16).to_le_bytes());
        out.extend_from_slice(h.as_bytes());
        out.extend_from_slice(data);
        out
    }

    fn zip_stored(members: &[(&str, Vec<u8>)]) -> Vec<u8> {
        let mut out = Vec::new();
        for (name, d) in members {
            out.extend_from_slice(b"PK\x03\x04");
            out.extend_from_slice(&[20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
            out.extend_from_slice(&(d.len() as u32).to_le_bytes());
            out.extend_from_slice(&(d.len() as u32).to_le_bytes());
            out.extend_from_slice(&(name.len() as u16).to_le_bytes());
            out.extend_from_slice(&0u16.to_le_bytes());
            out.extend_from_slice(name.as_bytes());
            out.extend_from_slice(d);
        }
        out.extend_from_slice(b"PK\x01\x02 central directory stand-in");
        out
    }

    #[test]
    fn npz_round_trip() {
        let vals: Vec<u8> = (0..60u64).flat_map(|i| ((i as f64) * 1.5 + 0.25).to_le_bytes()).collect();
        let ints: Vec<u8> = (0..7i64).flat_map(|i| (i * -3).to_le_bytes()).collect();
        let z = zip_stored(&[("a.npy", npy("(30, 2)", "<f8", &vals)), ("b.npy", npy("(7,)", "<i8", &ints)),
                             ("c.npy", npy("(5,)", "|u1", &[1, 2, 3, 4, 5]))]);
        let mut t = z.clone();
        npz_transform(&mut t, true);
        assert_ne!(t, z);
        assert_eq!(t.len(), z.len());
        npz_transform(&mut t, false);
        assert_eq!(t, z);
        let mut junk = b"PK\x03\x04 not really".to_vec();
        let before = junk.clone();
        npz_transform(&mut junk, true);
        assert_eq!(junk, before);
    }

    #[test]
    fn block_sign_verify() {
        let seed = [7u8; 32];
        let vkey = [3u8; 32];
        let items = vec![("p/b".to_string(), b"hello".to_vec(), false), ("f/a".to_string(), vec![0u8; 1000], false)];
        let b = pack_block(12, 1, 2, &vkey, &items, 3, &seed).unwrap();
        let pk = VerifyingKey::from(&SigningKey::from_bytes(&seed));
        assert_eq!(verify_block(&b, 12, 1, 2, &vkey, &[pk]).unwrap(), 2);
        assert!(verify_block(&b, 12, 1, 3, &vkey, &[pk]).is_err());
        assert!(verify_block(&b, 12, 1, 2, &[4u8; 32], &[pk]).is_err());
        assert!(verify_block(&b, 12, 1, 2, &vkey, &[]).is_err());
        let other = VerifyingKey::from(&SigningKey::from_bytes(&[8u8; 32]));
        assert!(verify_block(&b, 12, 1, 2, &vkey, &[other]).is_err());
        let mut bad = b.clone();
        let last = bad.len() - 1;
        bad[last] ^= 1;
        assert!(verify_block(&bad, 12, 1, 2, &vkey, &[pk]).is_err());
        assert_eq!(block_item(&b, "p/b").unwrap().unwrap(), b"hello");
        assert_eq!(block_item(&b, "f/a").unwrap().unwrap(), vec![0u8; 1000]);
        assert!(block_item(&b, "nope").unwrap().is_none());
        let m = make_manifest(b"{\"a\":1}", &seed, 3).unwrap();
        assert_eq!(open_manifest(&m, &[pk]).unwrap(), b"{\"a\":1}");
        assert!(open_manifest(&m, &[other]).is_err());
    }
}
