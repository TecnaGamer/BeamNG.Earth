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
//! values are copied into Rust-owned storage (any strides) before anything else is called, each distinct element
//! once (a stride-0, broadcast axis is kept as one element with a zero stride), and the sums are written straight
//! into the new result array (no second copy of the result): the memory used is the result's plus the operands'
//! distinct values (Codex review: a broadcast (10^6, 1, 1) operand). Sizes are checked: a result too big is numpy's
//! ValueError / MemoryError, never an abort.

use numpy::npyffi::{self, PY_ARRAY_API};
use numpy::{Element, PyArray, PyArrayDescrMethods, PyArrayDyn, PyArrayMethods, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::exceptions::{PyMemoryError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::mem::MaybeUninit;

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

/// numpy's pairwise sum (loops_utils.h.src, @TYPE@_pairwise_sum) of v(lo) .. v(lo + n - 1), the reduce loop's own
/// (v called once per element, in increasing order: the matmul's products made as the sum reads them, no k-long
/// scratch): below 8 one by one from -0.0 (not reached from matmul: k > 8 here, the halves 64 or more), to 128 eight
/// accumulators, above that the two halves (the first a multiple of 8). TRACK: the flags of every add. depth: the recursion's (0 the reduce loop's call): the operands' order where two NaNs meet is the
/// compiled wheel's (numpy 2.5's x86-64 build: the function inlined into itself once, so the two code copies
/// alternate by depth: at an even depth r3 + r2 and r5 + r4 in the accumulators' tree and the halves first + second,
/// at an odd depth r2 + r3, r4 + r5 and second + first; fitted on every NaN pair placement, tests/test_dnp_matmul_native.py).
fn pairwise<T: Fl, const TRACK: bool, F: FnMut(usize) -> T>(v: &mut F, lo: usize, n: usize, depth: u32, fl: &mut u8)
                                                                -> T {
    if n < 8 {
        let mut res = T::NEG_ZERO;
        for i in 0..n {
            let x = v(lo + i);
            let r = ad(res, x);
            if TRACK { *fl |= flags(res, x, r); }
            res = r;
        }
        res
    } else if n <= PW_BLOCKSIZE {
        let mut r = [T::ZERO; 8];
        for (j, x) in r.iter_mut().enumerate() {
            *x = v(lo + j);
        }
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, rj) in r.iter_mut().enumerate() {
                let x = v(lo + i + j);
                let s = ad(*rj, x);
                if TRACK { *fl |= flags(*rj, x, s); }
                *rj = s;
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
            res = add(res, v(lo + i), fl);
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        let x = pairwise::<T, TRACK, F>(v, lo, n2, depth + 1, fl);
        let y = pairwise::<T, TRACK, F>(v, lo + n2, n - n2, depth + 1, fl);
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

/// The problem: the operands' distinct values, the element strides into them, the sizes.
struct Mm<T> {
    a: Vec<T>,
    b: Vec<T>,
    /// the stacks' broadcast shape, and per stack axis a's and b's element strides (0 where broadcast)
    st: Vec<usize>,
    sta: Vec<usize>,
    stb: Vec<usize>,
    /// element strides of a's n and k axes and b's k and m axes (0 where broadcast)
    an: usize,
    ak: usize,
    bk: usize,
    bm: usize,
    n: usize,
    k: usize,
    m: usize,
    /// k > 8: the pairwise sum over k (k the reduce's inner loop), else left to right
    pw: bool,
}

impl<T: Fl> Mm<T> {
    /// The products summed in the Python's order into out (the result in C order, written whole); TRACK: only each
    /// ufunc call's flags (k <= 8: [mul0, -, mul1, add1, ..]; k > 8: [mul, reduce]), out untouched.
    fn run<const TRACK: bool>(&self, ops: &mut [u8], out: &mut [MaybeUninit<T>]) {
        let (n, k, m) = (self.n, self.k, self.m);
        let (a, b) = (&self.a[..], &self.b[..]);
        let (an, ak, bk, bm) = (self.an, self.ak, self.bk, self.bm);
        let ns = self.st.len();
        let nblocks: usize = self.st.iter().product();
        let mut idx = vec![0usize; ns];
        let (mut oa, mut ob, mut pos) = (0usize, 0usize, 0usize);
        for _ in 0..nblocks {
            // every index read below is at most the block's last element (offsets and strides >= 0), checked here
            // once: the reads unchecked (the hot loop of 229k small calls)
            assert!(oa + (n - 1) * an + (k - 1) * ak < a.len() && ob + (k - 1) * bk + (m - 1) * bm < b.len());
            assert!(TRACK || pos + n * m <= out.len());
            // SAFETY: ix within the block's elements, in bounds by the asserts above
            let ga = |ix: usize| unsafe { *a.get_unchecked(ix) };
            let gb = |ix: usize| unsafe { *b.get_unchecked(ix) };
            if k <= 8 {
                // the Python's column loop, an output column at a time: b's column read once (each sum, its order
                // and its flags are the same in any output order)
                for j in 0..m {
                    let bj = ob + j * bm;
                    let mut bc = [T::ZERO; 8];
                    for (q, v) in bc.iter_mut().enumerate().take(k) {
                        *v = gb(bj + q * bk);
                    }
                    for i in 0..n {
                        let ai = oa + i * an;
                        let (x, y) = (ga(ai), bc[0]);
                        let mut s = mu(x, y);
                        if TRACK { ops[0] |= flags(x, y, s); }
                        for (q, &y) in bc.iter().enumerate().take(k).skip(1) {
                            let x = ga(ai + q * ak);
                            let p = mu(x, y);
                            let t = ad(s, p);
                            if TRACK {
                                ops[2 * q] |= flags(x, y, p);
                                ops[2 * q + 1] |= flags(s, p, t);
                            }
                            s = t;
                        }
                        if !TRACK {
                            // SAFETY: one of the block's n * m outputs from pos, within out by the assert above
                            unsafe { out.get_unchecked_mut(pos + i * m + j).write(s); }
                        }
                    }
                }
                pos += n * m;
            } else {
                for i in 0..n {
                    let ai = oa + i * an;
                    for j in 0..m {
                        let bj = ob + j * bm;
                        let s = if self.pw {
                            let mut fm = 0u8;
                            let mut prod = |q: usize| {
                                let (x, y) = (ga(ai + q * ak), gb(bj + q * bk));
                                let p = mu(x, y);
                                if TRACK { fm |= flags(x, y, p); }
                                p
                            };
                            let mut fl = 0u8;
                            let ps = pairwise::<T, TRACK, _>(&mut prod, 0, k, 0, &mut fl);
                            let s = ad(T::ZERO, ps);
                            if TRACK {
                                ops[0] |= fm;
                                ops[1] |= fl | flags(T::ZERO, ps, s);
                            }
                            s
                        } else {
                            let mut s = T::ZERO;
                            for q in 0..k {
                                let (x, y) = (ga(ai + q * ak), gb(bj + q * bk));
                                let p = mu(x, y);
                                let t = ad(s, p);
                                if TRACK {
                                    ops[0] |= flags(x, y, p);
                                    ops[1] |= flags(s, p, t);
                                }
                                s = t;
                            }
                            s
                        };
                        if !TRACK {
                            // SAFETY: pos < the block's end, within out by the assert above
                            unsafe { out.get_unchecked_mut(pos).write(s); }
                        }
                        pos += 1;
                    }
                }
            }
            // the next stack position (C order): the blocks' offsets follow it, no table
            for d in (0..ns).rev() {
                idx[d] += 1;
                oa += self.sta[d];
                ob += self.stb[d];
                if idx[d] < self.st[d] {
                    break;
                }
                oa -= self.sta[d] * self.st[d];
                ob -= self.stb[d] * self.st[d];
                idx[d] = 0;
            }
        }
    }
}

/// An operand's distinct values (C order over its axes, an axis of numpy stride 0 held as one element) and per axis
/// the element stride into them (0 where the axis is broadcast or has one element).
struct Op<T> {
    v: Vec<T>,
    st: Vec<usize>,
}

/// u's distinct values, read at numpy's byte offsets (any strides, aligned or not): a stride-0 axis read once.
fn values<T: Element + Copy>(u: &Bound<'_, PyUntypedArray>) -> PyResult<Op<T>> {
    let (shape, strides) = (u.shape(), u.strides());
    let nd = shape.len();
    // the distinct shape: a stride-0 axis of one element (an empty one stays empty)
    let ds = |d: usize| if strides[d] == 0 { shape[d].min(1) } else { shape[d] };
    let mut st = vec![0usize; nd];
    let mut total = 1usize;                         // (at most the live array's size: numpy checked it)
    for d in (0..nd).rev() {
        st[d] = if ds(d) > 1 { total } else { 0 };
        total *= ds(d);
    }
    let mut out = Vec::new();
    out.try_reserve_exact(total).map_err(|_| PyMemoryError::new_err("dm_matmul: no memory for an operand's copy"))?;
    if total == 0 {
        return Ok(Op { v: out, st });
    }
    // SAFETY: as_array_ptr is the live array u holds (the GIL held, nothing called meanwhile); every offset read is
    // an element numpy's own indexing reads (its byte strides at an index within its shape), read unaligned.
    let base = unsafe { (*u.as_array_ptr()).fields.data } as *const u8;
    let isz = std::mem::size_of::<T>() as isize;
    if u.is_c_contiguous() {
        // (no stride-0 axis of more than one element: the distinct values are all of them)
        for e in 0..total as isize {
            out.push(unsafe { std::ptr::read_unaligned(base.offset(e * isz) as *const T) });
        }
        return Ok(Op { v: out, st });
    }
    let mut idx = vec![0usize; nd];
    let mut off: isize = 0;
    loop {
        out.push(unsafe { std::ptr::read_unaligned(base.offset(off) as *const T) });
        let mut d = nd;
        loop {
            if d == 0 {
                return Ok(Op { v: out, st });
            }
            d -= 1;
            idx[d] += 1;
            off += strides[d];
            if idx[d] < ds(d) {
                break;
            }
            off -= strides[d] * ds(d) as isize;
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
    let (oa, ob) = (values::<T>(ua)?, values::<T>(ub)?);
    // the element strides of the promoted operands' axes, a stack axis right-aligned to the broadcast stacks
    let (an, ak) = if va { (0, oa.st[0]) } else { (oa.st[na - 2], oa.st[na - 1]) };
    let (bk, bm) = if vb { (ob.st[0], 0) } else { (ob.st[nb - 2], ob.st[nb - 1]) };
    let stack = |t: &[usize], s: &[usize]| -> Vec<usize> {
        (0..ns).map(|d| if d + t.len() >= ns { s[d + t.len() - ns] } else { 0 }).collect()
    };
    let (sta, stb) = (stack(ta, &oa.st), stack(tb, &ob.st));
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
    let mm = Mm { a: oa.v, b: ob.v, st, sta, stb, an, ak, bk, bm, n, k, m, pw };
    // the sums into out (the GIL released for big work), then numpy reports the flags the Python's calls set; out
    // whole afterwards (run writes every element), returned initialised
    let fill = |out: &mut [MaybeUninit<T>]| -> PyResult<&[T]> {
        let mut none = [0u8; 0];
        if total.saturating_mul(k) > DETACH_WORK {
            py.detach(|| mm.run::<false>(&mut none, out));
        } else {
            mm.run::<false>(&mut none, out);
        }
        // SAFETY: run::<false> wrote every one of out's elements (the blocks' n * m outputs each, in turn)
        let done = unsafe { std::slice::from_raw_parts(out.as_ptr() as *const T, out.len()) };
        if !done.iter().all(|v| v.finite()) {
            let mut ops = vec![0u8; if k <= 8 { 2 * k } else { 2 }];
            mm.run::<true>(&mut ops, &mut []);
            report::<T>(py, &ops, k)?;
        }
        Ok(done)
    };
    if shape.is_empty() {
        let mut out = [MaybeUninit::<T>::uninit()];
        let v = fill(&mut out)?[0];
        return scalar::<T>(py, v);
    }
    let arr = new_array::<T>(py, &shape, false)?;
    // SAFETY: the new array's data: C-contiguous and aligned, of total elements, held only here (no Python object
    // refers to it, so nothing reads or writes it while the GIL is released), its memory not yet initialised (viewed
    // as MaybeUninit until written whole); dropped unreturned where report raises
    let out = unsafe {
        std::slice::from_raw_parts_mut((*arr.as_array_ptr()).fields.data as *mut MaybeUninit<T>, total)
    };
    fill(out)?;
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
        let p = pairwise::<f64, false, _>(&mut |i| v[i], 0, v.len(), 0, &mut fl);
        assert!((p - 4485.0).abs() < 1e-9);
        assert_eq!(pairwise::<f64, false, _>(&mut |i| [1.0, 2.0, 3.0][i], 0, 3, 0, &mut fl), 6.0);
        assert_eq!(flags(f64::MAX, f64::MAX, f64::MAX + f64::MAX), OVER);
        assert_eq!(flags(f64::INFINITY, 0.0, f64::INFINITY * 0.0), INVALID);
        assert_eq!(flags(f64::NAN, 1.0, f64::NAN), 0);
    }
}
