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
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
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

pub struct Output {
    _stream: cpal::Stream,
    pub ring: Arc<Ring>,
    pub sample_rate: u32,
    pub channels: u16,
    pub device_name: String,
    stop: Arc<AtomicBool>,
    worker: Option<std::thread::JoinHandle<()>>,
    pub controls: Arc<Mutex<Controls>>,
}

impl Drop for Output {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// Open the default output device (shared mode) and start the live rain on a worker thread. The callback copies only.
pub fn open(seed: u64, initial: Controls) -> Result<Output, String> {
    let host = cpal::default_host();
    let device = host.default_output_device().ok_or("no default output device")?;
    let device_name = device.description().map(|d| d.name().to_string()).unwrap_or_else(|_| "?".into());
    let cfg = device.default_output_config().map_err(|e| format!("default output config: {e}"))?;
    let sample_rate = cfg.sample_rate();
    let channels = cfg.channels();
    if cfg.sample_format() != cpal::SampleFormat::F32 {
        return Err(format!("device format {:?} (only f32 is handled in this prototype)", cfg.sample_format()));
    }
    let config: cpal::StreamConfig = cfg.into();
    // ~200 ms of interleaved audio in the ring; the worker keeps it ~2/3 full
    let ring = Arc::new(Ring::new((sample_rate as usize * channels as usize) / 5));
    let r2 = ring.clone();
    let err_flag = Arc::new(AtomicBool::new(false));
    let e2 = err_flag.clone();
    let stream = device
        .build_output_stream(
            &config,
            move |data: &mut [f32], _info: &cpal::OutputCallbackInfo| r2.pop_into(data),
            move |err| {
                eprintln!("audio stream error: {err}");
                e2.store(true, Ordering::Relaxed);
            },
            None,
        )
        .map_err(|e| format!("build_output_stream: {e}"))?;
    stream.play().map_err(|e| format!("play: {e}"))?;
    let stop = Arc::new(AtomicBool::new(false));
    let controls = Arc::new(Mutex::new(initial));
    let (s2, c2, r3) = (stop.clone(), controls.clone(), ring.clone());
    let worker = std::thread::Builder::new()
        .name("weather-synth".into())
        .spawn(move || synth_worker(seed, r3, c2, s2, sample_rate, channels))
        .map_err(|e| e.to_string())?;
    Ok(Output { _stream: stream, ring, sample_rate, channels, device_name, stop, worker: Some(worker), controls })
}

/// The worker: renders 512-sample blocks at the engine rate, resamples linearly to the device rate (the grain bank
/// moves to the device rate later), duplicates to every channel, and keeps the ring topped up.
fn synth_worker(seed: u64, ring: Arc<Ring>, controls: Arc<Mutex<Controls>>, stop: Arc<AtomicBool>, sr: u32, ch: u16) {
    let mut lr = LiveRain::new(seed);
    let step = ENGINE_FS / sr as f64; // engine samples per device sample
    let mut src = vec![0.0f32; 512];
    let mut src_pos = 0.0f64; // position in the current block
    let mut prev = 0.0f32;
    let mut have = false;
    let mut frame = Vec::with_capacity(4096);
    let target = ring.cap() * 2 / 3;
    while !stop.load(Ordering::Relaxed) {
        if ring.filled() >= target {
            std::thread::sleep(std::time::Duration::from_millis(5));
            continue;
        }
        if let Ok(c) = controls.lock() {
            lr.set(*c);
        }
        frame.clear();
        // produce ~10 ms of device frames
        let frames = sr as usize / 100;
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
        while off < frame.len() && !stop.load(Ordering::Relaxed) {
            off += ring.push(&frame[off..]);
            if off < frame.len() {
                std::thread::sleep(std::time::Duration::from_millis(2));
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
