//! Thunder from a lightning channel (the quasilinear model of Ribner & Roy 1982 on Few's N-wave source, 1969/1970).
//!
//! 1. A random tortuous channel: a cloud-to-ground stroke from the ground point up into the cloud, built from macro
//!    segments (median 25 m, direction change ~16 degrees, Hill 1968) made of short meso steps (median 2.5 m, small
//!    jitter); downward branches; horizontal in-cloud channels; 1-5 return strokes down the same channel 20-200 ms apart.
//! 2. Every 0.5 m piece of channel radiates an N-wave (a +1 to -1 linear ramp between two shocks). Its length follows
//!    Few's relaxation radius R0: the dominant frequency 0.63 c / R0 at 1 km, shorter nearer and longer farther (weak
//!    shock lengthening, ~sqrt(ln r)); the amplitude falls as 1/r times the matching weak-shock factor.
//! 3. The pieces are summed with their propagation delay (r / c): a piece spreads its weight over the arrival times of
//!    its two ends, so a segment seen side-on arrives all at once (a clap) and one seen end-on is smeared (a rumble).
//!    A ground reflection (image observer, coefficient 0.85) arrives a few ms after each piece.
//! 4. Pieces are grouped by distance (1 % bins). Each bin is convolved, in the frequency domain, with its N-wave and its
//!    air absorption over that distance (ISO 9613-1, 22 C, 85 % humidity) as a minimum-phase filter, so the far bins
//!    keep only the low rumble and the near bins keep the crack.
//! 5. Echoes from the surroundings (hills, tree lines, buildings): diffuse clusters of reflections 0.1-7 s late, each
//!    low-passed by its extra path's absorption.
//! 6. 25 Hz high-pass, cut from the first arrival, fades, then gain into a true-peak limiter until the loudest 3 s hit
//!    the class's loudness.

use crate::dsp::{self, Biquad, C};
use crate::loudness;
use crate::m;
use crate::rng::Rng;
use std::collections::HashMap;

pub const FS: f64 = 44100.0;
const SOUND: f64 = 344.6; // m/s at 22 C
const OBS_H: f64 = 1.7;
const GROUND_R: f64 = 0.85;
const PIECE: f64 = 0.5; // m
const BIN_W: f64 = 0.01; // relative distance bin width

/// Empirical source tilt (dB per octave above TILT_F, flat again above 8 kHz). The ideal-N-wave sum is far darker than
/// recorded thunder (it fits Few's infrasound-capable spectra, peak 50-100 Hz); real shock fronts are spikier
/// (turbulence, the near-source shock shape) and every recording chain rolls off the lowest octaves. Calibrated so
/// close and strike renders sit in the recorded files' spectral balance. WEATHERSYNTH_TILT overrides it (testing).
const TILT_F: f64 = 150.0;
fn tilt_db_oct() -> f64 {
    std::env::var("WEATHERSYNTH_TILT").ok().and_then(|v| v.parse().ok()).unwrap_or(8.0)
}
fn tilt_gain(f: f64, db_oct: f64) -> f64 {
    let x = f.min(8000.0) / TILT_F;
    // smooth knee: (1 + x^2)^(db_oct / (20 log10 2) / 2)
    m::pow(1.0 + x * x, db_oct / 6.0206 / 2.0)
}

#[derive(Clone, Copy, Debug)]
pub struct Class {
    pub name: &'static str,
    pub d_min: f64,
    pub d_max: f64,
    pub length_s: f64,
    pub target_lufs: f64,
    pub ceil_db: f64,
    pub attack_s: f64,
    pub release_s: f64,
    pub echo: f64,
    /// empirical excess attenuation (ground effect, turbulent scattering, refraction): dB per km per sqrt(kHz)
    pub excess: f64,
    pub fade_in_s: f64,
    pub fade_out_s: f64,
}

pub fn class(name: &str) -> Option<Class> {
    let c = match name {
        "strike" => Class {
            name: "strike",
            d_min: 60.0,
            d_max: 280.0,
            length_s: 14.0,
            target_lufs: -9.5,
            ceil_db: -0.6,
            attack_s: 0.004,
            release_s: 0.2,
            echo: 0.55,
            excess: 0.0,
            fade_in_s: 0.003,
            fade_out_s: 4.0,
        },
        "close" => Class {
            name: "close",
            d_min: 600.0,
            d_max: 1400.0,
            length_s: 14.0,
            target_lufs: -14.0,
            ceil_db: -1.2,
            attack_s: 0.002,
            release_s: 0.08,
            echo: 0.5,
            excess: 0.0,
            fade_in_s: 0.01,
            fade_out_s: 3.5,
        },
        "mid" => Class {
            name: "mid",
            d_min: 2500.0,
            d_max: 5000.0,
            length_s: 15.0,
            target_lufs: -18.0,
            ceil_db: -1.2,
            attack_s: 0.002,
            release_s: 0.08,
            echo: 0.35,
            excess: 0.15,
            fade_in_s: 0.05,
            fade_out_s: 3.5,
        },
        "far" => Class {
            name: "far",
            d_min: 7000.0,
            d_max: 12000.0,
            length_s: 15.0,
            target_lufs: -22.0,
            ceil_db: -1.2,
            attack_s: 0.002,
            release_s: 0.08,
            echo: 0.25,
            excess: 0.2,
            fade_in_s: 0.2,
            fade_out_s: 3.5,
        },
        _ => return None,
    };
    Some(c)
}

/// ISO 9613-1 atmospheric absorption, dB per metre.
pub fn iso9613_alpha(f: f64, t_c: f64, rh: f64, p_kpa: f64) -> f64 {
    let t = t_c + 273.15;
    let t0 = 293.15;
    let t01 = 273.16;
    let pr = 101.325;
    let pa = p_kpa;
    let csat = -6.8346 * m::pow(t01 / t, 1.261) + 4.6151;
    let h = rh * m::pow(10.0, csat) / (pa / pr);
    let fro = (pa / pr) * (24.0 + 4.04e4 * h * (0.02 + h) / (0.391 + h));
    let frn = (pa / pr) * m::pow(t / t0, -0.5) * (9.0 + 280.0 * h * m::exp(-4.170 * (m::pow(t / t0, -1.0 / 3.0) - 1.0)));
    let f2 = f * f;
    8.686
        * f2
        * (1.84e-11 * (pr / pa) * m::sqrt(t / t0)
            + m::pow(t / t0, -2.5)
                * (0.01275 * m::exp(-2239.1 / t) / (fro + f2 / fro) + 0.1068 * m::exp(-3352.0 / t) / (frn + f2 / frn)))
}

fn alpha(f: f64) -> f64 {
    iso9613_alpha(f, 22.0, 85.0, 101.325)
}

type V3 = [f64; 3];

fn add(a: V3, b: V3) -> V3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}
fn sc(a: V3, s: f64) -> V3 {
    [a[0] * s, a[1] * s, a[2] * s]
}
fn sub(a: V3, b: V3) -> V3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn len(a: V3) -> f64 {
    m::sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
}
fn norm(a: V3) -> V3 {
    sc(a, 1.0 / len(a).max(1e-12))
}
fn cross(a: V3, b: V3) -> V3 {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

/// Turn a unit vector by a random small angle (two Gaussian components of `sigma` radians across it).
fn perturb(rng: &mut Rng, d: V3, sigma: f64) -> V3 {
    let helper = if d[2].abs() < 0.9 { [0.0, 0.0, 1.0] } else { [1.0, 0.0, 0.0] };
    let u = norm(cross(d, helper));
    let v = cross(d, u);
    norm(add(d, add(sc(u, sigma * rng.normal()), sc(v, sigma * rng.normal()))))
}

#[derive(Clone)]
struct Layer {
    pts: Vec<V3>, // a polyline of meso steps
    r0: f64,
    gain: f64,
    t_off: f64,
}

/// A tortuous walk: macro segments (direction change `macro_sigma`, pulled toward `bias` by `pull`), each made of meso
/// steps jittered by `meso_sigma`. Stops when `stop` says so or the length is used up.
fn walk(
    rng: &mut Rng,
    start: V3,
    dir0: V3,
    bias: V3,
    pull: f64,
    total: f64,
    stop: &dyn Fn(V3) -> bool,
) -> Vec<V3> {
    let mut pts = vec![start];
    let mut p = start;
    let mut d = norm(dir0);
    let mut used = 0.0;
    while used < total {
        d = norm(add(perturb(rng, d, 0.22), sc(bias, pull)));
        let mlen = rng.lognormal(25.0, 0.6).clamp(4.0, 150.0);
        let mut ml = 0.0;
        while ml < mlen {
            let step = rng.lognormal(2.5, 0.5).clamp(0.5, 8.0);
            let sd = perturb(rng, d, 0.2);
            p = add(p, sc(sd, step));
            pts.push(p);
            ml += step;
            if stop(p) {
                return pts;
            }
        }
        used += mlen;
    }
    pts
}

struct Geometry {
    layers: Vec<Layer>,
    ground: V3,
    seed: u64,
}

/// Micro-tortuosity: recursive midpoint displacement of every meso step down to pieces of `MICRO_MIN` metres (the
/// channel is tortuous on every scale). Without it each straight step adds up coherently and smears its own shocks,
/// and the thunder loses everything above a few hundred Hz.
const MICRO_MIN: f64 = 0.06;
const MICRO_SIGMA: f64 = 0.22;

fn fractalise(rng: &mut Rng, pts: &[V3]) -> Vec<V3> {
    fn rec(rng: &mut Rng, a: V3, b: V3, out: &mut Vec<V3>) {
        let d = sub(b, a);
        let l = len(d);
        if l <= MICRO_MIN {
            out.push(b);
            return;
        }
        let dir = sc(d, 1.0 / l);
        let helper = if dir[2].abs() < 0.9 { [0.0, 0.0, 1.0] } else { [1.0, 0.0, 0.0] };
        let u = norm(cross(dir, helper));
        let v = cross(dir, u);
        let s = MICRO_SIGMA * l;
        let mid = add(sc(add(a, b), 0.5), add(sc(u, s * rng.normal()), sc(v, s * rng.normal())));
        rec(rng, a, mid, out);
        rec(rng, mid, b, out);
    }
    let mut out = vec![pts[0]];
    for w in pts.windows(2) {
        rec(rng, w[0], w[1], &mut out);
    }
    out
}

fn make_channel(seed: u64, cl: &Class) -> Geometry {
    let mut rng = Rng::new(seed, &format!("thunder-geometry-{}", cl.name));
    let dist = rng.range(cl.d_min, cl.d_max);
    let az = rng.range(0.0, m::TAU);
    let ground = [dist * m::cos(az), dist * m::sin(az), 0.0];
    let top = rng.range(4200.0, 6500.0);
    // the channel's overall lean
    let lean_az = rng.range(0.0, m::TAU);
    let lean = rng.range(0.0, 0.45);
    let up = norm([lean * m::cos(lean_az), lean * m::sin(lean_az), 1.0]);
    let r0 = rng.range(1.0, 3.0);
    let mut micro = Rng::new(seed, &format!("thunder-micro-{}", cl.name));
    let meso = walk(&mut rng, ground, up, up, 0.35, 20000.0, &|p: V3| p[2] >= top);
    let main = fractalise(&mut micro, &meso);
    let mut layers = vec![Layer { pts: main.clone(), r0, gain: 1.0, t_off: 0.0 }];

    // Return strokes down the same channel. The wind carries the hot channel between strokes (5-15 m/s, more aloft),
    // so each stroke's small-scale shape is its own: exact copies would comb-filter the sum at 1 / (stroke interval).
    let mut brng = Rng::new(seed, &format!("thunder-strokes-{}", cl.name));
    let strokes = (1 + brng.poisson(2.0)).min(5);
    let wind_az = brng.range(0.0, m::TAU);
    let wind = brng.range(5.0, 15.0);
    let mut t = 0.0;
    for _ in 1..strokes {
        t += brng.lognormal(0.06, 0.5).clamp(0.02, 0.2);
        let shifted: Vec<V3> = meso
            .iter()
            .map(|p| {
                let d = wind * t * (1.0 + p[2] / 2000.0);
                [p[0] + d * m::cos(wind_az), p[1] + d * m::sin(wind_az), p[2]]
            })
            .collect();
        let pts = fractalise(&mut micro, &shifted);
        layers.push(Layer { pts, r0: r0 * brng.range(0.5, 0.8), gain: brng.range(0.4, 0.8), t_off: t });
    }

    // downward branches from the main channel
    let nb = 3 + brng.poisson(2.5).min(6);
    for _ in 0..nb {
        let cands: Vec<usize> = (0..main.len()).filter(|&i| main[i][2] > 200.0 && main[i][2] < top * 0.85).collect();
        if cands.is_empty() {
            break;
        }
        let i = cands[(brng.f() * cands.len() as f64) as usize];
        let baz = brng.range(0.0, m::TAU);
        let out = [m::cos(baz), m::sin(baz), -brng.range(0.4, 1.2)];
        let blen = brng.lognormal(500.0, 0.5).clamp(100.0, 2000.0);
        let pts = walk(&mut brng, main[i], out, norm(out), 0.25, blen, &|p: V3| p[2] < 30.0);
        let pts = fractalise(&mut micro, &pts);
        layers.push(Layer { pts, r0: r0 * 0.45, gain: 0.5, t_off: 0.0 });
    }

    // horizontal in-cloud channels from the top
    let nic = 1 + brng.poisson(1.2).min(3);
    let topp = *main.last().unwrap();
    for _ in 0..nic {
        let iaz = brng.range(0.0, m::TAU);
        let h = [m::cos(iaz), m::sin(iaz), brng.range(-0.04, 0.06)];
        let ilen = brng.lognormal(5000.0, 0.5).clamp(1500.0, 12000.0);
        let zlo = top - 1200.0;
        let zhi = top + 1500.0;
        let pts = walk(&mut brng, topp, h, norm(h), 0.6, ilen, &|p: V3| p[2] < zlo || p[2] > zhi);
        let pts = fractalise(&mut micro, &pts);
        layers.push(Layer { pts, r0: r0 * 0.55, gain: 0.45, t_off: brng.range(0.0, 0.05) });
    }
    Geometry { layers, ground, seed }
}

/// N-wave length at distance r for relaxation radius r0: 0.66 / f_m at 1 km with Few's f_m = 0.63 c / R0, and the
/// weak-shock lengthening sqrt(1 + 0.5 ln(r / 10 R0)) relative to 1 km.
fn nwave_len(r: f64, r0: f64) -> f64 {
    let tm = 0.66 / (0.63 * SOUND / r0);
    tm * m::sqrt(shock_g(r, r0) / shock_g(1000.0, r0))
}
fn shock_g(r: f64, r0: f64) -> f64 {
    (1.0 + 0.5 * m::ln(r / (10.0 * r0))).max(1.0)
}

/// Spectrum of the N-wave p(t) = 1 - 2t/T on [0, T] at angular frequency w.
fn nwave_spec(w: f64, t: f64) -> C {
    let wt = w * t;
    if wt < 0.05 {
        // series: i w T^2/6 + w^2 T^3/12 - i w^3 T^4/40
        return C::new(w * w * t * t * t / 12.0, w * t * t / 6.0 - w * w * w * t * t * t * t / 40.0);
    }
    let e = C::new(m::cos(wt), -m::sin(wt));
    // term1 = (1 - E) * (-i / w)
    let one_m_e = C::new(1.0 - e.re, -e.im);
    let term1 = one_m_e.mul(C::new(0.0, -1.0 / w));
    // term2 = (2/T) [E (1/w^2 + i T / w) - 1/w^2]
    let inner = e.mul(C::new(1.0 / (w * w), t / w));
    let term2 = C::new(inner.re - 1.0 / (w * w), inner.im).scale(2.0 / t);
    C::new(term1.re - term2.re, term1.im - term2.im)
}

/// Add a weight spread uniformly over [a, b] (in samples) into buf.
fn deposit(buf: &mut [f64], a: f64, b: f64, w: f64) {
    let n = buf.len() as isize;
    if b - a < 1.0 {
        let c = 0.5 * (a + b);
        let i = c.floor();
        let fr = c - i;
        let i = i as isize;
        if i >= 0 && i < n {
            buf[i as usize] += w * (1.0 - fr);
        }
        if i + 1 >= 0 && i + 1 < n {
            buf[(i + 1) as usize] += w * fr;
        }
        return;
    }
    let dens = w / (b - a);
    let mut i = a.floor() as isize;
    while (i as f64) < b {
        let lo = (i as f64).max(a);
        let hi = ((i + 1) as f64).min(b);
        if i >= 0 && i < n && hi > lo {
            buf[i as usize] += dens * (hi - lo);
        }
        i += 1;
    }
}

struct Entry {
    t0: f64,
    t1: f64,
    w: f64,
    img: bool,
}

/// The ground's reflection: 0.85 at low frequencies, falling above ~800 Hz (soft soil and grass absorb the highs, and
/// turbulence decorrelates them), so the image's comb filter fades out up the spectrum as it does outdoors.
fn ground_gain(f: f64) -> f64 {
    GROUND_R / m::sqrt(1.0 + (f / 800.0) * (f / 800.0))
}

/// Attenuation in dB at frequency f over r metres (ISO 9613-1 + the class's excess + a band limit above 16 kHz).
fn atten_db(f: f64, r: f64, excess: f64) -> f64 {
    let mut d = alpha(f) * r + excess * (r / 1000.0) * m::sqrt(f / 1000.0);
    if f > 16000.0 {
        let x = (f - 16000.0) / 1000.0;
        d += 3.0 * x * x;
    }
    d
}

/// Render the dry channel sound (no echoes), time 0 = the flash. Returns the signal and the first arrival (s).
fn render_dry(geo: &Geometry, cl: &Class, total_s: f64) -> (Vec<f64>, f64) {
    let obs: V3 = [0.0, 0.0, OBS_H];
    let img: V3 = [0.0, 0.0, -OBS_H];
    let n_out = (total_s * FS) as usize;
    let mut out = vec![0.0; n_out];
    let r_lo = 20.0;
    let lbin = m::ln(1.0 + BIN_W);
    let r0_main = geo.layers[0].r0;
    let mut first = f64::INFINITY;
    let mut amin_cache: HashMap<(usize, usize), Vec<C>> = HashMap::new();
    let mut tilt_cache: HashMap<usize, Vec<f64>> = HashMap::new();
    let tdb = tilt_db_oct();
    for (li, layer) in geo.layers.iter().enumerate() {
        let mut bins: Vec<Vec<Entry>> = Vec::new();
        for w in layer.pts.windows(2) {
            let (a, b) = (w[0], w[1]);
            let seg = sub(b, a);
            let sl = len(seg);
            let k = (sl / PIECE).ceil().max(1.0) as usize;
            for j in 0..k {
                let p0 = add(a, sc(seg, j as f64 / k as f64));
                let p1 = add(a, sc(seg, (j + 1) as f64 / k as f64));
                let mid = sc(add(p0, p1), 0.5);
                let ds = sl / k as f64;
                let r = len(sub(mid, obs));
                let ri = len(sub(mid, img));
                let ws = m::sqrt(shock_g(1000.0, layer.r0) / shock_g(r, layer.r0));
                let wgt = layer.gain * (layer.r0 / r0_main) * ds / r * ws;
                let bi = (m::ln(r.max(r_lo) / r_lo) / lbin).floor() as usize;
                if bins.len() <= bi {
                    bins.resize_with(bi + 1, Vec::new);
                }
                let t0 = len(sub(p0, obs)) / SOUND + layer.t_off;
                let t1 = len(sub(p1, obs)) / SOUND + layer.t_off;
                first = first.min(t0.min(t1));
                bins[bi].push(Entry { t0: t0.min(t1), t1: t0.max(t1), w: wgt, img: false });
                let ti0 = len(sub(p0, img)) / SOUND + layer.t_off;
                let ti1 = len(sub(p1, img)) / SOUND + layer.t_off;
                bins[bi].push(Entry { t0: ti0.min(ti1), t1: ti0.max(ti1), w: wgt * r / ri, img: true });
            }
        }
        for (bi, ents) in bins.iter().enumerate() {
            if ents.is_empty() {
                continue;
            }
            let rb = r_lo * m::exp((bi as f64 + 0.5) * lbin);
            // The energy put into the channel varies along it, so R0 (the N-wave's length and strength) does too:
            // log-normal, 25 %, drawn per layer and distance bin. A single R0 would leave the N-wave spectrum's nulls
            // (near k / T) as steady stripes through the whole file.
            let mut vr = Rng::new(geo.seed ^ ((li as u64) << 32) ^ bi as u64, "thunder-r0-variation");
            let r0v = vr.lognormal(1.0, 0.25).clamp(0.55, 1.8);
            let tn = nwave_len(rb, layer.r0 * r0v);
            let tmin = ents.iter().map(|e| e.t0).fold(f64::INFINITY, f64::min);
            let tmax = ents.iter().map(|e| e.t1).fold(0.0, f64::max);
            let start = (tmin * FS).floor() as isize - 2;
            let span = ((tmax * FS).ceil() as isize - start + 2) as usize;
            let pad = (tn * FS) as usize + (0.12 * FS) as usize;
            let n = dsp::next_pow2(span + pad).max(4096);
            // direct pieces in one buffer, ground images in another; both go through one complex FFT
            let mut buf = vec![0.0; n];
            let mut ibuf = vec![0.0; n];
            for e in ents {
                let tgt = if e.img { &mut ibuf } else { &mut buf };
                deposit(tgt, e.t0 * FS - start as f64, e.t1 * FS - start as f64, e.w);
            }
            let amin = amin_cache.entry((bi, n)).or_insert_with(|| {
                let lm: Vec<f64> = (0..=n / 2)
                    .map(|k| {
                        let f = k as f64 * FS / n as f64;
                        (-atten_db(f, rb, cl.excess) * m::ln(10.0) / 20.0).max(-25.0)
                    })
                    .collect();
                dsp::min_phase(&lm, n)
            });
            let tilt = tilt_cache.entry(n).or_insert_with(|| {
                (0..=n / 2).map(|k| tilt_gain(k as f64 * FS / n as f64, tdb)).collect::<Vec<f64>>()
            });
            let mut z: Vec<C> = buf.iter().zip(ibuf.iter()).map(|(&a, &b)| C::new(a, b)).collect();
            dsp::fft(&mut z, false);
            let mut x = vec![C::default(); n];
            for k in 0..=n / 2 {
                let zk = z[k];
                let zc = z[(n - k) % n];
                let zc = C::new(zc.re, -zc.im);
                let d = C::new(0.5 * (zk.re + zc.re), 0.5 * (zk.im + zc.im));
                // (zk - zc) / 2i
                let im = C::new(0.5 * (zk.im - zc.im), -0.5 * (zk.re - zc.re));
                let f = k as f64 * FS / n as f64;
                let w = m::TAU * f;
                let s = nwave_spec(w, tn).scale(FS * tilt[k]).mul(amin[k]);
                x[k] = d.add(im.scale(ground_gain(f))).mul(s).scale(r0v);
                if k > 0 && k < n / 2 {
                    x[n - k] = C::new(x[k].re, -x[k].im);
                }
            }
            x[n / 2].im = 0.0;
            x[0].im = 0.0;
            dsp::fft(&mut x, true);
            for (i, z) in x.iter().enumerate() {
                let o = start + i as isize;
                if o >= 0 && (o as usize) < n_out {
                    out[o as usize] += z.re;
                }
            }
        }
    }
    (out, first)
}

/// Frequency where the attenuation over `path` metres reaches 6 dB (log search).
pub fn cutoff_for_path(path: f64) -> f64 {
    let mut lo = 20.0f64;
    let mut hi = 20000.0f64;
    if alpha(hi) * path < 6.0 {
        return hi;
    }
    for _ in 0..40 {
        let mid = m::sqrt(lo * hi);
        if alpha(mid) * path > 6.0 {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    lo
}

/// Echoes: clusters of reflections from extended surfaces, low-passed (forward-backward one-pole x2) by their extra path.
fn add_echoes(x: &[f64], seed: u64, cl: &Class) -> Vec<f64> {
    let mut rng = Rng::new(seed, &format!("thunder-echo-{}", cl.name));
    let mut y = x.to_vec();
    let n_refl = 10 + rng.poisson(16.0 * cl.echo);
    for _ in 0..n_refl {
        let tau = rng.lognormal(0.9, 0.7).clamp(0.08, 7.0);
        let g = cl.echo * rng.range(0.05, 0.25) / (1.0 + tau / 1.5);
        let fc = cutoff_for_path(tau * SOUND).min(rng.range(1200.0, 4000.0)); // the surface's own roll-off
        let a = dsp::onepole_coef(FS, fc);
        let mut f = x.to_vec();
        for _ in 0..2 {
            let mut s = 0.0;
            for v in f.iter_mut() {
                s += a * (*v - s);
                *v = s;
            }
            let mut s = 0.0;
            for v in f.iter_mut().rev() {
                s += a * (*v - s);
                *v = s;
            }
        }
        let spread = rng.range(0.03, 0.25);
        let taps = 8;
        for _ in 0..taps {
            let d = tau + (rng.normal() * spread).abs();
            let gg = g * rng.range(0.5, 1.0) / m::sqrt(taps as f64);
            let di = (d * FS) as usize;
            for i in di..y.len() {
                y[i] += gg * f[i - di];
            }
        }
    }
    y
}

/// Diffuse tail: the roll's scattering off terrain, forest and buildings, as a decaying velvet-noise impulse response
/// (600 taps/s, T60 3.5-6 s) that darkens with its delay (each 0.2 s piece low-passed for its path's air absorption).
/// Its energy relative to the direct sound is `echo` squared.
fn add_reverb(x: &[f64], seed: u64, cl: &Class) -> Vec<f64> {
    let mut rng = Rng::new(seed, &format!("thunder-reverb-{}", cl.name));
    let t60 = rng.range(3.5, 6.0);
    let n = ((t60 * 1.2).min(8.0) * FS) as usize;
    let mut h = vec![0.0; n];
    let cell = FS / 600.0;
    let mut c = 0.03 * FS;
    while c + cell < n as f64 {
        let i = (c + rng.f() * cell) as usize;
        let t = i as f64 / FS;
        let sign = if rng.chance(0.5) { 1.0 } else { -1.0 };
        h[i] += sign * m::exp(-6.9 * t / t60) * (1.0 - m::exp(-t / 0.12));
        c += cell;
    }
    let seg = (0.2 * FS) as usize;
    let mut i0 = 0;
    while i0 < n {
        let i1 = (i0 + seg).min(n);
        let t = (i0 + i1) as f64 * 0.5 / FS;
        let fc = cutoff_for_path(SOUND * t + 200.0).clamp(250.0, 5000.0);
        let a = dsp::onepole_coef(FS, fc);
        for _ in 0..2 {
            let mut s = 0.0;
            for v in h[i0..i1].iter_mut() {
                s += a * (*v - s);
                *v = s;
            }
        }
        i0 = i1;
    }
    let e: f64 = h.iter().map(|v| v * v).sum();
    let g = cl.echo / m::sqrt(e.max(1e-30));
    for v in h.iter_mut() {
        *v *= g;
    }
    let tail = dsp::convolve(x, &h);
    x.iter().zip(tail.iter()).map(|(a, b)| a + b).collect()
}

pub struct Rendered {
    pub signal: Vec<f64>,
    pub info: String,
}

pub fn render(seed: u64, cl: &Class) -> Rendered {
    let geo = make_channel(seed, cl);
    let gd = len(sub(geo.ground, [0.0, 0.0, 0.0]));
    let total_len: f64 = geo.layers.iter().map(|l| l.pts.windows(2).map(|w| len(sub(w[1], w[0]))).sum::<f64>()).sum();
    let horizon = gd / SOUND + cl.length_s + 9.0;
    let (dry, first) = render_dry(&geo, cl, horizon);
    // Cut where the sound becomes audible: the first arrival, or later if that is only a faint precursor (a branch
    // nearer than the main channel): the first 10 ms that reach 24 dB under the loudest. The game delays the file
    // by the strike's distance after the flash.
    let w = (0.01 * FS) as usize;
    let env: Vec<f64> = dry.chunks(w).map(|c| c.iter().map(|v| v * v).sum::<f64>()).collect();
    let emax = env.iter().cloned().fold(0.0, f64::max);
    let audible = env.iter().position(|&e| e >= emax * m::undb(-48.0)).unwrap_or(0) as f64 * 0.01;
    let pre = if cl.name == "strike" { 0.003 } else { 0.01 };
    let start = first.max(audible - 0.05) - pre;
    let s0 = ((start * FS).floor() as isize).max(0) as usize;
    let nlen = (cl.length_s * FS) as usize;
    let dry: Vec<f64> = (0..nlen).map(|i| dry.get(s0 + i).copied().unwrap_or(0.0)).collect();
    let echoed = add_echoes(&dry, seed, cl);
    let mut y = add_reverb(&echoed, seed, cl);
    let mut hp = Biquad::highpass(FS, 25.0, 0.7071);
    hp.run(&mut y);
    let fi = (cl.fade_in_s * FS) as usize;
    for (i, v) in y.iter_mut().enumerate().take(fi) {
        *v *= i as f64 / fi as f64;
    }
    let fo = (cl.fade_out_s * FS) as usize;
    for i in 0..fo {
        let k = nlen - fo + i;
        y[k] *= m::cos(0.5 * m::PI * i as f64 / fo as f64);
    }
    // WEATHERSYNTH_RAW=1: peak-normalised only, no limiter (to look at the natural envelope)
    let (y, gain_db) = if std::env::var("WEATHERSYNTH_RAW").is_ok() {
        let pk = y.iter().fold(0.0f64, |a, v| a.max(v.abs())).max(1e-30);
        (y.iter().map(|v| v * 0.9 / pk).collect(), m::db(0.9 / pk))
    } else {
        normalise_peak_class(&y, cl)
    };
    let info = format!(
        "class={} seed={} ground_distance_m={:.0} channel_m={:.0} layers={} r0_m={:.2} first_arrival_s={:.3} file_start_s={:.3} makeup_gain_db={:.1}",
        cl.name,
        seed,
        gd,
        total_len,
        geo.layers.len(),
        geo.layers[0].r0,
        first,
        start,
        gain_db
    );
    Rendered { signal: y, info }
}

/// Gain into the limiter until the loudest 3 s reach the class's loudness (bisection; the loudness never falls as the
/// gain rises).
fn normalise_peak_class(x: &[f64], cl: &Class) -> (Vec<f64>, f64) {
    let env = loudness::tp_envelope(x, false);
    let ceil = m::undb(cl.ceil_db);
    let attack = (cl.attack_s * FS).round() as usize;
    let apply = |gdb: f64| -> Vec<f64> {
        let g = m::undb(gdb);
        let e: Vec<f64> = env.iter().map(|v| v * g).collect();
        let gc = loudness::limiter_gain(&e, ceil, attack, cl.release_s, FS);
        x.iter().zip(gc.iter()).map(|(a, b)| a * g * b).collect()
    };
    let (mut lo, mut hi) = (-80.0, 80.0);
    for _ in 0..32 {
        let mid = 0.5 * (lo + hi);
        if loudness::max_short_term(&apply(mid), FS) < cl.target_lufs {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let g = 0.5 * (lo + hi);
    (apply(g), g)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso9613_matches_the_standards_table() {
        // ISO 9613-2 Table 2, 20 C, 70 % humidity: dB/km at 1, 4, 8 kHz
        for (f, want) in [(1000.0, 5.0), (4000.0, 22.9), (8000.0, 76.6)] {
            let got = iso9613_alpha(f, 20.0, 70.0, 101.325) * 1000.0;
            assert!((got - want).abs() / want < 0.03, "{f} Hz: {got} vs {want}");
        }
    }

    #[test]
    fn nwave_spectrum_peaks_near_0_66_over_t() {
        let t = 0.01;
        let mut best = (0.0, 0.0);
        let mut f = 1.0;
        while f < 500.0 {
            let s = nwave_spec(m::TAU * f, t);
            let a = m::sqrt(s.re * s.re + s.im * s.im);
            if a > best.1 {
                best = (f, a);
            }
            f += 0.5;
        }
        assert!((best.0 * t - 0.66).abs() < 0.03, "peak at {} Hz", best.0);
        // the series branch joins the closed form
        let a = nwave_spec(0.0499 / t, t);
        let b = nwave_spec(0.0501 / t, t);
        let mag = m::sqrt(a.re * a.re + a.im * a.im);
        assert!((a.re - b.re).abs() < 0.01 * mag && (a.im - b.im).abs() < 0.01 * mag);
    }
}
