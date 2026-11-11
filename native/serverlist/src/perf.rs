//! The anonymous performance data (TELEMETRY.md sections 2-5; the app's server/perfstats.py and the mod's
//! perfsummary.lua): a report's "perf" section is folded into daily totals per group (everyone, an app version, a GPU
//! model, a CPU model, a BeamNG.Earth quality preset, a screen class, an OS, a RAM size, the window's main scene, VRAM,
//! graphics API, vsync, refresh rate, other mods, frame pacing, the window's memory state, the server's size ...) and
//! then dropped: no report is kept. An install's setup (its machine and settings, sent once a session) is held in
//! memory only, to know which groups its windows count for; the install id is never written next to the numbers (a
//! group's distinct installs are counted through a keyed hash whose key lives in memory).
//!
//! Version 2 windows (perfsummary.lua VERSION 2, 2026-11-11: "so we can measure hitches and 1% lows properly") also carry
//! their own p50 / p99 / p99.9 frame time, a log-spaced histogram (FT_EDGES_MS), the frames over 2x / 4x their median,
//! each slow frame's context (TAGS), the 1 s+ causes, the slow frames by time since a teleport and while idle, and the
//! machine's memory. The collector keeps the distribution of the windows' p99 / p99.9 (WIN_BINS), so a group's "typical
//! 1 % low" is the median window's, not the pooled frames' (where a few slow machines set everyone's low). Version 1
//! windows are read as before.
//!
//! GET /stats/performance?days=N (1-400, default 7; 0: everything kept) public: per dimension, every group with at least
//! K_ANON installs on one day of the range (the rest summed as "other", shown when that has K_ANON too), with its fps,
//! 1 % lows, smooth share, hitches per hour, per speed band fps, the time held at the edge of the ground and the
//! catch-up share, the block waits and the scene shares; everyone's hitch context, time since teleport, idle, memory;
//! and the version x RAM x session length cross-tab. The error fingerprints are on /stats/advanced only.
//!
//! Kept in BNGE_TELEMETRY_DIR/perf/<unix day>.json (one file a day, today's rewritten every SAVE_S), DAY_KEEP days.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::time::{Duration, Instant};

pub const BINS: usize = 10; // perfsummary.lua FRAME_MS (9 upper edges) + the slowest bin
pub const EDGES_MS: [f64; 9] = [8.0, 11.0, 14.0, 17.0, 22.0, 34.0, 50.0, 100.0, 250.0];
/// perfsummary.lua FT_MS (version 2): the fine frame-time steps, about log-spaced from 4 ms to 2 s, every EDGES_MS edge
/// among them; a window sends them sparse ({"<0-based bin>": frames})
pub const FT_BINS: usize = 22;
pub const FT_EDGES_MS: [f64; 21] =
    [4.0, 6.0, 8.0, 11.0, 14.0, 17.0, 22.0, 28.0, 34.0, 42.0, 50.0, 70.0, 100.0, 140.0, 180.0, 250.0, 350.0, 500.0, 700.0,
     1000.0, 2000.0];
/// The windows' own p99 / p99.9 counted in log bins of 15 % from 4 ms (bin 0: under 4 ms; the last: from ~2.5 s)
pub const WIN_BINS: usize = 48;
const WIN_BASE_MS: f64 = 4.0;
const WIN_STEP: f64 = 1.15;
const BLOCK_BINS: usize = 8; // BLOCK_S (7 upper edges) + over the last
const BLOCK_EDGES: [&str; 8] = ["0.5 s", "1 s", "2 s", "4 s", "8 s", "16 s", "32 s", "over 32 s"];
const HITCH_BINS: usize = 4;
const DAY_KEEP: u64 = 400;
const MAX_GROUPS: usize = 4000; // a day's groups at most
const MAX_CAUSES: usize = 32;
const MAX_CAUSES_1S: usize = 8;
const MAX_LOADED: usize = 16;
const MAX_ERRORS: usize = 256;
const MAX_WINDOWS: usize = 6; // windows a report may carry
const SAVE_S: u64 = 600;
const SETUP_TTL: Duration = Duration::from_secs(36 * 3600);
pub const K_ANON: u32 = crate::telemetry::K_ANON;
pub const BANDS: [&str; 6] = ["stopped", "u30", "s30", "s60", "s100", "s150"];
pub const SCENES: [&str; 5] = ["empty", "rural", "town", "urban", "dense"];
/// a slow frame's context (perfsummary.lua TAGS, and 'none')
pub const TAGS: [&str; 19] = ["swap", "swapNext", "collision", "far", "retex", "roads", "traffic", "trees", "treeShapes",
                              "decode", "spawn", "despawn", "catchup", "ui", "rebase", "gc", "loading", "held", "none"];
/// time since the last teleport (perfsummary.lua TP_NAMES: under 10 s, 10-30 s, 30-120 s, longer)
pub const TP_NAMES: [&str; 4] = ["t10", "t30", "t120", "long"];

// ---- what a report carries (perfsummary.lua / perfstats.py); unknown fields are ignored. Version 2's fields are read
// as plain JSON values: a field of an unexpected shape is skipped, never the whole report ----

#[derive(Deserialize, Default)]
pub struct BandIn {
    s: Option<f64>,
    fh: Option<Vec<f64>>,
    hit: Option<Value>,
}

#[derive(Deserialize, Default)]
pub struct HeldIn {
    edge: Option<f64>,
    drop: Option<f64>,
    loading: Option<f64>,
}

#[derive(Deserialize, Default)]
pub struct DropsIn {
    n: Option<f64>,
    s: Option<f64>,
}

#[derive(Deserialize, Default)]
pub struct WindowIn {
    s: Option<f64>,
    fh: Option<Vec<f64>>,
    hit: Option<Vec<f64>>,
    #[serde(rename = "hitLoad")]
    hit_load: Option<Vec<f64>>,
    causes: Option<HashMap<String, f64>>,
    teleports: Option<f64>,
    blocks: Option<Vec<f64>>,
    bands: Option<HashMap<String, BandIn>>,
    scene: Option<HashMap<String, f64>>,
    held: Option<HeldIn>,
    drops: Option<DropsIn>,
    age: Option<f64>,
    // version 1 fields the first collector dropped
    loaded: Option<Value>,
    lua: Option<Value>,
    #[serde(rename = "roadsQueuedP90")]
    roads_q: Option<Value>,
    #[serde(rename = "mapOpen")]
    map_open: Option<Value>,
    // version 2
    ft: Option<Value>,
    p50: Option<Value>,
    p99: Option<Value>,
    p999: Option<Value>,
    over2x: Option<Value>,
    over4x: Option<Value>,
    ctx: Option<Value>,
    cause1s: Option<Value>,
    tp: Option<Value>,
    idle: Option<Value>,
    mach: Option<Value>,
}

/// The session-length bands (perfsummary.lua's ageBand: the band's lower edge in minutes) and their group names
/// "age:<name>"; any other number counts in none.
pub const AGES: [(f64, &str); 5] = [(0.0, "u30"), (30.0, "u60"), (60.0, "u120"), (120.0, "u240"), (240.0, "o240")];

#[derive(Deserialize, Default)]
pub struct SetupIn {
    cpu: Option<String>,
    gpu: Option<String>,
    #[serde(rename = "ramGB")]
    ram_gb: Option<f64>,
    res: Option<String>,
    os: Option<String>,
    #[serde(rename = "modSettings")]
    mod_settings: Option<Value>,
    // sent from the start, read from 2026-11-11
    #[serde(rename = "vramGB")]
    vram_gb: Option<Value>,
    api: Option<Value>,
    hz: Option<Value>,
    #[serde(rename = "gameSettings")]
    game_settings: Option<Value>,
    #[serde(rename = "otherMods")]
    other_mods: Option<Value>,
}

#[derive(Deserialize, Default)]
pub struct ServerIn {
    #[serde(rename = "catchupS")]
    catchup_s: Option<f64>,
    catchups: Option<f64>,
    errors: Option<HashMap<String, f64>>,
    // per fingerprint, its error codes ("OSError:24": n; perfstats.ErrorPrints)
    #[serde(rename = "errorCodes")]
    error_codes: Option<HashMap<String, HashMap<String, f64>>>,
    // the server's process tree: CPU cores used, memory (Linux PSS, Windows private bytes; MB), worker processes
    cores: Option<Value>,
    #[serde(rename = "memMB")]
    mem_mb: Option<Value>,
    workers: Option<Value>,
}

#[derive(Deserialize, Default)]
pub struct PerfIn {
    setup: Option<SetupIn>,
    game: Option<Vec<WindowIn>>,
    server: Option<ServerIn>,
}

// ---- reading the plain values ----

fn vnum(v: &Option<Value>) -> Option<f64> {
    v.as_ref().and_then(|x| x.as_f64()).filter(|x| x.is_finite())
}

fn vobj(v: &Option<Value>) -> Option<&serde_json::Map<String, Value>> {
    v.as_ref().and_then(|x| x.as_object())
}

/// n counts from a JSON list of numbers (missing, negative or not numbers: 0)
fn vcounts(v: Option<&Value>, n: usize) -> Vec<u64> {
    let mut out = vec![0u64; n];
    if let Some(list) = v.and_then(|x| x.as_array()) {
        for (i, x) in list.iter().take(n).enumerate() {
            if let Some(f) = x.as_f64().filter(|f| f.is_finite() && *f >= 0.0) {
                out[i] = f.min(1.0e7) as u64;
            }
        }
    }
    out
}

fn on_off(v: Option<&Value>) -> Option<&'static str> {
    match v? {
        Value::Bool(b) => Some(if *b { "on" } else { "off" }),
        Value::Number(n) => n.as_f64().map(|f| if f != 0.0 { "on" } else { "off" }),
        Value::String(s) => match s.to_ascii_lowercase().as_str() {
            "1" | "true" | "on" | "yes" => Some("on"),
            "0" | "false" | "off" | "no" => Some("off"),
            _ => None,
        },
        _ => None,
    }
}

fn vstr(v: &Option<Value>, max: usize) -> Option<String> {
    let s = v.as_ref()?.as_str()?.to_string();
    label(&Some(s), max)
}

/// a size in MB as a group label ("512 MB", "1.5 GB", "8 GB")
fn mb_label(mb: f64) -> String {
    if mb >= 1024.0 {
        let gb = (mb / 1024.0 * 10.0).round() / 10.0;
        if gb.fract() == 0.0 { format!("{} GB", gb as u64) } else { format!("{gb:.1} GB") }
    } else {
        format!("{} MB", mb.max(0.0) as u64)
    }
}

/// the WIN_BINS bin of a window's frame time (ms)
pub fn win_bin(ms: f64) -> usize {
    if !(ms >= WIN_BASE_MS) {
        return 0;
    }
    (((ms / WIN_BASE_MS).ln() / WIN_STEP.ln()).floor() as usize + 1).min(WIN_BINS - 1)
}

/// a WIN_BINS bin's (lower, upper) edges, ms
fn win_edges(i: usize) -> (f64, f64) {
    if i == 0 {
        (WIN_BASE_MS / WIN_STEP, WIN_BASE_MS)
    } else {
        (WIN_BASE_MS * WIN_STEP.powi(i as i32 - 1), WIN_BASE_MS * WIN_STEP.powi(i as i32))
    }
}

/// The q-quantile (0-1, of the windows, slowest last) of a WIN_BINS distribution, ms, placed geometrically inside its bin.
pub fn win_quantile(h: &[u64], q: f64) -> Option<f64> {
    let n: u64 = h.iter().sum();
    if n == 0 {
        return None;
    }
    let want = (q * n as f64).max(0.5);
    let mut acc = 0.0;
    for (i, c) in h.iter().enumerate() {
        let c = *c as f64;
        if c > 0.0 && acc + c >= want {
            let (lo, hi) = win_edges(i);
            let f = ((want - acc) / c).clamp(0.0, 1.0);
            return Some(lo * (hi / lo).powf(f));
        }
        acc += c;
    }
    None
}

fn fps_of(ms: Option<f64>) -> Value {
    match ms {
        Some(ms) if ms > 0.0 => serde_json::json!((1000.0 / ms * 10.0).round() / 10.0),
        _ => Value::Null,
    }
}

// ---- the totals ----

#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
#[serde(default)]
pub struct Band {
    pub s: f64,
    pub fh: Vec<u64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub hit: Vec<u64>, // (version 2: hitches over 50 / 100 / 250 / 1000 ms in this band)
}

/// Seconds and the hitches in them (time since a teleport, idle)
#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
#[serde(default)]
pub struct Span {
    pub s: f64,
    pub hit: Vec<u64>,
}

impl Span {
    fn merge(&mut self, o: &Span) {
        self.s += o.s;
        add_vec(&mut self.hit, &o.hit, HITCH_BINS);
    }
    fn from(v: Option<&Value>) -> Option<Span> {
        let c = vcounts(v, HITCH_BINS + 1);
        let s = v.and_then(|x| x.as_array()).and_then(|a| a.first()).and_then(|x| x.as_f64())
            .filter(|x| x.is_finite()).unwrap_or(0.0).clamp(0.0, 3600.0);
        if s <= 0.0 && c.iter().skip(1).all(|x| *x == 0) {
            return None;
        }
        Some(Span { s, hit: c[1..].to_vec() })
    }
}

/// A sum and a count (a mean over windows / reports)
#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
#[serde(default)]
pub struct Mean {
    pub sum: f64,
    pub n: u64,
}

impl Mean {
    fn add(&mut self, v: Option<f64>) {
        if let Some(v) = v.filter(|x| x.is_finite() && *x >= 0.0 && *x < 1.0e9) {
            self.sum += v;
            self.n += 1;
        }
    }
    fn merge(&mut self, o: &Mean) {
        self.sum += o.sum;
        self.n += o.n;
    }
    fn get(&self) -> Value {
        if self.n == 0 { Value::Null } else { serde_json::json!((self.sum / self.n as f64 * 10.0).round() / 10.0) }
    }
    fn is_empty(&self) -> bool {
        self.n == 0
    }
}

fn is_zero(v: &u64) -> bool {
    *v == 0
}
fn is_zero_f(v: &f64) -> bool {
    *v == 0.0
}

#[derive(Serialize, Deserialize, Default, Clone, Debug)]
#[serde(default)]
pub struct Agg {
    pub installs: u32, // distinct installs this day (the keyed-hash set's size, kept as a number)
    pub windows: u64,
    pub s: f64,
    pub fh: Vec<u64>,
    pub hit: Vec<u64>,
    pub hit_load: Vec<u64>, // (of hit: while loading after a teleport)
    pub bands: BTreeMap<String, Band>,
    pub causes: BTreeMap<String, u64>,
    pub scene: BTreeMap<String, f64>,
    pub edge: f64,
    pub drophold: f64,
    pub loading: f64,
    pub drops: u64,
    pub drop_s: f64,
    pub blocks: Vec<u64>,
    pub teleports: u64,
    pub catchups: u64,
    pub catchup_s: f64,
    // the version 1 fields read from 2026-11-11 (means over windows)
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub loaded: BTreeMap<String, Mean>,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub heap_mb: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub garbage_mb: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub roads_q: Mean,
    #[serde(skip_serializing_if = "is_zero_f")]
    pub map_open: f64,
    // version 2: the windows that carried their own percentiles, their seconds, their p99 / p99.9 distributions, the
    // fine histogram, frames over 2x / 4x the median, the context of slow frames, the 1 s+ causes, since teleport, idle
    #[serde(skip_serializing_if = "is_zero")]
    pub v2_windows: u64,
    #[serde(skip_serializing_if = "is_zero_f")]
    pub v2_s: f64,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub ft: Vec<u64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub wp99: Vec<u64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub wp999: Vec<u64>,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub p50_ms: Mean,
    #[serde(skip_serializing_if = "is_zero")]
    pub over2x: u64,
    #[serde(skip_serializing_if = "is_zero")]
    pub over4x: u64,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub ctx: BTreeMap<String, Vec<u64>>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub cause1s: BTreeMap<String, u64>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub tp: BTreeMap<String, Span>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub idle: Option<Span>,
    // the machine over the windows (MB, the steps' lower edges) and the server's process tree over the reports
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub game_mb: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub free_mb: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub vram_mb: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub srv_mem: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub srv_cores: Mean,
    #[serde(skip_serializing_if = "Mean::is_empty")]
    pub srv_workers: Mean,
    #[serde(skip)]
    seen: HashSet<u64>,
}

fn add_vec(dst: &mut Vec<u64>, src: &[u64], n: usize) {
    if src.is_empty() {
        return;
    }
    dst.resize(n, 0);
    for (i, v) in src.iter().take(n).enumerate() {
        dst[i] += v;
    }
}

fn counts(v: &Option<Vec<f64>>, n: usize) -> Vec<u64> {
    let mut out = vec![0u64; n];
    if let Some(v) = v {
        for (i, x) in v.iter().take(n).enumerate() {
            if x.is_finite() && *x >= 0.0 {
                out[i] = x.min(1.0e7) as u64;
            }
        }
    }
    out
}

fn secs(v: Option<f64>) -> f64 {
    v.filter(|x| x.is_finite()).unwrap_or(0.0).clamp(0.0, 3600.0)
}

fn per_hour(n: u64, s: f64) -> Value {
    if s < 60.0 { Value::Null } else { serde_json::json!((n as f64 / (s / 3600.0) * 10.0).round() / 10.0) }
}

impl Agg {
    fn merge(&mut self, o: &Agg) {
        self.windows += o.windows;
        self.s += o.s;
        add_vec(&mut self.fh, &o.fh, BINS);
        add_vec(&mut self.hit, &o.hit, HITCH_BINS);
        add_vec(&mut self.hit_load, &o.hit_load, HITCH_BINS);
        for (k, b) in &o.bands {
            let e = self.bands.entry(k.clone()).or_default();
            e.s += b.s;
            add_vec(&mut e.fh, &b.fh, BINS);
            add_vec(&mut e.hit, &b.hit, HITCH_BINS);
        }
        for (k, v) in &o.causes {
            *self.causes.entry(k.clone()).or_default() += v;
        }
        for (k, v) in &o.scene {
            *self.scene.entry(k.clone()).or_default() += v;
        }
        self.edge += o.edge;
        self.drophold += o.drophold;
        self.loading += o.loading;
        self.drops += o.drops;
        self.drop_s += o.drop_s;
        add_vec(&mut self.blocks, &o.blocks, BLOCK_BINS);
        self.teleports += o.teleports;
        self.catchups += o.catchups;
        self.catchup_s += o.catchup_s;
        for (k, m) in &o.loaded {
            self.loaded.entry(k.clone()).or_default().merge(m);
        }
        self.heap_mb.merge(&o.heap_mb);
        self.garbage_mb.merge(&o.garbage_mb);
        self.roads_q.merge(&o.roads_q);
        self.map_open += o.map_open;
        self.v2_windows += o.v2_windows;
        self.v2_s += o.v2_s;
        add_vec(&mut self.ft, &o.ft, FT_BINS);
        add_vec(&mut self.wp99, &o.wp99, WIN_BINS);
        add_vec(&mut self.wp999, &o.wp999, WIN_BINS);
        self.p50_ms.merge(&o.p50_ms);
        self.over2x += o.over2x;
        self.over4x += o.over4x;
        for (k, c) in &o.ctx {
            add_vec(self.ctx.entry(k.clone()).or_default(), c, HITCH_BINS);
        }
        for (k, v) in &o.cause1s {
            *self.cause1s.entry(k.clone()).or_default() += v;
        }
        for (k, t) in &o.tp {
            self.tp.entry(k.clone()).or_default().merge(t);
        }
        if let Some(i) = &o.idle {
            self.idle.get_or_insert_with(Span::default).merge(i);
        }
        self.game_mb.merge(&o.game_mb);
        self.free_mb.merge(&o.free_mb);
        self.vram_mb.merge(&o.vram_mb);
        self.srv_mem.merge(&o.srv_mem);
        self.srv_cores.merge(&o.srv_cores);
        self.srv_workers.merge(&o.srv_workers);
    }

    fn window(w: &WindowIn) -> Agg {
        let mut a = Agg { windows: 1, s: secs(w.s), fh: counts(&w.fh, BINS), hit: counts(&w.hit, HITCH_BINS),
                          hit_load: counts(&w.hit_load, HITCH_BINS),
                          blocks: counts(&w.blocks, BLOCK_BINS), ..Default::default() };
        if let Some(bands) = &w.bands {
            for name in BANDS {
                if let Some(b) = bands.get(name) {
                    let hit = if b.hit.is_some() { vcounts(b.hit.as_ref(), HITCH_BINS) } else { Vec::new() };
                    a.bands.insert(name.to_string(), Band { s: secs(b.s), fh: counts(&b.fh, BINS), hit });
                }
            }
        }
        if let Some(c) = &w.causes {
            for (k, v) in c.iter().take(MAX_CAUSES) {
                if name_ok(k) && v.is_finite() && *v >= 0.0 {
                    a.causes.insert(k.clone(), v.min(1.0e6) as u64);
                }
            }
        }
        if let Some(sc) = &w.scene {
            for name in SCENES {
                if let Some(v) = sc.get(name) {
                    a.scene.insert(name.to_string(), secs(Some(*v)));
                }
            }
        }
        if let Some(h) = &w.held {
            a.edge = secs(h.edge);
            a.drophold = secs(h.drop);
            a.loading = secs(h.loading);
        }
        if let Some(d) = &w.drops {
            a.drops = d.n.filter(|x| x.is_finite()).unwrap_or(0.0).clamp(0.0, 1000.0) as u64;
            a.drop_s = secs(d.s);
        }
        a.teleports = w.teleports.filter(|x| x.is_finite()).unwrap_or(0.0).clamp(0.0, 1000.0) as u64;
        // (version 1's fields the first collector dropped)
        if let Some(m) = vobj(&w.loaded) {
            for (k, v) in m.iter().take(MAX_LOADED) {
                if name_ok(k) {
                    let mut e = Mean::default();
                    e.add(v.as_f64());
                    if e.n > 0 {
                        a.loaded.insert(k.clone(), e);
                    }
                }
            }
        }
        if let Some(m) = vobj(&w.lua) {
            a.heap_mb.add(m.get("heapMB").and_then(|x| x.as_f64()));
            a.garbage_mb.add(m.get("garbageMB").and_then(|x| x.as_f64()));
        }
        a.roads_q.add(vnum(&w.roads_q));
        a.map_open = secs(vnum(&w.map_open));
        // version 2
        if let Some(m) = vobj(&w.ft) {
            let mut ft = vec![0u64; FT_BINS];
            for (k, v) in m {
                if let (Ok(i), Some(c)) = (k.parse::<usize>(), v.as_f64()) {
                    if i < FT_BINS && c.is_finite() && c >= 0.0 {
                        ft[i] += c.min(1.0e7) as u64;
                    }
                }
            }
            if ft.iter().any(|c| *c > 0) {
                a.ft = ft;
            }
        }
        let ok_ms = |v: Option<f64>| v.filter(|x| *x > 0.0 && *x <= 600_000.0);
        if let Some(p99) = ok_ms(vnum(&w.p99)) {
            a.v2_windows = 1;
            a.v2_s = a.s;
            a.wp99 = vec![0; WIN_BINS];
            a.wp99[win_bin(p99)] = 1;
            if let Some(p999) = ok_ms(vnum(&w.p999)) {
                a.wp999 = vec![0; WIN_BINS];
                a.wp999[win_bin(p999)] = 1;
            }
            a.p50_ms.add(ok_ms(vnum(&w.p50)));
            let n = |v: &Option<Value>| vnum(v).unwrap_or(0.0).clamp(0.0, 1.0e6) as u64;
            a.over2x = n(&w.over2x);
            a.over4x = n(&w.over4x);
        }
        if let Some(m) = vobj(&w.ctx) {
            for tag in TAGS {
                if let Some(v) = m.get(tag) {
                    let c = vcounts(Some(v), HITCH_BINS);
                    if c.iter().any(|x| *x > 0) {
                        a.ctx.insert(tag.to_string(), c);
                    }
                }
            }
        }
        if let Some(m) = vobj(&w.cause1s) {
            for (k, v) in m.iter().take(MAX_CAUSES_1S) {
                if let Some(n) = v.as_f64().filter(|x| x.is_finite() && *x > 0.0) {
                    if name_ok(k) {
                        a.cause1s.insert(k.clone(), n.min(1.0e6) as u64);
                    }
                }
            }
        }
        if let Some(m) = vobj(&w.tp) {
            for name in TP_NAMES {
                if let Some(sp) = Span::from(m.get(name)) {
                    a.tp.insert(name.to_string(), sp);
                }
            }
        }
        a.idle = Span::from(w.idle.as_ref());
        if let Some(m) = vobj(&w.mach) {
            a.game_mb.add(m.get("gameMB").and_then(|x| x.as_f64()));
            a.free_mb.add(m.get("freeMB").and_then(|x| x.as_f64()));
            a.vram_mb.add(m.get("vramMB").and_then(|x| x.as_f64()));
        }
        a
    }

    /// The numbers a page shows; full: everything (everyone's row), else the compare table's.
    pub fn view(&self, full: bool) -> Value {
        let frames: u64 = self.fh.iter().sum();
        let ft: f64 = self.bands.values().map(|b| b.s).sum();
        let fps = |fh: &[u64], s: f64| -> Value {
            let n: u64 = fh.iter().sum();
            if n == 0 || s <= 0.0 { Value::Null } else { serde_json::json!((n as f64 / s * 10.0).round() / 10.0) }
        };
        let mut bands = serde_json::Map::new();
        for name in BANDS {
            if let Some(b) = self.bands.get(name) {
                if b.s >= 60.0 {
                    let mut v = serde_json::json!({"fps": fps(&b.fh, b.s), "low1": low1(&b.fh), "minutes": (b.s / 60.0).round()});
                    if !b.hit.is_empty() {
                        v["hitchesPerHour"] = per_hour(b.hit[1], b.s);
                        v["h1sPerHour"] = per_hour(b.hit[3], b.s);
                    }
                    bands.insert(name.into(), v);
                }
            }
        }
        let hours = (ft / 3600.0).max(1e-9);
        let total: f64 = self.scene.values().sum();
        let scene: BTreeMap<&String, f64> =
            self.scene.iter().map(|(k, v)| (k, if total > 0.0 { (v / total * 1000.0).round() / 10.0 } else { 0.0 })).collect();
        let nb: u64 = self.blocks.iter().sum();
        let mut causes: Vec<(&String, &u64)> = self.causes.iter().collect();
        causes.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
        causes.truncate(8);
        // (hitches outside the loading after teleports: what driving feels like)
        let driving = {
            let h = self.hit.get(1).copied().unwrap_or(0).saturating_sub(self.hit_load.get(1).copied().unwrap_or(0));
            let dh = ((ft - self.loading) / 3600.0).max(1e-9);
            if ft - self.loading > 60.0 { serde_json::json!((h as f64 / dh * 10.0).round() / 10.0) } else { Value::Null }
        };
        let pct = |v: f64| if self.s > 0.0 { (v / self.s * 1000.0).round() / 10.0 } else { 0.0 };
        let mut v = serde_json::json!({
            "installs": self.installs, "hours": (self.s / 360.0).round() / 10.0,
            "fps": fps(&self.fh, ft), "low1": low1(&self.fh),
            "smooth": if frames > 0 { (self.fh.iter().take(4).sum::<u64>() as f64 / frames as f64 * 1000.0).round() / 10.0 } else { 0.0 },
            "hitchesPerHour": (self.hit.get(1).copied().unwrap_or(0) as f64 / hours * 10.0).round() / 10.0,
            "bigHitchesPerHour": (self.hit.get(2).copied().unwrap_or(0) as f64 / hours * 10.0).round() / 10.0,
            "freezesPerHour": (self.hit.get(3).copied().unwrap_or(0) as f64 / hours * 10.0).round() / 10.0,
            "drivingHitchesPerHour": driving,
            "dropsPerHour": (self.drops as f64 / hours * 10.0).round() / 10.0,
            "edgeHeldPct": pct(self.edge), "groundHoldPct": pct(self.drophold), "loadingPct": pct(self.loading),
            "catchupPct": pct(self.catchup_s), "teleportsPerHour": (self.teleports as f64 / hours * 10.0).round() / 10.0,
            "blockWait": if nb > 0 { serde_json::json!(BLOCK_EDGES[median_bin(&self.blocks)]) } else { Value::Null },
            "blockWaitOver4sPct": if nb > 0 { (self.blocks.iter().skip(4).sum::<u64>() as f64 / nb as f64 * 1000.0).round() / 10.0 } else { 0.0 },
            "bands": bands, "scene": scene, "topCauses": causes,
            // version 2: the windows' own 1 % lows (the median window's: "typical"; the slowest tenth's), the 0.1 %
            // low, the pooled 1 % low from the fine steps, frames over 2x / 4x their window's median per hour
            "v2Hours": (self.v2_s / 360.0).round() / 10.0,
            "low1Typical": fps_of(win_quantile(&self.wp99, 0.5)),
            "low1Worst10": fps_of(win_quantile(&self.wp99, 0.9)),
            "low01Typical": fps_of(win_quantile(&self.wp999, 0.5)),
            "low1Pooled": low1_fine(&self.ft),
            "medianFrameMs": self.p50_ms.get(),
            "over2xPerHour": per_hour(self.over2x, self.v2_s),
            "over4xPerHour": per_hour(self.over4x, self.v2_s),
        });
        if !full {
            return v;
        }
        // everyone's row: what was going on in the slow frames, the 1 s+ causes, since the teleport, idle, memory
        let h100: u64 = self.ctx.values().map(|c| c.get(1).copied().unwrap_or(0)).max().unwrap_or(0);
        let all100 = {
            // (a frame can carry several tags: the share is of the hitches with any context counted, none included)
            let mut n = 0;
            for t in &self.tp {
                n += t.1.hit.get(1).copied().unwrap_or(0);
            }
            n.max(h100)
        };
        let mut ctx: Vec<Value> = self.ctx.iter().map(|(k, c)| {
            let g = |i: usize| c.get(i).copied().unwrap_or(0);
            serde_json::json!({"tag": k, "h50PerHour": per_hour(g(0), self.v2_s), "h100PerHour": per_hour(g(1), self.v2_s),
                               "h250PerHour": per_hour(g(2), self.v2_s), "h1sPerHour": per_hour(g(3), self.v2_s),
                               "pctOf100": if all100 > 0 { (g(1) as f64 / all100 as f64 * 1000.0).round() / 10.0 } else { 0.0 },
                               "n1s": g(3)})
        }).collect();
        ctx.sort_by(|a, b| b["pctOf100"].as_f64().unwrap_or(0.0).total_cmp(&a["pctOf100"].as_f64().unwrap_or(0.0)));
        let mut c1: Vec<(&String, &u64)> = self.cause1s.iter().collect();
        c1.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
        c1.truncate(10);
        let span = |sp: &Span| {
            let g = |i: usize| sp.hit.get(i).copied().unwrap_or(0);
            serde_json::json!({"minutes": (sp.s / 60.0).round(), "h50PerHour": per_hour(g(0), sp.s),
                               "h100PerHour": per_hour(g(1), sp.s), "h250PerHour": per_hour(g(2), sp.s),
                               "h1sPerHour": per_hour(g(3), sp.s)})
        };
        let tp: Vec<Value> = TP_NAMES.iter().filter_map(|n| self.tp.get(*n).map(|sp| {
            let mut x = span(sp);
            x["name"] = serde_json::json!(n);
            x
        })).collect();
        let loaded: BTreeMap<&String, Value> = self.loaded.iter().map(|(k, m)| (k, m.get())).collect();
        v["context"] = serde_json::json!(ctx);
        v["causes1s"] = serde_json::json!(c1);
        v["sinceTeleport"] = serde_json::json!(tp);
        v["idle"] = self.idle.as_ref().map(span).unwrap_or(Value::Null);
        v["loaded"] = serde_json::json!(loaded);
        v["luaHeapMB"] = self.heap_mb.get();
        v["luaGarbageMB"] = self.garbage_mb.get();
        v["roadsQueued"] = self.roads_q.get();
        v["mapOpenPct"] = serde_json::json!(pct(self.map_open));
        v["machine"] = serde_json::json!({"gameMB": self.game_mb.get(), "freeMB": self.free_mb.get(), "vramMB": self.vram_mb.get()});
        v["server"] = serde_json::json!({"memMB": self.srv_mem.get(), "cores": self.srv_cores.get(), "workers": self.srv_workers.get()});
        v
    }
}

/// Whether a build's perf reports are counted: from the counting fix on (mod fe802694, dev.249). The first dev builds
/// (1.8.2-beta.dev.245 to .248) counted frames while paused or in the background as hitches and only the guard's full
/// pause as held at the edge; their reports are dropped, as are any older release's.
pub const FIRST_COUNTED_DEV: u64 = 249;
pub fn counted_version(v: &str) -> bool {
    let (base, dev) = match v.split_once(".dev.") {
        Some((b, d)) => (b, d.parse::<u64>().ok()),
        None => (v, None),
    };
    let nums: Vec<u64> = base.split(|c: char| !c.is_ascii_digit()).filter(|x| !x.is_empty()).take(3)
        .map(|x| x.parse().unwrap_or(0)).collect();
    let triple = (nums.first().copied().unwrap_or(0), nums.get(1).copied().unwrap_or(0), nums.get(2).copied().unwrap_or(0));
    match triple.cmp(&(1, 8, 2)) {
        std::cmp::Ordering::Less => false,
        std::cmp::Ordering::Greater => true,
        std::cmp::Ordering::Equal => dev.map_or(false, |n| n >= FIRST_COUNTED_DEV),
    }
}

/// The 1 % low as fps: the frame time the slowest 1 % of frames reach, placed linearly inside its bin (the first
/// night's reports, read as the bin's upper edge, could only say 4, 10, 20 or 29 fps); the slowest bin: under 4 fps.
pub fn low1(fh: &[u64]) -> Value {
    let n: u64 = fh.iter().sum();
    if n == 0 {
        return Value::Null;
    }
    let want = (n as f64 * 0.01).ceil() as u64;
    let mut acc = 0;
    for i in (0..fh.len()).rev() {
        if acc + fh[i] >= want {
            if i >= EDGES_MS.len() {
                return serde_json::json!(4);
            }
            let (hi, lo) = (EDGES_MS[i], if i > 0 { EDGES_MS[i - 1] } else { 1000.0 / 240.0 });
            let f = (want - acc) as f64 / fh[i] as f64;          // (from the bin's slow end)
            let ms = hi - f * (hi - lo);
            return serde_json::json!((1000.0 / ms).round() as u64);
        }
        acc += fh[i];
    }
    Value::Null
}

/// The pooled 1 % low (fps) from the fine steps (version 2 windows), placed geometrically inside its bin; over the
/// last edge: under 0.5 fps.
pub fn low1_fine(ft: &[u64]) -> Value {
    let n: u64 = ft.iter().sum();
    if n == 0 {
        return Value::Null;
    }
    let want = (n as f64 * 0.01).ceil() as u64;
    let mut acc = 0;
    for i in (0..ft.len()).rev() {
        if acc + ft[i] >= want {
            if i >= FT_EDGES_MS.len() {
                return serde_json::json!(0.5);
            }
            let (hi, lo) = (FT_EDGES_MS[i], if i > 0 { FT_EDGES_MS[i - 1] } else { 2.0 });
            let f = (want - acc) as f64 / ft[i] as f64;
            let ms = hi * (lo / hi).powf(f);
            return serde_json::json!((1000.0 / ms * 10.0).round() / 10.0);
        }
        acc += ft[i];
    }
    Value::Null
}

fn median_bin(b: &[u64]) -> usize {
    let n: u64 = b.iter().sum();
    let mut acc = 0;
    for (i, v) in b.iter().enumerate() {
        acc += v;
        if acc * 2 >= n {
            return i.min(BLOCK_EDGES.len() - 1);
        }
    }
    0
}

fn name_ok(k: &str) -> bool {
    !k.is_empty() && k.len() <= 32 && k.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'+' | b'.' | b':' | b'-'))
}

fn label(s: &Option<String>, max: usize) -> Option<String> {
    let t: String = s.as_deref().unwrap_or("").chars().filter(|c| c.is_ascii_graphic() || *c == ' ').take(max).collect();
    let t = t.split_whitespace().collect::<Vec<_>>().join(" ");
    if t.is_empty() { None } else { Some(t) }
}

#[derive(Serialize, Deserialize, Default)]
struct DayFile {
    day: u64,
    groups: BTreeMap<String, Agg>,
    errors: BTreeMap<String, u64>,
    #[serde(default)]
    codes: BTreeMap<String, BTreeMap<String, u64>>,
}

fn fingerprint_ok(k: &str) -> bool {
    k.len() == 8 && k.bytes().all(|b| b.is_ascii_hexdigit())
}

fn code_ok(c: &str) -> bool {
    !c.is_empty() && c.len() <= 40 && c.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b':' | b'_' | b'-'))
}

struct Setup {
    at: Instant,
    groups: Vec<String>,
    ram: Option<String>, // (the cross-tab's RAM tier)
}

/// The cross-tab's groups: "xt:<version>|<RAM>|<session length>"
const XT: &str = "xt:";

pub struct Perf {
    days: BTreeMap<u64, DayFile>,
    setups: HashMap<String, Setup>,
    salt: u64,
    dir: Option<PathBuf>,
    saved_at: u64,
    dirty: bool,
}

impl Perf {
    pub fn new(dir: Option<PathBuf>, salt: u64) -> Perf {
        let mut p = Perf { days: BTreeMap::new(), setups: HashMap::new(), salt, dir, saved_at: 0, dirty: false };
        p.load();
        p
    }

    fn load(&mut self) {
        let Some(dir) = &self.dir else { return };
        let Ok(rd) = fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if let Some(d) = name.strip_suffix(".json").and_then(|d| d.parse::<u64>().ok()) {
                if let Ok(text) = fs::read_to_string(e.path()) {
                    if let Ok(f) = serde_json::from_str::<DayFile>(&text) {
                        self.days.insert(d, f);
                    }
                }
            }
        }
    }

    fn save(&mut self, unix_now: u64) {
        let Some(dir) = self.dir.clone() else { return };
        let _ = fs::create_dir_all(&dir);
        let today = unix_now / 86400;
        for d in [today.saturating_sub(1), today] {
            if let Some(f) = self.days.get(&d) {
                let tmp = dir.join(format!("{d}.json.tmp"));
                if fs::write(&tmp, serde_json::to_string(f).unwrap()).is_ok() {
                    let _ = fs::rename(&tmp, dir.join(format!("{d}.json")));
                }
            }
        }
        let cut = today.saturating_sub(DAY_KEEP);
        let old: Vec<u64> = self.days.keys().copied().filter(|d| *d < cut).collect();
        for d in old {
            self.days.remove(&d);
            let _ = fs::remove_file(dir.join(format!("{d}.json")));
        }
        self.saved_at = unix_now;
        self.dirty = false;
    }

    /// Every few seconds (main): today's file now and then, setups of installs gone quiet forgotten.
    pub fn tick(&mut self, now: Instant, unix_now: u64) {
        self.setups.retain(|_, s| now.duration_since(s.at) < SETUP_TTL);
        if self.dirty && unix_now.saturating_sub(self.saved_at) >= SAVE_S {
            self.save(unix_now);
        }
    }

    pub fn flush(&mut self, unix_now: u64) {
        if self.dirty {
            self.save(unix_now);
        }
    }

    /// A setup's groups: its machine and settings.
    fn setup_groups(s: &SetupIn) -> (Vec<String>, Option<String>) {
        let mut groups = Vec::new();
        if let Some(g) = label(&s.gpu, 80) { groups.push(format!("gpu:{g}")); }
        if let Some(c) = label(&s.cpu, 80) { groups.push(format!("cpu:{c}")); }
        if let Some(r) = label(&s.res, 12) { groups.push(format!("res:{r}")); }
        let ram = s.ram_gb.filter(|x| x.is_finite() && *x > 0.0 && *x < 4096.0).map(|r| format!("{} GB", r as u64));
        if let Some(r) = &ram { groups.push(format!("ram:{r}")); }
        let ms = s.mod_settings.as_ref().and_then(|m| m.as_object());
        let preset = ms.and_then(|m| m.get("quality")).and_then(|q| q.as_str()).map(|q| q.to_string());
        if let Some(q) = label(&preset, 16) { groups.push(format!("preset:{q}")); }
        if let Some(p) = on_off(ms.and_then(|m| m.get("framePacing"))) { groups.push(format!("pacing:{p}")); }
        if let Some(p) = on_off(ms.and_then(|m| m.get("teleportPriority"))) { groups.push(format!("tpprio:{p}")); }
        if let Some(v) = vnum(&s.vram_gb).filter(|x| *x > 0.0 && *x < 1024.0) { groups.push(format!("vram:{} GB", v as u64)); }
        if let Some(a) = vstr(&s.api, 16) { groups.push(format!("api:{a}")); }
        if let Some(h) = vnum(&s.hz).filter(|x| *x > 0.0 && *x < 1000.0) { groups.push(format!("hz:{} Hz", h as u64)); }
        let vsync = s.game_settings.as_ref().and_then(|g| g.as_object()).and_then(|g| g.get("vsync"));
        if let Some(v) = on_off(vsync) { groups.push(format!("vsync:{v}")); }
        if let Some(m) = vnum(&s.other_mods).filter(|x| *x >= 0.0 && *x < 10000.0) { groups.push(format!("mods:{}", m as u64)); }
        let _ = &s.os;
        (groups, ram)
    }

    /// A report's perf section from install id (already checked by telemetry.rs), its version and OS.
    pub fn add(&mut self, id: &str, v: &str, os: &str, p: PerfIn, now: Instant, unix_now: u64) {
        if !counted_version(v) {
            return;
        }
        if let Some(s) = &p.setup {
            let (groups, ram) = Self::setup_groups(s);
            self.setups.insert(id.to_string(), Setup { at: now, groups, ram });
        } else if let Some(s) = self.setups.get_mut(id) {
            s.at = now;
        }
        let day = unix_now / 86400;
        let key = {
            let mut h = DefaultHasher::new();
            (self.salt, day, id).hash(&mut h);
            h.finish()
        };
        let mut base: Vec<String> = vec!["all".into()];
        if !v.is_empty() { base.push(format!("v:{v}")); }
        if !os.is_empty() { base.push(format!("os:{os}")); }
        let ram = self.setups.get(id).and_then(|s| s.ram.clone());
        if let Some(s) = self.setups.get(id) { base.extend(s.groups.iter().cloned()); }
        // the server's process tree over this report: its own numbers and groups for the report's windows
        let srv = p.server.as_ref();
        let srv_mem = srv.and_then(|s| vnum(&s.mem_mb)).filter(|x| *x >= 0.0 && *x < 1.0e7);
        let srv_cores = srv.and_then(|s| vnum(&s.cores)).filter(|x| *x >= 0.0 && *x < 1024.0);
        let srv_workers = srv.and_then(|s| vnum(&s.workers)).filter(|x| *x >= 0.0 && *x < 1024.0);
        if let Some(m) = srv_mem { base.push(format!("srvmem:{}", mb_label(m))); }
        if let Some(w) = srv_workers { base.push(format!("workers:{}", w as u64)); }
        let f = self.days.entry(day).or_insert_with(|| DayFile { day, ..Default::default() });
        let touch = |f: &mut DayFile, g: &str, a: &Agg| {
            if f.groups.len() >= MAX_GROUPS && !f.groups.contains_key(g) {
                return;
            }
            let e = f.groups.entry(g.to_string()).or_default();
            e.merge(a);
            if e.seen.insert(key) {
                e.installs = e.installs.max(e.seen.len() as u32);
            }
        };
        for w in p.game.unwrap_or_default().iter().take(MAX_WINDOWS) {
            let a = Agg::window(w);
            let main_scene = a.scene.iter().max_by(|x, y| x.1.total_cmp(y.1)).map(|(k, _)| k.clone());
            for g in &base { touch(f, g, &a); }
            if let Some(sc) = main_scene { touch(f, &format!("scene:{sc}"), &a); }
            let age = w.age.and_then(|m| AGES.iter().find(|(lo, _)| *lo == m)).map(|x| x.1);
            if let Some(age) = age {
                touch(f, &format!("age:{age}"), &a);
                if let (Some(r), false) = (&ram, v.is_empty()) {
                    touch(f, &format!("{XT}{}|{r}|{age}", label(&Some(v.to_string()), 40).unwrap_or_default()), &a);
                }
            }
            // the window's memory state as groups (MB steps' lower edges): how the game runs by it
            if a.game_mb.n > 0 { touch(f, &format!("gamemem:{}", mb_label(a.game_mb.sum)), &a); }
            if a.free_mb.n > 0 { touch(f, &format!("freemem:{}", mb_label(a.free_mb.sum)), &a); }
            if a.vram_mb.n > 0 { touch(f, &format!("vramuse:{}", mb_label(a.vram_mb.sum)), &a); }
        }
        if let Some(s) = &p.server {
            let mut a = Agg { catchups: s.catchups.filter(|x| x.is_finite()).unwrap_or(0.0).clamp(0.0, 1000.0) as u64,
                              catchup_s: secs(s.catchup_s), ..Default::default() };
            a.srv_mem.add(srv_mem);
            a.srv_cores.add(srv_cores);
            a.srv_workers.add(srv_workers);
            if a.catchups > 0 || a.catchup_s > 0.0 || a.srv_mem.n + a.srv_cores.n + a.srv_workers.n > 0 {
                for g in &base { touch(f, g, &a); }
            }
            if let Some(errs) = &s.errors {
                for (k, n) in errs.iter().take(32) {
                    if fingerprint_ok(k) && (f.errors.len() < MAX_ERRORS || f.errors.contains_key(k)) {
                        *f.errors.entry(k.to_ascii_lowercase()).or_default() += n.clamp(0.0, 1.0e6) as u64;
                    }
                }
            }
            if let Some(codes) = &s.error_codes {
                for (k, by) in codes.iter().take(32) {
                    if !fingerprint_ok(k) || !f.errors.contains_key(&k.to_ascii_lowercase()) {
                        continue;
                    }
                    let e = f.codes.entry(k.to_ascii_lowercase()).or_default();
                    for (c, n) in by.iter().take(4) {
                        if code_ok(c) && (e.len() < 8 || e.contains_key(c)) {
                            *e.entry(c.clone()).or_default() += n.clamp(0.0, 1.0e6) as u64;
                        }
                    }
                }
            }
        }
        self.dirty = true;
    }

    /// The groups over the last `days` days (0: all kept), merged; installs: the most on one day (a group counts as
    /// K-anonymous when one day had K_ANON installs in it).
    fn merged(&self, unix_now: u64, days: u64) -> BTreeMap<String, Agg> {
        let today = unix_now / 86400;
        let from = if days == 0 { 0 } else { today.saturating_sub(days - 1) };
        let mut out: BTreeMap<String, Agg> = BTreeMap::new();
        for f in self.days.range(from..=today).map(|x| x.1) {
            for (g, a) in &f.groups {
                let e = out.entry(g.clone()).or_default();
                e.merge(a);
                e.installs = e.installs.max(a.installs);
            }
        }
        out
    }

    pub fn public(&self, unix_now: u64, days: u64) -> Value {
        let m = self.merged(unix_now, days);
        let mut dims: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        let mut other: BTreeMap<String, Agg> = BTreeMap::new();
        let mut all = Value::Null;
        let mut crosstab: Vec<Value> = Vec::new();
        for (g, a) in &m {
            if g == "all" {
                if a.installs >= K_ANON { all = a.view(true); }
                continue;
            }
            if let Some(cell) = g.strip_prefix(XT) {
                // (version x RAM x session length: cells under K_ANON are left out, not summed)
                let parts: Vec<&str> = cell.splitn(3, '|').collect();
                if a.installs >= K_ANON && parts.len() == 3 {
                    let full = a.view(false);
                    crosstab.push(serde_json::json!({"v": parts[0], "ram": parts[1], "age": parts[2],
                        "installs": a.installs, "hours": full["hours"], "fps": full["fps"], "low1": full["low1"],
                        "low1Typical": full["low1Typical"], "hitchesPerHour": full["hitchesPerHour"],
                        "bigHitchesPerHour": full["bigHitchesPerHour"], "freezesPerHour": full["freezesPerHour"]}));
                }
                continue;
            }
            let Some((dim, name)) = g.split_once(':') else { continue };
            if a.installs >= K_ANON {
                let mut v = a.view(false);
                v["name"] = serde_json::json!(name);
                dims.entry(dim.to_string()).or_default().push(v);
            } else {
                let o = other.entry(dim.to_string()).or_default();
                o.merge(a);
                o.installs += a.installs;
            }
        }
        for (dim, o) in other {
            if o.installs >= K_ANON {
                let mut v = o.view(false);
                v["name"] = serde_json::json!("other");
                dims.entry(dim).or_default().push(v);
            }
        }
        for list in dims.values_mut() {
            list.sort_by(|a, b| b["hours"].as_f64().unwrap_or(0.0).total_cmp(&a["hours"].as_f64().unwrap_or(0.0)));
        }
        crosstab.sort_by(|a, b| {
            let k = |x: &Value| (x["v"].as_str().unwrap_or("").to_string(), x["ram"].as_str().unwrap_or("").to_string());
            k(b).0.cmp(&k(a).0).then(k(a).1.cmp(&k(b).1)).then(a["age"].as_str().cmp(&b["age"].as_str()))
        });
        let first = self.days.keys().next().map(|d| d * 86400);
        serde_json::json!({"days": days, "kAnon": K_ANON, "all": all, "groups": dims, "crosstab": crosstab,
                           "dataSince": first, "bands": BANDS, "frameEdgesMs": EDGES_MS, "fineEdgesMs": FT_EDGES_MS,
                           "ages": AGES.iter().map(|x| x.1).collect::<Vec<_>>()})
    }

    /// The advanced page's part: the error fingerprints of the last 7 days: [fingerprint, count, {code: count}].
    pub fn errors(&self, unix_now: u64) -> Value {
        let today = unix_now / 86400;
        let mut e: BTreeMap<&String, u64> = BTreeMap::new();
        let mut c: BTreeMap<&String, BTreeMap<&String, u64>> = BTreeMap::new();
        for f in self.days.range(today.saturating_sub(6)..=today).map(|x| x.1) {
            for (k, n) in &f.errors {
                *e.entry(k).or_default() += n;
            }
            for (k, by) in &f.codes {
                for (code, n) in by {
                    *c.entry(k).or_default().entry(code).or_default() += n;
                }
            }
        }
        let mut v: Vec<(&String, u64)> = e.into_iter().collect();
        v.sort_by(|a, b| b.1.cmp(&a.1));
        v.truncate(50);
        let empty = BTreeMap::new();
        serde_json::json!(v.into_iter().map(|(k, n)| serde_json::json!([k, n, c.get(k).unwrap_or(&empty)])).collect::<Vec<_>>())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_counted_from_the_fix() {
        assert!(!counted_version("1.8.2-beta.dev.245"));
        assert!(!counted_version("1.8.2-beta.dev.248"));
        assert!(counted_version("1.8.2-beta.dev.249"));
        assert!(counted_version("1.8.2-beta.dev.1000"));
        assert!(!counted_version("1.8.2-beta"));         // (the release: before the fix)
        assert!(!counted_version("1.7.3-beta.dev.900"));
        assert!(counted_version("1.8.3-beta"));
        assert!(counted_version("1.9.0-beta.dev.1"));
        assert!(!counted_version(""));
        assert!(!counted_version("garbage"));
    }

    #[test]
    fn low1_inside_its_bin() {
        // 1000 frames: 990 at 60 fps (bin 3, 14-17 ms), 10 in the 50-100 ms bin: the 1 % point is that bin's fast end
        let mut fh = [0u64; BINS];
        fh[3] = 990;
        fh[7] = 10;
        assert_eq!(low1(&fh), serde_json::json!(20));
        // 5 slow (50-100 ms) and 10 at 17-22 ms: the 1 % point halfway into that bin, 19.5 ms: 51 fps, not its edge's 45
        fh[7] = 5;
        fh[4] = 10;
        fh[3] = 985;
        assert_eq!(low1(&fh), serde_json::json!(51));
        // the slowest bin: under 4 fps
        let mut slow = [0u64; BINS];
        slow[BINS - 1] = 3;
        slow[0] = 97;
        assert_eq!(low1(&slow), serde_json::json!(4));
    }

    fn report(gpu: &str, fps60: u64, with_setup: bool) -> PerfIn {
        let w: WindowIn = serde_json::from_value(serde_json::json!({
            "v": 1, "s": 300, "fh": [0, 0, 0, fps60, 0, 0, 0, 1, 0, 0], "hit": [1, 1, 0, 0], "hitLoad": [1, 0, 0, 0],
            "bands": {"s60": {"s": 300, "fh": [0, 0, 0, fps60, 0, 0, 0, 1, 0, 0]}}, "scene": {"urban": 250, "town": 50},
            "held": {"edge": 15}, "blocks": [3, 1, 0, 0, 0, 0, 0, 0], "causes": {"terrainCommit": 1, "bad key!": 4},
            "lat": 39.2, "age": if with_setup { 60 } else { 45 }})).unwrap();
        let setup: Option<SetupIn> = if with_setup {
            Some(serde_json::from_value(serde_json::json!({"gpu": gpu, "cpu": "AMD Ryzen 5 3600", "ramGB": 32, "res": "1440p",
                "modSettings": {"quality": "high"}})).unwrap())
        } else { None };
        PerfIn { setup, game: Some(vec![w]), server: Some(serde_json::from_value(serde_json::json!(
            {"catchups": 1, "catchupS": 30, "errors": {"a3f9c2e1": 2, "not-hex!": 9},
             "errorCodes": {"a3f9c2e1": {"OSError:24": 1, "bad code!": 5}, "not-hex!": {"x": 1}}})).unwrap()) }
    }

    #[test]
    fn folds_into_groups_and_hides_small_ones() {
        let mut p = Perf::new(None, 7);
        let now = Instant::now();
        let u = 1_800_000_000;
        for k in 0..6 {
            let gpu = if k < 5 { "NVIDIA GeForce RTX 3060" } else { "Rare GPU 9000" };
            p.add(&format!("{:032x}", k), "1.9.0", "windows", report(gpu, 17999, true), now, u);
        }
        // a second window from the first install without a setup: still its groups
        p.add(&format!("{:032x}", 0), "1.9.0", "windows", report("", 17999, false), now, u + 300);
        let v = p.public(u + 600, 7);
        assert_eq!(v["all"]["installs"], 6);
        assert!((v["all"]["fps"].as_f64().unwrap() - 60.0).abs() < 0.5);
        assert_eq!(v["all"]["low1"], 59);       // (the 1 % in the 14-17 ms bin: 59 fps)
        assert_eq!(v["all"]["edgeHeldPct"], 5.0);
        assert_eq!(v["all"]["blockWait"], "0.5 s");
        assert_eq!(v["all"]["drivingHitchesPerHour"], 12.0);    // (1 a 300 s window, none of them while loading)
        let gpus = v["groups"]["gpu"].as_array().unwrap();
        assert_eq!(gpus.len(), 1);              // the rare GPU alone is under K_ANON and "other" has 1 install: hidden
        assert_eq!(gpus[0]["name"], "NVIDIA GeForce RTX 3060");
        assert_eq!(v["groups"]["preset"][0]["name"], "high");
        assert_eq!(v["groups"]["scene"][0]["name"], "urban");
        let ages = v["groups"]["age"].as_array().unwrap();     // (a band it does not know: no group)
        assert_eq!(ages.len(), 1);
        assert_eq!(ages[0]["name"], "u120");
        let causes = v["all"]["topCauses"].as_array().unwrap();
        assert!(causes.iter().all(|c| c[0] != "bad key!"));
        let e = p.errors(u + 600);
        assert_eq!(e, serde_json::json!([["a3f9c2e1", 14, {"OSError:24": 7}]]));
        assert!(!serde_json::to_string(&v).unwrap().contains("39.2"));
    }

    #[test]
    fn days_survive_a_restart_without_ids() {
        let dir = std::env::temp_dir().join(format!("bnge_perf_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let u = 1_800_000_000;
        {
            let mut p = Perf::new(Some(dir.clone()), 1);
            for k in 0..5 {
                p.add(&format!("{:032x}", k + 100), "1.9.0", "linux", report("AMD Radeon RX 7800 XT", 9000, true), Instant::now(), u);
            }
            p.flush(u);
        }
        let text = fs::read_to_string(dir.join(format!("{}.json", u / 86400))).unwrap();
        assert!(!text.contains(&format!("{:032x}", 100)));      // no install id on disk
        let p = Perf::new(Some(dir.clone()), 2);
        assert_eq!(p.public(u, 7)["all"]["installs"], 5);
        let _ = fs::remove_dir_all(&dir);
    }

    /// A version 2 window as perfsummary.lua sends it (through perfstats.py), steady at p99_ms with `slow` 120 ms frames.
    fn v2_window(p99_ms: f64, slow: u64, age: u64) -> Value {
        serde_json::json!({
            "v": 2, "s": 300, "fh": [0, 0, 0, 17000, 0, 0, 0, slow, 0, 0], "hit": [slow, slow, 0, 0], "hitLoad": [0, 0, 0, 0],
            "ft": {"5": 17000, "13": slow}, "p50": 16.625, "p99": p99_ms, "p999": 120.125, "over2x": slow, "over4x": slow,
            "ctx": {"swap": [slow, slow, 0, 0], "far": [1, 0, 0, 0], "bogus": [9, 9, 9, 9]},
            "cause1s": {"FramePresent": 1, "a b": 4},
            "tp": {"t10": [10, 1, 1, 0, 0], "long": [290, slow, slow, 0, 1]}, "idle": [60, 0, 0, 0, 0],
            "bands": {"s60": {"s": 300, "fh": [0, 0, 0, 17000, 0, 0, 0, slow, 0, 0], "hit": [slow, slow, 0, 0]}},
            "scene": {"urban": 300}, "held": {"edge": 0}, "blocks": [2, 0, 0, 0, 0, 0, 0, 0], "causes": {"Render": slow},
            "loaded": {"trees": 10000, "roadTiles": 50}, "lua": {"heapMB": 256, "garbageMB": 128}, "roadsQueuedP90": 10,
            "mapOpen": 30, "mach": {"gameMB": 6144, "freeMB": 2048, "vramMB": 8192}, "age": age})
    }

    fn v2_setup(ram: u64) -> Value {
        serde_json::json!({"v": 2, "cpu": "AMD Ryzen 5 3600", "gpu": "NVIDIA GeForce RTX 3060", "vramGB": 12, "ramGB": ram,
            "api": "DX11", "hz": 144, "res": "1440p", "otherMods": 2, "gameSettings": {"vsync": false},
            "modSettings": {"quality": "high", "framePacing": true, "teleportPriority": false}})
    }

    fn v2_report(ram: u64, p99: f64, slow: u64, setup: bool) -> PerfIn {
        let mut p = serde_json::json!({"game": [v2_window(p99, slow, 60)],
                                       "server": {"catchups": 0, "catchupS": 0, "cores": 1.5, "memMB": 4096, "workers": 8}});
        if setup { p["setup"] = v2_setup(ram); }
        serde_json::from_value(p).unwrap()
    }

    #[test]
    fn version_2_windows_and_setups() {
        let mut p = Perf::new(None, 3);
        let now = Instant::now();
        let u = 1_800_000_000;
        for k in 0..6 {
            p.add(&format!("{:032x}", k), "1.9.2-beta", "windows", v2_report(16, 18.0, 10, true), now, u);
        }
        let v = p.public(u + 60, 7);
        let a = &v["all"];
        assert_eq!(a["installs"], 6);
        let typical = a["low1Typical"].as_f64().unwrap();       // (p99 18 ms, 55.6 fps: its 15 % bin's middle)
        assert!((52.0..=62.0).contains(&typical), "{typical}");
        assert!(a["low1Pooled"].as_f64().unwrap() > 50.0);
        assert_eq!(a["over2xPerHour"], 120.0);
        assert_eq!(a["medianFrameMs"], 16.6);
        assert_eq!(a["freezesPerHour"], 0.0);
        let ctx = a["context"].as_array().unwrap();
        assert_eq!(ctx[0]["tag"], "swap");
        assert_eq!(ctx[0]["h100PerHour"], 120.0);
        assert!(ctx.iter().all(|c| c["tag"] != "bogus"));
        assert_eq!(a["causes1s"], serde_json::json!([["FramePresent", 6]]));
        let tp = a["sinceTeleport"].as_array().unwrap();
        assert_eq!(tp[0]["name"], "t10");
        assert_eq!(tp[1]["name"], "long");
        assert_eq!(a["idle"]["minutes"], 6.0);
        assert_eq!(a["loaded"]["trees"], 10000.0);
        assert_eq!(a["luaHeapMB"], 256.0);
        assert_eq!(a["mapOpenPct"], 10.0);
        assert_eq!(a["machine"]["freeMB"], 2048.0);
        assert_eq!(a["server"]["workers"], 8.0);
        assert_eq!(a["bands"]["s60"]["hitchesPerHour"], 120.0);
        let g = &v["groups"];
        for (dim, name) in [("vram", "12 GB"), ("api", "DX11"), ("vsync", "off"), ("hz", "144 Hz"), ("mods", "2"),
                            ("pacing", "on"), ("tpprio", "off"), ("gamemem", "6 GB"), ("freemem", "2 GB"),
                            ("vramuse", "8 GB"), ("srvmem", "4 GB"), ("workers", "8"), ("age", "u120")] {
            assert_eq!(g[dim][0]["name"], name, "{dim}");
        }
        let xt = v["crosstab"].as_array().unwrap();
        assert_eq!(xt.len(), 1);
        assert_eq!((xt[0]["v"].as_str(), xt[0]["ram"].as_str(), xt[0]["age"].as_str()),
                   (Some("1.9.2-beta"), Some("16 GB"), Some("u120")));
        assert!(g.get("xt").is_none());
    }

    #[test]
    fn typical_low_is_the_median_windows_not_the_slowest_machine() {
        // five installs at a steady 60 fps (p99 17.5 ms), one weak machine at a steady 25 fps (p99 41 ms) for almost as
        // many windows as all of them together: pooled, its frames set everyone's 1 % low; per window, the median is 60's
        let mut p = Perf::new(None, 4);
        let now = Instant::now();
        let u = 1_800_000_000;
        for k in 0..5 {
            p.add(&format!("{:032x}", k), "1.9.2-beta", "windows", v2_report(32, 17.5, 0, true), now, u);
        }
        for _ in 0..4 {
            let mut w = v2_window(41.0, 0, 60);
            w["ft"] = serde_json::json!({"9": 7500});
            w["fh"] = serde_json::json!([0, 0, 0, 0, 0, 0, 7500, 0, 0, 0]);
            let r: PerfIn = serde_json::from_value(serde_json::json!({"setup": v2_setup(8), "game": [w]})).unwrap();
            p.add(&format!("{:032x}", 99), "1.9.2-beta", "windows", r, now, u);
        }
        let a = &p.public(u, 7)["all"];
        let typical = a["low1Typical"].as_f64().unwrap();
        let worst = a["low1Worst10"].as_f64().unwrap();
        let pooled = a["low1Pooled"].as_f64().unwrap();
        assert!(typical > 50.0, "typical {typical}");
        assert!(worst < 30.0, "the slowest tenth of windows {worst}");
        assert!(pooled < 30.0, "pooled {pooled}");
    }

    #[test]
    fn old_clients_and_odd_shapes_are_still_read() {
        // a version 1 report exactly as the dev.249+ builds send it, through the whole Report
        let old = r#"{"id": "0123456789abcdef0123456789abcdef", "v": "1.9.0-beta", "os": "windows", "game": true,
            "perf": {"setup": {"v": 1, "cpu": "Intel Core i5", "gpu": "GTX 1060", "ramGB": 16, "modSettings": {"quality": "medium"},
                               "gameSettings": {"vsync": 1}, "vramGB": 6, "api": "DX11", "hz": 60, "otherMods": 0},
                     "game": [{"v": 1, "s": 300, "fh": [0, 0, 0, 9000, 0, 0, 0, 3, 0, 0], "hit": [3, 3, 0, 0],
                               "hitLoad": [0, 0, 0, 0], "causes": {"Lua": 3}, "teleports": 0, "blocks": [1, 0, 0, 0, 0, 0, 0, 0],
                               "bands": {"s30": {"s": 300, "fh": [0, 0, 0, 9000, 0, 0, 0, 3, 0, 0]}}, "scene": {"town": 300},
                               "loaded": {"trees": 5000}, "lua": {"heapMB": 512, "garbageMB": 64}, "held": {"edge": 5},
                               "roadsQueuedP90": 1, "mapOpen": 0, "age": 30}],
                     "server": {"catchupS": 0, "catchups": 0, "cores": 1, "memMB": 3072}}}"#;
        let mut r: crate::telemetry::Report = serde_json::from_str(old).expect("an old report");
        let mut p = Perf::new(None, 5);
        let perf = r.perf.take().unwrap();
        p.add("0123456789abcdef0123456789abcdef", "1.9.0-beta", "windows", perf, Instant::now(), 1_800_000_000);
        let d = &p.days[&(1_800_000_000 / 86400)];
        let a = &d.groups["all"];
        assert_eq!((a.windows, a.v2_windows, a.fh[3]), (1, 0, 9000));
        assert!(a.ft.is_empty() && a.wp99.is_empty());
        assert_eq!(a.loaded["trees"].sum, 5000.0);
        assert!(d.groups.contains_key("vsync:on") && d.groups.contains_key("vram:6 GB") && d.groups.contains_key("srvmem:3 GB"));
        let v = a.view(true);
        assert!(v["low1Typical"].is_null() && v["fps"].as_f64().unwrap() > 29.0);
        // Lua's empty tables arrive as [] and fields of an unexpected type: skipped, the report kept
        let odd = r#"{"id": "0123456789abcdef0123456789abcdee", "v": "1.9.2-beta",
            "perf": {"setup": {"modSettings": [], "gameSettings": [], "vramGB": "x", "api": 5},
                     "game": [{"s": 300, "fh": [1, 2], "ctx": [], "ft": [], "tp": "no", "idle": {"a": 1}, "mach": [],
                               "p99": "fast", "cause1s": [], "loaded": [], "lua": 4,
                               "bands": {"s60": {"s": 300, "fh": [1], "hit": {"x": 1}}}}]}}"#;
        let mut r: crate::telemetry::Report = serde_json::from_str(odd).expect("odd shapes still a report");
        p.add("0123456789abcdef0123456789abcdee", "1.9.2-beta", "linux", r.perf.take().unwrap(), Instant::now(), 1_800_000_000);
        assert_eq!(p.days[&(1_800_000_000 / 86400)].groups["all"].windows, 2);
        // a day file written by the first collector (no version 2 fields) loads
        let dir = std::env::temp_dir().join(format!("bnge_perf_old_{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        fs::write(dir.join("20833.json"), r#"{"day": 20833, "groups": {"all": {"installs": 7, "windows": 3, "s": 900,
            "fh": [0,0,0,3,0,0,0,0,0,0], "hit": [0,0,0,0], "bands": {"s60": {"s": 900, "fh": [0,0,0,3,0,0,0,0,0,0]}},
            "causes": {}, "scene": {}, "edge": 0, "drophold": 0, "loading": 0, "drops": 0, "drop_s": 0,
            "blocks": [0,0,0,0,0,0,0,0], "teleports": 0, "catchups": 0, "catchup_s": 0}}, "errors": {}}"#).unwrap();
        let p2 = Perf::new(Some(dir.clone()), 6);
        assert_eq!(p2.public(20833 * 86400, 1)["all"]["installs"], 7);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn win_bins_and_quantiles() {
        assert_eq!(win_bin(3.0), 0);
        assert_eq!(win_bin(4.0), 1);
        assert_eq!(win_bin(1.0e6), WIN_BINS - 1);
        let mut h = vec![0u64; WIN_BINS];
        h[win_bin(16.7)] = 9;
        h[win_bin(100.0)] = 1;
        let med = win_quantile(&h, 0.5).unwrap();
        let (lo, hi) = win_edges(win_bin(16.7));
        assert!(med >= lo && med <= hi && lo <= 16.7 && hi > 16.7);
        assert!(win_quantile(&h, 0.95).unwrap() > 80.0);
        assert_eq!(mb_label(1536.0), "1.5 GB");
        assert_eq!(mb_label(512.0), "512 MB");
        assert_eq!(mb_label(8192.0), "8 GB");
    }
}
