//! The sea's / lakes' per-cell lookup (the full port's seacarve.SeaCarver.water cell body, 495): the floor depth
//! bilinear over the 2 x 2 pixels round a point and the surface level of its nearest pixel, read straight from the
//! cell's block-stored raster (seacarve._Blocks: RAW_DTYPE records, RAW_BLOCK_PX blocks, an index of slots). Python
//! did it with whole-array NumPy steps and _Blocks.__getitem__ (floor division, modulo and a gather for each of the
//! six pixel reads): ~2.6 CPU-s over Manhattan, Fells Point and Vienna's cold builds (2026-11-18 profile).
//! The numbers are NumPy's (x86-64) to the bit: the same f64 products and sums in its order (the f32 pixels widened),
//! the f32 rounding of the result, NaNs as its vector loops propagate them (x86),NumPy's float64 -> int64 cast on
//! x86-64 (NaN and out of range: i64::MIN) and np.clip (NaN through, -0.0 kept); the near-sea test in f32 as NumPy's
//! weak Python-float scalars give it.
use numpy::{PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray1, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// seacarve.RAW_DTYPE: [("depth", "<f4"), ("level", "<f4"), ("outline", "?")], packed (9 bytes a record)
const REC: usize = 9;
const OFF_DEPTH: usize = 0;
const OFF_LEVEL: usize = 4;

/// NumPy's float64 -> int64 cast on x86-64 (cvttsd2si): truncation, NaN and out of range give i64::MIN.
#[inline]
fn np_i64(v: f64) -> i64 {
    if (-9_223_372_036_854_775_808.0..9_223_372_036_854_775_808.0).contains(&v) {
        v as i64
    } else {
        i64::MIN
    }
}

/// np.clip(v, 0, 1) for a float: NaN through, -0.0 kept.
#[inline]
fn clip01(v: f64) -> f64 {
    if v.is_nan() {
        v
    } else if v < 0.0 {
        0.0
    } else if v > 1.0 {
        1.0
    } else {
        v
    }
}

/// The result of a NumPy f64 operation a OP b on x86-64: a NaN operand's NaN, quieted (a's when both are: NumPy's own
/// vector body; its tail loops give b's, so where two NaNs meet NumPy's bits follow the element's place in the array
/// and the CPU's vector width, and these are the body's on every CPU), else the default NaN (negative); whichever
/// order the compiler gives the operands here.
#[inline]
fn x86(a: f64, b: f64, r: f64) -> f64 {
    const QUIET: u64 = 0x0008_0000_0000_0000;
    if !r.is_nan() {
        r
    } else if a.is_nan() {
        f64::from_bits(a.to_bits() | QUIET)
    } else if b.is_nan() {
        f64::from_bits(b.to_bits() | QUIET)
    } else {
        f64::from_bits(0xFFF8_0000_0000_0000)
    }
}

#[inline]
fn add(a: f64, b: f64) -> f64 {
    x86(a, b, a + b)
}

#[inline]
fn sub(a: f64, b: f64) -> f64 {
    x86(a, b, a - b)
}

#[inline]
fn mul(a: f64, b: f64) -> f64 {
    x86(a, b, a * b)
}

/// f32 -> f64 as x86-64 widens (cvtss2sd): a NaN keeps its sign and payload, quieted.
#[inline]
fn widen(x: f32) -> f64 {
    if x.is_nan() {
        let b = x.to_bits() as u64;
        f64::from_bits(((b & 0x8000_0000) << 32) | 0x7FF8_0000_0000_0000 | ((b & 0x007F_FFFF) << 29))
    } else {
        x as f64
    }
}

/// f64 -> f32 as x86-64 narrows (cvtsd2ss, round to nearest): a NaN keeps its sign and the payload's top bits, quieted.
#[inline]
fn narrow(v: f64) -> f32 {
    if v.is_nan() {
        let b = v.to_bits();
        f32::from_bits((((b >> 63) as u32) << 31) | 0x7FC0_0000 | (((b >> 29) as u32) & 0x003F_FFFF))
    } else {
        v as f32
    }
}

struct Raster<'a> {
    idx: numpy::ndarray::ArrayView2<'a, i32>,
    raw: &'a [u8],
    bh: usize,
    bw: usize,
}

impl Raster<'_> {
    #[inline]
    fn at(&self, j: usize, i: usize, off: usize) -> [u8; 4] {
        let s = self.idx[[j / self.bh, i / self.bw]] as usize;     // (checked: 0 <= s < slots)
        let o = ((s * self.bh + j % self.bh) * self.bw + i % self.bw) * REC + off;
        [self.raw[o], self.raw[o + 1], self.raw[o + 2], self.raw[o + 3]]
    }

    #[inline]
    fn depth(&self, j: usize, i: usize) -> f64 {
        widen(f32::from_le_bytes(self.at(j, i, OFF_DEPTH)))
    }

    #[inline]
    fn level(&self, j: usize, i: usize) -> f32 {
        f32::from_bits(u32::from_le_bytes(self.at(j, i, OFF_LEVEL)))      // (the bits as stored: NaN payloads too)
    }
}

/// seacarve.SeaCarver.water's lookup in cell (kx, ky) (origin ox, oy in z16 pixels: kx * CELL_PX, ky * CELL_PX) for
/// the points whose cell (cx, cy) it is (mx, my: their fractional z16 pixels; all flat, one order), from a raster of
/// h x w pixels stored as bh x bw blocks: idx (slots of the blocks), raw (the slots' RAW_DTYPE records as bytes). With
/// near (a raster without the sea's bathymetry, seacarve._near_sea): False, nothing written, when any point's 2 x 2
/// pixels hold a level within thr of sea_e (as f32s). Otherwise d (the floor depth, bilinear) and lv (the nearest
/// pixel's level) are written at those points, True.
#[pyfunction]
#[pyo3(signature = (mx, my, cx, cy, kx, ky, ox, oy, idx, raw, bh, bw, h, w, near, sea_e, thr, d, lv))]
#[allow(clippy::too_many_arguments)]
pub fn sea_cell_lookup(mx: PyReadonlyArray1<'_, f64>, my: PyReadonlyArray1<'_, f64>, cx: PyReadonlyArray1<'_, i64>,
                       cy: PyReadonlyArray1<'_, i64>, kx: i64, ky: i64, ox: f64, oy: f64,
                       idx: PyReadonlyArray2<'_, i32>, raw: PyReadonlyArray1<'_, u8>, bh: usize, bw: usize, h: usize,
                       w: usize, near: bool, sea_e: f64, thr: f64, mut d: PyReadwriteArray1<'_, f32>,
                       mut lv: PyReadwriteArray1<'_, f32>) -> PyResult<bool> {
    // (rust-numpy's views assume aligned data: an unaligned NumPy array, a view at a byte offset, is refused first)
    if !(mx.is_aligned() && my.is_aligned() && cx.is_aligned() && cy.is_aligned() && idx.is_aligned()
         && d.is_aligned() && lv.is_aligned()) {
        return Err(PyValueError::new_err("sea_cell_lookup: unaligned arrays"));
    }
    let (mx, my, cx, cy) = (mx.as_array(), my.as_array(), cx.as_array(), cy.as_array());
    let n = mx.len();
    if my.len() != n || cx.len() != n || cy.len() != n || d.len() != n || lv.len() != n {
        return Err(PyValueError::new_err("sea_cell_lookup: mx, my, cx, cy, d and lv alike"));
    }
    let raw = raw.as_slice().map_err(|_| PyValueError::new_err("sea_cell_lookup: raw not contiguous"))?;
    let idx = idx.as_array();
    let (ir, ic) = idx.dim();
    let block = bh.checked_mul(bw).and_then(|v| v.checked_mul(REC));
    let Some(block) = block.filter(|&v| v > 0 && raw.len() % v == 0) else {
        return Err(PyValueError::new_err("sea_cell_lookup: raw is not whole bh x bw blocks"));
    };
    let slots = raw.len() / block;
    if h < 2 || w < 2 || ir.checked_mul(bh).is_none_or(|v| v < h) || ic.checked_mul(bw).is_none_or(|v| v < w) {
        return Err(PyValueError::new_err("sea_cell_lookup: a raster of at least 2 x 2 pixels, inside its blocks"));
    }
    if idx.iter().any(|&s| s < 0 || s as usize >= slots) {
        return Err(PyValueError::new_err("sea_cell_lookup: raster index out of range"));
    }
    let r = Raster { idx, raw, bh, bw };
    let (hi_j, hi_i) = (h as i64 - 2, w as i64 - 2);
    let corner = |fx: f64, fy: f64| -> (usize, usize) {
        (np_i64(fx.floor()).clamp(0, hi_i) as usize, np_i64(fy.floor()).clamp(0, hi_j) as usize)
    };
    if near {
        let (e, t) = (sea_e as f32, thr as f32);
        for k in 0..n {
            if cx[k] != kx || cy[k] != ky {
                continue;
            }
            let (i0, j0) = corner((mx[k] - ox) + 0.5, (my[k] - oy) + 0.5);
            for (a, b) in [(0, 0), (0, 1), (1, 0), (1, 1)] {
                if (r.level(j0 + a, i0 + b) - e).abs() < t {
                    return Ok(false);
                }
            }
        }
    }
    let mut d = d.as_array_mut();
    let mut lv = lv.as_array_mut();
    for k in 0..n {
        if cx[k] != kx || cy[k] != ky {
            continue;
        }
        let fx = add(sub(mx[k], ox), 0.5);      // raster coords: pixel centres at i + 0.5 - 1
        let fy = add(sub(my[k], oy), 0.5);
        let (i0, j0) = corner(fx, fy);
        let tx = clip01(sub(fx, i0 as f64));
        let ty = clip01(sub(fy, j0 as f64));
        let (ux, uy) = (sub(1.0, tx), sub(1.0, ty));
        let top = add(mul(r.depth(j0, i0), ux), mul(r.depth(j0, i0 + 1), tx));
        let bottom = add(mul(r.depth(j0 + 1, i0), ux), mul(r.depth(j0 + 1, i0 + 1), tx));
        d[k] = narrow(add(mul(top, uy), mul(bottom, ty)));
        let ni = np_i64((fx + 0.5).floor()).clamp(0, w as i64 - 1) as usize;
        let nj = np_i64((fy + 0.5).floor()).clamp(0, h as i64 - 1) as usize;
        lv[k] = r.level(nj, ni);
    }
    Ok(true)
}
