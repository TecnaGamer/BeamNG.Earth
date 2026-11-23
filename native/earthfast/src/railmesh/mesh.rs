//! Accumulate strips and cuboids in source order. Winding is tested in tile-local coordinates.
use crate::busstation::{points, rows, Point};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};
type V = [f64; 3];
pub(super) fn at(p: Point, z: f64) -> V {
    [p.0, p.1, z]
}
fn sub(a: V, b: V) -> V {
    std::array::from_fn(|i| a[i] - b[i])
}
fn faces_back(p: &[V], idx: [usize; 3], want: V) -> bool {
    let a = sub(p[idx[1]], p[idx[0]]);
    let b = sub(p[idx[2]], p[idx[0]]);
    let n = [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ];
    (n[0] * want[0] + n[1] * want[1]) + n[2] * want[2] < 0.
}
type Output<'p> = (
    Bound<'p, PyArray2<f64>>,
    Bound<'p, PyArray2<i64>>,
    Bound<'p, PyArray2<f64>>,
    Vec<String>,
    Bound<'p, PyArray1<i64>>,
);
#[pyclass]
pub(super) struct RailMesh {
    origin: V,
    p: Vec<V>,
    uv: Vec<[f64; 2]>,
    t: Vec<[i64; 3]>,
    m: Vec<i64>,
    mats: Vec<String>,
}
impl RailMesh {
    pub(super) fn band(
        &mut self,
        a: &[V],
        b: &[V],
        ua: f64,
        ub: f64,
        v: &[f64],
        mi: i64,
        up: &[V],
    ) {
        let k = a.len();
        let base = self.p.len();
        self.p
            .extend(a.iter().chain(b).map(|&p| sub(p, self.origin)));
        for u in [ua, ub] {
            self.uv.extend(v.iter().map(|&v| [u, v]));
        }
        for second in [false, true] {
            for i in 0..k.saturating_sub(1) {
                let tri = if second {
                    [i, k + i + 1, i + 1]
                } else {
                    [i, k + i, k + i + 1]
                };
                self.triangle(
                    tri.map(|j| base + j),
                    mi,
                    up[if up.len() == 1 { 0 } else { i }],
                );
            }
        }
    }
    fn triangle(&mut self, mut idx: [usize; 3], mi: i64, w: V) {
        if faces_back(&self.p, idx, w) {
            idx.swap(0, 2)
        }
        self.t.push(idx.map(|i| i as i64));
        self.m.push(mi);
    }
    pub(super) fn cubes(
        &mut self,
        c: &[Point],
        ax: &[Point],
        ay: &[Point],
        hx: f64,
        hy: f64,
        z0: &[f64],
        z1: &[f64],
        mi: i64,
    ) {
        let n = c.len();
        let base = self.p.len();
        let corners = [(-1., -1.), (1., -1.), (1., 1.), (-1., 1.)];
        for i in 0..n {
            for z in [z1[i], z0[i]] {
                for (sx, sy) in corners {
                    let q = c[i] + ax[i] * (sx * hx) + ay[i] * (sy * hy);
                    self.p.push(sub(at(q, z), self.origin));
                    self.uv.push([(sx + 1.) * 0.125, (sy + 1.) * 0.125]);
                }
            }
        }
        for [a, b, c1, d] in [
            [0, 1, 2, 3],
            [0, 1, 5, 4],
            [1, 2, 6, 5],
            [2, 3, 7, 6],
            [3, 0, 4, 7],
        ] {
            for face in [[a, b, c1], [a, c1, d]] {
                for i in 0..n {
                    let idx = face.map(|j| base + i * 8 + j);
                    let [p, q, r] = idx.map(|j| self.p[j]);
                    let mean = std::array::from_fn(|k| (p[k] + q[k] + r[k]) / 3.);
                    let ctr = sub(at(c[i], (z0[i] + z1[i]) / 2.), self.origin);
                    self.triangle(idx, mi, sub(mean, ctr));
                }
            }
        }
    }
}
#[pymethods]
impl RailMesh {
    #[new]
    pub(super) fn new(origin: V) -> Self {
        Self {
            origin,
            p: vec![],
            uv: vec![],
            t: vec![],
            m: vec![],
            mats: vec![],
        }
    }
    #[getter]
    pub(super) fn nv(&self) -> usize {
        self.p.len()
    }
    pub(super) fn mat(&mut self, name: &str) -> i64 {
        if let Some(i) = self.mats.iter().position(|v| v == name) {
            return i as i64;
        }
        self.mats.push(name.into());
        (self.mats.len() - 1) as i64
    }
    fn strip(
        &mut self,
        a: Vec<V>,
        b: Vec<V>,
        ua: f64,
        ub: f64,
        v: Vec<f64>,
        mi: i64,
        up: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let up = if up.getattr("ndim")?.extract::<usize>()? == 2 {
            up.extract::<Vec<V>>()?
        } else {
            vec![up.extract()?]
        };
        if b.len() != a.len() || v.len() != a.len() || (up.len() != 1 && up.len() != a.len()) {
            return Err(PyValueError::new_err("rail strip row counts differ"));
        }
        self.band(&a, &b, ua, ub, &v, mi, &up);
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    fn boxes(
        &mut self,
        c: PyReadonlyArray2<'_, f64>,
        ax: PyReadonlyArray2<'_, f64>,
        ay: PyReadonlyArray2<'_, f64>,
        hx: f64,
        hy: f64,
        z0: PyReadonlyArray1<'_, f64>,
        z1: PyReadonlyArray1<'_, f64>,
        mi: i64,
    ) -> PyResult<()> {
        let (c, ax, ay) = (points(c)?, points(ax)?, points(ay)?);
        let (z0, z1) = (z0.as_array().to_vec(), z1.as_array().to_vec());
        if [ax.len(), ay.len(), z0.len(), z1.len()]
            .iter()
            .any(|&n| n != c.len())
        {
            return Err(PyValueError::new_err("rail box row counts differ"));
        }
        self.cubes(&c, &ax, &ay, hx, hy, &z0, &z1, mi);
        Ok(())
    }
    pub(super) fn result<'p>(&self, py: Python<'p>) -> Option<Output<'p>> {
        if self.p.is_empty() {
            return None;
        }
        Some((
            rows(py, self.p.clone()),
            rows(py, self.t.clone()),
            rows(py, self.uv.clone()),
            self.mats.clone(),
            self.m.clone().into_pyarray(py),
        ))
    }
}
