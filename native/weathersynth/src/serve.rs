//! `weathersynth live-serve`: the live weather sounds for the world server (server/liveaudio.py starts it).
//!
//! stdin: the mod's 'weather_audio', one JSON line each (proto.rs). EOF: fade out and exit.
//! stdout: a state line every STATE_S, {"state":"live","latencyMs":..,"underruns":.. (from 1 s in),"renderer":..,"rendered":..,
//! "late":..,"dropped":..}, or {"state":"failed","reason":..} once before a non-zero exit (no device, the device lost).
//! The server forwards each as 'audio_state'; the mod plays its pre-recorded sounds whenever they stop.
//!
//! Quiet (rain and thunder faded to silence, the device kept open) while the mod says muted (paused, unfocused), when
//! its lines stop for HEARTBEAT_S (a hung game), and in Pre-recorded mode.
//!
//!   weathersynth live-serve [--thunder grains|grains:PATH|procedural|files:DIR] [--seed N] [--buffer FRAMES (default 256)]

use crate::live::Controls;
use crate::proto::Msg;

pub const STATE_S: f64 = 0.5;
pub const HEARTBEAT_S: f64 = 1.0;
/// the thunder's volume in a tunnel or under a deck (storm.lua MUFFLED)
pub const MUFFLED: f64 = 0.45;
/// device reopens allowed per minute before 'failed'
pub const REOPEN_MAX: usize = 5;
/// ... within this window
pub const REOPEN_WINDOW_S: f64 = 120.0;
/// failures in the first GRACE_S after the start do not count toward giving up
pub const GRACE_S: f64 = 60.0;
/// the first open tried again after these waits
pub const OPEN_RETRY_S: [f64; 2] = [0.5, 1.5];
/// a start on a machine busier than this (the 1-minute load per cpu, Linux) asks for twice the first buffer
pub const BUSY_LOAD: f64 = 1.0;

/// Pure: the first buffer for the load per cpu (None: not known).
pub fn start_buffer(first: u32, load_per_cpu: Option<f64>) -> u32 {
    match load_per_cpu {
        Some(l) if l > BUSY_LOAD => grown_buffer(first).unwrap_or(first),
        _ => first,
    }
}

/// The 1-minute load average per cpu (Linux /proc/loadavg), None elsewhere.
pub fn load_per_cpu() -> Option<f64> {
    let s = std::fs::read_to_string("/proc/loadavg").ok()?;
    let l: f64 = s.split_whitespace().next()?.parse().ok()?;
    let n = std::thread::available_parallelism().ok()?.get() as f64;
    Some(l / n)
}
/// the device buffer doubles on trouble (a stream error, or XRUN_GROW xruns within XRUN_WINDOW_S) up to this, and
/// halves again (not below the first) after SHRINK_AFTER_S without trouble: a load burst does not keep the latency up
pub const BUFFER_MAX: u32 = 2048;
pub const XRUN_GROW: usize = 3;
pub const XRUN_WINDOW_S: f64 = 60.0;
pub const SHRINK_AFTER_S: f64 = 300.0;

/// Pure: the buffer to step back down to after a quiet SHRINK_AFTER_S, or None (at the first size already).
pub fn shrunk_buffer(frames: u32, first: u32) -> Option<u32> {
    if frames > first {
        Some((frames / 2).max(first))
    } else {
        None
    }
}

/// Pure: the next device buffer after trouble (doubled, at most BUFFER_MAX), or None when it is at the most already.
pub fn grown_buffer(frames: u32) -> Option<u32> {
    if frames >= BUFFER_MAX {
        None
    } else {
        Some((frames.max(64) * 2).min(BUFFER_MAX))
    }
}

/// The rain's controls and the thunders' master gain for the last message (None: none yet) and whether it is stale.
pub fn mix_for(m: Option<&Msg>, stale: bool) -> (Controls, f64) {
    let Some(m) = m else {
        return (Controls { rain_mm_h: 0.0, shelter: 1.0, cabin: 0.0, volume: 0.0, muted: true }, 0.0);
    };
    let quiet = !m.live || m.muted || stale;
    let rain = Controls {
        rain_mm_h: m.rain_mm_h,
        shelter: m.shelter,
        cabin: if m.cabin { 1.0 } else { 0.0 },
        volume: m.volume * m.duck * m.rain_vol,
        // (in a tunnel the rain is not heard: the mod's own goes to its muffled layer, ours stays quiet for now)
        muted: quiet || m.tunnel,
    };
    let muffle = if m.tunnel || m.shelter < 0.5 { MUFFLED } else { 1.0 };
    let thunder = if quiet { 0.0 } else { m.thunder_volume * muffle };
    (rain, thunder)
}

/// buf: (the largest callback in frames, the ring's fill target in frames, whether the fixed buffer was taken)
pub fn state_line(latency_ms: f64, underruns: u64, renderer: &str, counts: (u64, u64, u64, usize), buf: (u32, u32, bool)) -> String {
    format!(
        "{{\"state\":\"live\",\"latencyMs\":{:.1},\"underruns\":{underruns},\"renderer\":{},\"rendered\":{},\"late\":{},\"dropped\":{},\"callbackFrames\":{},\"targetFrames\":{},\"fixedBuffer\":{}}}",
        latency_ms,
        crate::proto::quote(renderer),
        counts.0,
        counts.1,
        counts.2,
        buf.0,
        buf.1,
        buf.2
    )
}

/// The ready thunders for the state line: [[id, d], ...].
pub fn ready_json(r: &[(String, f64)]) -> String {
    format!("[{}]", r.iter().map(|(id, d)| format!("[{},{:.1}]", crate::proto::quote(id), d)).collect::<Vec<_>>().join(","))
}

pub fn failed_line(reason: &str) -> String {
    format!("{{\"state\":\"failed\",\"reason\":{}}}", crate::proto::quote(reason))
}

#[cfg(not(feature = "audio"))]
pub fn run(_args: &[String]) -> Result<(), String> {
    let why = "weathersynth was built without the audio feature";
    eprintln!("{why}");
    println!("{}", failed_line(why));
    std::process::exit(2);
}

#[cfg(feature = "audio")]
pub fn run(args: &[String]) -> Result<(), String> {
    use crate::thunderlive::{self, Bus};
    use std::io::{BufRead, Write};
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    let get = |k: &str| args.iter().position(|a| a == k).and_then(|i| args.get(i + 1)).cloned();
    // the thunder: by the message's set (generated: none here, the game plays the method B files; recorded: the grain
    // bank), or one renderer for both with --thunder (tests)
    let choice = get("--thunder");
    let seed: u64 = get("--seed").and_then(|s| s.parse().ok()).unwrap_or(1);
    // the server's lines are read from the start, before the device opens (a slow open never fills the pipe: the
    // server's writer would wait on it)
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    std::thread::Builder::new()
        .name("stdin".into())
        .spawn(move || {
            let stdin = std::io::stdin();
            let mut lock = stdin.lock();
            let mut line = String::new();
            loop {
                line.clear();
                match lock.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        if line.len() <= crate::proto::MAX_LINE && tx.send(line.clone()).is_err() {
                            break;
                        }
                    }
                }
            }
        })
        .map_err(|e| e.to_string())?;

    let asked: Option<u32> = get("--buffer").and_then(|s| s.parse().ok());
    let load = load_per_cpu();
    let mut buffer: u32 = asked.unwrap_or_else(|| start_buffer(crate::audio::DEFAULT_BUFFER_FRAMES, load));
    let first_buffer = asked.unwrap_or(crate::audio::DEFAULT_BUFFER_FRAMES);
    let mut last_trouble = Instant::now();
    let fail = |why: String| -> ! {
        eprintln!("{why}");
        println!("{}", failed_line(&why));
        let _ = std::io::stdout().flush();
        std::process::exit(2);
    };
    // (the grain bank by default; without its file the physical model, said on stderr: thunder still plays)
    let (renderer, sets): (Box<dyn thunderlive::ThunderRenderer>, Vec<&str>) = match &choice {
        Some(c) => (thunderlive::renderer(c).unwrap_or_else(|e| fail(e)), vec!["generated", "recorded"]),
        None => {
            let (recorded, mut sets): (Box<dyn thunderlive::ThunderRenderer>, Vec<&str>) =
                match thunderlive::renderer("grains") {
                    Ok(r) => (r, vec!["recorded"]),
                    Err(e) => {
                        // (no bank file: the recorded set is not offered; the mod then plays its rec files)
                        eprintln!("live-serve: {e}; the recorded thunder stays with the game's files");
                        (Box::new(thunderlive::NoThunder), vec![])
                    }
                };
            sets.sort();
            // the generated set live: method B's strikes and closes rendered ahead (the mod plays the files for
            // the rest, and for any strike not ready in time: readyThunders)
            sets.push("generated");
            sets.sort();
            (Box::new(thunderlive::BySet { generated: Box::new(thunderlive::MethodB), recorded }), sets)
        }
    };
    let rain_sets = if crate::texbuild::default_bank().is_some() { r#"["generated","recorded"]"# } else { r#"["generated"]"# };
    let sets_json = format!("[{}]", sets.iter().map(|v| crate::proto::quote(v)).collect::<Vec<_>>().join(","));
    let rname = renderer.name();
    let bus = Arc::new(Bus::new());
    let (silent, _) = mix_for(None, true);
    // the first open tried again after OPEN_RETRY_S (a device busy at the game's own start)
    let mut tries = 0;
    let mut out = loop {
        match crate::audio::open(seed, crate::audio::Mix { rain: silent, thunder: 0.0, rain_rec: false }, bus.clone(), buffer) {
            Ok(o) => break o,
            Err(e) if tries < OPEN_RETRY_S.len() => {
                eprintln!("live-serve: audio device: {e}; trying again in {} s", OPEN_RETRY_S[tries]);
                std::thread::sleep(Duration::from_secs_f64(OPEN_RETRY_S[tries]));
                tries += 1;
            }
            Err(e) => fail(format!("audio device: {e}")),
        }
    };
    let started_at = Instant::now();
    eprintln!(
        "live-serve: device {:?} {} Hz x{} {} buffer={:?} (load {:.2} per cpu) thunder={rname}",
        out.device_name, out.sample_rate, out.channels, out.format, out.fixed_frames, load.unwrap_or(-1.0)
    );
    let b2 = bus.clone();
    let render = std::thread::Builder::new()
        .name("thunder-render".into())
        .spawn(move || thunderlive::render_loop(b2, renderer, thunderlive::utc_now))
        .map_err(|e| e.to_string())?;

    let mut last: Option<Msg> = None;
    let mut last_at = Instant::now();
    let mut state_at = Instant::now() - Duration::from_secs(1);
    let mut bad_lines = 0u64;
    // underruns counted from 1 s in (the device's first callbacks prime its own buffer, larger than what is queued)
    let started = Instant::now();
    let mut base_underruns: Option<u64> = None;
    let mut reopens = 0u32;
    let mut reopen_at: Vec<Instant> = Vec::new();
    let mut trouble_run = 0u32; // troubles since the last quiet minute
    let mut xrun_at: Vec<Instant> = Vec::new();
    let mut xruns_seen = 0u64;
    let mut xruns_total = 0u64;
    let mut underruns_before = 0u64; // (from streams before a reopen)
    let stdout = std::io::stdout();
    loop {
        match rx.recv_timeout(Duration::from_millis(50)) {
            Ok(line) => match crate::proto::msg(line.trim_end()) {
                Ok(m) => {
                    for th in &m.thunders {
                        bus.offer(th);
                    }
                    last = Some(m);
                    last_at = Instant::now();
                }
                Err(e) => {
                    bad_lines += 1;
                    if bad_lines <= 5 {
                        eprintln!("live-serve: a bad line: {e}");
                    }
                }
            },
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break, // EOF: the server stopped us
        }
        let stale = last_at.elapsed().as_secs_f64() > HEARTBEAT_S;
        let (rain, thunder) = mix_for(last.as_ref(), stale);
        if let Ok(mut mx) = out.mix.lock() {
            *mx = crate::audio::Mix { rain, thunder, rain_rec: last.as_ref().map(|m| m.rain_rec).unwrap_or(false) };
        }
        let x = out.xruns.load(std::sync::atomic::Ordering::Relaxed);
        for _ in xruns_seen..x {
            xrun_at.push(Instant::now());
        }
        xruns_total += x - xruns_seen;
        xruns_seen = x;
        xrun_at.retain(|t: &Instant| t.elapsed().as_secs_f64() < XRUN_WINDOW_S);
        let stream_error = out.error.load(std::sync::atomic::Ordering::Relaxed);
        let xrun_trouble = xrun_at.len() >= XRUN_GROW && grown_buffer(buffer).is_some();
        let calm = !stream_error && xrun_at.is_empty() && last_trouble.elapsed().as_secs_f64() >= SHRINK_AFTER_S;
        let shrink = if calm { shrunk_buffer(buffer, first_buffer) } else { None };
        if stream_error || xrun_trouble || shrink.is_some() {
            // trouble: a stream error (ALSA POLLERR after an xrun the sound server did not recover, a device switched
            // or unplugged) or repeated xruns (a starved audio thread): the default device opened again with a buffer
            // twice as large (up to BUFFER_MAX: more latency, no gaps); at the largest, at most REOPEN_MAX reopens a
            // minute, then failed (the mod's files take over)
            let next = if shrink.is_some() { shrink } else { grown_buffer(buffer) };
            last_trouble = Instant::now();
            if next.is_none() && started_at.elapsed().as_secs_f64() >= GRACE_S {
                // (failures in the first GRACE_S do not count: a start while the game loads keeps going)
                reopen_at.retain(|t: &Instant| t.elapsed().as_secs_f64() < REOPEN_WINDOW_S);
                if reopen_at.len() >= REOPEN_MAX {
                    fail("the audio device keeps failing (unplugged or changed)".into());
                }
                reopen_at.push(Instant::now());
            }
            if shrink.is_none() {
                trouble_run += 1;
            } else {
                trouble_run = 0;
            }
            buffer = next.unwrap_or(buffer);
            reopens += 1;
            xrun_at.clear();
            xruns_seen = 0;
            eprintln!(
                "live-serve: {}; opening the default device again with a {buffer}-frame buffer ({reopens})",
                if stream_error {
                    "the audio stream failed"
                } else if shrink.is_some() {
                    "quiet for 5 minutes"
                } else {
                    "repeated xruns"
                }
            );
            let u = out.ring.underruns.load(std::sync::atomic::Ordering::Relaxed);
            underruns_before += u - base_underruns.unwrap_or(u);
            drop(out);
            // (a little longer after each trouble in a row: a loaded machine gets time to settle)
            std::thread::sleep(Duration::from_millis(200 * trouble_run.clamp(1, 5) as u64));
            out = crate::audio::open(seed, crate::audio::Mix { rain, thunder, rain_rec: last.as_ref().map(|m| m.rain_rec).unwrap_or(false) }, bus.clone(), buffer)
                .unwrap_or_else(|e| fail(format!("audio device (reopening): {e}")));
            base_underruns = None;
        }
        if state_at.elapsed().as_secs_f64() >= STATE_S {
            state_at = Instant::now();
            let u = out.ring.underruns.load(std::sync::atomic::Ordering::Relaxed);
            if base_underruns.is_none() && started.elapsed().as_secs_f64() >= 1.0 {
                base_underruns = Some(u);
            }

            let ld = |a: &std::sync::atomic::AtomicU32| a.load(std::sync::atomic::Ordering::Relaxed);
            let buf = (ld(&out.callback_frames), ld(&out.target_frames), out.fixed_frames.is_some());
            let line = state_line(out.latency_ms(), underruns_before + u - base_underruns.unwrap_or(u), &rname, bus.counts(), buf);
            let (rl, rm) = bus.render_ms();
            let ready = ready_json(&bus.ready());
            let line = format!(
                "{},\"reopens\":{reopens},\"xruns\":{xruns_total},\"bufferAsked\":{buffer},\"renderMsLast\":{rl:.0},\"renderMsMax\":{rm:.0},\"limited\":{},\"thunderSets\":{sets_json},\"rainSets\":{rain_sets},\"readyThunders\":{ready}}}",
                &line[..line.len() - 1],
                out.limited.load(std::sync::atomic::Ordering::Relaxed)
            );
            let mut lock = stdout.lock();
            if writeln!(lock, "{line}").and_then(|_| lock.flush()).is_err() {
                break; // (the server is gone)
            }
        }
    }
    // fade out (LiveRain and the thunder gain follow the silent mix), then stop
    if let Ok(mut mx) = out.mix.lock() {
        *mx = crate::audio::Mix { rain: silent, thunder: 0.0, rain_rec: false };
    }
    std::thread::sleep(Duration::from_millis(400));
    bus.stop();
    let _ = render.join();
    drop(out);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(line: &str) -> Msg {
        crate::proto::msg(line).unwrap()
    }

    #[test]
    fn mix_follows_the_message() {
        let (r, t) = mix_for(Some(&m(r#"{"mode":"live","rainMmH":6,"shelter":0.8,"cabin":true,"volume":0.5,"thunderVolume":0.4,"duck":0.5,"rainVol":1}"#)), false);
        assert!(!r.muted && r.rain_mm_h == 6.0 && r.cabin == 1.0 && r.volume == 0.25 && r.shelter == 0.8);
        assert_eq!(t, 0.4);
        let (_, t) = mix_for(Some(&m(r#"{"mode":"live","thunderVolume":1,"shelter":0.2}"#)), false);
        assert_eq!(t, MUFFLED, "under a deck: muffled");
        let (r, t) = mix_for(Some(&m(r#"{"mode":"live","tunnel":true,"thunderVolume":1}"#)), false);
        assert!(r.muted && t == MUFFLED, "a tunnel: no rain, the thunder muffled");
    }

    #[test]
    fn quiet_when_muted_stale_prerecorded_or_nothing() {
        for (line, stale) in [
            (r#"{"mode":"live","muted":true,"thunderVolume":1}"#, false),
            (r#"{"mode":"live","thunderVolume":1}"#, true),
            (r#"{"mode":"prerecorded","thunderVolume":1}"#, false),
        ] {
            let (r, t) = mix_for(Some(&m(line)), stale);
            assert!(r.muted && t == 0.0, "{line} stale={stale}");
        }
        let (r, t) = mix_for(None, false);
        assert!(r.muted && t == 0.0);
    }

    #[test]
    fn a_busy_start_asks_for_more() {
        assert_eq!(start_buffer(512, None), 512);
        assert_eq!(start_buffer(512, Some(0.5)), 512);
        assert_eq!(start_buffer(512, Some(1.3)), 1024);
        assert_eq!(start_buffer(2048, Some(3.0)), 2048);
    }

    #[test]
    fn buffer_steps_back_down_to_the_first() {
        assert_eq!(shrunk_buffer(2048, 512), Some(1024));
        assert_eq!(shrunk_buffer(768, 512), Some(512));
        assert_eq!(shrunk_buffer(512, 512), None);
    }

    #[test]
    fn buffer_grows_to_the_cap() {
        assert_eq!(grown_buffer(256), Some(512));
        assert_eq!(grown_buffer(1024), Some(2048));
        assert_eq!(grown_buffer(1500), Some(2048));
        assert_eq!(grown_buffer(2048), None);
    }

    #[test]
    fn state_lines_parse() {
        let l = state_line(23.456, 2, "procedural", (5, 1, 0, 1), (256, 768, true));
        let j = crate::proto::parse(&l).unwrap();
        assert_eq!(j.str("state"), Some("live"));
        assert_eq!(j.num("latencyMs"), Some(23.5));
        assert_eq!(j.str("renderer"), Some("procedural"));
        let f = crate::proto::parse(&failed_line("no \"device\"")).unwrap();
        assert_eq!(f.str("reason"), Some("no \"device\""));
    }
}
