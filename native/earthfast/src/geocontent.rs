//! Road geotiles (phase 1c, .claude/work/geotiles_plan.md): the kernels that let a road tile be built once in its
//! geotile's own frame (content) and finished per anchor (the draped skin).
//!
//! - geo_owns: which points of a tile frame's world belong to the tile (its lat / lon in the tile): content items
//!   (buildings, stations, wells ...) are each in exactly one tile, as the anchor tiles' half-open boxes made them.
//! - clip_tris_convex: triangles cut to a convex polygon (a geotile's quad mapped into an anchor's world). Each edge
//!   is evaluated with its end points in a canonical order (the smaller first), so the two tiles sharing an edge (whose
//!   corners are bit-equal: the same lat / lon through the same arithmetic) cut along the very same line, one keeping
//!   each side: their skins neither overlap nor leave a gap.
//! - cdae_zstats / cdae_shear: a shape's vertex heights (pivot = their median, relief = the farthest from it) and its
//!   "exact variant": every vertex moved by s (p.z - pivot) (s = R^T c of geotiles.placement), so the shape placed
//!   rigidly (setPosRot, which cannot shear) lands where the exact world map puts it.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::geotiles::{tile_of, Frame};

// --- ownership -----------------------------------------------------------------------------------------------------

/// geotiles.GeoBox.owns: for each point (x, y) of the world of the frame (lat0, lon0) at h = 0, whether its place
/// lies in tile (z, gx, gy).
#[pyfunction]
pub fn geo_owns<'py>(py: Python<'py>, lat0: f64, lon0: f64, z: u32, gx: i64, gy: i64, x: PyReadonlyArray1<'py, f64>,
                     y: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyArray1<bool>> {
    let f = Frame::new(lat0, lon0);
    let (xs, ys) = (x.as_array(), y.as_array());
    let n = xs.len().min(ys.len());
    let out: Vec<bool> = py.allow_threads(|| {
        (0..n).map(|i| {
            let (la, lo, _) = f.world_to_geodetic(xs[i], ys[i], 0.0);
            tile_of(la, lo, z) == (gx, gy)
        }).collect()
    });
    out.into_pyarray(py)
}

// --- clipping ------------------------------------------------------------------------------------------------------

/// The signed side of point p of the line through a, b, the ends in canonical order (the lexicographically smaller
/// first): the same number (sign included) whichever of the two tiles sharing the edge asks.
#[inline]
fn side(a: [f64; 2], b: [f64; 2], p: [f64; 2]) -> f64 {
    let (u, v) = if (a[0], a[1]) <= (b[0], b[1]) { (a, b) } else { (b, a) };
    (v[0] - u[0]) * (p[1] - u[1]) - (v[1] - u[1]) * (p[0] - u[0])
}

pub struct Clipped {
    pub p: Vec<[f64; 3]>,
    pub uv: Vec<[f64; 2]>,
    pub t: Vec<[i64; 3]>,
    pub src: Vec<i64>,
}

/// Triangles (P (n, 3), UV (n, 2), T (m, 3)) cut to the convex polygon poly: those inside kept with their own
/// vertices, those across its edges clipped (Sutherland-Hodgman, position and uv interpolated) and fanned with new
/// vertices appended, those outside dropped. src: each output triangle's input index. A point exactly on an edge is
/// inside (both tiles keep it: a zero-area seam).
pub fn clip_convex(p: &[[f64; 3]], uv: &[[f64; 2]], t: &[[i64; 3]], poly: &[[f64; 2]]) -> Clipped {
    let k = poly.len();
    let mut cx = 0.0;
    let mut cy = 0.0;
    for q in poly {
        cx += q[0];
        cy += q[1];
    }
    let c = [cx / k as f64, cy / k as f64];
    // per edge: its ends and the sign of the inside
    let edges: Vec<([f64; 2], [f64; 2], f64)> = (0..k).map(|e| {
        let (a, b) = (poly[e], poly[(e + 1) % k]);
        let s = side(a, b, c);
        (a, b, if s >= 0.0 { 1.0 } else { -1.0 })
    }).collect();
    let mut out = Clipped { p: p.to_vec(), uv: uv.to_vec(), t: Vec::new(), src: Vec::new() };
    for (ti, tri) in t.iter().enumerate() {
        let vs = [tri[0] as usize, tri[1] as usize, tri[2] as usize];
        let mut all_in = true;
        let mut out_of_one = false;
        for &(a, b, sg) in &edges {
            let d: Vec<f64> = vs.iter().map(|&i| sg * side(a, b, [p[i][0], p[i][1]])).collect();
            if d.iter().any(|&v| v < 0.0) {
                all_in = false;
            }
            if d.iter().all(|&v| v < 0.0) {
                out_of_one = true;
            }
        }
        if all_in {
            out.t.push(*tri);
            out.src.push(ti as i64);
            continue;
        }
        if out_of_one {
            continue;
        }
        let mut poly_p: Vec<([f64; 3], [f64; 2])> = vs.iter().map(|&i| (p[i], uv[i])).collect();
        for &(a, b, sg) in &edges {
            if poly_p.is_empty() {
                break;
            }
            let mut res = Vec::with_capacity(poly_p.len() + 2);
            let n = poly_p.len();
            for j in 0..n {
                let (pa, ua) = poly_p[j];
                let (pb, ub) = poly_p[(j + 1) % n];
                let da = sg * side(a, b, [pa[0], pa[1]]);
                let db = sg * side(a, b, [pb[0], pb[1]]);
                if da >= 0.0 {
                    res.push((pa, ua));
                }
                if (da >= 0.0) != (db >= 0.0) {
                    let f = da / (da - db);
                    res.push(([pa[0] + (pb[0] - pa[0]) * f, pa[1] + (pb[1] - pa[1]) * f, pa[2] + (pb[2] - pa[2]) * f],
                              [ua[0] + (ub[0] - ua[0]) * f, ua[1] + (ub[1] - ua[1]) * f]));
                }
            }
            poly_p = res;
        }
        if poly_p.len() < 3 {
            continue;
        }
        let base = out.p.len() as i64;
        for (q, u) in &poly_p {
            out.p.push(*q);
            out.uv.push(*u);
        }
        for a in 1..poly_p.len() as i64 - 1 {
            out.t.push([base, base + a, base + a + 1]);
            out.src.push(ti as i64);
        }
    }
    out
}

/// roadgeo.clip_quad: (P (n', 3), UV (n', 2), T (m', 3), src (m',)) of the triangles cut to the convex polygon
/// poly (k, 2) (clip_convex).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn clip_tris_convex<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>, uv: PyReadonlyArray2<'py, f64>,
                             t: PyReadonlyArray2<'py, i64>, poly: PyReadonlyArray2<'py, f64>)
                             -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>,
                                          Bound<'py, PyArray1<i64>>)> {
    let (pa, ua, ta, qa) = (p.as_array(), uv.as_array(), t.as_array(), poly.as_array());
    if pa.ncols() < 3 || ua.ncols() < 2 || ta.ncols() != 3 || qa.ncols() < 2 || qa.nrows() < 3 {
        return Err(PyValueError::new_err("clip_tris_convex: P (n, 3), UV (n, 2), T (m, 3), poly (k >= 3, 2)"));
    }
    if ua.nrows() != pa.nrows() {
        return Err(PyValueError::new_err("clip_tris_convex: P and UV differ in length"));
    }
    let n = pa.nrows() as i64;
    let pv: Vec<[f64; 3]> = (0..pa.nrows()).map(|i| [pa[[i, 0]], pa[[i, 1]], pa[[i, 2]]]).collect();
    let uvv: Vec<[f64; 2]> = (0..ua.nrows()).map(|i| [ua[[i, 0]], ua[[i, 1]]]).collect();
    let tv: Vec<[i64; 3]> = (0..ta.nrows()).map(|i| [ta[[i, 0]], ta[[i, 1]], ta[[i, 2]]]).collect();
    if tv.iter().any(|tr| tr.iter().any(|&v| v < 0 || v >= n)) {
        return Err(PyValueError::new_err("clip_tris_convex: a triangle's index is out of range"));
    }
    let qv: Vec<[f64; 2]> = (0..qa.nrows()).map(|i| [qa[[i, 0]], qa[[i, 1]]]).collect();
    let c = py.allow_threads(|| clip_convex(&pv, &uvv, &tv, &qv));
    let (np_, nt) = (c.p.len(), c.t.len());
    let p2 = numpy::ndarray::Array2::from_shape_vec((np_, 3), c.p.into_iter().flatten().collect()).unwrap();
    let u2 = numpy::ndarray::Array2::from_shape_vec((np_, 2), c.uv.into_iter().flatten().collect()).unwrap();
    let t2 = numpy::ndarray::Array2::from_shape_vec((nt, 3), c.t.into_iter().flatten().collect()).unwrap();
    Ok((p2.into_pyarray(py), u2.into_pyarray(py), t2.into_pyarray(py), c.src.into_pyarray(py)))
}

/// grading.trim_plan by a convex polygon (a map cell's quad in a planning region's frame): per segment of xy (n, 2)
/// the (t0, t1) of its part inside poly (k, 2), NaN when none (as earthfast.clip_segments for a box). Each edge's side
/// is evaluated with its ends in canonical order (side), so two cells sharing an edge cut a road at the very same t.
#[pyfunction]
pub fn clip_segments_convex<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, poly: PyReadonlyArray2<'py, f64>)
                                 -> PyResult<Bound<'py, PyArray2<f64>>> {
    let p = xy.as_array();
    let q = poly.as_array();
    if p.ncols() < 2 || q.ncols() < 2 || q.nrows() < 3 {
        return Err(PyValueError::new_err("clip_segments_convex: xy (n, 2), poly (k >= 3, 2)"));
    }
    let k = q.nrows();
    let pts: Vec<[f64; 2]> = (0..k).map(|i| [q[[i, 0]], q[[i, 1]]]).collect();
    let (mut cx, mut cy) = (0.0, 0.0);
    for v in &pts {
        cx += v[0];
        cy += v[1];
    }
    let c = [cx / k as f64, cy / k as f64];
    let edges: Vec<([f64; 2], [f64; 2], f64)> = (0..k).map(|e| {
        let (a, b) = (pts[e], pts[(e + 1) % k]);
        (a, b, if side(a, b, c) >= 0.0 { 1.0 } else { -1.0 })
    }).collect();
    let n = p.nrows().saturating_sub(1);
    let mut out = vec![f64::NAN; n * 2];
    for i in 0..n {
        let a = [p[[i, 0]], p[[i, 1]]];
        let b = [p[[i + 1, 0]], p[[i + 1, 1]]];
        let (mut t0, mut t1) = (0.0f64, 1.0f64);
        let mut inside = true;
        for &(e0, e1, sg) in &edges {
            let da = sg * side(e0, e1, a);
            let db = sg * side(e0, e1, b);
            if da < 0.0 && db < 0.0 {
                inside = false;
                break;
            }
            if da >= 0.0 && db >= 0.0 {
                continue;
            }
            let t = da / (da - db);
            if da < 0.0 {
                t0 = t0.max(t);
            } else {
                t1 = t1.min(t);
            }
            if t1 <= t0 {
                inside = false;
                break;
            }
        }
        if inside && t1 > t0 {
            out[2 * i] = t0;
            out[2 * i + 1] = t1;
        }
    }
    Ok(numpy::ndarray::Array2::from_shape_vec((n, 2), out).unwrap().into_pyarray(py))
}

// --- shapes (.cdae, server cdae.py: a msgpack body) -----------------------------------------------------------------

struct Rd<'a> {
    b: &'a [u8],
    i: usize,
}

fn be(b: &[u8], n: usize) -> u64 {
    let mut v = 0u64;
    for &x in &b[..n] {
        v = (v << 8) | x as u64;
    }
    v
}

impl<'a> Rd<'a> {
    fn need(&self, n: usize) -> Result<(), String> {
        if self.i + n > self.b.len() { Err(format!("truncated at {}", self.i)) } else { Ok(()) }
    }

    /// One value skipped; Some((start, len)) of a bin's or str's bytes, else None.
    fn skip(&mut self) -> Result<Option<(usize, usize)>, String> {
        self.need(1)?;
        let t = self.b[self.i];
        self.i += 1;
        let fixed = |n: usize, me: &mut Self| -> Result<Option<(usize, usize)>, String> {
            me.need(n)?;
            me.i += n;
            Ok(None)
        };
        match t {
            0x00..=0x7f | 0xe0..=0xff | 0xc0 | 0xc2 | 0xc3 => Ok(None),
            0xcc | 0xd0 => fixed(1, self),
            0xcd | 0xd1 => fixed(2, self),
            0xce | 0xd2 | 0xca => fixed(4, self),
            0xcf | 0xd3 | 0xcb => fixed(8, self),
            0xa0..=0xbf | 0xd9 | 0xda | 0xdb | 0xc4 | 0xc5 | 0xc6 => {
                let (n, s) = match t {
                    0xa0..=0xbf => ((t & 31) as usize, self.i),
                    0xd9 | 0xc4 => { self.need(1)?; (be(&self.b[self.i..], 1) as usize, self.i + 1) }
                    0xda | 0xc5 => { self.need(2)?; (be(&self.b[self.i..], 2) as usize, self.i + 2) }
                    _ => { self.need(4)?; (be(&self.b[self.i..], 4) as usize, self.i + 4) }
                };
                self.i = s;
                self.need(n)?;
                self.i += n;
                Ok(Some((s, n)))
            }
            0x90..=0x9f | 0xdc | 0xdd | 0x80..=0x8f | 0xde | 0xdf => {
                let (n, map) = match t {
                    0x90..=0x9f => ((t & 15) as usize, false),
                    0x80..=0x8f => ((t & 15) as usize, true),
                    0xdc | 0xde => { self.need(2)?; let n = be(&self.b[self.i..], 2) as usize; self.i += 2; (n, t == 0xde) }
                    _ => { self.need(4)?; let n = be(&self.b[self.i..], 4) as usize; self.i += 4; (n, t == 0xdf) }
                };
                for _ in 0..(if map { 2 * n } else { n }) {
                    self.skip()?;
                }
                Ok(None)
            }
            _ => Err(format!("unsupported msgpack type 0x{t:02x} at {}", self.i - 1)),
        }
    }

    fn int(&mut self) -> Result<i64, String> {
        self.need(1)?;
        let t = self.b[self.i];
        let start = self.i + 1;
        let v = match t {
            0x00..=0x7f => t as i64,
            0xe0..=0xff => t as i64 - 256,
            0xcc => { self.need(2)?; self.b[start] as i64 }
            0xcd => { self.need(3)?; be(&self.b[start..], 2) as i64 }
            0xce => { self.need(5)?; be(&self.b[start..], 4) as i64 }
            0xcf => { self.need(9)?; be(&self.b[start..], 8) as i64 }
            0xd0 => { self.need(2)?; self.b[start] as i8 as i64 }
            0xd1 => { self.need(3)?; be(&self.b[start..], 2) as u16 as i16 as i64 }
            0xd2 => { self.need(5)?; be(&self.b[start..], 4) as u32 as i32 as i64 }
            0xd3 => { self.need(9)?; be(&self.b[start..], 8) as i64 }
            0xca => { self.need(5)?; f32::from_bits(be(&self.b[start..], 4) as u32) as i64 }
            _ => return Err(format!("expected an int at {}", self.i)),
        };
        self.skip()?;
        Ok(v)
    }

    /// A vector (count, element size, bin): (count, size, start of its bytes).
    fn vector(&mut self) -> Result<(usize, usize, usize), String> {
        let n = self.int()? as usize;
        let s = self.int()? as usize;
        match self.skip()? {
            Some((at, len)) if len == n * s => Ok((n, s, at)),
            _ => Err(format!("bad vector at {}", self.i)),
        }
    }
}

const SHAPE_VECTORS: usize = 19;
const MESH_VECTORS: usize = 9;

/// The byte offsets and counts of every standard mesh's vertex array (12-byte float32 xyz) of a v31 shape.
fn vert_blocks(b: &[u8]) -> Result<Vec<(usize, usize)>, String> {
    if b.len() < 8 {
        return Err("shape too short".into());
    }
    let hsize = u32::from_le_bytes([b[4], b[5], b[6], b[7]]) as usize;
    let mut r = Rd { b, i: 8 + hsize };
    for _ in 0..6 {
        r.skip()?;          // smallestVisibleSize, DL, radius, tubeRadius, center, bounds
    }
    for _ in 0..SHAPE_VECTORS {
        r.vector()?;
    }
    let names = r.int()?;
    for _ in 0..names {
        r.skip()?;
    }
    let meshes = r.int()?;
    let mut out = Vec::new();
    for _ in 0..meshes {
        let kind = r.int()?;
        if kind == 4 {
            continue;
        }
        if kind != 0 {
            return Err(format!("mesh type {kind} not supported"));
        }
        for _ in 0..6 {
            r.skip()?;      // numFrames, numMatFrames, parentMesh, bounds, center, radius
        }
        let (n, s, at) = r.vector()?;
        if s != 12 {
            return Err(format!("vertex size {s}"));
        }
        out.push((at, n));
        for _ in 1..MESH_VECTORS {
            r.vector()?;
        }
        r.skip()?;
        r.skip()?;          // vertsPerFrame, flags
    }
    Ok(out)
}

fn vert(b: &[u8], at: usize, k: usize) -> [f32; 3] {
    let o = at + 12 * k;
    let f = |j: usize| f32::from_le_bytes([b[o + j], b[o + j + 1], b[o + j + 2], b[o + j + 3]]);
    [f(0), f(4), f(8)]
}

/// roadgeo: (vertices, median z, min z, max z) of a shape's standard meshes (every detail, the collision ones too);
/// (0, 0, 0, 0) without vertices.
#[pyfunction]
pub fn cdae_zstats(data: &[u8]) -> PyResult<(usize, f64, f64, f64)> {
    let blocks = vert_blocks(data).map_err(PyValueError::new_err)?;
    let mut zs: Vec<f64> = Vec::new();
    for (at, n) in blocks {
        for k in 0..n {
            zs.push(vert(data, at, k)[2] as f64);
        }
    }
    if zs.is_empty() {
        return Ok((0, 0.0, 0.0, 0.0));
    }
    let lo = zs.iter().cloned().fold(f64::INFINITY, f64::min);
    let hi = zs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let m = zs.len() / 2;
    zs.select_nth_unstable_by(m, |a, b| a.partial_cmp(b).unwrap());
    Ok((zs.len(), zs[m], lo, hi))
}

/// roadgeo.exact_variant: the shape with every vertex v moved by s (oz + v.z - pz) (oz: the object's position's z,
/// pz the pivot's: s = R^T c of geotiles.placement, in the shape's own axes). The rest of the file as it was (its
/// bounds stay the base shape's: the move is a few cm to dm).
#[pyfunction]
pub fn cdae_shear<'py>(py: Python<'py>, data: &[u8], oz: f64, sx: f64, sy: f64, sz: f64, pz: f64)
                       -> PyResult<Bound<'py, PyBytes>> {
    let blocks = vert_blocks(data).map_err(PyValueError::new_err)?;
    let mut out = data.to_vec();
    for (at, n) in blocks {
        for k in 0..n {
            let v = vert(data, at, k);
            let d = oz + v[2] as f64 - pz;
            let w = [(v[0] as f64 + sx * d) as f32, (v[1] as f64 + sy * d) as f32, (v[2] as f64 + sz * d) as f32];
            let o = at + 12 * k;
            for j in 0..3 {
                out[o + 4 * j..o + 4 * j + 4].copy_from_slice(&w[j].to_le_bytes());
            }
        }
    }
    Ok(PyBytes::new(py, &out))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn area(c: &Clipped) -> f64 {
        c.t.iter().map(|t| {
            let (a, b, d) = (c.p[t[0] as usize], c.p[t[1] as usize], c.p[t[2] as usize]);
            0.5 * ((b[0] - a[0]) * (d[1] - a[1]) - (b[1] - a[1]) * (d[0] - a[0])).abs()
        }).sum()
    }

    #[test]
    fn two_tiles_split_a_triangle_exactly() {
        // a triangle across the shared edge of two quads: the two halves add up to it
        let p = vec![[-3.0, -1.0, 0.0], [4.0, -0.5, 0.0], [0.5, 3.0, 0.0]];
        let uv = vec![[0.0, 0.0]; 3];
        let t = vec![[0i64, 1, 2]];
        let left = [[-10.0, -10.0], [0.3, -10.0], [-0.2, 10.0], [-10.0, 10.0]];
        let right = [[0.3, -10.0], [10.0, -10.0], [10.0, 10.0], [-0.2, 10.0]];
        let (a, b) = (clip_convex(&p, &uv, &t, &left), clip_convex(&p, &uv, &t, &right));
        let whole = 0.5 * ((7.0f64) * 4.0 - 0.5 * 3.5).abs();
        assert!((area(&a) + area(&b) - whole).abs() < 1e-9, "{} + {} vs {whole}", area(&a), area(&b));
    }
}
