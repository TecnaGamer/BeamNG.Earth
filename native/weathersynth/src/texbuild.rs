//! The recorded weather textures' bank ("recording mix" for rain, wind, drips and hail, after the thunder grain bank):
//! clean stretches ("blocks") of CC0 field recordings, found, checked, levelled and packed for texmix.rs.
//!
//!   weathersynth texbank-build --gb DIR --sources SOURCES.json --out BANKDIR [--threads N] [--max-s S]
//!
//! - **Inputs:** SOURCES.json `{"sources": [{id, cls, title, author, page, license, checked}]}`; the originals are
//!   DIR/originals/fs<ID>.* and every one must match DIR/originals.sha256 before ffmpeg decodes it (as the thunder
//!   builder). Classes: rain_light, rain_moderate, rain_heavy, rain_car, rain_under, drips, gutter, wind_light,
//!   wind_moderate, wind_strong, hail.
//! - **Screening:** 5 s windows through grainbuild::screen_window with the class's rules (the rain-only checks for
//!   outdoor rain: traffic swell, unsteady, <120 Hz rumble, knocks; the hiss-floor rule only for windows under
//!   -40 dBFS, since dense steady rain fits it too), plus: thunder (rain classes: the 20-200 Hz band's 400 ms level
//!   12 dB over its median for >= 1 s blanks [start - 1 s, end + 8 s]); microphone buffeting (wind classes:
//!   the <100 Hz 10 ms level jumping > 12 dB over its 1 s median, more than 0.5 times a second); still raining (drips:
//!   1-8 kHz 20 ms levels p99 - p50 under 4 dB means a steady wash, not drips); a transient outlier (the window's
//!   sample peak more than 24 dB over its RMS; light rain 30, drips 34, hail 30).
//! - **Blocks:** runs of >= 2 clean windows, 0.25 s trimmed at each end, at most MAX_BLOCK_S each (longer runs split).
//! - **Selection:** per class a budget, at most a cap per source, round-robin over the sources (longest first) so
//!   every recording is used before any is used twice.
//! - **Processing:** DC out, a 30 Hz (40 Hz for wind) 2nd-order Butterworth high-pass, then a gain to -30 LUFS
//!   (integrated, K-weighted) per block, lowered if the peak would pass 0.9. No denoising (rain and wind are noise).
//! - **Density check:** a light-rain block must be sparse (2.5-6 kHz envelope kurtosis >= 8), a heavy-rain block dense
//!   (<= 40); others are dropped (BANKDIR/blocks_fit.tsv lists every block).
//! - **The car takes:** a source declared roof_light / roof / roof_heavy (its title says how hard it rains) keeps that;
//!   rain_car takes ("variations") are split into the three by their 2.5-6 kHz envelope kurtosis terciles.
//! - **Screening cache:** the window verdicts per (original sha256, class, SCREEN_VERSION) in --cache DIR (default
//!   BANKDIR/screen_cache), so re-cutting with other selection rules skips the slow part.
//! - **Output:** BANKDIR/tex_bank.bin ("WSTEX1", i16), BANKDIR/manifest.json, BANKDIR/screening.tsv (every window and
//!   why it was rejected), BANKDIR/CREDITS.md. Deterministic: sources in file order, libm maths, fixed-order sums;
//!   threads only split sources.

use crate::dsp::Biquad;
use crate::grainbuild::{self as gb, Out};
use crate::loudness;
use crate::m;
use crate::proto::{self, Json};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const SR: f64 = 44100.0;
const WIN_S: f64 = 5.0;
const TRIM_S: f64 = 0.25;
pub const MAX_BLOCK_S: f64 = 30.0;
const BLOCK_LUFS: f64 = -30.0;
const MAGIC: &[u8; 8] = b"WSTEX1\0\0";
const THUNDER_DB: f64 = 12.0;
const HISS_QUIET_DBFS: f64 = -40.0;
/// drips: the 1-8 kHz 20 ms levels' p99 - p50 under this is a steady wash (rain still falling), not drips. (The
/// after-rain takes measure 4-12 dB: dense soft drips in a reverberant yard; a downpour measures 1-3 dB.)
const DRIP_SPREAD_DB: f64 = 4.0;

pub const CLASSES: [&str; 13] = [
    "rain_light",
    "rain_moderate",
    "rain_heavy",
    "roof_light",
    "roof",
    "roof_heavy",
    "rain_under",
    "drips",
    "gutter",
    "wind_light",
    "wind_moderate",
    "wind_strong",
    "hail",
];

/// The bank's file name; live-serve reads it from next to the program, or from the source tree's assets folder.
#[cfg_attr(not(feature = "audio"), allow(dead_code))]
pub const BANK_FILE: &str = "tex_bank.bin";

#[cfg_attr(not(feature = "audio"), allow(dead_code))]
pub fn default_bank() -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let mut c = vec![dir.join(BANK_FILE)];
    for up in dir.ancestors().take(4) {
        c.push(up.join("assets").join(BANK_FILE));
    }
    c.into_iter().find(|p| p.is_file()).map(|p| p.to_string_lossy().into_owned())
}

pub fn class_index(c: &str) -> Option<usize> {
    CLASSES.iter().position(|k| *k == c)
}

/// (budget s, cap per source s) of a class
fn budget(c: &str) -> (f64, f64) {
    match c {
        "wind_light" | "wind_moderate" | "wind_strong" => (300.0, 120.0),
        "rain_car" => (360.0, 120.0),
        _ => (240.0, 90.0),
    }
}

/// the highest sample-peak-to-RMS (dB) of a clean 5 s window: rain is many small impacts (about 12-20 dB), drips and
/// hail are sparse hits
fn crest_max(c: &str) -> f64 {
    match c {
        // light rain is sparse distinct drops, close ones far above the window's level
        "rain_light" | "roof_light" => *LIGHT_CREST.get().unwrap_or(&30.0),
        "drips" => 34.0,
        "hail" => 30.0,
        _ => 24.0,
    }
}

/// windows over this crest are noted in the verdicts ("transient outlier (crest N dB)"); crest_max decides
const CREST_RECORD_DB: f64 = 24.0;
/// --light-crest DB: the light classes' crest limit (default 30; a variant with the sparsest close-drop takes: 40)
static LIGHT_CREST: std::sync::OnceLock<f64> = std::sync::OnceLock::new();

fn crest_of(reason: &str) -> Option<f64> {
    reason.strip_prefix("transient outlier (crest ")?.strip_suffix(" dB)")?.parse().ok()
}

/// the screening kind grainbuild::screen_window uses for a source class
fn screen_kind(c: &str) -> &'static str {
    match c {
        "rain_light" => "rain_light",
        "rain_moderate" => "rain_moderate",
        "rain_heavy" => "rain_heavy",
        "rain_car" | "roof_light" | "roof" | "roof_heavy" => "rain_car",
        c if c.starts_with("wind") => "wind",
        _ => "rain_cover",
    }
}

#[derive(Clone, Debug)]
pub struct Source {
    pub id: String,
    pub cls: String,
    pub title: String,
    pub author: String,
    pub page: String,
    pub license: String,
    pub checked: String,
}

fn read_sources(p: &Path) -> Result<Vec<Source>, String> {
    let t = std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))?;
    let j = proto::parse_limit(&t, 1 << 24).map_err(|e| format!("{}: {e}", p.display()))?;
    let Some(Json::Arr(list)) = j.get("sources") else { return Err("sources: no list".into()) };
    let s = |r: &Json, k: &str| r.str(k).unwrap_or("").to_string();
    let mut out = Vec::new();
    for r in list {
        let src = Source {
            id: s(r, "id"),
            cls: s(r, "cls"),
            title: s(r, "title"),
            author: s(r, "author"),
            page: s(r, "page"),
            license: s(r, "license"),
            checked: s(r, "checked"),
        };
        if !src.license.contains("publicdomain/zero") {
            return Err(format!("{}: not CC0 ({})", src.id, src.license));
        }
        if src.cls != "rain_car" && class_index(&src.cls).is_none() {
            return Err(format!("{}: unknown class {}", src.id, src.cls));
        }
        out.push(src);
    }
    Ok(out)
}

/// A clean run in an original.
#[derive(Clone, Debug)]
pub struct Run {
    pub src: usize,
    pub t0: f64,
    pub t1: f64,
}

fn fr_db(x: &[f64], win: usize) -> Vec<f64> {
    gb::frame_rms_db(x, win, win)
}

fn median(v: &[f64]) -> f64 {
    crate::npx::median(v)
}

/// The windows' verdicts: (start s, reasons), and the thunder spans blanked.
fn screen_source(x: &[f64], cls: &str) -> Vec<(f64, Vec<String>)> {
    let n = (WIN_S * SR) as usize;
    let nw = x.len() / n;
    let mut blank: Vec<(f64, f64)> = Vec::new();
    if cls.starts_with("rain") || cls == "drips" || cls == "gutter" {
        // thunder: the 20-200 Hz band's 400 ms level (100 ms hop) more than THUNDER_DB over its median for at least
        // 1 s (a rumble; footsteps, knocks and drops on the mic are far shorter), blanked from 1 s before to 8 s after
        let lf = gb::bandpass_fft(x, 20.0, 200.0);
        let (w, hop) = ((0.4 * SR) as usize, (0.1 * SR) as usize);
        let e = gb::frame_rms_db(&lf, w, hop);
        let md = median(&e);
        let mut i = 0;
        while i < e.len() {
            if e[i] > md + THUNDER_DB {
                let mut j = i;
                while j < e.len() && e[j] > md + THUNDER_DB - 3.0 {
                    j += 1;
                }
                if (j - i) as f64 * 0.1 >= 1.0 {
                    blank.push((i as f64 * 0.1 - 1.0, j as f64 * 0.1 + 0.4 + 8.0));
                }
                i = j;
            } else {
                i += 1;
            }
        }
    }
    let kind = screen_kind(cls);
    let mut out = Vec::with_capacity(nw);
    for i in 0..nw {
        let t = i as f64 * WIN_S;
        let w = &x[i * n..(i + 1) * n];
        let mut reasons: Vec<String> = Vec::new();
        if blank.iter().any(|(a, b)| t + WIN_S > *a && t < *b) {
            reasons.push("thunder".into());
        }
        let (_, r) = gb::screen_window(w, kind);
        // the hiss-floor rule (a steady, bright 6-12 kHz band) also fits dense steady rain itself; it rejects only a
        // quiet window here, where recorder hiss would be heard
        let loud = gb::db10(gb::mean_sq(w)) > HISS_QUIET_DBFS;
        reasons.extend(r.into_iter().filter(|s| !(loud && s == "broadband hiss floor" && !cls.starts_with("wind"))));
        if cls.starts_with("wind") {
            let l10 = fr_db(&gb::bandpass_fft(w, 20.0, 100.0), (0.01 * SR) as usize);
            let k = 50usize;
            let mut jumps = 0usize;
            let mut last = -1000i64;
            for j in 3..l10.len() {
                let a = j.saturating_sub(k);
                let b = (j + k + 1).min(l10.len());
                let md = median(&l10[a..b]);
                if l10[j] - md > 12.0 && l10[j] - l10[j - 3] > 9.0 && (j as i64 - last) > 10 {
                    jumps += 1;
                    last = j as i64;
                }
            }
            if jumps as f64 / WIN_S > 0.5 {
                reasons.push("microphone buffeting".into());
            }
        }
        if cls == "drips" {
            let h = fr_db(&gb::bandpass_fft(w, 1000.0, 8000.0), (0.02 * SR) as usize);
            if gb::percentile(&h, 99.0) - gb::percentile(&h, 50.0) < DRIP_SPREAD_DB {
                reasons.push("steady wash (still raining)".into());
            }
        }
        // one hit far above the window's level: a knock, a thunder crack, a drop on the microphone
        let pk = w.iter().fold(0.0f64, |a, v| a.max(v.abs()));
        let crest = 20.0 * m::log10(pk.max(1e-9)) - gb::db10(gb::mean_sq(w));
        if crest > CREST_RECORD_DB {
            reasons.push(format!("transient outlier (crest {crest:.0} dB)"));
        }
        out.push((t, reasons));
    }
    out
}

/// Bump when screen_source's rules change: the cached verdicts (keyed by the original's sha256, its class and this) are
/// then made again.
const SCREEN_VERSION: u32 = 3;

fn read_cache(p: &Path) -> Option<Vec<(f64, Vec<String>)>> {
    let t = std::fs::read_to_string(p).ok()?;
    let mut v = Vec::new();
    for l in t.lines() {
        let (a, b) = l.split_once('\t')?;
        let r: Vec<String> = if b.is_empty() { vec![] } else { b.split("; ").map(|x| x.to_string()).collect() };
        v.push((a.parse().ok()?, r));
    }
    Some(v)
}

fn write_cache(p: &Path, v: &[(f64, Vec<String>)]) {
    let t: String = v.iter().map(|(a, r)| format!("{a}\t{}\n", r.join("; "))).collect();
    let tmp = p.with_extension("tmp");
    if std::fs::write(&tmp, t).is_ok() {
        let _ = std::fs::rename(&tmp, p);
    }
}

/// Whether a block fits its class by its density: light rain is sparse distinct drops (a peaky 2.5-6 kHz envelope,
/// kurtosis >= LIGHT_KURT_MIN), heavy rain a dense roar (kurtosis <= HEAVY_KURT_MAX). Other classes: always.
fn fits_class(cls: &str, kurt: f64) -> bool {
    match cls {
        "rain_light" => kurt >= LIGHT_KURT_MIN,
        "rain_heavy" => kurt <= HEAVY_KURT_MAX,
        _ => true,
    }
}
const LIGHT_KURT_MIN: f64 = 8.0;
const HEAVY_KURT_MAX: f64 = 40.0;

fn runs_of(verdicts: &[(f64, Vec<String>)], src: usize) -> Vec<Run> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < verdicts.len() {
        if !verdicts[i].1.is_empty() {
            i += 1;
            continue;
        }
        let mut j = i;
        while j < verdicts.len() && verdicts[j].1.is_empty() {
            j += 1;
        }
        if j - i >= 2 {
            let (a, b) = (verdicts[i].0 + TRIM_S, verdicts[j - 1].0 + WIN_S - TRIM_S);
            let k = ((b - a) / MAX_BLOCK_S).ceil().max(1.0) as usize;
            let len = (b - a) / k as f64;
            for q in 0..k {
                out.push(Run { src, t0: a + q as f64 * len, t1: a + (q + 1) as f64 * len });
            }
        }
        i = j;
    }
    out
}

/// Envelope statistics of a block: (kurtosis of the 2.5-6 kHz 5 ms envelope, onsets per s >= 6 dB over the 1 s
/// running median of that envelope, spectral centroid Hz 40 Hz-16 kHz).
pub fn block_stats(x: &[f64]) -> (f64, f64, f64) {
    let b = gb::bandpass_fft(x, 2500.0, 6000.0);
    let w = (0.005 * SR) as usize;
    let env: Vec<f64> = (0..b.len() / w).map(|i| m::sqrt(gb::mean_sq(&b[i * w..(i + 1) * w]))).collect();
    let n = env.len().max(1) as f64;
    let mu = env.iter().sum::<f64>() / n;
    let var = env.iter().map(|v| (v - mu) * (v - mu)).sum::<f64>() / n;
    let k4 = env.iter().map(|v| m::pow(v - mu, 4.0)).sum::<f64>() / n;
    let kurt = if var > 0.0 { k4 / (var * var) } else { 0.0 };
    let edb: Vec<f64> = env.iter().map(|v| 20.0 * m::log10(v.max(1e-9))).collect();
    let half = 100usize;
    let mut on = 0usize;
    let mut above = false;
    let mut j = 0;
    while j < edb.len() {
        let a = j.saturating_sub(half);
        let c = (j + half + 1).min(edb.len());
        let md = median(&edb[a..c]);
        let up = edb[j] - md > 6.0;
        if up && !above {
            on += 1;
        }
        above = up;
        j += 1;
    }
    let (f, p) = gb::psd(x, 4096);
    let mut num = 0.0;
    let mut den = 0.0;
    for (fk, pk) in f.iter().zip(&p) {
        if *fk >= 40.0 && *fk < 16000.0 {
            num += fk * pk;
            den += pk;
        }
    }
    (kurt, on as f64 / (x.len() as f64 / SR), if den > 0.0 { num / den } else { 0.0 })
}

pub struct Block {
    pub cls: usize,
    pub src: usize,
    pub t0: f64,
    pub t1: f64,
    pub gain_db: f64,
    pub kurt: f64,
    pub onsets: f64,
    pub centroid: f64,
    pub samples: Vec<f64>,
}

fn process(x: &[f64], cls: &str) -> (Vec<f64>, f64) {
    let mu = x.iter().sum::<f64>() / x.len().max(1) as f64;
    let mut y: Vec<f64> = x.iter().map(|v| v - mu).collect();
    let fc = if cls.starts_with("wind") { 40.0 } else { 30.0 };
    Biquad::highpass(SR, fc, std::f64::consts::FRAC_1_SQRT_2).run(&mut y);
    let l = loudness::integrated(&y, SR, false);
    let mut g = m::undb(BLOCK_LUFS - l);
    let pk = y.iter().fold(0.0f64, |a, v| a.max(v.abs()));
    if pk * g > 0.9 {
        g = 0.9 / pk;
    }
    for v in y.iter_mut() {
        *v *= g;
    }
    (y, m::db(g))
}

pub struct Bank {
    pub blocks: Vec<TexBlock>,
}

pub struct TexBlock {
    pub cls: u8,
    pub src_id: u32,
    pub samples: Vec<f32>,
}

impl Bank {
    pub fn load(path: &str) -> Result<Bank, String> {
        let b = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
        Bank::parse(&b)
    }
    pub fn parse(b: &[u8]) -> Result<Bank, String> {
        if b.len() < 20 || &b[..8] != MAGIC {
            return Err("not a WSTEX1 bank".into());
        }
        let rd32 = |o: usize| u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]);
        let count = rd32(8) as usize;
        if count > 100_000 {
            return Err("bank: bad count".into());
        }
        let mut o = 12;
        let mut heads = Vec::with_capacity(count);
        for _ in 0..count {
            if o + 9 > b.len() {
                return Err("bank: truncated header".into());
            }
            let cls = b[o];
            let src = rd32(o + 1);
            let n = rd32(o + 5) as usize;
            o += 9;
            if cls as usize >= CLASSES.len() {
                return Err("bank: bad class".into());
            }
            heads.push((cls, src, n));
        }
        let total: usize = heads.iter().map(|h| h.2).sum();
        if o + total * 2 + 8 != b.len() {
            return Err("bank: size mismatch".into());
        }
        let data = &b[o..o + total * 2];
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for &v in data {
            h ^= v as u64;
            h = h.wrapping_mul(0x0100_0000_01b3);
        }
        let want = u64::from_le_bytes(b[o + total * 2..].try_into().unwrap());
        if h != want {
            return Err("bank: checksum".into());
        }
        let mut blocks = Vec::with_capacity(count);
        let mut p = 0;
        for (cls, src, n) in heads {
            let s: Vec<f32> = (0..n)
                .map(|i| i16::from_le_bytes([data[(p + i) * 2], data[(p + i) * 2 + 1]]) as f32 / 32767.0)
                .collect();
            p += n;
            blocks.push(TexBlock { cls, src_id: src, samples: s });
        }
        Ok(Bank { blocks })
    }
}

fn to_bytes(blocks: &[Block], srcs: &[Source]) -> Vec<u8> {
    let mut o = Vec::new();
    o.extend_from_slice(MAGIC);
    o.extend_from_slice(&(blocks.len() as u32).to_le_bytes());
    for b in blocks {
        o.push(b.cls as u8);
        o.extend_from_slice(&srcs[b.src].id.parse::<u32>().unwrap_or(0).to_le_bytes());
        o.extend_from_slice(&(b.samples.len() as u32).to_le_bytes());
    }
    let mut data = Vec::new();
    for b in blocks {
        for v in &b.samples {
            data.extend_from_slice(&crate::grainbank::to_i16(*v as f32).to_le_bytes());
        }
    }
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &v in &data {
        h ^= v as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    o.extend_from_slice(&data);
    o.extend_from_slice(&h.to_le_bytes());
    o
}

fn arg(args: &[String], key: &str) -> Option<String> {
    args.iter().position(|a| a == key).and_then(|i| args.get(i + 1).cloned())
}

pub fn run(args: &[String]) -> Result<(), String> {
    let gbdir = PathBuf::from(arg(args, "--gb").ok_or("--gb DIR")?);
    let sp = PathBuf::from(arg(args, "--sources").ok_or("--sources SOURCES.json")?);
    let out = PathBuf::from(arg(args, "--out").ok_or("--out BANKDIR")?);
    let cache = PathBuf::from(arg(args, "--cache").unwrap_or_else(|| out.join("screen_cache").to_string_lossy().into_owned()));
    std::fs::create_dir_all(&cache).map_err(|e| e.to_string())?;
    let threads: usize = arg(args, "--threads").and_then(|v| v.parse().ok()).unwrap_or(1);
    let max_s: f64 = arg(args, "--max-s").and_then(|v| v.parse().ok()).unwrap_or(900.0);
    if let Some(c) = arg(args, "--light-crest").and_then(|v| v.parse().ok()) {
        let _ = LIGHT_CREST.set(c);
    }
    let t0 = std::time::Instant::now();
    let srcs = read_sources(&sp)?;
    let shas = gb::read_sha_list(&gbdir)?;
    std::fs::create_dir_all(&out).map_err(|e| e.to_string())?;
    // per source: decode (checked), screen, runs, and the run samples cut
    let per: Vec<Result<(Vec<(f64, Vec<String>)>, Vec<(Run, Vec<f64>)>, String, String), String>> =
        gb::par_map(srcs.len(), threads, |i| {
            let s = &srcs[i];
            let p = gb::original_of(&gbdir, &s.id).ok_or(format!("fs{}: no original", s.id))?;
            let (mut x, name, sha) = gb::load_original(&p, &shas)?;
            x.truncate((max_s * SR) as usize);
            let key = format!("{sha}_{}_{SCREEN_VERSION}_{}", s.cls, max_s as u64);
            let cp = cache.join(format!("{key}.tsv"));
            let v = match read_cache(&cp) {
                Some(v) => v,
                None => {
                    let v = screen_source(&x, &s.cls);
                    write_cache(&cp, &v);
                    v
                }
            };
            // the class's own crest limit on the recorded outliers (the verdicts keep every window over
            // CREST_RECORD_DB with its crest, so a limit change needs no new screening)
            let v: Vec<(f64, Vec<String>)> = v
                .into_iter()
                .map(|(t, r)| (t, r.into_iter().filter(|w| crest_of(w).map(|c| c > crest_max(&s.cls)).unwrap_or(true)).collect()))
                .collect();
            let runs = runs_of(&v, i);
            let cut: Vec<(Run, Vec<f64>)> = runs
                .into_iter()
                .map(|r| {
                    let a = (r.t0 * SR) as usize;
                    let b = ((r.t1 * SR) as usize).min(x.len());
                    let y = x[a..b].to_vec();
                    (r, y)
                })
                .collect();
            Ok((v, cut, name, sha))
        });
    let mut screening = String::from("source\tclass\tstart_s\tok\treasons\n");
    let mut runs: Vec<(Run, Vec<f64>)> = Vec::new();
    let mut names: BTreeMap<usize, (String, String)> = BTreeMap::new();
    for (i, r) in per.into_iter().enumerate() {
        let (v, cut, name, sha) = r?;
        for (t, why) in &v {
            screening.push_str(&format!("{}\t{}\t{:.1}\t{}\t{}\n", srcs[i].id, srcs[i].cls, t, why.is_empty(), why.join("; ")));
        }
        names.insert(i, (name, sha));
        runs.extend(cut);
    }
    std::fs::write(out.join("screening.tsv"), &screening).map_err(|e| e.to_string())?;
    // processing and stats per run
    let blocks_all: Vec<Block> = gb::par_map(runs.len(), threads, |k| {
        let (r, x) = &runs[k];
        let cls = &srcs[r.src].cls;
        let (y, g) = process(x, cls);
        let (kurt, onsets, centroid) = block_stats(&y);
        let ci = class_index(if cls == "rain_car" { "roof" } else { cls }).unwrap();
        Block { cls: ci, src: r.src, t0: r.t0, t1: r.t1, gain_db: g, kurt, onsets, centroid, samples: y }
    });
    let mut fit_log = String::from("source\tclass\tt0\tt1\tkurtosis\tonsets_per_s\tcentroid\tkept\n");
    for b in &blocks_all {
        let c = &srcs[b.src].cls;
        fit_log.push_str(&format!("{}\t{c}\t{:.2}\t{:.2}\t{:.2}\t{:.2}\t{:.0}\t{}\n", srcs[b.src].id, b.t0, b.t1, b.kurt, b.onsets, b.centroid, fits_class(c, b.kurt)));
    }
    std::fs::write(out.join("blocks_fit.tsv"), fit_log).map_err(|e| e.to_string())?;
    let mut blocks_all: Vec<Block> = blocks_all.into_iter().filter(|b| fits_class(&srcs[b.src].cls, b.kurt)).collect();
    // the car takes split by kurtosis (terciles over the seconds)
    let car: Vec<usize> = (0..blocks_all.len()).filter(|&k| srcs[blocks_all[k].src].cls == "rain_car").collect();
    if !car.is_empty() {
        let mut ord = car.clone();
        ord.sort_by(|a, b| blocks_all[*b].kurt.total_cmp(&blocks_all[*a].kurt));
        let total: f64 = ord.iter().map(|&k| blocks_all[k].t1 - blocks_all[k].t0).sum();
        let mut acc = 0.0;
        for &k in &ord {
            let d = blocks_all[k].t1 - blocks_all[k].t0;
            let mid = (acc + d / 2.0) / total;
            acc += d;
            let c = if mid < 1.0 / 3.0 { "roof_light" } else if mid < 2.0 / 3.0 { "roof" } else { "roof_heavy" };
            blocks_all[k].cls = class_index(c).unwrap();
        }
    }
    // selection: per class, round-robin over sources (their longest runs first), within budget and caps
    let mut chosen: Vec<Block> = Vec::new();
    let mut summary = String::new();
    for (ci, cname) in CLASSES.iter().enumerate() {
        let (bud, cap) = budget(if cname.starts_with("roof") { "rain_car" } else { cname });
        let mut by_src: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for (k, b) in blocks_all.iter().enumerate() {
            if b.cls == ci {
                by_src.entry(b.src).or_default().push(k);
            }
        }
        for v in by_src.values_mut() {
            v.sort_by(|a, b| {
                let (da, db) = (blocks_all[*a].t1 - blocks_all[*a].t0, blocks_all[*b].t1 - blocks_all[*b].t0);
                db.total_cmp(&da).then(a.cmp(b))
            });
        }
        let mut used: BTreeMap<usize, f64> = BTreeMap::new();
        let mut pick: Vec<usize> = Vec::new();
        let mut total = 0.0;
        let mut round = 0;
        loop {
            let mut any = false;
            for (s, v) in &by_src {
                if let Some(&k) = v.get(round) {
                    let d = blocks_all[k].t1 - blocks_all[k].t0;
                    let u = used.get(s).cloned().unwrap_or(0.0);
                    if total + d <= bud && u + d <= cap {
                        pick.push(k);
                        total += d;
                        used.insert(*s, u + d);
                    }
                    any = true;
                }
            }
            if !any {
                break;
            }
            round += 1;
        }
        pick.sort();
        if !pick.is_empty() {
            summary.push_str(&format!("{cname}: {} blocks {:.0} s from {} recordings\n", pick.len(), total, used.len()));
        }
        for k in pick {
            let b = &blocks_all[k];
            chosen.push(Block {
                cls: b.cls,
                src: b.src,
                t0: b.t0,
                t1: b.t1,
                gain_db: b.gain_db,
                kurt: b.kurt,
                onsets: b.onsets,
                centroid: b.centroid,
                samples: b.samples.clone(),
            });
        }
    }
    std::fs::write(out.join("tex_bank.bin"), to_bytes(&chosen, &srcs)).map_err(|e| e.to_string())?;
    // manifest and credits
    let blocks_json: Vec<Out> = chosen
        .iter()
        .enumerate()
        .map(|(k, b)| {
            let s = &srcs[b.src];
            let (name, sha) = names.get(&b.src).cloned().unwrap_or_default();
            Out::Obj(vec![
                ("id".into(), Out::S(format!("{}_{k:03}_{}", CLASSES[b.cls], s.id))),
                ("class".into(), Out::S(CLASSES[b.cls].into())),
                ("source_id".into(), Out::S(s.id.clone())),
                ("source_class".into(), Out::S(s.cls.clone())),
                ("source_file".into(), Out::S(name)),
                ("source_sha256".into(), Out::S(sha)),
                ("t0".into(), Out::F(m::round(b.t0 * 1000.0) / 1000.0)),
                ("t1".into(), Out::F(m::round(b.t1 * 1000.0) / 1000.0)),
                ("gain_db".into(), Out::F(m::round(b.gain_db * 100.0) / 100.0)),
                ("kurtosis_2k5_6k".into(), Out::F(m::round(b.kurt * 100.0) / 100.0)),
                ("onsets_per_s".into(), Out::F(m::round(b.onsets * 100.0) / 100.0)),
                ("centroid_hz".into(), Out::F(m::round(b.centroid))),
                ("title".into(), Out::S(s.title.clone())),
                ("author".into(), Out::S(s.author.clone())),
                ("page".into(), Out::S(s.page.clone())),
                ("license".into(), Out::S(s.license.clone())),
                ("checked".into(), Out::S(s.checked.clone())),
            ])
        })
        .collect();
    gb::write_json(&out.join("manifest.json"), &Out::Obj(vec![("blocks".into(), Out::Arr(blocks_json))]))?;
    let mut cr = String::from(
        "# Credits: recorded weather textures (rain, wind, drips, hail)\n\nEvery block comes from a field recording released \
under Creative Commons Zero (CC0 1.0) on Freesound. CC0 needs no attribution; we credit the recordists anyway, with \
thanks. Each recording was screened in 5 s windows, its clean stretches cut, high-passed and levelled; weathersynth \
layers and crossfades them. Licences were checked on each sound page on the date shown.\n\n| Freesound sound | title | \
recordist | licence | checked | blocks |\n|---|---|---|---|---|---|\n",
    );
    let mut per_src: BTreeMap<usize, (usize, f64, Vec<&str>)> = BTreeMap::new();
    for b in &chosen {
        let e = per_src.entry(b.src).or_insert((0, 0.0, Vec::new()));
        e.0 += 1;
        e.1 += b.t1 - b.t0;
        if !e.2.contains(&CLASSES[b.cls]) {
            e.2.push(CLASSES[b.cls]);
        }
    }
    for (s, (n, d, cl)) in &per_src {
        let x = &srcs[*s];
        cr.push_str(&format!(
            "| [{}]({}) | {} | {} | CC0 1.0 | {} | {n} ({:.0} s: {}) |\n",
            x.id,
            x.page,
            x.title.replace('|', "/"),
            x.author,
            x.checked,
            d,
            cl.join(", ")
        ));
    }
    std::fs::write(out.join("CREDITS.md"), cr).map_err(|e| e.to_string())?;
    print!("{summary}");
    eprintln!("texbank-build: {} blocks, {:.1} s", chosen.len(), t0.elapsed().as_secs_f64());
    Ok(())
}

/// The WSTEX1 bytes of a list of blocks (the layout Bank::parse reads).
pub fn bank_bytes(blocks: &[TexBlock]) -> Vec<u8> {
    let mut o = Vec::new();
    o.extend_from_slice(MAGIC);
    o.extend_from_slice(&(blocks.len() as u32).to_le_bytes());
    for b in blocks {
        o.push(b.cls);
        o.extend_from_slice(&b.src_id.to_le_bytes());
        o.extend_from_slice(&(b.samples.len() as u32).to_le_bytes());
    }
    let mut data = Vec::new();
    for b in blocks {
        for v in &b.samples {
            data.extend_from_slice(&crate::grainbank::to_i16(*v).to_le_bytes());
        }
    }
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &v in &data {
        h ^= v as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    o.extend_from_slice(&data);
    o.extend_from_slice(&h.to_le_bytes());
    o
}

/// The live bank (`texbank-live --bank BIN --out tex_bank.bin [--per-class S] [--max-block S]`): the classes the
/// live engine plays (outdoor rain, the roof, wind), each at most S seconds, taken round-robin over its recordings
/// (each block's middle, at most max-block seconds), so a small file still holds every take. The offline bank keeps
/// everything for the loops.
pub fn run_live(args: &[String]) -> Result<(), String> {
    let bank = Bank::load(&arg(args, "--bank").ok_or("--bank BIN")?)?;
    let out = arg(args, "--out").ok_or("--out BIN")?;
    let per: f64 = arg(args, "--per-class").and_then(|v| v.parse().ok()).unwrap_or(36.0);
    let maxb: f64 = arg(args, "--max-block").and_then(|v| v.parse().ok()).unwrap_or(12.0);
    let live = [
        "rain_light",
        "rain_moderate",
        "rain_heavy",
        "roof_light",
        "roof",
        "roof_heavy",
        "wind_light",
        "wind_moderate",
        "wind_strong",
    ];
    let mut keep: Vec<TexBlock> = Vec::new();
    let mut report = String::new();
    for c in live {
        let ci = class_index(c).unwrap() as u8;
        let mut by: Vec<(u32, Vec<usize>)> = Vec::new();
        for (k, b) in bank.blocks.iter().enumerate() {
            if b.cls != ci {
                continue;
            }
            match by.iter_mut().find(|(s, _)| *s == b.src_id) {
                Some(e) => e.1.push(k),
                None => by.push((b.src_id, vec![k])),
            }
        }
        let mut total = 0.0;
        let mut round = 0;
        let mut n = 0;
        'outer: loop {
            let mut any = false;
            for (_, v) in &by {
                let Some(&k) = v.get(round) else { continue };
                any = true;
                let s = &bank.blocks[k].samples;
                let want = ((maxb.min(per - total)) * SR) as usize;
                if want < (6.0 * SR) as usize {
                    break 'outer;
                }
                let len = want.min(s.len());
                let a = (s.len() - len) / 2;
                keep.push(TexBlock { cls: ci, src_id: bank.blocks[k].src_id, samples: s[a..a + len].to_vec() });
                total += len as f64 / SR;
                n += 1;
            }
            if !any {
                break;
            }
            round += 1;
        }
        report.push_str(&format!("{c}: {n} blocks {total:.0} s\n"));
    }
    let bytes = bank_bytes(&keep);
    std::fs::write(&out, &bytes).map_err(|e| e.to_string())?;
    print!("{report}{} bytes\n", bytes.len());
    Ok(())
}
