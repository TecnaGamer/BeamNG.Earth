//! The anonymous performance data (TELEMETRY.md sections 2-5; the app's server/perfstats.py and the mod's
//! perfsummary.lua): a report's "perf" section is folded into daily totals per group (everyone, an app version, a GPU
//! model, a CPU model, a BeamNG.Earth quality preset, a screen class, an OS, a RAM size, the window's main scene) and
//! then dropped: no report is kept. An install's setup (its machine and settings, sent once a session) is held in
//! memory only, to know which groups its windows count for; the install id is never written next to the numbers (a
//! group's distinct installs are counted through a keyed hash whose key lives in memory).
//!
//! GET /stats/performance?days=N (1-400, default 7; 0: everything kept) public: per dimension, every group with at least
//! K_ANON installs on one day of the range (the rest summed as "other", shown when that has K_ANON too), with its fps,
//! 1 % low, smooth share, hitches per hour, per speed band fps, the time held at the edge of the ground and the
//! catch-up share, the block waits and the scene shares. The error fingerprints are on /stats/advanced only.
//!
//! Kept in BNGE_TELEMETRY_DIR/perf/<unix day>.json (one file a day, today's rewritten every SAVE_S), DAY_KEEP days.

use serde::{Deserialize, Serialize};
use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::time::{Duration, Instant};

pub const BINS: usize = 10; // perfsummary.lua FRAME_MS (9 upper edges) + the slowest bin
pub const EDGES_MS: [f64; 9] = [8.0, 11.0, 14.0, 17.0, 22.0, 34.0, 50.0, 100.0, 250.0];
const BLOCK_BINS: usize = 8; // BLOCK_S (7 upper edges) + over the last
const BLOCK_EDGES: [&str; 8] = ["0.5 s", "1 s", "2 s", "4 s", "8 s", "16 s", "32 s", "over 32 s"];
const HITCH_BINS: usize = 4;
const DAY_KEEP: u64 = 400;
const MAX_GROUPS: usize = 4000; // a day's groups at most
const MAX_CAUSES: usize = 32;
const MAX_ERRORS: usize = 256;
const MAX_WINDOWS: usize = 6; // windows a report may carry
const SAVE_S: u64 = 600;
const SETUP_TTL: Duration = Duration::from_secs(36 * 3600);
pub const K_ANON: u32 = crate::telemetry::K_ANON;
pub const BANDS: [&str; 6] = ["stopped", "u30", "s30", "s60", "s100", "s150"];
pub const SCENES: [&str; 5] = ["empty", "rural", "town", "urban", "dense"];

// ---- what a report carries (perfsummary.lua / perfstats.py); unknown fields are ignored ----

#[derive(Deserialize, Default)]
pub struct BandIn {
    s: Option<f64>,
    fh: Option<Vec<f64>>,
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
    mod_settings: Option<HashMap<String, serde_json::Value>>,
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
}

#[derive(Deserialize, Default)]
pub struct PerfIn {
    setup: Option<SetupIn>,
    game: Option<Vec<WindowIn>>,
    server: Option<ServerIn>,
}

// ---- the totals ----

#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
pub struct Band {
    pub s: f64,
    pub fh: Vec<u64>,
}

#[derive(Serialize, Deserialize, Default, Clone, Debug)]
pub struct Agg {
    pub installs: u32, // distinct installs this day (the keyed-hash set's size, kept as a number)
    pub windows: u64,
    pub s: f64,
    pub fh: Vec<u64>,
    pub hit: Vec<u64>,
    #[serde(default)]
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
    #[serde(skip)]
    seen: HashSet<u64>,
}

fn add_vec(dst: &mut Vec<u64>, src: &[u64], n: usize) {
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
    }

    fn window(w: &WindowIn) -> Agg {
        let mut a = Agg { windows: 1, s: secs(w.s), fh: counts(&w.fh, BINS), hit: counts(&w.hit, HITCH_BINS),
                          hit_load: counts(&w.hit_load, HITCH_BINS),
                          blocks: counts(&w.blocks, BLOCK_BINS), ..Default::default() };
        if let Some(bands) = &w.bands {
            for name in BANDS {
                if let Some(b) = bands.get(name) {
                    a.bands.insert(name.to_string(), Band { s: secs(b.s), fh: counts(&b.fh, BINS) });
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
        a
    }

    /// The numbers a page shows.
    pub fn view(&self) -> serde_json::Value {
        let frames: u64 = self.fh.iter().sum();
        let ft: f64 = self.bands.values().map(|b| b.s).sum();
        let fps = |fh: &[u64], s: f64| -> serde_json::Value {
            let n: u64 = fh.iter().sum();
            if n == 0 || s <= 0.0 { serde_json::Value::Null } else { serde_json::json!((n as f64 / s * 10.0).round() / 10.0) }
        };
        let mut bands = serde_json::Map::new();
        for name in BANDS {
            if let Some(b) = self.bands.get(name) {
                if b.s >= 60.0 {
                    bands.insert(name.into(), serde_json::json!({"fps": fps(&b.fh, b.s), "low1": low1(&b.fh),
                                                                 "minutes": (b.s / 60.0).round()}));
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
            if ft - self.loading > 60.0 { serde_json::json!((h as f64 / dh * 10.0).round() / 10.0) } else { serde_json::Value::Null }
        };
        let pct = |v: f64| if self.s > 0.0 { (v / self.s * 1000.0).round() / 10.0 } else { 0.0 };
        serde_json::json!({
            "installs": self.installs, "hours": (self.s / 360.0).round() / 10.0,
            "fps": fps(&self.fh, ft), "low1": low1(&self.fh),
            "smooth": if frames > 0 { (self.fh.iter().take(4).sum::<u64>() as f64 / frames as f64 * 1000.0).round() / 10.0 } else { 0.0 },
            "hitchesPerHour": (self.hit.get(1).copied().unwrap_or(0) as f64 / hours * 10.0).round() / 10.0,
            "bigHitchesPerHour": (self.hit.get(2).copied().unwrap_or(0) as f64 / hours * 10.0).round() / 10.0,
            "drivingHitchesPerHour": driving,
            "dropsPerHour": (self.drops as f64 / hours * 10.0).round() / 10.0,
            "edgeHeldPct": pct(self.edge), "groundHoldPct": pct(self.drophold), "loadingPct": pct(self.loading),
            "catchupPct": pct(self.catchup_s), "teleportsPerHour": (self.teleports as f64 / hours * 10.0).round() / 10.0,
            "blockWait": if nb > 0 { serde_json::json!(BLOCK_EDGES[median_bin(&self.blocks)]) } else { serde_json::Value::Null },
            "blockWaitOver4sPct": if nb > 0 { (self.blocks.iter().skip(4).sum::<u64>() as f64 / nb as f64 * 1000.0).round() / 10.0 } else { 0.0 },
            "bands": bands, "scene": scene, "topCauses": causes,
        })
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
pub fn low1(fh: &[u64]) -> serde_json::Value {
    let n: u64 = fh.iter().sum();
    if n == 0 {
        return serde_json::Value::Null;
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
    serde_json::Value::Null
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
}

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

    /// A report's perf section from install id (already checked by telemetry.rs), its version and OS.
    pub fn add(&mut self, id: &str, v: &str, os: &str, p: PerfIn, now: Instant, unix_now: u64) {
        if !counted_version(v) {
            return;
        }
        if let Some(s) = &p.setup {
            let mut groups = Vec::new();
            if let Some(g) = label(&s.gpu, 80) { groups.push(format!("gpu:{g}")); }
            if let Some(c) = label(&s.cpu, 80) { groups.push(format!("cpu:{c}")); }
            if let Some(r) = label(&s.res, 12) { groups.push(format!("res:{r}")); }
            if let Some(r) = s.ram_gb.filter(|x| x.is_finite() && *x > 0.0 && *x < 4096.0) { groups.push(format!("ram:{} GB", r as u64)); }
            let preset = s.mod_settings.as_ref().and_then(|m| m.get("quality")).and_then(|q| q.as_str()).map(|q| q.to_string());
            if let Some(q) = label(&preset, 16) { groups.push(format!("preset:{q}")); }
            let _ = &s.os;
            self.setups.insert(id.to_string(), Setup { at: now, groups });
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
        if let Some(s) = self.setups.get(id) { base.extend(s.groups.iter().cloned()); }
        let f = self.days.entry(day).or_insert_with(|| DayFile { day, ..Default::default() });
        let mut touch = |f: &mut DayFile, g: &str, a: &Agg| {
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
            if let Some((_, age)) = w.age.and_then(|m| AGES.iter().find(|(lo, _)| *lo == m)) {
                touch(f, &format!("age:{age}"), &a);
            }
        }
        if let Some(s) = &p.server {
            let a = Agg { catchups: s.catchups.filter(|x| x.is_finite()).unwrap_or(0.0).clamp(0.0, 1000.0) as u64,
                          catchup_s: secs(s.catchup_s), ..Default::default() };
            if a.catchups > 0 || a.catchup_s > 0.0 {
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

    pub fn public(&self, unix_now: u64, days: u64) -> serde_json::Value {
        let m = self.merged(unix_now, days);
        let mut dims: BTreeMap<String, Vec<serde_json::Value>> = BTreeMap::new();
        let mut other: BTreeMap<String, Agg> = BTreeMap::new();
        let mut all = serde_json::Value::Null;
        for (g, a) in &m {
            if g == "all" {
                if a.installs >= K_ANON { all = a.view(); }
                continue;
            }
            let Some((dim, name)) = g.split_once(':') else { continue };
            if a.installs >= K_ANON {
                let mut v = a.view();
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
                let mut v = o.view();
                v["name"] = serde_json::json!("other");
                dims.entry(dim).or_default().push(v);
            }
        }
        for list in dims.values_mut() {
            list.sort_by(|a, b| b["hours"].as_f64().unwrap_or(0.0).total_cmp(&a["hours"].as_f64().unwrap_or(0.0)));
        }
        let first = self.days.keys().next().map(|d| d * 86400);
        serde_json::json!({"days": days, "kAnon": K_ANON, "all": all, "groups": dims, "dataSince": first,
                           "bands": BANDS, "frameEdgesMs": EDGES_MS})
    }

    /// The advanced page's part: the error fingerprints of the last 7 days: [fingerprint, count, {code: count}].
    pub fn errors(&self, unix_now: u64) -> serde_json::Value {
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
}
