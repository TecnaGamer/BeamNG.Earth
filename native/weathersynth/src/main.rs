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

mod analyze;
mod dsp;
mod live;
mod loudness;
mod m;
mod rain;
mod rng;
mod thunder;
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
        "roof_light" | "roof" | "roof_heavy" => rain::finish_loop(rain::roof(seed, &sc, &out), &target),
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
                        render_rain(&sub, seed, shift, &mut medium_raw)?
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
            }
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
        "drop-spectra" => print!("{}", rain::drop_spectra()),
        "thunder-bench" => {
            let mm: f64 = arg(&args, "--micro").unwrap_or("0.25".into()).parse().map_err(|_| "bad --micro")?;
            print!("{}", live::thunder_bench(mm));
        }
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
