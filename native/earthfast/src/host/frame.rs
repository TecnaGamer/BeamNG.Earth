//! The game connection's framing (server/protocol.py's): a 4-byte little-endian length, then that many bytes of UTF-8
//! JSON. At most MAX_FRAME bytes a frame. The error texts are protocol.py's, word for word: Connection.run logs them
//! as "client %s: %s".
use std::io::{self, ErrorKind, Read};

pub const HEADER: usize = 4;
pub const MAX_FRAME: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum FrameError {
    /// a header asking for more than MAX_FRAME (protocol.ProtocolError)
    TooLarge(u64),
    /// EOF after part of a header (protocol.ProtocolError)
    InHeader,
    /// EOF after the header, inside the payload (protocol.ProtocolError)
    InFrame,
    /// the socket failed (ConnectionResetError and its kin in Python)
    Io(io::Error),
}

impl FrameError {
    /// protocol.py's text for the error
    pub fn text(&self) -> String {
        match self {
            FrameError::TooLarge(n) => format!("frame too large: {n} bytes"),
            FrameError::InHeader => "connection closed inside a frame header".to_string(),
            FrameError::InFrame => "connection closed inside a frame".to_string(),
            FrameError::Io(e) => e.to_string(),
        }
    }

    /// whether Python sees it as protocol.ProtocolError (else a ConnectionError)
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn is_protocol(&self) -> bool {
        !matches!(self, FrameError::Io(_))
    }
}

/// Fills buf from r; returns how many bytes came before EOF (buf.len() when it is full). Retries EINTR.
fn fill<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut got = 0;
    while got < buf.len() {
        match r.read(&mut buf[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(got)
}

/// The next frame's payload, or None at a clean EOF (between frames).
pub fn read_frame<R: Read>(r: &mut R) -> Result<Option<Vec<u8>>, FrameError> {
    let mut head = [0u8; HEADER];
    match fill(r, &mut head) {
        Ok(0) => return Ok(None),
        Ok(n) if n < HEADER => return Err(FrameError::InHeader),
        Ok(_) => {}
        Err(e) => return Err(FrameError::Io(e)),
    }
    let n = u32::from_le_bytes(head) as u64;
    if n > MAX_FRAME as u64 {
        return Err(FrameError::TooLarge(n));
    }
    let mut payload = vec![0u8; n as usize];
    match fill(r, &mut payload) {
        Ok(got) if got < payload.len() => Err(FrameError::InFrame),
        Ok(_) => Ok(Some(payload)),
        Err(e) => Err(FrameError::Io(e)),
    }
}

/// A whole frame for payload (header + bytes), as protocol.encode makes them.
#[cfg_attr(not(test), allow(dead_code))]
pub fn frame(payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER + payload.len());
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    out.extend_from_slice(payload);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    /// reads in pieces of at most `step` bytes (a socket's short reads)
    struct Trickle<'a> {
        data: &'a [u8],
        step: usize,
    }

    impl Read for Trickle<'_> {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let n = self.step.min(buf.len()).min(self.data.len());
            buf[..n].copy_from_slice(&self.data[..n]);
            self.data = &self.data[n..];
            Ok(n)
        }
    }

    #[test]
    fn frames_then_clean_eof() {
        let mut bytes = frame(br#"{"type":"hello"}"#);
        bytes.extend(frame(b""));
        bytes.extend(frame("{\"type\":\"x\",\"t\":\"Cu\u{f4}te\"}".as_bytes()));
        for step in [1, 3, 7, 4096] {
            let mut r = Trickle { data: &bytes, step };
            assert_eq!(read_frame(&mut r).unwrap().unwrap(), br#"{"type":"hello"}"#);
            assert_eq!(read_frame(&mut r).unwrap().unwrap(), b"");
            assert_eq!(read_frame(&mut r).unwrap().unwrap(), "{\"type\":\"x\",\"t\":\"Cu\u{f4}te\"}".as_bytes());
            assert!(read_frame(&mut r).unwrap().is_none());
            assert!(read_frame(&mut r).unwrap().is_none(), "EOF stays EOF");
        }
    }

    #[test]
    fn eof_inside_header_and_frame() {
        let mut r = Cursor::new(vec![5u8, 0]);
        let e = read_frame(&mut r).unwrap_err();
        assert!(e.is_protocol());
        assert_eq!(e.text(), "connection closed inside a frame header");
        let mut part = frame(b"hello");
        part.truncate(HEADER + 2);
        let e = read_frame(&mut Cursor::new(part)).unwrap_err();
        assert_eq!(e.text(), "connection closed inside a frame");
        // a header and nothing after it
        let e = read_frame(&mut Cursor::new(frame(b"x")[..HEADER].to_vec())).unwrap_err();
        assert_eq!(e.text(), "connection closed inside a frame");
    }

    #[test]
    fn the_size_limit() {
        // MAX_FRAME is allowed, one more is not (and nothing of its payload is read)
        let ok = (MAX_FRAME as u32).to_le_bytes();
        let mut data = ok.to_vec();
        data.resize(HEADER + MAX_FRAME, b' ');
        assert_eq!(read_frame(&mut Cursor::new(data)).unwrap().unwrap().len(), MAX_FRAME);
        let big = ((MAX_FRAME + 1) as u32).to_le_bytes();
        let e = read_frame(&mut Cursor::new(big.to_vec())).unwrap_err();
        assert!(e.is_protocol());
        assert_eq!(e.text(), "frame too large: 16777217 bytes");
        let e = read_frame(&mut Cursor::new(u32::MAX.to_le_bytes().to_vec())).unwrap_err();
        assert_eq!(e.text(), "frame too large: 4294967295 bytes");
    }

    #[test]
    fn io_errors_are_not_protocol_errors() {
        struct Reset;
        impl Read for Reset {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::new(ErrorKind::ConnectionReset, "reset by peer"))
            }
        }
        let e = read_frame(&mut Reset).unwrap_err();
        assert!(!e.is_protocol());
        assert_eq!(e.text(), "reset by peer");
    }

    #[test]
    fn interrupted_reads_are_retried() {
        struct Eintr {
            left: u32,
            inner: Cursor<Vec<u8>>,
        }
        impl Read for Eintr {
            fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                if self.left > 0 {
                    self.left -= 1;
                    return Err(io::Error::new(ErrorKind::Interrupted, "eintr"));
                }
                let one = 1.min(buf.len());
                let n = self.inner.read(&mut buf[..one])?;
                self.left = 1;
                Ok(n)
            }
        }
        let mut r = Eintr { left: 1, inner: Cursor::new(frame(b"{}")) };
        assert_eq!(read_frame(&mut r).unwrap().unwrap(), b"{}");
    }
}
