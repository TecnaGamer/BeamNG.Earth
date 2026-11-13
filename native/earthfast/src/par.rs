//! Threads for the per-sample kernels (the plan apply's grade_points_binned, the mip levels, the CPU BC1 / BC7
//! encoders): one cap for every kernel of a process, so that several server processes busy at once do not each start
//! a thread per core.
//!
//! The cap is the smallest of:
//! - the CPUs this process may run on: its affinity (cpupin.py pins the server's processes to the server's cores and
//!   widens the set while loading: re-read at most once a second), and on Linux the cgroup's CPU quota;
//! - BNGE_NATIVE_THREADS (earthd sets it for a small machine: a Steam Deck's 4 cores are shared with the game), or
//!   earthfast.set_native_threads(n) (a test's 1 against n);
//! - MAX_THREADS.
//!
//! The kernels split their outputs into parts each computed from its own inputs only, so the numbers are the same bits
//! on any number of threads (tests/test_native_threads.py checks it per kernel).

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use pyo3::prelude::*;

/// Never more than this many threads in one kernel call (the server's processes share the server's cores).
pub const MAX_THREADS: usize = 8;

/// earthfast.set_native_threads's cap (0: none, BNGE_NATIVE_THREADS / the CPU set decide).
static SET_CAP: AtomicUsize = AtomicUsize::new(0);
/// The CPU set's size and BNGE_NATIVE_THREADS as last read (0: not yet), and when (ms since START).
static CPUS: AtomicUsize = AtomicUsize::new(0);
static ENV_CAP: AtomicUsize = AtomicUsize::new(0);
static READ_MS: AtomicU64 = AtomicU64::new(0);
static START: OnceLock<Instant> = OnceLock::new();

const REREAD_MS: u64 = 1000;

#[cfg(windows)]
fn cpu_set() -> usize {
    // (std's available_parallelism counts the machine's processors on Windows, not the process's affinity mask:
    // cpupin.py sets the mask)
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> isize;
        fn GetProcessAffinityMask(process: isize, process_mask: *mut usize, system_mask: *mut usize) -> i32;
    }
    let (mut pm, mut sm) = (0usize, 0usize);
    // SAFETY: GetCurrentProcess's pseudo handle needs no closing; the two out pointers are valid locals
    let ok = unsafe { GetProcessAffinityMask(GetCurrentProcess(), &mut pm, &mut sm) };
    if ok != 0 && pm != 0 {
        return pm.count_ones() as usize;
    }
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

#[cfg(not(windows))]
fn cpu_set() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

fn env_cap() -> usize {
    std::env::var("BNGE_NATIVE_THREADS").ok().and_then(|v| v.trim().parse::<usize>().ok()).unwrap_or(0)
}

/// The threads a kernel call may use now (1..=MAX_THREADS).
pub fn threads() -> usize {
    let set = SET_CAP.load(Ordering::Relaxed);
    let start = START.get_or_init(Instant::now);
    let now = start.elapsed().as_millis() as u64 + 1;
    let at = READ_MS.load(Ordering::Relaxed);
    let mut cpus = CPUS.load(Ordering::Relaxed);
    let mut env = ENV_CAP.load(Ordering::Relaxed);
    if cpus == 0 || now.saturating_sub(at) >= REREAD_MS {
        cpus = cpu_set().max(1);
        env = env_cap();
        CPUS.store(cpus, Ordering::Relaxed);
        ENV_CAP.store(env, Ordering::Relaxed);
        READ_MS.store(now, Ordering::Relaxed);
    }
    let cap = if set > 0 { set } else if env > 0 { env } else { MAX_THREADS };
    cpus.min(cap).clamp(1, MAX_THREADS)
}

/// threads(), but at most one per min_items of the call's items (a thread's start costs ~20-50 µs: small calls stay on
/// the calling thread).
pub fn threads_for(items: usize, min_items: usize) -> usize {
    threads().min(items / min_items.max(1)).max(1)
}

/// f over every part, on up to `threads` threads (the calling thread one of them): the parts are taken from one queue
/// in their order, so parts of uneven cost spread over the threads. Each part is handed to f once.
pub fn run_parts<P: Send, F: Fn(P) + Sync>(parts: Vec<P>, threads: usize, f: F) {
    let threads = threads.min(parts.len());
    if threads <= 1 {
        for p in parts {
            f(p);
        }
        return;
    }
    let queue = Mutex::new(parts.into_iter());
    let work = || loop {
        let next = queue.lock().unwrap_or_else(|e| e.into_inner()).next();
        match next {
            Some(p) => f(p),
            None => break,
        }
    };
    std::thread::scope(|sc| {
        for _ in 1..threads {
            sc.spawn(work);
        }
        work();
    });
}

/// Rows of out (row_len values each) filled by f(row, its values), in bands over up to `threads` threads (each row from
/// its own inputs only: the same numbers as one thread).
pub fn rows<T: Send, F: Fn(usize, &mut [T]) + Sync>(out: &mut [T], row_len: usize, threads: usize, f: F) {
    let rows = if row_len == 0 { 0 } else { out.len() / row_len };
    let threads = threads.min(rows.max(1));
    if threads <= 1 {
        for (y, r) in out.chunks_mut(row_len.max(1)).enumerate() {
            f(y, r);
        }
        return;
    }
    let per = rows.div_ceil(threads);
    let parts: Vec<(usize, &mut [T])> = out.chunks_mut(per * row_len).enumerate().collect();
    run_parts(parts, threads, |(bi, band)| {
        for (k, r) in band.chunks_mut(row_len).enumerate() {
            f(bi * per + k, r);
        }
    });
}

/// The per-sample kernels' thread cap for this process (n >= 1; 0: back to BNGE_NATIVE_THREADS and the CPU set).
/// Returns the threads a large call uses now.
#[pyfunction]
pub fn set_native_threads(n: usize) -> usize {
    SET_CAP.store(n, Ordering::Relaxed);
    threads()
}

/// The threads a large kernel call uses now.
#[pyfunction]
pub fn native_threads() -> usize {
    threads()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_cover_every_row_once() {
        for t in 1..6 {
            let mut v = vec![0u32; 7 * 13];
            rows(&mut v, 13, t, |y, r| {
                for (x, o) in r.iter_mut().enumerate() {
                    *o += (y * 100 + x) as u32 + 1;
                }
            });
            for y in 0..7 {
                for x in 0..13 {
                    assert_eq!(v[y * 13 + x], (y * 100 + x) as u32 + 1);
                }
            }
        }
    }

    #[test]
    fn parts_once_each() {
        let hits: Vec<AtomicUsize> = (0..50).map(|_| AtomicUsize::new(0)).collect();
        run_parts((0..50).collect(), 4, |i: usize| {
            hits[i].fetch_add(1, Ordering::Relaxed);
        });
        assert!(hits.iter().all(|h| h.load(Ordering::Relaxed) == 1));
    }
}
