//! The live thunder: a renderer behind a trait (swappable: the procedural model of thunder.rs today, a sample-based
//! one or another tomorrow, chosen with `live-serve --thunder`) and the bus that schedules what it renders.
//!
//! The mod sends each strike twice (liveaudio.lua): a look-ahead hint up to 3 s early (render it now) and the final
//! one at the strike, with the real distance and the UTC time its sound arrives (play it). Only finals play, so a
//! paused or skipped strike never makes a sound without its flash. The render runs on its own thread and the mixer
//! takes a finished one when its time comes. A render that is late still plays if under LATE_MAX_S late (from its
//! start, so the crack is never cut off).

use crate::proto::{Kind, Thunder};
use std::collections::HashMap;
use std::sync::{Arc, Condvar, Mutex};

pub const LATE_MAX_S: f64 = 2.0;
/// a hint whose strike never came is forgotten this long after its time
pub const HINT_KEEP_S: f64 = 3.0;
pub const MAX_ENTRIES: usize = 64;
pub const MAX_VOICES: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Spec {
    pub seed: u32,
    pub d: f64,
    pub kind: Kind,
    /// the recorded set (the grain bank); else the generated one
    pub rec: bool,
}

/// Live thunder is never louder than this (dBFS sample peak): it sums with the game in the system mixer.
pub const LIVE_PEAK_DB: f64 = -3.0;

/// No thunder (the generated set while Live: the game plays the method B files; the synth only ducks its rain).
pub struct NoThunder;

impl ThunderRenderer for NoThunder {
    fn name(&self) -> String {
        "none".into()
    }
    fn render(&mut self, _spec: &Spec) -> Vec<f32> {
        Vec::new()
    }
}

/// One renderer per thunder set (the Weather page's Thunder sound).
pub struct BySet {
    pub generated: Box<dyn ThunderRenderer>,
    pub recorded: Box<dyn ThunderRenderer>,
}

impl ThunderRenderer for BySet {
    fn name(&self) -> String {
        format!("generated={} recorded={}", self.generated.name(), self.recorded.name())
    }
    fn render(&mut self, spec: &Spec) -> Vec<f32> {
        self.render_timed(spec).0
    }
    fn render_timed(&mut self, spec: &Spec) -> (Vec<f32>, f64) {
        if spec.rec {
            self.recorded.render_timed(spec)
        } else {
            self.generated.render_timed(spec)
        }
    }
}

/// Scaled down so its sample peak is at most LIVE_PEAK_DB (never up).
pub fn cap_peak(mut x: Vec<f32>) -> Vec<f32> {
    let peak = x.iter().fold(0.0f32, |a, v| a.max(v.abs())) as f64;
    let cap = crate::m::undb(LIVE_PEAK_DB);
    if peak > cap {
        let g = (cap / peak) as f32;
        for v in x.iter_mut() {
            *v *= g;
        }
    }
    x
}

/// The scheduled rain duck under a strike (the same rule as the mod's ambience.lua, CLOSE_STRIKE_AND_LOUDNESS.md):
/// a pre-dip from when the strike is known, a 15 ms ramp ending at its crack, -10 dB at 60 m to -5 dB at 280 m, held,
/// released (linear in dB).
pub const DUCK_RAMP_S: f64 = 0.015;
pub const DUCK_HOLD_S: f64 = 0.45;
pub const DUCK_RELEASE_S: f64 = 2.0;
pub const PREDIP_DB: f64 = -2.5;

#[derive(Clone, Copy, Debug)]
pub struct Duck {
    pub from: f64,
    pub t0: f64,
    pub depth_db: f64,
}

pub fn duck_depth_db(d: f64) -> f64 {
    -10.0 + 5.0 * ((d - 60.0) / 220.0).clamp(0.0, 1.0)
}

/// Pure: the envelope 0..1 of a duck at time t, and whether it is over.
pub fn duck_envelope(k: &Duck, t: f64) -> (f64, bool) {
    let pre = PREDIP_DB.min(0.0) / k.depth_db;
    let r0 = k.t0 - DUCK_RAMP_S;
    if t < k.from {
        return (0.0, false);
    }
    if t < r0 {
        return (pre, false);
    }
    if t < k.t0 {
        return (pre + (1.0 - pre) * (t - r0) / DUCK_RAMP_S, false);
    }
    let h = k.t0 + DUCK_HOLD_S;
    if t < h {
        return (1.0, false);
    }
    let e = 1.0 - (t - h) / DUCK_RELEASE_S;
    if e <= 0.0 {
        (0.0, true)
    } else {
        (e, false)
    }
}

/// A thunder renderer: mono f32 at 44.1 kHz, the sound's start at index 0 (the distance delay is the bus's job),
/// at the kind's level (the pre-recorded files' loudness targets); the strike's gain and the volumes are the mixer's.
pub trait ThunderRenderer: Send {
    fn name(&self) -> String;
    fn render(&mut self, spec: &Spec) -> Vec<f32>;
    /// The render and where its crack (the first arrival) is, in s from its start: the bus starts it that much before
    /// the thunder's `at`, so the crack lands on the time the rain ducks to. 0 for renders that start at the crack.
    fn render_timed(&mut self, spec: &Spec) -> (Vec<f32>, f64) {
        (self.render(spec), 0.0)
    }
}

/// Method B live (the Generated set): strikes and closes rendered by methodb's fast path on one thread (deterministic
/// per seed and distance: the same thunder on every machine), cut to start CRACK_LEAD_S before their crack (found in
/// the render, not assumed); mid and far render nothing, so the mod plays its files for them.
pub struct MethodB;

pub const CRACK_LEAD_S: f64 = 0.010;

/// The first arrival of a render: the first sample at a quarter of the peak of its first 1.5 s (the crack's
/// leading edge; methodb puts the first arrival near 0.5 s, the files' FILE_ONSET_S).
pub fn crack_index(y: &[f64]) -> usize {
    let n = y.len().min((1.5 * crate::npx::SR) as usize);
    let pk = y[..n].iter().fold(0.0f64, |a, v| a.max(v.abs()));
    if pk <= 0.0 {
        return 0;
    }
    y[..n].iter().position(|v| v.abs() >= 0.25 * pk).unwrap_or(0)
}

impl ThunderRenderer for MethodB {
    fn name(&self) -> String {
        "methodb".into()
    }
    fn render(&mut self, spec: &Spec) -> Vec<f32> {
        self.render_timed(spec).0
    }
    fn render_timed(&mut self, spec: &Spec) -> (Vec<f32>, f64) {
        let class = match spec.kind {
            Kind::Strike => crate::methodb::BClass::Strike,
            Kind::Close => crate::methodb::BClass::Close,
            _ => return (Vec::new(), 0.0),
        };
        let mut set = crate::methodb::settings(class);
        set.fast = true;
        set.threads = 1;
        let (y, _) = crate::methodb::render(spec.seed as u64, spec.d.max(30.0), &set, false);
        let c = crack_index(&y);
        let start = c.saturating_sub((CRACK_LEAD_S * crate::npx::SR) as usize);
        (y[start..].iter().map(|v| *v as f32).collect(), (c - start) as f64 / crate::npx::SR)
    }
}

/// thunder.rs's physical model (render_live), set to the kind's loudness target with its peak under the ceiling.
pub struct Procedural {
    pub micro_min: f64,
    pub length_s: f64,
}

impl ThunderRenderer for Procedural {
    fn name(&self) -> String {
        "procedural".into()
    }
    fn render(&mut self, spec: &Spec) -> Vec<f32> {
        let az = (spec.seed % 360) as f64 * crate::m::PI / 180.0;
        let (y, _) = crate::thunder::render_live(spec.seed as u64, spec.d.max(50.0), az, self.micro_min, self.length_s);
        let cl = crate::thunder::class(spec.kind.name()).expect("a known class");
        level(&y, cl.target_lufs, cl.ceil_db)
    }
}

/// Scaled to the loudest 3 s at target LUFS (the offline files' rule), the sample peak at most ceil dBFS.
pub fn level(y: &[f64], target_lufs: f64, ceil_db: f64) -> Vec<f32> {
    let st = crate::loudness::max_short_term(y, crate::thunder::FS);
    let peak = y.iter().fold(0.0f64, |a, v| a.max(v.abs()));
    if !(st.is_finite() && peak > 0.0) {
        return vec![0.0; y.len()];
    }
    let g = crate::m::undb(target_lufs - st).min(crate::m::undb(ceil_db) / peak);
    y.iter().map(|v| (v * g) as f32).collect()
}

/// Fixed files per kind (16-bit or float WAV, mono, 44.1 kHz): `bne_thunder_<kind><n>.wav` in a directory, picked by
/// the seed like storm.lua picks its .ogg. A way to try sample sets without code; also the tests' renderer.
pub struct Files {
    sets: Vec<(Kind, Vec<Arc<Vec<f32>>>)>,
}

impl Files {
    pub fn load(dir: &str) -> Result<Files, String> {
        let mut sets = Vec::new();
        for kind in [Kind::Strike, Kind::Close, Kind::Mid, Kind::Far] {
            let mut v = Vec::new();
            for n in 1..=16 {
                let p = format!("{dir}/bne_thunder_{}{n}.wav", kind.name());
                if !std::path::Path::new(&p).exists() {
                    break;
                }
                let (x, rate) = crate::wav::read(&p)?;
                if rate != 44100 {
                    return Err(format!("{p}: {rate} Hz (44100 wanted)"));
                }
                v.push(Arc::new(x.iter().map(|s| *s as f32).collect()));
            }
            if v.is_empty() {
                return Err(format!("{dir}: no bne_thunder_{}1.wav", kind.name()));
            }
            sets.push((kind, v));
        }
        Ok(Files { sets })
    }
    #[cfg(test)]
    pub fn from_sets(sets: Vec<(Kind, Vec<Arc<Vec<f32>>>)>) -> Files {
        Files { sets }
    }
}

impl ThunderRenderer for Files {
    fn name(&self) -> String {
        "files".into()
    }
    fn render(&mut self, spec: &Spec) -> Vec<f32> {
        match self.sets.iter().find(|(k, _)| *k == spec.kind) {
            Some((_, v)) if !v.is_empty() => v[spec.seed as usize % v.len()].as_ref().clone(),
            _ => Vec::new(),
        }
    }
}

/// `--thunder procedural` (default) or `--thunder files:DIR`.
pub fn renderer(choice: &str) -> Result<Box<dyn ThunderRenderer>, String> {
    // the grain bank (method A, the default): `grains` looks for thunder_grains.bin next to this program (the app
    // bundle) or in native/weathersynth/assets/ (a source tree); `grains:PATH` names the file
    if choice == "grains" || choice.starts_with("grains:") {
        let path = match choice.strip_prefix("grains:") {
            Some(p) => p.to_string(),
            None => default_bank().ok_or("no thunder_grains.bin next to weathersynth")?,
        };
        let bank = crate::grainbank::Bank::load(&path)?;
        return Ok(Box::new(crate::grainbank::Grains { bank: Arc::new(bank) }));
    }
    if choice == "none" {
        return Ok(Box::new(NoThunder));
    }
    if choice == "methodb" {
        return Ok(Box::new(MethodB));
    }
    if choice == "procedural" {
        let micro_min = std::env::var("WEATHERSYNTH_MICRO").ok().and_then(|v| v.parse().ok()).unwrap_or(0.06);
        return Ok(Box::new(Procedural { micro_min, length_s: 14.0 }));
    }
    if let Some(dir) = choice.strip_prefix("files:") {
        return Ok(Box::new(Files::load(dir)?));
    }
    Err(format!("unknown --thunder {choice:?} (grains | grains:PATH | procedural | files:DIR)"))
}

/// The bank file's places: next to this program, then the source tree's assets folder.
pub fn default_bank() -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let mut c = vec![dir.join(crate::grainbank::BANK_FILE)];
    for up in dir.ancestors().take(4) {
        c.push(up.join("assets").join(crate::grainbank::BANK_FILE));
    }
    c.into_iter().find(|p| p.is_file()).map(|p| p.to_string_lossy().into_owned())
}

struct Entry {
    spec: Spec,
    at: f64,
    gain: f64,
    fin: bool,
    samples: Option<Arc<Vec<f32>>>,
    /// the crack's offset in the render (s): the voice starts that much before `at`
    crack_s: f64,
    rendering: bool,
    started: bool,
}

/// A hint's distance may move this much (relative) before its render is redone; the mod takes a ready render for a
/// final within the same margin.
pub const READY_REL_D: f64 = 0.15;

fn d_moved(a: f64, b: f64) -> bool {
    (a - b).abs() > READY_REL_D * a.max(1.0)
}

struct Voice {
    samples: Arc<Vec<f32>>,
    pos: usize,
    gain: f32,
}

#[derive(Default)]
struct State {
    ducks: Vec<Duck>,
    entries: HashMap<String, Entry>,
    voices: Vec<Voice>,
    stop: bool,
    rendered: u64,
    render_ms_max: f64,
    render_ms_last: f64,
    late: u64,
    dropped: u64,
}

/// The schedule between the stdin thread (offer), the render thread (next_job / done) and the synth worker (start_due /
/// mix). One lock, held briefly; the audio callback never touches it.
#[derive(Default)]
pub struct Bus {
    st: Mutex<State>,
    cv: Condvar,
}

impl Bus {
    pub fn new() -> Bus {
        Bus::default()
    }

    /// A thunder from a message (a hint or a final). A final whose kind changed since the hint is rendered again.
    pub fn offer(&self, th: &Thunder) {
        self.offer_at(th, utc_now())
    }

    /// (offer with the time it is known: the duck's pre-dip starts then)
    pub fn offer_at(&self, th: &Thunder, now: f64) {
        let mut st = self.st.lock().unwrap();
        let spec = Spec { seed: th.seed, d: th.d, kind: th.kind, rec: th.rec };
        // a strike's final: its rain duck, whoever plays the thunder (once per strike)
        if th.fin && th.kind == Kind::Strike && !st.entries.get(&th.id).map(|e| e.fin).unwrap_or(false) {
            if st.ducks.len() < 16 {
                st.ducks.push(Duck { from: now.min(th.at), t0: th.at, depth_db: duck_depth_db(th.d) });
            }
        }
        // a final the game plays from its file: no voice here (only the duck above, at the file's crack)
        if th.fin && !th.play {
            if st.entries.get(&th.id).is_some_and(|e| !e.started) {
                st.entries.remove(&th.id);
            }
            self.cv.notify_all();
            return;
        }
        match st.entries.get_mut(&th.id) {
            Some(e) => {
                if th.fin {
                    if e.spec.kind != th.kind && !e.started {
                        e.samples = None;
                        e.spec = spec;
                    }
                    e.fin = true;
                    e.at = th.at;
                    e.gain = th.gain;
                } else if !e.fin && !e.started {
                    // a hint again (the listener moved): a new kind or a distance moved past the margin renders again
                    if e.spec.kind != th.kind || d_moved(e.spec.d, th.d) {
                        e.samples = None;
                        e.spec = spec;
                    }
                    e.at = th.at;
                    e.gain = th.gain;
                }
            }
            None => {
                if st.entries.len() >= MAX_ENTRIES {
                    return;
                }
                st.entries.insert(
                    th.id.clone(),
                    Entry { spec, at: th.at, gain: th.gain, fin: th.fin, samples: None, crack_s: 0.0, rendering: false,
                        started: false },
                );
            }
        }
        self.cv.notify_all();
    }

    /// (render thread) the next thunder to render: the earliest without a render. Waits up to wait_s for one; None
    /// when there is none by then, or after stop.
    pub fn next_job(&self, now: impl Fn() -> f64, wait_s: f64) -> Option<(String, Spec)> {
        let mut st = self.st.lock().unwrap();
        for attempt in 0..2 {
            if st.stop {
                return None;
            }
            let t = now();
            let pick = st
                .entries
                .iter()
                .filter(|(_, e)| e.samples.is_none() && !e.rendering && !e.started && e.at > t - LATE_MAX_S)
                .min_by(|a, b| a.1.at.partial_cmp(&b.1.at).unwrap_or(std::cmp::Ordering::Equal))
                .map(|(k, e)| (k.clone(), e.spec));
            if let Some((id, spec)) = pick {
                st.entries.get_mut(&id).unwrap().rendering = true;
                return Some((id, spec));
            }
            if attempt == 0 && wait_s > 0.0 {
                st = self.cv.wait_timeout(st, std::time::Duration::from_secs_f64(wait_s)).unwrap().0;
            }
        }
        None
    }

    pub fn stopped(&self) -> bool {
        self.st.lock().unwrap().stop
    }

    /// (render thread) a finished render (for the spec it was asked: a kind changed meanwhile renders again)
    #[cfg(test)]
    pub fn done(&self, id: &str, spec: Spec, samples: Vec<f32>) {
        self.done_timed(id, spec, samples, 0.0, 0.0)
    }

    pub fn done_timed(&self, id: &str, spec: Spec, samples: Vec<f32>, ms: f64, crack_s: f64) {
        let mut st = self.st.lock().unwrap();
        st.rendered += 1;
        st.render_ms_last = ms;
        st.render_ms_max = st.render_ms_max.max(ms);
        if let Some(e) = st.entries.get_mut(id) {
            e.rendering = false;
            if e.spec.kind == spec.kind && e.spec.d == spec.d {
                e.samples = Some(Arc::new(samples));
                e.crack_s = crack_s;
            }
        }
        self.cv.notify_all();
    }

    /// The thunders rendered and waiting (not started, not empty): (id, the distance rendered for), at most 24, the
    /// soonest first. The mod plays a final through the synth only when it was listed here (within READY_REL_D).
    pub fn ready(&self) -> Vec<(String, f64)> {
        let st = self.st.lock().unwrap();
        let mut v: Vec<(f64, String, f64)> = st
            .entries
            .iter()
            .filter(|(_, e)| !e.started && e.samples.as_ref().is_some_and(|s| !s.is_empty()))
            .map(|(k, e)| (e.at, k.clone(), e.spec.d))
            .collect();
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1)));
        v.into_iter().take(24).map(|(_, k, d)| (k, d)).collect()
    }

    /// (synth worker) start the finals due by t_out (the UTC time the next block will be heard); forget stale ones.
    pub fn start_due(&self, t_out: f64) {
        let mut st = self.st.lock().unwrap();
        let mut start = Vec::new();
        let mut late = 0;
        let mut dropped = 0;
        st.entries.retain(|_, e| {
            if e.started {
                return t_out < e.at + 60.0;
            }
            if e.fin && e.at - e.crack_s <= t_out {
                if t_out - e.at > LATE_MAX_S {
                    dropped += 1;
                    return false;
                }
                if let Some(s) = &e.samples {
                    if t_out - e.at > 0.05 {
                        late += 1;
                    }
                    start.push(Voice { samples: s.clone(), pos: 0, gain: e.gain as f32 });
                    e.started = true;
                }
                return true;
            }
            !(!e.fin && t_out > e.at + HINT_KEEP_S)
        });
        st.late += late;
        st.dropped += dropped;
        for v in start {
            if st.voices.len() >= MAX_VOICES {
                st.voices.remove(0);
            }
            st.voices.push(v);
        }
    }

    /// (synth worker) add the playing thunders x master into out (mono, 44.1 kHz)
    pub fn mix(&self, out: &mut [f32], master: f32) {
        let mut st = self.st.lock().unwrap();
        for v in st.voices.iter_mut() {
            let n = out.len().min(v.samples.len().saturating_sub(v.pos));
            let g = v.gain * master;
            for (o, s) in out[..n].iter_mut().zip(&v.samples[v.pos..v.pos + n]) {
                *o += s * g;
            }
            v.pos += n;
        }
        st.voices.retain(|v| v.pos < v.samples.len());
    }

    /// (the last render's ms, the longest's)
    pub fn render_ms(&self) -> (f64, f64) {
        let st = self.st.lock().unwrap();
        (st.render_ms_last, st.render_ms_max)
    }

    /// (synth worker) the rain's gain at time t under the ducks (1: none); the ducks that are over dropped.
    pub fn duck_gain(&self, t: f64) -> f64 {
        let mut st = self.st.lock().unwrap();
        if st.ducks.is_empty() {
            return 1.0;
        }
        let mut g = 1.0f64;
        st.ducks.retain(|k| {
            let (e, over) = duck_envelope(k, t);
            if e > 0.0 {
                g = g.min(crate::m::undb(k.depth_db * e));
            }
            !over
        });
        g
    }

    pub fn stop(&self) {
        self.st.lock().unwrap().stop = true;
        self.cv.notify_all();
    }

    /// (rendered, late, dropped, playing)
    pub fn counts(&self) -> (u64, u64, u64, usize) {
        let st = self.st.lock().unwrap();
        (st.rendered, st.late, st.dropped, st.voices.len())
    }
}

/// The render thread: renders the bus's jobs until stop.
pub fn render_loop(bus: Arc<Bus>, mut r: Box<dyn ThunderRenderer>, now: fn() -> f64) {
    while !bus.stopped() {
        if let Some((id, spec)) = bus.next_job(now, 0.25) {
            let t0 = std::time::Instant::now();
            let (s, crack_s) = r.render_timed(&spec);
            bus.done_timed(&id, spec, cap_peak(s), t0.elapsed().as_secs_f64() * 1000.0, crack_s);
        }
    }
}

/// UTC seconds (the mod's storm.now() clock).
pub fn utc_now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn th(id: &str, at: f64, kind: Kind, fin: bool) -> Thunder {
        Thunder { id: id.into(), at, d: 1000.0, kind, gain: 0.5, seed: 3, fin, rec: false, play: true }
    }

    fn files() -> Files {
        let one = |v: f32| Arc::new(vec![v; 4]);
        Files::from_sets(vec![
            (Kind::Strike, vec![one(1.0)]),
            (Kind::Close, vec![one(2.0), one(3.0)]),
            (Kind::Mid, vec![one(4.0)]),
            (Kind::Far, vec![one(5.0)]),
        ])
    }

    #[test]
    fn hint_renders_final_plays() {
        let bus = Bus::new();
        let mut r = files();
        bus.offer(&th("a", 100.0, Kind::Close, false));
        let (id, spec) = bus.next_job(|| 99.0, 0.0).unwrap();
        bus.done(&id, spec, r.render(&spec));
        bus.start_due(101.0);
        let mut out = [0.0f32; 4];
        bus.mix(&mut out, 1.0);
        assert_eq!(out, [0.0; 4], "a hint never plays");
        bus.offer(&th("a", 100.5, Kind::Close, true));
        bus.start_due(100.4);
        bus.mix(&mut out, 1.0);
        assert_eq!(out, [0.0; 4], "not before its time");
        bus.start_due(100.5);
        bus.mix(&mut out, 0.5);
        assert_eq!(out, [0.75; 4], "seed 3 -> the second close file x gain 0.5 x master 0.5");
        assert_eq!(bus.counts(), (1, 0, 0, 0), "rendered once, done playing");
        bus.start_due(100.6);
        let mut out = [0.0f32; 4];
        bus.mix(&mut out, 1.0);
        assert_eq!(out, [0.0; 4], "played once");
    }

    #[test]
    fn kind_change_rerenders_and_stale_go() {
        let bus = Bus::new();
        let mut r = files();
        bus.offer(&th("a", 10.0, Kind::Close, false));
        let (id, spec) = bus.next_job(|| 9.0, 0.0).unwrap();
        bus.offer(&th("a", 9.5, Kind::Strike, true)); // (moved nearer before the render finished)
        bus.done(&id, spec, r.render(&spec));
        let (id2, spec2) = bus.next_job(|| 9.0, 0.0).expect("rendered again for the new kind");
        assert_eq!(spec2.kind, Kind::Strike);
        bus.done(&id2, spec2, r.render(&spec2));
        bus.start_due(9.5);
        let mut out = [0.0f32; 4];
        bus.mix(&mut out, 1.0);
        assert_eq!(out, [0.5; 4]);
        // a hint whose strike never came, a final far too late
        bus.offer(&th("h", 20.0, Kind::Far, false));
        bus.offer(&th("late", 20.0, Kind::Mid, true));
        bus.start_due(20.0 + HINT_KEEP_S + 0.1);
        assert!(bus.next_job(|| 20.0 + HINT_KEEP_S + 0.1, 0.0).is_none(), "nothing left to render");
        assert_eq!(bus.counts().2, 1, "the late final dropped");
        bus.stop();
        assert!(bus.stopped() && bus.next_job(|| 0.0, 1.0).is_none(), "stop ends the render thread");
    }

    #[test]
    fn strike_duck_and_peak_cap() {
        let bus = Bus::new();
        let mut t = th("s", 100.5, Kind::Strike, true);
        t.d = 60.0;
        bus.offer_at(&t, 100.0);
        bus.offer_at(&t, 100.0); // (the same strike again: one duck)
        assert!((bus.duck_gain(100.2) - crate::m::undb(PREDIP_DB)).abs() < 1e-9, "the pre-dip");
        assert!((bus.duck_gain(100.6) - crate::m::undb(-10.0)).abs() < 1e-9, "-10 dB at 60 m");
        assert_eq!(bus.duck_gain(100.5 + DUCK_HOLD_S + DUCK_RELEASE_S + 0.1), 1.0, "over");
        assert!(bus.st.lock().unwrap().ducks.is_empty(), "dropped");
        bus.offer_at(&th("c", 10.0, Kind::Close, true), 9.0);
        assert_eq!(bus.duck_gain(10.1), 1.0, "only strikes duck");
        let y = cap_peak(vec![0.0, 0.9, -0.2]);
        assert!((y[1] as f64 - crate::m::undb(LIVE_PEAK_DB)).abs() < 1e-6);
        assert_eq!(cap_peak(vec![0.1, -0.2]), vec![0.1, -0.2], "never up");
    }

    #[test]
    fn earliest_first_and_caps() {
        let bus = Bus::new();
        bus.offer(&th("later", 50.0, Kind::Far, false));
        bus.offer(&th("sooner", 40.0, Kind::Mid, false));
        assert_eq!(bus.next_job(|| 30.0, 0.0).unwrap().0, "sooner");
        assert_eq!(bus.next_job(|| 30.0, 0.0).unwrap().0, "later");
        for i in 0..(MAX_ENTRIES + 10) {
            bus.offer(&th(&format!("x{i}"), 60.0, Kind::Far, false));
        }
        assert!(bus.st.lock().unwrap().entries.len() <= MAX_ENTRIES);
    }

    #[test]
    fn ready_list_play_flag_and_moved_hints() {
        let bus = Bus::new();
        let mut r = files();
        bus.offer(&th("a", 50.0, Kind::Close, false));
        assert!(bus.ready().is_empty(), "not rendered yet");
        let (id, spec) = bus.next_job(|| 20.0, 0.0).unwrap();
        bus.done_timed(&id, spec, r.render(&spec), 1.0, 0.25);
        assert_eq!(bus.ready(), vec![("a".to_string(), 1000.0)]);
        // the listener moved 10 %: kept; 30 %: rendered again (and not ready meanwhile)
        let mut h = th("a", 50.0, Kind::Close, false);
        h.d = 1100.0;
        bus.offer(&h);
        assert_eq!(bus.ready().len(), 1, "within the margin: kept");
        h.d = 1400.0;
        bus.offer(&h);
        assert!(bus.ready().is_empty(), "moved: rendered again");
        let (id, spec) = bus.next_job(|| 20.0, 0.0).unwrap();
        assert_eq!(spec.d, 1400.0);
        // a render for an older spec is not kept
        bus.done_timed(&id, Spec { d: 1000.0, ..spec }, vec![1.0; 4], 1.0, 0.0);
        assert!(bus.ready().is_empty(), "stale render dropped");
        bus.done_timed(&id, spec, vec![1.0; 4], 1.0, 0.25);
        // the final: the voice starts crack_s before at
        let mut f = th("a", 50.0, Kind::Close, true);
        f.d = 1400.0;
        bus.offer(&f);
        bus.start_due(49.70);
        assert_eq!(bus.counts().3, 0, "not yet");
        bus.start_due(49.75);
        assert_eq!(bus.counts().3, 1, "started 0.25 s early: the crack lands on at");
        // a final the game plays (play = false): no voice, but a strike still ducks the rain at its at
        bus.offer(&th("b", 60.0, Kind::Strike, false));
        let mut g = th("b", 60.5, Kind::Strike, true);
        g.play = false;
        g.d = 60.0;
        bus.offer_at(&g, 60.0);
        assert!(bus.next_job(|| 59.0, 0.0).is_none() && bus.ready().is_empty(), "nothing to render or play");
        assert!((bus.duck_gain(60.6) - crate::m::undb(-10.0)).abs() < 1e-9, "the rain ducks at the file's crack");
    }

    /// (a real fast-path render, a few seconds: run with --ignored)
    #[test]
    #[ignore]
    fn methodb_live_strike_starts_at_its_crack() {
        let mut r = MethodB;
        let spec = Spec { seed: 11, d: 122.5, kind: Kind::Strike, rec: false };
        let (y, crack_s) = r.render_timed(&spec);
        assert!((crack_s - CRACK_LEAD_S).abs() < 1e-9, "cut {CRACK_LEAD_S} s before the crack");
        let full = 14.0 * crate::npx::SR;
        let cut_s = (full - y.len() as f64) / crate::npx::SR;
        assert!((0.35..0.7).contains(&(cut_s + crack_s)), "the crack was {:.3} s into the render", cut_s + crack_s);
        let c = crack_index(&y.iter().map(|v| *v as f64).collect::<Vec<_>>());
        assert_eq!(c, (CRACK_LEAD_S * crate::npx::SR) as usize);
        assert!(r.render_timed(&Spec { kind: Kind::Mid, ..spec }).0.is_empty(), "mid: the files");
        let (y2, _) = r.render_timed(&spec);
        assert_eq!(y, y2, "deterministic");
    }

    #[test]
    fn crack_index_finds_the_leading_edge() {
        let mut y = vec![0.0f64; 44100];
        y[22050] = 0.3;
        y[22051] = -1.0;
        y[30000] = 0.5;
        assert_eq!(crack_index(&y), 22050);
        assert_eq!(crack_index(&[0.0; 10]), 0);
    }

    #[test]
    fn level_hits_the_ceiling_or_target() {
        let y: Vec<f64> = (0..44100 * 4).map(|i| (i as f64 * 0.05).sin() * 0.01).collect();
        let v = level(&y, -9.5, -1.0);
        let peak = v.iter().fold(0.0f32, |a, s| a.max(s.abs()));
        assert!(peak <= crate::m::undb(-1.0) as f32 + 1e-6);
        assert_eq!(level(&[0.0; 100], -9.5, -1.0), vec![0.0; 100]);
    }
}
