//! The deadline scheduler (architecture v2, phase V1: ~/bnge-tests/archv2/DESIGN.md sections 3.2 and 3.3), behind
//! BNE_V2_HOST=on (server/v2host.py). Off, nothing here runs and the server's executors are today's.
//!
//! Every piece of work is a job: a lane (a kind of worker with a number of slots: the server's own CPU work, or a set
//! of worker processes), a class (P0 safety ... P5 late), a deadline, the place (anchor) it is for, and optionally a
//! single-flight key. Python functions run as jobs on threads this module owns (PyJob, as host/pyjob.rs), each job
//! under the GIL only while it runs Python.
//!
//! Rules (DESIGN 3.2):
//! - Strict class order, then earliest deadline first. A P0 job may take one slot beyond its lane's cap (the urgent
//!   slot): collision never waits behind anything. P4 (speculative) only takes a slot that leaves another free; P5
//!   (late) only an idle lane. A P2+ job waiting longer than STARVE_S takes the next slot when no P2+ job of its lane
//!   runs (as the road processes' "low" jobs were never starved before).
//! - A job that waits for another job never holds a slot: `block()` gives its slot back while it waits (`wait()`,
//!   pools.result_running under the switch), and the job it waits for inherits its class and deadline (`boost`).
//! - Cancel by place: `cancel_place(aid)` (a teleport left the anchor) drops its queued jobs at once (their futures
//!   raise pools.Abandoned) and flags its running ones (`sched_cancelled()`, read by xproc's cancel checks; a job
//!   waiting on a worker process has that process restarted when it does not give up within its grace: pools.SlotPool).
//!   New jobs of a place left are refused until `uncancel_place` (the teleport did not happen).
//! - Adaptive CPU (DESIGN 3.3): the adaptive lanes' slots in use follow a target between `min` and `max` threads,
//!   down at once when the game's frame time rises above its own baseline (the pose's frameMs / frameMaxMs) or the
//!   machine's CPU pressure (Linux PSI) is high, up one at a time while both have room. Threads that run P0 / P1 work
//!   keep the process's priority; the others run below it (Linux nice +BACKGROUND_NICE per thread, Windows
//!   THREAD_PRIORITY_BELOW_NORMAL; not SCHED_IDLE while the jobs hold Python's GIL: an idle-class holder that the OS
//!   never runs on a busy PC would stall the event loop waiting for the GIL).
//! - Lateness per class (finish - deadline) and queue waits are kept for the stats line.
//!
//! Scheduling order never changes what a job makes: outputs stay byte-identical (the jobs are the same functions).
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

pub const CLASSES: usize = 6;
/// each class's default deadline from submission (s): P0 collision ... P5 late (DESIGN 3.2's margins, without the
/// arrival time the path prediction will give: V1 schedules by submission + budget)
pub const DUE_S: [f64; CLASSES] = [2.0, 4.0, 8.0, 15.0, 30.0, 120.0];
/// a P2+ job waiting this long (s) takes the next slot when none of its lane's P2+ jobs runs
pub const STARVE_S: f64 = 15.0;
/// per-class samples kept between two stats reads
const SAMPLES_MAX: usize = 20_000;
/// threads at most (running, blocked and idle)
const THREADS_MAX: usize = 256;
/// idle threads kept per tier; more end after IDLE_S
const KEEP_IDLE: usize = 2;
const IDLE_S: f64 = 30.0;
/// the background tier's niceness above the process's (Linux)
const BACKGROUND_NICE: i32 = 5;

/// A deadline (s) as a key that sorts as the number (NaN last).
fn okey(d: f64) -> u64 {
    let d = if d.is_nan() { f64::INFINITY } else { d };
    let b = d.to_bits();
    if b >> 63 == 1 { !b } else { b | (1 << 63) }
}

#[derive(Clone, Debug)]
pub struct LaneSpec {
    pub name: String,
    pub cap: usize,
    /// its slots count against the adaptive CPU target
    pub adaptive: bool,
    /// a P0 job may take one slot beyond cap (CPU lanes; a lane of worker processes has no spare process)
    pub urgent: bool,
}

struct LaneSt {
    spec: LaneSpec,
    /// slots in use (running and not blocked), not counting the urgent one
    busy: usize,
    /// P0 jobs in the urgent slot (0 or 1, more only after unblocks)
    urgent: usize,
    /// P2+ jobs running (blocked or not): the starvation rule
    low: usize,
    /// (class, deadline key, id)
    ready: BTreeSet<(u8, u64, u64)>,
}

pub struct Queued<P, H> {
    lane: usize,
    class: u8,
    deadline: f64,
    place: Option<i64>,
    key: Option<String>,
    queued: f64,
    payload: P,
    handle: H,
}

pub struct Run<H> {
    lane: usize,
    class: u8,
    deadline: f64,
    place: Option<i64>,
    key: Option<String>,
    queued: f64,
    started: f64,
    cancel: Arc<AtomicBool>,
    blocked: u32,
    urgent: bool,
    handle: H,
}

#[allow(dead_code)]
pub struct Picked<P> {
    pub id: u64,
    pub lane: usize,
    pub class: u8,
    pub deadline: f64,
    pub payload: P,
    pub cancel: Arc<AtomicBool>,
}

pub enum Submitted<P, H> {
    Queued(u64),
    /// the same key queued or running already: that job (boosted); the new payload and handle given back
    Attached(u64, P, H),
    /// its place was left (cancel_place): given back
    Refused(P, H),
}

#[derive(Default, Clone)]
pub struct ClassStats {
    pub done: u64,
    pub failed: u64,
    pub dropped: u64,
    pub refused: u64,
    pub missed: u64,
    /// finish - deadline (s)
    pub late: Vec<f32>,
    /// start - submission (s)
    pub wait: Vec<f32>,
}

impl ClassStats {
    fn push(v: &mut Vec<f32>, x: f64) {
        if v.len() < SAMPLES_MAX {
            v.push(x as f32);
        }
    }
}

/// nearest-rank percentile of xs (sorted in place), or None
pub fn pct(xs: &mut [f32], p: f64) -> Option<f64> {
    if xs.is_empty() {
        return None;
    }
    xs.sort_by(|a, b| a.total_cmp(b));
    let n = xs.len();
    let r = ((p / 100.0) * n as f64).ceil() as usize;
    Some(xs[r.clamp(1, n) - 1] as f64)
}

/// The scheduler's state (no Python: the unit tests drive it directly).
pub struct Core<P, H> {
    lanes: Vec<LaneSt>,
    queued: BTreeMap<u64, Queued<P, H>>,
    running: BTreeMap<u64, Run<H>>,
    keys: BTreeMap<String, u64>,
    left: BTreeSet<i64>,
    next: u64,
    /// the adaptive lanes' slots in use at most
    target: usize,
    adaptive_busy: usize,
    pub stats: [ClassStats; CLASSES],
}

impl<P, H> Core<P, H> {
    pub fn new(target: usize) -> Self {
        Core { lanes: Vec::new(), queued: BTreeMap::new(), running: BTreeMap::new(), keys: BTreeMap::new(),
               left: BTreeSet::new(), next: 1, target: target.max(1), adaptive_busy: 0,
               stats: Default::default() }
    }

    pub fn add_lane(&mut self, spec: LaneSpec) -> usize {
        self.lanes.push(LaneSt { spec: LaneSpec { cap: spec.cap.max(1), ..spec }, busy: 0, urgent: 0, low: 0,
                                 ready: BTreeSet::new() });
        self.lanes.len() - 1
    }

    pub fn lanes(&self) -> usize {
        self.lanes.len()
    }

    pub fn set_target(&mut self, n: usize) {
        self.target = n.max(1);
    }

    pub fn target(&self) -> usize {
        self.target
    }

    pub fn queued_len(&self) -> usize {
        self.queued.len()
    }

    pub fn running_len(&self) -> usize {
        self.running.len()
    }

    pub fn is_left(&self, place: i64) -> bool {
        self.left.contains(&place)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn submit(&mut self, lane: usize, class: u8, deadline: f64, place: Option<i64>, key: Option<String>,
                  payload: P, handle: H, now: f64) -> Submitted<P, H> {
        let class = class.min(CLASSES as u8 - 1);
        if let Some(p) = place {
            if self.left.contains(&p) {
                self.stats[class as usize].refused += 1;
                return Submitted::Refused(payload, handle);
            }
        }
        if let Some(k) = &key {
            if let Some(&id) = self.keys.get(k) {
                self.boost(id, class, deadline);
                return Submitted::Attached(id, payload, handle);
            }
        }
        let id = self.next;
        self.next += 1;
        let lane = lane.min(self.lanes.len().saturating_sub(1));
        self.lanes[lane].ready.insert((class, okey(deadline), id));
        if let Some(k) = &key {
            self.keys.insert(k.clone(), id);
        }
        self.queued.insert(id, Queued { lane, class, deadline, place, key, queued: now, payload, handle });
        Submitted::Queued(id)
    }

    /// The handle of a queued or running job.
    pub fn handle(&self, id: u64) -> Option<&H> {
        self.queued.get(&id).map(|q| &q.handle).or_else(|| self.running.get(&id).map(|r| &r.handle))
    }

    fn room(&self, l: usize) -> bool {
        let ln = &self.lanes[l];
        ln.busy < ln.spec.cap && (!ln.spec.adaptive || self.adaptive_busy < self.target)
    }

    /// The job lane l would start now for a thread of tier (0: any class; 1: P2+ only), and whether it takes the
    /// urgent slot.
    fn candidate(&self, l: usize, tier: u8, now: f64) -> Option<((u8, u64, u64), bool)> {
        let ln = &self.lanes[l];
        let room = self.room(l);
        let first = *ln.ready.iter().next()?;
        // (the starvation rule: an old P2+ job before the P0 / P1 ones when no P2+ job of the lane runs)
        if room && first.0 < 2 && ln.low == 0 {
            if let Some(&low) = ln.ready.range((2u8, 0u64, 0u64)..).next() {
                if self.queued.get(&low.2).is_some_and(|q| now - q.queued >= STARVE_S) {
                    return Some((low, false));
                }
            }
        }
        if tier == 1 && first.0 < 2 {
            return None;        // (a P0 / P1 job first: a normal-priority thread's, never passed by a P2+ one)
        }
        let c = first.0;
        let ok_room = match c {
            4 => room && (ln.busy + 1 < ln.spec.cap || ln.busy == 0),
            5 => room && ln.busy == 0,
            _ => room,
        };
        if ok_room {
            return Some((first, false));
        }
        if c == 0 && ln.urgent == 0 && ln.spec.urgent {
            return Some((first, true));
        }
        None
    }

    /// Is there a job a thread of tier could start now? Its class.
    pub fn peek(&self, tier: u8, now: f64) -> Option<u8> {
        (0..self.lanes.len()).filter_map(|l| self.candidate(l, tier, now)).map(|(k, _)| k.0).min()
    }

    pub fn pick(&mut self, tier: u8, now: f64) -> Option<Picked<P>> {
        let mut best: Option<(usize, (u8, u64, u64), bool)> = None;
        for l in 0..self.lanes.len() {
            if let Some((k, urgent)) = self.candidate(l, tier, now) {
                if best.as_ref().is_none_or(|b| (k.0, k.1) < (b.1 .0, b.1 .1)) {
                    best = Some((l, k, urgent));
                }
            }
        }
        let (l, k, urgent) = best?;
        self.lanes[l].ready.remove(&k);
        let q = self.queued.remove(&k.2)?;
        let ln = &mut self.lanes[l];
        if urgent {
            ln.urgent += 1;
        } else {
            ln.busy += 1;
            if ln.spec.adaptive {
                self.adaptive_busy += 1;
            }
        }
        if q.class >= 2 {
            self.lanes[l].low += 1;
        }
        let cancel = Arc::new(AtomicBool::new(q.place.is_some_and(|p| self.left.contains(&p))));
        ClassStats::push(&mut self.stats[q.class as usize].wait, now - q.queued);
        self.running.insert(k.2, Run { lane: l, class: q.class, deadline: q.deadline, place: q.place, key: q.key,
                                       queued: q.queued, started: now, cancel: cancel.clone(), blocked: 0, urgent,
                                       handle: q.handle });
        Some(Picked { id: k.2, lane: l, class: q.class, deadline: q.deadline, payload: q.payload, cancel })
    }

    fn release(&mut self, l: usize, urgent: bool) {
        let adaptive = self.lanes[l].spec.adaptive;
        let ln = &mut self.lanes[l];
        if urgent {
            ln.urgent = ln.urgent.saturating_sub(1);
        } else {
            ln.busy = ln.busy.saturating_sub(1);
            if adaptive {
                self.adaptive_busy = self.adaptive_busy.saturating_sub(1);
            }
        }
    }

    fn acquire(&mut self, l: usize, urgent: bool) {
        let adaptive = self.lanes[l].spec.adaptive;
        let ln = &mut self.lanes[l];
        if urgent {
            ln.urgent += 1;
        } else {
            ln.busy += 1;
            if adaptive {
                self.adaptive_busy += 1;
            }
        }
    }

    /// A running job finished (ok: without an exception): its slot back, its stats; its handle.
    pub fn done(&mut self, id: u64, ok: bool, now: f64) -> Option<H> {
        let r = self.running.remove(&id)?;
        if r.blocked == 0 {
            self.release(r.lane, r.urgent);
        }
        if r.class >= 2 {
            self.lanes[r.lane].low = self.lanes[r.lane].low.saturating_sub(1);
        }
        if let Some(k) = &r.key {
            if self.keys.get(k) == Some(&id) {
                self.keys.remove(k);
            }
        }
        let st = &mut self.stats[r.class as usize];
        if ok {
            st.done += 1;
            let late = now - r.deadline;
            if late > 0.0 {
                st.missed += 1;
            }
            ClassStats::push(&mut st.late, late);
        } else {
            st.failed += 1;
        }
        let _ = (r.queued, r.started);
        Some(r.handle)
    }

    /// A running job waits for another: its slot given back meanwhile (nested waits count once).
    pub fn block(&mut self, id: u64) -> bool {
        let Some(r) = self.running.get_mut(&id) else { return false };
        r.blocked += 1;
        if r.blocked == 1 {
            let (l, u) = (r.lane, r.urgent);
            self.release(l, u);
        }
        true
    }

    pub fn unblock(&mut self, id: u64) {
        let Some(r) = self.running.get_mut(&id) else { return };
        if r.blocked == 0 {
            return;
        }
        r.blocked -= 1;
        if r.blocked == 0 {
            let (l, u) = (r.lane, r.urgent);
            self.acquire(l, u);
        }
    }

    /// The job id inherits class and deadline (a job waiting for it is more urgent).
    pub fn boost(&mut self, id: u64, class: u8, deadline: f64) {
        if let Some(q) = self.queued.get_mut(&id) {
            let (nc, nd) = (q.class.min(class), q.deadline.min(deadline));
            if (nc, okey(nd)) != (q.class, okey(q.deadline)) {
                let ready = &mut self.lanes[q.lane].ready;
                ready.remove(&(q.class, okey(q.deadline), id));
                q.class = nc;
                q.deadline = nd;
                ready.insert((nc, okey(nd), id));
            }
        } else if let Some(r) = self.running.get_mut(&id) {
            r.deadline = r.deadline.min(deadline);
            if class < r.class {
                if r.class >= 2 && class < 2 {
                    self.lanes[r.lane].low = self.lanes[r.lane].low.saturating_sub(1);
                }
                r.class = class;
            }
        }
    }

    /// A teleport left place: its queued jobs dropped (given back), its running ones flagged; new ones refused.
    pub fn cancel_place(&mut self, place: i64) -> (Vec<(P, H)>, usize) {
        self.left.insert(place);
        let ids: Vec<u64> = self.queued.iter().filter(|(_, q)| q.place == Some(place)).map(|(&id, _)| id).collect();
        let mut out = Vec::with_capacity(ids.len());
        for id in ids {
            if let Some(q) = self.queued.remove(&id) {
                self.lanes[q.lane].ready.remove(&(q.class, okey(q.deadline), id));
                if let Some(k) = &q.key {
                    if self.keys.get(k) == Some(&id) {
                        self.keys.remove(k);
                    }
                }
                self.stats[q.class as usize].dropped += 1;
                out.push((q.payload, q.handle));
            }
        }
        let mut flagged = 0;
        for r in self.running.values() {
            if r.place == Some(place) {
                r.cancel.store(true, Ordering::SeqCst);
                flagged += 1;
            }
        }
        (out, flagged)
    }

    /// The teleport did not happen: place's work wanted again (its running jobs' flags cleared).
    pub fn uncancel_place(&mut self, place: i64) {
        self.left.remove(&place);
        for r in self.running.values() {
            if r.place == Some(place) {
                r.cancel.store(false, Ordering::SeqCst);
            }
        }
    }

    /// Jobs of lane l queued or running (blocked ones too).
    pub fn lane_live(&self, l: usize) -> usize {
        self.lanes.get(l).map_or(0, |ln| ln.ready.len()) + self.running.values().filter(|r| r.lane == l).count()
    }

    /// Lane l's queued jobs taken out (shutdown(cancel_futures)): their handles.
    pub fn drain_lane(&mut self, l: usize) -> Vec<(P, H)> {
        let Some(ln) = self.lanes.get_mut(l) else { return Vec::new() };
        let keys: Vec<(u8, u64, u64)> = std::mem::take(&mut ln.ready).into_iter().collect();
        let mut out = Vec::with_capacity(keys.len());
        for k in keys {
            if let Some(q) = self.queued.remove(&k.2) {
                if let Some(key) = &q.key {
                    if self.keys.get(key) == Some(&k.2) {
                        self.keys.remove(key);
                    }
                }
                out.push((q.payload, q.handle));
            }
        }
        out
    }

    /// Every place wanted again (a new server's anchors: their ids start again at 1).
    pub fn reset_places(&mut self) {
        self.left.clear();
    }

    pub fn class_deadline(&self, id: u64) -> Option<(u8, f64)> {
        self.running.get(&id).map(|r| (r.class, r.deadline)).or_else(|| self.queued.get(&id).map(|q| (q.class, q.deadline)))
    }

    pub fn lane_name(&self, l: usize) -> &str {
        &self.lanes[l].spec.name
    }

    /// (busy, urgent, queued, cap) of lane l
    pub fn lane_load(&self, l: usize) -> (usize, usize, usize, usize) {
        let ln = &self.lanes[l];
        (ln.busy, ln.urgent, ln.ready.len(), ln.spec.cap)
    }
}

/// The adaptive CPU target (DESIGN 3.3).
#[derive(Clone, Debug)]
pub struct Adapt {
    pub min: usize,
    pub max: usize,
    pub cur: usize,
    /// the game's own frame time when nothing of ours weighs on it: a near-minimum average of frameMs
    pub base_ms: Option<f64>,
    hot: u32,
    last_change: f64,
    pub psi: f64,
    pub hold: bool,
    pub fixed: bool,
}

/// frame time over baseline * HOT_X + HOT_ADD_MS (ms) twice running: one thread less (VERY_HOT: half)
pub const HOT_X: f64 = 1.20;
pub const HOT_ADD_MS: f64 = 1.5;
pub const VERY_HOT_X: f64 = 1.5;
pub const VERY_HOT_ADD_MS: f64 = 4.0;
/// a frame this much over the baseline (x, and at least +ms) counts as hot by itself
pub const SPIKE_X: f64 = 3.0;
pub const SPIKE_ADD_MS: f64 = 50.0;
/// s between two steps up
pub const UP_S: f64 = 5.0;
/// s between two steps down for CPU pressure alone
pub const PSI_DOWN_S: f64 = 2.0;
/// Linux PSI cpu "some" avg10 (%): over HIGH a step down, under LOW a step up allowed
pub const PSI_HIGH: f64 = 60.0;
pub const PSI_LOW: f64 = 25.0;

impl Adapt {
    pub fn new(min: usize, max: usize) -> Self {
        let min = min.max(1);
        let max = max.max(min);
        Adapt { min, max, cur: max, base_ms: None, hot: 0, last_change: f64::NEG_INFINITY, psi: 0.0, hold: false,
                fixed: false }
    }

    /// A pose's frame times (mean and max over its window, ms); hold: the player waits for us (the edge guard
    /// holds the car, a teleport loads), the game's smoothness matters less than our speed. -> the target.
    pub fn note_frame(&mut self, mean_ms: f64, max_ms: f64, hold: bool, now: f64) -> usize {
        self.hold = hold;
        if self.fixed {
            return self.cur;
        }
        if hold {
            self.hot = 0;
            if self.cur < self.max {
                self.cur = self.max;
                self.last_change = now;
            }
            return self.cur;
        }
        if !(mean_ms.is_finite() && mean_ms > 0.0) {
            return self.step_up(now);
        }
        let base = match self.base_ms {
            None => mean_ms,
            Some(b) if mean_ms < b => b + 0.3 * (mean_ms - b),
            Some(b) => b + 0.01 * (mean_ms - b),
        };
        self.base_ms = Some(base);
        let max_ms = if max_ms.is_finite() { max_ms } else { mean_ms };
        let very = mean_ms > base * VERY_HOT_X + VERY_HOT_ADD_MS;
        let hot = mean_ms > base * HOT_X + HOT_ADD_MS || max_ms > (base * SPIKE_X).max(base + SPIKE_ADD_MS);
        if hot {
            self.hot += 1;
        } else {
            self.hot = 0;
        }
        if self.hot >= 2 || very {
            self.hot = 0;
            let n = if very { (self.cur / 2).max(self.min) } else { self.cur.saturating_sub(1).max(self.min) };
            if n != self.cur {
                self.cur = n;
            }
            self.last_change = now;
            return self.cur;
        }
        if !hot {
            return self.step_up(now);
        }
        self.cur
    }

    fn step_up(&mut self, now: f64) -> usize {
        if self.cur < self.max && now - self.last_change >= UP_S && self.psi < PSI_LOW {
            self.cur += 1;
            self.last_change = now;
        }
        self.cur
    }

    /// The machine's CPU pressure (Linux /proc/pressure/cpu "some avg10", %). -> the target.
    pub fn note_psi(&mut self, avg10: f64, now: f64) -> usize {
        self.psi = if avg10.is_finite() { avg10 } else { 0.0 };
        if self.fixed || self.hold {
            return self.cur;
        }
        if self.psi > PSI_HIGH && now - self.last_change >= PSI_DOWN_S && self.cur > self.min {
            self.cur -= 1;
            self.last_change = now;
        }
        self.cur
    }
}

/// Linux's CPU pressure, "some avg10" (%), or None (no PSI: another system, an old kernel, containers).
fn read_psi() -> Option<f64> {
    #[cfg(target_os = "linux")]
    {
        let s = std::fs::read_to_string("/proc/pressure/cpu").ok()?;
        let line = s.lines().find(|l| l.starts_with("some "))?;
        let v = line.split_whitespace().find_map(|w| w.strip_prefix("avg10="))?;
        return v.parse().ok();
    }
    #[allow(unreachable_code)]
    None
}

// ---------------------------------------------------------------------------------------------------------------------
// Threads and Python

#[cfg(target_os = "linux")]
fn lower_thread() {
    extern "C" {
        fn gettid() -> i32;
        fn getpriority(which: i32, who: u32) -> i32;
        fn setpriority(which: i32, who: u32, prio: i32) -> i32;
    }
    const PRIO_PROCESS: i32 = 0;
    // SAFETY: plain system calls on this thread's own id
    unsafe {
        let tid = gettid() as u32;
        let now = getpriority(PRIO_PROCESS, tid);
        setpriority(PRIO_PROCESS, tid, (now + BACKGROUND_NICE).min(19));
    }
}

#[cfg(windows)]
fn lower_thread() {
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThread() -> isize;
        fn SetThreadPriority(thread: isize, priority: i32) -> i32;
    }
    const THREAD_PRIORITY_BELOW_NORMAL: i32 = -1;
    // SAFETY: GetCurrentThread's pseudo handle needs no closing
    unsafe {
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
    }
}

#[cfg(not(any(target_os = "linux", windows)))]
fn lower_thread() {}

struct PyCall {
    f: Py<PyAny>,
    args: Py<PyTuple>,
}

struct State {
    core: Core<PyCall, Py<PyAny>>,
    adapt: Adapt,
    idle: [usize; 2],
    threads: [usize; 2],
    stop: bool,
}

struct Shared {
    st: Mutex<State>,
    cv: [Condvar; 2],
    name: String,
    clock: Instant,
    /// pools.Abandoned: what a dropped or refused job's future raises
    abandoned: Py<PyAny>,
    /// the controller thread's wake-ups
    ctl_cv: Condvar,
    seq: AtomicU64,
}

impl Shared {
    fn now(&self) -> f64 {
        self.clock.elapsed().as_secs_f64()
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.st.lock().unwrap_or_else(|e| e.into_inner())
    }
}

struct Cur {
    sh: Weak<Shared>,
    id: u64,
    cancel: Arc<AtomicBool>,
}

thread_local! {
    static CUR: RefCell<Option<Cur>> = const { RefCell::new(None) };
}

/// This thread's job of sh (None: not a job thread of it).
fn current(sh: &Arc<Shared>) -> Option<u64> {
    CUR.with(|c| c.borrow().as_ref().filter(|c| c.sh.as_ptr() == Arc::as_ptr(sh)).map(|c| c.id))
}

/// Is the job this thread runs (of any scheduler) cancelled (its place left)? False off a job thread.
#[pyfunction]
pub fn sched_cancelled() -> bool {
    CUR.with(|c| c.borrow().as_ref().is_some_and(|c| c.cancel.load(Ordering::SeqCst)))
}

/// Wake an idle thread for a job that can start, or start one.
fn kick(sh: &Arc<Shared>) {
    let (spawn, tier) = {
        let mut st = sh.state();
        if st.stop {
            return;
        }
        let now = sh.now();
        let Some(c) = st.core.peek(0, now) else { return };
        if c >= 2 && st.idle[1] > 0 && st.core.peek(1, now).is_some() {
            sh.cv[1].notify_one();
            return;
        }
        if st.idle[0] > 0 {
            sh.cv[0].notify_one();
            return;
        }
        let tier = if c >= 2 { 1 } else { 0 };
        if st.threads[0] + st.threads[1] >= THREADS_MAX {
            return;
        }
        st.threads[tier] += 1;
        (true, tier)
    };
    if spawn {
        let sh2 = sh.clone();
        let n = sh.seq.fetch_add(1, Ordering::Relaxed);
        let name = format!("{}{}_{}", sh.name, if tier == 1 { "-bg" } else { "" }, n);
        if std::thread::Builder::new().name(name).spawn(move || work(sh2, tier as u8)).is_err() {
            sh.state().threads[tier] -= 1;
        }
    }
}

fn work(sh: Arc<Shared>, tier: u8) {
    if tier == 1 {
        lower_thread();
    }
    let t = tier as usize;
    loop {
        let picked = {
            let mut st = sh.state();
            let mut idle_since = sh.now();
            loop {
                if st.stop {
                    st.threads[t] -= 1;
                    return;
                }
                let now = sh.now();
                if let Some(p) = st.core.pick(tier, now) {
                    break p;
                }
                if now - idle_since >= IDLE_S && st.idle[t] >= KEEP_IDLE {
                    st.threads[t] -= 1;
                    return;
                }
                st.idle[t] += 1;
                let (g, to) = sh.cv[t].wait_timeout(st, Duration::from_secs_f64(IDLE_S)).unwrap_or_else(|e| e.into_inner());
                st = g;
                st.idle[t] -= 1;
                if !to.timed_out() {
                    idle_since = sh.now();
                }
            }
        };
        kick(&sh);          // (another job that can start: its own thread)
        let id = picked.id;
        CUR.with(|c| *c.borrow_mut() = Some(Cur { sh: Arc::downgrade(&sh), id, cancel: picked.cancel.clone() }));
        let mut call = Some(picked.payload);
        let ran = Python::try_attach(|py| {
            let Some(PyCall { f, args }) = call.take() else { return };
            let fut = sh.state().core.handle(id).map(|h| h.clone_ref(py));
            let ok = match &fut {
                Some(fut) => run(py, fut.bind(py), f, args),
                None => false,
            };
            let h = sh.state().core.done(id, ok, sh.now());
            drop(h);
            drop(fut);
        });
        CUR.with(|c| *c.borrow_mut() = None);
        if ran.is_none() {
            // the interpreter is finalizing: nothing of Python touched again (references leaked)
            std::mem::forget(call.take());
            let mut st = sh.state();
            st.stop = true;
            st.threads[t] -= 1;
            return;
        }
        kick(&sh);
    }
}

/// f(*args) for fut (a concurrent.futures.Future); true when it returned (no exception).
fn run(py: Python<'_>, fut: &Bound<'_, PyAny>, f: Py<PyAny>, args: Py<PyTuple>) -> bool {
    match fut.call_method0("set_running_or_notify_cancel").and_then(|r| r.is_truthy()) {
        Ok(true) => {}
        Ok(false) => return false,     // (cancelled while it waited)
        Err(e) => {
            e.write_unraisable(py, Some(fut));
            return false;
        }
    }
    let (r, ok) = match f.bind(py).call1(args.bind(py)) {
        Ok(v) => (fut.call_method1("set_result", (v,)), true),
        Err(e) => (fut.call_method1("set_exception", (e.value(py),)), false),
    };
    if let Err(e) = r {
        e.write_unraisable(py, Some(fut));
    }
    ok
}

/// The controller thread: CPU pressure every second (Linux), a file read never on the event loop.
fn control(sh: Weak<Shared>) {
    loop {
        let Some(s) = sh.upgrade() else { return };
        {
            let st = s.state();
            if st.stop {
                return;
            }
            let (_g, _to) = s.ctl_cv.wait_timeout(st, Duration::from_secs(1)).unwrap_or_else(|e| e.into_inner());
        }
        let Some(psi) = read_psi() else {
            continue;
        };
        let changed = {
            let mut st = s.state();
            if st.stop {
                return;
            }
            let now = s.now();
            let before = st.adapt.cur;
            let n = st.adapt.note_psi(psi, now);
            st.core.set_target(n);
            n > before
        };
        if changed {
            kick(&s);
        }
    }
}

fn place_of(v: &Bound<'_, PyAny>) -> Option<i64> {
    if v.is_none() {
        return None;
    }
    if let Ok(n) = v.extract::<i64>() {
        return Some(n);
    }
    v.getattr("id").ok().and_then(|i| i.extract::<i64>().ok())
}

/// Sched(name, abandoned, min_threads, max_threads): the scheduler. abandoned: the exception class (pools.Abandoned)
/// a dropped job's future raises.
#[pyclass(module = "earthfast", frozen)]
pub struct Sched {
    sh: Arc<Shared>,
}

#[allow(clippy::too_many_arguments)]
fn submit_on<'py>(sh: &Arc<Shared>, py: Python<'py>, lane: usize, cls: u8, due: Option<f64>, place: Option<i64>,
                  key: Option<String>, f: Py<PyAny>, args: Py<PyTuple>) -> PyResult<Bound<'py, PyAny>> {
    let fut = py.import("concurrent.futures")?.getattr("Future")?.call0()?;
    let cls = cls.min(CLASSES as u8 - 1);
    let now = sh.now();
    let deadline = now + due.unwrap_or(DUE_S[cls as usize]);
    let got = {
        let mut st = sh.state();
        if st.stop {
            return Err(PyRuntimeError::new_err("cannot schedule new futures after shutdown"));
        }
        if lane >= st.core.lanes() {
            return Err(PyValueError::new_err("no such lane"));
        }
        match st.core.submit(lane, cls, deadline, place, key, PyCall { f, args }, fut.clone().unbind(), now) {
            Submitted::Queued(id) => Ok(id),
            Submitted::Attached(id, p, h) => Err((st.core.handle(id).map(|x| x.clone_ref(py)), p, h)),
            Submitted::Refused(p, h) => Err((None, p, h)),
        }
    };
    match got {
        Ok(id) => {
            fut.setattr("_bne_job", id)?;
            kick(sh);
            Ok(fut)
        }
        Err((Some(theirs), p, h)) => {
            drop((p, h));
            Ok(theirs.into_bound(py))
        }
        Err((None, p, h)) => {
            drop((p, h));
            let e = sh.abandoned.bind(py).call0()?;
            fut.call_method1("set_exception", (e,))?;
            Ok(fut)
        }
    }
}

#[pymethods]
impl Sched {
    #[new]
    #[pyo3(signature = (name, abandoned, min_threads = 2, max_threads = 8))]
    fn new(name: &str, abandoned: Py<PyAny>, min_threads: usize, max_threads: usize) -> Self {
        let adapt = Adapt::new(min_threads, max_threads);
        let sh = Arc::new(Shared {
            st: Mutex::new(State { core: Core::new(adapt.cur), adapt, idle: [0; 2], threads: [0; 2], stop: false }),
            cv: [Condvar::new(), Condvar::new()],
            name: name.to_string(),
            clock: Instant::now(),
            abandoned,
            ctl_cv: Condvar::new(),
            seq: AtomicU64::new(0),
        });
        let w = Arc::downgrade(&sh);
        let _ = std::thread::Builder::new().name(format!("{name}-ctl")).spawn(move || control(w));
        Sched { sh }
    }

    /// A lane of cap slots (adaptive: they count against the CPU target): its index.
    /// A lane of cap slots (adaptive: they count against the CPU target; urgent: a P0 job may take one more): its
    /// index.
    #[pyo3(signature = (name, cap, adaptive = false, urgent = true))]
    fn lane(&self, name: &str, cap: usize, adaptive: bool, urgent: bool) -> usize {
        self.sh.state().core.add_lane(LaneSpec { name: name.to_string(), cap, adaptive, urgent })
    }

    /// f(*args) as a job: a concurrent.futures.Future (with _bne_job, its id). due: s to its deadline (None: the
    /// class's DUE_S); place: the anchor id it is for (cancel_place); key: single-flight (the same key queued or
    /// running: that job's future).
    #[pyo3(signature = (lane, cls, due, place, key, f, *args))]
    #[allow(clippy::too_many_arguments)]
    fn submit<'py>(&self, py: Python<'py>, lane: usize, cls: u8, due: Option<f64>, place: Option<Bound<'py, PyAny>>,
                   key: Option<String>, f: Py<PyAny>, args: Py<PyTuple>) -> PyResult<Bound<'py, PyAny>> {
        let place = place.as_ref().and_then(place_of);
        submit_on(&self.sh, py, lane, cls, due, place, key, f, args)
    }

    /// An executor (submit / shutdown, as concurrent.futures') for lane at class cls; place_arg: the argument that is
    /// the job's anchor (an object with .id, or the id).
    #[pyo3(signature = (lane, cls, due = None, place_arg = None))]
    fn executor(&self, lane: usize, cls: u8, due: Option<f64>, place_arg: Option<usize>) -> Lane {
        Lane { sh: self.sh.clone(), lane, cls: cls.min(CLASSES as u8 - 1), due, place_arg }
    }

    /// A teleport left place: its queued jobs dropped (their futures raise abandoned), its running ones flagged
    /// (sched_cancelled); its new ones refused until uncancel_place. -> (dropped, flagged).
    fn cancel_place(&self, py: Python<'_>, place: i64) -> PyResult<(usize, usize)> {
        let (dropped, flagged) = self.sh.state().core.cancel_place(place);
        let n = dropped.len();
        for (_call, fut) in dropped {
            let e = self.sh.abandoned.bind(py).call0()?;
            let fut = fut.bind(py);
            if fut.call_method0("set_running_or_notify_cancel")?.is_truthy()? {
                fut.call_method1("set_exception", (e,))?;
            }
        }
        Ok((n, flagged))
    }

    fn uncancel_place(&self, place: i64) {
        self.sh.state().core.uncancel_place(place);
        kick(&self.sh);
    }

    /// A new Server in this process (tests make many; its anchor ids start again at 1): no place left any more.
    fn reset_places(&self) {
        self.sh.state().core.reset_places();
    }

    fn is_left(&self, place: i64) -> bool {
        self.sh.state().core.is_left(place)
    }

    /// This thread's job waits for something else (another job, a worker process doing another job's work): its
    /// slot given back. True when this is a job thread of this scheduler.
    fn block(&self) -> bool {
        let Some(id) = current(&self.sh) else { return false };
        let ok = self.sh.state().core.block(id);
        if ok {
            kick(&self.sh);
        }
        ok
    }

    fn unblock(&self) {
        if let Some(id) = current(&self.sh) {
            self.sh.state().core.unblock(id);
        }
    }

    /// Is this thread's job flagged (its place left)?
    fn cancelled(&self) -> bool {
        sched_cancelled()
    }

    /// fut's result, waited for without holding a slot: a job of this scheduler that fut is inherits this thread's
    /// job's class and deadline. A cancelled job (its place left) stops waiting: abandoned raised. timeout (s):
    /// concurrent.futures.TimeoutError past it.
    #[pyo3(signature = (fut, timeout = None))]
    fn wait<'py>(&self, py: Python<'py>, fut: Bound<'py, PyAny>, timeout: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let me = current(&self.sh);
        if let (Some(me), Ok(id)) = (me, fut.getattr("_bne_job").and_then(|v| v.extract::<u64>())) {
            let mut st = self.sh.state();
            if let Some((c, d)) = st.core.class_deadline(me) {
                st.core.boost(id, c, d);
            }
        }
        let blocked = self.block();
        let timeout_err = py.import("concurrent.futures")?.getattr("TimeoutError")?;
        let t0 = Instant::now();
        let r = loop {
            match fut.call_method1("result", (0.1f64,)) {
                Ok(v) => break Ok(v),
                Err(e) if e.is_instance(py, &timeout_err) => {
                    if blocked && sched_cancelled() {
                        break Err(PyErr::from_value(self.sh.abandoned.bind(py).call0()?));
                    }
                    if timeout.is_some_and(|t| t0.elapsed().as_secs_f64() >= t) {
                        break Err(e);
                    }
                }
                Err(e) => break Err(e),
            }
        };
        if blocked {
            self.unblock();
        }
        r
    }

    /// The pose's frame times (ms; None when the mod sends none) and whether the player waits for us. -> target.
    #[pyo3(signature = (mean_ms, max_ms = None, hold = false))]
    fn note_frame(&self, mean_ms: Option<f64>, max_ms: Option<f64>, hold: bool) -> usize {
        let (n, up) = {
            let mut st = self.sh.state();
            let now = self.sh.now();
            let before = st.adapt.cur;
            let n = st.adapt.note_frame(mean_ms.unwrap_or(f64::NAN), max_ms.unwrap_or(f64::NAN), hold, now);
            st.core.set_target(n);
            (n, n > before)
        };
        if up {
            kick(&self.sh);
        }
        n
    }

    /// A fixed target (tests, the settings' hard cap): the controller off.
    fn set_target(&self, n: usize) {
        {
            let mut st = self.sh.state();
            st.adapt.fixed = true;
            st.adapt.cur = n.max(1);
            st.core.set_target(n);
        }
        kick(&self.sh);
    }

    fn target(&self) -> usize {
        self.sh.state().core.target()
    }

    fn pending(&self) -> usize {
        self.sh.state().core.queued_len()
    }

    fn running(&self) -> usize {
        self.sh.state().core.running_len()
    }

    fn threads(&self) -> usize {
        let st = self.sh.state();
        st.threads[0] + st.threads[1]
    }

    /// {"target", "baseMs", "psi", "lanes": {name: [busy, urgent, queued, cap]}, "classes": {"P0": {...}}}; reset:
    /// the samples cleared (one stats line per interval).
    #[pyo3(signature = (reset = true))]
    fn stats<'py>(&self, py: Python<'py>, reset: bool) -> PyResult<Bound<'py, PyDict>> {
        let (stats, lanes, target, base, psi) = {
            let mut st = self.sh.state();
            let stats = if reset {
                std::mem::take(&mut st.core.stats)
            } else {
                st.core.stats.clone()
            };
            let lanes: Vec<(String, (usize, usize, usize, usize))> =
                (0..st.core.lanes()).map(|l| (st.core.lane_name(l).to_string(), st.core.lane_load(l))).collect();
            (stats, lanes, st.core.target(), st.adapt.base_ms, st.adapt.psi)
        };
        let d = PyDict::new(py);
        d.set_item("target", target)?;
        d.set_item("baseMs", base)?;
        d.set_item("psi", psi)?;
        let ld = PyDict::new(py);
        for (name, load) in lanes {
            ld.set_item(name, load)?;
        }
        d.set_item("lanes", ld)?;
        let cd = PyDict::new(py);
        for (c, mut s) in stats.into_iter().enumerate() {
            let e = PyDict::new(py);
            e.set_item("done", s.done)?;
            e.set_item("failed", s.failed)?;
            e.set_item("dropped", s.dropped)?;
            e.set_item("refused", s.refused)?;
            e.set_item("missed", s.missed)?;
            e.set_item("lateP50", pct(&mut s.late, 50.0))?;
            e.set_item("lateP95", pct(&mut s.late, 95.0))?;
            e.set_item("lateMax", pct(&mut s.late, 100.0))?;
            e.set_item("waitP50", pct(&mut s.wait, 50.0))?;
            e.set_item("waitP95", pct(&mut s.wait, 95.0))?;
            e.set_item("waitMax", pct(&mut s.wait, 100.0))?;
            cd.set_item(format!("P{c}"), e)?;
        }
        d.set_item("classes", cd)?;
        Ok(d)
    }

    /// No new jobs; cancel_futures: the queued ones' futures cancelled. The threads end once idle (not joined).
    #[pyo3(signature = (wait = true, cancel_futures = false))]
    fn shutdown(&self, py: Python<'_>, wait: bool, cancel_futures: bool) -> PyResult<()> {
        let _ = wait;
        let dropped: Vec<Py<PyAny>> = {
            let mut st = self.sh.state();
            st.stop = true;
            if cancel_futures {
                let ids: Vec<u64> = st.core.queued.keys().copied().collect();
                let mut out = Vec::new();
                for id in ids {
                    if let Some(q) = st.core.queued.remove(&id) {
                        let ready = &mut st.core.lanes[q.lane].ready;
                        ready.remove(&(q.class, okey(q.deadline), id));
                        out.push(q.handle);
                        drop(q.payload);
                    }
                }
                out
            } else {
                Vec::new()
            }
        };
        self.sh.cv[0].notify_all();
        self.sh.cv[1].notify_all();
        self.sh.ctl_cv.notify_all();
        for f in dropped {
            f.bind(py).call_method0("cancel")?;
        }
        Ok(())
    }
}

impl Drop for Sched {
    fn drop(&mut self) {
        self.sh.state().stop = true;
        self.sh.cv[0].notify_all();
        self.sh.cv[1].notify_all();
        self.sh.ctl_cv.notify_all();
    }
}

/// One lane at one class, as an executor (concurrent.futures' submit / shutdown): the server's thread pools under
/// the switch.
#[pyclass(module = "earthfast", frozen)]
pub struct Lane {
    sh: Arc<Shared>,
    lane: usize,
    cls: u8,
    due: Option<f64>,
    place_arg: Option<usize>,
}

#[pymethods]
impl Lane {
    #[pyo3(signature = (f, *args))]
    fn submit<'py>(&self, py: Python<'py>, f: Py<PyAny>, args: Py<PyTuple>) -> PyResult<Bound<'py, PyAny>> {
        let place = self.place_arg.and_then(|i| args.bind(py).get_item(i).ok()).as_ref().and_then(place_of);
        submit_on(&self.sh, py, self.lane, self.cls, self.due, place, None, f, args)
    }

    /// As ThreadPoolExecutor.shutdown, for this lane alone (the scheduler goes on): cancel_futures: its queued jobs'
    /// futures cancelled; wait: until none of its jobs is queued or running.
    #[pyo3(signature = (wait = true, cancel_futures = false))]
    fn shutdown(&self, py: Python<'_>, wait: bool, cancel_futures: bool) -> PyResult<()> {
        if cancel_futures {
            let dropped = self.sh.state().core.drain_lane(self.lane);
            for (_call, fut) in dropped {
                fut.bind(py).call_method0("cancel")?;
            }
        }
        if wait {
            let sh = self.sh.clone();
            let lane = self.lane;
            py.detach(move || {
                while sh.state().core.lane_live(lane) > 0 {
                    std::thread::sleep(Duration::from_millis(20));
                }
            });
        }
        Ok(())
    }

    #[getter]
    fn cls(&self) -> u8 {
        self.cls
    }
}

/// The process jobs' classes (pools.SlotPool): (function name, class); the others take their lane's.
const PROC_CLASSES: &[(&str, u8)] = &[
    ("plan_first", 0),
    ("roads_geo", 1),
    ("roads_tile", 1),
    ("terrain_patch", 1),
    ("plan_cell", 1),
    ("veg_geo", 2),
    ("veg_geo_many", 2),
    ("roadsurf_area", 3),
    ("navgraph", 2),
    ("signs_tile", 2),
    ("far_buildings_tile", 3),
    ("skyline_tile", 3),
    ("far_bridge_tile", 3),
    ("far_geo", 3),
    ("traffic_geo", 3),
    ("prebuild_geo_skin", 4),
];

/// (class, place) of a process job f(*args) on a lane of class default (pools.SlotPool): PROC_CLASSES by f's name
/// (a decorator's __wrapped__, a wrapper's .fn / .func looked through); the place is args[0] when f's first parameter is "aid".
#[pyfunction]
pub fn sched_classify(f: &Bound<'_, PyAny>, args: &Bound<'_, PyTuple>, default: u8) -> (u8, Option<i64>) {
    let mut g = f.clone();
    for _ in 0..4 {             // (decorators' __wrapped__, revisit.StaleJob's .fn, functools.partial's .func)
        if let Ok(w) = g.getattr("__wrapped__") {
            g = w;
            continue;
        }
        if g.getattr("__code__").is_err() {
            if let Ok(inner) = g.getattr("fn").or_else(|_| g.getattr("func")) {
                g = inner;
                continue;
            }
        }
        break;
    }
    let name: Option<String> = g.getattr("__name__").ok().and_then(|n| n.extract().ok());
    let cls = name.as_deref().and_then(|n| PROC_CLASSES.iter().find(|(k, _)| *k == n)).map(|(_, c)| *c).unwrap_or(default);
    let first: Option<String> = g.getattr("__code__").ok()
        .and_then(|c| c.getattr("co_varnames").ok())
        .and_then(|v| v.get_item(0).ok())
        .and_then(|v| v.extract().ok());
    let place = if first.as_deref() == Some("aid") {
        args.get_item(0).ok().and_then(|a| a.extract::<i64>().ok())
    } else {
        None
    };
    (cls.min(CLASSES as u8 - 1), place)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Sched>()?;
    m.add_class::<Lane>()?;
    m.add_function(wrap_pyfunction!(sched_cancelled, m)?)?;
    m.add_function(wrap_pyfunction!(sched_classify, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn core() -> Core<u32, u32> {
        let mut c = Core::new(8);
        c.add_lane(LaneSpec { name: "a".into(), cap: 1, adaptive: false, urgent: true });
        c
    }

    fn id(s: Submitted<u32, u32>) -> u64 {
        match s {
            Submitted::Queued(id) => id,
            _ => panic!("not queued"),
        }
    }

    #[test]
    fn class_then_deadline_order() {
        let mut c = core();
        let a = id(c.submit(0, 3, 1.0, None, None, 1, 1, 0.0));
        let b = id(c.submit(0, 1, 9.0, None, None, 2, 2, 0.0));
        let d = id(c.submit(0, 1, 5.0, None, None, 3, 3, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, d);
        assert!(c.pick(0, 0.0).is_none(), "one slot");
        c.done(d, true, 1.0);
        assert_eq!(c.pick(0, 1.0).unwrap().id, b);
        c.done(b, true, 2.0);
        assert_eq!(c.pick(0, 2.0).unwrap().id, a);
    }

    #[test]
    fn p0_takes_the_urgent_slot() {
        let mut c = core();
        let a = id(c.submit(0, 2, 1.0, None, None, 1, 1, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, a);
        let u = id(c.submit(0, 0, 1.0, None, None, 2, 2, 0.0));
        let v = id(c.submit(0, 0, 2.0, None, None, 3, 3, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, u, "beyond the cap");
        assert!(c.pick(0, 0.0).is_none(), "one urgent slot");
        c.done(u, true, 0.5);
        assert_eq!(c.pick(0, 0.5).unwrap().id, v);
    }

    #[test]
    fn background_tier_takes_only_p2_up() {
        let mut c = Core::<u32, u32>::new(8);
        c.add_lane(LaneSpec { name: "a".into(), cap: 4, adaptive: false, urgent: true });
        let a = id(c.submit(0, 1, 1.0, None, None, 1, 1, 0.0));
        let b = id(c.submit(0, 2, 1.0, None, None, 2, 2, 0.0));
        assert!(c.pick(1, 0.0).is_none(), "a P1 job first: not passed by the background tier");
        assert_eq!(c.pick(0, 0.0).unwrap().id, a);
        assert_eq!(c.pick(1, 0.0).unwrap().id, b);
        assert!(c.pick(1, 0.0).is_none());
    }

    #[test]
    fn a_blocked_job_frees_its_slot() {
        let mut c = core();
        let a = id(c.submit(0, 1, 1.0, None, None, 1, 1, 0.0));
        let b = id(c.submit(0, 1, 2.0, None, None, 2, 2, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, a);
        assert!(c.pick(0, 0.0).is_none());
        assert!(c.block(a));
        assert_eq!(c.pick(0, 0.0).unwrap().id, b, "a waits for b: b runs");
        c.done(b, true, 1.0);
        c.unblock(a);
        let c2 = id(c.submit(0, 1, 3.0, None, None, 3, 3, 1.0));
        assert!(c.pick(0, 1.0).is_none(), "a holds its slot again");
        c.done(a, true, 2.0);
        assert_eq!(c.pick(0, 2.0).unwrap().id, c2);
    }

    #[test]
    fn boost_moves_a_queued_job_ahead() {
        let mut c = core();
        let x = id(c.submit(0, 1, 1.0, None, None, 1, 1, 0.0));
        let late = id(c.submit(0, 3, 50.0, None, None, 2, 2, 0.0));
        let mid = id(c.submit(0, 2, 5.0, None, None, 3, 3, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, x);
        c.block(x);
        c.boost(late, 1, 1.0);
        assert_eq!(c.pick(0, 0.0).unwrap().id, late);
        let _ = mid;
    }

    #[test]
    fn cancel_place_drops_flags_and_refuses() {
        let mut c = Core::<u32, u32>::new(8);
        c.add_lane(LaneSpec { name: "a".into(), cap: 1, adaptive: false, urgent: true });
        let run = id(c.submit(0, 0, 1.0, Some(1), None, 1, 1, 0.0));
        let _q = id(c.submit(0, 0, 2.0, Some(1), None, 2, 2, 0.0));
        let other = id(c.submit(0, 1, 3.0, Some(2), None, 3, 3, 0.0));
        let p = c.pick(0, 0.0).unwrap();
        assert_eq!(p.id, run);
        let (dropped, flagged) = c.cancel_place(1);
        assert_eq!(dropped.iter().map(|d| d.0).collect::<Vec<_>>(), vec![2]);
        assert_eq!(flagged, 1);
        assert!(p.cancel.load(Ordering::SeqCst));
        assert!(matches!(c.submit(0, 0, 1.0, Some(1), None, 4, 4, 0.0), Submitted::Refused(4, 4)));
        // the new place's job starts in the urgent slot? no: P1; it waits for the slot the old one holds unless that
        // job gives up (the process lanes preempt it)
        assert!(c.pick(0, 0.0).is_none());
        c.done(run, false, 0.5);
        assert_eq!(c.pick(0, 0.5).unwrap().id, other);
        c.uncancel_place(1);
        assert!(matches!(c.submit(0, 0, 1.0, Some(1), None, 5, 5, 1.0), Submitted::Queued(_)));
    }

    #[test]
    fn single_flight_attaches_and_boosts() {
        let mut c = core();
        let a = id(c.submit(0, 3, 9.0, None, Some("k".into()), 1, 1, 0.0));
        match c.submit(0, 1, 1.0, None, Some("k".into()), 2, 2, 0.0) {
            Submitted::Attached(i, 2, 2) => assert_eq!(i, a),
            _ => panic!("not attached"),
        }
        assert_eq!(c.class_deadline(a), Some((1, 1.0)));
        let p = c.pick(0, 0.0).unwrap();
        c.done(p.id, true, 0.5);
        assert!(matches!(c.submit(0, 1, 1.0, None, Some("k".into()), 3, 3, 1.0), Submitted::Queued(_)));
    }

    #[test]
    fn speculative_and_late_need_room() {
        let mut c = Core::<u32, u32>::new(8);
        c.add_lane(LaneSpec { name: "a".into(), cap: 2, adaptive: false, urgent: true });
        let a = id(c.submit(0, 1, 1.0, None, None, 1, 1, 0.0));
        let s = id(c.submit(0, 4, 1.0, None, None, 2, 2, 0.0));
        let l = id(c.submit(0, 5, 1.0, None, None, 3, 3, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, a);
        assert!(c.pick(0, 0.0).is_none(), "P4 leaves the last slot free");
        c.done(a, true, 1.0);
        assert_eq!(c.pick(0, 1.0).unwrap().id, s);
        assert!(c.pick(0, 1.0).is_none(), "P5 only on an idle lane");
        c.done(s, true, 2.0);
        assert_eq!(c.pick(0, 2.0).unwrap().id, l);
    }

    #[test]
    fn starved_low_job_gets_a_slot() {
        let mut c = core();
        let low = id(c.submit(0, 2, 100.0, None, None, 1, 1, 0.0));
        let hi = id(c.submit(0, 1, 1.0, None, None, 2, 2, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, hi);
        c.done(hi, true, 1.0);
        let hi2 = id(c.submit(0, 1, 2.0, None, None, 3, 3, 1.0));
        assert_eq!(c.pick(0, STARVE_S + 1.0).unwrap().id, low, "waited past STARVE_S");
        c.done(low, true, STARVE_S + 2.0);
        assert_eq!(c.pick(0, STARVE_S + 2.0).unwrap().id, hi2);
    }

    #[test]
    fn adaptive_lanes_follow_the_target() {
        let mut c = Core::<u32, u32>::new(1);
        c.add_lane(LaneSpec { name: "cpu".into(), cap: 4, adaptive: true, urgent: true });
        c.add_lane(LaneSpec { name: "fixed".into(), cap: 4, adaptive: false, urgent: true });
        let a = id(c.submit(0, 2, 1.0, None, None, 1, 1, 0.0));
        let b = id(c.submit(0, 2, 2.0, None, None, 2, 2, 0.0));
        let f = id(c.submit(1, 2, 3.0, None, None, 3, 3, 0.0));
        assert_eq!(c.pick(0, 0.0).unwrap().id, a);
        assert_eq!(c.pick(0, 0.0).unwrap().id, f, "the fixed lane is not capped");
        assert!(c.pick(0, 0.0).is_none());
        c.set_target(2);
        assert_eq!(c.pick(0, 0.0).unwrap().id, b);
    }

    #[test]
    fn stats_lateness() {
        let mut c = core();
        let a = id(c.submit(0, 1, 1.0, None, None, 1, 1, 0.0));
        c.pick(0, 0.0);
        c.done(a, true, 3.0);
        assert_eq!(c.stats[1].done, 1);
        assert_eq!(c.stats[1].missed, 1);
        assert_eq!(pct(&mut c.stats[1].late, 95.0), Some(2.0));
    }

    #[test]
    fn adapt_steps_down_on_hot_frames_and_up_slowly() {
        let mut a = Adapt::new(2, 8);
        assert_eq!(a.cur, 8);
        for i in 0..20 {
            a.note_frame(16.7, 20.0, false, i as f64 * 0.5);
        }
        assert_eq!(a.cur, 8);
        a.note_frame(22.0, 30.0, false, 10.5);
        assert_eq!(a.cur, 8, "one hot frame window is not enough");
        a.note_frame(22.0, 30.0, false, 11.0);
        assert_eq!(a.cur, 7);
        a.note_frame(40.0, 60.0, false, 11.5);
        assert_eq!(a.cur, 3, "very hot: halved");
        a.note_frame(16.7, 20.0, false, 12.0);
        assert_eq!(a.cur, 3, "not before UP_S");
        a.note_frame(16.7, 20.0, false, 11.5 + UP_S);
        assert_eq!(a.cur, 4);
        a.note_frame(16.7, 20.0, true, 17.0);
        assert_eq!(a.cur, 8, "the player waits for us: all of it");
    }

    #[test]
    fn adapt_psi() {
        let mut a = Adapt::new(2, 4);
        a.note_psi(80.0, 10.0);
        assert_eq!(a.cur, 3);
        a.note_psi(80.0, 11.0);
        assert_eq!(a.cur, 3, "rate limited");
        a.note_psi(80.0, 12.0);
        assert_eq!(a.cur, 2);
        a.note_psi(80.0, 20.0);
        assert_eq!(a.cur, 2, "the floor");
        a.note_frame(f64::NAN, f64::NAN, false, 30.0);
        assert_eq!(a.cur, 2, "pressure still high: no step up");
        a.note_psi(5.0, 31.0);
        a.note_frame(f64::NAN, f64::NAN, false, 36.0);
        assert_eq!(a.cur, 3);
    }

    #[test]
    fn okey_orders_like_numbers() {
        let xs = [-5.0, -0.5, 0.0, 0.25, 3.0, 1e9, f64::INFINITY];
        for w in xs.windows(2) {
            assert!(okey(w[0]) < okey(w[1]), "{} {}", w[0], w[1]);
        }
        assert_eq!(okey(f64::NAN), okey(f64::INFINITY));
    }
}
