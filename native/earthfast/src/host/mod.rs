//! The game connection owned by Rust (the Rust port's phase 1: .claude/work/rust-port-plan-2026-11-15.md section 4):
//! the listening socket, the reads and the framing, the outgoing queue and the writes run on threads of their own,
//! without the GIL; Python's asyncio loop (protocol.HostServer / HostStream) takes what came in batches and runs the
//! handlers unchanged (the "Python lane").
//!
//! Threads (std, blocking sockets; a handful of connections: the game, the launcher's readiness probe, tools):
//!   bnge-io        accepts (TCP_NODELAY on each connection, as asyncio sets it)
//!   bnge-rd-N      reads connection N's frames into the inbox
//!   bnge-wr-N      writes connection N's queue
//!   bnge-watch     logs a stalled lane ("python lane: N frames waiting X s"): the only thread that takes the GIL
//! The hand-over needs no GIL: the first event into an empty inbox writes one byte to a local socket pair whose other
//! end the event loop reads (as it reads any socket: one wake, as asyncio's own reads cost); the loop then takes
//! everything that came (Host.take), so what comes while it is busy waits in one batch. Frames reach Python in the
//! order read, per connection; connections are independent.
pub mod frame;
mod merge;
pub mod farstate;
pub mod msgs;
pub mod pyjob;
pub mod pyjson;
mod record;
mod wire;

use std::io::Write;
use std::net::{SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyOSError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

use wire::{End, WireCore};

/// a frame waiting this long for the Python lane is logged (earthd.LAG_STALL_S)
const STALL_S: f64 = 2.0;
/// waits kept per stats interval (the percentiles of the host line)
const WAITS_MAX: usize = 100_000;

/// What Python takes.
pub enum Ev {
    Connect(Arc<WireCore>),
    Frame(u64, Vec<u8>, Instant),
    /// the reading ended: None at a clean EOF (or after this side closed)
    End(u64, Option<End>),
    /// the outgoing queue went below LOW for a waiting drain() (or the connection went)
    Drained(u64),
}

#[derive(Default)]
struct Stats {
    frames_in: u64,
    bytes_in: u64,
    frames_out: u64,
    bytes_out: u64,
    /// each frame's wait before Python took it, µs
    waits: Vec<u32>,
    wait_n: u64,
    wait_max_us: u64,
    connections: u64,
    /// poses merged (1b)
    merged: u64,
}

#[derive(Default)]
struct Inbox {
    evs: Vec<Ev>,
    /// the wake byte is written and Python has not taken the inbox since
    woken: bool,
    /// a stall logged for the frames waiting now (once per stall)
    stall_logged: bool,
}

pub struct Shared {
    inbox: Mutex<Inbox>,
    /// the watchdog's wake-ups (a thread ended, shutdown)
    cv: Condvar,
    /// this end of the wake pair (non-blocking: one byte per wake, a full buffer only means a wake is already there)
    waker: Mutex<Option<Box<dyn Write + Send>>>,
    stop: AtomicBool,
    /// accepting stopped (Host.close)
    closed: AtomicBool,
    /// the accept thread and the connections' threads still running: once closed and none is, the watchdog ends by
    /// itself (letting go of the logger) when nothing is left to take
    live: AtomicUsize,
    /// 1b: consecutive poses merged while they wait for the lane (BNGE_HOST_MERGE=off: never)
    merge: bool,
    /// 1c: BNGE_HOST_RECORD (None: nothing recorded)
    record: Option<record::Recording>,
    stats: Mutex<Stats>,
    conns: Mutex<Vec<Weak<WireCore>>>,
}

impl Shared {
    fn thread_done(&self) {
        self.live.fetch_sub(1, Ordering::SeqCst);
        let _ib = self.inbox();
        self.cv.notify_all();
    }

    fn inbox(&self) -> std::sync::MutexGuard<'_, Inbox> {
        self.inbox.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn stats(&self) -> std::sync::MutexGuard<'_, Stats> {
        self.stats.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// An event for Python; the first since Python last took the inbox wakes the loop.
    pub fn push(&self, ev: Ev) {
        let wake = {
            let mut ib = self.inbox();
            ib.evs.push(ev);
            !std::mem::replace(&mut ib.woken, true)
        };
        if wake {
            self.wake();
        }
    }

    pub fn stopping(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }

    /// A frame read (its bytes already counted unread): into the inbox, or, with the merge on (1b, merge.rs), into
    /// the pose before it when that is its connection's previous frame still waiting there (a drained mark is no
    /// frame) and they may merge: the merged pose keeps the earlier one's place and read time. Merging as frames
    /// come keeps a stalled lane's poses one frame (and its unread bytes small) however long the stall.
    pub fn push_frame(&self, w: &WireCore, data: Vec<u8>, at: Instant) {
        let mut merged = None;
        let wake = {
            let mut ib = self.inbox();
            if self.merge && merge::maybe_pose(&data) {
                let prev = ib.evs.iter_mut().rev().find(|p| ev_id(p) == w.id && !matches!(p, Ev::Drained(_)));
                if let Some(Ev::Frame(_, prev, _)) = prev {
                    if merge::maybe_pose(prev) {
                        if let Some(m) = merge::merge(prev, &data) {
                            merged = Some((prev.len() + data.len() + 2 * frame::HEADER, m.len() + frame::HEADER));
                            *prev = m;
                        }
                    }
                }
            }
            if merged.is_none() {
                ib.evs.push(Ev::Frame(w.id, data, at));
            }
            !std::mem::replace(&mut ib.woken, true)
        };
        if let Some((old, new)) = merged {
            w.merged(old, new);
            self.stats().merged += 1;
        }
        if wake {
            self.wake();
        }
    }

    fn wake(&self) {
        if let Some(w) = self.waker.lock().unwrap_or_else(|p| p.into_inner()).as_mut() {
            let _ = w.write(&[1]);
        }
    }

    pub fn count_in(&self, bytes: u64) {
        let mut s = self.stats();
        s.frames_in += 1;
        s.bytes_in += bytes;
    }

    pub fn count_out(&self, frames: u64, bytes: u64) {
        let mut s = self.stats();
        s.frames_out += frames;
        s.bytes_out += bytes;
    }
}

/// The wake pair: (this end, the event loop's end as a raw descriptor Python's socket.socket(fileno=...) takes over).
#[cfg(unix)]
fn wake_pair() -> std::io::Result<(Box<dyn Write + Send>, i64)> {
    use std::os::fd::IntoRawFd;
    let (a, b) = std::os::unix::net::UnixStream::pair()?;
    a.set_nonblocking(true)?;
    Ok((Box::new(a), b.into_raw_fd() as i64))
}

#[cfg(windows)]
fn wake_pair() -> std::io::Result<(Box<dyn Write + Send>, i64)> {
    // (no socketpair on Windows: two loopback TCP ends, the accepted one checked to be ours)
    use std::os::windows::io::IntoRawSocket;
    let l = TcpListener::bind("127.0.0.1:0")?;
    let a = TcpStream::connect(l.local_addr()?)?;
    let mine = a.local_addr()?;
    for _ in 0..8 {
        let (b, peer) = l.accept()?;
        if peer == mine {
            a.set_nodelay(true)?;
            a.set_nonblocking(true)?;
            return Ok((Box::new(a), b.into_raw_socket() as i64));
        }
    }
    Err(std::io::Error::new(std::io::ErrorKind::Other, "host: the wake pair's connection was not ours"))
}

#[cfg(windows)]
fn raise_thread() {
    // (the event loop's treatment, earthd.LOOP_RAISE / budget.raise_thread: Windows threads start at normal priority
    // whatever their creator's)
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThread() -> isize;
        fn SetThreadPriority(thread: isize, priority: i32) -> i32;
    }
    const THREAD_PRIORITY_ABOVE_NORMAL: i32 = 1;
    // SAFETY: GetCurrentThread's pseudo handle needs no closing
    unsafe {
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
    }
}

#[cfg(not(windows))]
fn raise_thread() {
    // (Linux: threads inherit the creating thread's niceness, scheduling class and CPU set: the loop's)
}

fn spawn<F: FnOnce() + Send + 'static>(name: String, raise: bool, f: F) -> std::io::Result<JoinHandle<()>> {
    std::thread::Builder::new().name(name).spawn(move || {
        if raise {
            raise_thread();
        }
        f()
    })
}

/// The listening socket and its threads (earthfast.Host).
#[pyclass(module = "earthfast", frozen)]
pub struct Host {
    shared: Arc<Shared>,
    addr: SocketAddr,
    wake_fd: Mutex<Option<i64>>,
    accept: Mutex<Option<JoinHandle<()>>>,
    watch: Mutex<Option<JoinHandle<()>>>,
    threads: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

fn bind(host: &str, port: u16) -> PyResult<TcpListener> {
    let fail = |e: std::io::Error| {
        let (errno, text) = wire::os_error(&e);
        // (asyncio's text: "[Errno 98] error while attempting to bind on address ('127.0.0.1', 47613): address
        // already in use")
        let what = format!("error while attempting to bind on address ('{host}', {port}): {}", text.to_lowercase());
        match errno {
            Some(n) => PyOSError::new_err((n, what)),
            None => PyOSError::new_err(what),
        }
    };
    let addrs: Vec<SocketAddr> = (host, port).to_socket_addrs().map_err(fail)?.collect();
    let mut last = None;
    for a in addrs {
        // (std sets SO_REUSEADDR on Unix and not on Windows, as asyncio.start_server does)
        match TcpListener::bind(a) {
            Ok(l) => return Ok(l),
            Err(e) => last = Some(e),
        }
    }
    Err(fail(last.unwrap_or_else(|| std::io::Error::new(std::io::ErrorKind::AddrNotAvailable, "no address"))))
}

fn accept_loop(listener: TcpListener, shared: Arc<Shared>, raise: bool, threads: Arc<Mutex<Vec<JoinHandle<()>>>>) {
    let next = AtomicU64::new(1);
    for conn in listener.incoming() {
        if shared.closed.load(Ordering::SeqCst) || shared.stop.load(Ordering::SeqCst) {
            break;
        }
        let sock = match conn {
            Ok(s) => s,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => {
                // (EMFILE and kin: asyncio logs and pauses accepting; a short wait keeps this from spinning)
                std::thread::sleep(Duration::from_millis(100));
                continue;
            }
        };
        let _ = sock.set_nodelay(true);
        let id = next.fetch_add(1, Ordering::Relaxed);
        let w = match WireCore::new(id, sock, shared.clone()) {
            Ok(w) => w,
            Err(_) => continue,
        };
        {
            let mut conns = shared.conns.lock().unwrap_or_else(|p| p.into_inner());
            conns.retain(|c| c.strong_count() > 0);
            conns.push(Arc::downgrade(&w));
        }
        shared.stats().connections += 1;
        shared.push(Ev::Connect(w.clone()));
        let mut t = threads.lock().unwrap_or_else(|p| p.into_inner());
        t.retain(|h| !h.is_finished());
        for (name, reads) in [(format!("bnge-wr-{id}"), false), (format!("bnge-rd-{id}"), true)] {
            let (wc, sh) = (w.clone(), shared.clone());
            shared.live.fetch_add(1, Ordering::SeqCst);
            let started = spawn(name, raise, move || {
                if reads {
                    wc.read_loop();
                } else {
                    wc.write_loop();
                }
                sh.thread_done();
            });
            match started {
                Ok(h) => t.push(h),
                Err(_) => {
                    shared.thread_done();
                    if reads {
                        // (no reader: Python hears the end at once)
                        w.abort();
                        shared.push(Ev::End(id, None));
                    }
                }
            }
        }
    }
    shared.thread_done();
}

/// The watchdog: a lane that has not taken what came for STALL_S is logged, once per stall (through Python's
/// logging, so as soon as the GIL is free); ends once closed with every connection over and nothing left to take.
fn watch_loop(shared: Arc<Shared>, logger: Py<PyAny>) {
    loop {
        let mut ib = shared.inbox();
        if !shared.stop.load(Ordering::SeqCst) {
            ib = shared.cv.wait_timeout(ib, Duration::from_millis(500)).unwrap_or_else(|p| p.into_inner()).0;
        }
        if shared.stop.load(Ordering::SeqCst)
            || (ib.evs.is_empty() && shared.closed.load(Ordering::SeqCst) && shared.live.load(Ordering::SeqCst) == 0)
        {
            break;
        }
        let oldest = ib.evs.iter().find_map(|e| if let Ev::Frame(_, _, at) = e { Some(*at) } else { None });
        if let Some(at) = oldest {
            let waited = at.elapsed().as_secs_f64();
            if waited > STALL_S && !ib.stall_logged {
                ib.stall_logged = true;
                let n = ib.evs.iter().filter(|e| matches!(e, Ev::Frame(..))).count();
                drop(ib);
                let _ = Python::try_attach(|py| {
                    let _ = logger.bind(py).call_method1("warning", ("python lane: %d frames waiting %.1f s", n, waited));
                });
            }
        }
    }
    // (the logger let go while attached; leaked once the interpreter is ending, never touched then)
    let mut logger = Some(logger);
    if Python::try_attach(|_py| drop(logger.take())).is_none() {
        std::mem::forget(logger.take());
    }
}

fn ev_id(e: &Ev) -> u64 {
    match e {
        Ev::Connect(w) => w.id,
        Ev::Frame(id, ..) | Ev::End(id, _) | Ev::Drained(id) => *id,
    }
}

#[pymethods]
impl Host {
    /// Host(host, port, logger, raise_threads=False, merge=True, record=None, record_out=False): binds at once (an
    /// OSError with its errno as asyncio.start_server raises it) and starts accepting. take_wake() gives the event
    /// loop's end of the wake pair (once); each byte on it means: take(). logger: a logging.Logger for the stall line.
    #[new]
    #[pyo3(signature = (host, port, logger, raise_threads=false, merge=true, record=None, record_out=false))]
    fn new(host: &str, port: u16, logger: Py<PyAny>, raise_threads: bool, merge: bool,
           record: Option<std::path::PathBuf>, record_out: bool) -> PyResult<Self> {
        let listener = bind(host, port)?;
        let addr = listener.local_addr().map_err(|e| PyOSError::new_err(e.to_string()))?;
        let (waker, wake_fd) = wake_pair().map_err(|e| PyOSError::new_err(format!("host: no wake pair: {e}")))?;
        let shared = Arc::new(Shared {
            inbox: Mutex::new(Inbox::default()),
            cv: Condvar::new(),
            waker: Mutex::new(Some(waker)),
            stop: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            live: AtomicUsize::new(1), // (the accept thread)
            merge,
            record: record.map(|dir| record::Recording {
                dir,
                out: record_out,
                stamp: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis())
                    .unwrap_or(0),
            }),
            stats: Mutex::new(Stats::default()),
            conns: Mutex::new(Vec::new()),
        });
        let threads = Arc::new(Mutex::new(Vec::new()));
        let (sh, th) = (shared.clone(), threads.clone());
        let accept = spawn("bnge-io".into(), raise_threads, move || accept_loop(listener, sh, raise_threads, th))
            .map_err(|e| PyRuntimeError::new_err(format!("host: no accept thread: {e}")))?;
        let sh = shared.clone();
        let watch = spawn("bnge-watch".into(), raise_threads, move || watch_loop(sh, logger))
            .map_err(|e| PyRuntimeError::new_err(format!("host: no watchdog thread: {e}")))?;
        Ok(Host {
            shared,
            addr,
            wake_fd: Mutex::new(Some(wake_fd)),
            accept: Mutex::new(Some(accept)),
            watch: Mutex::new(Some(watch)),
            threads,
        })
    }

    #[getter]
    fn port(&self) -> u16 {
        self.addr.port()
    }

    /// The event loop's end of the wake pair, a raw socket for socket.socket(fileno=...), which owns it from then on.
    fn take_wake(&self) -> PyResult<i64> {
        self.wake_fd
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("host: the wake socket was taken already"))
    }

    /// [(connection id, kind, value)]: everything that came since the last take, in order (poses merged, 1b). kind
    /// 0: a new connection, value its Wire; 1: a frame's payload (bytes); 2: the reading ended, value None (a clean
    /// EOF) or (errno or None, text, protocol error?); 3: the outgoing queue drained for a waiting drain() (None).
    /// Each frame's wait for the lane is counted now.
    fn take<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let evs = {
            let mut ib = self.shared.inbox();
            ib.woken = false;
            ib.stall_logged = false;
            std::mem::take(&mut ib.evs)
        };
        let now = Instant::now();
        let out = PyList::empty(py);
        let mut waits = Vec::new();
        for e in evs {
            let item = match e {
                Ev::Connect(w) => {
                    let id = w.id;
                    (id, 0u8, Py::new(py, Wire { core: w })?.into_any())
                }
                Ev::Frame(id, data, at) => {
                    waits.push(now.saturating_duration_since(at).as_micros() as u64);
                    (id, 1u8, PyBytes::new(py, &data).into_any().unbind())
                }
                Ev::End(id, None) => (id, 2u8, py.None()),
                Ev::End(id, Some(e)) => {
                    (id, 2u8, (e.errno, e.text, e.protocol).into_pyobject(py)?.into_any().unbind())
                }
                Ev::Drained(id) => (id, 3u8, py.None()),
            };
            out.append(item)?;
        }
        if !waits.is_empty() {
            let mut s = self.shared.stats();
            for w in waits {
                s.wait_n += 1;
                s.wait_max_us = s.wait_max_us.max(w);
                if s.waits.len() < WAITS_MAX {
                    s.waits.push(w.min(u32::MAX as u64) as u32);
                }
            }
        }
        Ok(out)
    }

    fn is_serving(&self) -> bool {
        !self.shared.closed.load(Ordering::SeqCst)
    }

    /// Stops accepting (asyncio's Server.close): open connections stay.
    fn close(&self, py: Python<'_>) {
        if self.shared.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        let h = self.accept.lock().unwrap_or_else(|p| p.into_inner()).take();
        py.detach(|| stop_accepting(self.addr, h));
    }

    /// Ends everything: stops accepting, aborts the connections, stops the watchdog and joins every thread (before
    /// the interpreter finalizes: protocol.py's atexit hook and earthd's main_async call it).
    fn shutdown(&self, py: Python<'_>) {
        self.close(py);
        let conns: Vec<Arc<WireCore>> = self
            .shared
            .conns
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .iter()
            .filter_map(|w| w.upgrade())
            .collect();
        for c in &conns {
            c.abort();
        }
        drop(conns);
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.cv.notify_all();
        let w = self.watch.lock().unwrap_or_else(|p| p.into_inner()).take();
        let ts: Vec<JoinHandle<()>> = std::mem::take(&mut *self.threads.lock().unwrap_or_else(|p| p.into_inner()));
        py.detach(|| {
            if let Some(w) = w {
                let _ = w.join();
            }
            for t in ts {
                let _ = t.join();
            }
        });
        // (the wake pair's end here closes: the loop's end reads EOF)
        self.shared.waker.lock().unwrap_or_else(|p| p.into_inner()).take();
    }

    /// The host's numbers since the last stats(reset=True): frames and bytes in and out, each frame's wait for the
    /// Python lane (p50 / p99 / max ms), connections accepted, poses merged.
    #[pyo3(signature = (reset=false))]
    fn stats<'py>(&self, py: Python<'py>, reset: bool) -> PyResult<Bound<'py, PyDict>> {
        let mut s = self.shared.stats();
        let d = PyDict::new(py);
        d.set_item("frames_in", s.frames_in)?;
        d.set_item("bytes_in", s.bytes_in)?;
        d.set_item("frames_out", s.frames_out)?;
        d.set_item("bytes_out", s.bytes_out)?;
        d.set_item("connections", s.connections)?;
        d.set_item("waits", s.wait_n)?;
        d.set_item("merged", s.merged)?;
        let mut w = s.waits.clone();
        w.sort_unstable();
        let pick = |q: f64| -> f64 {
            if w.is_empty() {
                0.0
            } else {
                let i = ((q * w.len() as f64).ceil() as usize).clamp(1, w.len()) - 1;
                w[i] as f64 / 1000.0
            }
        };
        d.set_item("wait_p50_ms", pick(0.5))?;
        d.set_item("wait_p99_ms", pick(0.99))?;
        d.set_item("wait_max_ms", s.wait_max_us as f64 / 1000.0)?;
        if reset {
            *s = Stats::default();
        }
        Ok(d)
    }
}

/// Wakes the accept thread (its flag already set) with a connection to its own address and waits for it to end.
fn stop_accepting(addr: SocketAddr, accept: Option<JoinHandle<()>>) {
    let mut wake = addr;
    if wake.ip().is_unspecified() {
        wake.set_ip(if addr.is_ipv4() { [127, 0, 0, 1].into() } else { std::net::Ipv6Addr::LOCALHOST.into() });
    }
    // (the accept returns with this connection, sees its flag and ends; the listener closes with its thread)
    if TcpStream::connect_timeout(&wake, Duration::from_secs(2)).is_ok() {
        if let Some(h) = accept {
            let _ = h.join();
        }
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        // (dropped by Python's GC without shutdown(): the threads are told to end, never joined here, where the GIL
        // is held and the watchdog may wait for it)
        if !self.shared.closed.swap(true, Ordering::SeqCst) {
            let h = self.accept.get_mut().unwrap_or_else(|p| p.into_inner()).take();
            let addr = self.addr;
            std::thread::spawn(move || stop_accepting(addr, h));
        }
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.cv.notify_all();
    }
}

/// One connection, as Python's protocol.HostStream sees it (earthfast.Wire).
#[pyclass(module = "earthfast", frozen)]
pub struct Wire {
    core: Arc<WireCore>,
}

#[pymethods]
impl Wire {
    #[getter]
    fn id(&self) -> u64 {
        self.core.id
    }

    /// Queues a frame's bytes (copied) for the writer thread; ignored once closing.
    fn write(&self, data: &[u8]) {
        self.core.write(data);
    }

    /// bytes queued and not yet in the socket (asyncio's transport.get_write_buffer_size())
    fn pending(&self) -> usize {
        self.core.pending()
    }

    fn is_closing(&self) -> bool {
        self.core.is_closing()
    }

    /// the connection is gone (aborted, failed, or closed and flushed): drain() raises
    fn lost(&self) -> bool {
        self.core.lost()
    }

    fn paused(&self) -> bool {
        self.core.paused()
    }

    /// true: the queue is over the high-water mark; a drained event (kind 3) comes once it is at the low one
    fn drain_wait(&self) -> bool {
        self.core.drain_wait()
    }

    fn close(&self) {
        self.core.close();
    }

    fn abort(&self) {
        self.core.abort();
    }

    fn peer(&self) -> (String, u16) {
        self.core.peer.clone()
    }

    /// The handler consumed a frame of n bytes (payload + header): the reader's flow control (READ_HIGH / READ_LOW).
    fn consumed(&self, n: usize) {
        self.core.consumed(n);
    }

    /// bytes read and not yet consumed by the handler
    fn unread(&self) -> usize {
        self.core.unread_bytes()
    }
}

/// test_host.py only: holds the GIL for `seconds` (a kernel that never releases it), to show the host's I/O moving
/// meanwhile.
#[pyfunction]
fn host_hold_gil(_py: Python<'_>, seconds: f64) {
    std::thread::sleep(Duration::from_secs_f64(seconds.clamp(0.0, 30.0)));
}

/// 1c: a host recording's records [(ns since the connection was accepted, bytes)] (tools/hostreplay.py).
#[pyfunction]
fn host_read_recording<'py>(py: Python<'py>, path: std::path::PathBuf) -> PyResult<Bound<'py, PyList>> {
    let recs = py.detach(|| record::read(&path)).map_err(|e| PyOSError::new_err(format!("{}: {e}", path.display())))?;
    let out = PyList::empty(py);
    for (ns, data) in recs {
        out.append((ns, PyBytes::new(py, &data)))?;
    }
    Ok(out)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Host>()?;
    m.add_class::<Wire>()?;
    m.add_function(wrap_pyfunction!(host_hold_gil, m)?)?;
    m.add_function(wrap_pyfunction!(host_read_recording, m)?)?;
    // phase 2a: pyjson, the PyJob executor, the messages made in Rust
    m.add_function(wrap_pyfunction!(pyjson::encode_frame, m)?)?;
    m.add_function(wrap_pyfunction!(pyjson::pyjson_dumps, m)?)?;
    m.add_function(wrap_pyfunction!(pyjson::pyjson_dumps_j, m)?)?;
    m.add_function(wrap_pyfunction!(pyjson::pyjson_round, m)?)?;
    pyjob::register(m)?;
    msgs::register(m)?;
    farstate::register(m)?;
    Ok(())
}
