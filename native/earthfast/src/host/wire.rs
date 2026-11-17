//! One connection's sockets: the outgoing queue and its writer thread, the reader thread that frames what the game
//! sends, and close / abort as asyncio's transport does them.
//!
//! The writer thread takes the queued frames in order and hands them to the socket in pieces of at most PIECE bytes,
//! so the pending count (asyncio's get_write_buffer_size) falls as the kernel takes them. Python's drain() waits while
//! the queue is "paused": over HIGH bytes, until it is at LOW or less again (asyncio's default water marks, with its
//! hysteresis). No thread here ever takes the GIL: what Python must hear (a frame, the end, the queue drained) goes to
//! the host's inbox, which wakes the event loop.
use std::collections::VecDeque;
use std::io::{BufReader, ErrorKind, Write};
use std::net::{Shutdown, TcpStream};

use std::sync::{Arc, Condvar, Mutex};
use std::time::Instant;

use super::frame;
use super::record::Rec;
use super::{Ev, Shared};

/// asyncio's default write-buffer water marks (FlowControlMixin: high 64 KiB, low a quarter of it)
pub const HIGH: usize = 64 * 1024;
pub const LOW: usize = 16 * 1024;
/// the most bytes handed to the socket in one write: a blocking send returns only once all of it is in the kernel
pub const PIECE: usize = 256 * 1024;
/// The read side's flow control, asyncio's StreamReader (limit 64 KiB: reading pauses while more than twice the limit
/// is buffered and resumes at the limit): bytes read (frames with their headers) and not yet consumed by the handler
/// (HostStream.next_message), wherever they wait: the inbox, the batch, Python's queue. Over READ_HIGH the reader
/// stops between frames until the handler has consumed down to READ_LOW; the game's sends then wait in the kernel's
/// buffers, as with asyncio. At most READ_HIGH + one frame waits per connection.
pub const READ_HIGH: usize = 2 * 64 * 1024;
pub const READ_LOW: usize = 64 * 1024;

/// An OS error as Python would make it: (errno, strerror) for OSError(errno, strerror); None for errors with no errno.
pub fn os_error(e: &std::io::Error) -> (Option<i32>, String) {
    match e.raw_os_error() {
        Some(n) => {
            // (Rust prints "<strerror> (os error N)"; Python's OSError(N, strerror) prints "[Errno N] <strerror>")
            let text = e.to_string();
            let suffix = format!(" (os error {n})");
            (Some(n), text.strip_suffix(&suffix).unwrap_or(&text).to_string())
        }
        None => (None, e.to_string()),
    }
}

#[derive(Default)]
struct Out {
    q: VecDeque<Vec<u8>>,
    pending: usize,
    closing: bool,
    aborted: bool,
    /// the socket failed under a write (asyncio's connection lost with an exception)
    lost: Option<(Option<i32>, String)>,
    paused: bool,
    /// flushed after close() and shut down (asyncio's connection lost without an exception)
    gone: bool,
    /// a Python drain() waits for the queue to go below LOW (a Drained event when it does)
    want_drain: bool,
}

pub struct WireCore {
    pub id: u64,
    pub peer: (String, u16),
    sock: TcpStream,
    out: Mutex<Out>,
    out_cv: Condvar,
    /// bytes read and not yet consumed by the handler (READ_HIGH / READ_LOW), and the reader's wait on it
    unread: Mutex<usize>,
    read_cv: Condvar,
    /// when it was accepted (the recorder counts its times from it)
    t0: Instant,
    shared: Arc<Shared>,
}

impl WireCore {
    pub fn new(id: u64, sock: TcpStream, shared: Arc<Shared>) -> std::io::Result<Arc<WireCore>> {
        let peer = match sock.peer_addr() {
            Ok(a) => (a.ip().to_string(), a.port()),
            Err(_) => (String::new(), 0),
        };
        Ok(Arc::new(WireCore {
            id,
            peer,
            sock,
            out: Mutex::new(Out::default()),
            out_cv: Condvar::new(),
            unread: Mutex::new(0),
            read_cv: Condvar::new(),
            t0: Instant::now(),
            shared,
        }))
    }

    fn out(&self) -> std::sync::MutexGuard<'_, Out> {
        self.out.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn unread(&self) -> std::sync::MutexGuard<'_, usize> {
        self.unread.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// bytes read and not yet consumed by the handler
    pub fn unread_bytes(&self) -> usize {
        *self.unread()
    }

    /// The handler consumed a frame of n bytes (its payload + the 4-byte header): reading resumes at READ_LOW.
    pub fn consumed(&self, n: usize) {
        let mut u = self.unread();
        *u = u.saturating_sub(n);
        if *u <= READ_LOW {
            self.read_cv.notify_all();
        }
    }

    /// Two frames merged into one (1b): `old` bytes now count as `new`.
    pub fn merged(&self, old: usize, new: usize) {
        let mut u = self.unread();
        *u = (*u + new).saturating_sub(old);
        if *u <= READ_LOW {
            self.read_cv.notify_all();
        }
    }

    /// The reader's pause: over READ_HIGH, wait (between frames) until the handler has consumed down to READ_LOW or
    /// the connection is closing (whatever comes then is dropped anyway).
    fn wait_for_room(&self) {
        let mut u = self.unread();
        if *u <= READ_HIGH {
            return;
        }
        while *u > READ_LOW && !self.is_closing() && !self.shared.stopping() {
            u = self.read_cv.wait_timeout(u, std::time::Duration::from_millis(200)).unwrap_or_else(|p| p.into_inner()).0;
        }
    }

    /// Queues a frame (its bytes copied); ignored once closing, aborted or lost (as Connection.send checks first).
    pub fn write(&self, data: &[u8]) {
        if data.is_empty() {
            return;
        }
        let mut o = self.out();
        if o.closing || o.aborted || o.lost.is_some() {
            return;
        }
        o.q.push_back(data.to_vec());
        o.pending += data.len();
        drop(o);
        self.shared.count_out(1, 0);
        self.out_cv.notify_one();
    }

    pub fn pending(&self) -> usize {
        self.out().pending
    }

    pub fn is_closing(&self) -> bool {
        let o = self.out();
        o.closing || o.aborted || o.lost.is_some()
    }

    /// asyncio's "connection lost": aborted, failed, or closed and flushed (drain() raises ConnectionResetError)
    pub fn lost(&self) -> bool {
        let o = self.out();
        o.aborted || o.gone || o.lost.is_some()
    }

    pub fn paused(&self) -> bool {
        self.out().paused
    }

    /// drain(): true when the queue is paused (a Drained event follows once it is at LOW or less, or the connection
    /// goes); false: no need to wait
    pub fn drain_wait(&self) -> bool {
        let mut o = self.out();
        // (asyncio pauses once what the kernel has not taken is over HIGH: asyncio's write hands the bytes to the
        // socket at once, the writer thread here a moment later, so the queue is judged when drain() asks)
        if o.pending > HIGH {
            o.paused = true;
        }
        if o.paused && !o.aborted && o.lost.is_none() {
            o.want_drain = true;
            true
        } else {
            false
        }
    }

    /// asyncio's close(): what is queued is sent, then the socket is shut down; nothing more is read into Python
    pub fn close(&self) {
        self.out().closing = true;
        self.out_cv.notify_one();
        self.read_cv.notify_all(); // (a reader paused for room: nothing more reaches the handler now)
    }

    /// asyncio's abort(): what is queued is dropped and the socket shut down at once
    pub fn abort(&self) {
        let wake = {
            let mut o = self.out();
            o.aborted = true;
            o.q.clear();
            o.pending = 0;
            o.paused = false;
            std::mem::take(&mut o.want_drain)
        };
        if wake {
            self.shared.push(Ev::Drained(self.id));
        }
        self.out_cv.notify_one();
        self.read_cv.notify_all();
        let _ = self.sock.shutdown(Shutdown::Both);
    }

    /// The writer thread's body.
    pub fn write_loop(self: Arc<Self>) {
        let mut sock = match self.sock.try_clone() {
            Ok(s) => s,
            Err(e) => {
                self.fail(&e);
                return;
            }
        };
        // (1c: what goes out, with BNGE_HOST_RECORD_OUT=1; finished when this thread ends)
        let mut rec = self.shared.record.as_ref().filter(|r| r.out).and_then(|r| Rec::open(r, self.id, "out", self.t0));
        loop {
            let chunk = {
                let mut o = self.out();
                loop {
                    if o.aborted || o.lost.is_some() {
                        return;
                    }
                    if let Some(c) = o.q.pop_front() {
                        break c;
                    }
                    if o.closing {
                        o.gone = true;
                        drop(o);
                        // (flushed: the socket goes; the reader sees its end)
                        let _ = self.sock.shutdown(Shutdown::Both);
                        return;
                    }
                    o = self.out_cv.wait(o).unwrap_or_else(|p| p.into_inner());
                }
            };
            if let Some(r) = rec.as_mut() {
                r.put(&chunk);
            }
            let mut off = 0;
            while off < chunk.len() {
                let end = (off + PIECE).min(chunk.len());
                match sock.write(&chunk[off..end]) {
                    Ok(0) => {
                        self.fail(&std::io::Error::new(ErrorKind::WriteZero, "socket accepted no bytes"));
                        return;
                    }
                    Ok(n) => {
                        off += n;
                        let wake = {
                            let mut o = self.out();
                            if o.aborted {
                                return;
                            }
                            o.pending = o.pending.saturating_sub(n);
                            if o.paused && o.pending <= LOW {
                                o.paused = false;
                                std::mem::take(&mut o.want_drain)
                            } else {
                                false
                            }
                        };
                        self.shared.count_out(0, n as u64);
                        if wake {
                            self.shared.push(Ev::Drained(self.id));
                        }
                    }
                    Err(e) if e.kind() == ErrorKind::Interrupted => {}
                    Err(e) => {
                        self.fail(&e);
                        return;
                    }
                }
            }
        }
    }

    /// The socket failed under a write: the queue goes, a waiting drain wakes (and raises), the reader ends with it.
    fn fail(&self, e: &std::io::Error) {
        self.mark_lost(os_error(e));
        let _ = self.sock.shutdown(Shutdown::Both);
    }

    /// The connection failed (a write or a read): nothing more is sent, a waiting drain wakes and raises.
    fn mark_lost(&self, err: (Option<i32>, String)) {
        let wake = {
            let mut o = self.out();
            if o.aborted || o.lost.is_some() {
                false
            } else {
                o.lost = Some(err);
                o.q.clear();
                o.pending = 0;
                o.paused = false;
                std::mem::take(&mut o.want_drain)
            }
        };
        if wake {
            self.shared.push(Ev::Drained(self.id));
        }
        self.out_cv.notify_one();
    }

    /// The reader thread's body: frames into the host's inbox in the order read, then the end (None: a clean EOF, or
    /// any end once this side closed or aborted, as asyncio's connection_lost(None) reads as EOF).
    pub fn read_loop(self: Arc<Self>) {
        let sock = match self.sock.try_clone() {
            Ok(s) => s,
            Err(e) => {
                self.shared.push(Ev::End(self.id, Some(End::os(&e))));
                return;
            }
        };
        let mut r = BufReader::with_capacity(64 * 1024, sock);
        // (1c: every frame read, BNGE_HOST_RECORD; finished when this thread ends)
        let mut rec = self.shared.record.as_ref().and_then(|r| Rec::open(r, self.id, "in", self.t0));
        let end = loop {
            self.wait_for_room();
            match frame::read_frame(&mut r) {
                Ok(Some(data)) => {
                    if let Some(rc) = rec.as_mut() {
                        rc.put(&data);
                    }
                    if self.is_closing() {
                        continue; // (asyncio stops reading at close: nothing more reaches the handler)
                    }
                    self.shared.count_in(data.len() as u64);
                    *self.unread() += data.len() + frame::HEADER;
                    self.shared.push_frame(&self, data, Instant::now());
                }
                Ok(None) => break None,
                Err(e) => break Some(End::from_frame(&e)),
            }
        };
        drop(rec); // (the recording complete before Python hears the end)
        let end = {
            let o = self.out();
            if o.closing || o.aborted {
                None
            } else if let Some((errno, text)) = &o.lost {
                // (the writer saw the socket fail first: the read side reports it, as asyncio's connection_lost(exc))
                Some(End { errno: *errno, text: text.clone(), protocol: false })
            } else {
                end
            }
        };
        let clean = end.is_none();
        if let Some(e) = &end {
            if !e.protocol {
                self.mark_lost((e.errno, e.text.clone()));
                let _ = self.sock.shutdown(Shutdown::Both);
            }
        }
        self.shared.push(Ev::End(self.id, end));
        if clean {
            // (the peer's EOF: asyncio's transport closes, sending what is queued first)
            self.close();
        }
    }
}

/// How a connection's reading ended, for Python: protocol.ProtocolError(text), or OSError(errno, text)
#[derive(Debug, Clone)]
pub struct End {
    pub errno: Option<i32>,
    pub text: String,
    pub protocol: bool,
}

impl End {
    fn protocol(text: String) -> End {
        End { errno: None, text, protocol: true }
    }

    fn os(e: &std::io::Error) -> End {
        let (errno, text) = os_error(e);
        End { errno, text, protocol: false }
    }

    fn from_frame(e: &frame::FrameError) -> End {
        match e {
            frame::FrameError::Io(io) => End::os(io),
            other => End::protocol(other.text()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_errors_as_python_prints_them() {
        let e = std::io::Error::from_raw_os_error(104);
        let (n, t) = os_error(&e);
        assert_eq!(n, Some(104));
        assert!(!t.contains("os error"), "{t}");
        let (n, t) = os_error(&std::io::Error::new(ErrorKind::Other, "plain"));
        assert_eq!((n, t.as_str()), (None, "plain"));
    }
}
