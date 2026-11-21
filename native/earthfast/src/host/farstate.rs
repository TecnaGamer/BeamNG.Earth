//! The far state machine (the Rust port's phase 2b, plan section 3 "Phase 2"): a connection's far tiles (what it
//! wants, has been sent, shows, is building), the swap groups between them, the build queue and the order of the far
//! messages. Python keeps the selection's inputs (the pose, the horizon, the camera), the tile builders (Server.tile,
//! through its pools) and the asyncio waits; this decides what to build next, what to send and when.
//!
//! The order rules (the mod's far.lua relies on them):
//! - a far_swap after every far_tile of its adds (emit_groups: a group goes once all its adds are sent);
//! - far_done after the last group (Python's _stream asks whole());
//! - a previous anchor's tiles go (far_remove) only once nothing that would leave a hole is still to come (flush_old).
//!
//! One instance per connection, used on the event loop; other threads may read it (Server.in_use from a worker): every
//! access takes the state's lock with the GIL let go while it waits, and nothing calls back into Python that could use
//! the instance again while the lock is held (that raises instead of deadlocking).
use std::borrow::Borrow;
use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::Hash;
use std::ops::{Deref, DerefMut};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, TryLockError};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PySet, PyString, PyTuple};

use super::msgs;
use super::pyjson::J;

#[path = "farselect.rs"]
pub mod farselect;
use farselect::{Node, SelSt};

type Key = Arc<str>;
type Ck = (i64, Key);
type Group = (Vec<Key>, Vec<Key>);
type GKey = (i64, Vec<Key>, Vec<Key>);

/// Python's None as an anchor id (a connection with no active anchor): no real anchor has it.
const NO_AID: i64 = i64::MIN;

// --- an insertion-ordered map (Python's dict order: the far messages go out in the order Python's dicts gave) -------

pub struct OMap<K, V> {
    slots: Vec<Option<(K, V)>>,
    idx: HashMap<K, usize>,
}

impl<K: Hash + Eq + Clone, V> OMap<K, V> {
    fn new() -> Self {
        OMap { slots: Vec::new(), idx: HashMap::new() }
    }

    fn len(&self) -> usize {
        self.idx.len()
    }

    fn is_empty(&self) -> bool {
        self.idx.is_empty()
    }

    fn get<Q: Hash + Eq + ?Sized>(&self, k: &Q) -> Option<&V> where K: Borrow<Q> {
        self.idx.get(k).and_then(|&i| self.slots[i].as_ref().map(|(_, v)| v))
    }

    fn contains_key<Q: Hash + Eq + ?Sized>(&self, k: &Q) -> bool where K: Borrow<Q> {
        self.idx.contains_key(k)
    }

    /// As a dict's assignment: an existing key keeps its place.
    fn insert(&mut self, k: K, v: V) {
        if let Some(&i) = self.idx.get(&k) {
            if let Some(s) = self.slots[i].as_mut() {
                s.1 = v;
            }
            return;
        }
        self.idx.insert(k.clone(), self.slots.len());
        self.slots.push(Some((k, v)));
    }

    fn remove<Q: Hash + Eq + ?Sized>(&mut self, k: &Q) -> Option<V> where K: Borrow<Q> {
        let i = self.idx.remove(k)?;
        let v = self.slots[i].take().map(|(_, v)| v);
        if self.slots.len() > 64 && self.slots.len() > 2 * self.idx.len() {
            let old = std::mem::take(&mut self.slots);
            self.idx.clear();
            for (k, v) in old.into_iter().flatten() {
                self.idx.insert(k.clone(), self.slots.len());
                self.slots.push(Some((k, v)));
            }
        }
        v
    }

    fn iter(&self) -> impl Iterator<Item = (&K, &V)> {
        self.slots.iter().filter_map(|s| s.as_ref().map(|(k, v)| (k, v)))
    }

    fn keys(&self) -> impl Iterator<Item = &K> {
        self.iter().map(|(k, _)| k)
    }

    fn clear(&mut self) {
        self.slots.clear();
        self.idx.clear();
    }

    /// The entries f keeps, in their order.
    fn retain(&mut self, mut f: impl FnMut(&K, &V) -> bool) {
        let old = std::mem::take(&mut self.slots);
        self.idx.clear();
        for (k, v) in old.into_iter().flatten() {
            if f(&k, &v) {
                self.idx.insert(k.clone(), self.slots.len());
                self.slots.push(Some((k, v)));
            }
        }
    }
}

// --- tiles -----------------------------------------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Plain,
    Global,
    Piece,
}

fn kind_of(k: &str) -> Kind {
    if k.starts_with('m') {
        Kind::Global
    } else if k.contains("_c") {
        Kind::Piece
    } else {
        Kind::Plain
    }
}

fn special(k: &str) -> bool {
    kind_of(k) != Kind::Plain
}

/// A far tile of the selection (farfield.Tile / GTile / Piece): the object itself (for the builders and the
/// messages) and what the state machine reads of it.
pub struct TRec {
    obj: Py<PyAny>,
    key: Key,
    level: i64,
    bbox: [f64; 4],
    kind: Kind,
    /// a global tile's quad or a piece's polygon (flat x, y)
    poly: Option<Vec<f64>>,
}

/// farfield.level_size
fn level_size(level: i64) -> f64 {
    libm::ldexp(256.0, level as i32)
}

/// (level, i, j) of a plain key ("L_i_j"), farfield.tile_of
fn node_of(k: &str) -> Option<(i64, i64, i64)> {
    let mut it = k.split('_').map(|v| v.parse::<i64>());
    let (l, i, j) = (it.next()?.ok()?, it.next()?.ok()?, it.next()?.ok()?);
    if it.next().is_some() {
        return None;
    }
    Some((l, i, j))
}

fn plain_bbox(k: &str) -> Option<[f64; 4]> {
    let (l, i, j) = node_of(k)?;
    let s = level_size(l);
    Some([i as f64 * s, j as f64 * s, (i + 1) as f64 * s, (j + 1) as f64 * s])
}

fn square(b: [f64; 4]) -> Vec<f64> {
    vec![b[0], b[1], b[2], b[1], b[2], b[3], b[0], b[3]]
}

fn overlaps(a: &[f64; 4], b: &[f64; 4]) -> bool {
    a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
}

// --- swap groups (farfield.swap_groups) ------------------------------------------------------------------------------

pub struct GroupCfg {
    pub min_area: f64,
    pub overlap_rel: f64,
    pub horizon_max_level: i64,
}

/// polys(key) -> the footprint polygon of a special key, or None
pub type PolyFn<'a> = &'a mut dyn FnMut(&Key) -> PyResult<Option<Vec<f64>>>;

/// LOD changes from key set `old` to `new` as swap groups [(adds, removes)]: each a connected set of added and removed
/// tiles that overlap (a parent and its descendants; global tiles and pieces by their footprints, polys(key) -> the
/// polygon or None: one-sided). Sorted lists, the groups by their first key: the same whatever the sets' order.
pub fn swap_groups(py: Python<'_>, old: &[Key], new: &[Key], polys: Option<PolyFn<'_>>,
                   cfg: &GroupCfg) -> PyResult<Vec<Group>> {
    let old_s: HashSet<&Key> = old.iter().collect();
    let new_s: HashSet<&Key> = new.iter().collect();
    let mut side_k: Vec<Key> = Vec::new();
    let mut side: Vec<u8> = Vec::new();
    let mut at: HashMap<Key, usize> = HashMap::new();
    for k in new.iter().filter(|k| !old_s.contains(k)) {
        if !at.contains_key(k) {
            at.insert(k.clone(), side_k.len());
            side_k.push(k.clone());
            side.push(0);
        }
    }
    for k in old.iter().filter(|k| !new_s.contains(k)) {
        if !at.contains_key(k) {
            at.insert(k.clone(), side_k.len());
            side_k.push(k.clone());
            side.push(1);
        }
    }
    let n = side_k.len();
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(p: &mut [usize], mut k: usize) -> usize {
        while p[k] != k {
            p[k] = p[p[k]];
            k = p[k];
        }
        k
    }
    let mut by_node: HashMap<(i64, i64, i64), usize> = HashMap::new();
    for (q, k) in side_k.iter().enumerate() {
        if !special(k) {
            if let Some(nd) = node_of(k) {
                by_node.insert(nd, q);
            }
        }
    }
    if let Some(polys) = polys {
        if side_k.iter().any(|k| special(k)) {
            let mut keys = Vec::new();
            let mut ps = Vec::new();
            for (q, k) in side_k.iter().enumerate() {
                let p = if special(k) { polys(k)? } else { plain_bbox(k).map(square) };
                if let Some(p) = p {
                    keys.push(q);
                    ps.push(p);
                }
            }
            let sides: Vec<u8> = keys.iter().map(|&q| side[q]).collect();
            // (the polygons' clipping without the GIL, as before: the server's other threads go on meanwhile)
            let (min_area, rel) = (cfg.min_area, cfg.overlap_rel);
            let pairs = py.detach(|| crate::farglobal::overlap_pairs(&ps, &sides, min_area, rel));
            for (a, b) in pairs {
                let (ka, kb) = (keys[a], keys[b]);
                if special(&side_k[ka]) || special(&side_k[kb]) {
                    let (ra, rb) = (find(&mut parent, ka), find(&mut parent, kb));
                    parent[ra] = rb;
                }
            }
        }
    }
    for q in 0..n {
        if special(&side_k[q]) {
            continue;
        }
        let Some((mut l, mut i, mut j)) = node_of(&side_k[q]) else { continue };
        while l < cfg.horizon_max_level {
            l += 1;
            i >>= 1;
            j >>= 1;
            if let Some(&a) = by_node.get(&(l, i, j)) {
                if side[a] != side[q] {
                    let (ra, rb) = (find(&mut parent, q), find(&mut parent, a));
                    parent[ra] = rb;
                }
            }
        }
    }
    let mut comps: Vec<Vec<usize>> = Vec::new();
    let mut comp_of: HashMap<usize, usize> = HashMap::new();
    for q in 0..n {
        let r = find(&mut parent, q);
        let c = *comp_of.entry(r).or_insert_with(|| {
            comps.push(Vec::new());
            comps.len() - 1
        });
        comps[c].push(q);
    }
    let mut groups: Vec<Group> = comps.into_iter().map(|c| {
        let mut a: Vec<Key> = c.iter().filter(|&&q| side[q] == 0).map(|&q| side_k[q].clone()).collect();
        let mut r: Vec<Key> = c.iter().filter(|&&q| side[q] == 1).map(|&q| side_k[q].clone()).collect();
        a.sort();
        r.sort();
        (a, r)
    }).collect();
    groups.sort_by(|x, y| {
        let fx = if x.0.is_empty() { &x.1[0] } else { &x.0[0] };
        let fy = if y.0.is_empty() { &y.1[0] } else { &y.0[0] };
        fx.cmp(fy)
    });
    Ok(groups)
}

fn keys_of(v: &Bound<'_, PyAny>) -> PyResult<Vec<Key>> {
    let mut out = Vec::new();
    for k in v.try_iter()? {
        out.push(Key::from(k?.extract::<String>()?.as_str()));
    }
    Ok(out)
}

/// farfield.swap_groups(old, new, polys=None) for Python (the tests; the far state machine calls swap_groups itself).
#[pyfunction]
#[pyo3(signature = (old, new, polys = None))]
fn far_swap_groups<'py>(py: Python<'py>, old: &Bound<'py, PyAny>, new: &Bound<'py, PyAny>,
                        polys: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyList>> {
    let ff = py.import("farfield")?;
    let cfg = GroupCfg { min_area: ff.getattr("GLOBAL_MIN_AREA")?.extract()?,
                         overlap_rel: ff.getattr("GLOBAL_OVERLAP_REL")?.extract()?,
                         horizon_max_level: ff.getattr("HORIZON_MAX_LEVEL")?.extract()? };
    let (old, new) = (keys_of(old)?, keys_of(new)?);
    let mut f = |k: &Key| -> PyResult<Option<Vec<f64>>> {
        let p = polys.expect("polys").call1((k.as_ref(),))?;
        if p.is_none() {
            return Ok(None);
        }
        let mut v = Vec::new();
        for x in p.try_iter()? {
            v.push(x?.extract::<f64>()?);
        }
        Ok(Some(v))
    };
    let groups = swap_groups(py, &old, &new, if polys.is_some() { Some(&mut f) } else { None }, &cfg)?;
    groups_py(py, &groups)
}

fn strs<'py>(py: Python<'py>, ks: &[Key]) -> PyResult<Bound<'py, PyList>> {
    PyList::new(py, ks.iter().map(|k| PyString::new(py, k)))
}

fn groups_py<'py>(py: Python<'py>, groups: &[Group]) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    for (a, r) in groups {
        out.append(PyTuple::new(py, [strs(py, a)?, strs(py, r)?])?)?;
    }
    Ok(out)
}

// --- the state -------------------------------------------------------------------------------------------------------

enum Delta {
    /// known without a computation: (dIn, dOut) less DELTA_MARGIN
    Ready(Py<PyAny>, Py<PyAny>),
    /// group_deltas on the delta pool (a concurrent.futures.Future)
    Fut(Py<PyAny>),
}

impl Delta {
    fn clone_ref(&self, py: Python<'_>) -> Delta {
        match self {
            Delta::Ready(a, b) => Delta::Ready(a.clone_ref(py), b.clone_ref(py)),
            Delta::Fut(f) => Delta::Fut(f.clone_ref(py)),
        }
    }
}

#[derive(Default)]
struct Batch {
    start: f64,
    sent: i64,
    swaps: i64,
}

struct St {
    /// the tile objects seen, by address (each holds its object: an address is not reused while it is here)
    recs: HashMap<usize, Arc<TRec>>,
    /// the selection (key -> tile), in Python's order
    desired: OMap<Key, Arc<TRec>>,
    /// (anchorId, key) -> footprint in the active anchor's world: what the mod shows
    shown: OMap<Ck, [f64; 4]>,
    /// sent and not swapped in yet (active anchor)
    sent: OMap<Key, Arc<TRec>>,
    /// (anchorId, key) -> monotonic time it left shown / sent
    retired: OMap<Ck, f64>,
    /// groups still to emit
    groups: Vec<Group>,
    /// (aid, the shown keys, the desired keys) of the last regroup
    rg_last: Option<(i64, HashSet<Key>, HashSet<Key>)>,
    deltas: HashMap<GKey, Delta>,
    /// tiles to build, by priority
    queue: Vec<Arc<TRec>>,
    qi: usize,
    /// a new queue's number (Server.maybe_restore: once per queue)
    queue_gen: u64,
    /// graded tiles passed over at the GRADED_SHARE cap, by priority
    graded_wait: VecDeque<Arc<TRec>>,
    /// tiles passed over while a bulk restore has them (Server.restoring)
    restore_wait: Vec<Arc<TRec>>,
    next_group: i64,
    /// (anchorId, key) -> (asyncio future of its TileInfo, tile)
    inflight: OMap<Ck, (Py<PyAny>, Arc<TRec>)>,
    /// (anchorId, key) -> when its imagery prefetch was last asked for
    prefetched: HashMap<Ck, f64>,
    failed: HashSet<Key>,
    /// far_done owed once every group is emitted
    dirty: bool,
    batch: Batch,
    /// (anchorId, key) -> a shown global tile's quad in that anchor's world
    gpolys: HashMap<Ck, Vec<f64>>,
    /// piece key -> its polygon (pieces sent / shown under the active anchor)
    ppolys: HashMap<Key, Vec<f64>>,
    /// the selection's own (farselect.rs)
    sel: SelSt,
}

struct Cfg {
    log: Py<PyAny>,
    monotonic: Py<PyAny>,
    tile_of: Py<PyAny>,
    group_deltas: Py<PyAny>,
    slot_number: Py<PyAny>,
    delta_margin: Py<PyAny>,
    global_aid: i64,
    imagery_prefetch: usize,
    prefetch_spare: usize,
    prefetch_again: f64,
    spare_again: f64,
    spare_per_pass: i64,
    prefetched_max: usize,
    view_penalty: f64,
    far_trace: bool,
    groups: GroupCfg,
    /// earthd's globals (its constants and modules, read live: the tests patch them); None: no selection here
    globals: Option<Py<PyDict>>,
    /// Connection.send (its own: the messages go straight to the wire; another, a test's, is called)
    send_fn: Option<Py<PyAny>>,
}

static NEXT_TID: AtomicU64 = AtomicU64::new(1);
thread_local!(static TID: u64 = NEXT_TID.fetch_add(1, Ordering::Relaxed));

fn tid() -> u64 {
    TID.with(|t| *t)
}

struct G<'a> {
    g: MutexGuard<'a, St>,
    owner: &'a AtomicU64,
}

impl Deref for G<'_> {
    type Target = St;
    fn deref(&self) -> &St {
        &self.g
    }
}

impl DerefMut for G<'_> {
    fn deref_mut(&mut self) -> &mut St {
        &mut self.g
    }
}

impl Drop for G<'_> {
    fn drop(&mut self) {
        self.owner.store(0, Ordering::SeqCst);
    }
}

/// FarState(cfg): a connection's far tiles (module notes). cfg: earthd's constants and callables (Connection.__init__).
#[pyclass(module = "earthfast", frozen)]
pub struct FarState {
    st: Mutex<St>,
    owner: AtomicU64,
    cfg: Cfg,
}

fn ck_tuple<'py>(py: Python<'py>, ck: &Ck) -> PyResult<Bound<'py, PyTuple>> {
    let a = if ck.0 == NO_AID { py.None().into_bound(py) } else { ck.0.into_pyobject(py)?.into_any() };
    PyTuple::new(py, [a, PyString::new(py, &ck.1).into_any()])
}

fn ck_of(t: &Bound<'_, PyAny>) -> PyResult<Ck> {
    let a = t.get_item(0)?;
    let aid = if a.is_none() { NO_AID } else { a.extract::<i64>()? };
    Ok((aid, Key::from(t.get_item(1)?.extract::<String>()?.as_str())))
}

fn aid_of(a: &Bound<'_, PyAny>) -> PyResult<i64> {
    if a.is_none() { Ok(NO_AID) } else { a.extract::<i64>() }
}

fn floats(v: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    let mut out = Vec::new();
    for x in v.try_iter()? {
        out.push(x?.extract::<f64>()?);
    }
    Ok(out)
}

fn bbox4(v: &Bound<'_, PyAny>) -> PyResult<[f64; 4]> {
    let f = floats(v)?;
    if f.len() != 4 {
        return Err(PyRuntimeError::new_err("a bbox of 4 values"));
    }
    Ok([f[0], f[1], f[2], f[3]])
}

/// Python's max(xs) (the first of the largest, by >)
fn py_max<'py>(xs: Vec<Bound<'py, PyAny>>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let mut it = xs.into_iter();
    let Some(mut m) = it.next() else { return Ok(None) };
    for x in it {
        if x.gt(&m)? {
            m = x;
        }
    }
    Ok(Some(m))
}

/// Python's sum(xs) (from int 0)
fn py_sum<'py>(py: Python<'py>, xs: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyAny>> {
    let mut s = 0i64.into_pyobject(py)?.into_any();
    for x in xs {
        s = s.add(x)?;
    }
    Ok(s)
}

/// 1.2 * x + 0.2 as Python computes it
fn bound_of<'py>(py: Python<'py>, x: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    PyFloat::new(py, 1.2).into_any().mul(x)?.add(PyFloat::new(py, 0.2))
}

impl St {
    fn ck(&self, global_aid: i64, aid: i64, k: &Key) -> Ck {
        if kind_of(k) == Kind::Global { (global_aid, k.clone()) } else { (aid, k.clone()) }
    }

    fn retire(&mut self, cks: impl IntoIterator<Item = Ck>, now: f64) {
        for ck in cks {
            self.retired.insert(ck, now);
        }
    }

    fn graded(t: &TRec, top: i64) -> bool {
        t.kind != Kind::Global && t.level <= top
    }

    /// Connection._next_tile: the next tile to build by priority, None when none is left; graded_ok false: passing
    /// over the graded ones (GRADED_SHARE: they go first again once a graded slot is free).
    fn next_tile(&mut self, ga: i64, aid: i64, graded_ok: bool, top: i64, restoring: &Bound<'_, PyAny>)
                 -> PyResult<Option<Arc<TRec>>> {
        let py = restoring.py();
        let check = restoring.len().map(|n| n > 0).unwrap_or(true);
        let held = |st: &St, t: &TRec| -> PyResult<bool> {
            if !check {
                return Ok(false);
            }
            restoring.contains(ck_tuple(py, &st.ck(ga, aid, &t.key))?)
        };
        let wanted = |st: &St, t: &TRec| {
            !st.sent.contains_key(&t.key) && !st.inflight.contains_key(&st.ck(ga, aid, &t.key))
                && st.desired.contains_key(&t.key)
        };
        let mut i = 0;
        while i < self.restore_wait.len() {
            let t = self.restore_wait[i].clone();
            if held(self, &t)? {
                i += 1;
                continue;
            }
            self.restore_wait.remove(i);
            if !wanted(self, &t) {
                continue;
            }
            if !graded_ok && Self::graded(&t, top) {
                self.graded_wait.push_back(t);
                continue;
            }
            return Ok(Some(t));
        }
        if graded_ok {
            while let Some(t) = self.graded_wait.pop_front() {
                if wanted(self, &t) {
                    return Ok(Some(t));
                }
            }
        }
        while self.qi < self.queue.len() {
            let t = self.queue[self.qi].clone();
            self.qi += 1;
            if !wanted(self, &t) {
                continue;
            }
            if held(self, &t)? {
                self.restore_wait.push(t);
                continue;
            }
            if !graded_ok && Self::graded(&t, top) {
                self.graded_wait.push_back(t);
                continue;
            }
            return Ok(Some(t));
        }
        Ok(None)
    }

    fn blocked(&self) -> usize {
        self.groups.iter().filter(|g| g.0.iter().any(|k| self.failed.contains(k))).count()
    }

    fn shown_count(&self, ga: i64, aid: i64) -> usize {
        self.desired.keys().filter(|k| self.shown.contains_key(&self.ck(ga, aid, k))).count()
    }

    fn rec(&mut self, obj: &Bound<'_, PyAny>) -> PyResult<Arc<TRec>> {
        self.rec_keyed(obj, None)
    }

    /// rec of a tile whose key is known (the selection's dict key: no call of its cached property)
    fn rec_keyed(&mut self, obj: &Bound<'_, PyAny>, key: Option<&Bound<'_, PyAny>>) -> PyResult<Arc<TRec>> {
        let p = obj.as_ptr() as usize;
        if let Some(r) = self.recs.get(&p) {
            return Ok(r.clone());
        }
        let key: String = match key {
            Some(k) => k.extract()?,
            None => obj.getattr("key")?.extract()?,
        };
        let kind = kind_of(&key);
        let poly = match kind {
            Kind::Global => Some(floats(&obj.getattr("quad")?)?),
            Kind::Piece => Some(floats(&obj.getattr("poly")?)?),
            Kind::Plain => None,
        };
        // (a piece is a new object at every selection: the last selection's of that key and polygon gives its level and
        // box, without their Python properties)
        let same = match kind {
            Kind::Piece => self.desired.get(key.as_str()).or_else(|| self.sent.get(key.as_str()))
                .filter(|o| o.kind == Kind::Piece && o.poly == poly).map(|o| (o.level, o.bbox)),
            _ => None,
        };
        let (level, bbox) = match same {
            Some(v) => v,
            None => (obj.getattr("level")?.extract()?, bbox4(&obj.getattr("bbox")?)?),
        };
        let r = Arc::new(TRec { obj: obj.clone().unbind(), key: Key::from(key.as_str()), level, bbox, kind, poly });
        if self.recs.len() > 4 * (self.desired.len() + self.sent.len() + self.queue.len() + self.inflight.len()) + 4096 {
            #[allow(clippy::disallowed_methods)] // hash order: a filter, each entry on its own
            self.recs.retain(|_, r| Arc::strong_count(r) > 1);
        }
        if kind != Kind::Piece {
            // (a piece is a new object at every selection: kept here, thousands of dead ones held the cycle collector)
            self.recs.insert(p, r.clone());
        }
        Ok(r)
    }
}

impl FarState {
    fn lock(&self, py: Python<'_>) -> PyResult<G<'_>> {
        let me = tid();
        loop {
            match self.st.try_lock() {
                Ok(g) => {
                    self.owner.store(me, Ordering::SeqCst);
                    return Ok(G { g, owner: &self.owner });
                }
                Err(TryLockError::Poisoned(p)) => {
                    self.owner.store(me, Ordering::SeqCst);
                    return Ok(G { g: p.into_inner(), owner: &self.owner });
                }
                Err(TryLockError::WouldBlock) => {
                    if self.owner.load(Ordering::SeqCst) == me {
                        return Err(PyRuntimeError::new_err("FarState used again from a callback while it is busy"));
                    }
                    py.detach(std::thread::yield_now);
                }
            }
        }
    }

    fn now(&self, py: Python<'_>) -> PyResult<f64> {
        self.cfg.monotonic.bind(py).call0()?.extract()
    }

    pub(super) fn visit_shown<'py>(&self, py: Python<'py>,
        mut visit: impl FnMut(Bound<'py,PyTuple>, [f64;4]) -> PyResult<()>) -> PyResult<()> {
        let state = self.lock(py)?;
        for (ck, bbox) in state.shown.iter() { visit(ck_tuple(py,ck)?,*bbox)?; }
        Ok(())
    }

    fn ga(&self) -> i64 {
        self.cfg.global_aid
    }

    /// Connection.send of a message made here: straight to the wire (send_frame) when the connection's send is its own,
    /// else through the one it has (a test's capture).
    fn send(&self, conn: &Bound<'_, PyAny>, m: J) -> PyResult<()> {
        let py = conn.py();
        let f = conn.getattr("send")?;
        let own = match (&self.cfg.send_fn, f.getattr("__func__")) {
            (Some(sf), Ok(func)) => func.is(sf.bind(py)),
            _ => false,
        };
        if !own {
            f.call1((m.to_py(py)?,))?;
            return Ok(());
        }
        let limit: usize = match &self.cfg.globals {
            Some(g) => g.bind(py).get_item("SEND_BUFFER_MAX")?
                .ok_or_else(|| PyRuntimeError::new_err("earthd.SEND_BUFFER_MAX"))?.extract()?,
            None => return Err(PyRuntimeError::new_err("FarState: no earthd globals (g)")),
        };
        send_frame(conn, limit, &mut || {
            let mut data = vec![0u8; 4];
            m.encode(py, &mut data)?;
            let n = (data.len() - 4) as u32;
            data[..4].copy_from_slice(&n.to_le_bytes());
            Ok(data)
        })?;
        Ok(())
    }

    fn far_remove(&self, conn: &Bound<'_, PyAny>, aid: i64, key: &str) -> PyResult<()> {
        let a = if aid == NO_AID { J::Null } else { J::Int(aid) };
        self.send(conn, J::O(vec![("type", J::S("far_remove".into())), ("anchorId", a), ("key", J::S(key.into()))]))
    }

    fn poke(conn: &Bound<'_, PyAny>) -> PyResult<()> {
        conn.getattr("credits")?.call_method1("poke", (conn,))?;
        Ok(())
    }

    /// (dIn, dOut) of a group whose adds are all sent: known at once (a global tile's or a piece's group goes by the
    /// errors' bound; a tile record evicted, by the errors left), else group_deltas on the delta pool.
    #[allow(clippy::too_many_arguments)]
    fn make_delta<'py>(&self, py: Python<'py>, info: &Bound<'py, PyAny>, server: &Bound<'py, PyAny>,
                       conn: &Bound<'py, PyAny>, aid: i64, adds: &[Key], removes: &[Key],
                       waker: &mut Option<Bound<'py, PyAny>>) -> PyResult<Delta> {
        let ga = self.ga();
        let ckt = |k: &Key| -> PyResult<Bound<'py, PyTuple>> {
            let ck = if kind_of(k) == Kind::Global { (ga, k.clone()) } else { (aid, k.clone()) };
            ck_tuple(py, &ck)
        };
        let errs = |ks: &mut dyn Iterator<Item = &Key>| -> PyResult<Vec<Bound<'py, PyAny>>> {
            let mut e = Vec::new();
            for k in ks {
                let c = ckt(k)?;
                if info.contains(&c)? {
                    e.push(info.get_item(&c)?.getattr("err")?);
                }
            }
            Ok(e)
        };
        let m = self.cfg.delta_margin.bind(py);
        let ready = |d: Bound<'py, PyAny>| -> PyResult<Delta> {
            let v = d.sub(m)?;
            Ok(Delta::Ready(v.clone().unbind(), v.unbind()))
        };
        if adds.iter().chain(removes).any(|k| special(k)) {
            // a global tile's grid is on its Mercator vertices, a piece's its node's: the mod's bound,
            // 1.2 (err_new + err_old) + 0.2 (crossover.delta) on the group's largest errors
            let zero = || PyFloat::new(py, 0.0).into_any();
            let en = py_max(errs(&mut adds.iter())?)?.unwrap_or_else(zero);
            let eo = py_max(errs(&mut removes.iter())?)?.unwrap_or_else(zero);
            let b: f64 = bound_of(py, &en.add(&eo)?)?.extract()?;
            return ready(PyFloat::new(py, b).into_any());
        }
        let mut sides = Vec::new();
        for ks in [adds, removes] {
            let l = PyList::empty(py);
            for k in ks {
                let c = ck_tuple(py, &(aid, k.clone()))?;
                if !info.contains(&c)? {
                    let e = errs(&mut adds.iter().chain(removes))?;
                    let d = if e.is_empty() { PyFloat::new(py, 0.0).into_any() } else { bound_of(py, &py_sum(py, &e)?)? };
                    return ready(d);
                }
                let grid = info.get_item(&c)?.getattr("grid")?;
                l.append(PyTuple::new(py, [self.cfg.tile_of.bind(py).call1((k.as_ref(),))?, grid])?)?;
            }
            sides.push(l);
        }
        let args = PyTuple::new(py, sides)?;
        let fut = server.getattr("delta_pool")?.call_method1("submit", (self.cfg.group_deltas.bind(py), args))?;
        if waker.is_none() {
            *waker = Some(conn.call_method0("_delta_waker")?);
        }
        fut.call_method1("add_done_callback", (waker.as_ref().unwrap(),))?;
        Ok(Delta::Fut(fut.unbind()))
    }

    /// Connection.emit_groups: far_swap every group whose adds are all sent.
    fn emit_groups(&self, py: Python<'_>, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>, aid: i64) -> PyResult<()> {
        let ga = self.ga();
        let info = server.getattr("tiles")?;
        let groups = self.lock(py)?.groups.clone();
        let mut rest = Vec::new();
        let mut live: HashSet<GKey> = HashSet::new();
        let mut waker = None;
        let aid_py = if aid == NO_AID { py.None().into_bound(py) } else { aid.into_pyobject(py)?.into_any() };
        for (adds, removes) in groups {
            if !{
                let st = self.lock(py)?;
                adds.iter().all(|k| st.sent.contains_key(k))
            } {
                rest.push((adds, removes));
                continue;
            }
            let gk: GKey = (aid, adds.clone(), removes.clone());
            live.insert(gk.clone());
            let have = self.lock(py)?.deltas.get(&gk).map(|d| d.clone_ref(py));
            let d = match have {
                Some(d) => d,
                None => {
                    let d = self.make_delta(py, &info, server, conn, aid, &adds, &removes, &mut waker)?;
                    self.lock(py)?.deltas.insert(gk.clone(), d.clone_ref(py));
                    d
                }
            };
            if let Delta::Fut(f) = &d {
                if !f.bind(py).call_method0("done")?.is_truthy()? {
                    rest.push((adds, removes));
                    continue;
                }
            }
            self.lock(py)?.deltas.remove(&gk);
            let m = self.cfg.delta_margin.bind(py);
            let (d_in, d_out) = match d {
                Delta::Ready(a, b) => (a.into_bound(py), b.into_bound(py)),
                Delta::Fut(f) => {
                    let f = f.into_bound(py);
                    let cancelled = f.call_method0("cancelled")?.is_truthy()?;
                    let exc = if cancelled { py.None().into_bound(py) } else { f.call_method0("exception")? };
                    if cancelled || !exc.is_none() {
                        let keys = PyTuple::new(py, adds.iter().map(|k| PyString::new(py, k)))?;
                        self.cfg.log.bind(py).call_method1("warning", ("swap deltas of %s: %r", keys, exc))?;
                        let mut e = Vec::new();
                        for k in adds.iter().chain(&removes) {
                            let c = ck_tuple(py, &if kind_of(k) == Kind::Global { (ga, k.clone()) } else { (aid, k.clone()) })?;
                            if info.contains(&c)? {
                                e.push(info.get_item(&c)?.getattr("err")?);
                            }
                        }
                        let d = if e.is_empty() { PyFloat::new(py, 0.0).into_any() } else { bound_of(py, &py_sum(py, &e)?)? };
                        let v = d.sub(m)?;
                        (v.clone(), v)
                    } else {
                        let r = f.call_method0("result")?;
                        (r.get_item(0)?, r.get_item(1)?)
                    }
                }
            };
            let (d_in, d_out) = (d_in.add(m)?, d_out.add(m)?);
            let gid = {
                let mut st = self.lock(py)?;
                st.next_group += 1;
                st.next_group - 1
            };
            let msg = msgs::far_swap(&aid_py, &gid.into_pyobject(py)?.into_any(), strs(py, &adds)?.as_any(),
                                     strs(py, &removes)?.as_any(), &d_in, &d_out)?;
            self.send(conn, msg)?;
            {
                let now = self.now(py)?;
                let mut st = self.lock(py)?;
                let rcks: Vec<Ck> = removes.iter().map(|k| st.ck(ga, aid, k)).collect();
                for c in &rcks {
                    st.shown.remove(c);
                }
                st.retire(rcks, now);
                for k in &adds {
                    if let Some(t) = st.sent.remove(k) {
                        let c = st.ck(ga, aid, k);
                        st.shown.insert(c, t.bbox);
                    }
                }
                st.batch.swaps += 1;
            }
            Self::poke(conn)?;
        }
        let gone: Vec<Delta> = {
            let mut st = self.lock(py)?;
            st.groups = rest;
            #[allow(clippy::disallowed_methods)] // hash order: groups re-planned meanwhile, each one's job cancelled
            let ks: Vec<GKey> = st.deltas.keys().filter(|g| !live.contains(*g)).cloned().collect();
            ks.into_iter().filter_map(|g| st.deltas.remove(&g)).collect()
        };
        for d in gone {
            if let Delta::Fut(f) = d {
                f.bind(py).call_method0("cancel")?;
            }
        }
        Ok(())
    }

    /// Connection.flush_old: far_remove every shown tile of a previous anchor once no desired tile that is not swapped
    /// in yet overlaps it (so nothing ever leaves a hole).
    fn flush_old(&self, py: Python<'_>, conn: &Bound<'_, PyAny>, aid: i64) -> PyResult<()> {
        let ga = self.ga();
        let (cands, pending): (Vec<(Ck, [f64; 4])>, Vec<[f64; 4]>) = {
            let st = self.lock(py)?;
            let cands: Vec<(Ck, [f64; 4])> = st.shown.iter().filter(|(c, _)| c.0 != aid && c.0 != ga)
                .map(|(c, b)| (c.clone(), *b)).collect();
            if cands.is_empty() {
                return Ok(());
            }
            let pending = st.desired.iter().filter(|(k, _)| !st.shown.contains_key(&st.ck(ga, aid, k)))
                .map(|(_, t)| t.bbox).collect();
            (cands, pending)
        };
        // (the scan without the state's lock nor the GIL: thousands of a previous anchor's tiles against every pending
        // one at each pass of a rebase; the server's other threads go on meanwhile, Codex r2)
        let keep: Vec<bool> = py.detach(|| cands.iter().map(|(_, c)| {
            !pending.iter().any(|u| c[0] < u[2] && u[0] < c[2] && c[1] < u[3] && u[1] < c[3])
        }).collect());
        let gone: Vec<Ck> = cands.into_iter().zip(keep).filter(|(_, k)| *k).map(|((c, _), _)| c).collect();
        for ck in gone {
            let now = self.now(py)?;
            {
                let mut st = self.lock(py)?;
                st.shown.remove(&ck);
                st.retire([ck.clone()], now);
            }
            self.far_remove(conn, ck.0, &ck.1)?;
            Self::poke(conn)?;
        }
        Ok(())
    }
}

#[pymethods]
impl FarState {
    #[new]
    fn new(cfg: &Bound<'_, PyDict>) -> PyResult<Self> {
        let get = |k: &str| -> PyResult<Bound<'_, PyAny>> {
            cfg.get_item(k)?.ok_or_else(|| PyRuntimeError::new_err(format!("FarState: {k} missing")))
        };
        let obj = |k: &str| -> PyResult<Py<PyAny>> { Ok(get(k)?.unbind()) };
        Ok(FarState {
            st: Mutex::new(St {
                recs: HashMap::new(), desired: OMap::new(), shown: OMap::new(), sent: OMap::new(), retired: OMap::new(),
                groups: Vec::new(), rg_last: None, deltas: HashMap::new(), queue: Vec::new(), qi: 0, queue_gen: 0,
                graded_wait: VecDeque::new(), restore_wait: Vec::new(), next_group: 1, inflight: OMap::new(),
                prefetched: HashMap::new(), failed: HashSet::new(), dirty: false, batch: Batch::default(),
                gpolys: HashMap::new(), ppolys: HashMap::new(), sel: SelSt::default(),
            }),
            owner: AtomicU64::new(0),
            cfg: Cfg {
                log: obj("log")?, monotonic: obj("monotonic")?, tile_of: obj("tile_of")?, group_deltas: obj("group_deltas")?,
                slot_number: obj("slot_number")?, delta_margin: obj("delta_margin")?,
                global_aid: get("global_aid")?.extract()?, imagery_prefetch: get("imagery_prefetch")?.extract()?,
                prefetch_spare: get("prefetch_spare")?.extract()?, prefetch_again: get("prefetch_again")?.extract()?,
                spare_again: get("spare_again")?.extract()?, spare_per_pass: get("spare_per_pass")?.extract()?,
                prefetched_max: get("prefetched_max")?.extract()?, view_penalty: get("view_penalty")?.extract()?,
                far_trace: get("far_trace")?.extract()?,
                groups: GroupCfg { min_area: get("min_area")?.extract()?, overlap_rel: get("overlap_rel")?.extract()?,
                                   horizon_max_level: get("horizon_max_level")?.extract()? },
                globals: cfg.get_item("g")?.map(|g| g.cast_into::<PyDict>().map(|d| d.unbind())).transpose()?,
                send_fn: cfg.get_item("send_fn")?.map(|f| f.unbind()),
            },
        })
    }

    // --- the state as Python reads it (Server.in_use, credits, the tests) ---------------------------------------------

    /// The selection (Connection.desired's setter): key -> tile, in its order.
    fn set_desired(&self, py: Python<'_>, d: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut st = self.lock(py)?;
        let mut m = OMap::new();
        for (k, t) in d.iter() {
            let r = st.rec_keyed(&t, Some(&k))?;
            m.insert(r.key.clone(), r);
        }
        st.desired = m;
        st.sel.gen += 1;
        Ok(())
    }

    // --- the selection (farselect.rs: Connection.update_far and its parts) ------------------------------------------

    /// Connection.update_far: the desired set, the swap groups and the build queue; the streamer woken. gate (a pose):
    /// not when nothing the selection goes by has changed.
    #[pyo3(signature = (conn, server, gate=false))]
    fn update_far(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>, gate: bool) -> PyResult<()> {
        self.update_far_impl(conn, server, gate)
    }

    /// Connection.prebuild_coarse: a teleport's coarse cover built ahead; the builds started.
    fn prebuild_coarse(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>, anchor: &Bound<'_, PyAny>)
                       -> PyResult<usize> {
        self.prebuild_coarse_impl(conn, server, anchor)
    }

    /// Connection.eye_clearance: the camera's height over the ground under the view (0 below EYE_LOD_MIN_M).
    fn eye_clearance(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>) -> PyResult<f64> {
        self.eye_clearance_impl(conn, server)
    }

    /// The screen-space LOD camera (cx, cy, dz, ppr, tau, reach, h, cap, cells, errs, aid, gerr), or None.
    fn view_args<'py>(&self, conn: &Bound<'py, PyAny>, server: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        self.view_impl(conn, server)
    }

    /// The pixels per radian the screen-space LOD goes by (the sliding maximum).
    fn lod_ppr(&self, py: Python<'_>, h: f64, fov: f64, now: f64) -> PyResult<f64> {
        self.lod_ppr_impl(py, h, fov, now)
    }

    /// K of this connection's levels 0..=n (0 below level 1).
    fn ks(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>, n: i32) -> PyResult<Vec<f64>> {
        self.ks_impl(conn, server, n)
    }

    /// The nodes refined by the last selection (the hysteresis), a set of (L, i, j) / ("m", z, x, y).
    #[getter]
    fn refined<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PySet>> {
        let ns: Vec<Node> = self.lock(py)?.sel.refined.iter().copied().collect();
        let out = PySet::empty(py)?;
        for n in ns {
            out.add(n.to_py(py)?)?;
        }
        Ok(out)
    }

    fn set_refined(&self, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut ns = std::collections::BTreeSet::new();
        for n in v.try_iter()? {
            ns.insert(Node::of(&n?)?);
        }
        let mut st = self.lock(py)?;
        st.sel.refined = ns;
        st.sel.gen += 1;
        Ok(())
    }

    /// node -> monotonic time of its last state change (held for HOLD_S).
    #[getter]
    fn node_changed<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let ns: Vec<(Node, f64)> = self.lock(py)?.sel.node_changed.iter().map(|(n, t)| (*n, *t)).collect();
        let d = PyDict::new(py);
        for (n, t) in ns {
            d.set_item(n.to_py(py)?, t)?;
        }
        Ok(d)
    }

    fn set_node_changed(&self, py: Python<'_>, d: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut m = std::collections::BTreeMap::new();
        for (n, t) in d.iter() {
            m.insert(Node::of(&n)?, t.extract::<f64>()?);
        }
        self.lock(py)?.sel.node_changed = m;
        Ok(())
    }

    /// Pose selections skipped by the gate (the last one surely the same).
    #[getter]
    fn skipped(&self, py: Python<'_>) -> PyResult<u64> {
        Ok(self.lock(py)?.sel.skipped)
    }

    /// A pose's far field (Connection.on_pose): the selection (gated), the far PVS (Connection.pvs_tick, after the
    /// selection: its radius), the larger far tiles' best photos (far_lazy_tick).
    fn pose(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>) -> PyResult<()> {
        self.update_far_impl(conn, server, true)?;
        conn.call_method0("pvs_tick")?;
        self.far_lazy_tick_impl(conn, server)
    }

    /// Connection.pvs_reveal: far tiles a new far PVS no longer hides re-rendered (far_retex).
    fn pvs_reveal(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>) -> PyResult<()> {
        self.pvs_reveal_impl(conn, server)
    }

    /// Connection.far_lazy_tick: the larger far tiles' best photos queued (every FAR_LAZY_EVERY_S at most).
    fn far_lazy_tick(&self, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>) -> PyResult<()> {
        self.far_lazy_tick_impl(conn, server)
    }

    #[getter]
    fn shown<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let st = self.lock(py)?;
        let d = PyDict::new(py);
        for (c, b) in st.shown.iter() {
            d.set_item(ck_tuple(py, c)?, PyTuple::new(py, b.to_vec())?)?;
        }
        Ok(d)
    }

    fn set_shown(&self, py: Python<'_>, d: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut m = OMap::new();
        for (c, b) in d.iter() {
            m.insert(ck_of(&c)?, bbox4(&b)?);
        }
        self.lock(py)?.shown = m;
        Ok(())
    }

    #[getter]
    fn sent<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let st = self.lock(py)?;
        let d = PyDict::new(py);
        for (k, t) in st.sent.iter() {
            d.set_item(k.as_ref(), t.obj.bind(py))?;
        }
        Ok(d)
    }

    fn set_sent(&self, py: Python<'_>, d: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut st = self.lock(py)?;
        let mut m = OMap::new();
        for (_, t) in d.iter() {
            let r = st.rec(&t)?;
            m.insert(r.key.clone(), r);
        }
        st.sent = m;
        Ok(())
    }

    #[getter]
    fn inflight<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let st = self.lock(py)?;
        let d = PyDict::new(py);
        for (c, (f, t)) in st.inflight.iter() {
            d.set_item(ck_tuple(py, c)?, (f.bind(py), t.obj.bind(py)))?;
        }
        Ok(d)
    }

    fn set_inflight(&self, py: Python<'_>, d: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut st = self.lock(py)?;
        let mut m = OMap::new();
        for (c, v) in d.iter() {
            let r = st.rec(&v.get_item(1)?)?;
            m.insert(ck_of(&c)?, (v.get_item(0)?.unbind(), r));
        }
        st.inflight = m;
        Ok(())
    }

    #[getter]
    fn retired<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let st = self.lock(py)?;
        let d = PyDict::new(py);
        for (c, t) in st.retired.iter() {
            d.set_item(ck_tuple(py, c)?, *t)?;
        }
        Ok(d)
    }

    /// The tests: a tile retired at t.
    fn retire_at(&self, py: Python<'_>, ck: &Bound<'_, PyAny>, t: f64) -> PyResult<()> {
        let c = ck_of(ck)?;
        self.lock(py)?.retired.insert(c, t);
        Ok(())
    }

    /// update_far: the retired tiles kept retired_keep seconds.
    fn prune_retired(&self, py: Python<'_>, now: f64, keep: f64) -> PyResult<()> {
        self.lock(py)?.retired.retain(|_, t| now - *t < keep);
        Ok(())
    }

    #[getter]
    fn groups<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let g = self.lock(py)?.groups.clone();
        groups_py(py, &g)
    }

    #[getter]
    fn queue<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let st = self.lock(py)?;
        PyList::new(py, st.queue.iter().map(|t| t.obj.bind(py)))
    }

    /// The tests: a build queue as it is (qi 0, nothing passed over).
    fn set_queue(&self, py: Python<'_>, q: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut st = self.lock(py)?;
        let mut v = Vec::new();
        for t in q.try_iter()? {
            v.push(st.rec(&t?)?);
        }
        st.queue = v;
        st.qi = 0;
        st.queue_gen += 1;
        st.graded_wait.clear();
        st.restore_wait.clear();
        Ok(())
    }

    #[getter]
    fn qi(&self, py: Python<'_>) -> PyResult<usize> {
        Ok(self.lock(py)?.qi)
    }

    #[getter]
    fn queue_gen(&self, py: Python<'_>) -> PyResult<u64> {
        Ok(self.lock(py)?.queue_gen)
    }

    /// Reserve eligible tiles from the unconsumed build queue, in streamer order. The worker
    /// publishes its results later. A retry of the same queue generation schedules nothing.
    fn reserve_restore<'py>(&self, py: Python<'py>, conn: &Bound<'py, PyAny>, server: &Bound<'py, PyAny>,
                            anchor: &Bound<'py, PyAny>, min_batch: usize) -> PyResult<Option<(Bound<'py,PyList>,Bound<'py,PyList>)>> {
        let generation = self.lock(py)?.queue_gen;
        if conn.getattr("_restore_q").is_ok_and(|v| v.extract::<u64>().ok() == Some(generation)) { return Ok(None); }
        conn.setattr("_restore_q", generation)?;
        if !server.call_method0("fin_name_len")?.is_truthy()? || server.call_method0("map_on")?.is_truthy()? { return Ok(None); }
        let aid = aid_of(&conn.getattr("active")?)?;
        let anchor_id = aid_of(&anchor.getattr("id")?)?;
        let coarse = server.getattr("coarse_keys")?;
        let tiles = server.getattr("tiles")?;
        let inflight = server.getattr("tile_inflight")?;
        let tried = server.getattr("restore_tried")?;
        let restoring = server.getattr("restoring")?;
        let batch = PyList::empty(py);
        let keys = PyList::empty(py);
        {
            let st = self.lock(py)?;
            for t in &st.queue[st.qi.min(st.queue.len())..] {
                if t.kind == Kind::Piece || st.sent.contains_key(&t.key) || !st.desired.contains_key(&t.key)
                    || coarse.contains(t.key.as_ref())? { continue; }
                let ck = st.ck(self.ga(), aid, &t.key);
                if st.inflight.contains_key(&ck) { continue; }
                let k = ck_tuple(py, &ck)?;
                if tiles.contains(&k)? || inflight.contains(&k)? || tried.contains(&k)? || restoring.contains(&k)? { continue; }
                batch.append(t.obj.bind(py))?;
                keys.append(ck_tuple(py, &st.ck(self.ga(),anchor_id,&t.key))?)?;
            }
        }
        if batch.len() < min_batch { return Ok(None); }
        if tried.len()? > 200_000 { tried.call_method0("clear")?; }
        tried.call_method1("update", (&keys,))?;
        restoring.call_method1("update", (&keys,))?;
        Ok(Some((batch,keys)))
    }

    #[getter]
    fn graded_wait<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let st = self.lock(py)?;
        PyList::new(py, st.graded_wait.iter().map(|t| t.obj.bind(py)))
    }

    #[getter]
    fn failed<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PySet>> {
        let st = self.lock(py)?;
        let s = PySet::empty(py)?;
        #[allow(clippy::iter_over_hash_type)] // hash order: into a set
        for k in &st.failed {
            s.add(k.as_ref())?;
        }
        Ok(s)
    }

    #[getter]
    fn dirty(&self, py: Python<'_>) -> PyResult<bool> {
        Ok(self.lock(py)?.dirty)
    }

    #[setter]
    fn set_dirty(&self, py: Python<'_>, v: bool) -> PyResult<()> {
        self.lock(py)?.dirty = v;
        Ok(())
    }

    /// (start, tiles sent, swaps sent) of the current change (the logs)
    #[getter]
    fn batch(&self, py: Python<'_>) -> PyResult<(f64, i64, i64)> {
        let st = self.lock(py)?;
        Ok((st.batch.start, st.batch.sent, st.batch.swaps))
    }

    /// update_far: a change of the selection while none was owed: far_done owed, a new batch from t0.
    fn start_batch(&self, py: Python<'_>, t0: f64) -> PyResult<()> {
        let mut st = self.lock(py)?;
        if !st.dirty {
            st.dirty = true;
            st.batch = Batch { start: t0, sent: 0, swaps: 0 };
        }
        Ok(())
    }

    fn is_shown(&self, py: Python<'_>, ck: &Bound<'_, PyAny>) -> PyResult<bool> {
        let c = ck_of(ck)?;
        Ok(self.lock(py)?.shown.contains_key(&c))
    }

    fn is_sent(&self, py: Python<'_>, key: &str) -> PyResult<bool> {
        Ok(self.lock(py)?.sent.contains_key(key))
    }

    fn is_inflight(&self, py: Python<'_>, ck: &Bound<'_, PyAny>) -> PyResult<bool> {
        let c = ck_of(ck)?;
        Ok(self.lock(py)?.inflight.contains_key(&c))
    }

    /// The shown tiles of anchor aid and the global ones (their keys as (anchorId, key)).
    fn shown_cks<'py>(&self, py: Python<'py>, aid: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        let (aid, ga) = (aid_of(aid)?, self.ga());
        let st = self.lock(py)?;
        let l = PyList::empty(py);
        for c in st.shown.keys().filter(|c| c.0 == aid || c.0 == ga) {
            l.append(ck_tuple(py, c)?)?;
        }
        Ok(l)
    }

    /// Desired tiles shown under the active anchor aid (far_done's count).
    fn shown_count(&self, py: Python<'_>, aid: &Bound<'_, PyAny>) -> PyResult<usize> {
        let aid = aid_of(aid)?;
        Ok(self.lock(py)?.shown_count(self.ga(), aid))
    }

    /// status.backlog: desired tiles of the active anchor that are not shown yet (V1_4_SPEC §4).
    fn backlog(&self, py: Python<'_>, aid: &Bound<'_, PyAny>) -> PyResult<usize> {
        if aid.is_none() {
            return Ok(0);
        }
        let aid = aid_of(aid)?;
        let st = self.lock(py)?;
        Ok(st.desired.len() - st.shown_count(self.ga(), aid))
    }

    /// The desired keys whose tiles the mod shows or has been sent (pvs_reveal), in their order.
    fn reveal_keys<'py>(&self, py: Python<'py>, aid: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        let (aid, ga) = (aid_of(aid)?, self.ga());
        let st = self.lock(py)?;
        let ks: Vec<Bound<'_, PyString>> = st.desired.keys()
            .filter(|k| st.shown.contains_key(&st.ck(ga, aid, k)) || st.sent.contains_key(*k))
            .map(|k| PyString::new(py, k)).collect();
        PyList::new(py, ks)
    }

    /// Builds this connection waits on that are not done yet.
    fn busy(&self, py: Python<'_>) -> PyResult<usize> {
        let st = self.lock(py)?;
        let mut n = 0;
        for (_, (f, _)) in st.inflight.iter() {
            if !f.bind(py).call_method0("done")?.is_truthy()? {
                n += 1;
            }
        }
        Ok(n)
    }

    fn has_inflight(&self, py: Python<'_>) -> PyResult<bool> {
        Ok(!self.lock(py)?.inflight.is_empty())
    }

    /// The far field is whole: nothing in flight, far_done owed and every group left blocked (an add failed).
    fn whole(&self, py: Python<'_>) -> PyResult<bool> {
        let st = self.lock(py)?;
        Ok(st.inflight.is_empty() && st.dirty && st.groups.len() == st.blocked())
    }

    /// The builds' futures (_stream waits on them).
    fn futures<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let st = self.lock(py)?;
        PyList::new(py, st.inflight.iter().map(|(_, (f, _))| f.bind(py)))
    }

    /// Every build's waiter cancelled (the streamer cancelled); except_aid: those of that anchor kept (a commit).
    #[pyo3(signature = (except_aid = None))]
    fn cancel(&self, py: Python<'_>, except_aid: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        let keep = match except_aid { Some(a) => Some(aid_of(a)?), None => None };
        let fs: Vec<Py<PyAny>> = {
            let st = self.lock(py)?;
            st.inflight.iter().filter(|(c, _)| keep != Some(c.0)).map(|(_, (f, _))| f.clone_ref(py)).collect()
        };
        for f in fs {
            f.bind(py).call_method0("cancel")?;
        }
        Ok(())
    }

    /// Server.in_use's part: (anchorId, key) of every tile this connection shows, has been sent, wants or builds
    /// (under the active anchor aid), or let go of less than keep seconds ago.
    fn in_use<'py>(&self, py: Python<'py>, aid: &Bound<'py, PyAny>, now: f64, keep: f64) -> PyResult<Bound<'py, PySet>> {
        let (aid, ga) = (aid_of(aid)?, self.ga());
        let st = self.lock(py)?;
        let out = PySet::empty(py)?;
        for c in st.shown.keys() {
            out.add(ck_tuple(py, c)?)?;
        }
        for k in st.sent.keys().chain(st.desired.keys()) {
            out.add(ck_tuple(py, &st.ck(ga, aid, k))?)?;
        }
        for c in st.inflight.keys() {
            out.add(ck_tuple(py, c)?)?;
        }
        for (c, t) in st.retired.iter() {
            if now - *t < keep {
                out.add(ck_tuple(py, c)?)?;
            }
        }
        Ok(out)
    }

    /// Server.names_in_use's part: the tiles about to be sent or built (sent, desired, in flight).
    fn names_in_use<'py>(&self, py: Python<'py>, aid: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PySet>> {
        let (aid, ga) = (aid_of(aid)?, self.ga());
        let st = self.lock(py)?;
        let out = PySet::empty(py)?;
        for k in st.sent.keys().chain(st.desired.keys()) {
            out.add(ck_tuple(py, &st.ck(ga, aid, k))?)?;
        }
        for c in st.inflight.keys() {
            out.add(ck_tuple(py, c)?)?;
        }
        Ok(out)
    }

    /// commit_anchor: tiles sent but not swapped in are dropped by the mod at the anchor switch, but for the global
    /// ones when keep_global (any anchor's: the mod keeps them loading, placed into the new anchor); the rest retired
    /// under the old anchor old_aid. The new anchor's selection starts empty.
    fn commit(&self, py: Python<'_>, old_aid: &Bound<'_, PyAny>, keep_global: bool) -> PyResult<()> {
        let (old, ga) = (aid_of(old_aid)?, self.ga());
        let now = self.now(py)?;
        let mut st = self.lock(py)?;
        let sent = std::mem::replace(&mut st.sent, OMap::new());
        let mut gone = Vec::new();
        for (k, t) in sent.iter() {
            if keep_global && t.kind == Kind::Global {
                st.sent.insert(k.clone(), t.clone());
            } else {
                gone.push(st.ck(ga, old, k));
            }
        }
        st.retire(gone, now);
        st.ppolys.clear();
        st.desired = OMap::new();
        st.groups.clear();
        st.queue.clear();
        st.qi = 0;
        st.queue_gen += 1;
        st.rg_last = None;
        st.graded_wait.clear();
        st.restore_wait.clear();
        st.failed.clear();
        Ok(())
    }

    /// forget_world: the mod's level started again: it has none of the tiles.
    fn forget(&self, py: Python<'_>) -> PyResult<()> {
        let mut st = self.lock(py)?;
        st.shown.clear();
        st.sent.clear();
        st.desired = OMap::new();
        st.groups.clear();
        st.queue.clear();
        st.qi = 0;
        st.queue_gen += 1;
        st.rg_last = None;
        st.failed.clear();
        Ok(())
    }

    /// The connection is gone: every tile, build waiter and swap-delta job let go (a job's done callback holds the
    /// connection, and a reference held here is one Python's cycle collector cannot see), the jobs cancelled.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let (deltas, inflight) = {
            let mut st = self.lock(py)?;
            st.recs.clear();
            st.desired.clear();
            st.shown.clear();
            st.sent.clear();
            st.retired.clear();
            st.groups.clear();
            st.rg_last = None;
            st.queue.clear();
            st.qi = 0;
            st.graded_wait.clear();
            st.restore_wait.clear();
            st.prefetched.clear();
            st.sel = SelSt::default();
            (std::mem::take(&mut st.deltas), std::mem::replace(&mut st.inflight, OMap::new()))
        };
        #[allow(clippy::iter_over_hash_type)] // hash order: each job cancelled on its own
        for (_, d) in deltas {
            if let Delta::Fut(f) = d {
                f.bind(py).call_method0("cancel")?;
            }
        }
        for (_, (f, _)) in inflight.iter() {
            f.bind(py).call_method0("cancel")?;
        }
        Ok(())
    }

    // --- the machine ------------------------------------------------------------------------------------------------

    /// Connection._regroup: the swap groups from what is shown to the selection (the same as the last time when
    /// neither changed); sent tiles no group needs any more removed (far_remove); the build order: L0 under the
    /// committed / pending block (gate: their boxes; the mod's cover and commit gating need them), then groups that
    /// are partly sent, then the rest; nearest first (cone: the camera's view cone, farfield.view_cone).
    /// frame_ll: the active anchor's frame origin (lat0, lon0) for global tiles' quads.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (conn, aid, fx, fy, cone, gate, forced_level, frame_ll))]
    fn regroup(&self, py: Python<'_>, conn: &Bound<'_, PyAny>, aid: &Bound<'_, PyAny>, fx: f64, fy: f64,
               cone: Option<Vec<f64>>, gate: Vec<[f64; 4]>, forced_level: i64, frame_ll: Option<(f64, f64)>)
               -> PyResult<()> {
        let (aid, ga) = (aid_of(aid)?, self.ga());
        let now = self.now(py)?;
        let removed: Vec<Key> = {
            let mut guard = self.lock(py)?;
            let st = &mut *guard;
            let cur: Vec<Key> = st.shown.keys().filter(|c| c.0 == aid || c.0 == ga).map(|c| c.1.clone()).collect();
            if st.gpolys.len() > 4 * st.desired.len() + 1000 {
                #[allow(clippy::disallowed_methods)] // hash order: a filter, each entry on its own
                st.gpolys.retain(|c, _| c.0 == aid);
            }
            let cur_s: HashSet<Key> = cur.iter().cloned().collect();
            let des_s: HashSet<Key> = st.desired.keys().cloned().collect();
            let same = matches!(&st.rg_last, Some((a, c, d)) if *a == aid && *c == cur_s && *d == des_s);
            if !same {
                let new: Vec<Key> = st.desired.keys().cloned().collect();
                let (desired, gpolys, ppolys) = (&st.desired, &mut st.gpolys, &st.ppolys);
                let mut poly_of = |k: &Key| -> PyResult<Option<Vec<f64>>> {
                    if let Some(t) = desired.get(k) {
                        return Ok(t.poly.clone());
                    }
                    if kind_of(k) == Kind::Global {
                        let c = (aid, k.clone());
                        if let Some(q) = gpolys.get(&c) {
                            return Ok(Some(q.clone()));
                        }
                        let Some((lat0, lon0)) = frame_ll else { return Ok(None) };
                        let mut it = k[1..].split('_').map(|v| v.parse::<i64>());
                        let (Some(Ok(z)), Some(Ok(x)), Some(Ok(y)), None) = (it.next(), it.next(), it.next(), it.next())
                        else {
                            return Ok(None);
                        };
                        let q = crate::geotiles::quad(&crate::geotiles::Frame::new(lat0, lon0), z as u32, x, y);
                        let q: Vec<f64> = q.iter().flatten().copied().collect();
                        gpolys.insert(c, q.clone());
                        return Ok(Some(q));
                    }
                    Ok(ppolys.get(k).cloned())
                };
                st.groups = swap_groups(py, &cur, &new, Some(&mut poly_of), &self.cfg.groups)?;
            }
            st.rg_last = Some((aid, cur_s, des_s));
            // sent tiles no group needs any more: the mod drops them (a global one kept from a previous anchor takes
            // the tile of this selection: its quad in this anchor's world)
            let needed: HashSet<Key> = st.groups.iter().flat_map(|g| g.0.iter().cloned()).collect();
            let upd: Vec<(Key, Arc<TRec>)> = st.sent.iter().filter_map(|(k, t)| match st.desired.get(k) {
                Some(d) if !Arc::ptr_eq(d, t) => Some((k.clone(), d.clone())),
                _ => None,
            }).collect();
            for (k, d) in upd {
                st.sent.insert(k, d);
            }
            let removed: Vec<Key> = st.sent.keys().filter(|k| !needed.contains(*k)).cloned().collect();
            for k in &removed {
                st.sent.remove(k);
                let c = st.ck(ga, aid, k);
                st.retire([c], now);
            }
            let started: HashSet<&Key> = st.groups.iter().filter(|g| g.0.iter().any(|a| st.sent.contains_key(a)))
                .flat_map(|g| g.0.iter()).collect();
            #[allow(clippy::disallowed_methods)] // hash order: sorted below by (class, distance, level, key), a
            // total order (the keys are distinct)
            let todo: Vec<Arc<TRec>> = needed.iter().filter(|k| !st.sent.contains_key(*k))
                .filter_map(|k| st.desired.get(k).cloned()).collect();
            let cone = cone.unwrap_or_default();
            let mut pr: Vec<(u8, f64, Arc<TRec>)> = todo.into_iter().map(|t| {
                let d = crate::view_distance(t.bbox, fx, fy, &cone, self.cfg.view_penalty);
                let block = (t.level == 0 || t.level == forced_level) && gate.iter().any(|b| overlaps(&t.bbox, b));
                let cls = if block || st.sel.driving.iter().any(|b| overlaps(&t.bbox, b)) {
                    0
                } else if started.contains(&t.key) {
                    1
                } else {
                    2
                };
                (cls, d, t)
            }).collect();
            pr.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
                .then(a.2.level.cmp(&b.2.level)).then(a.2.key.cmp(&b.2.key)));
            st.queue = pr.into_iter().map(|p| p.2).collect();
            st.qi = 0;
            st.queue_gen += 1;
            st.graded_wait.clear();
            st.restore_wait.clear();
            st.failed.clear();
            removed
        };
        for k in removed {
            self.far_remove(conn, aid, &k)?;
        }
        Ok(())
    }

    /// Connection._next_tile (the tests; pump calls it itself).
    fn next_tile<'py>(&self, py: Python<'py>, aid: &Bound<'py, PyAny>, graded_ok: bool, graded_top: i64,
                      restoring: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let aid = aid_of(aid)?;
        let t = self.lock(py)?.next_tile(self.ga(), aid, graded_ok, graded_top, restoring)?;
        Ok(t.map(|t| t.obj.bind(py).clone()))
    }

    /// One pass of Connection._stream's work: start builds by priority up to `workers` (graded ones up to g_cap, more
    /// when nothing else is left), the imagery of the queued tiles ahead asked for (prefetch: the imagery on, no
    /// teleport pending, no coarse cover), then every swap group whose adds are all sent, and a previous anchor's
    /// tiles that nothing would leave a hole under.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (conn, server, anchor, aid, teleport, workers, g_cap, graded_top, restoring, prefetch))]
    fn pump(&self, py: Python<'_>, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>, anchor: &Bound<'_, PyAny>,
            aid: &Bound<'_, PyAny>, teleport: bool, workers: usize, g_cap: usize, graded_top: i64,
            restoring: &Bound<'_, PyAny>, prefetch: bool) -> PyResult<()> {
        if anchor.is_none() {
            return Ok(());
        }
        let (aid, ga) = (aid_of(aid)?, self.ga());
        if !teleport {
            // (only builds under way count: a tile the server has already, a bulk restore's, is sent in this pass)
            let (mut g_n, mut busy) = (0usize, 0usize);
            {
                let st = self.lock(py)?;
                for (_, (f, t)) in st.inflight.iter() {
                    if !f.bind(py).call_method0("done")?.is_truthy()? {
                        busy += 1;
                        g_n += St::graded(t, graded_top) as usize;
                    }
                }
            }
            while busy < workers {
                let t = {
                    let mut st = self.lock(py)?;
                    let mut t = st.next_tile(ga, aid, g_n < g_cap, graded_top, restoring)?;
                    if t.is_none() && g_n >= g_cap {
                        // (nothing else to build: the cap would only leave workers idle)
                        t = st.next_tile(ga, aid, true, graded_top, restoring)?;
                    }
                    t
                };
                let Some(t) = t else { break };
                let fut = server.call_method1("tile", (anchor, t.obj.bind(py)))?;
                let done = fut.call_method0("done")?.is_truthy()?;
                {
                    let mut st = self.lock(py)?;
                    let c = st.ck(ga, aid, &t.key);
                    st.inflight.insert(c, (fut.unbind(), t.clone()));
                }
                if !done {
                    busy += 1;
                    g_n += St::graded(&t, graded_top) as usize;
                }
            }
        }
        if prefetch {
            // a tile at most every PREFETCH_AGAIN_S (a prefetch skips sources short of request tokens): the loop wakes on
            // every tile done and every message from the game
            let now = self.now(py)?;
            let cands: Vec<Arc<TRec>> = {
                let mut st = self.lock(py)?;
                if st.prefetched.len() > self.cfg.prefetched_max {
                    st.prefetched.clear();
                }
                let a = st.qi.min(st.queue.len());
                let b = (st.qi + self.cfg.imagery_prefetch + self.cfg.prefetch_spare).min(st.queue.len());
                st.queue[a..b].to_vec()
            };
            let tiles = server.getattr("tiles")?;
            let mut spare = self.cfg.spare_per_pass;
            for (i, t) in cands.iter().enumerate() {
                let soon = i < self.cfg.imagery_prefetch;
                if !soon && spare <= 0 {
                    break;
                }
                let c = self.lock(py)?.ck(ga, aid, &t.key);
                if tiles.contains(ck_tuple(py, &c)?)? {
                    continue;
                }
                let go = {
                    let mut st = self.lock(py)?;
                    let last = st.prefetched.get(&c).copied().unwrap_or(-1e9);
                    let go = now - last >= if soon { self.cfg.prefetch_again } else { self.cfg.spare_again };
                    if go {
                        st.prefetched.insert(c, now);
                    }
                    go
                };
                if go {
                    spare -= (!soon) as i64;
                    server.call_method1("prefetch_imagery", (anchor, t.obj.bind(py), soon))?;
                }
            }
        }
        self.emit_groups(py, conn, server, aid)?;
        self.flush_old(py, conn, aid)?;
        Ok(())
    }

    /// The builds done since the last pass (Connection._stream): each one still wanted sent (far_tile) with its
    /// pooled slot's serial, its material and texture; a failed one noted (its groups blocked). first_ramp: a
    /// teleport's coarse cover is under way; vfs_dir: the tiles' folder in the game; fmt4: the layout's format >= 4.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (conn, server, aid, first_ramp, vfs_dir, fmt4))]
    fn collect(&self, py: Python<'_>, conn: &Bound<'_, PyAny>, server: &Bound<'_, PyAny>, aid: &Bound<'_, PyAny>,
               first_ramp: bool, vfs_dir: &str, fmt4: bool) -> PyResult<()> {
        let aid_py = aid;
        let (aid, ga) = (aid_of(aid)?, self.ga());
        let done: Vec<(Ck, Py<PyAny>, Arc<TRec>)> = {
            let mut st = self.lock(py)?;
            let mut ks = Vec::new();
            for (c, (f, _)) in st.inflight.iter() {
                if f.bind(py).call_method0("done")?.is_truthy()? {
                    ks.push(c.clone());
                }
            }
            ks.into_iter().filter_map(|c| st.inflight.remove(&c).map(|(f, t)| (c, f, t))).collect()
        };
        if done.is_empty() {
            return Ok(());
        }
        let tiles = server.getattr("tiles")?;
        let matslots = server.getattr("matslots")?;
        let log = self.cfg.log.bind(py);
        for (ck, fut, t) in done {
            let fut = fut.into_bound(py);
            if fut.call_method0("cancelled")?.is_truthy()? {
                continue;
            }
            let exc = fut.call_method0("exception")?;
            if !exc.is_none() {
                let first = !self.lock(py)?.failed.contains(&t.key);
                let kw = PyDict::new(py);
                kw.set_item("exc_info", if first { exc.clone() } else { py.None().into_bound(py) })?;
                log.call_method("error", ("far tile %s of anchor %d failed: %r", t.key.as_ref(), ck.0, &exc), Some(&kw))?;
                self.lock(py)?.failed.insert(t.key.clone());
                continue;
            }
            {
                let st = self.lock(py)?;
                if ck != st.ck(ga, aid, &t.key) || !st.desired.contains_key(&t.key) || st.shown.contains_key(&ck)
                    || st.sent.contains_key(&t.key) {
                    continue;           // superseded meanwhile; the file stays cached
                }
            }
            let ckt = ck_tuple(py, &ck)?;
            // the server's latest record: a live retexture may have finished since this build
            let info = tiles.call_method1("get", (&ckt, fut.call_method0("result")?))?;
            if !info.getattr("material")?.is_none() && (server.getattr("renewing")?.contains(&ckt)?
                || !matslots.call_method1("owns", (&ckt, info.getattr("material")?))?.is_truthy()?) {
                let anchor = server.getattr("anchors")?.get_item(aid_py)?;
                let renewal = server.call_method1("renew_slot", (&ckt, &info, anchor, t.obj.bind(py)))?;
                self.lock(py)?.inflight.insert(ck, (renewal.unbind(), t));
                continue;
            }
            self.lock(py)?.sent.insert(t.key.clone(), t.clone());
            let material = info.getattr("material")?;
            if !material.is_none() && !info.getattr("stale")?.is_truthy()? && !first_ramp
                && conn.call_method1("tex_short", (&info, t.obj.bind(py)))?.is_truthy()? {
                info.setattr("stale", true)?;   // (a coarse cover's texture: rendered again, the whole chain at its own size)
            }
            if info.getattr("stale")?.is_truthy()? {      // a region turned ready since: re-rendered live, then far_retex
                if !server.getattr("degraded")?.contains(&ckt)? || !server.call_method0("imagery_down")?.is_truthy()? {
                    server.call_method1("retex_request", (&ckt,))?;
                }
            }
            let n_sent = {
                let mut st = self.lock(py)?;
                st.batch.sent += 1;
                if t.kind == Kind::Piece {
                    if let Some(p) = &t.poly {
                        st.ppolys.insert(t.key.clone(), p.clone());
                    }
                }
                st.batch.sent
            };
            // (geotiles phase 3b: a global tile placed by the mod from its own frame and quad; a piece by its polygon;
            // a pooled shape (matslots.py) with its send serial; protocol 4: the tile's own imagery material, its shape
            // with UV1, and M1's texture px)
            let obj = t.obj.bind(py);
            let gt = t.kind == Kind::Global;
            let poly = match t.kind {
                Kind::Global => Some(obj.getattr("quad")?),
                Kind::Piece => Some(obj.getattr("poly")?),
                Kind::Plain => None,
            };
            let shape = info.getattr("shape")?;
            let mut serial = None;
            if !shape.is_none() && shape.call_method1("startswith", ("slot_",))?.is_truthy()? {
                let s = server.getattr("slot_serial")?.call_method0("__next__")?;
                let n = self.cfg.slot_number.bind(py).call1((&material,))?;
                conn.getattr("slot_holds")?.set_item(n, &s)?;
                serial = Some(s);
            }
            let mut tex = None;
            if !material.is_none() && fmt4 {
                let px = info.getattr("tex_px")?;
                tex = Some(if px.is_truthy()? { px } else { server.call_method1("tex_size", (obj.getattr("level")?,))? });
            }
            let shape_name = if shape.is_truthy()? { shape.str()? } else { server.call_method1("tile_name", (ck_tuple(py, &ck)?.get_item(0)?, ck.1.as_ref()))?.str()? };
            let shape_path = PyString::new(py, &format!("{vfs_dir}/{}", shape_name.to_str()?));
            let texture = PyString::new(py, &format!("{vfs_dir}/{}", info.getattr("texture")?.str()?.to_str()?));
            fn opt(v: Bound<'_, PyAny>) -> Option<Bound<'_, PyAny>> { if v.is_none() { None } else { Some(v) } }
            let sink = opt(info.getattr("sink")?);
            let frame = if gt { opt(info.getattr("frame")?) } else { None };
            let m = msgs::far_tile(aid_py, obj.getattr("key")?.as_any(), &obj.getattr("level")?, shape_path.as_any(),
                                   &info.getattr("pos")?, &info.getattr("err")?, &info.getattr("skirt")?, sink.as_ref(),
                                   frame.as_ref(), poly.as_ref(), serial.as_ref(),
                                   if material.is_none() { None } else { Some(&material) }, Some(texture.as_any()),
                                   tex.as_ref())?;
            self.send(conn, m)?;
            if self.cfg.far_trace {
                conn.call_method1("_fartrace", (obj, &info))?;
            }
            if n_sent % 50 == 0 {
                let left: usize = {
                    let st = self.lock(py)?;
                    st.groups.iter().map(|g| g.0.iter().filter(|k| !st.sent.contains_key(*k)).count()).sum()
                };
                // this change's tiles (sent / sent + still to send), not the whole far field
                conn.call_method1("status", (format!("Far field loading: {}/{} tiles", n_sent, left as i64 + n_sent),))?;
            }
        }
        Ok(())
    }
}

// --- Connection.send ------------------------------------------------------------------------------------------------

/// A frame to the connection's writer as earthd's Connection.send sent it: nothing once it is closing (abort_stream),
/// lost its lease (a BeamMP reconnect's new connection holds it) or its writer is closing; past `limit` bytes waiting the
/// connection is aborted ("outgoing buffer limit exceeded"). A HostStream's wire is written here, any other writer
/// (asyncio's, the tests') through its write. The frame is made (encode) only once those checks pass, as Python's
/// Connection.send called protocol.encode. True: sent.
pub fn send_frame<'py>(conn: &Bound<'py, PyAny>, limit: usize, encode: &mut dyn FnMut() -> PyResult<Vec<u8>>)
                       -> PyResult<bool> {
    let py = conn.py();
    let get = |o: &Bound<'py, PyAny>, n: &str| -> PyResult<Option<Bound<'py, PyAny>>> {
        match o.getattr(n) {
            Ok(v) => Ok(Some(v)),
            Err(e) if e.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => Ok(None),
            Err(e) => Err(e),
        }
    };
    if let Some(c) = get(conn, "closing")? {
        if c.is_truthy()? {
            return Ok(false);
        }
    }
    if let Some(lease) = get(conn, "lease")?.filter(|l| !l.is_none()) {
        if !lease.getattr("connection")?.is(conn) {
            return Ok(false);
        }
    }
    let writer = conn.getattr("writer")?;
    let abort = || -> PyResult<bool> {
        conn.call_method1("abort_stream", ("outgoing buffer limit exceeded",))?;
        Ok(false)
    };
    // (a HostStream whose methods nobody replaced, the tests' mocks of its transport: straight to its wire)
    let own = |o: &Bound<'py, PyAny>, names: &[&str]| -> PyResult<bool> {
        let d = get(o, "__dict__")?;
        match d {
            Some(d) => {
                for n in names {
                    if d.contains(*n)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            None => Ok(true),
        }
    };
    let transport = get(&writer, "transport")?.filter(|t| !t.is_none());
    if type_name_is(&writer, "HostStream") && own(&writer, &["write", "is_closing"])?
        && transport.as_ref().map(|t| own(t, &["get_write_buffer_size"])).transpose()?.unwrap_or(true) {
        if let Some(w) = get(&writer, "_wire")? {
            if let Ok(w) = w.cast::<super::Wire>() {
                let core = &w.get().core;
                if core.is_closing() {
                    return Ok(false);
                }
                let data = encode()?;
                if core.pending() + data.len() > limit {
                    return abort();
                }
                core.write(&data);
                return Ok(true);
            }
        }
    }
    if writer.call_method0("is_closing")?.is_truthy()? {
        return Ok(false);
    }
    let data = encode()?;
    let pending: usize = match transport {
        Some(t) => t.call_method0("get_write_buffer_size")?.extract()?,
        None => 0,
    };
    if pending + data.len() > limit {
        return abort();
    }
    writer.call_method1("write", (pyo3::types::PyBytes::new(py, &data),))?;
    Ok(true)
}

fn type_name_is(o: &Bound<'_, PyAny>, name: &str) -> bool {
    o.get_type().name().map(|n| n.to_str().is_ok_and(|n| n == name)).unwrap_or(false)
}

/// earthd Connection.send(msg): its frame (protocol.encode's) through send_frame. True: sent.
#[pyfunction]
fn conn_send(conn: &Bound<'_, PyAny>, msg: &Bound<'_, PyAny>, limit: usize) -> PyResult<bool> {
    let py = conn.py();
    let raw = py.import("protocol")?.getattr("RawJson")?;
    send_frame(conn, limit, &mut || Ok(super::pyjson::encode_frame(py, msg, Some(&raw))?.as_bytes().to_vec()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(conn_send, m)?)?;
    m.add_class::<FarState>()?;
    m.add_function(wrap_pyfunction!(far_swap_groups, m)?)?;
    farselect::register(m)?;
    Ok(())
}
