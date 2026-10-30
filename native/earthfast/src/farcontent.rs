//! Far content as geotiles (phase 2, .claude/work/geotiles_plan.md): the far bridges' slabs (buildings.far_bridge_tile),
//! made once per far bridge geotile in its own frame and split by the road geotile each segment belongs to, so the mod
//! hides each piece where that road geotile shows its own deck (one ownership rule by geotile key, near or far).
//!
//! - bridge_slabs: per bridge part (its centre line, deck heights and width) a slab per segment: the deck's top, its
//!   two sides and its underside down to its girders (four quads, buildings.Mesh.quad's corner order). Each segment is
//!   kept by its midpoint: in box mode the half-open box [x0, x1) x [y0, y1) (an anchor tile's square); in geotile mode
//!   the place of the midpoint (world_to_geodetic at h = 0) picks the zoom-cz tile, kept when it lies under the
//!   zoom-pz parent tile (px, py) and grouped by it.

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::geotiles::{tile_of, Frame};

/// roadmesh._normals: the mitred unit normals of a polyline (each joint's normal over the cosine to its segments',
/// at most 2x), its ends the end segments'.
fn normals(xy: &[[f64; 2]]) -> Vec<[f64; 2]> {
    let n = xy.len();
    let mut ns = Vec::with_capacity(n.saturating_sub(1));
    for k in 0..n.saturating_sub(1) {
        let (dx, dy) = (xy[k + 1][0] - xy[k][0], xy[k + 1][1] - xy[k][1]);
        let mut l = dx.hypot(dy);
        if l < 1e-9 {
            l = 1e-9;
        }
        let (tx, ty) = (dx / l, dy / l);
        ns.push([-ty, tx]);
    }
    let mut out = vec![[0.0f64; 2]; n];
    if ns.is_empty() {
        return out;
    }
    out[0] = ns[0];
    out[n - 1] = ns[ns.len() - 1];
    for k in 1..n.saturating_sub(1) {
        let mut m = [ns[k - 1][0] + ns[k][0], ns[k - 1][1] + ns[k][1]];
        let mut ml = m[0].hypot(m[1]);
        if ml < 1e-9 {
            ml = 1.0;
        }
        m = [m[0] / ml, m[1] / ml];
        let cos = (m[0] * ns[k][0] + m[1] * ns[k][1]).clamp(0.5, 1.0);
        out[k] = [m[0] / cos, m[1] / cos];
    }
    out
}

#[derive(Default)]
struct Slab {
    p: Vec<f64>,
    c: Vec<u8>,
    t: Vec<i64>,
    n: i64,
}

impl Slab {
    fn quad(&mut self, q: [[f64; 3]; 4], rgba: [u8; 4]) {
        for v in q.iter() {
            self.p.extend_from_slice(v);
            self.c.extend_from_slice(&rgba);
        }
        let i = self.n;
        self.t.extend_from_slice(&[i, i + 1, i + 2, i, i + 2, i + 3]);
        self.n += 4;
    }
}

/// The slabs of bridge parts (xy (n, 2) and z (n,) of every part one after another, off (k + 1,) their starts,
/// widths (k,) m) relative to origin (x, y): [(gx, gy, P (m, 3), T (t, 3), C (m, 4) RGBA)], one per owner. depth: m from
/// the deck's surface to its girders' bottom; top / side: the top's and the sides' and underside's colours. cz == 0:
/// box mode (box = x0, y0, x1, y1; one entry, gx = gy = -1); else geotile mode in the world of frame (lat0, lon0): the
/// zoom-cz tiles under the zoom-pz tile (px, py), each its own entry (sorted by gx, gy).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn bridge_slabs<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                         off: PyReadonlyArray1<'py, i64>, widths: PyReadonlyArray1<'py, f64>, origin: (f64, f64),
                         depth: f64, top: (u8, u8, u8, u8), side: (u8, u8, u8, u8), bbox: (f64, f64, f64, f64),
                         lat0: f64, lon0: f64, cz: u32, pz: u32, px: i64, py_: i64)
                         -> PyResult<Vec<(i64, i64, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>,
                                          Bound<'py, PyArray2<u8>>)>> {
    let (xa, za, oa, wa) = (xy.as_array(), z.as_array(), off.as_array(), widths.as_array());
    if xa.ncols() < 2 || za.len() != xa.nrows() || oa.len() != wa.len() + 1 {
        return Err(PyValueError::new_err("bridge_slabs: xy (n, 2), z (n,), off (k + 1,), widths (k,)"));
    }
    if oa.iter().any(|&o| o < 0 || o as usize > xa.nrows()) || oa.windows(2).into_iter().any(|w| w[1] < w[0]) {
        return Err(PyValueError::new_err("bridge_slabs: off out of range"));
    }
    if cz > 0 && pz > cz {
        return Err(PyValueError::new_err("bridge_slabs: the parent zoom is finer than the owners'"));
    }
    let pts: Vec<[f64; 2]> = (0..xa.nrows()).map(|i| [xa[[i, 0]], xa[[i, 1]]]).collect();
    let zs: Vec<f64> = za.iter().copied().collect();
    let offs: Vec<usize> = oa.iter().map(|&o| o as usize).collect();
    let ws: Vec<f64> = wa.iter().copied().collect();
    let (top, side) = ([top.0, top.1, top.2, top.3], [side.0, side.1, side.2, side.3]);
    let groups = py.allow_threads(|| {
        let frame = Frame::new(lat0, lon0);
        let mut by: std::collections::BTreeMap<(i64, i64), Slab> = std::collections::BTreeMap::new();
        for k in 0..ws.len() {
            let (a0, a1) = (offs[k], offs[k + 1]);
            if a1 < a0 + 2 {
                continue;
            }
            let line = &pts[a0..a1];
            let zl = &zs[a0..a1];
            let half = ws[k] / 2.0;
            let nr = normals(line);
            // (xy + nrm - origin, the Python's order of operations)
            let l: Vec<[f64; 2]> = line.iter().zip(&nr)
                .map(|(p, n)| [(p[0] + n[0] * half) - origin.0, (p[1] + n[1] * half) - origin.1]).collect();
            let r: Vec<[f64; 2]> = line.iter().zip(&nr)
                .map(|(p, n)| [(p[0] - n[0] * half) - origin.0, (p[1] - n[1] * half) - origin.1]).collect();
            for i in 0..line.len() - 1 {
                let j = i + 1;
                let (mx, my) = ((line[i][0] + line[j][0]) / 2.0, (line[i][1] + line[j][1]) / 2.0);
                let owner = if cz == 0 {
                    if mx >= bbox.0 && mx < bbox.2 && my >= bbox.1 && my < bbox.3 { Some((-1, -1)) } else { None }
                } else {
                    let (la, lo, _) = frame.world_to_geodetic(mx, my, 0.0);
                    let (gx, gy) = tile_of(la, lo, cz);
                    let sh = cz - pz;
                    if (gx >> sh, gy >> sh) == (px, py_) { Some((gx, gy)) } else { None }
                };
                let Some(key) = owner else { continue };
                let s = by.entry(key).or_default();
                let (a, b, c, d) = (l[i], l[j], r[i], r[j]);
                let (zt0, zt1) = (zl[i], zl[j]);
                let (zb0, zb1) = (zt0 - depth, zt1 - depth);
                s.quad([[a[0], a[1], zt0], [c[0], c[1], zt0], [d[0], d[1], zt1], [b[0], b[1], zt1]], top);
                s.quad([[a[0], a[1], zb0], [a[0], a[1], zt0], [b[0], b[1], zt1], [b[0], b[1], zb1]], side);
                s.quad([[d[0], d[1], zb1], [d[0], d[1], zt1], [c[0], c[1], zt0], [c[0], c[1], zb0]], side);
                s.quad([[c[0], c[1], zb0], [a[0], a[1], zb0], [b[0], b[1], zb1], [d[0], d[1], zb1]], side);
            }
        }
        by
    });
    let mut out = Vec::with_capacity(groups.len());
    for ((gx, gy), s) in groups {
        let m = s.p.len() / 3;
        let nt = s.t.len() / 3;
        let p = numpy::ndarray::Array2::from_shape_vec((m, 3), s.p).unwrap();
        let t = numpy::ndarray::Array2::from_shape_vec((nt, 3), s.t).unwrap();
        let c = numpy::ndarray::Array2::from_shape_vec((m, 4), s.c).unwrap();
        out.push((gx, gy, p.into_pyarray(py), t.into_pyarray(py), c.into_pyarray(py)));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normals_straight_and_mitred() {
        let n = normals(&[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]);
        assert!((n[0][0] - 0.0).abs() < 1e-12 && (n[0][1] - 1.0).abs() < 1e-12);
        // the joint: the bisector over cos 45 deg (length sqrt 2)
        assert!((n[1][0].hypot(n[1][1]) - 2f64.sqrt()).abs() < 1e-9);
        assert!((n[2][0] + 1.0).abs() < 1e-12 && n[2][1].abs() < 1e-12);
    }
}
