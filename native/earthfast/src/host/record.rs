//! 1c: the session recorder (BNGE_HOST_RECORD=DIR, a developer's switch). Every frame a connection reads goes to
//! DIR/<host start>-<connection>.in.zst as it was read; with BNGE_HOST_RECORD_OUT=1 every chunk written to it goes
//! to ...out.zst too (Connection.send writes one whole frame, header included, a chunk). Later phases replay the
//! game's side of a session against a ported server (tools/hostreplay.py) and compare what comes back.
//!
//! The file: one zstd stream of "BNGEREC1", then records: u64 LE nanoseconds since the connection was accepted, u32
//! LE length, the bytes. A recording holds the player's positions: private, never in a report, never uploaded.
use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::time::Instant;

pub const MAGIC: &[u8; 8] = b"BNGEREC1";

/// Where a host records (None: it does not).
#[derive(Clone)]
pub struct Recording {
    pub dir: PathBuf,
    pub out: bool,
    /// the host's start, in the files' names (unix ms): two runs into one folder never mix
    pub stamp: u128,
}

/// One direction of one connection being written.
pub struct Rec {
    enc: Option<zstd::stream::write::Encoder<'static, BufWriter<File>>>,
    t0: Instant,
}

impl Rec {
    pub fn open(rec: &Recording, id: u64, what: &str, t0: Instant) -> Option<Rec> {
        let path = rec.dir.join(format!("{}-{id}.{what}.zst", rec.stamp));
        let f = File::create(path).ok()?;
        let mut enc = zstd::stream::write::Encoder::new(BufWriter::new(f), 3).ok()?;
        enc.write_all(MAGIC).ok()?;
        Some(Rec { enc: Some(enc), t0 })
    }

    pub fn put(&mut self, data: &[u8]) {
        let ns = self.t0.elapsed().as_nanos() as u64;
        let ok = match self.enc.as_mut() {
            Some(e) => e
                .write_all(&ns.to_le_bytes())
                .and_then(|_| e.write_all(&(data.len() as u32).to_le_bytes()))
                .and_then(|_| e.write_all(data))
                .is_ok(),
            None => return,
        };
        if !ok {
            self.enc = None; // (a full disk: the recording stops, the connection goes on)
        }
    }

    pub fn finish(&mut self) {
        if let Some(e) = self.enc.take() {
            if let Ok(mut w) = e.finish() {
                let _ = w.flush();
            }
        }
    }
}

impl Drop for Rec {
    fn drop(&mut self) {
        self.finish();
    }
}

/// A recording's records [(ns, bytes)], as far as it reads (a recording cut short by a killed server ends where its
/// last whole record does).
pub fn read(path: &Path) -> std::io::Result<Vec<(u64, Vec<u8>)>> {
    let mut dec = zstd::stream::read::Decoder::new(BufReader::new(File::open(path)?))?;
    let mut magic = [0u8; 8];
    dec.read_exact(&mut magic)?;
    if &magic != MAGIC {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "not a host recording"));
    }
    let mut out = Vec::new();
    loop {
        let mut head = [0u8; 12];
        if dec.read_exact(&mut head).is_err() {
            break;
        }
        let ns = u64::from_le_bytes(head[..8].try_into().unwrap_or([0; 8]));
        let n = u32::from_le_bytes(head[8..].try_into().unwrap_or([0; 4])) as usize;
        if n > super::frame::MAX_FRAME + super::frame::HEADER {
            break;
        }
        let mut data = vec![0u8; n];
        if dec.read_exact(&mut data).is_err() {
            break;
        }
        out.push((ns, data));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_a_cut_recording() {
        let dir = std::env::temp_dir().join(format!("bnge-rec-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let r = Recording { dir: dir.clone(), out: false, stamp: 7 };
        let mut rec = Rec::open(&r, 3, "in", Instant::now()).unwrap();
        rec.put(br#"{"type":"hello"}"#);
        rec.put(b"");
        rec.put(br#"{"type":"pose","anchorId":1}"#);
        rec.finish();
        let path = dir.join("7-3.in.zst");
        let got = read(&path).unwrap();
        assert_eq!(got.iter().map(|g| g.1.clone()).collect::<Vec<_>>(),
                   vec![br#"{"type":"hello"}"#.to_vec(), b"".to_vec(), br#"{"type":"pose","anchorId":1}"#.to_vec()]);
        assert!(got.windows(2).all(|w| w[0].0 <= w[1].0), "times in order");
        // (cut short: the whole records before the cut)
        let raw = zstd::stream::decode_all(&std::fs::read(&path).unwrap()[..]).unwrap();
        let cut = zstd::stream::encode_all(&raw[..raw.len() - 5], 3).unwrap();
        std::fs::write(&path, cut).unwrap();
        assert_eq!(read(&path).unwrap().len(), 2);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
