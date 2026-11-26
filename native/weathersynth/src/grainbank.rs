//! Thunder from a bank of CC0 recordings ("grains"): method A, the Recorded thunder set, grain bank v3 (the user
//! approved its references on 2026-11-25). The behaviour is ~/bnge-tests/weather/procedural/grainbank/v3/SPEC.md
//! sections 6-7, ported from its reference renderer (tools/render_v3.py: v1's scheduling; absorption only for the
//! distance beyond each grain's own recording, scaled per class; far rolls drawn from every roll pool; the rumble
//! balance shelves crossfaded in from 0.6 s after the first arrival).
//! The draws are numpy's Generator (nprng: the same values in the same order as render_v3.py's
//! default_rng(seed)), so a seed schedules the same grains, times and gains as the reference; the filters and the
//! decoded grains differ slightly (kernels cut short, i16 PCM). Deterministic on every platform: nprng, libm maths,
//! f64 mixing in a fixed order, and grains read from raw i16 PCM, so no Vorbis decoder is involved at run time.
//!
//! - **The bank file:** `thunder_grains.bin`, written by `weathersynth grainbank-pack` from the cutting tool's
//!   manifest.json and grains/*.ogg (ffmpeg decodes them once, at pack time). Re-cutting the bank from the originals
//!   only swaps this file.
//!   - Layout: "WSGRAIN2"; u32 count; per grain u8 kind, u8 the source recording's class (0 strike, 1 close, 2 mid,
//!     3 far), u8 id length, the id, u32 source id, f32 centroid Hz (-1: none), u32 samples; then every grain's i16
//!     LE samples in order; then u64 FNV-1a of those sample bytes.
//! - **Absorption:** the reference's FFT minimum-phase filter (spec 6b). Each kernel is designed once per distance,
//!   quantised to 1 % steps; the result depends only on that key, so the cache never changes output. The kernel is cut
//!   where it holds all but 1e-7 of its energy, then applied directly when short or by overlap-add when long. A strike
//!   costs tens of ms instead of the reference's one big FFT per grain.

use crate::dsp::{self, Biquad, C};
use crate::m;
use crate::proto::Kind;
use crate::nprng::NpRng as Rng;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};

pub const SR: f64 = 44100.0;
pub const SOUND: f64 = 344.6;
pub const LENGTH_S: f64 = 14.0;
pub const PRE_S: f64 = 0.5;
pub const CEIL_DB: f64 = -1.0;
pub const LIM_HEADROOM_DB: f64 = 5.0;
pub const LIM_LOOK_MS: f64 = 5.0;
pub const LIM_REL_MS: f64 = 80.0;
pub const HP_HZ: f64 = 28.0;
pub const FADE_OUT_S: f64 = 3.0;
const MAGIC: &[u8; 8] = b"WSGRAIN2";
/// The bank's file name. `grainbank-pack` writes it to native/weathersynth/assets/ from assets/grains/ (the cutting
/// tool's manifest.json, grains/*.ogg and CREDITS.md, committed: a re-cut replaces that folder); the release workflow
/// packs it once and bundles it next to weathersynth.
pub const BANK_FILE: &str = "thunder_grains.bin";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GKind {
    Body = 0,
    Crack = 1,
    Clap = 2,
    Roll = 3,
    MidBody = 4,
    MidRoll = 5,
    FarRoll = 6,
}

impl GKind {
    pub fn from_name(s: &str) -> Option<GKind> {
        Some(match s {
            "body" => GKind::Body,
            "crack" => GKind::Crack,
            "clap" => GKind::Clap,
            "roll" => GKind::Roll,
            "mid_body" => GKind::MidBody,
            "mid_roll" => GKind::MidRoll,
            "far_roll" => GKind::FarRoll,
            _ => return None,
        })
    }
    fn from_u8(v: u8) -> Option<GKind> {
        [GKind::Body, GKind::Crack, GKind::Clap, GKind::Roll, GKind::MidBody, GKind::MidRoll, GKind::FarRoll]
            .get(v as usize)
            .copied()
    }
    /// The varispeed range (spec section 2).
    fn varispeed(self) -> (f64, f64) {
        match self {
            GKind::Body | GKind::MidBody => (0.90, 1.10),
            GKind::Crack => (0.95, 1.25),
            GKind::Clap => (0.87, 1.13),
            GKind::Roll | GKind::MidRoll | GKind::FarRoll => (0.85, 1.05),
        }
    }
}

pub struct Grain {
    pub id: String,
    pub kind: GKind,
    /// the class of the recording it was cut from (its recording distance: k_rec)
    pub src_class: Kind,
    pub source_id: u32,
    pub centroid_hz: f32,
    pub pcm: Vec<i16>,
}

pub struct Bank {
    pub grains: Vec<Grain>,
}

fn fnv(bytes: &[u8], mut h: u64) -> u64 {
    for b in bytes {
        h ^= *b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}
const FNV0: u64 = 0xcbf2_9ce4_8422_2325;

impl Bank {
    /// Read and check a bank file (bounds-checked: a damaged file is an error, never a panic).
    pub fn load(path: &str) -> Result<Bank, String> {
        let b = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        Bank::parse(&b).map_err(|e| format!("{path}: {e}"))
    }

    pub fn parse(b: &[u8]) -> Result<Bank, String> {
        let mut i = 0usize;
        let take = |i: &mut usize, n: usize| -> Result<&[u8], String> {
            let s = b.get(*i..*i + n).ok_or("truncated")?;
            *i += n;
            Ok(s)
        };
        if take(&mut i, 8)? != MAGIC {
            return Err("not a grain bank (magic)".into());
        }
        let u32le = |s: &[u8]| u32::from_le_bytes([s[0], s[1], s[2], s[3]]);
        let count = u32le(take(&mut i, 4)?) as usize;
        if count == 0 || count > 10_000 {
            return Err(format!("bad grain count {count}"));
        }
        let mut heads = Vec::with_capacity(count);
        for _ in 0..count {
            let kind = GKind::from_u8(take(&mut i, 1)?[0]).ok_or("bad grain kind")?;
            let src_class = kind_from_u8(take(&mut i, 1)?[0]).ok_or("bad source class")?;
            let idn = take(&mut i, 1)?[0] as usize;
            let id = String::from_utf8(take(&mut i, idn)?.to_vec()).map_err(|_| "bad grain id")?;
            let source_id = u32le(take(&mut i, 4)?);
            let c = take(&mut i, 4)?;
            let centroid_hz = f32::from_le_bytes([c[0], c[1], c[2], c[3]]);
            let n = u32le(take(&mut i, 4)?) as usize;
            if n == 0 || n > 60 * 44100 {
                return Err(format!("bad grain length {n}"));
            }
            heads.push((id, kind, src_class, source_id, centroid_hz, n));
        }
        let total: usize = heads.iter().map(|h| h.5).sum();
        let data = take(&mut i, total * 2)?;
        let sum = u64::from_le_bytes(take(&mut i, 8)?.try_into().map_err(|_| "truncated")?);
        if fnv(data, FNV0) != sum {
            return Err("checksum mismatch".into());
        }
        if i != b.len() {
            return Err("trailing bytes".into());
        }
        let mut grains = Vec::with_capacity(count);
        let mut off = 0;
        for (id, kind, src_class, source_id, centroid_hz, n) in heads {
            let pcm = data[off..off + 2 * n].chunks_exact(2).map(|p| i16::from_le_bytes([p[0], p[1]])).collect();
            off += 2 * n;
            grains.push(Grain { id, kind, src_class, source_id, centroid_hz, pcm });
        }
        Ok(Bank { grains })
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&(self.grains.len() as u32).to_le_bytes());
        for g in &self.grains {
            out.push(g.kind as u8);
            out.push(kind_to_u8(g.src_class));
            out.push(g.id.len() as u8);
            out.extend_from_slice(g.id.as_bytes());
            out.extend_from_slice(&g.source_id.to_le_bytes());
            out.extend_from_slice(&g.centroid_hz.to_le_bytes());
            out.extend_from_slice(&(g.pcm.len() as u32).to_le_bytes());
        }
        let start = out.len();
        for g in &self.grains {
            for s in &g.pcm {
                out.extend_from_slice(&s.to_le_bytes());
            }
        }
        let sum = fnv(&out[start..], FNV0);
        out.extend_from_slice(&sum.to_le_bytes());
        out
    }

    fn pool(&self, kind: GKind) -> Vec<usize> {
        (0..self.grains.len()).filter(|&i| self.grains[i].kind == kind).collect()
    }

    pub fn seconds(&self) -> f64 {
        self.grains.iter().map(|g| g.pcm.len()).sum::<usize>() as f64 / SR
    }
}

/// `grainbank-pack`: the cutting tool's manifest.json and grains/*.ogg into one bank file. ffmpeg decodes each grain to
/// 16-bit PCM here, once; the game never decodes Vorbis for the thunder.
pub fn pack(manifest: &str, out: &str) -> Result<String, String> {
    let text = std::fs::read_to_string(manifest).map_err(|e| format!("{manifest}: {e}"))?;
    let doc = crate::proto::parse_limit(&text, 16 << 20)?;
    let dir = std::path::Path::new(manifest).parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let Some(crate::proto::Json::Arr(list)) = doc.get("grains") else {
        return Err("manifest: no grains".into());
    };
    let mut grains = Vec::new();
    for g in list {
        let id = g.str("id").ok_or("grain without id")?.to_string();
        let kind = g.str("kind").and_then(GKind::from_name).ok_or_else(|| format!("{id}: unknown kind"))?;
        let src_class = g
            .str("class_of_source")
            .and_then(|c| Kind::from_str(c.trim_start_matches("thunder_")))
            .ok_or_else(|| format!("{id}: no class_of_source"))?;
        let file = g.str("file").ok_or_else(|| format!("{id}: no file"))?;
        let path = dir.join(file);
        // ffmpeg's own Vorbis decoder named explicitly (-c:a vorbis, not libvorbis), its float output taken as is
        // (the grains are mono 44.1 kHz: no remix, no resampling), and the conversion to i16 done here with a fixed
        // rounding, so the decoder is the only moving part (the release workflow packs once and checks the sha256)
        let o = std::process::Command::new("ffmpeg")
            .args(["-nostdin", "-v", "error", "-c:a", "vorbis", "-i"])
            .arg(&path)
            .args(["-ac", "1", "-ar", "44100", "-f", "f32le", "-"])
            .output()
            .map_err(|e| format!("ffmpeg: {e}"))?;
        if !o.status.success() || o.stdout.len() < 4 {
            return Err(format!("ffmpeg could not decode {}", path.display()));
        }
        let pcm: Vec<i16> =
            o.stdout.chunks_exact(4).map(|p| to_i16(f32::from_le_bytes([p[0], p[1], p[2], p[3]]))).collect();
        if id.len() > 255 {
            return Err(format!("{id}: id too long"));
        }
        grains.push(Grain {
            id,
            kind,
            src_class,
            source_id: g.num("source_id").unwrap_or(0.0) as u32,
            centroid_hz: g.num("centroid_hz").map(|v| v as f32).unwrap_or(-1.0),
            pcm,
        });
    }
    let bank = Bank { grains };
    let bytes = bank.to_bytes();
    let tmp = format!("{out}.new");
    std::fs::write(&tmp, &bytes).map_err(|e| format!("{tmp}: {e}"))?;
    std::fs::rename(&tmp, out).map_err(|e| format!("{out}: {e}"))?;
    Ok(format!("{} grains, {:.1} s, {:.1} MB -> {out}", bank.grains.len(), bank.seconds(), bytes.len() as f64 / 1e6))
}

fn kind_to_u8(k: Kind) -> u8 {
    match k {
        Kind::Strike => 0,
        Kind::Close => 1,
        Kind::Mid => 2,
        Kind::Far => 3,
    }
}

fn kind_from_u8(v: u8) -> Option<Kind> {
    [Kind::Strike, Kind::Close, Kind::Mid, Kind::Far].get(v as usize).copied()
}

/// The distance a source class's recordings were made at (v3: only the distance beyond it is filtered).
pub fn k_rec(k: Kind) -> f64 {
    match k {
        Kind::Strike => 250.0,
        Kind::Close => 1000.0,
        Kind::Mid => 3500.0,
        Kind::Far => 9000.0,
    }
}

/// A float sample to 16 bits: x 32767, rounded half away from zero, clamped (NaN: 0).
pub fn to_i16(v: f32) -> i16 {
    let x = v as f64 * 32767.0;
    if !x.is_finite() {
        return 0;
    }
    m::round(x).clamp(-32768.0, 32767.0) as i16
}

// ------------------------------------------------------------------ classes (spec section 2)

pub struct Class {
    pub name: &'static str,
    pub d: (f64, f64),
    pub lufs: f64,
    pub d_rec: f64,
    pub body: Option<GKind>,
    pub crack_db: Option<(f64, f64)>,
    pub n_echo: (u64, u64),
    pub echo_db: (f64, f64),
    /// the extra distance's absorption x this (spec 7)
    pub abs_scale: f64,
    /// the rumble balance: low shelf dB at f_lo, high shelf dB at 1 kHz (spec 7)
    pub tail_lo_db: f64,
    pub tail_f_lo: f64,
    pub tail_hi_db: f64,
}

pub fn class(kind: Kind) -> Class {
    match kind {
        Kind::Strike => Class { name: "strike", d: (60.0, 300.0), lufs: -9.5, d_rec: 300.0, body: Some(GKind::Body),
            crack_db: Some((-15.0, -9.0)), n_echo: (1, 3), echo_db: (-20.0, -12.0), abs_scale: 1.0, tail_lo_db: -4.0,
            tail_f_lo: 150.0, tail_hi_db: 0.0 },
        Kind::Close => Class { name: "close", d: (600.0, 1400.0), lufs: -14.0, d_rec: 800.0, body: Some(GKind::Body),
            crack_db: Some((-21.0, -15.0)), n_echo: (1, 3), echo_db: (-20.0, -12.0), abs_scale: 0.1, tail_lo_db: -10.0,
            tail_f_lo: 220.0, tail_hi_db: 3.0 },
        Kind::Mid => Class { name: "mid", d: (2500.0, 5000.0), lufs: -18.0, d_rec: 3000.0, body: Some(GKind::MidBody),
            crack_db: None, n_echo: (0, 2), echo_db: (-22.0, -14.0), abs_scale: 0.0, tail_lo_db: -12.0, tail_f_lo: 250.0,
            tail_hi_db: 6.0 },
        Kind::Far => Class { name: "far", d: (7000.0, 12000.0), lufs: -22.0, d_rec: 8000.0, body: None, crack_db: None,
            n_echo: (0, 2), echo_db: (-22.0, -14.0), abs_scale: 0.3, tail_lo_db: -4.0, tail_f_lo: 150.0,
            tail_hi_db: 5.0 },
    }
}

/// Live loudness target by distance: the class targets in log distance (150 m -9.5, 1000 m -14, 3500 m -18, 9000 m
/// -22 LUFS), clamped outside.
pub fn live_target(d: f64) -> f64 {
    let a = [(150.0f64, -9.5f64), (1000.0, -14.0), (3500.0, -18.0), (9000.0, -22.0)];
    if d <= a[0].0 {
        return a[0].1;
    }
    for w in a.windows(2) {
        if d <= w[1].0 {
            let t = (m::ln(d) - m::ln(w[0].0)) / (m::ln(w[1].0) - m::ln(w[0].0));
            return w[0].1 + t * (w[1].1 - w[0].1);
        }
    }
    a[3].1
}

// ------------------------------------------------------------------ random helpers

/// numpy's rng.integers(n) (an index; no draw for n = 1).
fn pick(rng: &mut Rng, n: usize) -> usize {
    rng.integers(0, n as i64) as usize
}

/// numpy's rng.uniform(a, b).
trait Uniform {
    fn range(&mut self, a: f64, b: f64) -> f64;
}

impl Uniform for Rng {
    fn range(&mut self, a: f64, b: f64) -> f64 {
        self.uniform(a, b)
    }
}

/// An integer uniform in [lo, hi] (both inclusive).
fn int_in(rng: &mut Rng, lo: u64, hi: u64) -> u64 {
    rng.integers(lo as i64, hi as i64 + 1) as u64
}

// ------------------------------------------------------------------ channel walk (spec section 4)

type V3 = [f64; 3];
fn vadd(a: V3, b: V3) -> V3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}
fn vsc(a: V3, s: f64) -> V3 {
    [a[0] * s, a[1] * s, a[2] * s]
}
fn vlen(a: V3) -> f64 {
    m::sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
}
fn vnorm(a: V3) -> V3 {
    vsc(a, 1.0 / vlen(a).max(1e-12))
}
fn vdist(a: V3, b: V3) -> f64 {
    vlen([a[0] - b[0], a[1] - b[1], a[2] - b[2]])
}
fn nvec(rng: &mut Rng) -> V3 {
    [rng.standard_normal(), rng.standard_normal(), rng.standard_normal()]
}

fn walk(rng: &mut Rng, mut p: V3, mut d: V3, bias: &dyn Fn(V3) -> V3, stop: &dyn Fn(V3, f64) -> bool) -> Vec<V3> {
    let mut pts = vec![p];
    let mut run = 0.0;
    while !stop(p, run) && pts.len() < 1500 {
        let step = rng.lognormal(m::ln(25.0), 0.5);
        d = vnorm(vadd(vadd(d, vsc(nvec(rng), 0.3)), bias(p)));
        p = vadd(p, vsc(d, step));
        run += step;
        pts.push(p);
    }
    pts
}

/// The claps: (dt s after the first arrival, amplitude, mean distance m), the 16 loudest with 0.25 < dt < 9 s.
pub fn channel_claps(rng: &mut Rng, dist: f64) -> Vec<(f64, f64, f64)> {
    let obs = [0.0, 0.0, 1.7];
    let az = rng.range(0.0, m::TAU);
    let g0 = [dist * m::cos(az), dist * m::sin(az), 0.0];
    let top = rng.range(1500.0, 3000.0);
    let horiz = rng.range(2000.0, 6000.0);
    let mut hdir = nvec(rng);
    hdir[2] = 0.0;
    let hdir = vnorm(hdir);
    let main = walk(rng, g0, [0.0, 0.0, 1.0], &|_| [0.0, 0.0, 0.25], &|p, _| p[2] > top);
    let last = *main.last().unwrap();
    let cloud = walk(rng, last, hdir, &|p| vadd(vsc(hdir, 0.25), [0.0, 0.0, 0.05 * (top - p[2]) / 300.0]),
        &|_, run| run > horiz);
    let mut chans: Vec<(Vec<V3>, f64)> = vec![(main.clone(), 1.0), (cloud, 0.6)];
    let nb = int_in(rng, 1, 3);
    for _ in 0..nb {
        let n = main.len();
        let k = rng.integers((n / 3) as i64, n as i64) as usize;
        let mut dd = nvec(rng);
        dd[2] = -dd[2].abs() - 0.5;
        let dd = vnorm(dd);
        let l = rng.range(200.0, 1200.0);
        chans.push((walk(rng, main[k], dd, &|_| [0.0, 0.0, -0.1], &|p, run| run > l || p[2] < 50.0), 1.0));
    }
    let t0 = chans.iter().flat_map(|(c, _)| c.iter().map(|p| vdist(*p, obs))).fold(f64::INFINITY, f64::min) / SOUND;
    let mut out = Vec::new();
    for (c, w) in &chans {
        let r: Vec<f64> = c.iter().map(|p| vdist(*p, obs)).collect();
        let seg: Vec<f64> = c.windows(2).map(|q| vdist(q[1], q[0])).collect();
        let side: Vec<bool> = (0..seg.len()).map(|i| ((r[i + 1] - r[i]) / seg[i].max(1e-9)).abs() < 0.3).collect();
        let mut i = 0;
        while i < side.len() {
            if side[i] {
                let mut j = i;
                while j + 1 < side.len() && side[j + 1] {
                    j += 1;
                }
                let l: f64 = seg[i..=j].iter().sum();
                let rr = r[i..=j + 1].iter().sum::<f64>() / (j + 2 - i) as f64;
                out.push((rr / SOUND - t0, w * l / rr * dist, rr));
                i = j + 1;
            } else {
                i += 1;
            }
        }
    }
    out.retain(|o| o.0 > 0.25 && o.0 < 9.0);
    // (a stable sort by amplitude, descending: ties keep their order, as Python's sort does)
    out.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    out.truncate(16);
    out
}

// ------------------------------------------------------------------ DSP helpers

/// Varispeed: f times faster (pitch x f, length / f), linear interpolation as the reference.
fn resample(x: &[i16], f: f64) -> Vec<f64> {
    let n = (x.len() as f64 / f) as usize;
    let s = 1.0 / 32768.0;
    (0..n)
        .map(|i| {
            let p = i as f64 * f;
            let k = p as usize;
            let fr = p - k as f64;
            let a = x[k.min(x.len() - 1)] as f64 * s;
            let b = x[(k + 1).min(x.len() - 1)] as f64 * s;
            a + (b - a) * fr
        })
        .collect()
}

fn place(buf: &mut [f64], g: &[f64], at: f64, gain: f64) {
    let i = m::round(at * SR) as i64;
    let n_buf = buf.len() as i64;
    if i >= n_buf || i + g.len() as i64 <= 0 {
        return;
    }
    let a = (-i).max(0);
    let n = (g.len() as i64 - a).min(n_buf - i - a);
    for k in 0..n {
        buf[(i + a + k) as usize] += g[(a + k) as usize] * gain;
    }
}

const KERNEL_N: usize = 16384;

/// The kernel cache key: the extra distance and the low-pass corner in 1 % steps (0: none).
fn qkey(v: f64) -> i64 {
    if v < 1.0 {
        0
    } else {
        m::round(m::ln(v) / m::ln(1.01)) as i64
    }
}
fn qval(k: i64) -> f64 {
    m::exp(k as f64 * m::ln(1.01))
}

fn kernel_cache() -> &'static Mutex<BTreeMap<(i64, i64), Arc<Vec<f64>>>> {
    static C: OnceLock<Mutex<BTreeMap<(i64, i64), Arc<Vec<f64>>>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(BTreeMap::new()))
}

/// The minimum-phase kernel for air absorption over r m and an optional 1st-order low-pass at fc (the echoes),
/// designed by the cepstral method at KERNEL_N points and cut where its energy is all but 1e-7. None: identity.
fn kernel(r: f64, fc: Option<f64>) -> Option<Arc<Vec<f64>>> {
    let key = (qkey(r), fc.map(qkey).unwrap_or(0));
    if key == (0, 0) {
        return None;
    }
    if let Some(k) = kernel_cache().lock().unwrap().get(&key) {
        return Some(k.clone());
    }
    let (rq, fq) = (if key.0 == 0 { 0.0 } else { qval(key.0) }, if key.1 == 0 { None } else { Some(qval(key.1)) });
    let n = KERNEL_N;
    let logmag: Vec<f64> = (0..=n / 2)
        .map(|k| {
            let f = k as f64 * SR / n as f64;
            let mut db = -crate::thunder::alpha(f) * rq;
            if let Some(fc) = fq {
                db += -10.0 * m::log10(1.0 + (f / fc) * (f / fc));
            }
            (db / 20.0 * m::LN_10).max(m::ln(1e-6))
        })
        .collect();
    let mut h = dsp::min_phase(&logmag, n);
    dsp::fft(&mut h, true);
    let ir: Vec<f64> = h.iter().map(|z| z.re).collect();
    let total: f64 = ir.iter().map(|v| v * v).sum();
    let mut acc = 0.0;
    let mut len = ir.len();
    for (i, v) in ir.iter().enumerate() {
        acc += v * v;
        if acc >= total * (1.0 - 1e-7) {
            len = (i + 1).max(8);
            break;
        }
    }
    let k = Arc::new(ir[..len].to_vec());
    kernel_cache().lock().unwrap().insert(key, k.clone());
    Some(k)
}

/// x through the kernel, the output as long as x (the reference keeps [:n]).
fn fir(x: &[f64], h: &[f64]) -> Vec<f64> {
    let n = x.len();
    if h.len() <= 48 {
        let mut y = vec![0.0; n];
        for i in 0..n {
            let mut s = 0.0;
            let kmax = h.len().min(i + 1);
            for k in 0..kmax {
                s += h[k] * x[i - k];
            }
            y[i] = s;
        }
        return y;
    }
    // overlap-add: blocks of `hop` samples through FFTs of `m` points
    let m_ = dsp::next_pow2(h.len() * 4).max(2048);
    let hop = m_ - h.len() + 1;
    let mut hf: Vec<C> = (0..m_).map(|i| C::new(if i < h.len() { h[i] } else { 0.0 }, 0.0)).collect();
    dsp::fft(&mut hf, false);
    let mut y = vec![0.0; n + m_];
    let mut buf = vec![C::default(); m_];
    let mut s = 0;
    while s < n {
        let e = (s + hop).min(n);
        for (i, b) in buf.iter_mut().enumerate() {
            *b = C::new(if s + i < e { x[s + i] } else { 0.0 }, 0.0);
        }
        dsp::fft(&mut buf, false);
        for i in 0..m_ {
            buf[i] = buf[i].mul(hf[i]);
        }
        dsp::fft(&mut buf, true);
        for i in 0..m_ {
            y[s + i] += buf[i].re;
        }
        s = e;
    }
    y.truncate(n);
    y
}

fn absorb(x: Vec<f64>, r: f64, fc: Option<f64>) -> Vec<f64> {
    match kernel(r.max(0.0), fc) {
        None => x,
        Some(h) => fir(&x, &h),
    }
}

/// The 4-stage Schroeder all-pass chain (y[n] = -g x[n] + x[n-D] + g y[n-D]).
fn allpass_chain(x: &[f64], rng: &mut Rng) -> Vec<f64> {
    let mut y = x.to_vec();
    for _ in 0..4 {
        let dl = ((rng.range(1.5, 12.0) * SR / 1000.0) as usize).max(1);
        // rng.choice([-1, 1])
        let g = rng.range(0.3, 0.6) * if rng.integers(0, 2) == 0 { -1.0 } else { 1.0 };
        let mut out = vec![0.0; y.len()];
        for i in 0..y.len() {
            let xd = if i >= dl { y[i - dl] } else { 0.0 };
            let yd = if i >= dl { out[i - dl] } else { 0.0 };
            out[i] = -g * y[i] + xd + g * yd;
        }
        y = out;
    }
    y
}

/// The look-ahead peak limiter (reference peak_limit): 1 ms control blocks, the gain the minimum over the look-ahead,
/// instant attack, exponential release; returns the largest gain reduction (dB).
pub fn peak_limit(x: &mut [f64], ceil_db: f64, look_ms: f64, rel_ms: f64) -> f64 {
    let ceil = m::undb(ceil_db);
    let l = (look_ms * SR / 1000.0) as usize;
    let hop = 44usize;
    let n = x.len() / hop + 1;
    let mut a = vec![0.0f64; n];
    for (i, v) in x.iter().enumerate() {
        let b = &mut a[i / hop];
        *b = b.max(v.abs());
    }
    let k = (l + hop - 1) / hop + 1;
    let g: Vec<f64> = (0..n)
        .map(|i| {
            let am = a[i..(i + k).min(n)].iter().fold(0.0f64, |p, v| p.max(*v));
            (ceil / am.max(1e-12)).min(1.0)
        })
        .collect();
    let ar = m::exp(-1.0 / (rel_ms / 1000.0 * SR / hop as f64));
    let mut s = 1.0;
    let mut gs = vec![0.0; n];
    for i in 0..n {
        s = if g[i] < s { g[i] } else { (1.0 - ar) * g[i] + ar * s };
        gs[i] = s;
    }
    for (i, v) in x.iter_mut().enumerate() {
        let p = i as f64 / hop as f64;
        let j = p as usize;
        let gv = if j + 1 < n { gs[j] + (gs[j + 1] - gs[j]) * (p - j as f64) } else { gs[n - 1] };
        *v *= gv;
    }
    -20.0 * m::log10(gs.iter().fold(1.0f64, |p, v| p.min(*v)))
}

pub fn soft_clip(x: &mut [f64], ceil: f64) {
    let k = 0.5 * ceil;
    for v in x.iter_mut() {
        let a = v.abs();
        if a > k {
            *v = v.signum() * (k + (ceil - k) * m::tanh((a - k) / (ceil - k)));
        }
    }
}

fn scaled(x: &[f64], g: f64) -> Vec<f64> {
    x.iter().map(|v| v * g).collect()
}

/// Offline level: the class target on the loudest 3 s, the limiter and the saturator, measured again and corrected up
/// to 8 times until within 0.2 dB. Returns (signal, measured LUFS, largest gain reduction dB).
pub fn level_offline(x: &[f64], target: f64) -> (Vec<f64>, f64, f64) {
    let ceil = m::undb(CEIL_DB);
    let mut y = scaled(x, m::undb(target - crate::loudness::max_short_term(x, SR)));
    let mut z = y.clone();
    let mut l = target;
    let mut gr = 0.0;
    for _ in 0..8 {
        z = y.clone();
        gr = peak_limit(&mut z, CEIL_DB + LIM_HEADROOM_DB, LIM_LOOK_MS, LIM_REL_MS);
        soft_clip(&mut z, ceil);
        l = crate::loudness::max_short_term(&z, SR);
        if (l - target).abs() < 0.2 {
            break;
        }
        y = scaled(&y, m::undb(target - l));
    }
    (z, l, gr)
}

/// Live level: one gain to the distance's target on the render's own loudness estimate, then the limiter and the
/// saturator (no iteration).
pub fn level_live(x: &[f64], d: f64) -> Vec<f64> {
    let mut y = scaled(x, m::undb(live_target(d) - crate::loudness::max_short_term(x, SR)));
    peak_limit(&mut y, CEIL_DB + LIM_HEADROOM_DB, LIM_LOOK_MS, LIM_REL_MS);
    soft_clip(&mut y, m::undb(CEIL_DB));
    y
}

// ------------------------------------------------------------------ one strike (spec section 5)

pub struct Info {
    pub class: &'static str,
    pub dist: f64,
    pub claps: usize,
    pub grains: Vec<(String, f64, f64)>, // (id, at s, gain dB)
    pub echoes: Vec<(f64, f64, f64)>,    // (delay s, fc Hz, gain dB)
}

/// The unlevelled mix of one strike: `dist` given (live) or drawn from the class range; `pre` the silence before the
/// first arrival (offline PRE_S, live 0).
pub fn render_mix(bank: &Bank, kind: Kind, seed: u64, dist: Option<f64>, pre: f64) -> (Vec<f64>, Info) {
    let c = class(kind);
    let mut rng = Rng::new(&[seed]);
    let dist = dist.unwrap_or_else(|| rng.range(c.d.0, c.d.1));
    let n = (LENGTH_S * SR) as usize;
    let mut out = vec![0.0f64; n];
    let mut info = Info { class: c.name, dist, claps: 0, grains: Vec::new(), echoes: Vec::new() };
    let drec = c.d_rec;

    // (the v1 scheduling passes each grain's channel distance; v3 filters only the strike's distance beyond the
    // grain's recording, x the class's abs_scale)
    let take = |out: &mut Vec<f64>, rng: &mut Rng, gi: usize, at: f64, gain: f64, vs: GKind, _channel_r: f64,
                    info: &mut Info| {
        let (lo, hi) = vs.varispeed();
        let g = resample(&bank.grains[gi].pcm, rng.range(lo, hi));
        let extra = (dist - k_rec(bank.grains[gi].src_class)).max(0.0);
        let g = if extra > 1.0 { absorb(g, extra * c.abs_scale, None) } else { g };
        place(out, &g, at, gain);
        info.grains.push((bank.grains[gi].id.clone(), at, 20.0 * m::log10(gain + 1e-12)));
    };

    // body
    let mut body_src = None;
    if let Some(bk) = c.body {
        let mut bp = bank.pool(bk);
        if kind == Kind::Strike {
            let pref: Vec<usize> = bp.iter().copied().filter(|&i| bank.grains[i].centroid_hz > 300.0).collect();
            if !pref.is_empty() {
                bp = pref;
            }
        }
        if !bp.is_empty() {
            let b = bp[pick(&mut rng, bp.len())];
            body_src = Some(bank.grains[b].source_id);
            take(&mut out, &mut rng, b, pre, 1.0, bk, dist - drec, &mut info);
        }
    }
    // crack
    if let Some((lo, hi)) = c.crack_db {
        let cp: Vec<usize> =
            bank.pool(GKind::Crack).into_iter().filter(|&i| Some(bank.grains[i].source_id) != body_src).collect();
        if !cp.is_empty() {
            let g = cp[pick(&mut rng, cp.len())];
            let gdb = rng.range(lo, hi);
            let at = pre + rng.range(-0.004, 0.02);
            let gain = m::undb(gdb) * m::sqrt(300.0 / dist.max(300.0));
            take(&mut out, &mut rng, g, at, gain, GKind::Crack, dist - 300.0, &mut info);
        }
    }
    // claps from the channel's side-on runs (strike, close, mid)
    let claps = if kind != Kind::Far { channel_claps(&mut rng, dist) } else { Vec::new() };
    info.claps = claps.len();
    if let Some(&(_, a0, _)) = claps.first() {
        let cpool = bank.pool(GKind::Clap);
        for &(dt, amp, r) in &claps {
            if cpool.is_empty() {
                break;
            }
            let g = cpool[pick(&mut rng, cpool.len())];
            let mut lvl = m::sqrt(amp / a0).clamp(0.15, 1.0) * m::undb(rng.range(-12.0, -5.0)) * m::pow(dist / r, 0.7)
                * m::exp(-dt / 6.0);
            if kind == Kind::Mid {
                lvl *= 0.5;
            }
            take(&mut out, &mut rng, g, pre + dt, lvl, GKind::Clap, r - drec.min(800.0), &mut info);
        }
    }
    // rolls
    if kind == Kind::Far {
        let mut rp = bank.pool(GKind::FarRoll);
        rp.extend(bank.pool(GKind::MidRoll));
        rp.extend(bank.pool(GKind::Roll));
        let mut tt = pre;
        let mut lvl = 1.0;
        let mut k = 0;
        while tt < LENGTH_S - 3.0 && !rp.is_empty() {
            let g = rp[pick(&mut rng, rp.len())];
            let jit = rng.range(0.7, 1.2);
            let rec = if bank.grains[g].kind == GKind::FarRoll { drec } else { 3000.0 };
            take(&mut out, &mut rng, g, tt, lvl * jit, GKind::FarRoll, dist + (tt - pre) * SOUND * 0.3 - rec, &mut info);
            tt += rng.range(1.4, 2.4);
            if k > 0 {
                lvl *= m::undb(rng.range(-5.0, -1.5));
            }
            k += 1;
        }
        // a slow onset: far thunder swells in
        let fi = (rng.range(0.25, 0.8) * SR) as usize;
        let o = (pre * SR) as usize;
        for i in 0..fi {
            if o + i < out.len() {
                let s = m::sin(m::PI / 2.0 * i as f64 / (fi.max(2) - 1) as f64);
                out[o + i] *= s * s;
            }
        }
    } else {
        let kinds: &[GKind] = if kind == Kind::Mid { &[GKind::MidRoll, GKind::Roll] } else { &[GKind::Roll] };
        let mut tt = pre + rng.range(0.8, 1.4);
        let mut lvl = m::undb(rng.range(-13.0, -8.0)) * if kind == Kind::Mid { 2.0 } else { 1.0 };
        while tt < LENGTH_S - 3.0 {
            let kk = kinds[pick(&mut rng, kinds.len())];
            let rp = bank.pool(kk);
            if rp.is_empty() {
                break;
            }
            let g = rp[pick(&mut rng, rp.len())];
            let jit = rng.range(0.7, 1.2);
            let rec = if kk == GKind::Roll { drec.min(800.0) } else { drec };
            take(&mut out, &mut rng, g, tt, lvl * jit, kk, dist + (tt - pre) * SOUND - rec, &mut info);
            tt += rng.range(1.6, 2.6);
            lvl *= m::undb(rng.range(-7.0, -3.0));
        }
    }
    // sparse terrain echoes of the first 3 s
    let head: Vec<f64> = out[..((pre + 3.0) * SR) as usize].to_vec();
    let ne = int_in(&mut rng, c.n_echo.0, c.n_echo.1);
    for _ in 0..ne {
        let dly = rng.range(0.3, 2.8);
        let fc = rng.range(700.0, 1800.0);
        let gdb = rng.range(c.echo_db.0, c.echo_db.1);
        let e = absorb(head.clone(), dly * SOUND, Some(fc));
        let e = allpass_chain(&e, &mut rng);
        place(&mut out, &e, dly, m::undb(gdb));
        info.echoes.push((dly, fc, gdb));
    }
    // the rumble balance (spec 7): low / high shelves as a minimum-phase filter, crossfaded in linearly from 0.6 to
    // 1.2 s after the first arrival (the crack and the first clap stay as recorded)
    if c.tail_lo_db != 0.0 || c.tail_hi_db != 0.0 {
        let (gl, gh, flo) = (m::undb(c.tail_lo_db), m::undb(c.tail_hi_db), c.tail_f_lo);
        let mag = move |f: f64| {
            let a = (f / flo) * (f / flo);
            let b = (f / 1000.0) * (f / 1000.0);
            m::sqrt(gl + (1.0 - gl) * a / (1.0 + a)) * (1.0 + (gh - 1.0) * b / (1.0 + b))
        };
        let h = crate::methodb::min_phase_ir(&mag, 16384);
        let eq = crate::methodb::conv_crop(&out, &h, out.len());
        for (i, (o, e)) in out.iter_mut().zip(&eq).enumerate() {
            let w = ((i as f64 / SR - pre - 0.6) / 0.6).clamp(0.0, 1.0);
            *o = *o * (1.0 - w) + e * w;
        }
    }
    // 28 Hz high-pass (Butterworth: minimum phase), the 3 s cos² fade-out
    let mut hp = Biquad::highpass(SR, HP_HZ, std::f64::consts::FRAC_1_SQRT_2);
    hp.run(&mut out);
    let fo = (FADE_OUT_S * SR) as usize;
    let s0 = out.len() - fo;
    for i in 0..fo {
        let cth = m::cos(m::PI / 2.0 * i as f64 / (fo - 1) as f64);
        out[s0 + i] *= cth * cth;
    }
    (out, info)
}

// ------------------------------------------------------------------ the live renderer

/// The grain-bank thunder for live-serve (`--thunder grains:PATH`): the bank read once, kept resident (i16).
pub struct Grains {
    pub bank: Arc<Bank>,
}

impl crate::thunderlive::ThunderRenderer for Grains {
    fn name(&self) -> String {
        "grains".into()
    }
    fn render(&mut self, spec: &crate::thunderlive::Spec) -> Vec<f32> {
        let (mix, _) = render_mix(&self.bank, spec.kind, spec.seed as u64, Some(spec.d.max(1.0)), 0.0);
        level_live(&mix, spec.d.max(1.0)).into_iter().map(|v| v as f32).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tiny_bank() -> Bank {
        let mut grains = Vec::new();
        let kinds = [GKind::Body, GKind::Crack, GKind::Clap, GKind::Roll, GKind::MidBody, GKind::MidRoll, GKind::FarRoll];
        for (i, k) in kinds.iter().enumerate() {
            for j in 0..2 {
                let n = 44100 / 2;
                let pcm = (0..n)
                    .map(|t| (m::sin(t as f64 * 0.05 * (1 + i + j) as f64) * 8000.0 * (1.0 - t as f64 / n as f64)) as i16)
                    .collect();
                grains.push(Grain { id: format!("{k:?}_{j}"), kind: *k, src_class: kind_from_u8((i % 4) as u8).unwrap(),
                    source_id: (i * 10 + j) as u32,
                    centroid_hz: 200.0 + 200.0 * j as f32, pcm });
            }
        }
        Bank { grains }
    }

    #[test]
    fn bank_round_trips_and_rejects_damage() {
        let b = tiny_bank();
        let bytes = b.to_bytes();
        let c = Bank::parse(&bytes).unwrap();
        assert_eq!(c.grains.len(), b.grains.len());
        assert_eq!(c.grains[3].pcm, b.grains[3].pcm);
        assert_eq!(c.grains[5].id, b.grains[5].id);
        let mut bad = bytes.clone();
        let k = bad.len() - 20;
        bad[k] ^= 1;
        assert!(Bank::parse(&bad).is_err(), "checksum");
        assert!(Bank::parse(&bytes[..bytes.len() - 3]).is_err(), "truncated");
        assert!(Bank::parse(b"WSGRAIN2\xff\xff\xff\xff").is_err(), "bad count");
        assert!(Bank::parse(b"WSGRAIN1\x01\x00\x00\x00").is_err(), "the v1 format is refused");
        assert_eq!(c.grains[5].src_class, b.grains[5].src_class);
        assert_eq!((to_i16(0.5), to_i16(-1.5), to_i16(f32::NAN), to_i16(1.0), to_i16(-0.25)), (16384, -32768, 0, 32767, -8192));
    }

    #[test]
    fn deterministic_and_levelled() {
        let b = tiny_bank();
        for kind in [Kind::Strike, Kind::Close, Kind::Mid, Kind::Far] {
            let (x1, i1) = render_mix(&b, kind, 7, None, PRE_S);
            let (x2, _) = render_mix(&b, kind, 7, None, PRE_S);
            assert_eq!(x1, x2, "same seed, same bits");
            let c = class(kind);
            assert!(i1.dist >= c.d.0 && i1.dist <= c.d.1);
            let (y, l, _) = level_offline(&x1, c.lufs);
            assert!((l - c.lufs).abs() < 0.3, "{} {l}", c.name);
            let peak = y.iter().fold(0.0f64, |a, v| a.max(v.abs()));
            assert!(peak <= m::undb(CEIL_DB) + 1e-9);
        }
    }

    #[test]
    fn live_target_interpolates() {
        assert_eq!(live_target(50.0), -9.5);
        assert_eq!(live_target(1000.0), -14.0);
        assert!((live_target(m::sqrt(1000.0 * 3500.0)) - -16.0).abs() < 1e-9);
        assert_eq!(live_target(20000.0), -22.0);
    }

    #[test]
    fn kernel_is_identity_at_zero_and_lowpass_far() {
        assert!(kernel(0.0, None).is_none());
        let h = kernel(8000.0, None).unwrap();
        let dc: f64 = h.iter().sum();
        assert!((dc - 1.0).abs() < 0.01, "unity at DC: {dc}");
        // a 4 kHz tone 8 km away is gone; a 100 Hz one stays
        let tone = |f: f64| -> f64 {
            let x: Vec<f64> = (0..20000).map(|i| m::sin(m::TAU * f * i as f64 / SR)).collect();
            let y = fir(&x, &h);
            m::sqrt(y[10000..].iter().map(|v| v * v).sum::<f64>() / 10000.0) / m::sqrt(0.5)
        };
        for f in [100.0, 300.0, 1000.0] {
            let want = m::undb(-crate::thunder::alpha(f) * qval(qkey(8000.0)));
            assert!((tone(f) / want - 1.0).abs() < 0.02, "{f} Hz: {} vs ISO {want}", tone(f));
        }
        assert!(tone(4000.0) < 1e-3);
    }

    #[test]
    fn fir_overlap_add_matches_direct() {
        let x: Vec<f64> = (0..9000).map(|i| m::sin(i as f64 * 0.37) + m::cos(i as f64 * 0.011)).collect();
        let h: Vec<f64> = (0..300).map(|i| m::exp(-(i as f64) / 40.0) * 0.02).collect();
        let y = fir(&x, &h);
        for i in [0usize, 1, 299, 4000, 8999] {
            let d: f64 = (0..h.len().min(i + 1)).map(|k| h[k] * x[i - k]).sum();
            assert!((y[i] - d).abs() < 1e-9, "{i}");
        }
    }
}
