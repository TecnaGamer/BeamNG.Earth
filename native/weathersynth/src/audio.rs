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
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
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
}

/// The ring's fill target: fill_target's (about one callback and a margin), more after each underrun (from GROW_AFTER_S in), at
/// most MAX_QUEUE_S. The latency is the fill plus the device's own buffering.
pub const MARGIN_FRAMES: u64 = 512;
pub const GROW_AFTER_S: f64 = 1.0;
pub const MAX_QUEUE_S: f64 = 0.1;
/// the device buffer asked for (frames) when the caller gives none: cpal BufferSize::Fixed, clamped to the device's range
pub const DEFAULT_BUFFER_FRAMES: u32 = 256;

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
    /// set by the stream's error callback (a device lost: the caller reports 'failed')
    pub error: Arc<AtomicBool>,
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
    channels: usize,
}

impl Cb {
    fn note(&self, info: &cpal::OutputCallbackInfo, samples: usize) {
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
        let e2 = cb.error.clone();
        move |err: cpal::StreamError| {
            eprintln!("audio stream error: {err}");
            e2.store(true, Ordering::Relaxed);
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
        channels: channels as usize,
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
        error: cb.error,
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
/// One callback's worth (it can ask for all of it at once) plus a margin of half a callback or MARGIN_FRAMES, whichever
/// is more (ALSA at 256 frames: 768, as 3 callbacks; WASAPI shared mode, which calls back with its whole ~22 ms buffer
/// whatever is asked: 1.5 callbacks), and half a callback more (at least 128 frames) per underrun.
pub fn fill_target(callback_frames: u32, grown: u64, max_frames: u32) -> u32 {
    let cb = callback_frames.max(32) as u64;
    let t = cb + (cb / 2).max(MARGIN_FRAMES) + grown * (cb / 2).max(128);
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
}

impl Worker {
    /// Renders 512-sample blocks at the engine rate (the rain, then the thunders due by the time the block will be
    /// heard), resamples linearly to the device rate, duplicates to every channel, and keeps the ring at its target in
    /// callback-sized pieces, sleeping about half a callback between checks.
    fn run(self) {
        let (sr, ch) = (self.sr, self.ch as usize);
        let mut lr = LiveRain::new(self.seed);
        let step = ENGINE_FS / sr as f64; // engine samples per device sample
        let mut src = vec![0.0f32; 512];
        let mut src_pos = 0.0f64;
        let mut prev = 0.0f32;
        let mut have = false;
        let mut frame: Vec<f32> = Vec::with_capacity(8192);
        let mut thunder_gain = 0.0f32;
        let started = std::time::Instant::now();
        let mut base_underruns: Option<u64> = None;
        let max_frames = (sr as f64 * MAX_QUEUE_S) as u32;
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
                lr.set(m.rain);
            }
            let want_thunder = m.map(|m| m.thunder as f32).unwrap_or(0.0);
            frame.clear();
            let frames = (target as usize - filled_frames).min(cbf as usize).max(32);
            for _ in 0..frames {
                if !have || src_pos >= src.len() as f64 {
                    if have {
                        prev = src[src.len() - 1];
                        src_pos -= src.len() as f64;
                    }
                    for v in src.iter_mut() {
                        *v = 0.0;
                    }
                    lr.process(&mut src);
                    // the UTC time this block will be heard: now + what is queued ahead of it + the device's latency
                    let queued = (self.ring.filled() + frame.len()) as f64 / (sr as f64 * ch as f64);
                    let t_out = utc_now() + queued + self.dev_lat.load(Ordering::Relaxed) as f64 * 1e-6;
                    self.bus.start_due(t_out);
                    thunder_gain += (want_thunder - thunder_gain) * 0.2;
                    self.bus.mix(&mut src, thunder_gain);
                    have = true;
                }
                let i = src_pos.floor() as usize;
                let fr = (src_pos - i as f64) as f32;
                let a = if i == 0 { prev } else { src[i - 1] };
                let b = src[i];
                let v = a + (b - a) * fr;
                for _ in 0..ch {
                    frame.push(v);
                }
                src_pos += step;
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
        assert_eq!(super::fill_target(256, 0, 4410), 768, "ALSA 256: three callbacks");
        assert_eq!(super::fill_target(1056, 0, 4800), 1584, "WASAPI's 1056: one and a half");
        assert_eq!(super::fill_target(256, 2, 4410), 1024, "128 frames more per underrun");
        assert_eq!(super::fill_target(256, 100, 4410), 4410, "capped");
        assert_eq!(super::fill_target(0, 0, 4410), 544, "an unknown callback: the floor plus the margin");
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
