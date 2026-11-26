//! Thread priority for the live output's threads (best effort: never fatal, nothing changes when the system says no).
//!
//! - Linux: the calling thread's nice value lowered to the first of NICE_STEPS the system allows (setpriority on its
//!   own thread id; RLIMIT_NICE decides: a default desktop allows none, a player in a realtime / audio group or with
//!   an rtkit-style limit gets some). Never a realtime policy: a thread stuck in a loop under SCHED_FIFO could take a
//!   whole core from the game.
//! - Windows: THREAD_PRIORITY_HIGHEST for the worker (cpal already runs its WASAPI callback thread at
//!   TIME_CRITICAL); Win32 only (kernel32), no WinRT.
//! - Elsewhere: nothing.
//!
//! No allocation and no lock: safe to call from the audio callback.

/// the nice values tried, highest priority first (Linux)
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub const NICE_STEPS: [i32; 3] = [-11, -6, -2];
/// "not raised"
pub const NOT_RAISED: i32 = i32::MIN;

#[cfg(target_os = "linux")]
mod imp {
    extern "C" {
        fn setpriority(which: i32, who: u32, prio: i32) -> i32;
        fn getpriority(which: i32, who: u32) -> i32;
        fn gettid() -> i32;
    }
    const PRIO_PROCESS: i32 = 0;

    pub fn raise() -> i32 {
        // SAFETY: plain libc calls on this thread's own id, no pointers
        let tid = unsafe { gettid() };
        if tid <= 0 {
            return super::NOT_RAISED;
        }
        for &n in super::NICE_STEPS.iter() {
            if unsafe { setpriority(PRIO_PROCESS, tid as u32, n) } == 0 {
                return n;
            }
        }
        super::NOT_RAISED
    }

    pub fn lower() -> i32 {
        // SAFETY: as raise
        let tid = unsafe { gettid() };
        if tid <= 0 {
            return super::NOT_RAISED;
        }
        // (already lower, a server started at nice 19: left there; going back up is not allowed anyway)
        let now = unsafe { getpriority(PRIO_PROCESS, tid as u32) };
        if now >= super::LOW_NICE {
            return now;
        }
        if unsafe { setpriority(PRIO_PROCESS, tid as u32, super::LOW_NICE) } == 0 {
            super::LOW_NICE
        } else {
            super::NOT_RAISED
        }
    }
}

#[cfg(windows)]
mod imp {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThread() -> *mut c_void;
        fn SetThreadPriority(thread: *mut c_void, priority: i32) -> i32;
    }
    const THREAD_PRIORITY_HIGHEST: i32 = 2;
    const THREAD_PRIORITY_BELOW_NORMAL: i32 = -1;

    pub fn lower() -> i32 {
        // SAFETY: as raise
        if unsafe { SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL) } != 0 {
            THREAD_PRIORITY_BELOW_NORMAL
        } else {
            super::NOT_RAISED
        }
    }

    pub fn raise() -> i32 {
        // SAFETY: the pseudo-handle of the calling thread; SetThreadPriority only reads it
        if unsafe { SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST) } != 0 {
            THREAD_PRIORITY_HIGHEST
        } else {
            super::NOT_RAISED
        }
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
mod imp {
    pub fn raise() -> i32 {
        super::NOT_RAISED
    }
    pub fn lower() -> i32 {
        super::NOT_RAISED
    }
}

/// Raise the calling thread's priority: the value it got (Linux: the nice value; Windows: the thread priority), or
/// NOT_RAISED.
pub fn raise_current_thread() -> i32 {
    imp::raise()
}

/// The nice value for offline-style work in the live synth (method B renders, Linux), and Windows' below-normal.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub const LOW_NICE: i32 = 10;

/// Lower the calling thread's priority (the render thread: seconds of work per strike that must never compete with the
/// game): Linux nice LOW_NICE (always allowed: a thread may lower itself), Windows THREAD_PRIORITY_BELOW_NORMAL. Returns
/// what it got, or NOT_RAISED when it could not.
pub fn lower_current_thread() -> i32 {
    imp::lower()
}

/// For the log: "nice -11", "highest", "normal (not allowed)".
pub fn describe(v: i32) -> String {
    if v == NOT_RAISED {
        "normal (not allowed)".into()
    } else if cfg!(windows) {
        if v < 0 { "below normal".into() } else { "highest".into() }
    } else {
        format!("nice {v}")
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn raising_never_fails_and_says_what_it_got() {
        // (on a thread of its own: the test runner's thread keeps its priority)
        let v = std::thread::spawn(super::raise_current_thread).join().unwrap();
        assert!(v == super::NOT_RAISED || super::NICE_STEPS.contains(&v) || v == 2, "{v}");
        assert!(!super::describe(v).is_empty());
        assert_eq!(super::describe(super::NOT_RAISED), "normal (not allowed)");
    }

    #[test]
    fn the_render_thread_goes_low() {
        let v = std::thread::spawn(super::lower_current_thread).join().unwrap();
        if cfg!(target_os = "linux") {
            assert!(v >= super::LOW_NICE && v <= 19, "a thread may always lower itself: {v}");
        } else if cfg!(windows) {
            assert_eq!(v, -1);
        }
    }
}
