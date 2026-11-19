//! railways.drawn and railways.wear for a whole layer at once (the full port): which features are built railway track
//! drawn in 3D, with which class and wear. Asked ~640k times a Manhattan visit, feature by feature in Python (every node
//! window of railprofile's lattice and every tile asking the same cells' lines again); now one pass a layer, the glue
//! reading the columns. The strings arrive as UCS-4 rows (fast.ucs4: each value's str, NUL padded; a value's trailing
//! NULs are numpy's own padding, gone from its str too).
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// The classes drawn() returns, by code (railways.DRAWN, then "tram" for bridges=True).
pub const CLASSES: [&str; 4] = ["standard_gauge", "narrow_gauge", "light_rail", "tram"];
const TRAM: i8 = 3;

/// OSM railway values never drawn (railways.OSM_RAILWAY_NOT; "historic" too with BNE_RAIL_LIFECYCLE).
const OSM_NOT: [&str; 11] = ["proposed", "construction", "razed", "dismantled", "planned", "subway", "tram", "monorail",
                             "funicular", "miniature", "platform"];
/// OSM railway values drawn with this wear (railways.OSM_RAILWAY_WEAR).
const OSM_WEAR: [(&str, i8); 6] = [("rail", 0), ("light_rail", 0), ("narrow_gauge", 0), ("preserved", 0),
                                   ("disused", 1), ("abandoned", 2)];

/// A UCS-4 column: its code points (rows of w) as one C-contiguous slice.
struct Col<'a> {
    s: &'a [u32],
    w: usize,
}

impl<'a> Col<'a> {
    fn new(a: &'a PyReadonlyArray2<'_, u32>, n: usize, what: &str) -> PyResult<Self> {
        let v = a.as_array();
        if v.nrows() != n {
            return Err(PyValueError::new_err(format!("rail_drawn_cols: {what} needs a row per feature")));
        }
        let w = v.ncols();
        let s = a.as_slice().map_err(|_| PyValueError::new_err("rail_drawn_cols: UCS-4 rows must be contiguous"))?;
        Ok(Col { s, w })
    }

    /// Row i without its NUL padding.
    fn row(&self, i: usize) -> &'a [u32] {
        let r = &self.s[i * self.w..(i + 1) * self.w];
        let mut e = r.len();
        while e > 0 && r[e - 1] == 0 {
            e -= 1;
        }
        &r[..e]
    }
}

/// r == s for an ASCII s.
fn eq(r: &[u32], s: &str) -> bool {
    r.len() == s.len() && r.iter().zip(s.bytes()).all(|(&a, b)| a == u32::from(b))
}

/// Python's `s in r` for an ASCII s (not empty).
fn contains(r: &[u32], s: &str) -> bool {
    r.len() >= s.len() && r.windows(s.len()).any(|x| eq(x, s))
}

fn class_code(c: &[u32]) -> Option<i8> {
    CLASSES.iter().position(|k| eq(c, k)).map(|k| k as i8)
}

/// Python's builtin sum() of floats, started from the int 0 (CPython 3.12+: Neumaier's compensated sum, the
/// compensation added at the end when it is finite and not zero). Only its comparison with 0.5 is read.
#[derive(Clone, Copy, Default)]
struct PySum {
    started: bool,
    s: f64,
    c: f64,
}

impl PySum {
    fn add(&mut self, x: f64) {
        if !self.started {
            // (0 + x: the int start and the first float)
            self.started = true;
            self.s = 0.0 + x;
            return;
        }
        let t = self.s + x;
        if abs(self.s) >= abs(x) {
            self.c += (self.s - t) + x;
        } else {
            self.c += (x - t) + self.s;
        }
        self.s = t;
    }

    fn over_half(&self) -> bool {
        if !self.started {
            return false;
        }
        let v = if self.c != 0.0 && self.c.is_finite() { self.s + self.c } else { self.s };
        v > 0.5
    }
}

fn abs(x: f64) -> f64 {
    f64::from_bits(x.to_bits() & !(1u64 << 63))
}

/// A flags rule table: feature, start, end, value.
type FlagCols<'a> = (&'a [i64], &'a [f64], &'a [f64], &'a Col<'a>);

/// Each feature's wear (railways.wear): 0 in use, 1 disused, 2 abandoned, -1 not built yet (a flag's rows covering
/// over half of it: each matching row's end - start, summed in row order as Python's sum() of them).
fn wears(n: usize, flags: Option<FlagCols>) -> Vec<i8> {
    let mut out = vec![0i8; n];
    let Some((feature, start, end, value)) = flags else { return out };
    if feature.is_empty() {
        return out;
    }
    let mut sums = vec![[PySum::default(); 3]; n];        // unbuilt, abandoned, disused
    for (j, &f) in feature.iter().enumerate() {
        if f < 0 || f as u64 >= n as u64 {
            continue;
        }
        let v = value.row(j);
        let len = end[j] - start[j];
        let s = &mut sums[f as usize];
        for (k, flag) in ["is_under_construction", "is_abandoned", "is_disused"].iter().enumerate() {
            if contains(v, flag) {
                s[k].add(len);
            }
        }
    }
    for (o, s) in out.iter_mut().zip(&sums) {
        *o = if s[0].over_half() { -1 } else if s[1].over_half() { 2 } else if s[2].over_half() { 1 } else { 0 };
    }
    out
}

type Flags<'py> = (PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>,
                   PyReadonlyArray2<'py, u32>);

/// railways.drawn(feats, i, bridges) for every feature i of a layer of n: (plain, bridges, wear), int8 each: the class
/// code (CLASSES; -1 not drawn) for bridges=False and for bridges=True, and the wear drawn() returns with it. The
/// columns (UCS-4 rows of str(v or "")) are None where the layer has no such attribute; flags its "flags" rule table
/// (feature, start, end as float64, value) or None; historic_not: "historic" is not drawn either (BNE_RAIL_LIFECYCLE).
#[pyfunction]
#[pyo3(signature = (n, subtype, cls, osm_railway, osm_highway, flags, historic_not))]
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
pub fn rail_drawn_cols<'py>(py: Python<'py>, n: usize, subtype: Option<PyReadonlyArray2<'py, u32>>,
                            cls: Option<PyReadonlyArray2<'py, u32>>, osm_railway: Option<PyReadonlyArray2<'py, u32>>,
                            osm_highway: Option<PyReadonlyArray2<'py, u32>>, flags: Option<Flags<'py>>,
                            historic_not: bool)
                            -> PyResult<(Bound<'py, PyArray1<i8>>, Bound<'py, PyArray1<i8>>, Bound<'py, PyArray1<i8>>)> {
    let sub = subtype.as_ref().map(|a| Col::new(a, n, "subtype")).transpose()?;
    let cls = cls.as_ref().map(|a| Col::new(a, n, "class")).transpose()?;
    let osm = osm_railway.as_ref().map(|a| Col::new(a, n, "osm_railway")).transpose()?;
    let hw = osm_highway.as_ref().map(|a| Col::new(a, n, "osm_highway")).transpose()?;
    let fl = match &flags {
        None => None,
        Some((f, s, e, v)) => {
            let m = f.len()?;
            if s.len()? != m || e.len()? != m || v.as_array().nrows() != m {
                return Err(PyValueError::new_err("rail_drawn_cols: the flags columns differ in length"));
            }
            let sl = |a: &PyReadonlyArray1<'py, f64>| {
                a.as_slice().map(|x| x.to_vec())
                    .map_err(|_| PyValueError::new_err("rail_drawn_cols: flags columns must be contiguous"))
            };
            let fv = f.as_slice().map(|x| x.to_vec())
                .map_err(|_| PyValueError::new_err("rail_drawn_cols: flags columns must be contiguous"))?;
            Some((fv, sl(s)?, sl(e)?, Col::new(v, m, "flags value")?))
        }
    };
    let wear = wears(n, fl.as_ref().map(|(f, s, e, v)| (f.as_slice(), s.as_slice(), e.as_slice(), v)));
    let mut plain = vec![-1i8; n];
    let mut bridges = vec![-1i8; n];
    let mut wout = vec![0i8; n];
    for i in 0..n {
        let Some(sub) = &sub else { break };
        if !eq(sub.row(i), "rail") || wear[i] < 0 {
            continue;
        }
        let w = wear[i];
        let c: &[u32] = cls.as_ref().map(|c| c.row(i)).unwrap_or(&[]);
        let code = if cls.is_some() { class_code(c) } else { Some(0) };
        let drawn_code = |code: Option<i8>| code.filter(|&k| k != TRAM);
        if let Some(v) = osm.as_ref().map(|o| o.row(i)).filter(|v| !v.is_empty()) {
            if eq(v, "tram") {
                bridges[i] = TRAM;
                wout[i] = w;
                continue;
            }
            if OSM_NOT.iter().any(|k| eq(v, k)) || (historic_not && eq(v, "historic")) {
                continue;
            }
            if eq(v, "abandoned") && hw.as_ref().is_some_and(|h| !h.row(i).is_empty()) {
                continue;
            }
            if let Some(&(_, ow)) = OSM_WEAR.iter().find(|(k, _)| eq(v, k)) {
                let k = drawn_code(code).unwrap_or(0);
                plain[i] = k;
                bridges[i] = k;
                wout[i] = ow;
                continue;
            }
        }
        let code = if cls.is_some() && eq(c, "unknown") && w != 0 { Some(0) } else { code };
        if let Some(k) = drawn_code(code) {
            plain[i] = k;
        }
        if let Some(k) = code {
            bridges[i] = k;
        }
        wout[i] = w;
    }
    Ok((plain.into_pyarray(py), bridges.into_pyarray(py), wout.into_pyarray(py)))
}
