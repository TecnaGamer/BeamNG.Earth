//! BeamNG.Earth patch (native/earthfast/Cargo.toml, [patch.crates-io]): no thread runs Python on top of Rust frames
//! once the interpreter finalizes; the threads that would attach then park instead.
//!
//! CPython before 3.14 ends a non-main thread that takes the GIL during finalization with `pthread_exit()`
//! (`take_gil` -> `PyThread_exit_thread`). On Linux that is a forced unwind through every frame of the thread, and
//! when the thread is in an extension's Rust code (a kernel coming back from `Python::detach`, or Python code a kernel
//! called back, like the paced analyses' `pace` sleeping or a DEM fetch) it unwinds through Rust frames: undefined
//! behaviour that ran drop glue without the GIL (`Py_DECREF`s racing the main thread's final garbage collection) and
//! crashed or hung the process at exit (2026-11-24: earthd's tests under the packaged app's Python 3.12, the
//! de-shadow worker in its horizon kernel at exit). CPython 3.14 hangs such threads instead (gh-87135,
//! `PyThread_hang_thread`); this does the same for the threads attaching through PyO3, on every version.
//!
//! `begin` is called once, attached, by the thread that will finalize (earthfast's atexit hook, server/fast.py). From
//! then on every other thread that would attach through PyO3 (the end of `detach`, `Python::attach` from a detached
//! or a Rust thread) parks forever with the GIL released. `begin` then releases the GIL until every other thread with
//! PyO3 frames on its stack is detached (it parks when it comes back) or gone from PyO3: the ones running Python or
//! Rust with the GIL finish, the ones on their way in are let in first. The checks are Dekker pairs, both sides SeqCst
//! (either the thread sees the flag and parks, or `begin` sees its count and waits for it). The thread calling `begin`
//! stays exempt: atexit hooks after it and the final collection may call kernels on it.
use crate::ffi;
use core::cell::Cell;
use core::sync::atomic::{AtomicBool, AtomicIsize, Ordering::SeqCst};
use std::time::{Duration, Instant};

static EXITING: AtomicBool = AtomicBool::new(false);
/// Threads with PyO3 frames on their stack: attach depth > 0 (a trampoline's or an attach's guard), also while
/// detached inside one.
static INSIDE: AtomicIsize = AtomicIsize::new(0);
/// Of those, the ones detached in `Python::detach` (they park when they come back, once exiting).
static AWAY: AtomicIsize = AtomicIsize::new(0);
/// Threads between their exit check and their attach count (in `PyGILState_Ensure`).
static ENTERING: AtomicIsize = AtomicIsize::new(0);

std::thread_local! {
    static EXEMPT: Cell<bool> = const { Cell::new(false) };
}

fn exempt() -> bool {
    EXEMPT.try_with(|e| e.get()).unwrap_or(false)
}

/// The interpreter is about to finalize: park every other thread that attaches through PyO3 from now on, and wait (at
/// most `wait`, the GIL released) until no other thread runs on top of PyO3 frames. True when none was left. Call it
/// attached, on the thread that runs the finalization (the main thread's atexit). Idempotent.
///
/// # Safety
/// The calling thread must be attached to the interpreter (it detaches and re-attaches while it waits).
pub unsafe fn begin(wait: Duration) -> bool {
    EXEMPT.with(|e| e.set(true));
    EXITING.store(true, SeqCst);
    let own = isize::from(crate::internal::state::thread_is_attached());
    let until = Instant::now() + wait;
    loop {
        let entering = ENTERING.load(SeqCst);
        let away = AWAY.load(SeqCst);
        let inside = INSIDE.load(SeqCst);
        if inside - away - own + entering <= 0 {
            return true;
        }
        if Instant::now() >= until {
            return false;
        }
        // (the raw pair, not detach: the GIL handed to the other threads, this thread back after them)
        unsafe {
            let t = ffi::PyEval_SaveThread();
            std::thread::sleep(Duration::from_millis(1));
            ffi::PyEval_RestoreThread(t);
        }
    }
}

/// `begin` has been called (the interpreter is shutting down).
pub fn exiting() -> bool {
    EXITING.load(SeqCst)
}

/// The threads `begin` would still wait for (other threads on top of PyO3 frames), for tests: inside, away, entering.
pub fn counts() -> (isize, isize, isize) {
    (INSIDE.load(SeqCst), AWAY.load(SeqCst), ENTERING.load(SeqCst))
}

/// This thread's attach depth went 0 -> 1.
#[inline(always)]
pub(crate) fn entered() {
    INSIDE.fetch_add(1, SeqCst);
}

/// This thread's attach depth went 1 -> 0.
#[inline(always)]
pub(crate) fn left() {
    INSIDE.fetch_sub(1, SeqCst);
}

/// `Python::detach` is about to release the GIL (SuspendAttach::new, attached).
#[inline(always)]
pub(crate) fn detaching() {
    AWAY.fetch_add(1, SeqCst);
}

/// The end of `Python::detach`: `restore` (PyEval_RestoreThread) unless exiting, else park forever.
#[inline]
pub(crate) fn reattach(restore: impl FnOnce()) {
    AWAY.fetch_sub(1, SeqCst);
    if !exempt() && EXITING.load(SeqCst) {
        AWAY.fetch_add(1, SeqCst);
        hang();
    }
    restore();
}

/// `Python::attach` from a thread not attached (PyGILState_Ensure, then the attach count): `attach` unless exiting,
/// else park forever.
#[inline]
pub(crate) fn attach<T>(attach: impl FnOnce() -> T) -> T {
    if exempt() {
        return attach();
    }
    ENTERING.fetch_add(1, SeqCst);
    if EXITING.load(SeqCst) {
        ENTERING.fetch_sub(1, SeqCst);
        hang();
    }
    let r = attach();
    ENTERING.fetch_sub(1, SeqCst);
    r
}

#[cold]
fn hang() -> ! {
    loop {
        std::thread::park();
    }
}
