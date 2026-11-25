//! weathersynth: BeamNG.Earth's thunder and rain made from physics, rendered offline into the mod's .ogg files.
//! BeamNG plays sounds from files only (Engine.Audio.playOnce / createSource take a file name; the Lua API has no way to
//! push samples), so the synthesis runs here and the game plays its output.
//!
//! Every render is seeded and deterministic: the same seed gives the same samples on any platform (libm maths, one
//! thread, fixed summation order). The Ogg files come from ffmpeg's libvorbis, mono 44.1 kHz.
//!
//!   weathersynth thunder --class strike|close|mid|far --seed N --out FILE.wav
//!   weathersynth rain --kind light|medium|heavy|roof|muffled --seed N --out FILE.wav
//!   weathersynth set --out DIR --set K [--ogg]       all 13 files with the mod's names (seeds from K)
//!   weathersynth analyze FILE.wav ...               a table describing each file

mod analyze;
mod dsp;
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
    medium_raw: &mut Option<(u64, Vec<f64>)>,
) -> Result<(Vec<f64>, String), String> {
    let rate = rain::rate_for(kind).ok_or(format!("unknown rain kind {kind}"))?;
    // -2.1 dBTP in the WAV: Vorbis q4 adds up to ~0.5 dB of overshoot, the files then stay under -1.5 dBTP
    let target = rain::LoopTarget { lufs: -20.0, ceil_db: -2.1 + ceil_shift };
    let mut medium = |seed: u64| -> Vec<f64> {
        if let Some((s, v)) = medium_raw {
            if *s == seed {
                return v.clone();
            }
        }
        let v = rain::outdoor(seed, 6.0, "rain-outdoor");
        *medium_raw = Some((seed, v.clone()));
        v
    };
    let (y, g) = match kind {
        "roof" => {
            let out = medium(seed);
            rain::finish_loop(rain::roof(seed, rate, &out), &target)
        }
        "muffled" => {
            // the mod's derivation from the finished medium loop
            let (med, g) = rain::finish_loop(medium(seed), &target);
            let muf = rain::muffle(&med);
            let env = loudness::tp_envelope(&muf, true);
            (rain::limit_loop(&muf, &env, 1.0, target.ceil_db), g)
        }
        "medium" => rain::finish_loop(medium(seed), &target),
        _ => rain::finish_loop(rain::outdoor(seed, rate, "rain-outdoor"), &target),
    };
    Ok((y, format!("kind={kind} seed={seed} rate_mm_h={rate} makeup_gain_db={g:.1}")))
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
                render_rain(&arg(&args, "--kind").ok_or("--kind")?, seed, 0.0, &mut None)?
            };
            wav::write_f32(&out, &sig, 44100).map_err(|e| e.to_string())?;
            println!("{out}\t{info}");
        }
        "set" => {
            let dir = arg(&args, "--out").ok_or("--out DIR")?;
            let k: u64 = arg(&args, "--set").unwrap_or("1".into()).parse().map_err(|_| "bad --set")?;
            let ogg = args.iter().any(|a| a == "--ogg");
            std::fs::create_dir_all(format!("{dir}/wav")).map_err(|e| e.to_string())?;
            let mut jobs: Vec<(String, String, String, u64)> = Vec::new();
            for class in ["strike", "close", "mid", "far"] {
                for v in 1..=2u64 {
                    jobs.push((format!("bne_thunder_{class}{v}"), "thunder".into(), class.into(), 100 * k + v));
                }
            }
            for kind in ["light", "medium", "heavy", "roof", "muffled"] {
                jobs.push((format!("bne_rain_{kind}"), "rain".into(), kind.into(), 100 * k + 1));
            }
            let mut medium_raw = None;
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
