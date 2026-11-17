//! The game connection owned by Rust (the Rust port's phase 1: .claude/work/rust-port-plan-2026-11-15.md section 4):
//! the listening socket, the reads and the framing, the outgoing queue and the writes run on threads of their own,
//! without the GIL; Python's asyncio loop (protocol.HostServer / HostStream) takes what came in batches and runs the
//! handlers unchanged (the "Python lane").
//!
//! Threads (std, blocking sockets; a handful of connections: the game, the launcher's readiness probe, tools):
//!   bnge-io        accepts (TCP_NODELAY on each connection, as asyncio sets it)
//!   bnge-rd-N      reads connection N's frames into the inbox
//!   bnge-wr-N      writes connection N's queue
//!   bnge-deliver   the only one that takes the GIL: the inbox as one batch per wake, handed to the loop with
//!                  loop.call_soon_threadsafe(on_batch, batch); a stall of the lane is logged from here
//! Frames reach Python in the order read, per connection; connections are independent.
pub mod frame;
mod wire;

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

/// What the delivery thread hands to Python.
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
}

struct Inbox {
    evs: Vec<Ev>,
    /// the batch handed to the loop and not yet taken (at most one: what comes meanwhile joins it), with frames:
    /// (its serial, its frames, the oldest one's read time)
    out: Option<(u64, usize, Instant)>,
    serial: u64,
    /// a stall logged for the frames waiting now (once per stall)
    stall_logged: bool,
}

pub struct Shared {
    inbox: Mutex<Inbox>,
    cv: Condvar,
    stop: AtomicBool,
    /// accepting stopped (Host.close)
    closed: Arc<AtomicBool>,
    /// the accept thread and the connections' threads still running: once closed and none is, the delivery thread
    /// ends by itself (letting go of the Python callbacks) when nothing is left to hand over
    live: AtomicUsize,
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

    pub fn push(&self, ev: Ev) {
        self.inbox().evs.push(ev);
        self.cv.notify_one();
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

/// The Python objects the delivery thread calls (dropped while attached, or leaked once the interpreter is gone).
struct Sinks {
    event_loop: Py<PyAny>,
    on_batch: Py<PyAny>,
    logger: Py<PyAny>,
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
    /// accepting stopped (the accept thread's flag too)
    closed: Arc<AtomicBool>,
    accept: Mutex<Option<JoinHandle<()>>>,
    deliver: Mutex<Option<JoinHandle<()>>>,
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

fn deliver_loop(shared: Arc<Shared>, sinks: Sinks) {
    let mut last: Option<Arc<BatchInner>> = None;
    loop {
        let mut ib = shared.inbox();
        if ib.evs.is_empty() && !shared.stop.load(Ordering::SeqCst) {
            ib = shared.cv.wait_timeout(ib, Duration::from_millis(500)).unwrap_or_else(|p| p.into_inner()).0;
        }
        if shared.stop.load(Ordering::SeqCst) {
            break;
        }
        if ib.evs.is_empty() && shared.closed.load(Ordering::SeqCst) && shared.live.load(Ordering::SeqCst) == 0 {
            break; // (closed, every connection ended and handed over: nothing can come any more)
        }
        // (frames handed to the loop and still not taken: the lane is stalled; said once per stall)
        if let Some((_, handed, at)) = ib.out {
            let waited = at.elapsed().as_secs_f64();
            if waited > STALL_S && !ib.stall_logged {
                ib.stall_logged = true;
                let n = handed + ib.evs.iter().filter(|e| matches!(e, Ev::Frame(..))).count();
                drop(ib);
                log_stall(&sinks, n, waited);
                continue;
            }
        }
        if ib.evs.is_empty() {
            continue;
        }
        let evs = std::mem::take(&mut ib.evs);
        drop(ib);
        let frames = evs.iter().filter(|e| matches!(e, Ev::Frame(..))).count();
        let oldest = evs.iter().find_map(|e| if let Ev::Frame(_, _, at) = e { Some(*at) } else { None });
        // (the batch handed over last and not taken yet, the lane busy: these join it, one callback for all of it
        // once the lane is free, in order; locks: the batch's, then the inbox's, as take() does)
        let mut evs = Some(evs);
        if let Some(b) = &last {
            let mut g = b.evs.lock().unwrap_or_else(|p| p.into_inner());
            if let Some(v) = g.as_mut() {
                v.extend(evs.take().unwrap_or_default());
                let mut ib = shared.inbox();
                match (&mut ib.out, oldest) {
                    (Some(o), _) => o.1 += frames,
                    (none, Some(at)) => *none = Some((b.serial, frames, at)),
                    _ => {}
                }
            }
        }
        let Some(evs) = evs else { continue };
        let serial = {
            let mut ib = shared.inbox();
            ib.serial += 1;
            if let Some(at) = oldest {
                ib.out = Some((ib.serial, frames, at));
            }
            ib.serial
        };
        let inner = Arc::new(BatchInner { evs: Mutex::new(Some(evs)), serial });
        let handed = Python::try_attach(|py| -> bool {
            let batch = match Py::new(py, Batch { inner: inner.clone(), shared: shared.clone() }) {
                Ok(b) => b,
                Err(_) => return false,
            };
            // (a closed loop raises RuntimeError: the server is ending, nothing more to hand over)
            sinks.event_loop.bind(py).call_method1("call_soon_threadsafe", (sinks.on_batch.bind(py), batch)).is_ok()
        });
        if handed != Some(true) {
            shared.stop.store(true, Ordering::SeqCst);
            break;
        }
        last = Some(inner);
    }
    // (the Python objects let go while attached; leaked once the interpreter is ending, never touched then)
    let mut sinks = Some(sinks);
    if Python::try_attach(|_py| drop(sinks.take())).is_none() {
        std::mem::forget(sinks.take());
    }
}

fn log_stall(sinks: &Sinks, frames: usize, waited: f64) {
    let _ = Python::try_attach(|py| {
        let _ = sinks.logger.bind(py).call_method1(
            "warning",
            ("python lane: %d frames waiting %.1f s", frames, waited),
        );
    });
}

#[pymethods]
impl Host {
    /// Host(host, port, loop, on_batch, logger, raise_threads): binds at once (OSError with its errno as
    /// asyncio.start_server raises it) and starts accepting; on_batch(batch) runs on the loop for everything that came
    /// (batch.take()); logger: a logging.Logger for the stall line.
    #[new]
    #[pyo3(signature = (host, port, event_loop, on_batch, logger, raise_threads=false))]
    fn new(host: &str, port: u16, event_loop: Py<PyAny>, on_batch: Py<PyAny>, logger: Py<PyAny>,
           raise_threads: bool) -> PyResult<Self> {
        let listener = bind(host, port)?;
        let addr = listener.local_addr().map_err(|e| PyOSError::new_err(e.to_string()))?;
        let closed = Arc::new(AtomicBool::new(false));
        let shared = Arc::new(Shared {
            inbox: Mutex::new(Inbox { evs: Vec::new(), out: None, serial: 0, stall_logged: false }),
            cv: Condvar::new(),
            stop: AtomicBool::new(false),
            closed: closed.clone(),
            live: AtomicUsize::new(1), // (the accept thread)
            stats: Mutex::new(Stats::default()),
            conns: Mutex::new(Vec::new()),
        });
        let threads = Arc::new(Mutex::new(Vec::new()));
        let (sh, th) = (shared.clone(), threads.clone());
        let accept = spawn("bnge-io".into(), raise_threads, move || accept_loop(listener, sh, raise_threads, th))
            .map_err(|e| PyRuntimeError::new_err(format!("host: no accept thread: {e}")))?;
        let sh = shared.clone();
        let sinks = Sinks { event_loop, on_batch, logger };
        let deliver = spawn("bnge-deliver".into(), raise_threads, move || deliver_loop(sh, sinks))
            .map_err(|e| PyRuntimeError::new_err(format!("host: no delivery thread: {e}")))?;
        Ok(Host {
            shared,
            addr,
            closed,
            accept: Mutex::new(Some(accept)),
            deliver: Mutex::new(Some(deliver)),
            threads,
        })
    }

    #[getter]
    fn port(&self) -> u16 {
        self.addr.port()
    }

    fn is_serving(&self) -> bool {
        !self.closed.load(Ordering::SeqCst)
    }

    /// Stops accepting (asyncio's Server.close): open connections stay.
    fn close(&self, py: Python<'_>) {
        if self.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        let h = self.accept.lock().unwrap_or_else(|p| p.into_inner()).take();
        py.detach(|| stop_accepting(self.addr, h));
    }

    /// Ends everything: stops accepting, aborts the connections, stops the delivery and joins every thread (before
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
        let d = self.deliver.lock().unwrap_or_else(|p| p.into_inner()).take();
        let ts: Vec<JoinHandle<()>> = std::mem::take(&mut *self.threads.lock().unwrap_or_else(|p| p.into_inner()));
        py.detach(|| {
            if let Some(d) = d {
                let _ = d.join();
            }
            for t in ts {
                let _ = t.join();
            }
        });
    }

    /// The host's numbers since the last stats(reset=True): frames and bytes in and out, each frame's wait for the
    /// Python lane (p50 / p99 / max ms), connections accepted.
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
        // is held and the delivery thread may wait for it)
        if !self.closed.swap(true, Ordering::SeqCst) {
            let h = self.accept.get_mut().unwrap_or_else(|p| p.into_inner()).take();
            let addr = self.addr;
            std::thread::spawn(move || stop_accepting(addr, h));
        }
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.cv.notify_all();
    }
}

/// A batch's events: the delivery thread adds to them until Python takes them (None after).
struct BatchInner {
    evs: Mutex<Option<Vec<Ev>>>,
    serial: u64,
}

/// Everything that came since the last batch was taken, handed to the loop (earthfast.HostBatch).
#[pyclass(module = "earthfast", name = "HostBatch", frozen)]
pub struct Batch {
    inner: Arc<BatchInner>,
    shared: Arc<Shared>,
}

#[pymethods]
impl Batch {
    /// [(connection id, kind, value)] in the order they came, once (later calls: []). kind 0: a new connection,
    /// value its Wire; 1: a frame's payload (bytes); 2: the reading ended, value None (a clean EOF) or (errno or
    /// None, text, protocol error?); 3: the outgoing queue drained for a waiting drain() (None). Each frame's wait is
    /// counted now (the loop runs this callback: the lane took them).
    fn take<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let evs = {
            let mut g = self.inner.evs.lock().unwrap_or_else(|p| p.into_inner());
            let evs = g.take().unwrap_or_default();
            let mut ib = self.shared.inbox();
            if ib.out.is_some_and(|o| o.0 == self.inner.serial) {
                ib.out = None;
                ib.stall_logged = false;
            }
            evs
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
                    if let Some(w) = self.find(id) {
                        w.inbox_bytes.fetch_sub(data.len(), Ordering::Relaxed);
                    }
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
}

impl Batch {
    fn find(&self, id: u64) -> Option<Arc<WireCore>> {
        self.shared
            .conns
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .iter()
            .filter_map(|w| w.upgrade())
            .find(|w| w.id == id)
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
}

/// test_host.py only: holds the GIL for `seconds` (a kernel that never releases it), to show the host's I/O moving
/// meanwhile.
#[pyfunction]
fn host_hold_gil(_py: Python<'_>, seconds: f64) {
    std::thread::sleep(Duration::from_secs_f64(seconds.clamp(0.0, 30.0)));
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Host>()?;
    m.add_class::<Wire>()?;
    m.add_class::<Batch>()?;
    m.add_function(wrap_pyfunction!(host_hold_gil, m)?)?;
    Ok(())
}
