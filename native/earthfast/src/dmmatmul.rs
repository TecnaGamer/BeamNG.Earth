//! dnp.matmul / dnp.dot in Rust (earthfast 472, the full Rust port): numpy's matmul rules with the products summed in
//! the order the Python summed them, bit for bit (tests/test_dnp_matmul_native.py keeps that Python as the reference).
//! The Python was numpy array arithmetic, 229k small calls a cold visit (sidewalks, junction cuts, corners, building
//! areas), its cost the per-call array overhead.
//!
//! The order (A (S.., n, k) and B (S.., k, m) after a 1-D operand's promotion; every product A[.., i, q] * B[.., q, j]
//! rounded once, never fused):
//! - k <= 8: p0 + p1 + ... + p(k-1), left to right (the Python's column loop: out = out + a[.., j] * b[j, ..]);
//! - k > 8: np.add.reduce(a[.., :, :, None] * b[.., None, :, :], axis=-2), whose loop order follows the operands'
//!   memory layout (see k_inner): where the k axis is the reduction's inner loop, each output element is 0.0 +
//!   numpy's pairwise sum of p0 .. p(k-1) (DOUBLE_pairwise_sum: 8 accumulators to 128, halves above); where k is an
//!   outer loop, 0.0 + p0 + p1 + ... left to right (one elementwise add per q). C-ordered operands: pairwise for
//!   m == 1, left to right for m > 1; a Fortran-ordered (n, k) a with m == 1, or a vector times a Fortran-ordered
//!   (k, m) b, the other way round.
//!
//! NaNs: where a NaN operand meets another number or NaN, the NaN kept is the one numpy's x86-64 loops kept (ad / mu:
//! the ufunc's first operand's, quieted; the pairwise sum's tree as its compiled code ordered it, see pairwise), so a
//! NaN's sign and payload are the Python's too. One exception, a documented difference: two different NaNs meeting
//! in an elementwise loop (the k <= 8 columns, a left-to-right k > 8 sum, the multiply) where numpy ran its SIMD tail
//! or a broadcast-scalar loop (inner loops longer than two vectors on non-C layouts or long m): those compiled loops
//! keep the other operand's NaN, and where the SIMD body ends depends on the CPU's vector width (AVX2 / AVX-512), so
//! the Python's own result was the machine's. The kernel keeps its one rule (deterministic); the server's calls make
//! no non-finite results at all (the online cold caches, every call).
//!
//! numpy's floating-point errors: the Python's ufunc calls (each column's multiply and add; the k > 8 multiply and
//! reduce) set the overflow / invalid flags, reported as np.errstate says (a warning by default; FloatingPointError
//! under "raise"). The kernel tracks which of those calls would have set which flag and, where any did, has numpy
//! report the same flags for the same calls in the same order (a tiny np.multiply / np.add / np.add.reduce that sets
//! exactly them), so errstate acts as before. Not tracked (accepted differences, Codex review of r2a6): underflow
//! (np.errstate(under=...) is never set in the server and numpy's default ignores it) and signalling NaNs (the server
//! makes none; the kernel quiets them as the hardware does, without numpy's invalid flag).
//!
//! Input contract: a and b exact numpy.ndarray (no subclass), both native float64 or both native float32, of 1+ axes;
//! anything else TypeError (dnp.py's glue makes them: np.asarray, the result dtype, integers to np.matmul). float16,
//! longdouble and complex operands are refused (TypeError) by the glue: a contract change from the Python (which
//! summed them in numpy), accepted as the server's calls are float64 only. sa / sb: the strides of the operands as the
//! Python saw them, where the glue cast one (the cast copy's layout can differ; only the order of the sum reads
//! them). Empty outputs and k == 0 (zeros) come from numpy before any operand is copied; otherwise the operands'
//! values are copied into Rust-owned storage (any strides) before anything else is called. Sizes are checked:
//! a result too big is numpy's ValueError / MemoryError, never an abort.

use numpy::npyffi::{self, PY_ARRAY_API};
use numpy::{Element, PyArray, PyArrayDescrMethods, PyArrayDyn, PyArrayMethods, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::exceptions::{PyMemoryError, PyTypeError, PyValueError};
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

/// The Python's k > 8 order: is k the inner loop of np.add.reduce(A' * B', axis=-2)? dims: the products' shape (C
/// order: S.., n, k, m); sa / sb: A' = a[.., :, :, None] and B' = b[.., None, :, :] as numpy's iterator holds them
/// broadcast to dims (byte strides, 0 where the operand's axis is 1 or missing).
///
/// The multiply allocates the products in its iteration order (order K; nditer_constr.c,
/// npyiter_find_best_axis_ordering): a stable insertion sort of the iterator's axes, the last C axis first, an axis
/// moving inside another only where every operand with nonzero strides on both has a strictly smaller |stride| on it
/// (an operand with a zero stride on either has no say; no say at all: keep looking further out); an allocated output
/// never flips a negative stride. The reduce (DONT_NEGATE_STRIDES, its own output unallocated) orders the products'
/// axes by their strides, so its inner loop is the multiply's innermost axis of more than one element: numpy's add
/// loop sees the reduction there (pairwise sum, IS_BINARY_REDUCE) only where that axis is k. No buffering applies
/// (one dtype, aligned), and the inner loop is the whole of k (checked past 8192, numpy's buffer size).
fn k_inner(dims: &[usize], sa: &[isize], sb: &[isize]) -> bool {
    let nd = dims.len();
    let st = |ax: usize| -> [isize; 2] { [sa[nd - 1 - ax], sb[nd - 1 - ax]] };
    let mut perm: Vec<usize> = (0..nd).collect();
    for i0 in 1..nd {
        let s0 = st(perm[i0]);
        let mut ipos = i0;
        for i1 in (0..i0).rev() {
            let s1 = st(perm[i1]);
            let (mut ambig, mut swap) = (true, false);
            for o in 0..2 {
                if s0[o] != 0 && s1[o] != 0 {
                    if s1[o].unsigned_abs() <= s0[o].unsigned_abs() {
                        swap = false;
                    } else if ambig {
                        swap = true;
                    }
                    ambig = false;
                }
            }
            if !ambig {
                if swap {
                    ipos = i1;
                } else {
                    break;
                }
            }
        }
        perm[ipos..=i0].rotate_right(1);
    }
    perm.iter().map(|&ax| nd - 1 - ax).find(|&c| dims[c] > 1).is_none_or(|c| c == nd - 2)
}

/// An operand's iterator strides broadcast to nd axes (right-aligned): 0 where its axis is 1 or missing.
fn iter_strides(shape: &[usize], strides: &[isize], nd: usize) -> Vec<isize> {
    let mut out = vec![0isize; nd];
    for (d, (&s, &x)) in shape.iter().zip(strides).enumerate() {
        if s != 1 {
            out[nd - shape.len() + d] = x;
        }
    }
    out
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
    /// k > 8: the pairwise sum over k (k the reduce's inner loop), else left to right
    pw: bool,
}

impl<T: Fl> Mm<T> {
    /// The products summed in the Python's order into out (reserved whole by the caller); TRACK: only each ufunc
    /// call's flags (k <= 8: [mul0, -, mul1, add1, ..]; k > 8: [mul, reduce]), out untouched. prods: the pairwise
    /// sum's scratch (k reserved by the caller where pw).
    fn run<const TRACK: bool>(&self, ops: &mut [u8], out: &mut Vec<T>, prods: &mut Vec<T>) {
        let (n, k, m) = (self.n, self.k, self.m);
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
                        if !TRACK { out.push(s); }
                    } else if self.pw {
                        prods.clear();
                        for q in 0..k {
                            let (x, y) = (ar[q], b[q * m + j]);
                            let p = mu(x, y);
                            if TRACK { ops[0] |= flags(x, y, p); }
                            prods.push(p);
                        }
                        let mut fl = 0u8;
                        let ps = pairwise::<T, TRACK>(prods, 0, &mut fl);
                        let s = ad(T::ZERO, ps);
                        if TRACK { ops[1] |= fl | flags(T::ZERO, ps, s); } else { out.push(s); }
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
                        if !TRACK { out.push(s); }
                    }
                }
            }
        }
    }
}

/// u's values in C order, read at numpy's byte offsets (any strides, aligned or not).
fn values<T: Element + Copy>(u: &Bound<'_, PyUntypedArray>) -> PyResult<Vec<T>> {
    let shape = u.shape().to_vec();
    let strides = u.strides().to_vec();
    let total: usize = shape.iter().product();      // (a live array's size: numpy checked it)
    let mut out = Vec::new();
    out.try_reserve_exact(total).map_err(|_| PyMemoryError::new_err("dm_matmul: no memory for an operand's copy"))?;
    if total == 0 {
        return Ok(out);
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
        return Ok(out);
    }
    let mut idx = vec![0usize; nd];
    let mut off: isize = 0;
    loop {
        out.push(unsafe { std::ptr::read_unaligned(base.offset(off) as *const T) });
        let mut d = nd;
        loop {
            if d == 0 {
                return Ok(out);
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

/// A new array of shape (C order) by numpy (PyArray_Zeros / PyArray_Empty: numpy checks the size and raises its own
/// ValueError / MemoryError).
fn new_array<'py, T: Fl>(py: Python<'py>, shape: &[usize], zero: bool) -> PyResult<Bound<'py, PyArrayDyn<T>>> {
    let mut dims: Vec<npyffi::npy_intp> = shape.iter().map(|&s| s as npyffi::npy_intp).collect();
    let d = numpy::dtype::<T>(py).into_dtype_ptr();
    // SAFETY: PyArray_Zeros / PyArray_Empty steal the descriptor's reference (into_dtype_ptr gave one) and read
    // shape.len() dims; a NULL result is numpy's error, raised; the result is an ndarray of T's dtype
    unsafe {
        let p = if zero {
            PY_ARRAY_API.PyArray_Zeros(py, dims.len() as std::ffi::c_int, dims.as_mut_ptr(), d, 0)
        } else {
            PY_ARRAY_API.PyArray_Empty(py, dims.len() as std::ffi::c_int, dims.as_mut_ptr(), d, 0)
        };
        Ok(Bound::from_owned_ptr_or_err(py, p)?.cast_into_unchecked())
    }
}

/// v as a numpy scalar of T's dtype.
fn scalar<'py, T: Fl>(py: Python<'py>, v: T) -> PyResult<Bound<'py, PyAny>> {
    let d = numpy::dtype::<T>(py);
    // SAFETY: PyArray_Scalar copies the value at &v (an element of d's type) into a new numpy scalar; base is unused
    // for a number type
    unsafe {
        let p = PY_ARRAY_API.PyArray_Scalar(py, &v as *const T as *mut std::ffi::c_void, d.as_dtype_ptr(),
                                            std::ptr::null_mut());
        Bound::from_owned_ptr_or_err(py, p)
    }
}

/// The operand's strides as the Python saw them: the glue's (a cast operand's original ones) or the array's own.
fn strides_of(u: &Bound<'_, PyUntypedArray>, given: Option<Vec<isize>>, what: &str) -> PyResult<Vec<isize>> {
    match given {
        None => Ok(u.strides().to_vec()),
        Some(s) if s.len() == u.ndim() => Ok(s),
        Some(_) => Err(contract(&format!("{what}: one stride per axis"))),
    }
}

fn compute<'py, T: Fl>(py: Python<'py>, ua: &Bound<'py, PyUntypedArray>, ub: &Bound<'py, PyUntypedArray>,
                       ra: Vec<isize>, rb: Vec<isize>) -> PyResult<Bound<'py, PyAny>> {
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
    let mut shape = st.clone();
    if !va { shape.push(n); }
    if !vb { shape.push(m); }
    // numpy's size rule (PyArray_NewFromDescr): the product of the nonzero axes times the item size within npy_intp,
    // an empty array's too, else ValueError. The Python's k > 8 path made the (S.., n, k, m) products first.
    let too_big = || PyValueError::new_err(
        "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.");
    let fits = |dims: &[usize]| {
        dims.iter().filter(|&&s| s != 0).try_fold(std::mem::size_of::<T>(), |acc, &s| acc.checked_mul(s))
            .is_some_and(|bytes| bytes <= isize::MAX as usize)
    };
    if k > 8 && !fits(&[st.as_slice(), &[n, k, m]].concat()) {
        return Err(too_big());
    }
    // an empty result, or k == 0 (zeros): numpy's (its own size check), before any copy or block table (the stacks'
    // product can be far beyond memory where another axis is 0)
    if k == 0 || n == 0 || m == 0 || st.contains(&0) {
        if shape.is_empty() {
            return scalar::<T>(py, T::ZERO);
        }
        return Ok(new_array::<T>(py, &shape, true)?.into_any());
    }
    if !fits(&shape) {
        return Err(too_big());
    }
    let total: usize = shape.iter().product();      // (no zero axis: within isize, checked above)
    let nblocks = total / (n * m);
    let a: Vec<T> = values(ua)?;
    let b: Vec<T> = values(ub)?;
    let mut blocks = Vec::new();
    blocks.try_reserve_exact(nblocks).map_err(|_| PyMemoryError::new_err("dm_matmul: no memory for the result"))?;
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
        blocks.push((oa * n * k, ob * k * m));      // (offsets within the operands' copies: no overflow)
        for d in (0..ns).rev() {
            idx[d] += 1;
            if idx[d] < st[d] {
                break;
            }
            idx[d] = 0;
        }
    }
    let pw = k > 8 && {
        // A' = a[.., :, :, None] and B' = b[.., None, :, :] (a 1-D operand promoted first) over (S.., n, k, m)
        let nd = ns + 3;
        let (mut sha, mut sta) = if va { (vec![1, k], vec![0, ra[0]]) } else { (sa0.clone(), ra) };
        sha.push(1);
        sta.push(0);
        let (mut shb, mut stb) = if vb { (vec![k, 1], vec![rb[0], 0]) } else { (sb0.clone(), rb) };
        shb.insert(shb.len() - 2, 1);
        stb.insert(stb.len() - 2, 0);
        let mut dims = st.clone();
        dims.extend_from_slice(&[n, k, m]);
        k_inner(&dims, &iter_strides(&sha, &sta, nd), &iter_strides(&shb, &stb, nd))
    };
    let mm = Mm { a, b, blocks, n, k, m, pw };
    let nomem = || PyMemoryError::new_err("dm_matmul: no memory for the result");
    let (mut out, mut prods) = (Vec::new(), Vec::new());
    out.try_reserve_exact(total).map_err(|_| nomem())?;
    if pw {
        prods.try_reserve_exact(k).map_err(|_| nomem())?;
    }
    let mut none = [0u8; 0];
    if total.saturating_mul(k) > DETACH_WORK {
        py.detach(|| mm.run::<false>(&mut none, &mut out, &mut prods));
    } else {
        mm.run::<false>(&mut none, &mut out, &mut prods);
    }
    if !out.iter().all(|v| v.finite()) {
        let mut ops = vec![0u8; if k <= 8 { 2 * k } else { 2 }];
        mm.run::<true>(&mut ops, &mut Vec::new(), &mut prods);
        report::<T>(py, &ops, k)?;
    }
    if shape.is_empty() {
        return scalar::<T>(py, out[0]);
    }
    let arr = new_array::<T>(py, &shape, false)?;
    // SAFETY: the new array is C-contiguous, held only here, and filled whole before it is returned
    unsafe { arr.as_slice_mut()? }.copy_from_slice(&out);
    Ok(arr.into_any())
}

/// dnp.matmul(a, b) of the input contract's arrays: a numpy scalar for two 1-D operands, else a new C-ordered array.
/// sa / sb: an operand's strides as the Python saw them, where the glue cast it (the order of a k > 8 sum follows
/// them); by default the array's own.
#[pyfunction]
#[pyo3(signature = (a, b, sa=None, sb=None))]
pub fn dm_matmul<'py>(py: Python<'py>, a: Bound<'py, PyAny>, b: Bound<'py, PyAny>, sa: Option<Vec<isize>>,
                      sb: Option<Vec<isize>>) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: the ndarray type object numpy exports, compared by address
    let nd = unsafe { npyffi::get_type_object(py, npyffi::NpyTypes::PyArray_Type) };
    if a.get_type().as_type_ptr() != nd || b.get_type().as_type_ptr() != nd {
        return Err(contract("an operand is no exact numpy.ndarray"));
    }
    let (ua, ub) = (a.cast::<PyUntypedArray>()?, b.cast::<PyUntypedArray>()?);
    if ua.ndim() == 0 || ub.ndim() == 0 {
        return Err(PyValueError::new_err("matmul: scalar operands are not allowed, use '*' instead"));
    }
    let (ra, rb) = (strides_of(ua, sa, "sa")?, strides_of(ub, sb, "sb")?);
    let (da, db) = (ua.dtype(), ub.dtype());
    let f64d = numpy::dtype::<f64>(py);
    if da.is_equiv_to(&f64d) && db.is_equiv_to(&f64d) {
        return compute::<f64>(py, ua, ub, ra, rb);
    }
    let f32d = numpy::dtype::<f32>(py);
    if da.is_equiv_to(&f32d) && db.is_equiv_to(&f32d) {
        return compute::<f32>(py, ua, ub, ra, rb);
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
