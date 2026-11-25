//! Deterministic maths. Every transcendental function goes through libm (pure Rust), so a seed renders the same bits on
//! Linux, Windows, macOS and any CPU. sqrt, floor, abs and the basic operations are exact IEEE operations already.

pub const PI: f64 = std::f64::consts::PI;
pub const TAU: f64 = 2.0 * PI;

#[inline]
pub fn sin(x: f64) -> f64 {
    libm::sin(x)
}
#[inline]
pub fn cos(x: f64) -> f64 {
    libm::cos(x)
}
#[inline]
pub fn tan(x: f64) -> f64 {
    libm::tan(x)
}
#[inline]
pub fn exp(x: f64) -> f64 {
    libm::exp(x)
}
#[inline]
pub fn ln(x: f64) -> f64 {
    libm::log(x)
}
#[inline]
pub fn log10(x: f64) -> f64 {
    libm::log10(x)
}
#[inline]
pub fn pow(x: f64, y: f64) -> f64 {
    libm::pow(x, y)
}
#[inline]
pub fn sqrt(x: f64) -> f64 {
    x.sqrt()
}
#[inline]
pub fn db(x: f64) -> f64 {
    20.0 * log10(x.max(1e-30))
}
#[inline]
pub fn undb(d: f64) -> f64 {
    pow(10.0, d / 20.0)
}

pub const LN_10: f64 = std::f64::consts::LN_10;

pub fn tanh(x: f64) -> f64 {
    libm::tanh(x)
}

/// Round half away from zero (libm, the same on every platform).
pub fn round(x: f64) -> f64 {
    libm::round(x)
}
