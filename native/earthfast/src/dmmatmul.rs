//! dnp.matmul / dnp.dot in Rust (earthfast 472, the full Rust port): numpy's matmul rules with the products summed in
//! the order the Python summed them, bit for bit (tests/test_dnp_matmul_native.py keeps that Python as the reference).
//! The Python was numpy array arithmetic, 229k small calls a cold visit (sidewalks, junction cuts, corners, building
//! areas), its cost the per-call array overhead.
//!
//! The order (A (S.., n, k) and B (S.., k, m) after a 1-D operand's promotion; every product A[.., i, q] * B[.., q, j]
//! rounded once, never fused):
//! - k <= 8: p0 + p1 + ... + p(k-1), left to right (the Python's column loop: out = out + a[.., j] * b[j, ..]);
//! - k > 8, m == 1: 0.0 + numpy's pairwise sum of p0 .. p(k-1) (np.add.reduce over the (.., n, k, 1) products: the
//!   reduced axis is the inner one, numpy's DOUBLE_pairwise_sum: 8 accumulators to 128, halves above);
//! - k > 8, m > 1: 0.0 + p0 + p1 + ... left to right (np.add.reduce over axis -2 with m inner: one add per q).
//!
//! NaNs: where a NaN operand meets another number or NaN, the NaN kept is the one numpy's x86-64 loops kept (ad / mu:
//! the ufunc's first operand's, quieted; the pairwise sum's tree as its compiled code ordered it, see pairwise), so a
//! NaN's sign and payload are the Python's too.
//!
//! numpy's floating-point errors: the Python's ufunc calls (each column's multiply and add; the k > 8 multiply and
//! reduce) set the overflow / invalid flags, reported as np.errstate says (a warning by default; FloatingPointError
//! under "raise"). The kernel tracks which of those calls would have set which flag and, where any did, has numpy
//! report the same flags for the same calls in the same order (a tiny np.multiply / np.add / np.add.reduce that sets
//! exactly them), so errstate acts as before. Underflow (ignored by numpy's default errstate, set nowhere in the
//! server) and signalling NaNs are not tracked.
//!
//! Input contract: a and b exact numpy.ndarray (no subclass), both native float64 or both native float32, of 1+ axes;
//! anything else TypeError (dnp.py's glue makes them: np.asarray, the result dtype, integers to np.matmul). Their
//! values are copied into Rust-owned storage (any strides) before anything else is called.

use numpy::npyffi::{self, PY_ARRAY_API};
use numpy::{Element, PyArray, PyArrayDescrMethods, PyArrayDyn, PyArrayMethods, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

const OVER: u8 = 1;
const INVALID: u8 = 2;
const PW_BLOCKSIZE: usize = 128;
const DETACH_WORK: usize = 1 << 15;

pub(crate) trait Fl: Element + Copy + std::ops::Add<Output = Self> + std::ops::Mul<Output = Self> + Send + Sync {
    const ZERO: Self;
    const NEG_ZERO: Self;
    const BIG: Self;
    const INF: Self;
    const NEG_INF: Self;
    const TWO: Self;
    fn finite(self) -> bool;
    fn nan(self) -> bool;
    /// self with its quiet bit set (x86's propagated NaN)
    fn quiet(self) -> Self;
}

impl Fl for f64 {
    const ZERO: f64 = 0.0;
    const NEG_ZERO: f64 = -0.0;
    const BIG: f64 = f64::MAX;
    const INF: f64 = f64::INFINITY;
    const NEG_INF: f64 = f64::NEG_INFINITY;
    const TWO: f64 = 2.0;
    fn finite(self) -> bool { self.is_finite() }
    fn nan(self) -> bool { self.is_nan() }
    fn quiet(self) -> f64 { f64::from_bits(self.to_bits() | 0x0008_0000_0000_0000) }
}

impl Fl for f32 {
    const ZERO: f32 = 0.0;
    const NEG_ZERO: f32 = -0.0;
    const BIG: f32 = f32::MAX;
    const INF: f32 = f32::INFINITY;
    const NEG_INF: f32 = f32::NEG_INFINITY;
    const TWO: f32 = 2.0;
    fn finite(self) -> bool { self.is_finite() }
    fn nan(self) -> bool { self.is_nan() }
    fn quiet(self) -> f32 { f32::from_bits(self.to_bits() | 0x0040_0000) }
}

/// x + y and x * y as numpy's loops computed them on x86 (x the ufunc's first operand: the accumulator of a sum, a's
/// element of a product), with the NaN rule of SSE / AVX made explicit (the compiler may swap a commutative
/// operation's operands): a NaN operand's NaN is the result, the first operand's where both are, quieted; an invalid
/// operation of two numbers gives the hardware's own default NaN.
#[inline]
fn ad<T: Fl>(x: T, y: T) -> T {
    let s = x + y;
    if s.nan() { nan_of(x, y, s) } else { s }
}

#[inline]
fn mu<T: Fl>(x: T, y: T) -> T {
    let s = x * y;
    if s.nan() { nan_of(x, y, s) } else { s }
}

#[cold]
fn nan_of<T: Fl>(x: T, y: T, s: T) -> T {
    if x.nan() { x.quiet() } else if y.nan() { y.quiet() } else { s }
}

/// The flags an operation x op y = r would have set (overflow: finite operands, an infinite result; invalid: no NaN
/// operand, a NaN result).
#[inline]
fn flags<T: Fl>(x: T, y: T, r: T) -> u8 {
    if r.finite() {
        0
    } else if r.nan() {
        if x.nan() || y.nan() { 0 } else { INVALID }
    } else if x.finite() && y.finite() {
        OVER
    } else {
        0
    }
}

/// numpy's pairwise sum (loops_utils.h.src, @TYPE@_pairwise_sum) of v, the reduce loop's own: below 8 one by one
/// from -0.0 (not reached from matmul: k > 8 here, the halves 64 or more), to 128 eight accumulators, above that the two halves (the first a multiple of 8). TRACK: the flags of
/// every add. depth: the recursion's (0 the reduce loop's call): the operands' order where two NaNs meet is the
/// compiled wheel's (numpy 2.5's x86-64 build: the function inlined into itself once, so the two code copies
/// alternate by depth: at an even depth r3 + r2 and r5 + r4 in the accumulators' tree and the halves first + second,
/// at an odd depth r2 + r3, r4 + r5 and second + first; fitted on every NaN pair placement, tests/test_dnp_matmul_native.py).
fn pairwise<T: Fl, const TRACK: bool>(v: &[T], depth: u32, fl: &mut u8) -> T {
    let n = v.len();
    if n < 8 {
        let mut res = T::NEG_ZERO;
        for &x in v {
            let r = ad(res, x);
            if TRACK { *fl |= flags(res, x, r); }
            res = r;
        }
        res
    } else if n <= PW_BLOCKSIZE {
        let mut r = [v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]];
        let mut i = 8;
        while i < n - (n % 8) {
            for j in 0..8 {
                let s = ad(r[j], v[i + j]);
                if TRACK { *fl |= flags(r[j], v[i + j], s); }
                r[j] = s;
            }
            i += 8;
        }
        let add = |x: T, y: T, fl: &mut u8| {
            let s = ad(x, y);
            if TRACK { *fl |= flags(x, y, s); }
            s
        };
        let even = depth.is_multiple_of(2);
        let a = add(r[0], r[1], fl);
        let b = if even { add(r[3], r[2], fl) } else { add(r[2], r[3], fl) };
        let c = if even { add(r[5], r[4], fl) } else { add(r[4], r[5], fl) };
        let d = add(r[6], r[7], fl);
        let ab = add(a, b, fl);
        let cd = add(c, d, fl);
        let mut res = add(ab, cd, fl);
        while i < n {
            res = add(res, v[i], fl);
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        let x = pairwise::<T, TRACK>(&v[..n2], depth + 1, fl);
        let y = pairwise::<T, TRACK>(&v[n2..], depth + 1, fl);
        let s = if depth.is_multiple_of(2) { ad(x, y) } else { ad(y, x) };
        if TRACK { *fl |= flags(x, y, s); }
        s
    }
}

/// The problem: the operands' values (C order), the stack blocks' offsets, the sizes.
struct Mm<T> {
    a: Vec<T>,
    b: Vec<T>,
    /// per output stack position: the offset of its A (n, k) block and B (k, m) block
    blocks: Vec<(usize, usize)>,
    n: usize,
    k: usize,
    m: usize,
}

impl<T: Fl> Mm<T> {
    /// The products summed in the Python's order; TRACK: each ufunc call's flags (k <= 8: [mul0, -, mul1, add1, ..];
    /// k > 8: [mul, reduce]).
    fn run<const TRACK: bool>(&self, ops: &mut [u8]) -> Vec<T> {
        let (n, k, m) = (self.n, self.k, self.m);
        let mut out = Vec::with_capacity(self.blocks.len() * n * m);
        let mut prods: Vec<T> = if k > 8 && m == 1 { Vec::with_capacity(k) } else { Vec::new() };
        for &(oa, ob) in &self.blocks {
            let (a, b) = (&self.a[oa..oa + n * k], &self.b[ob..ob + k * m]);
            for i in 0..n {
                let ar = &a[i * k..i * k + k];
                for j in 0..m {
                    if k <= 8 {
                        let mut s = mu(ar[0], b[j]);
                        if TRACK { ops[0] |= flags(ar[0], b[j], s); }
                        for q in 1..k {
                            let (x, y) = (ar[q], b[q * m + j]);
                            let p = mu(x, y);
                            let t = ad(s, p);
                            if TRACK {
                                ops[2 * q] |= flags(x, y, p);
                                ops[2 * q + 1] |= flags(s, p, t);
                            }
                            s = t;
                        }
                        out.push(s);
                    } else if m == 1 {
                        prods.clear();
                        for q in 0..k {
                            let p = mu(ar[q], b[q]);
                            if TRACK { ops[0] |= flags(ar[q], b[q], p); }
                            prods.push(p);
                        }
                        let mut fl = 0u8;
                        let ps = pairwise::<T, TRACK>(&prods, 0, &mut fl);
                        let s = ad(T::ZERO, ps);
                        if TRACK { ops[1] |= fl | flags(T::ZERO, ps, s); }
                        out.push(s);
                    } else {
                        let mut s = T::ZERO;
                        for q in 0..k {
                            let (x, y) = (ar[q], b[q * m + j]);
                            let p = mu(x, y);
                            let t = ad(s, p);
                            if TRACK {
                                ops[0] |= flags(x, y, p);
                                ops[1] |= flags(s, p, t);
                            }
                            s = t;
                        }
                        out.push(s);
                    }
                }
            }
        }
        out
    }
}

/// u's values in C order, read at numpy's byte offsets (any strides, aligned or not).
fn values<T: Element + Copy>(u: &Bound<'_, PyUntypedArray>) -> Vec<T> {
    let shape = u.shape().to_vec();
    let strides = u.strides().to_vec();
    let total: usize = shape.iter().product();
    let mut out = Vec::with_capacity(total);
    if total == 0 {
        return out;
    }
    // SAFETY: as_array_ptr is the live array u holds (the GIL held, nothing called meanwhile); every offset read is
    // an element numpy's own indexing reads (its shape and byte strides), read unaligned.
    let base = unsafe { (*u.as_array_ptr()).fields.data } as *const u8;
    let isz = std::mem::size_of::<T>() as isize;
    let nd = shape.len();
    if u.is_c_contiguous() {
        for e in 0..total as isize {
            out.push(unsafe { std::ptr::read_unaligned(base.offset(e * isz) as *const T) });
        }
        return out;
    }
    let mut idx = vec![0usize; nd];
    let mut off: isize = 0;
    loop {
        out.push(unsafe { std::ptr::read_unaligned(base.offset(off) as *const T) });
        let mut d = nd;
        loop {
            if d == 0 {
                return out;
            }
            d -= 1;
            idx[d] += 1;
            off += strides[d];
            if idx[d] < shape[d] {
                break;
            }
            off -= strides[d] * shape[d] as isize;
            idx[d] = 0;
        }
    }
}

/// A shape as Python prints a tuple: (3,), (2, 3).
fn tuple(s: &[usize]) -> String {
    match s.len() {
        1 => format!("({},)", s[0]),
        _ => format!("({})", s.iter().map(|v| v.to_string()).collect::<Vec<_>>().join(", ")),
    }
}

fn contract(what: &str) -> PyErr {
    PyTypeError::new_err(format!("dm_matmul: {what} (the input contract: exact numpy arrays, both float64 or both float32)"))
}

/// numpy reports what the Python's ufunc calls would have: per call with flags, in the Python's order, a call of the
/// same ufunc on values that set exactly those flags.
fn report<T: Fl>(py: Python<'_>, ops: &[u8], k: usize) -> PyResult<()> {
    let np = py.import("numpy")?;
    for (i, &f) in ops.iter().enumerate() {
        if f == 0 {
            continue;
        }
        let (mut x, mut y) = (Vec::new(), Vec::new());
        let reduce = k > 8 && i == 1;
        let mul = if k > 8 { i == 0 } else { i % 2 == 0 };
        if f & OVER != 0 {      // (MAX * 2, MAX + MAX: overflow)
            x.push(T::BIG);
            y.push(if mul { T::TWO } else { T::BIG });
        }
        if f & INVALID != 0 {   // (inf * 0, inf + -inf: invalid)
            x.push(T::INF);
            y.push(if mul { T::ZERO } else { T::NEG_INF });
        }
        if reduce {
            let mut rows = x.clone();
            rows.extend_from_slice(&y);
            let arr = PyArray::from_vec(py, rows).reshape([2, x.len()])?;
            let kw = PyDict::new(py);
            kw.set_item("axis", 0)?;
            np.getattr("add")?.getattr("reduce")?.call((arr,), Some(&kw))?;
        } else {
            let name = if mul { "multiply" } else { "add" };
            np.getattr(name)?.call1((PyArray::from_vec(py, x), PyArray::from_vec(py, y)))?;
        }
    }
    Ok(())
}

fn compute<'py, T: Fl>(py: Python<'py>, ua: &Bound<'py, PyUntypedArray>, ub: &Bound<'py, PyUntypedArray>)
                       -> PyResult<Bound<'py, PyAny>> {
    let (sa0, sb0) = (ua.shape().to_vec(), ub.shape().to_vec());
    let (va, vb) = (sa0.len() == 1, sb0.len() == 1);
    let sa = if va { vec![1, sa0[0]] } else { sa0.clone() };
    let sb = if vb { vec![sb0[0], 1] } else { sb0.clone() };
    let (na, nb) = (sa.len(), sb.len());
    let (n, k) = (sa[na - 2], sa[na - 1]);
    let m = sb[nb - 1];
    if sb[nb - 2] != k {
        return Err(PyValueError::new_err(format!("matmul: shapes {} and {} not aligned", tuple(&sa), tuple(&sb))));
    }
    // the stacks broadcast (numpy's rules, right-aligned)
    let (ta, tb) = (&sa[..na - 2], &sb[..nb - 2]);
    let ns = ta.len().max(tb.len());
    let mut st = vec![0usize; ns];
    for d in 0..ns {
        let x = if d + ta.len() >= ns { ta[d + ta.len() - ns] } else { 1 };
        let y = if d + tb.len() >= ns { tb[d + tb.len() - ns] } else { 1 };
        st[d] = if x == y || y == 1 { x } else if x == 1 { y } else {
            return Err(PyValueError::new_err(format!(
                "matmul: the stacks of {} and {} do not broadcast", tuple(&sa), tuple(&sb))));
        };
    }
    let a: Vec<T> = values(ua);
    let b: Vec<T> = values(ub);
    let nblocks: usize = st.iter().product();
    let mut blocks = Vec::with_capacity(nblocks);
    let mut idx = vec![0usize; ns];
    for _ in 0..nblocks {
        let (mut oa, mut ob) = (0usize, 0usize);
        for d in 0..ns {
            if d + ta.len() >= ns {
                let dd = d + ta.len() - ns;
                oa = oa * ta[dd] + if ta[dd] == 1 { 0 } else { idx[d] };
            }
            if d + tb.len() >= ns {
                let dd = d + tb.len() - ns;
                ob = ob * tb[dd] + if tb[dd] == 1 { 0 } else { idx[d] };
            }
        }
        blocks.push((oa * n * k, ob * k * m));
        for d in (0..ns).rev() {
            idx[d] += 1;
            if idx[d] < st[d] {
                break;
            }
            idx[d] = 0;
        }
    }
    let mut shape = st.clone();
    if !va { shape.push(n); }
    if !vb { shape.push(m); }
    let out: Vec<T> = if k == 0 {
        vec![T::ZERO; nblocks * n * m]
    } else {
        let mm = Mm { a, b, blocks, n, k, m };
        let mut none = [0u8; 0];
        let out = if nblocks * n * m * k > DETACH_WORK {
            py.detach(|| mm.run::<false>(&mut none))
        } else {
            mm.run::<false>(&mut none)
        };
        if !out.iter().all(|v| v.finite()) {
            let mut ops = vec![0u8; if k <= 8 { 2 * k } else { 2 }];
            let _ = mm.run::<true>(&mut ops);
            report::<T>(py, &ops, k)?;
        }
        out
    };
    if shape.is_empty() {
        let d = numpy::dtype::<T>(py);
        let v = out[0];
        // SAFETY: PyArray_Scalar copies the value at &v (an element of d's type) into a new numpy scalar; base is
        // unused for a number type
        return unsafe {
            let p = PY_ARRAY_API.PyArray_Scalar(py, &v as *const T as *mut std::ffi::c_void, d.as_dtype_ptr(),
                                                std::ptr::null_mut());
            Bound::from_owned_ptr_or_err(py, p)
        };
    }
    // SAFETY: the new array is filled whole before it is returned
    let arr = unsafe { PyArrayDyn::<T>::new(py, numpy::IxDyn(&shape), false) };
    unsafe { arr.as_slice_mut()? }.copy_from_slice(&out);
    Ok(arr.into_any())
}

/// dnp.matmul(a, b) of the input contract's arrays: a numpy scalar for two 1-D operands, else a new C-ordered array.
#[pyfunction]
pub fn dm_matmul<'py>(py: Python<'py>, a: Bound<'py, PyAny>, b: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: the ndarray type object numpy exports, compared by address
    let nd = unsafe { npyffi::get_type_object(py, npyffi::NpyTypes::PyArray_Type) };
    if a.get_type().as_type_ptr() != nd || b.get_type().as_type_ptr() != nd {
        return Err(contract("an operand is no exact numpy.ndarray"));
    }
    let (ua, ub) = (a.cast::<PyUntypedArray>()?, b.cast::<PyUntypedArray>()?);
    if ua.ndim() == 0 || ub.ndim() == 0 {
        return Err(PyValueError::new_err("matmul: scalar operands are not allowed, use '*' instead"));
    }
    let (da, db) = (ua.dtype(), ub.dtype());
    let f64d = numpy::dtype::<f64>(py);
    if da.is_equiv_to(&f64d) && db.is_equiv_to(&f64d) {
        return compute::<f64>(py, ua, ub);
    }
    let f32d = numpy::dtype::<f32>(py);
    if da.is_equiv_to(&f32d) && db.is_equiv_to(&f32d) {
        return compute::<f32>(py, ua, ub);
    }
    Err(contract("the operands' dtypes"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pairwise_small_and_blocks() {
        let mut fl = 0u8;
        let v: Vec<f64> = (0..300).map(|i| (i as f64) * 0.1).collect();
        // exact sums of tenths: compare against a plain sum of the same values (within rounding) and the 8-block
        let p = pairwise::<f64, false>(&v, 0, &mut fl);
        assert!((p - 4485.0).abs() < 1e-9);
        assert_eq!(pairwise::<f64, false>(&[1.0, 2.0, 3.0], 0, &mut fl), 6.0);
        assert_eq!(flags(f64::MAX, f64::MAX, f64::MAX + f64::MAX), OVER);
        assert_eq!(flags(f64::INFINITY, 0.0, f64::INFINITY * 0.0), INVALID);
        assert_eq!(flags(f64::NAN, 1.0, f64::NAN), 0);
    }
}
