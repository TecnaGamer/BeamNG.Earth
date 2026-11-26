//! Mono WAV in and out. Writes 32-bit float (format 3); reads 16-bit PCM and 32-bit float (what ffmpeg decodes to).

use std::fs;
use std::io::Write;

pub fn write_f32(path: &str, x: &[f64], fs: u32) -> std::io::Result<()> {
    let data_len = (x.len() * 4) as u32;
    let mut b: Vec<u8> = Vec::with_capacity(x.len() * 4 + 64);
    b.extend_from_slice(b"RIFF");
    b.extend_from_slice(&(36 + 14 + data_len).to_le_bytes());
    b.extend_from_slice(b"WAVE");
    b.extend_from_slice(b"fmt ");
    b.extend_from_slice(&18u32.to_le_bytes());
    b.extend_from_slice(&3u16.to_le_bytes()); // IEEE float
    b.extend_from_slice(&1u16.to_le_bytes()); // mono
    b.extend_from_slice(&fs.to_le_bytes());
    b.extend_from_slice(&(fs * 4).to_le_bytes());
    b.extend_from_slice(&4u16.to_le_bytes());
    b.extend_from_slice(&32u16.to_le_bytes());
    b.extend_from_slice(&0u16.to_le_bytes());
    // fact chunk (required for non-PCM)
    b.extend_from_slice(b"fact");
    b.extend_from_slice(&4u32.to_le_bytes());
    b.extend_from_slice(&(x.len() as u32).to_le_bytes());
    b.extend_from_slice(b"data");
    b.extend_from_slice(&data_len.to_le_bytes());
    for &v in x {
        b.extend_from_slice(&(v as f32).to_le_bytes());
    }
    let mut f = fs::File::create(path)?;
    f.write_all(&b)
}

/// Returns (samples, sample rate). Stereo files are averaged to mono.
pub fn read(path: &str) -> Result<(Vec<f64>, u32), String> {
    let b = fs::read(path).map_err(|e| format!("{path}: {e}"))?;
    if b.len() < 12 || &b[0..4] != b"RIFF" || &b[8..12] != b"WAVE" {
        return Err(format!("{path}: not a WAV file"));
    }
    let u16at = |i: usize| u16::from_le_bytes([b[i], b[i + 1]]);
    let u32at = |i: usize| u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]]);
    let mut i = 12;
    let (mut fmt, mut ch, mut fs, mut bits) = (0u16, 0u16, 0u32, 0u16);
    while i + 8 <= b.len() {
        let id = &b[i..i + 4];
        let len = u32at(i + 4) as usize;
        let body = i + 8;
        if id == b"fmt " {
            fmt = u16at(body);
            ch = u16at(body + 2);
            fs = u32at(body + 4);
            bits = u16at(body + 14);
            if fmt == 0xFFFE {
                fmt = u16at(body + 24);
            }
        } else if id == b"data" {
            let end = (body + len).min(b.len());
            let d = &b[body..end];
            let chn = ch.max(1) as usize;
            let mut out = Vec::new();
            match (fmt, bits) {
                (1, 16) => {
                    for fr in d.chunks_exact(2 * chn) {
                        let mut s = 0.0;
                        for c in 0..chn {
                            s += i16::from_le_bytes([fr[2 * c], fr[2 * c + 1]]) as f64 / 32768.0;
                        }
                        out.push(s / chn as f64);
                    }
                }
                (3, 32) => {
                    for fr in d.chunks_exact(4 * chn) {
                        let mut s = 0.0;
                        for c in 0..chn {
                            s += f32::from_le_bytes([fr[4 * c], fr[4 * c + 1], fr[4 * c + 2], fr[4 * c + 3]]) as f64;
                        }
                        out.push(s / chn as f64);
                    }
                }
                _ => return Err(format!("{path}: unsupported WAV format {fmt}/{bits}")),
            }
            return Ok((out, fs));
        }
        i = body + len + (len & 1);
    }
    Err(format!("{path}: no data chunk"))
}
