//! Reading zip files (read only): the central directory (with zip64, for the game's multi-GB level zips) and
//! single entries, stored or deflated. Used for the game's content zips (game.rs) and numpy's .npz (osm.rs).

use std::io::{Read, Seek, SeekFrom};

#[derive(Clone, Debug)]
pub struct Entry {
    pub name: String,
    pub method: u16,
    pub csize: u64,
    pub usize: u64,
    /// offset of the entry's local header
    pub local: u64,
}

fn u16_at(b: &[u8], i: usize) -> Option<u64> {
    b.get(i..i + 2).map(|s| u16::from_le_bytes([s[0], s[1]]) as u64)
}
fn u32_at(b: &[u8], i: usize) -> Option<u64> {
    b.get(i..i + 4).map(|s| u32::from_le_bytes(s.try_into().unwrap()) as u64)
}
fn u64_at(b: &[u8], i: usize) -> Option<u64> {
    b.get(i..i + 8).map(|s| u64::from_le_bytes(s.try_into().unwrap()))
}

fn bad(what: &str) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, format!("zip: {what}"))
}

fn read_at<R: Read + Seek>(r: &mut R, at: u64, n: usize) -> std::io::Result<Vec<u8>> {
    r.seek(SeekFrom::Start(at))?;
    let mut b = vec![0u8; n];
    r.read_exact(&mut b)?;
    Ok(b)
}

/// Every entry of the zip's central directory.
pub fn index<R: Read + Seek>(r: &mut R) -> std::io::Result<Vec<Entry>> {
    let len = r.seek(SeekFrom::End(0))?;
    let tail_n = len.min(65_557) as usize;
    let tail = read_at(r, len - tail_n as u64, tail_n)?;
    let e = (0..tail.len().saturating_sub(21))
        .rev()
        .find(|&i| tail[i..].starts_with(b"PK\x05\x06"))
        .ok_or_else(|| bad("no end of central directory"))?;
    let mut count = u16_at(&tail, e + 10).ok_or_else(|| bad("short"))?;
    let mut cd_size = u32_at(&tail, e + 12).ok_or_else(|| bad("short"))?;
    let mut cd_off = u32_at(&tail, e + 16).ok_or_else(|| bad("short"))?;
    if count == 0xFFFF || cd_size == 0xFFFF_FFFF || cd_off == 0xFFFF_FFFF {
        // zip64: the locator just before the end record names the zip64 end record
        let loc = e.checked_sub(20).filter(|&l| tail[l..].starts_with(b"PK\x06\x07")).ok_or_else(|| bad("no zip64 locator"))?;
        let z64 = u64_at(&tail, loc + 8).ok_or_else(|| bad("short"))?;
        let rec = read_at(r, z64, 56)?;
        if !rec.starts_with(b"PK\x06\x06") {
            return Err(bad("bad zip64 end record"));
        }
        count = u64_at(&rec, 32).unwrap();
        cd_size = u64_at(&rec, 40).unwrap();
        cd_off = u64_at(&rec, 48).unwrap();
    }
    if cd_size > 512 << 20 {
        return Err(bad("central directory too large"));
    }
    let cd = read_at(r, cd_off, cd_size as usize)?;
    let mut out = Vec::with_capacity(count as usize);
    let mut at = 0usize;
    for _ in 0..count {
        if !cd.get(at..).is_some_and(|s| s.starts_with(b"PK\x01\x02")) {
            return Err(bad("bad central directory entry"));
        }
        let short = || bad("short entry");
        let method = u16_at(&cd, at + 10).ok_or_else(short)? as u16;
        let mut csize = u32_at(&cd, at + 20).ok_or_else(short)?;
        let mut usize_ = u32_at(&cd, at + 24).ok_or_else(short)?;
        let nl = u16_at(&cd, at + 28).ok_or_else(short)? as usize;
        let xl = u16_at(&cd, at + 30).ok_or_else(short)? as usize;
        let cl = u16_at(&cd, at + 32).ok_or_else(short)? as usize;
        let mut local = u32_at(&cd, at + 42).ok_or_else(short)?;
        let name = String::from_utf8_lossy(cd.get(at + 46..at + 46 + nl).ok_or_else(short)?).into_owned();
        // zip64 extra field (id 1): the sizes and offset that did not fit, in this order
        let extra = cd.get(at + 46 + nl..at + 46 + nl + xl).ok_or_else(short)?;
        let mut x = 0;
        while x + 4 <= extra.len() {
            let id = u16_at(extra, x).unwrap();
            let n = u16_at(extra, x + 2).unwrap() as usize;
            if id == 1 {
                let mut k = x + 4;
                for field in [&mut usize_, &mut csize, &mut local] {
                    if *field == 0xFFFF_FFFF {
                        *field = u64_at(extra, k).ok_or_else(short)?;
                        k += 8;
                    }
                }
            }
            x += 4 + n;
        }
        out.push(Entry { name, method, csize, usize: usize_, local });
        at += 46 + nl + xl + cl;
    }
    Ok(out)
}

/// One entry's bytes.
pub fn read<R: Read + Seek>(r: &mut R, e: &Entry) -> std::io::Result<Vec<u8>> {
    let h = read_at(r, e.local, 30)?;
    if !h.starts_with(b"PK\x03\x04") {
        return Err(bad("bad local header"));
    }
    let start = e.local + 30 + u16_at(&h, 26).unwrap() + u16_at(&h, 28).unwrap();
    if e.csize > 1 << 31 {
        return Err(bad("entry too large"));
    }
    let comp = read_at(r, start, e.csize as usize)?;
    match e.method {
        0 => Ok(comp),
        8 => {
            let mut v = Vec::with_capacity(e.usize as usize);
            flate2::read::DeflateDecoder::new(&comp[..]).read_to_end(&mut v)?;
            Ok(v)
        }
        m => Err(bad(&format!("compression method {m}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Write};

    // a stored and a deflated entry, written by hand (local headers, central directory, end record)
    fn tiny_zip() -> Vec<u8> {
        let a = b"hello stored".to_vec();
        let plain = b"hello deflated hello deflated hello deflated".to_vec();
        let mut enc = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
        enc.write_all(&plain).unwrap();
        let b = enc.finish().unwrap();
        let mut z = Vec::new();
        let mut cd = Vec::new();
        for (name, method, data, usize_) in [("a.txt", 0u16, &a, a.len()), ("dir/b.npy", 8, &b, plain.len())] {
            let local = z.len() as u32;
            z.extend_from_slice(b"PK\x03\x04");
            z.extend_from_slice(&[20, 0, 0, 0]);
            z.extend_from_slice(&method.to_le_bytes());
            z.extend_from_slice(&[0; 8]); // time, date, crc
            z.extend_from_slice(&(data.len() as u32).to_le_bytes());
            z.extend_from_slice(&(usize_ as u32).to_le_bytes());
            z.extend_from_slice(&(name.len() as u16).to_le_bytes());
            z.extend_from_slice(&0u16.to_le_bytes());
            z.extend_from_slice(name.as_bytes());
            z.extend_from_slice(data);
            cd.extend_from_slice(b"PK\x01\x02");
            cd.extend_from_slice(&[20, 0, 20, 0, 0, 0]);
            cd.extend_from_slice(&method.to_le_bytes());
            cd.extend_from_slice(&[0; 8]);
            cd.extend_from_slice(&(data.len() as u32).to_le_bytes());
            cd.extend_from_slice(&(usize_ as u32).to_le_bytes());
            cd.extend_from_slice(&(name.len() as u16).to_le_bytes());
            cd.extend_from_slice(&[0; 12]); // extra, comment, disk, attrs
            cd.extend_from_slice(&local.to_le_bytes());
            cd.extend_from_slice(name.as_bytes());
        }
        let off = z.len() as u32;
        z.extend_from_slice(&cd);
        z.extend_from_slice(b"PK\x05\x06");
        z.extend_from_slice(&[0; 4]);
        z.extend_from_slice(&2u16.to_le_bytes());
        z.extend_from_slice(&2u16.to_le_bytes());
        z.extend_from_slice(&(cd.len() as u32).to_le_bytes());
        z.extend_from_slice(&off.to_le_bytes());
        z.extend_from_slice(&[0; 2]);
        z
    }

    #[test]
    fn reads_stored_and_deflated() {
        let mut c = Cursor::new(tiny_zip());
        let ix = index(&mut c).unwrap();
        assert_eq!(ix.len(), 2);
        assert_eq!(read(&mut c, &ix[0]).unwrap(), b"hello stored");
        assert_eq!(ix[1].name, "dir/b.npy");
        assert_eq!(read(&mut c, &ix[1]).unwrap(), b"hello deflated hello deflated hello deflated");
    }
}
