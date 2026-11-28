//! Generated wind (the user, 2026-11-26, after hearing background noises in the recorded wind; the synthetic drips were
//! liked): wind built from its physics, no recording, so no background by construction. Seeded and deterministic
//! (rng.rs streams, libm), a seamless loop (everything periodic on the loop's length).
//!
//! - **The wind speed U(t):** the class's mean U0 with gusts: a sum of slow sinusoids (periods 6-40 s) and faster
//!   turbulence (1-4 s), each a whole number of cycles per loop (so it loops), scaled to the class's gust share and
//!   kept at or above 0.3 U0.
//! - **Rumble:** noise through a low-pass that opens with U (60 + 6 U Hz), its level ~ (U / U0)^2: the pressure on the
//!   ground and the microphone-free "weight" of wind.
//! - **Whoosh:** noise through a broad band-pass rising with U (100 + 25 U Hz, at most 900), level ~ (U / U0)^2.5.
//! - **Moving resonances:** two narrower band-passes (Q 5) whose centres follow U^0.7 (cavities, eaves, a gap in a
//!   fence howling), level ~ (U / U0)^3.
//! - **Aeolian whistles:** vortex shedding off wires and edges: f = St U / d with St = 0.2, for a few diameters d; a
//!   very narrow band-pass (Q 40) at that frequency, sounding only while the gust is above the class's onset (attack
//!   0.3 s, release 0.8 s), so whistles come and go with the gusts.
//! - **Foliage roar:** smooth noise 200 Hz to 0.8-1.6 kHz (opening with U, two low-passes), level ~ (U / U0)^2, strong
//!   wind's tree roar; continuous noise, so no patter.
//! - Then two low-passes at 2.5 kHz over the whole: wind is low-mid, never a hiss (the first render's 2-8 kHz share
//!   sat at rain's level).
//! - The filters run over the loop twice and the second pass is kept, so their state is settled across the wrap; the
//!   coefficients follow U every sample (stepping them every 32 samples whined at 1.4 kHz).

use crate::dsp::Biquad;
use crate::m;
use crate::rng::Rng;

pub const SR: f64 = 44100.0;

#[derive(Clone, Copy)]
pub struct WindClass {
    pub u0: f64,
    pub gust: f64,
    pub rumble: f64,
    pub whoosh: f64,
    pub howl: f64,
    pub whistle: f64,
    pub foliage: f64,
    /// the whistles' onset: U over this share of U0
    pub whistle_on: f64,
    /// the shedding diameters (m)
    pub diam: [f64; 3],
}

pub fn class(name: &str) -> Option<WindClass> {
    Some(match name {
        "light" => WindClass { u0: 3.0, gust: 0.35, rumble: 1.0, whoosh: 0.5, howl: 0.05, whistle: 0.03, foliage: 0.25, whistle_on: 1.25, diam: [0.003, 0.004, 0.006] },
        "moderate" => WindClass { u0: 8.0, gust: 0.4, rumble: 1.0, whoosh: 0.8, howl: 0.25, whistle: 0.08, foliage: 0.5, whistle_on: 1.15, diam: [0.004, 0.006, 0.010] },
        "strong" => WindClass { u0: 15.0, gust: 0.5, rumble: 1.0, whoosh: 1.0, howl: 0.45, whistle: 0.12, foliage: 1.0, whistle_on: 1.1, diam: [0.006, 0.010, 0.016] },
        _ => return None,
    })
}

/// The gusting wind speed over n samples, periodic on n.
fn wind_speed(rng: &mut Rng, n: usize, c: &WindClass) -> Vec<f64> {
    let secs = n as f64 / SR;
    let mut comps: Vec<(f64, f64, f64)> = Vec::new(); // (cycles per loop, amplitude, phase)
    for _ in 0..6 {
        let period = rng.range(6.0, 40.0);
        comps.push(((secs / period).round().max(1.0), 1.0, rng.range(0.0, m::TAU)));
    }
    for _ in 0..6 {
        let period = rng.range(1.0, 4.0);
        comps.push(((secs / period).round().max(1.0), 0.35, rng.range(0.0, m::TAU)));
    }
    let raw: Vec<f64> = (0..n)
        .map(|i| {
            let t = i as f64 / n as f64;
            comps.iter().map(|(k, a, ph)| a * m::sin(m::TAU * k * t + ph)).sum::<f64>()
        })
        .collect();
    let sd = m::sqrt(raw.iter().map(|v| v * v).sum::<f64>() / n as f64).max(1e-9);
    raw.iter().map(|v| (c.u0 * (1.0 + c.gust * v / sd)).max(0.3 * c.u0)).collect()
}

struct Layer {
    f: Biquad,
    g: Biquad,
    h: Biquad,
    noise: Vec<f64>,
}

/// One class's wind loop of n samples (not levelled).
pub fn render(seed: u64, n: usize, c: &WindClass) -> Vec<f64> {
    let mut rng = Rng::new(seed, "windsynth-u");
    let u = wind_speed(&mut rng, n, c);
    let noise = |name: &str| -> Vec<f64> {
        let mut r = Rng::new(seed, name);
        (0..n).map(|_| r.normal()).collect()
    };
    let q = std::f64::consts::FRAC_1_SQRT_2;
    let mk = |name: &str| Layer { f: Biquad::lowpass(SR, 100.0, q), g: Biquad::lowpass(SR, 100.0, q), h: Biquad::lowpass(SR, 100.0, q), noise: noise(name) };
    let mut rumble = mk("windsynth-rumble");
    let mut whoosh = mk("windsynth-whoosh");
    let mut howl = [mk("windsynth-howl-0"), mk("windsynth-howl-1")];
    let mut whis = [mk("windsynth-whistle-0"), mk("windsynth-whistle-1"), mk("windsynth-whistle-2")];
    let mut fol = mk("windsynth-foliage");
    let howl_base = [250.0, 520.0];
    // each whistle's gate (smoothed 0..1) and its shedding wobble
    let mut gate = [0.0f64; 3];
    let (att, rel) = (1.0 - m::exp(-1.0 / (0.3 * SR)), 1.0 - m::exp(-1.0 / (0.8 * SR)));
    let mut out = vec![0.0f64; n];
    for pass in 0..2 {
        for i in 0..n {
            let ui = u[i];
            let r = ui / c.u0;
            // (every sample: coefficients stepped every 32 samples left a 1.4 kHz zipper whine and its harmonics)
            {
                let fr = (60.0 + 6.0 * ui).min(400.0);
                rumble.f = with_state(Biquad::lowpass(SR, fr, q), &rumble.f);
                rumble.g = with_state(Biquad::lowpass(SR, fr, q), &rumble.g);
                let fw = (100.0 + 25.0 * ui).min(900.0);
                whoosh.f = with_state(Biquad::bandpass(SR, fw, 0.7), &whoosh.f);
                for (k, h) in howl.iter_mut().enumerate() {
                    h.f = with_state(Biquad::bandpass(SR, howl_base[k] * m::pow(r, 0.7), 5.0), &h.f);
                }
                for (k, w) in whis.iter_mut().enumerate() {
                    let f = (0.2 * ui / c.diam[k]).clamp(80.0, 6000.0);
                    w.f = with_state(Biquad::bandpass(SR, f, 40.0), &w.f);
                }
                let fl = (600.0 + 60.0 * ui).min(1600.0);
                fol.f = with_state(Biquad::highpass(SR, 200.0, q), &fol.f);
                fol.g = with_state(Biquad::lowpass(SR, fl, q), &fol.g);
                fol.h = with_state(Biquad::lowpass(SR, fl, q), &fol.h);
            }
            let mut y = 0.0;
            let rb = rumble.g.tick(rumble.f.tick(rumble.noise[i]));
            y += rb * c.rumble * r * r;
            y += whoosh.f.tick(whoosh.noise[i]) * c.whoosh * m::pow(r, 2.5) * 0.5;
            for h in howl.iter_mut() {
                y += h.f.tick(h.noise[i]) * c.howl * r * r * r;
            }
            for (k, w) in whis.iter_mut().enumerate() {
                let on = if r > c.whistle_on + 0.05 * k as f64 { 1.0 } else { 0.0 };
                gate[k] += (if on > gate[k] { att } else { rel }) * (on - gate[k]);
                // (a band-pass of Q 40 passes little power: the gain brings the whistle up to its share)
                y += w.f.tick(w.noise[i]) * c.whistle * gate[k] * r * r * 6.0;
            }
            let fo = fol.f.tick(fol.noise[i]);
            let fo = fol.g.tick(fo);
            y += fol.h.tick(fo) * c.foliage * r * r * 0.3;
            if pass == 1 {
                out[i] = y;
            }
        }
    }
    // the air takes the top off: two low-passes at TOP_HZ over the whole (wind is low-mid: no hiss)
    for _ in 0..2 {
        Biquad::lowpass(SR, TOP_HZ, q).run_circular(&mut out);
    }
    out
}
const TOP_HZ: f64 = 2500.0;

fn with_state(mut b: Biquad, old: &Biquad) -> Biquad {
    b.copy_state(old);
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_seamless_and_stronger_is_louder() {
        let n = (8.0 * SR) as usize;
        let a = render(3, n, &class("moderate").unwrap());
        assert_eq!(a, render(3, n, &class("moderate").unwrap()));
        // the wrap: no jump bigger than the loop's own largest sample-to-sample step
        let step = a.windows(2).map(|w| (w[1] - w[0]).abs()).fold(0.0, f64::max);
        assert!((a[0] - a[n - 1]).abs() <= step, "wrap {} > {}", (a[0] - a[n - 1]).abs(), step);
        let rms = |x: &[f64]| m::sqrt(x.iter().map(|v| v * v).sum::<f64>() / x.len() as f64);
        let (l, s) = (render(3, n, &class("light").unwrap()), render(3, n, &class("strong").unwrap()));
        assert!(rms(&s) > rms(&l));
    }
}
