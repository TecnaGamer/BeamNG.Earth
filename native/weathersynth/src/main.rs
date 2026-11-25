//! weathersynth: BeamNG.Earth's thunder and rain made from physics, rendered offline into the mod's .ogg files.
//! BeamNG plays sounds from files only (Engine.Audio.playOnce / createSource take a file name; the Lua API has no way to
//! push samples), so the synthesis runs here and the game plays its output.
//!
//! Every render is seeded and deterministic: the same seed gives the same samples on any platform (libm maths, one
//! thread, fixed summation order). The Ogg files come from ffmpeg's libvorbis, mono 44.1 kHz.
//!
//!   weathersynth thunder --class strike|close|mid|far --seed N --out FILE.wav
//!   weathersynth rain --kind light|light_d1|medium|heavy|roof_light|roof|roof_heavy|muffled --seed N --out FILE.wav
//!   weathersynth set --out DIR --set K [--ogg] [--only thunder|rain]   the files with the mod's names (seeds from K)
//!   weathersynth analyze FILE.wav ...               a table describing each file
//!   weathersynth live-bench [--seconds S] [--block N] [--wav F]   the live rain engine timed (prototype)
//!   weathersynth thunder-bench [--micro M]           live thunder render times by distance
//!   weathersynth live-serve [--thunder grains|procedural|files:DIR]   the live weather sounds (serve.rs)
//!   weathersynth grainbank-pack --manifest M --out BIN     the grain bank's manifest + grains/*.ogg into one i16 .bin
//!   weathersynth grains-render --bank BIN --class C --seed N [--dist M] [--live] --out F.wav   one grain-bank strike
//!   weathersynth grains-set --bank BIN --out DIR [--seeds A,B] [--ogg]   bne_thunder_<class>1/2 from the grain bank

mod analyze;
#[cfg(feature = "audio")]
mod audio;
mod dsp;
mod grainbank;
mod live;
mod loudness;
mod m;
#[cfg_attr(not(feature = "audio"), allow(dead_code))]
mod proto;
mod rain;
mod rng;
#[cfg_attr(not(feature = "audio"), allow(dead_code))]
mod serve;
mod storm;
mod thunder;
#[cfg_attr(not(feature = "audio"), allow(dead_code))]
mod thunderlive;
mod wav;

use std::process::Command;

fn arg(args: &[String], key: &str) -> Option<String> {
    args.iter().position(|a| a == key).and_then(|i| args.get(i + 1).cloned())
}

/// `ceil_shift` (dB, <= 0) lowers the limiter's ceiling (the Ogg check below asks for it).
fn render_thunder(class: &str, seed: u64, ceil_shift: f64) -> Result<(Vec<f64>, String), String> {
    let mut cl = thunder::class(class).ok_or(format!("unknown thunder class {class}"))?;
    cl.ceil_db += ceil_shift;
    let r = thunder::render(seed, &cl);
    Ok((r.signal, r.info))
}

/// The rain loops. `medium_raw` reuses an outdoor medium render (the muffled and roof loops derive from it).
fn render_rain(
    kind: &str,
    seed: u64,
    ceil_shift: f64,
    cache: &mut Vec<(String, u64, Vec<f64>, String)>,
) -> Result<(Vec<f64>, String), String> {
    // light / medium / heavy outdoors; roof_light / roof / roof_heavy inside the car; muffled from the medium loop
    let scene_name = match kind {
        "light" | "light_d2" | "roof_light" => "light",
        "light_d1" => "light_d1",
        "medium" | "roof" | "muffled" => "medium",
        "heavy" | "roof_heavy" => "heavy",
        _ => return Err(format!("unknown rain kind {kind}")),
    };
    let sc = rain::scene(scene_name).unwrap();
    // -2.1 dBTP in the WAV: Vorbis q4 adds up to ~0.5 dB of overshoot (the Ogg check lowers it further if needed)
    let target = rain::LoopTarget { lufs: sc.lufs, ceil_db: -2.1 + ceil_shift };
    // the scene's outdoor render, kept for the roof and muffled loops of the same seed
    let mut outdoor = |seed: u64| -> (Vec<f64>, String) {
        if let Some(c) = cache.iter().find(|c| c.0 == sc.name && c.1 == seed) {
            return (c.2.clone(), c.3.clone());
        }
        let (v, st) = rain::outdoor(seed, &sc);
        let info = format!(
            "drops_per_s_150m={:.0} big_drops_per_s_3m={:.0} rendered_drops_per_s={:.0} drips_per_s={:.1} wash_share_db={:.1}",
            st.drops_per_s, st.big_per_s_3m, st.rendered_per_s, st.drips_per_s, st.wash_share_db
        );
        cache.push((sc.name.to_string(), seed, v.clone(), info.clone()));
        (v, info)
    };
    let (out, oinfo) = outdoor(seed);
    let (y, g) = match kind {
        "roof_light" | "roof" | "roof_heavy" => rain::finish_loop_shelf(rain::roof(seed, &sc, &out), &target, false),
        "muffled" => {
            // the mod's derivation from the finished medium loop
            let (med, g) = rain::finish_loop(out, &target);
            let muf = rain::muffle(&med);
            let env = loudness::tp_envelope(&muf, true);
            (rain::limit_loop(&muf, &env, 1.0, target.ceil_db), g)
        }
        _ => rain::finish_loop(out, &target),
    };
    Ok((
        y,
        format!("kind={kind} seed={seed} scene={} rate_mm_h={} {oinfo} makeup_gain_db={g:.1}", sc.name, sc.rate_mm_h),
    ))
}

/// One grain-bank render's description (the reference JSON's main fields, on one line).
fn grains_info(i: &grainbank::Info, lufs: f64, gr: f64, ms: f64) -> String {
    let ids: Vec<String> = i.grains.iter().map(|g| format!("{}@{:.3}/{:.1}", g.0, g.1, g.2)).collect();
    let ech: Vec<String> = i.echoes.iter().map(|e| format!("{:.3}s/{:.0}Hz/{:.1}dB", e.0, e.1, e.2)).collect();
    format!(
        "class={} dist_m={:.1} claps={} lufs_st_max={:.2} limiter_gr_db={:.1} render_ms={:.0} echoes=[{}] grains=[{}]",
        i.class,
        i.dist,
        i.claps,
        lufs,
        gr,
        ms,
        ech.join(" "),
        ids.join(" ")
    )
}

fn encode_ogg(wav: &str, ogg: &str, q: &str) -> Result<(), String> {
    let st = Command::new("ffmpeg")
        .args(["-y", "-hide_banner", "-loglevel", "error", "-i", wav, "-ac", "1", "-ar", "44100", "-c:a", "libvorbis"])
        .args(["-q:a", q, "-map_metadata", "-1", "-fflags", "+bitexact", ogg])
        .status()
        .map_err(|e| format!("ffmpeg: {e}"))?;
    if !st.success() {
        return Err(format!("ffmpeg failed for {wav}"));
    }
    Ok(())
}

/// Decode an Ogg (ffmpeg) and measure its true peak.
fn ogg_true_peak(ogg: &str, tmp_wav: &str) -> Result<f64, String> {
    let st = Command::new("ffmpeg")
        .args(["-y", "-hide_banner", "-loglevel", "error", "-i", ogg, "-ac", "1", "-c:a", "pcm_f32le", tmp_wav])
        .status()
        .map_err(|e| format!("ffmpeg: {e}"))?;
    if !st.success() {
        return Err(format!("ffmpeg could not decode {ogg}"));
    }
    let (x, _) = wav::read(tmp_wav)?;
    let _ = std::fs::remove_file(tmp_wav);
    Ok(loudness::true_peak_db(&x, false))
}

/// One file of a set: render, write the WAV, and with `ogg` encode it, decode it, measure its true peak and render it
/// again with a lower limiter ceiling until the class's target holds.
fn job(
    dir: &str,
    name: &str,
    what: &str,
    sub: &str,
    seed: u64,
    ogg: bool,
    medium_raw: &mut Vec<(String, u64, Vec<f64>, String)>,
) -> Result<(), String> {
    let (name, what, sub) = (name.to_string(), what.to_string(), sub.to_string());
    // The true-peak targets hold for the Ogg the game plays: Vorbis overshoots the WAV's peaks (more on
    // bright files), so each Ogg is decoded and measured, and rendered again with a lower ceiling if over.
    let tp_target = match sub.as_str() {
        "strike" => -0.3,
        _ if what == "thunder" => -1.0,
        _ => -1.5,
    };
    let mut shift = 0.0;
    for attempt in 0..4 {
        let (sig, info) = if what == "thunder" {
            render_thunder(&sub, seed, shift)?
        } else {
            render_rain(&sub, seed, shift, medium_raw)?
        };
        let w = format!("{dir}/wav/{name}.wav");
        wav::write_f32(&w, &sig, 44100).map_err(|e| e.to_string())?;
        if !ogg {
            println!("{name}\t{info}");
            break;
        }
        let q = if sub == "strike" { "5" } else { "4" };
        let o = format!("{dir}/{name}.ogg");
        encode_ogg(&w, &o, q)?;
        let tp = ogg_true_peak(&o, &format!("{dir}/wav/{name}.check.wav"))?;
        if tp <= tp_target || attempt == 3 {
            println!("{name}\t{info} ceiling_shift_db={shift:.2} ogg_true_peak_dbtp={tp:.2}");
            break;
        }
        shift -= tp - tp_target + 0.15;
    }
    Ok(())
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(|s| s.as_str()).unwrap_or("");
    match cmd {
        "thunder" | "rain" => {
            let seed: u64 = arg(&args, "--seed").unwrap_or("1".into()).parse().map_err(|_| "bad --seed")?;
            let out = arg(&args, "--out").ok_or("--out FILE.wav")?;
            let (sig, info) = if cmd == "thunder" {
                render_thunder(&arg(&args, "--class").ok_or("--class")?, seed, 0.0)?
            } else {
                render_rain(&arg(&args, "--kind").ok_or("--kind")?, seed, 0.0, &mut Vec::new())?
            };
            wav::write_f32(&out, &sig, 44100).map_err(|e| e.to_string())?;
            println!("{out}\t{info}");
        }
        "set" => {
            let dir = arg(&args, "--out").ok_or("--out DIR")?;
            let k: u64 = arg(&args, "--set").unwrap_or("1".into()).parse().map_err(|_| "bad --set")?;
            let ogg = args.iter().any(|a| a == "--ogg");
            std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
            let only = arg(&args, "--only"); // thunder | rain
            let mut jobs: Vec<(String, String, String, u64)> = Vec::new();
            if only.as_deref() != Some("rain") {
                for class in ["strike", "close", "mid", "far"] {
                    for v in 1..=2u64 {
                        jobs.push((format!("bne_thunder_{class}{v}"), "thunder".into(), class.into(), 100 * k + v));
                    }
                }
            }
            if only.as_deref() != Some("thunder") {
                for kind in ["light", "medium", "heavy", "roof_light", "roof", "roof_heavy", "muffled"] {
                    jobs.push((format!("bne_rain_{kind}"), "rain".into(), kind.into(), 100 * k + 1));
                }
            }
            let mut medium_raw = Vec::new();
            for (name, what, sub, seed) in jobs {
                job(&dir, &name, &what, &sub, seed, ogg, &mut medium_raw)?;
            }
        }
        "job" => {
            // one file, as `set` makes it: --out DIR --name NAME --what thunder|rain --sub CLASS|KIND --seed N [--ogg]
            let dir = arg(&args, "--out").ok_or("--out DIR")?;
            let name = arg(&args, "--name").ok_or("--name")?;
            let what = arg(&args, "--what").ok_or("--what")?;
            let sub = arg(&args, "--sub").ok_or("--sub")?;
            let seed: u64 = arg(&args, "--seed").ok_or("--seed")?.parse().map_err(|_| "bad --seed")?;
            let ogg = args.iter().any(|a| a == "--ogg");
            std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
            job(&dir, &name, &what, &sub, seed, ogg, &mut Vec::new())?;
        }
        "live-bench" => {
            // the live rain engine through a scripted sweep, timed (no audio device): --seconds S --block N [--wav F]
            let secs: f64 = arg(&args, "--seconds").unwrap_or("60".into()).parse().map_err(|_| "bad --seconds")?;
            let block: usize = arg(&args, "--block").unwrap_or("512".into()).parse().map_err(|_| "bad --block")?;
            let fixed = arg(&args, "--rate").map(|r| {
                let f = |k: &str, d: f64| arg(&args, k).and_then(|v| v.parse().ok()).unwrap_or(d);
                (r.parse().unwrap_or(5.0), f("--shelter", 1.0), f("--cabin", 0.0))
            });
            println!("{}", live::bench_with(secs, block, arg(&args, "--wav").as_deref(), fixed));
        }
        "thunder-bench" => {
            let mm: f64 = arg(&args, "--micro").unwrap_or("0.25".into()).parse().map_err(|_| "bad --micro")?;
            print!("{}", live::thunder_bench(mm));
        }
        "drop-spectra" => print!("{}", rain::drop_spectra()),
        #[cfg(feature = "audio")]
        "live-play" => {
            // the device prototype: the live rain out of the default output device (shared mode, output only).
            // --seconds S, then either fixed --rate R [--shelter S] [--cabin C] or the scripted sweep
            let secs: f64 = arg(&args, "--seconds").unwrap_or("30".into()).parse().map_err(|_| "bad --seconds")?;
            let fixed = arg(&args, "--rate").map(|r| {
                let f = |k: &str, d: f64| arg(&args, k).and_then(|v| v.parse().ok()).unwrap_or(d);
                (r.parse().unwrap_or(5.0), f("--shelter", 1.0), f("--cabin", 0.0))
            });
            let c0 = live::Controls { rain_mm_h: 0.0, shelter: 1.0, cabin: 0.0, volume: 0.5, muted: false };
            let out = audio::open(1, audio::Mix { rain: c0, thunder: 0.0 }, std::sync::Arc::new(thunderlive::Bus::new()), 0)?;
            println!("device={} rate={} channels={}", out.device_name, out.sample_rate, out.channels);
            let t0 = std::time::Instant::now();
            let mut last = -1i64;
            while t0.elapsed().as_secs_f64() < secs {
                let t = t0.elapsed().as_secs_f64();
                let (rate, shelter, cabin) = fixed.unwrap_or_else(|| {
                    let fr = t / secs;
                    if fr < 0.25 { (0.7, 1.0, 0.0) } else if fr < 0.5 { (5.0, 1.0, 0.0) } else if fr < 0.75 { (35.0, 1.0, 0.0) } else { (35.0, 1.0, 1.0) }
                });
                if let Ok(mut c) = out.mix.lock() {
                    c.rain = live::Controls { rain_mm_h: rate, shelter, cabin, volume: 0.5, muted: false };
                }
                if t as i64 != last {
                    last = t as i64;
                    println!(
                        "t={:.0}s rate={rate} cabin={cabin} ring={} underruns={}",
                        t,
                        out.ring.filled(),
                        out.ring.underruns.load(std::sync::atomic::Ordering::Relaxed)
                    );
                }
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
        }
        "live-serve" => serve::run(&args)?,
        "grainbank-pack" => {
            let man = arg(&args, "--manifest").ok_or("--manifest")?;
            let out = arg(&args, "--out").unwrap_or_else(|| format!("assets/{}", grainbank::BANK_FILE));
            println!("{}", grainbank::pack(&man, &out)?);
        }
        "grains-render" => {
            let bank = grainbank::Bank::load(&arg(&args, "--bank").ok_or("--bank")?)?;
            let kind = proto::Kind::from_str(&arg(&args, "--class").ok_or("--class")?).ok_or("bad --class")?;
            let seed: u64 = arg(&args, "--seed").ok_or("--seed")?.parse().map_err(|_| "bad --seed")?;
            let dist: Option<f64> = arg(&args, "--dist").and_then(|v| v.parse().ok());
            let out = arg(&args, "--out").ok_or("--out")?;
            let live = args.iter().any(|a| a == "--live");
            let t0 = std::time::Instant::now();
            let (mix, info) = grainbank::render_mix(&bank, kind, seed, dist, if live { 0.0 } else { grainbank::PRE_S });
            let (y, lufs, gr) = if live {
                (grainbank::level_live(&mix, info.dist), f64::NAN, f64::NAN)
            } else {
                grainbank::level_offline(&mix, grainbank::class(kind).lufs)
            };
            let ms = t0.elapsed().as_secs_f64() * 1000.0;
            wav::write_f32(&out, &y, 44100).map_err(|e| e.to_string())?;
            println!("{}", grains_info(&info, lufs, gr, ms));
        }
        "grains-set" => {
            let bank = grainbank::Bank::load(&arg(&args, "--bank").ok_or("--bank")?)?;
            let dir = arg(&args, "--out").ok_or("--out")?;
            let seeds: Vec<u64> = arg(&args, "--seeds")
                .unwrap_or_else(|| "1,2".into())
                .split(',')
                .filter_map(|v| v.trim().parse().ok())
                .collect();
            let ogg = args.iter().any(|a| a == "--ogg");
            std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
            for kind in [proto::Kind::Strike, proto::Kind::Close, proto::Kind::Mid, proto::Kind::Far] {
                for (i, seed) in seeds.iter().enumerate() {
                    let name = format!("bne_thunder_{}{}", kind.name(), i + 1);
                    let (mix, info) = grainbank::render_mix(&bank, kind, *seed, None, grainbank::PRE_S);
                    let (y, lufs, gr) = grainbank::level_offline(&mix, grainbank::class(kind).lufs);
                    let w = format!("{dir}/wav/{name}.wav");
                    wav::write_f32(&w, &y, 44100).map_err(|e| e.to_string())?;
                    let peak = y.iter().fold(0.0f64, |a, v| a.max(v.abs()));
                    let mut line = format!("{name}\tseed={seed} peak_dbfs={:.2} {}", m::db(peak), grains_info(&info, lufs, gr, f64::NAN));
                    if ogg {
                        let o = format!("{dir}/{name}.ogg");
                        encode_ogg(&w, &o, "6")?;
                        let tp = ogg_true_peak(&o, &format!("{dir}/wav/{name}.check.wav"))?;
                        line += &format!(" ogg_true_peak_dbtp={tp:.2}");
                    }
                    println!("{line}");
                }
            }
        }
        "storm-dump" => print!("{}", storm::dump()),
        "analyze" => {
            println!("{}", analyze::header());
            for f in &args[2..] {
                let (x, fs) = wav::read(f)?;
                let name = std::path::Path::new(f).file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
                println!("{}", analyze::describe(&name, &x, fs as f64));
            }
        }
        _ => {
            eprintln!("usage: weathersynth thunder|rain|set|analyze ... (see src/main.rs)");
            std::process::exit(2);
        }
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}
