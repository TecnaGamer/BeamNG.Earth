//! Python's float semantics the ported glue needs bit for bit: `round(x, ndigits)` on a Python float (CPython's
//! double_round: the correctly rounded decimal string, ties to even on the exact binary value, read back), numpy's
//! `np.round` (multiply by 10**ndigits, rint, divide), and mapcells.cell_of (the exact cell edges).

use crate::dm::Dm;

/// CPython's `round(x, ndigits)` of a float, ndigits >= 0: x unchanged when it is not finite, else the decimal string
/// correctly rounded to ndigits places (ties to even on the exact binary value: _Py_dg_dtoa mode 3) read back
/// (_Py_dg_strtod, correctly rounded). Rust's exact fixed-precision formatting (flt2dec's exact mode, Dragon4 behind
/// Grisu) rounds the same exact value with the same ties-to-even rule, and str::parse::<f64> is correctly rounded.
/// A result that rounds to zero keeps x's sign (-1e-9 -> -0.0), as CPython's string "-0e..." does.
pub fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // (CPython returns x itself past NDIGITS_MAX = 323: every double has fewer decimals than that)
    if ndigits > 323 {
        return x;
    }
    let s = format!("{:.*}", ndigits, x);
    s.parse::<f64>().unwrap_or(x)
}

/// numpy's `np.round(x, decimals)` (and a numpy float64 scalar's `round(x, decimals)`), decimals >= 0: x * 10**d
/// rounded to the nearest even integer (rint), / 10**d.
pub fn np_round(x: f64, decimals: i32) -> f64 {
    let f = 10f64.dpowf(decimals as f64);
    (x * f).round_ties_even() / f
}

pub const ZOOM: u32 = 14;
pub const MAX_LAT: f64 = 85.0511287798066;
const EDGE_EPS: f64 = 1e-6;

/// mapcells._lat_of: the latitude of the fractional tile row yf (0 = the north edge) at n tiles per axis (Python's
/// math.degrees: x * (180 / pi), Rust's to_degrees).
fn lat_of(yf: f64, n: f64) -> f64 {
    (std::f64::consts::PI * (1.0 - 2.0 * yf / n)).dsinh().datan().to_degrees()
}

fn lon_of(x: f64, n: f64) -> f64 {
    x / n * 360.0 - 180.0
}

/// mapcells.tile_xy of one point (np.radians: x * (pi / 180), Rust's to_radians).
pub(crate) fn tile_xy(lat: f64, lon: f64, n: f64) -> (f64, f64) {
    let lat = if lat < -MAX_LAT { -MAX_LAT } else if lat > MAX_LAT { MAX_LAT } else { lat };
    let x = (lon / 360.0 + 0.5) * n;
    let y = (0.5 - lat.to_radians().dtan().dasinh() / (2.0 * std::f64::consts::PI)) * n;
    (x, y)
}

/// mapcells.cell_of(lat, lon, z): (x, y) of the cell holding the point, a point on an edge in the cell east / south of
/// it, 180 E wrapped to x = 0. Err (mapcells' ValueError / OverflowError) for a latitude beyond the Web-Mercator limit
/// or a longitude that is not finite.
pub fn cell_of(lat: f64, lon: f64, z: u32) -> Result<(i64, i64), String> {
    if !(-MAX_LAT..=MAX_LAT).contains(&lat) {
        return Err(format!("latitude {lat} is beyond the Web-Mercator limit {MAX_LAT}"));
    }
    let ni = 1i64 << z;
    let n = ni as f64;
    let (xf, yf) = tile_xy(lat, lon, n);
    if !xf.is_finite() {
        return Err(format!("cannot convert float {xf} to integer"));
    }
    // _exact_col
    let mut x = xf.floor() as i64;
    if xf - (x as f64) < EDGE_EPS && lon < lon_of(x as f64, n) {
        x -= 1;
    } else if (x + 1) as f64 - xf < EDGE_EPS && lon >= lon_of((x + 1) as f64, n) {
        x += 1;
    }
    // _exact_row
    let mut y = (yf.floor() as i64).clamp(0, ni - 1);
    if yf - (y as f64) < EDGE_EPS && y > 0 && lat > lat_of(y as f64, n) {
        y -= 1;
    } else if (y + 1) as f64 - yf < EDGE_EPS && y < ni - 1 && lat <= lat_of((y + 1) as f64, n) {
        y += 1;
    }
    Ok((x.rem_euclid(ni), y))
}
