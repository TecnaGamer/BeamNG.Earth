//! Optional dispatch credits for JSON metadata. This is independent of shape-slot ownership and far-tile import
//! acknowledgements: a dispatch ack only says that the client's Lua handler consumed the metadata.
use std::collections::VecDeque;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::exceptions::PyValueError;

/// Critical world/control answers bypass the window. Keep this in agreement with core.lua's urgent handlers;
/// hello is a transport barrier and must not wait for credits negotiated by that very hello.
#[pyfunction]
pub fn stream_controlled(kind: &str) -> bool {
    !matches!(kind, "hello" | "anchor_ready" | "anchor_error" | "anchor_progress" |
        "terrain_ready" | "terrain_ready_set" | "terrain_refresh" | "terrain_error" | "block_base" |
        "terrain_patch" | "ground_ahead" | "status" | "load_progress" | "where_result" | "geocode_result" |
        "route_result" | "random_place_result" | "runtime_profiler_pong")
}

#[pyclass]
pub struct StreamDispatch {
    window: usize,
    max_bytes: usize,
    max_messages: usize,
    bytes: usize,
    next: u64,
    sent: u64,
    acked: u64,
    queue: VecDeque<(u64, Vec<u8>)>,
}

impl StreamDispatch {
    fn drain(&mut self) -> Vec<Vec<u8>> {
        let mut out = Vec::new();
        while self.sent - self.acked < self.window as u64 {
            let Some((seq, data)) = self.queue.pop_front() else { break; };
            self.bytes -= data.len();
            self.sent = seq;
            out.push(data);
        }
        out
    }

    fn push(&mut self, seq: u64, data: &[u8]) -> Result<Vec<Vec<u8>>, &'static str> {
        if seq != self.next || seq >= (1u64 << 53) {
            return Err("stream sequence is not the next exact Lua integer");
        }
        if self.queue.len() >= self.max_messages || data.len() > self.max_bytes.saturating_sub(self.bytes) {
            return Err("stream dispatch queue limit exceeded");
        }
        self.next += 1;
        self.bytes += data.len();
        self.queue.push_back((seq, data.to_vec()));
        Ok(self.drain())
    }

    fn ack(&mut self, seq: u64) -> Result<Vec<Vec<u8>>, &'static str> {
        if seq > self.sent { return Err("stream ack exceeds the last transmitted sequence"); }
        if seq <= self.acked { return Ok(vec![]); }
        self.acked = seq;
        Ok(self.drain())
    }
}

#[pymethods]
impl StreamDispatch {
    #[new]
    fn new(window: usize, max_bytes: usize, max_messages: usize) -> PyResult<Self> {
        if !(1..=128).contains(&window) || max_bytes == 0 || max_messages == 0 {
            return Err(PyValueError::new_err("invalid stream dispatch limits"));
        }
        Ok(Self { window, max_bytes, max_messages, bytes: 0, next: 1, sent: 0, acked: 0,
            queue: VecDeque::new() })
    }

    #[getter]
    fn next_seq(&self) -> u64 { self.next }
    #[getter]
    fn queued_bytes(&self) -> usize { self.bytes }
    #[getter]
    fn queued_messages(&self) -> usize { self.queue.len() }
    #[getter]
    fn inflight(&self) -> u64 { self.sent - self.acked }
    #[getter]
    fn capacity(&self) -> usize { self.window }

    fn enqueue<'py>(&mut self, py: Python<'py>, seq: u64, data: &[u8]) -> PyResult<Vec<Bound<'py, PyBytes>>> {
        Ok(self.push(seq, data).map_err(PyValueError::new_err)?.iter().map(|b| PyBytes::new(py, b)).collect())
    }
    fn acknowledge<'py>(&mut self, py: Python<'py>, seq: u64) -> PyResult<Vec<Bound<'py, PyBytes>>> {
        Ok(self.ack(seq).map_err(PyValueError::new_err)?.iter().map(|b| PyBytes::new(py, b)).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fifo_and_cumulative_credit() {
        let mut q = StreamDispatch::new(2, 100, 20).unwrap();
        assert_eq!(q.push(1, b"a").unwrap(), vec![b"a".to_vec()]);
        assert_eq!(q.push(2, b"b").unwrap(), vec![b"b".to_vec()]);
        assert!(q.push(3, b"c").unwrap().is_empty());
        assert!(q.push(4, b"d").unwrap().is_empty());
        assert_eq!(q.ack(1).unwrap(), vec![b"c".to_vec()]);
        assert_eq!(q.ack(3).unwrap(), vec![b"d".to_vec()]);
        assert_eq!(q.inflight(), 1);
        assert_eq!(q.queued_bytes(), 0);
        assert!(q.ack(3).unwrap().is_empty());
        assert!(q.ack(1).unwrap().is_empty());
        assert!(q.ack(5).is_err());
        assert_eq!(q.acked, 3);
    }
    #[test]
    fn limits_do_not_corrupt_the_next_sequence() {
        let mut q = StreamDispatch::new(1, 3, 1).unwrap();
        q.push(1, b"a").unwrap();
        q.push(2, b"bbb").unwrap();
        assert!(q.push(3, b"c").is_err());
        assert_eq!(q.next_seq(), 3);
        assert_eq!(q.queued_bytes(), 3);
        assert_eq!(q.ack(1).unwrap(), vec![b"bbb".to_vec()]);
        assert!(q.push(4, b"d").is_err());
        assert!(q.push(3, b"dddd").is_err());
        assert!(q.push(3, b"c").unwrap().is_empty());
        assert_eq!(q.ack(2).unwrap(), vec![b"c".to_vec()]);
    }
    #[test]
    fn terrain_and_ui_never_wait_for_dispatch_credit() {
        for kind in ["hello", "anchor_ready", "terrain_ready", "terrain_refresh", "block_base", "ground_ahead",
            "route_result", "status", "terrain_error", "runtime_profiler_pong"] {
            assert!(!stream_controlled(kind));
        }
        for kind in ["roads_tile", "veg_tile", "far_tile", "far_swap", "roads_drop", "sea", "weather"] {
            assert!(stream_controlled(kind));
        }
    }
}
