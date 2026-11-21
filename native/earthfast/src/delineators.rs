//! Regular verge rows. Country lookups are observable: sample a whole side, then query
//! its stations, before starting the other side. Only the final candidates are sorted.
use crate::dm::{interp, Dm};
use crate::furnstations::{at, Line};
use crate::roadnet::pyfloat::py_round;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2};
use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::BTreeMap;
use std::rc::Rc;

fn curve(a: &[f64], p: &[f64], b: &[f64]) -> (f64, f64) {
    let (ux, uy) = (p[0] - a[0], p[1] - a[1]);
    let (vx, vy) = (b[0] - p[0], b[1] - p[1]);
    let cross = ux * vy - uy * vx;
    let norm = |x: f64, y: f64| (x * x + y * y).sqrt();
    let lengths = norm(ux, uy) * norm(vx, vy) * norm(ux + vx, uy + vy);
    let r = if cross.abs() > 1e-8 && lengths > 1e-8 {
        lengths / (2.0 * cross.abs())
    } else {
        f64::INFINITY
    };
    (r, if cross == 0.0 { 0.0 } else { cross.signum() })
}

#[pyfunction]
pub fn delineator_curvature<'py>(
    py: Python<'py>, a: PyReadonlyArray2<f64>, p: PyReadonlyArray2<f64>, b: PyReadonlyArray2<f64>,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (a, p, b) = (a.as_array(), p.as_array(), b.as_array());
    if a.dim() != p.dim() || a.dim() != b.dim() || a.ncols() < 2 {
        return Err(PyValueError::new_err("expected three matching point arrays with x and y"));
    }
    let xy = |v: &numpy::ndarray::ArrayView2<f64>, i| [v[[i, 0]], v[[i, 1]]];
    let (radius, turn): (Vec<_>, Vec<_>) = (0..a.nrows())
        .map(|i| curve(&xy(&a, i), &xy(&p, i), &xy(&b, i))).unzip();
    Ok((radius.into_pyarray(py), turn.into_pyarray(py)))
}

struct Sample {
    point: Vec<f64>,
    tangent: (f64, f64),
    radius: f64,
    turn: f64,
}

impl Sample {
    fn new(line: &Line, arc: &[f64], station: f64) -> Self {
        let (mut a, mut p, mut b) = (Vec::new(), Vec::new(), Vec::new());
        at(line, arc, station - 15.0, &mut a);
        let tangent = at(line, arc, station, &mut p);
        at(line, arc, station + 15.0, &mut b);
        let (radius, turn) = curve(&a, &p, &b);
        Self { point: p, tangent, radius, turn }
    }

    fn shoulder(&self, side: f64, offset: f64) -> (f64, f64, f64) {
        (self.point[0] + side * -self.tangent.1 * offset,
         self.point[1] + side * self.tangent.0 * offset, self.point[2])
    }
}

struct Rule {
    marker: String,
    left: bool,
    iceland_minor: bool,
    norway: bool,
    spacing: f64,
    curves: Vec<(f64, f64)>,
}

impl Rule {
    fn gap(&self, radius: f64) -> f64 {
        for &(limit, distance) in &self.curves {
            if radius < limit {
                return distance * if self.iceland_minor { 2.0 } else { 1.0 };
            }
        }
        if self.iceland_minor { 100.0 } else { self.spacing }
    }

    fn verge(&self, radius: f64, turn: f64, side: f64) -> bool {
        (!self.iceland_minor || side < 0.0) && !(self.norway && radius < 50.0 && side * turn > 0.0)
    }
}

#[pyfunction]
#[pyo3(signature = (coords, spec, lit, phase, country_at, profiles, kits, left_hand, tile, owns, clear))]
pub fn delineator_rows<'py>(
    py: Python<'py>, coords: PyReadonlyArray2<f64>, spec: &Bound<PyAny>, lit: &Bound<PyAny>,
    phase: f64, country_at: &Bound<PyAny>, profiles: &Bound<PyAny>, kits: &Bound<PyDict>,
    left_hand: &Bound<PyAny>, tile: &Bound<PyAny>, owns: &Bound<PyAny>, clear: &Bound<PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let rows = PyList::empty(py);
    let arr = coords.as_array();
    let n = arr.nrows();
    if n == 0 || arr.ncols() < 2 {
        return Err(PyIndexError::new_err("empty delineator line or missing coordinates"));
    }
    let reverse = (arr[[n - 1, 0]], arr[[n - 1, 1]]) < (arr[[0, 0]], arr[[0, 1]]);
    let reversed;
    let data = if reverse {
        reversed = arr.rows().into_iter().rev().flat_map(|r| r.to_vec()).collect::<Vec<_>>();
        &reversed[..]
    } else {
        coords.as_slice()?
    };
    let line = Line { v: data, c: arr.ncols() };
    let arc = line.arclen();
    let length = arc[n - 1];
    if length < 1e-6 {
        return Ok(rows);
    }
    if !length.is_finite() || arr.ncols() < 3 {
        return Err(PyValueError::new_err("expected a finite delineator line with elevations"));
    }
    let offset = spec.getattr("width")?.extract::<f64>()? / 2.0 + 1.1;
    let mut rules = BTreeMap::new();
    let mut lookup = |xyz| -> PyResult<Option<Rc<Rule>>> {
        let country = country_at.call1(xyz)?;
        let Some(kit) = kits.get_item(&country)? else { return Ok(None) };
        let code: String = country.extract()?;
        if let Some(rule) = rules.get(&code) {
            return Ok(Option::clone(rule));
        }
        let rule = if profiles.call_method1("eligible", (&country, spec, lit))?.is_truthy()? {
            let policy = profiles.call_method1("policy", (&country,))?;
            let class: String = spec.getattr("cls")?.extract()?;
            Some(Rc::new(Rule {
                marker: format!("eu_marker_{}", kit.getattr("marker")?.extract::<String>()?),
                left: left_hand.contains(&country)?,
                iceland_minor: code == "IS" && matches!(class.as_str(), "tertiary" | "unclassified"),
                norway: code == "NO", spacing: policy.getattr("spacing")?.extract()?,
                curves: policy.getattr("curves")?.extract()?,
            }))
        } else {
            None
        };
        rules.insert(code, rule.clone());
        Ok(rule)
    };

    let mut stations: Vec<_> = (0..(length / 5.0).ceil() as usize).map(|i| i as f64 * 5.0).collect();
    stations.push(length);
    let samples: Vec<_> = stations.iter().map(|&s| Sample::new(&line, &arc, s)).collect();
    let mut candidates = Vec::new();
    for side in [-1.0, 1.0] {
        let mut gaps = Vec::with_capacity(stations.len());
        let mut eligible = false;
        for sample in &samples {
            let rule = lookup(sample.shoulder(side, offset))?;
            eligible |= rule.is_some();
            gaps.push(rule.map_or(50.0, |r| r.gap(sample.radius)));
        }
        if !eligible {
            continue;
        }
        let mut metric = vec![0.0];
        let mut sum = 0.0;
        for (s, g) in stations.windows(2).zip(gaps.windows(2)) {
            sum += (s[1] - s[0]) * (1.0 / g[0] + 1.0 / g[1]) / 2.0;
            metric.push(sum);
        }
        // np.arange stores start and start + step, then fills using their rounded difference.
        let second = phase + 1.0;
        let levels: Vec<_> = (0..(sum - phase).ceil() as usize).map(|i| match i {
            0 => phase, 1 => second, _ => phase + i as f64 * (second - phase),
        }).collect();
        for s in interp(&levels, &metric, &stations, None, None) {
            let sample = Sample::new(&line, &arc, s);
            let xyz = sample.shoulder(side, offset);
            if let Some(rule) = lookup(xyz)? {
                if rule.verge(sample.radius, sample.turn, side) {
                    candidates.push((s, side, rule, xyz, sample.tangent));
                }
            }
        }
    }
    candidates.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));
    for (_, side, rule, (x, y, z), (dx, dy)) in candidates {
        if !owns.call1((tile, x, y))?.is_truthy()? || !clear.call1(((x, y, z), py.None(), 0.65))?.is_truthy()? {
            continue;
        }
        let leading = if rule.left { side } else { -side };
        let yaw = (-dx).datan2(dy).to_degrees() + if leading < 0.0 { 180.0 } else { 0.0 };
        let row = PyList::empty(py);
        row.append(&rule.marker)?;
        for v in [py_round(x, 2), py_round(y, 2), py_round(z, 2), py_round(yaw.rem_euclid(360.0), 1), 1., 1.15, 0.] {
            row.append(v)?;
        }
        rows.append(row)?;
    }
    Ok(rows)
}
