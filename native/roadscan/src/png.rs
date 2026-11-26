//! A small PNG writer (RGB, 8 bits) for the report's plan views: deflate with the fixed Huffman codes and matches at
//! the previous pixel and the row above (the plan views are flat colours: about a tenth of the raw size). No
//! dependencies; the same bytes for the same image everywhere.

struct Bits { out: Vec<u8>, acc: u64, n: u32 }

impl Bits {
    fn put(&mut self, v: u32, n: u32) {
        self.acc |= (v as u64) << self.n;
        self.n += n;
        while self.n >= 8 { self.out.push(self.acc as u8); self.acc >>= 8; self.n -= 8; }
    }
    /// A Huffman code (written most significant bit first).
    fn code(&mut self, code: u32, len: u32) {
        let mut r = 0;
        for i in 0..len { r |= ((code >> i) & 1) << (len - 1 - i); }
        self.put(r, len);
    }
    fn finish(mut self) -> Vec<u8> { if self.n > 0 { self.out.push(self.acc as u8); } self.out }
}

const LEN_BASE: [u32; 29] = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258];
const LEN_EXTRA: [u32; 29] = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0];
const DIST_BASE: [u32; 30] = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577];
const DIST_EXTRA: [u32; 30] = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13];

fn lit(b: &mut Bits, v: u32) {
    match v {
        0..=143 => b.code(0x30 + v, 8),
        144..=255 => b.code(0x190 + v - 144, 9),
        256..=279 => b.code(v - 256, 7),
        _ => b.code(0xC0 + v - 280, 8),
    }
}

fn matched(b: &mut Bits, len: u32, dist: u32) {
    let li = LEN_BASE.iter().rposition(|&x| x <= len).unwrap();
    lit(b, 257 + li as u32);
    if LEN_EXTRA[li] > 0 { b.put(len - LEN_BASE[li], LEN_EXTRA[li]); }
    let di = DIST_BASE.iter().rposition(|&x| x <= dist).unwrap();
    b.code(di as u32, 5);
    if DIST_EXTRA[di] > 0 { b.put(dist - DIST_BASE[di], DIST_EXTRA[di]); }
}

/// A zlib stream of data (one fixed-Huffman block).
pub fn zlib(data: &[u8], stride: usize) -> Vec<u8> {
    let mut b = Bits { out: vec![0x78, 0x01], acc: 0, n: 0 };
    b.put(1, 1);        // BFINAL
    b.put(1, 2);        // fixed Huffman
    let mut i = 0;
    while i < data.len() {
        let mut best = (0usize, 0usize);
        for d in [3usize, stride] {
            if d == 0 || d > i || d > 32768 { continue; }
            let mut k = 0;
            while k < 258 && i + k < data.len() && data[i + k] == data[i + k - d] { k += 1; }
            if k > best.0 { best = (k, d); }
        }
        if best.0 >= 3 { matched(&mut b, best.0 as u32, best.1 as u32); i += best.0; } else { lit(&mut b, data[i] as u32); i += 1; }
    }
    lit(&mut b, 256);
    let mut out = b.finish();
    let (mut s1, mut s2) = (1u32, 0u32);
    for &x in data { s1 = (s1 + x as u32) % 65521; s2 = (s2 + s1) % 65521; }
    out.extend(((s2 << 16) | s1).to_be_bytes());
    out
}

fn crc32(parts: &[&[u8]]) -> u32 {
    let mut c = 0xFFFF_FFFFu32;
    for p in parts {
        for &x in *p {
            c ^= x as u32;
            for _ in 0..8 { c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 }; }
        }
    }
    !c
}

fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend((data.len() as u32).to_be_bytes());
    out.extend(kind);
    out.extend(data);
    out.extend(crc32(&[kind, data]).to_be_bytes());
}

/// An RGB image (w x h, rows top down) as PNG bytes.
pub fn encode(w: usize, h: usize, rgb: &[u8]) -> Vec<u8> {
    let stride = w * 3 + 1;
    let mut raw = Vec::with_capacity(stride * h);
    for y in 0..h { raw.push(0); raw.extend_from_slice(&rgb[y * w * 3..(y + 1) * w * 3]); }
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut ihdr = Vec::new();
    ihdr.extend((w as u32).to_be_bytes());
    ihdr.extend((h as u32).to_be_bytes());
    ihdr.extend([8, 2, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &zlib(&raw, stride));
    chunk(&mut out, b"IEND", &[]);
    out
}

/// A PPM (P6, from plan.rs) as PNG.
pub fn from_ppm(ppm: &[u8]) -> Option<Vec<u8>> {
    let head: Vec<&[u8]> = ppm.splitn(4, |&c| c == b'\n').collect();
    if head.len() < 4 || head[0] != b"P6" { return None; }
    let dims = std::str::from_utf8(head[1]).ok()?;
    let mut d = dims.split_whitespace().map(|v| v.parse::<usize>().ok());
    let (w, h) = (d.next()??, d.next()??);
    let body = head[3];
    if body.len() < w * h * 3 { return None; }
    Some(encode(w, h, &body[..w * h * 3]))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A minimal inflate of the fixed-Huffman block written above (the test's own reader).
    fn inflate(z: &[u8]) -> Vec<u8> {
        let mut pos = 16usize;      // (bit position after the 2-byte zlib header)
        let bit = |p: &mut usize| { let v = (z[*p / 8] >> (*p % 8)) & 1; *p += 1; v as u32 };
        let bits = |p: &mut usize, n: u32| { let mut v = 0; for i in 0..n { v |= bit(p) << i; } v };
        assert_eq!(bits(&mut pos, 3), 0b011);
        let mut out: Vec<u8> = Vec::new();
        loop {
            // decode a literal/length symbol of the fixed code
            let mut code = 0u32;
            let mut len = 0;
            let sym = loop {
                code = (code << 1) | bit(&mut pos);
                len += 1;
                match len {
                    7 if code <= 0x17 => break code + 256,
                    8 if (0x30..=0xBF).contains(&code) => break code - 0x30,
                    8 if (0xC0..=0xC7).contains(&code) => break code - 0xC0 + 280,
                    9 if (0x190..=0x1FF).contains(&code) => break code - 0x190 + 144,
                    _ => {}
                }
            };
            if sym < 256 { out.push(sym as u8); continue; }
            if sym == 256 { break; }
            let li = (sym - 257) as usize;
            let l = LEN_BASE[li] + bits(&mut pos, LEN_EXTRA[li]);
            let mut dc = 0;
            for _ in 0..5 { dc = (dc << 1) | bit(&mut pos); }
            let d = DIST_BASE[dc as usize] + bits(&mut pos, DIST_EXTRA[dc as usize]);
            for _ in 0..l { let v = out[out.len() - d as usize]; out.push(v); }
        }
        out
    }

    #[test]
    fn round_trip() {
        let (w, h) = (37usize, 23usize);
        let mut rgb = Vec::new();
        for y in 0..h { for x in 0..w { rgb.extend([(x * 7 % 256) as u8, if y % 5 == 0 { 9 } else { 200 }, ((x / 4 + y) % 3) as u8]); } }
        let stride = w * 3 + 1;
        let mut raw = Vec::new();
        for y in 0..h { raw.push(0); raw.extend_from_slice(&rgb[y * w * 3..(y + 1) * w * 3]); }
        let z = zlib(&raw, stride);
        assert_eq!(inflate(&z), raw);
        let flat = vec![120u8; 300 * 300 * 3];
        assert!(encode(300, 300, &flat).len() < 10_000);
        assert_eq!(crc32(&[b"IEND"]), 0xAE42_6082);
    }
}
