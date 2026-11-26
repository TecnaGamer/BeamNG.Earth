//! Objective description of a sound file: loudness, peaks, spectrum shares, onset rise, decay, noise floor, loop seam.

use crate::dsp::{self, C};
use crate::loudness;
use crate::m;

pub fn header() -> &'static str {
    "file\tdur_s\tint_lufs\tloop_lufs\tmax_st_lufs\ttp_dbtp\tpeak_freq_hz\tcentroid_hz\trolloff95_hz\t\
     lt60%\t60-250%\t250-1k%\t1k-4k%\t4k-12k%\tgt12k%\tonset_s\trise10_90_ms\tpeak_after_onset_s\t\
     decay20_s\tdecay40_s\tfloor_db\tcrest_db\tseam\ttrans_per_s\tenv_p99_p50_db"
}

pub fn describe(name: &str, x: &[f64], fs: f64) -> String {
    let n = x.len();
    let dur = n as f64 / fs;
    let il = loudness::integrated(x, fs, false);
    let ll = loudness::integrated(x, fs, true);
    let st = loudness::max_short_term(x, fs);
    let tp = loudness::true_peak_db(x, false);

    // Welch PSD, 4096 Hann, 50 % overlap
    let nf = 4096;
    let hann: Vec<f64> = (0..nf).map(|i| 0.5 - 0.5 * m::cos(m::TAU * i as f64 / nf as f64)).collect();
    let mut psd = vec![0.0; nf / 2 + 1];
    let mut i = 0;
    let mut frames = 0;
    while i + nf <= n {
        let mut b: Vec<C> = (0..nf).map(|k| C::new(x[i + k] * hann[k], 0.0)).collect();
        dsp::fft(&mut b, false);
        for k in 0..=nf / 2 {
            psd[k] += b[k].re * b[k].re + b[k].im * b[k].im;
        }
        frames += 1;
        i += nf / 2;
    }
    let df = fs / nf as f64;
    let tot: f64 = psd.iter().skip(1).sum::<f64>().max(1e-30);
    let band = |lo: f64, hi: f64| -> f64 {
        let s: f64 = psd.iter().enumerate().skip(1).filter(|(k, _)| {
            let f = *k as f64 * df;
            f >= lo && f < hi
        }).map(|(_, v)| *v).sum();
        100.0 * s / tot
    };
    let centroid = psd.iter().enumerate().skip(1).map(|(k, v)| k as f64 * df * v).sum::<f64>() / tot;
    let mut acc = 0.0;
    let mut roll = 0.0;
    for (k, v) in psd.iter().enumerate().skip(1) {
        acc += v;
        if acc >= 0.95 * tot {
            roll = k as f64 * df;
            break;
        }
    }
    // peak of the 1/3-octave-smoothed spectrum
    let mut pk = (0.0, 0.0);
    for (k, _) in psd.iter().enumerate().skip(2) {
        let f = k as f64 * df;
        let lo = ((f / 1.122) / df) as usize;
        let hi = (((f * 1.122) / df) as usize).min(nf / 2);
        let s: f64 = psd[lo.max(1)..=hi.max(lo.max(1))].iter().sum::<f64>() / (hi.max(lo.max(1)) - lo.max(1) + 1) as f64;
        if s > pk.1 {
            pk = (f, s);
        }
    }
    let _ = frames;

    // envelopes
    let rms_env = |win: usize| -> Vec<f64> {
        let mut out = Vec::with_capacity(n);
        let mut a = 0.0;
        for j in 0..n {
            a += x[j] * x[j];
            if j >= win {
                a -= x[j - win] * x[j - win];
            }
            out.push(m::sqrt((a / win as f64).max(0.0)));
        }
        out
    };
    let e1 = rms_env((0.001 * fs) as usize);
    let e50 = rms_env((0.05 * fs) as usize);
    let emax = e1.iter().cloned().fold(0.0, f64::max).max(1e-30);
    let onset = e1.iter().position(|&v| v > emax * m::undb(-40.0)).unwrap_or(0);
    let wend = (onset + (0.3 * fs) as usize).min(n);
    let m1 = e1[onset..wend].iter().cloned().fold(0.0, f64::max);
    let t10 = e1[onset..wend].iter().position(|&v| v >= 0.1 * m1).unwrap_or(0);
    let t90 = e1[onset..wend].iter().position(|&v| v >= 0.9 * m1).unwrap_or(0);
    let pmax_i = e50.iter().enumerate().fold((0, 0.0), |a, (j, &v)| if v > a.1 { (j, v) } else { a });
    let last_above = |thr_db: f64| -> f64 {
        let thr = pmax_i.1 * m::undb(thr_db);
        let mut last = pmax_i.0;
        for j in pmax_i.0..n {
            if e50[j] > thr {
                last = j;
            }
        }
        (last - pmax_i.0) as f64 / fs
    };
    // floor: the quietest 10 % of 50 ms windows (excluding the fades: middle 80 % of the file), relative to the loudest
    let lo = n / 10;
    let hi = n - n / 10;
    let mut mids: Vec<f64> = e50[lo..hi].iter().step_by((0.05 * fs) as usize).cloned().collect();
    mids.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let floor = if mids.is_empty() { 0.0 } else { m::db(mids[mids.len() / 10] / pmax_i.1.max(1e-30)) };
    let rms_all = m::sqrt(x.iter().map(|v| v * v).sum::<f64>() / n as f64);
    let peak = x.iter().fold(0.0f64, |a, v| a.max(v.abs()));
    let crest = m::db(peak / rms_all.max(1e-30));
    // seam: second difference across the wrap vs the file's 99.9th percentile of |second difference|
    let mut d2: Vec<f64> = (1..n - 1).map(|j| (x[j + 1] - 2.0 * x[j] + x[j - 1]).abs()).collect();
    d2.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p999 = d2[(d2.len() as f64 * 0.999) as usize];
    let wrap = (x[0] - 2.0 * x[n - 1] + x[n - 2]).abs().max((x[1] - 2.0 * x[0] + x[n - 1]).abs());
    let (trans, spread) = transients(x, fs);
    format!(
        "{name}\t{dur:.2}\t{il:.1}\t{ll:.1}\t{st:.1}\t{tp:.2}\t{:.0}\t{centroid:.0}\t{roll:.0}\t{:.1}\t{:.1}\t{:.1}\t{:.1}\t{:.1}\t{:.1}\t{:.3}\t{:.2}\t{:.2}\t{:.2}\t{:.2}\t{floor:.1}\t{crest:.1}\t{:.2}\t{trans:.1}\t{spread:.1}",
        pk.0,
        band(0.0, 60.0),
        band(60.0, 250.0),
        band(250.0, 1000.0),
        band(1000.0, 4000.0),
        band(4000.0, 12000.0),
        band(12000.0, 30000.0),
        onset as f64 / fs,
        (t90 as f64 - t10 as f64) / fs * 1000.0,
        (pmax_i.0 as f64 - onset as f64) / fs,
        last_above(-20.0),
        last_above(-40.0),
        wrap / p999.max(1e-30),
    )
}

/// How distinct the transients are: 5 ms RMS frames every 1 ms; a transient is a local peak at least 10 dB over the
/// running median of the surrounding 0.5 s, at most one per 25 ms. Returns (transients per second, the frames'
/// 99th minus 50th percentile in dB).
fn transients(x: &[f64], fs: f64) -> (f64, f64) {
    let hop = (0.001 * fs) as usize;
    let win = (0.005 * fs) as usize;
    let mut db = Vec::new();
    let mut i = 0;
    while i + win <= x.len() {
        let e: f64 = x[i..i + win].iter().map(|v| v * v).sum::<f64>() / win as f64;
        db.push(10.0 * m::log10(e.max(1e-20)));
        i += hop;
    }
    let n = db.len();
    if n < 3 {
        return (0.0, 0.0);
    }
    let half = 250;
    let mut count = 0usize;
    let mut last: isize = -1000;
    let mut tmp = Vec::with_capacity(2 * half + 1);
    for j in 1..n - 1 {
        if !(db[j] >= db[j - 1] && db[j] >= db[j + 1]) {
            continue;
        }
        if (j as isize) - last < 25 {
            continue;
        }
        tmp.clear();
        let lo = j.saturating_sub(half);
        let hi = (j + half).min(n - 1);
        tmp.extend_from_slice(&db[lo..=hi]);
        tmp.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let med = tmp[tmp.len() / 2];
        if db[j] > med + 10.0 {
            count += 1;
            last = j as isize;
        }
    }
    let mut s = db.clone();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let p = |q: f64| s[((s.len() - 1) as f64 * q) as usize];
    (count as f64 / (x.len() as f64 / fs), p(0.99) - p(0.5))
}
