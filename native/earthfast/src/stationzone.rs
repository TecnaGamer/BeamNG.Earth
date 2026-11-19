//! The bus stations' raised walkable ground (the full port's busstationmesh.zone loops and _clip_cell, 494): marching
//! squares over walk_field's signed distance F on the 1 m lattice, 2 x 2 blocks of full cells as one quad, the other
//! full cells one quad each, the partial cells clipped where F crosses 0 (a kerb wall and a tactile strip along every cut
//! edge). Python looped over every cell with tuples and closures: ~2.8 CPU-s of Manhattan's 25 cold road tiles
//! (2026-11-18, the stations' zone is built for both the render and the collision mesh of every tile that sees it).
//! The numbers are the Python's to the bit: the same products and sums in its order, CPython's math.hypot (py_hypot)
//! for a wall's length, Python's sum (left to right) for a clipped cell's centre.
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A clipped cell's vertex: a corner k, or a point on the edge k -> k + 1 at t (its height blended the same way).
#[derive(Clone, Copy)]
enum Src {
    C(usize),
    E(usize, f64),
}

#[derive(Clone, Copy)]
struct V {
    x: f64,
    y: f64,
    src: Src,
}

/// busstationmesh._clip_cell: the part of the quad (corners pts, counter-clockwise; values f) where f >= 0, as
/// polygons. (Its cut edges are exactly the polygons' edges between two edge points, so they are not returned.)
fn clip_cell(pts: &[[f64; 2]; 4], f: &[f64; 4], polys: &mut Vec<Vec<V>>) {
    polys.clear();
    let inside: [bool; 4] = [f[0] >= 0.0, f[1] >= 0.0, f[2] >= 0.0, f[3] >= 0.0];
    let n_in = inside.iter().filter(|&&b| b).count();
    if n_in == 0 {
        return;
    }
    let corner = |k: usize| V { x: pts[k][0], y: pts[k][1], src: Src::C(k) };
    if n_in == 4 {
        polys.push((0..4).map(corner).collect());
        return;
    }
    let cross = |k: usize| {
        let k1 = (k + 1) % 4;
        let (a, b) = (f[k], f[k1]);
        let t = a / (a - b);
        V { x: pts[k][0] + (pts[k1][0] - pts[k][0]) * t, y: pts[k][1] + (pts[k1][1] - pts[k][1]) * t, src: Src::E(k, t) }
    };
    if n_in == 2 && inside[0] == inside[2] {
        // two opposite corners: two triangles
        for (k, &inn) in inside.iter().enumerate() {
            if inn {
                polys.push(vec![cross((k + 3) % 4), corner(k), cross(k)]);
            }
        }
        return;
    }
    let mut ring = Vec::with_capacity(5);
    for k in 0..4 {
        if inside[k] {
            ring.push(corner(k));
        }
        if inside[k] != inside[(k + 1) % 4] {
            ring.push(cross(k));
        }
    }
    polys.push(ring);
}

/// The cell's blend of the corner values v at a vertex (busstationmesh.zone's height / lift).
#[inline]
fn blend(v: &[f64; 4], src: Src) -> f64 {
    match src {
        Src::C(k) => v[k],
        Src::E(k, t) => v[k] + (v[(k + 1) % 4] - v[k]) * t,
    }
}

/// The largest lattice the kernel takes (busstationmesh.MAX_VERTS, walk_field's own limit): the copies are sized by it.
const MAX_POINTS: usize = 600_000;

fn arr2<'py>(py: Python<'py>, v: Vec<f64>, cols: usize) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let n = v.len() / cols;
    Ok(numpy::ndarray::Array2::from_shape_vec((n, cols), v).map_err(|e| PyValueError::new_err(e.to_string()))?
        .into_pyarray(py))
}

fn arr2i<'py>(py: Python<'py>, v: Vec<i64>) -> PyResult<Bound<'py, PyArray2<i64>>> {
    let n = v.len() / 3;
    Ok(numpy::ndarray::Array2::from_shape_vec((n, 3), v).map_err(|e| PyValueError::new_err(e.to_string()))?
        .into_pyarray(py))
}

fn arrays(py: Python<'_>, o: Out) -> PyResult<ZoneArrays<'_>> {
    Ok((arr2(py, o.p, 3)?, arr2i(py, o.t)?, arr2(py, o.wp, 3)?, arr2(py, o.wuv, 2)?, arr2i(py, o.wt)?,
        arr2(py, o.tp, 3)?, arr2(py, o.tuv, 2)?, arr2i(py, o.tt)?))
}

#[derive(Default)]
struct Out {
    p: Vec<f64>,
    t: Vec<i64>,
    wp: Vec<f64>,
    wuv: Vec<f64>,
    wt: Vec<i64>,
    tp: Vec<f64>,
    tuv: Vec<f64>,
    tt: Vec<i64>,
}

type ZoneArrays<'py> = (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                        Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                        Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>);

/// busstationmesh.zone's mesh of one station: F, zg and h (ny + 1, nx + 1) the signed distance, the ground and the slab's
/// height at the lattice points (x0 + i, y0 + j); foot the walls' reach below the ground (SLAB_FOOT_M), kerb_rep the
/// kerb's metres per texture repeat (KERB[1]), tac (up, in, width, metres per repeat) the tactile strip's (TACTILE_UP_M,
/// TACTILE_IN_M, TACTILE_W_M, TACTILE[1]). Returns (P (k, 3), T (t, 3)) of the top, (P, UV, T) of the kerb walls and
/// (P, UV, T) of the tactile strips, in the Python's order. At most MAX_POINTS lattice points; a field with a non-finite
/// distance gives no zone (walk_field's distances to finite rings are finite: a NaN is a broken station's geometry, which
/// the Python drew as NaN vertices into the tile's mesh, their signs as the compiler's operand order left them).
#[pyfunction]
#[pyo3(signature = (x0, y0, f, zg, h, foot, kerb_rep, tac))]
#[allow(clippy::too_many_arguments)]
pub fn station_zone<'py>(py: Python<'py>, x0: f64, y0: f64, f: PyReadonlyArray2<'py, f64>, zg: PyReadonlyArray2<'py, f64>,
                         h: PyReadonlyArray2<'py, f64>, foot: f64, kerb_rep: f64, tac: (f64, f64, f64, f64))
                         -> PyResult<ZoneArrays<'py>> {
    let (fa, za, ha) = (f.as_array(), zg.as_array(), h.as_array());
    let (rows, cols) = fa.dim();
    if za.dim() != (rows, cols) || ha.dim() != (rows, cols) {
        return Err(PyValueError::new_err("station_zone: f, zg and h (ny + 1, nx + 1) alike"));
    }
    if rows.checked_mul(cols).is_none_or(|n| n > MAX_POINTS) {
        return Err(PyValueError::new_err("station_zone: more lattice points than busstationmesh.MAX_VERTS"));
    }
    // (copied out of numpy before the GIL is released)
    let fv: Vec<f64> = fa.iter().copied().collect();
    let zv: Vec<f64> = za.iter().copied().collect();
    let hv: Vec<f64> = ha.iter().copied().collect();
    if fv.iter().any(|v| !v.is_finite()) {
        return arrays(py, Out::default());
    }
    let o = py.detach(move || zone(x0, y0, rows, cols, &fv, &zv, &hv, foot, kerb_rep, tac));
    arrays(py, o)
}

#[allow(clippy::too_many_arguments)]
fn zone(x0: f64, y0: f64, rows: usize, cols: usize, f: &[f64], zg: &[f64], h: &[f64], foot: f64, kerb_rep: f64,
        tac: (f64, f64, f64, f64)) -> Out {
    let mut o = Out::default();
    if rows < 2 || cols < 2 {
        return o;
    }
    let (ny, nx) = (rows - 1, cols - 1);
    let at = |j: usize, i: usize| j * cols + i;
    let lx = |i: usize| x0 + i as f64;
    let ly = |j: usize| y0 + j as f64;
    let ins = |j: usize, i: usize| f[at(j, i)] >= 0.0;
    let full: Vec<bool> = (0..ny * nx).map(|c| {
        let (j, i) = (c / nx, c % nx);
        ins(j, i) && ins(j, i + 1) && ins(j + 1, i + 1) && ins(j + 1, i)
    }).collect();
    let mut done = vec![false; ny * nx];
    let mut k0: i64 = 0;
    let quad = |o: &mut Out, k0: &mut i64, corners: [(usize, usize); 4]| {
        for (jj, ii) in corners {
            o.p.extend_from_slice(&[lx(ii), ly(jj), zg[at(jj, ii)] + h[at(jj, ii)]]);
        }
        o.t.extend_from_slice(&[*k0, *k0 + 1, *k0 + 2, *k0, *k0 + 2, *k0 + 3]);
        *k0 += 4;
    };
    // 2 x 2 blocks of full cells (range(0, ny - 1, 2) x range(0, nx - 1, 2))
    let mut j = 0;
    while j + 1 < ny {
        let mut i = 0;
        while i + 1 < nx {
            let cells = [(j, i), (j, i + 1), (j + 1, i), (j + 1, i + 1)];
            if cells.iter().all(|&(a, b)| full[a * nx + b]) {
                for (a, b) in cells {
                    done[a * nx + b] = true;
                }
                quad(&mut o, &mut k0, [(j, i), (j, i + 2), (j + 2, i + 2), (j + 2, i)]);
            }
            i += 2;
        }
        j += 2;
    }
    // the other full cells (np.nonzero's row-major order)
    for c in 0..ny * nx {
        if full[c] && !done[c] {
            let (j, i) = (c / nx, c % nx);
            quad(&mut o, &mut k0, [(j, i), (j, i + 1), (j + 1, i + 1), (j + 1, i)]);
        }
    }
    let (tac_up, tac_in, tac_w, tac_rep) = tac;
    let (o0, o1) = (tac_in, tac_in + tac_w);
    let (mut k1, mut k2): (i64, i64) = (0, 0);
    let mut polys: Vec<Vec<V>> = Vec::new();
    for (c, &whole) in full.iter().enumerate() {
        let (j, i) = (c / nx, c % nx);
        let any = ins(j, i) || ins(j, i + 1) || ins(j + 1, i + 1) || ins(j + 1, i);
        if !any || whole {
            continue;
        }
        let corners = [(j, i), (j, i + 1), (j + 1, i + 1), (j + 1, i)];
        let pts = corners.map(|(a, b)| [lx(b), ly(a)]);
        let fv = corners.map(|(a, b)| f[at(a, b)]);
        let zv = corners.map(|(a, b)| zg[at(a, b)]);
        let hv = corners.map(|(a, b)| h[at(a, b)]);
        clip_cell(&pts, &fv, &mut polys);
        for poly in &polys {
            if poly.len() < 3 {
                continue;
            }
            let base = k0;
            for v in poly {
                o.p.extend_from_slice(&[v.x, v.y, blend(&zv, v.src) + blend(&hv, v.src)]);
                k0 += 1;
            }
            for q in 1..poly.len() as i64 - 1 {
                o.t.extend_from_slice(&[base, base + q, base + q + 1]);
            }
        }
        for poly in &polys {
            let n = poly.len();
            for q in 0..n {
                let (a, b) = (poly[q], poly[(q + 1) % n]);
                // (every edge between two edge points is a cut edge: the Python's `in cuts` test always held)
                if !(matches!(a.src, Src::E(..)) && matches!(b.src, Src::E(..))) {
                    continue;
                }
                let (za_, zb_) = (blend(&zv, a.src), blend(&zv, b.src));
                let (ha_, hb_) = (blend(&hv, a.src), blend(&hv, b.src));
                let length = crate::py_hypot(b.x - a.x, b.y - a.y);
                if length < 1e-4 {
                    continue;
                }
                o.wp.extend_from_slice(&[a.x, a.y, za_ - foot, b.x, b.y, zb_ - foot, b.x, b.y, zb_ + hb_,
                                         a.x, a.y, za_ + ha_]);
                o.wuv.extend_from_slice(&[0.0, (za_ - foot) / kerb_rep, length / kerb_rep, (zb_ - foot) / kerb_rep,
                                          length / kerb_rep, (zb_ + hb_) / kerb_rep, 0.0, (za_ + ha_) / kerb_rep]);
                o.wt.extend_from_slice(&[k1, k1 + 1, k1 + 2, k1, k1 + 2, k1 + 3]);
                k1 += 4;
                // the tactile strip along the kerb
                let (mut cx, mut cy) = (0.0f64, 0.0f64);
                for v in poly {
                    cx += v.x;
                }
                for v in poly {
                    cy += v.y;
                }
                let (cx, cy) = (cx / n as f64, cy / n as f64);
                let (ex, ey) = ((b.x - a.x) / length, (b.y - a.y) / length);
                let (mut nx_, mut ny_) = (-ey, ex);
                if (cx - (a.x + b.x) / 2.0) * nx_ + (cy - (a.y + b.y) / 2.0) * ny_ < 0.0 {
                    (nx_, ny_) = (-nx_, -ny_);
                }
                let (z0, z1) = (za_ + ha_ + tac_up, zb_ + hb_ + tac_up);
                o.tp.extend_from_slice(&[a.x + nx_ * o0, a.y + ny_ * o0, z0, b.x + nx_ * o0, b.y + ny_ * o0, z1,
                                         b.x + nx_ * o1, b.y + ny_ * o1, z1, a.x + nx_ * o1, a.y + ny_ * o1, z0]);
                let (ua, ub) = ((a.x * ex + a.y * ey) / tac_rep, (b.x * ex + b.y * ey) / tac_rep);
                o.tuv.extend_from_slice(&[ua, 0.0, ub, 0.0, ub, 1.0, ua, 1.0]);
                if nx_ * ey - ny_ * ex > 0.0 {
                    o.tt.extend_from_slice(&[k2, k2 + 2, k2 + 1, k2, k2 + 3, k2 + 2]);
                } else {
                    o.tt.extend_from_slice(&[k2, k2 + 1, k2 + 2, k2, k2 + 2, k2 + 3]);
                }
                k2 += 4;
            }
        }
    }
    o
}
