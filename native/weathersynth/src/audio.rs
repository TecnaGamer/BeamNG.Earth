//! Audio output for the live weather sound (feature `audio`): cpal, output streams ONLY.
//!
//! Rules (the crate review's conditions, 2026-11-25):
//! - Never build an input stream. On WASAPI an input stream opened on an output device turns on loopback capture of
//!   the system's audio. This module calls only `build_output_stream` (the test below fails if the word for the input
//!   builder appears in this file at all).
//! - On Windows cpal's callback thread runs at THREAD_PRIORITY_TIME_CRITICAL. Fine for audio, but no synthesis there:
//!   a worker thread renders into a lock-free single-producer / single-consumer ring, and the callback only copies out
//!   of it (zeros on an underrun, never a wait).
//! - No WinRT class is ever activated (windows-core's registration-free WinRT fallback would search for
//!   `<Namespace>.dll`): cpal's WASAPI backend uses Win32 COM (CoCreateInstance) only, and this module uses nothing
//!   else.

use crate::live::{Controls, LiveRain, FS as ENGINE_FS};
use crate::thunderlive::{utc_now, Bus};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

/// A single-producer / single-consumer ring of f32 samples (one interleaved frame per `channels` samples).
pub struct Ring {
    buf: Vec<std::cell::UnsafeCell<f32>>,
    head: AtomicUsize, // next write (producer)
    tail: AtomicUsize, // next read (consumer)
    pub underruns: AtomicU64,
}

// SAFETY: one producer writes only slots between tail and head's free space, one consumer reads only filled slots;
// head / tail are published with Release / Acquire, so a slot is never read and written at the same time.
unsafe impl Sync for Ring {}

impl Ring {
    pub fn new(cap: usize) -> Ring {
        Ring {
            buf: (0..cap).map(|_| std::cell::UnsafeCell::new(0.0)).collect(),
            head: AtomicUsize::new(0),
            tail: AtomicUsize::new(0),
            underruns: AtomicU64::new(0),
        }
    }
    fn cap(&self) -> usize {
        self.buf.len()
    }
    pub fn filled(&self) -> usize {
        let h = self.head.load(Ordering::Acquire);
        let t = self.tail.load(Ordering::Acquire);
        (h + self.cap() - t) % self.cap()
    }
    /// Producer: push as many samples as fit; returns how many were written.
    pub fn push(&self, data: &[f32]) -> usize {
        let h = self.head.load(Ordering::Relaxed);
        let t = self.tail.load(Ordering::Acquire);
        let free = (t + self.cap() - h - 1) % self.cap();
        let n = data.len().min(free);
        for (i, &v) in data.iter().take(n).enumerate() {
            // SAFETY: slot (h + i) is free (between head and tail - 1): the consumer does not read it
            unsafe { *self.buf[(h + i) % self.cap()].get() = v };
        }
        self.head.store((h + n) % self.cap(), Ordering::Release);
        n
    }
    /// Consumer: fill `out` from the ring, zeros for what is missing (counted as an underrun).
    pub fn pop_into(&self, out: &mut [f32]) {
        let t = self.tail.load(Ordering::Relaxed);
        let h = self.head.load(Ordering::Acquire);
        let avail = (h + self.cap() - t) % self.cap();
        let n = out.len().min(avail);
        for (i, o) in out.iter_mut().take(n).enumerate() {
            // SAFETY: slot (t + i) was published by the producer (before head) and is not written until tail passes
            *o = unsafe { *self.buf[(t + i) % self.cap()].get() };
        }
        for o in out.iter_mut().skip(n) {
            *o = 0.0;
        }
        if n < out.len() {
            self.underruns.fetch_add(1, Ordering::Relaxed);
        }
        self.tail.store((t + n) % self.cap(), Ordering::Release);
    }
}

/// What the worker plays from, set by the caller: the rain's controls and the thunders' master gain (the game's
/// master x UI volume, the muffle, 0 when quiet), both followed smoothly.
#[derive(Clone, Copy, Debug)]
pub struct Mix {
    pub rain: Controls,
    pub thunder: f64,
    /// the rain from the recorded texture bank (livetex.rs) instead of the procedural engine, when the bank loaded
    pub rain_rec: bool,
    /// the mod's rain volume for its layers (proto rainVol; None: RAIN_BED_GAIN x the share)
    pub rain_vol: Option<f64>,
    /// the recorded rain's roof outside near the car, and the shelter's kind (proto roofNear, enclosed)
    pub roof_near: f64,
    pub enclosed: bool,
}

/// The ring's fill target: fill_target's (about one callback and a margin), more after each underrun (from GROW_AFTER_S in), at
/// most MAX_QUEUE_S. The latency is the fill plus the device's own buffering.
/// (2026-11-26: 512 frames of margin, ~12 ms, was all a starved worker had on a PC busy rebuilding the world; 2048,
/// ~46 ms, rides out a scheduler stall that long. Nothing live needs less latency: the thunders are scheduled on the
/// UTC clock ahead of time and the latency is taken off; a strike's crack, known only at its flash, lands up to the
/// latency late, ~0.1 s, which reads as at once.)
pub const MARGIN_FRAMES: u64 = 2048;
pub const GROW_AFTER_S: f64 = 1.0;
pub const MAX_QUEUE_S: f64 = 0.25;
/// the device buffer asked for (frames) when the caller gives none: cpal BufferSize::Fixed, clamped to the device's range
/// (Linux 1024 since 2026-11-26: at 256 the Deck's ALSA thread at nice 19 under a game's load took xruns, and 512 was
/// the first size on the PC that started at 512 and had to restart at 1024 while busy; WASAPI shared mode ignores the
/// request and calls back with its own ~22 ms buffer anyway)
pub const DEFAULT_BUFFER_FRAMES: u32 = if cfg!(target_os = "linux") { 1024 } else { 256 };

pub struct Output {
    _stream: cpal::Stream,
    pub ring: Arc<Ring>,
    pub sample_rate: u32,
    pub channels: u16,
    pub device_name: String,
    pub format: String,
    /// the buffer asked for: Some(frames) when the device took BufferSize::Fixed, None when it runs its default
    pub fixed_frames: Option<u32>,
    stop: Arc<AtomicBool>,
    worker: Option<std::thread::JoinHandle<()>>,
    pub mix: Arc<Mutex<Mix>>,
    /// the device's own latency (callback to playback, µs), as the callback last saw it
    pub device_latency_us: Arc<AtomicU32>,
    /// the largest callback seen (frames)
    pub callback_frames: Arc<AtomicU32>,
    /// the worker's fill target now (frames)
    pub target_frames: Arc<AtomicU32>,
    /// times the output bus's limiter engaged
    pub limited: Arc<AtomicU64>,
    /// set by the stream's error callback for anything but an xrun (POLLERR, a device lost): the caller reopens
    pub error: Arc<AtomicBool>,
    /// the device's own xruns (cpal BufferUnderrun: recovered by cpal, an audible gap)
    pub xruns: Arc<AtomicU64>,
    /// the priorities the worker and the callback thread got (prio.rs; NOT_RAISED: none)
    pub worker_prio: Arc<AtomicI32>,
    pub callback_prio: Arc<AtomicI32>,
}

impl Output {
    /// The output latency now: what the ring holds plus the device's own (ms).
    pub fn latency_ms(&self) -> f64 {
        let ring = self.ring.filled() as f64 / (self.sample_rate as f64 * self.channels as f64);
        ring * 1000.0 + self.device_latency_us.load(Ordering::Relaxed) as f64 / 1000.0
    }
}

impl Drop for Output {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// The callback's shared state (all lock-free).
#[derive(Clone)]
struct Cb {
    ring: Arc<Ring>,
    lat: Arc<AtomicU32>,
    frames: Arc<AtomicU32>,
    error: Arc<AtomicBool>,
    xruns: Arc<AtomicU64>,
    channels: usize,
    /// the callback thread's priority (raised once, on its first call: Linux; cpal's WASAPI thread is TIME_CRITICAL)
    prio: Arc<AtomicI32>,
    prio_tried: Arc<AtomicBool>,
}

impl Cb {
    fn note(&self, info: &cpal::OutputCallbackInfo, samples: usize) {
        if cfg!(target_os = "linux") && !self.prio_tried.swap(true, Ordering::Relaxed) {
            // (one syscall, no allocation: prio.rs)
            self.prio.store(crate::prio::raise_current_thread(), Ordering::Relaxed);
        }
        let ts = info.timestamp();
        if let Some(d) = ts.playback.duration_since(&ts.callback) {
            self.lat.store(d.as_micros().min(u32::MAX as u128) as u32, Ordering::Relaxed);
        }
        let f = (samples / self.channels.max(1)) as u32;
        if f > self.frames.load(Ordering::Relaxed) {
            self.frames.store(f, Ordering::Relaxed);
        }
    }
}

/// Build the output stream for the device's sample format (f32 as is; i16 / u16 converted through a scratch made
/// here, before the stream starts). Output streams only.
fn build(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    fmt: cpal::SampleFormat,
    cb: &Cb,
) -> Result<cpal::Stream, String> {
    let err_fn = {
        let (e2, x2) = (cb.error.clone(), cb.xruns.clone());
        move |err: cpal::StreamError| match err {
            cpal::StreamError::BufferUnderrun => {
                x2.fetch_add(1, Ordering::Relaxed);
            }
            other => {
                // (once per stream: cpal's ALSA thread repeats a POLLERR on every poll until the stream is dropped)
                if !e2.swap(true, Ordering::Relaxed) {
                    eprintln!("audio stream error: {other}");
                }
            }
        }
    };
    let scratch_len = cb.ring.cap();
    let r = match fmt {
        cpal::SampleFormat::F32 => {
            let c = cb.clone();
            device.build_output_stream(
                config,
                move |data: &mut [f32], info: &cpal::OutputCallbackInfo| {
                    c.note(info, data.len());
                    c.ring.pop_into(data)
                },
                err_fn,
                None,
            )
        }
        cpal::SampleFormat::I16 => {
            let c = cb.clone();
            let mut tmp = vec![0.0f32; scratch_len];
            device.build_output_stream(
                config,
                move |data: &mut [i16], info: &cpal::OutputCallbackInfo| {
                    c.note(info, data.len());
                    copy_converted(&c.ring, &mut tmp, data, |v| (v.clamp(-1.0, 1.0) * 32767.0) as i16, 0)
                },
                err_fn,
                None,
            )
        }
        cpal::SampleFormat::U16 => {
            let c = cb.clone();
            let mut tmp = vec![0.0f32; scratch_len];
            device.build_output_stream(
                config,
                move |data: &mut [u16], info: &cpal::OutputCallbackInfo| {
                    c.note(info, data.len());
                    copy_converted(&c.ring, &mut tmp, data, |v| ((v.clamp(-1.0, 1.0) + 1.0) * 32767.5) as u16, 32768)
                },
                err_fn,
                None,
            )
        }
        other => return Err(format!("device sample format {other:?} is not handled (f32, i16, u16 are)")),
    };
    r.map_err(|e| format!("build_output_stream: {e}"))
}

/// Open the default output device (shared mode) with a small fixed buffer (buffer_frames, 0: DEFAULT_BUFFER_FRAMES,
/// clamped to the device's range; the device's default if it refuses) and start the worker: the live rain plus the
/// bus's thunders. The callback copies only.
pub fn open(seed: u64, initial: Mix, bus: Arc<Bus>, buffer_frames: u32) -> Result<Output, String> {
    let host = cpal::default_host();
    let device = host.default_output_device().ok_or("no default output device")?;
    let device_name = device.description().map(|d| d.name().to_string()).unwrap_or_else(|_| "?".into());
    let cfg = device.default_output_config().map_err(|e| format!("default output config: {e}"))?;
    let sample_rate = cfg.sample_rate();
    let channels = cfg.channels();
    let fmt = cfg.sample_format();
    let want = if buffer_frames == 0 { DEFAULT_BUFFER_FRAMES } else { buffer_frames };
    let fixed = match *cfg.buffer_size() {
        cpal::SupportedBufferSize::Range { min, max } => want.clamp(min.max(1), max.max(min.max(1))),
        cpal::SupportedBufferSize::Unknown => want,
    };
    let mut config: cpal::StreamConfig = cfg.into();
    // the ring can hold MAX_QUEUE_S and more; only its fill target (small) sets the latency
    let ring = Arc::new(Ring::new(((sample_rate as f64 * MAX_QUEUE_S * 2.0) as usize).max(4096) * channels as usize));
    let cb = Cb {
        ring: ring.clone(),
        lat: Arc::new(AtomicU32::new(0)),
        frames: Arc::new(AtomicU32::new(0)),
        error: Arc::new(AtomicBool::new(false)),
        xruns: Arc::new(AtomicU64::new(0)),
        channels: channels as usize,
        prio: Arc::new(AtomicI32::new(crate::prio::NOT_RAISED)),
        prio_tried: Arc::new(AtomicBool::new(false)),
    };
    config.buffer_size = cpal::BufferSize::Fixed(fixed);
    let (stream, fixed_frames) = match build(&device, &config, fmt, &cb) {
        Ok(s) => (s, Some(fixed)),
        Err(e) => {
            eprintln!("audio: a {fixed}-frame buffer was refused ({e}); the device's default buffer instead");
            config.buffer_size = cpal::BufferSize::Default;
            (build(&device, &config, fmt, &cb)?, None)
        }
    };
    let stop = Arc::new(AtomicBool::new(false));
    let mix = Arc::new(Mutex::new(initial));
    let first_target = fill_target(fixed, 0, (sample_rate as f64 * MAX_QUEUE_S) as u32);
    let target = Arc::new(AtomicU32::new(first_target));
    let limited = Arc::new(AtomicU64::new(0));
    let worker_prio = Arc::new(AtomicI32::new(crate::prio::NOT_RAISED));
    let w = Worker {
        seed,
        ring: ring.clone(),
        mix: mix.clone(),
        bus,
        dev_lat: cb.lat.clone(),
        cb_frames: cb.frames.clone(),
        target: target.clone(),
        stop: stop.clone(),
        sr: sample_rate,
        ch: channels,
        guess_frames: fixed,
        limited: limited.clone(),
        prio: worker_prio.clone(),
    };
    let worker = std::thread::Builder::new().name("weather-synth".into()).spawn(move || w.run()).map_err(|e| e.to_string())?;
    // the ring filled to its first target before the device starts pulling, at most 0.5 s
    let t0 = std::time::Instant::now();
    while ring.filled() < (first_target as usize * channels as usize).min(ring.cap() / 2)
        && t0.elapsed().as_secs_f64() < 0.5
    {
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    if let Err(e) = stream.play() {
        stop.store(true, Ordering::Relaxed);
        let _ = worker.join();
        return Err(format!("play: {e}"));
    }
    Ok(Output {
        _stream: stream,
        ring,
        sample_rate,
        channels,
        device_name,
        format: format!("{fmt:?}"),
        fixed_frames,
        stop,
        worker: Some(worker),
        mix,
        device_latency_us: cb.lat,
        callback_frames: cb.frames,
        target_frames: target,
        limited,
        error: cb.error,
        xruns: cb.xruns,
        worker_prio,
        callback_prio: cb.prio,
    })
}

/// (callback) the ring's samples into an integer device buffer: one pass through the scratch made beforehand; a
/// callback larger than the scratch (never seen) gets silence for the rest rather than an allocation.
fn copy_converted<T: Copy>(ring: &Ring, tmp: &mut [f32], data: &mut [T], conv: impl Fn(f32) -> T, silence: T) {
    let n = data.len().min(tmp.len());
    ring.pop_into(&mut tmp[..n]);
    for (d, v) in data[..n].iter_mut().zip(&tmp[..n]) {
        *d = conv(*v);
    }
    for d in data[n..].iter_mut() {
        *d = silence;
    }
}

/// Pure: the fill target (frames) for the callback size, the underruns counted since the start of growth and the cap.
/// One callback's worth (it can ask for all of it at once) plus a margin of a callback or MARGIN_FRAMES, whichever is
/// more (ALSA at 256 frames: 768, as 3 callbacks; WASAPI shared mode, which calls back with up to its whole ~22 ms
/// buffer whatever is asked: 2 callbacks; 1.5 had 2 underruns in 10 minutes), and half a callback more (at least 128
/// frames) per underrun.
pub fn fill_target(callback_frames: u32, grown: u64, max_frames: u32) -> u32 {
    let cb = callback_frames.max(32) as u64;
    let t = cb + cb.max(MARGIN_FRAMES) + grown * (cb / 2).max(128);
    t.min(max_frames.max(cb as u32) as u64) as u32
}

struct Worker {
    seed: u64,
    ring: Arc<Ring>,
    mix: Arc<Mutex<Mix>>,
    bus: Arc<Bus>,
    dev_lat: Arc<AtomicU32>,
    cb_frames: Arc<AtomicU32>,
    target: Arc<AtomicU32>,
    stop: Arc<AtomicBool>,
    sr: u32,
    ch: u16,
    guess_frames: u32,
    limited: Arc<AtomicU64>,
    prio: Arc<AtomicI32>,
}

impl Worker {
    /// Renders 512-sample blocks at the engine rate: the rain (under the bus's scheduled strike ducks), then the thunders
    /// due by the time the block will be heard, then the output bus (outbus.rs: the 2 ms look-ahead limiter at
    /// -1.5 dBTP, the ADAA soft clip, the clamp), then the windowed-sinc resampler to the device rate; duplicates to
    /// every channel and keeps the ring at its target in callback-sized pieces, sleeping about half a callback between.
    fn run(self) {
        use crate::outbus::{Limiter, Resampler, SoftClip};
        // above the game's threads where the system allows it (prio.rs): a starved worker is an underrun
        self.prio.store(crate::prio::raise_current_thread(), Ordering::Relaxed);
        let (sr, ch) = (self.sr, self.ch as usize);
        let mut lr = LiveRain::new(self.seed);
        // the recorded rain (texture bank next to the program; none: the procedural rain plays whatever is asked)
        let mut tex = crate::texbuild::default_bank()
            .and_then(|p| crate::texbuild::Bank::load(&p).ok())
            .map(|b| crate::livetex::LiveTex::new(Arc::new(b), self.seed));
        let mut src = vec![0.0f32; 512];
        let mut blk = vec![0.0f64; 512];
        let mut lim = Limiter::new(ENGINE_FS);
        let mut clip = SoftClip::new();
        let mut rs = Resampler::new(ENGINE_FS, sr as f64);
        let mut frame: Vec<f32> = Vec::with_capacity(8192);
        let mut thunder_gain = 0.0f32;
        let mut duck_prev = 1.0f64;
        let started = std::time::Instant::now();
        let mut base_underruns: Option<u64> = None;
        let max_frames = (sr as f64 * MAX_QUEUE_S) as u32;
        let block_s = src.len() as f64 / ENGINE_FS;
        while !self.stop.load(Ordering::Relaxed) {
            let cbf = match self.cb_frames.load(Ordering::Relaxed) {
                0 => self.guess_frames,
                f => f,
            };
            let u = self.ring.underruns.load(Ordering::Relaxed);
            let grown = match base_underruns {
                Some(b) => u - b,
                None => {
                    if started.elapsed().as_secs_f64() >= GROW_AFTER_S {
                        base_underruns = Some(u);
                    }
                    0
                }
            };
            let target = fill_target(cbf, grown, max_frames);
            self.target.store(target, Ordering::Relaxed);
            let filled_frames = self.ring.filled() / ch;
            if filled_frames >= target as usize {
                let half_cb_us = (cbf as f64 * 0.5e6 / sr as f64) as u64;
                std::thread::sleep(std::time::Duration::from_micros(half_cb_us.clamp(500, 4000)));
                continue;
            }
            let m = self.mix.lock().map(|m| *m).ok();
            if let Some(m) = m {
                let rec = m.rain_rec && tex.is_some();
                // the procedural rain (its own level cap, live.rs), silent while the recorded one plays
                let mut c = m.rain;
                c.muted |= rec;
                lr.set(c);
                if let Some(t) = tex.as_mut() {
                    let r = m.rain;
                    t.set(crate::livetex::Controls {
                        rain_mm_h: r.rain_mm_h,
                        // (wind and drips stay with the mod's files in both modes for now)
                        wind_ms: 0.0,
                        shelter: r.shelter,
                        cabin: r.cabin,
                        roof_near: m.roof_near,
                        enclosed: m.enclosed,
                        // the loops' loudness x the mod's rain volume (as the game plays its files)
                        rain_volume: r.volume
                            * m.rain_vol.unwrap_or_else(|| crate::live::RAIN_BED_GAIN * crate::live::rain_share(r.rain_mm_h)),
                        wind_volume: 0.0,
                        muted: r.muted || !rec,
                    });
                }
            }
            let want_thunder = m.map(|m| m.thunder as f32).unwrap_or(0.0);
            frame.clear();
            let frames = (target as usize - filled_frames).min(cbf as usize).max(32);
            while frame.len() < frames * ch {
                let v = match rs.pull() {
                    Some(v) => v as f32,
                    None => {
                        // one engine block: the UTC time it will be heard (now + what is queued ahead of it, the
                        // resampler's and the limiter's delay, the device's latency)
                        let queued = (self.ring.filled() + frame.len()) as f64 / (sr as f64 * ch as f64)
                            + rs.pending() / ENGINE_FS
                            + crate::outbus::LIM_LOOK_S;
                        let t_out = utc_now() + queued + self.dev_lat.load(Ordering::Relaxed) as f64 * 1e-6;
                        for v in src.iter_mut() {
                            *v = 0.0;
                        }
                        lr.process(&mut src);
                        if let Some(t) = tex.as_mut() {
                            t.process(&mut src);
                        }
                        // the rain under the strike ducks: the gain from the block's start to its end
                        let d1 = self.bus.duck_gain(t_out + block_s);
                        let n = src.len() as f32;
                        for (k, v) in src.iter_mut().enumerate() {
                            *v *= (duck_prev + (d1 - duck_prev) * (k as f64 / n as f64)) as f32;
                        }
                        duck_prev = d1;
                        self.bus.start_due(t_out);
                        thunder_gain += (want_thunder - thunder_gain) * 0.2;
                        self.bus.mix(&mut src, thunder_gain);
                        for (b, v) in blk.iter_mut().zip(&src) {
                            *b = clip.tick(lim.tick(*v as f64));
                        }
                        self.limited.store(lim.engaged, Ordering::Relaxed);
                        rs.push(&blk);
                        continue;
                    }
                };
                for _ in 0..ch {
                    frame.push(v);
                }
            }
            let mut off = 0;
            while off < frame.len() && !self.stop.load(Ordering::Relaxed) {
                off += self.ring.push(&frame[off..]);
                if off < frame.len() {
                    std::thread::sleep(std::time::Duration::from_micros(500));
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn output_streams_only() {
        // WASAPI: an input stream on an output device is loopback capture of the system's audio. Never.
        let src = include_str!("audio.rs");
        let forbidden = ["build_", "input_", "stream"].concat();
        assert!(!src.contains(&forbidden), "audio.rs must never build an input stream");
        let forbidden_raw = ["build_", "input_", "stream_raw"].concat();
        assert!(!src.contains(&forbidden_raw));
    }

    #[test]
    fn fill_target_grows_and_caps() {
        assert_eq!(super::fill_target(1024, 0, 11025), 3072, "ALSA 1024: the callback and the 2048-frame margin");
        assert_eq!(super::fill_target(256, 0, 11025), 2304, "a small callback: the margin still");
        assert_eq!(super::fill_target(1056, 0, 12000), 3104, "WASAPI's 1056");
        assert_eq!(super::fill_target(256, 2, 11025), 2560, "128 frames more per underrun");
        assert_eq!(super::fill_target(256, 100, 11025), 11025, "capped");
        assert_eq!(super::fill_target(0, 0, 11025), 2080, "an unknown callback: the floor plus the margin");
        assert_eq!(super::fill_target(1056, 1, 12000), 3632, "WASAPI: half a callback more per underrun");
        assert_eq!(super::fill_target(4096, 0, 11025), 8192, "a 4096-frame device: a whole callback of margin");
    }

    // the callback's path allocates nothing: a counting allocator (this test binary's), per thread
    struct Counting;
    thread_local! {
        static ALLOCS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
    }
    unsafe impl std::alloc::GlobalAlloc for Counting {
        unsafe fn alloc(&self, l: std::alloc::Layout) -> *mut u8 {
            let _ = ALLOCS.try_with(|c| c.set(c.get() + 1));
            std::alloc::System.alloc(l)
        }
        unsafe fn dealloc(&self, p: *mut u8, l: std::alloc::Layout) {
            std::alloc::System.dealloc(p, l)
        }
        unsafe fn realloc(&self, p: *mut u8, l: std::alloc::Layout, n: usize) -> *mut u8 {
            let _ = ALLOCS.try_with(|c| c.set(c.get() + 1));
            std::alloc::System.realloc(p, l, n)
        }
    }
    #[global_allocator]
    static COUNTING: Counting = Counting;

    #[test]
    fn the_callback_path_never_allocates() {
        let r = super::Ring::new(8192);
        let mut out = vec![0.0f32; 2048];
        let mut tmp = vec![0.0f32; 4096];
        let mut i16s = vec![0i16; 2048];
        r.push(&vec![0.25f32; 3000]);
        let before = ALLOCS.with(|c| c.get());
        for _ in 0..50 {
            r.pop_into(&mut out); // (an underrun on the way: zeros, a counter)
            super::copy_converted(&r, &mut tmp, &mut i16s, |v| (v.clamp(-1.0, 1.0) * 32767.0) as i16, 0);
            let _ = crate::prio::raise_current_thread();
        }
        assert_eq!(ALLOCS.with(|c| c.get()), before, "pop_into / copy_converted / the priority raise allocated");
        assert!(r.underruns.load(std::sync::atomic::Ordering::Relaxed) > 0);
    }

    #[test]
    fn ring_wraps_and_underruns() {
        let r = super::Ring::new(8);
        assert_eq!(r.push(&[1.0, 2.0, 3.0, 4.0, 5.0]), 5);
        let mut o = [0.0f32; 3];
        r.pop_into(&mut o);
        assert_eq!(o, [1.0, 2.0, 3.0]);
        assert_eq!(r.push(&[6.0, 7.0, 8.0, 9.0, 10.0]), 5);
        let mut o = [0.0f32; 8];
        r.pop_into(&mut o);
        assert_eq!(o, [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 0.0]);
        assert_eq!(r.underruns.load(std::sync::atomic::Ordering::Relaxed), 1);
    }
}
