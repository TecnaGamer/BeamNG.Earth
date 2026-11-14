//! A ground photo's whole paint in one call (earthfast 295, on 293; first 288 on 285: surface.paint_square,
//! server/paintlist.py): the crop rows (photo_fields), golf surfaces (photo_golf), parking lots and pads (photo_lots /
//! photo_pads), roundabout islands (_fill_island_photos), the carriageways in the photo's own tone (photo_roads) and
//! the rounded corners (photo_corners), from paint lists made once per set of map cells: each feature clipped, placed
//! and put in the anchor frame's metres by paintlist.py, then only offset and scaled to the square here. The main
//! process's Python made those lists again for every far square of a cell (44-153 sample-s a visit, under its
//! interpreter lock beside the event loop: research-2026-11-13-realworld-streaming.md proposal 5).
//!
//! The same pixels as the Python it replaces: every float expression in the Python's order and precision (numpy's
//! float32 blends rounded half to even, float64 coordinates), and Pillow 13's polygon and wide-line rasterisers ported
//! (src/libImaging/Draw.c: vertices truncated to integers, its scanline rules and float32 edge slopes), so a square
//! painted here is byte-identical to the Python's (tests/test_paintlist.py against tests/paint_reference.py).

use numpy::ndarray::{Array2, Array3, ArrayView2};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;

type Poly = Vec<[f64; 2]>;

/// Pillow's ImageDraw polygon fill and wide lines on an 8-bit canvas (Draw.c, Pillow 13.0: polygon_generic's non-alpha
/// path, ImagingDrawPolygon, ImagingDrawWideLine), bit for bit.
pub(crate) mod pil {
    #[derive(Clone, Copy)]
    pub struct Edge {
        x0: i32,
        y0: i32,
        xmin: i32,
        ymin: i32,
        xmax: i32,
        ymax: i32,
        dx: f32,
    }

    fn edge(x0: i32, y0: i32, x1: i32, y1: i32) -> Edge {
        let dx = if y0 == y1 { 0.0 } else { (x1 - x0) as f32 / (y1 - y0) as f32 };
        Edge { x0, y0, xmin: x0.min(x1), xmax: x0.max(x1), ymin: y0.min(y1), ymax: y0.max(y1), dx }
    }

    /// An 8-bit image (row-major, w x h) drawn into.
    pub struct Canvas<'a> {
        pub px: &'a mut [u8],
        pub w: i32,
        pub h: i32,
    }

    fn hline(c: &mut Canvas, mut x0: i32, y: i32, mut x1: i32, ink: u8) {
        if y < 0 || y >= c.h {
            return;
        }
        if x0 < 0 {
            x0 = 0;
        } else if x0 >= c.w {
            return;
        }
        if x1 < 0 {
            return;
        } else if x1 >= c.w {
            x1 = c.w - 1;
        }
        if x0 <= x1 {
            let row = (y * c.w) as usize;
            c.px[row + x0 as usize..=row + x1 as usize].fill(ink);
        }
    }

    fn point(c: &mut Canvas, x: i32, y: i32, ink: u8) {
        if x >= 0 && x < c.w && y >= 0 && y < c.h {
            c.px[(y * c.w + x) as usize] = ink;
        }
    }

    // ROUND_UP / ROUND_DOWN of a float (the positive branch adds in float, the negative one in double: fabs is double)
    fn round_up_f32(f: f32) -> i32 {
        if f >= 0.0 { ((f + 0.5f32) as f64).floor() as i32 } else { -((f as f64).abs() + 0.5).floor() as i32 }
    }

    fn round_down_f32(f: f32) -> i32 {
        if f >= 0.0 { ((f - 0.5f32) as f64).ceil() as i32 } else { -((f as f64).abs() - 0.5).ceil() as i32 }
    }

    fn round_up_f64(f: f64) -> i32 {
        if f >= 0.0 { (f + 0.5).floor() as i32 } else { -(f.abs() + 0.5).floor() as i32 }
    }

    fn round_down_f64(f: f64) -> i32 {
        if f >= 0.0 { (f - 0.5).ceil() as i32 } else { -(f.abs() - 0.5).ceil() as i32 }
    }

    fn at(e: &Edge, y: i32) -> f32 {
        (y - e.y0) as f32 * e.dx + e.x0 as f32
    }

    /// polygon_generic (the scanline fill; no alpha).
    fn polygon_generic(c: &mut Canvas, e: &[Edge], ink: u8) {
        if e.is_empty() {
            return;
        }
        let (mut ymin, mut ymax) = (c.h - 1, 0);
        let mut table: Vec<Edge> = Vec::with_capacity(e.len());
        for ed in e {
            ymin = ymin.min(ed.ymin);
            ymax = ymax.max(ed.ymax);
            if ed.ymin == ed.ymax {
                hline(c, ed.xmin, ed.ymin, ed.xmax, ink);
                continue;
            }
            table.push(*ed);
        }
        ymin = ymin.max(0);
        ymax = ymax.min(c.h);
        let mut xx: Vec<f32> = Vec::with_capacity(2 * table.len());
        let mut y = ymin;
        while y <= ymax {
            xx.clear();
            for i in 0..table.len() {
                let cur = table[i];
                if y < cur.ymin || y > cur.ymax {
                    continue;
                }
                xx.push(at(&cur, y));
                if y == cur.ymax && y < ymax {
                    let v = xx[xx.len() - 1];
                    xx.push(v);
                } else if (y == cur.ymin || y == cur.ymax) && cur.dx != 0.0 {
                    // (connect discontiguous corners)
                    for o in &table[..i] {
                        if (y != o.ymin && y != o.ymax) || o.dx == 0.0 {
                            continue;
                        }
                        let last = xx[xx.len() - 1];
                        if last.round() == at(o, y).round() {
                            let offset = if y == cur.ymax { -1 } else { 1 };
                            let adj = at(&cur, y + offset);
                            if y + offset >= o.ymin && y + offset <= o.ymax {
                                let adj_o = at(o, y + offset);
                                let n = xx.len() - 1;
                                if last > adj + 1.0 && last > adj_o + 1.0 {
                                    xx[n] = adj.max(adj_o).round() + 1.0;
                                } else if last < adj - 1.0 && last < adj_o - 1.0 {
                                    xx[n] = adj.min(adj_o).round() - 1.0;
                                }
                                break;
                            }
                        }
                    }
                }
            }
            xx.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let mut i = 1;
            while i < xx.len() {
                hline(c, round_up_f32(xx[i - 1]), y, round_down_f32(xx[i]), ink);
                i += 2;
            }
            y += 1;
        }
    }

    /// ImageDraw.polygon(xy, fill=ink): the vertices truncated to integers as _imaging's draw_polygon does.
    pub fn polygon(c: &mut Canvas, xy: &[(f64, f64)], ink: u8) {
        let n = xy.len();
        if n < 2 {
            return;
        }
        let p: Vec<(i32, i32)> = xy.iter().map(|&(x, y)| (x as i32, y as i32)).collect();
        let mut e: Vec<Edge> = Vec::with_capacity(n);
        for i in 0..n - 1 {
            let ((x0, y0), (x1, y1)) = (p[i], p[i + 1]);
            if y0 == y1 && i != 0 && y0 == p[i - 1].1 {
                // a horizontal line straight after another one
                let last = e.len() - 1;
                if x1 > x0 && x0 > p[i - 1].0 {
                    e[last].xmax = x1;
                    continue;
                } else if x1 < x0 && x0 < p[i - 1].0 {
                    e[last].xmin = x1;
                    continue;
                }
            }
            e.push(edge(x0, y0, x1, y1));
        }
        if p[n - 1] != p[0] {
            e.push(edge(p[n - 1].0, p[n - 1].1, p[0].0, p[0].1));
        }
        polygon_generic(c, &e, ink);
    }

    /// ImagingDrawWideLine (one segment of ImageDraw.line(xy, width=width > 1)).
    fn wide_line(c: &mut Canvas, x0: i32, y0: i32, x1: i32, y1: i32, width: i32, ink: u8) {
        let (dx, dy) = (x1 - x0, y1 - y0);
        if dx == 0 && dy == 0 {
            point(c, x0, y0, ink);
            return;
        }
        // (hypot of two integers: the exact sum's square root, the correctly rounded hypot)
        let big = ((dx as f64) * (dx as f64) + (dy as f64) * (dy as f64)).sqrt();
        let small = (width - 1) as f64 / 2.0;
        let ratio_max = round_up_f64(small) as f64 / big;
        let ratio_min = round_down_f64(small) as f64 / big;
        let dxmin = round_down_f64(ratio_min * dy as f64);
        let dxmax = round_down_f64(ratio_max * dy as f64);
        let dymin = round_down_f64(ratio_min * dx as f64);
        let dymax = round_down_f64(ratio_max * dx as f64);
        let v = [(x0 - dxmin, y0 + dymax), (x1 - dxmin, y1 + dymax), (x1 + dxmax, y1 - dymin), (x0 + dxmax, y0 - dymin)];
        let e = [edge(v[0].0, v[0].1, v[1].0, v[1].1), edge(v[1].0, v[1].1, v[2].0, v[2].1),
                 edge(v[2].0, v[2].1, v[3].0, v[3].1), edge(v[3].0, v[3].1, v[0].0, v[0].1)];
        polygon_generic(c, &e, ink);
    }

    /// ImageDraw.line(xy, fill=ink, width=width) for width > 1 (each segment's ends truncated to integers).
    pub fn wide_lines(c: &mut Canvas, xy: &[(f64, f64)], width: i32, ink: u8) {
        for k in 0..xy.len().saturating_sub(1) {
            let (a, b) = (xy[k], xy[k + 1]);
            wide_line(c, a.0 as i32, a.1 as i32, b.0 as i32, b.1 as i32, width, ink);
        }
    }
}

// --- the lists (paintlist.py makes them, once per set of map cells) ------------------------------------------------

fn polys_from(xy: &PyReadonlyArray2<'_, f64>, off: &PyReadonlyArray1<'_, i64>) -> PyResult<Vec<Poly>> {
    let xy = xy.as_array();
    let off = off.as_array();
    if xy.ncols() != 2 || off.is_empty() || off[off.len() - 1] as usize != xy.nrows() {
        return Err(pyo3::exceptions::PyValueError::new_err("paint list: xy (n, 2) and offsets (k + 1,) ending at n"));
    }
    let mut out = Vec::with_capacity(off.len() - 1);
    for k in 0..off.len() - 1 {
        let (a, b) = (off[k] as usize, off[k + 1] as usize);
        if a > b || b > xy.nrows() {
            return Err(pyo3::exceptions::PyValueError::new_err("paint list: offsets out of order"));
        }
        out.push((a..b).map(|i| [xy[[i, 0]], xy[[i, 1]]]).collect());
    }
    Ok(out)
}

/// Polygons in the anchor frame's metres (east, north): roundabout footprints, parking lots, a golf layer, corner pads,
/// gas station pads (paintlist.py), as concatenated points and offsets.
#[pyclass(module = "earthfast", frozen)]
pub struct PaintPolys {
    polys: Vec<Poly>,
}

#[pymethods]
impl PaintPolys {
    #[new]
    fn new(xy: PyReadonlyArray2<'_, f64>, off: PyReadonlyArray1<'_, i64>) -> PyResult<Self> {
        Ok(PaintPolys { polys: polys_from(&xy, &off)? })
    }

    fn __len__(&self) -> usize {
        self.polys.len()
    }
}

/// The carriageways of a set of cells as surface._road_segments reads them (each feature once, its ground parts placed:
/// vertices in metres, the tapered half widths per vertex, its box for the square's first test) and the ramp gores'
/// paved wedges.
#[pyclass(module = "earthfast", frozen)]
pub struct PaintRoads {
    v: Vec<[f64; 2]>,           // ring vertices
    hp: Vec<f64>,               // per vertex: tapered width / 2 + edge (m), NaN on a ring without tapers
    ring_off: Vec<usize>,       // ring k: v[ring_off[k]..ring_off[k + 1]]
    ring_kind: Vec<u8>,
    ring_taper: Vec<bool>,
    ring_ghost: Vec<bool>,      // its road paved without curbs (photo_ghosts)
    feat_ring: Vec<usize>,      // feature f: rings feat_ring[f]..feat_ring[f + 1]
    feat_box: Vec<[f64; 4]>,    // east min / max, north min / max of the feature's own coordinates
    feat_m: Vec<f64>,           // width / 2
    feat_hwm: Vec<f64>,         // width / 2 + edge
    gores: Vec<(Poly, Vec<f64>)>,   // middle line, half width per point (m)
}

#[pymethods]
impl PaintRoads {
    /// v (n, 2) / hp (n,) / ring_off (r + 1,) / ring_kind / ring_taper / ring_ghost (r,); feat_ring (f + 1,) /
    /// feat_box (f, 4) / feat_m / feat_hwm (f,); gore_xy (g, 2) / gore_hw (g,) / gore_off (k + 1,).
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(v: PyReadonlyArray2<'_, f64>, hp: PyReadonlyArray1<'_, f64>, ring_off: PyReadonlyArray1<'_, i64>,
           ring_kind: PyReadonlyArray1<'_, u8>, ring_taper: PyReadonlyArray1<'_, bool>,
           ring_ghost: PyReadonlyArray1<'_, bool>, feat_ring: PyReadonlyArray1<'_, i64>,
           feat_box: PyReadonlyArray2<'_, f64>, feat_m: PyReadonlyArray1<'_, f64>, feat_hwm: PyReadonlyArray1<'_, f64>,
           gore_xy: PyReadonlyArray2<'_, f64>, gore_hw: PyReadonlyArray1<'_, f64>,
           gore_off: PyReadonlyArray1<'_, i64>) -> PyResult<Self> {
        let bad = |what: &str| Err(pyo3::exceptions::PyValueError::new_err(format!("PaintRoads: {what}")));
        let (va, hpa) = (v.as_array(), hp.as_array());
        if va.ncols() != 2 || hpa.len() != va.nrows() {
            return bad("v (n, 2), hp (n,)");
        }
        let ring_off: Vec<usize> = ring_off.as_array().iter().map(|&x| x as usize).collect();
        let r = ring_off.len().saturating_sub(1);
        if ring_off.is_empty() || ring_off[r] != va.nrows() || ring_off.windows(2).any(|p| p[0] > p[1])
            || ring_kind.len()? != r || ring_taper.len()? != r || ring_ghost.len()? != r {
            return bad("ring offsets and per-ring arrays");
        }
        let feat_ring: Vec<usize> = feat_ring.as_array().iter().map(|&x| x as usize).collect();
        let f = feat_ring.len().saturating_sub(1);
        let fb = feat_box.as_array();
        if feat_ring.is_empty() || feat_ring[f] != r || feat_ring.windows(2).any(|p| p[0] > p[1]) || fb.nrows() != f
            || fb.ncols() != 4 || feat_m.len()? != f || feat_hwm.len()? != f {
            return bad("feature offsets and per-feature arrays");
        }
        let ghw = gore_hw.as_array();
        let gores = polys_from(&gore_xy, &gore_off)?;
        if ghw.len() != gore_xy.as_array().nrows() {
            return bad("gore_hw (g,)");
        }
        let goff = gore_off.as_array();
        let gores = gores.into_iter().enumerate()
            .map(|(k, p)| (p, ghw.slice(numpy::ndarray::s![goff[k] as usize..goff[k + 1] as usize]).to_vec())).collect();
        Ok(PaintRoads {
            v: (0..va.nrows()).map(|i| [va[[i, 0]], va[[i, 1]]]).collect(),
            hp: hpa.to_vec(),
            ring_off,
            ring_kind: ring_kind.as_array().to_vec(),
            ring_taper: ring_taper.as_array().to_vec(),
            ring_ghost: ring_ghost.as_array().to_vec(),
            feat_ring,
            feat_box: (0..f).map(|i| [fb[[i, 0]], fb[[i, 1]], fb[[i, 2]], fb[[i, 3]]]).collect(),
            feat_m: feat_m.as_array().to_vec(),
            feat_hwm: feat_hwm.as_array().to_vec(),
            gores,
        })
    }

    /// surface._road_segments of a w x h photo over [x0, x0 + size]²: (segs (m, 4), half widths (m,), kind (m,), road
    /// number (m,), its road paved without curbs (m,)) in pixels, or None when none is in it.
    #[allow(clippy::type_complexity)]
    fn segments<'py>(&self, py: Python<'py>, x0: f64, y0: f64, size: f64, w: usize, h: usize)
                     -> Option<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<u8>>,
                                Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<bool>>)> {
        let s = py.detach(|| self.cut(x0, y0, size, w, h));
        if s.kind.is_empty() {
            return None;
        }
        let m = s.kind.len();
        let segs = Array2::from_shape_vec((m, 4), s.segs.into_iter().flatten().collect()).unwrap();
        Some((segs.into_pyarray(py), s.hw.into_pyarray(py), s.kind.into_pyarray(py), s.road.into_pyarray(py),
              s.ghost.into_pyarray(py)))
    }
}

struct Segs {
    segs: Vec<[f64; 4]>,
    hw: Vec<f64>,
    kind: Vec<u8>,
    road: Vec<i64>,
    ghost: Vec<bool>,
}

impl PaintRoads {
    /// _road_segments' loop for one square (its two tests: the feature's box, then each ring's pixels).
    fn cut(&self, x0: f64, y0: f64, size: f64, w: usize, h: usize) -> Segs {
        let px_m = w as f64 / size;
        let top = y0 + size;
        let (wf, hf) = (w as f64, h as f64);
        let mut out = Segs { segs: Vec::new(), hw: Vec::new(), kind: Vec::new(), road: Vec::new(), ghost: Vec::new() };
        let mut nroad = 0i64;
        let mut c: Vec<f64> = Vec::new();
        let mut r: Vec<f64> = Vec::new();
        for f in 0..self.feat_m.len() {
            let (b, m) = (self.feat_box[f], self.feat_m[f]);
            if b[1] < x0 - m || b[0] > x0 + size + m || b[3] < y0 - m || b[2] > y0 + size + m {
                continue;
            }
            let hw = self.feat_hwm[f] * px_m;
            for k in self.feat_ring[f]..self.feat_ring[f + 1] {
                let pts = &self.v[self.ring_off[k]..self.ring_off[k + 1]];
                if pts.len() < 2 {
                    continue;
                }
                c.clear();
                r.clear();
                for p in pts {
                    c.push((p[0] - x0) * px_m);
                    r.push((top - p[1]) * px_m);
                }
                let (cmin, cmax) = c.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &v| (a.0.min(v), a.1.max(v)));
                let (rmin, rmax) = r.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &v| (a.0.min(v), a.1.max(v)));
                if cmax < -hw || cmin > wf + hw || rmax < -hw || rmin > hf + hw {
                    continue;
                }
                let hp = &self.hp[self.ring_off[k]..self.ring_off[k + 1]];
                for i in 0..pts.len() - 1 {
                    out.segs.push([c[i], r[i], c[i + 1], r[i + 1]]);
                    out.hw.push(if self.ring_taper[k] { (hp[i] * px_m + hp[i + 1] * px_m) / 2.0 } else { hw });
                    out.kind.push(self.ring_kind[k]);
                    out.road.push(nroad);
                    out.ghost.push(self.ring_ghost[k]);
                }
            }
            nroad += 1;
        }
        // ramp gores: more paved segments (gore_fills), each its own road. The cells' gores are all here (the Python
        // took those of the roads within 300 m of the square, each gore that reaches it among them): one that cannot
        // touch the photo (its line's box grown by its widest half width and the antialiased pixel, line_cover's
        // pad) is left out, as no pixel of it would be painted
        for (mid, hw) in &self.gores {
            let pad = hw.iter().fold(0f64, |a, &v| a.max(v)) * px_m + 1.0;
            let (mut cmin, mut cmax, mut rmin, mut rmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
            for p in mid {
                let (c, r) = ((p[0] - x0) * px_m, (top - p[1]) * px_m);
                cmin = cmin.min(c);
                cmax = cmax.max(c);
                rmin = rmin.min(r);
                rmax = rmax.max(r);
            }
            if mid.len() < 2 || cmax + pad <= 0.0 || cmin - pad >= wf || rmax + pad <= 0.0 || rmin - pad >= hf {
                continue;
            }
            for i in 0..mid.len().saturating_sub(1) {
                out.segs.push([(mid[i][0] - x0) * px_m, (top - mid[i][1]) * px_m, (mid[i + 1][0] - x0) * px_m,
                               (top - mid[i + 1][1]) * px_m]);
                out.hw.push(hw[i].max(hw[i + 1]) * px_m);
                out.kind.push(PAVED_KIND);
                out.road.push(nroad);
                out.ghost.push(false);
            }
            nroad += 1;
        }
        out
    }
}

/// The paved kind's index in surface.PHOTO_ROAD_RGBA's order (dirt, gravel, cobble, concrete, paved).
const PAVED_KIND: u8 = 4;

/// Farmland outer rings with their furrows' phase and direction (photo_fields: phase, v0, v1, the ring's centre), and
/// the dirt roads' ground parts kept clear of furrows (_dirt_road_mask: their width + both paint edges + both clear
/// strips, m).
#[pyclass(module = "earthfast", frozen)]
pub struct PaintFields {
    fields: Vec<(Poly, [f64; 5])>,
    dirt: Vec<(Poly, f64)>,
}

#[pymethods]
impl PaintFields {
    #[new]
    fn new(xy: PyReadonlyArray2<'_, f64>, off: PyReadonlyArray1<'_, i64>, params: PyReadonlyArray2<'_, f64>,
           dirt_xy: PyReadonlyArray2<'_, f64>, dirt_off: PyReadonlyArray1<'_, i64>,
           dirt_w: PyReadonlyArray1<'_, f64>) -> PyResult<Self> {
        let polys = polys_from(&xy, &off)?;
        let pa = params.as_array();
        if pa.nrows() != polys.len() || pa.ncols() != 5 {
            return Err(pyo3::exceptions::PyValueError::new_err("PaintFields: params (k, 5)"));
        }
        let lines = polys_from(&dirt_xy, &dirt_off)?;
        let dw = dirt_w.as_array();
        if dw.len() != lines.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("PaintFields: dirt_w (k,)"));
        }
        Ok(PaintFields {
            fields: polys.into_iter().enumerate()
                .map(|(k, p)| (p, [pa[[k, 0]], pa[[k, 1]], pa[[k, 2]], pa[[k, 3]], pa[[k, 4]]])).collect(),
            dirt: lines.into_iter().zip(dw.iter().copied()).collect(),
        })
    }
}

/// surface's paint constants (paintlist.style): the road kinds' colours (k, 4) and tone rows (k, 9) in
/// PHOTO_ROAD_RGBA's order, and the scalars (paintlist.STYLE_KEYS' order).
#[pyclass(module = "earthfast", frozen)]
pub struct PaintStyle {
    colors: Array2<f32>,
    tone: Array2<f32>,
    c: Vec<f64>,
}

// PaintStyle.c's slots (paintlist.STYLE_KEYS)
const S_RADIUS_M: usize = 0;
const S_LUMA0: usize = 1;
const S_LUMA1: usize = 2;
const S_SAT: usize = 3;
const S_GREEN: usize = 4;
const S_ISLAND_MIN_GREEN: usize = 5;
const S_ISLAND_LAWN_SHARE: usize = 6;
const S_ISLAND_GRASS: usize = 7;     // 3
const S_ISLAND_LAWN: usize = 10;     // 3
const S_LAWN_GREEN: usize = 13;
const S_FARM_ROW_M: usize = 14;
const S_FARM_FURROW_M: usize = 15;
const S_FARM_MIX: usize = 16;
const S_FARM_SOIL: usize = 17;       // 3
const S_GOLF: usize = 20;            // 4 layers x (r, g, b, opacity)
const S_LEN: usize = 36;

#[pymethods]
impl PaintStyle {
    #[new]
    fn new(colors: PyReadonlyArray2<'_, f32>, tone: PyReadonlyArray2<'_, f32>, c: Vec<f64>) -> PyResult<Self> {
        let (colors, tone) = (colors.as_array().to_owned(), tone.as_array().to_owned());
        if colors.ncols() != 4 || tone.nrows() != colors.nrows() || tone.ncols() != 9 || colors.nrows() <= PAVED_KIND as usize
            || c.len() != S_LEN {
            return Err(pyo3::exceptions::PyValueError::new_err("PaintStyle: colors (5, 4), tone (5, 9), 36 scalars"));
        }
        Ok(PaintStyle { colors, tone, c })
    }
}

impl PaintStyle {
    fn tone_args(&self) -> ((f32, f32), f32, f32) {
        ((self.c[S_LUMA0] as f32, self.c[S_LUMA1] as f32), self.c[S_SAT] as f32, self.c[S_GREEN] as f32)
    }

    /// int(round(PHOTO_TONE_RADIUS_M * w / size)) at least 1 (Python's round: half to even).
    fn radius(&self, w: usize, size: f64) -> usize {
        let v = (self.c[S_RADIUS_M] * w as f64 / size).round_ties_even();
        (v.max(1.0)) as usize
    }
}

// --- the steps ----------------------------------------------------------------------------------------------------

struct Sq {
    x0: f64,
    y0: f64,
    size: f64,
    w: usize,
    h: usize,
}

impl Sq {
    fn top(&self) -> f64 {
        self.y0 + self.size
    }

    /// A polygon's pixel coordinates (c, r) at px_x / px_y pixels a metre, and whether it reaches the photo (the
    /// Python's "c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h": left out).
    fn px(&self, p: &Poly, px_x: f64, px_y: f64) -> (Vec<(f64, f64)>, bool) {
        let top = self.top();
        let pts: Vec<(f64, f64)> = p.iter().map(|q| ((q[0] - self.x0) * px_x, (top - q[1]) * px_y)).collect();
        let (mut cmin, mut cmax, mut rmin, mut rmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for &(c, r) in &pts {
            cmin = cmin.min(c);
            cmax = cmax.max(c);
            rmin = rmin.min(r);
            rmax = rmax.max(r);
        }
        let inside = !pts.is_empty() && !(cmax < 0.0 || cmin > self.w as f64 || rmax < 0.0 || rmin > self.h as f64);
        (pts, inside)
    }
}

fn pix(img: &Array3<u8>, j: usize, i: usize) -> [u8; 3] {
    [img[[j, i, 0]], img[[j, i, 1]], img[[j, i, 2]]]
}

/// photo_fields: dirt furrows across each field (the photo between them), none on a dirt road and its clear strip.
fn step_fields(img: &mut Array3<u8>, sq: &Sq, f: &PaintFields, st: &PaintStyle) {
    if f.fields.is_empty() {
        return;
    }
    let (w, h) = (sq.w, sq.h);
    let (px_x, px_y) = (w as f64 / sq.size, h as f64 / sq.size);
    let mut dirt: Option<Option<Vec<u8>>> = None;
    let row_m = st.c[S_FARM_ROW_M];
    let furrow_m = st.c[S_FARM_FURROW_M];
    let mix = st.c[S_FARM_MIX];
    let keep = (1.0 - mix) as f32;
    let soil: [f32; 3] = [0, 1, 2].map(|k| st.c[S_FARM_SOIL + k] as f32 * mix as f32);
    for (poly, prm) in &f.fields {
        let (pts, inside) = sq.px(poly, px_x, px_y);
        if !inside {
            continue;
        }
        let (mut cmin, mut cmax, mut rmin, mut rmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for &(c, r) in &pts {
            cmin = cmin.min(c);
            cmax = cmax.max(c);
            rmin = rmin.min(r);
            rmax = rmax.max(r);
        }
        let c0 = (cmin.floor() as i64).max(0);
        let r0 = (rmin.floor() as i64).max(0);
        let c1 = (cmax.ceil() as i64 + 1).min(w as i64);
        let r1 = (rmax.ceil() as i64 + 1).min(h as i64);
        if c1 <= c0 || r1 <= r0 {
            continue;
        }
        let (ww, wh) = ((c1 - c0) as usize, (r1 - r0) as usize);
        let mut mask = vec![0u8; ww * wh];
        let local: Vec<(f64, f64)> = pts.iter().map(|&(c, r)| (c - c0 as f64, r - r0 as f64)).collect();
        pil::polygon(&mut pil::Canvas { px: &mut mask, w: ww as i32, h: wh as i32 }, &local, 255);
        if dirt.is_none() {
            dirt = Some(dirt_mask(sq, f, px_x, px_y));
        }
        let dm = dirt.as_ref().unwrap();
        let [phase, v0, v1, cx, cy] = *prm;
        let mut furrow = vec![false; ww * wh];
        let mut any = false;
        for jj in 0..wh {
            let nn = sq.top() - ((r0 as usize + jj) as f64 + 0.5) / px_y;
            for ii in 0..ww {
                if mask[jj * ww + ii] == 0 {
                    continue;
                }
                if let Some(d) = dm {
                    if d[(r0 as usize + jj) * w + c0 as usize + ii] != 0 {
                        continue;
                    }
                }
                let ee = sq.x0 + ((c0 as usize + ii) as f64 + 0.5) / px_x;
                let across = (nn - cy) * v1 + (ee - cx) * v0;
                // np.mod: fmod with the divisor's sign
                let mut md = (across - phase) % row_m;
                if md != 0.0 && md < 0.0 {
                    md += row_m;
                }
                if md < furrow_m {
                    furrow[jj * ww + ii] = true;
                    any = true;
                }
            }
        }
        if !any {
            continue;
        }
        for jj in 0..wh {
            for ii in 0..ww {
                if !furrow[jj * ww + ii] {
                    continue;
                }
                let (j, i) = (r0 as usize + jj, c0 as usize + ii);
                for k in 0..3 {
                    let v = img[[j, i, k]] as f32 * keep + soil[k];
                    img[[j, i, k]] = v.round_ties_even().clamp(0.0, 255.0) as u8;
                }
            }
        }
    }
}

/// _dirt_road_mask: the dirt roads drawn as PIL's wide lines (their clear strips included), or None.
fn dirt_mask(sq: &Sq, f: &PaintFields, px_x: f64, px_y: f64) -> Option<Vec<u8>> {
    let (w, h) = (sq.w, sq.h);
    let mut out: Option<Vec<u8>> = None;
    for (line, wm) in &f.dirt {
        if line.len() < 2 {
            continue;
        }
        let width_px = ((wm * px_x.max(px_y)).ceil() as i64).max(1) + 1;
        let wp = width_px as f64;
        let (pts, _) = sq.px(line, px_x, px_y);
        let (mut cmin, mut cmax, mut rmin, mut rmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for &(c, r) in &pts {
            cmin = cmin.min(c);
            cmax = cmax.max(c);
            rmin = rmin.min(r);
            rmax = rmax.max(r);
        }
        if cmax < -wp || cmin > w as f64 + wp || rmax < -wp || rmin > h as f64 + wp {
            continue;
        }
        let buf = out.get_or_insert_with(|| vec![0u8; w * h]);
        pil::wide_lines(&mut pil::Canvas { px: buf, w: w as i32, h: h as i32 }, &pts, width_px as i32, 255);
    }
    out
}

/// photo_golf: each layer's polygons blended over the photo at its colour and opacity (later layers over earlier ones).
fn step_golf(img: &mut Array3<u8>, sq: &Sq, st: &PaintStyle, py_polys: &[Option<&PaintPolys>]) {
    let (w, h) = (sq.w, sq.h);
    let (px_x, px_y) = (w as f64 / sq.size, h as f64 / sq.size);
    for (q, layer) in py_polys.iter().enumerate() {
        let Some(layer) = layer else { continue };
        if layer.polys.is_empty() {
            continue;
        }
        let mut mask = vec![0u8; w * h];
        let mut n = 0;
        for p in &layer.polys {
            let (pts, inside) = sq.px(p, px_x, px_y);
            if !inside {
                continue;
            }
            pil::polygon(&mut pil::Canvas { px: &mut mask, w: w as i32, h: h as i32 }, &pts, 255);
            n += 1;
        }
        if n == 0 {
            continue;
        }
        let colour = &st.c[S_GOLF + 4 * q..S_GOLF + 4 * q + 4];
        let a = (255f32 / 255.0) * colour[3] as f32;
        let paint = [colour[0] as f32, colour[1] as f32, colour[2] as f32];
        for j in 0..h {
            for i in 0..w {
                if mask[j * w + i] == 0 {
                    continue;
                }
                for k in 0..3 {
                    let v = img[[j, i, k]] as f32 * (1.0 - a) + paint[k] * a;
                    img[[j, i, k]] = v.round_ties_even().clamp(0.0, 255.0) as u8;
                }
            }
        }
    }
}

/// photo_pads (and photo_lots): the pads' 2 x 2 sub-pixel coverage painted as the paved roads in the photo's tone.
fn step_pads(img: &mut Array3<u8>, sq: &Sq, polys: &PaintPolys, st: &PaintStyle) {
    let (w, h) = (sq.w, sq.h);
    let (px_x, px_y) = (w as f64 / sq.size, h as f64 / sq.size);
    let mut cr: Vec<(Vec<f64>, Vec<f64>)> = Vec::new();
    for p in &polys.polys {
        let (pts, inside) = sq.px(p, px_x, px_y);
        if inside {
            cr.push((pts.iter().map(|q| q.0).collect(), pts.iter().map(|q| q.1).collect()));
        }
    }
    if cr.is_empty() {
        return;
    }
    let Some((r0, c0, nr, nc, bits)) = crate::groundpaint::pad_bits(h, w, &cr) else { return };
    let mut cov = vec![0f32; h * w];
    let mut any = false;
    for r in 0..nr {
        for c in 0..nc {
            let b = bits[r * nc + c];
            if b != 0 {
                cov[(r0 + r) * w + c0 + c] = (b.count_ones() as u8 as f32 / 4.0).clamp(0.0, 1.0);
                any = true;
            }
        }
    }
    if !any {
        return;
    }
    let pk = PAVED_KIND as usize;
    let col = st.colors.slice(numpy::ndarray::s![pk..pk + 1, ..]);
    let tn = st.tone.slice(numpy::ndarray::s![pk..pk + 1, ..]);
    let (luma, sat, green) = st.tone_args();
    *img = crate::roadtone::tone_paint(img.view(), &cov, 1, col, tn, st.radius(w, sq.size), luma, sat, green);
}

/// _fill_island_photos: each roundabout footprint the photo shows green filled with its greens' median pulled toward a
/// lawn (ISLAND_GRASS where too few are green), a paved one left as it is.
fn step_islands(img: &mut Array3<u8>, sq: &Sq, polys: &PaintPolys, st: &PaintStyle) {
    let (w, h) = (sq.w, sq.h);
    let px_m = w as f64 / sq.size;
    let shapes: Vec<Vec<(f64, f64)>> = polys.polys.iter().filter_map(|p| {
        let (pts, inside) = sq.px(p, px_m, px_m);
        inside.then_some(pts)
    }).collect();
    if shapes.is_empty() {
        return;
    }
    let orig = img.clone();
    let lawn_green = st.c[S_LAWN_GREEN] as i32;
    let green: Vec<bool> = (0..h * w).map(|p| {
        let q = pix(&orig, p / w, p % w);
        2 * q[1] as i32 - q[0] as i32 - q[2] as i32 >= lawn_green
    }).collect();
    let share = st.c[S_ISLAND_LAWN_SHARE];
    let mut mask = vec![0u8; w * h];
    for pts in &shapes {
        mask.fill(0);
        pil::polygon(&mut pil::Canvas { px: &mut mask, w: w as i32, h: h as i32 }, pts, 255);
        let (mut nm, mut ns) = (0usize, 0usize);
        let mut sel: [Vec<u8>; 3] = [Vec::new(), Vec::new(), Vec::new()];
        for p in 0..w * h {
            if mask[p] == 0 {
                continue;
            }
            nm += 1;
            if green[p] {
                ns += 1;
                let q = pix(&orig, p / w, p % w);
                for k in 0..3 {
                    sel[k].push(q[k]);
                }
            }
        }
        if nm == 0 || (ns as f64) < st.c[S_ISLAND_MIN_GREEN] * nm as f64 {
            continue;
        }
        let base: [f64; 3] = if ns >= 8 {
            // np.median of uint8 (float64), int() truncating
            [0, 1, 2].map(|k| {
                let v = &mut sel[k];
                v.sort_unstable();
                let n = v.len();
                let med = if n % 2 == 1 { v[n / 2] as f64 } else { (v[n / 2 - 1] as f64 + v[n / 2] as f64) / 2.0 };
                med.trunc()
            })
        } else {
            [0, 1, 2].map(|k| st.c[S_ISLAND_GRASS + k])
        };
        let fill: [u8; 3] = [0, 1, 2].map(|k| {
            (base[k] * (1.0 - share) + st.c[S_ISLAND_LAWN + k] * share).round_ties_even() as i64 as u8
        });
        for p in 0..w * h {
            if mask[p] != 0 {
                for k in 0..3 {
                    img[[p / w, p % w, k]] = fill[k];
                }
            }
        }
    }
}

/// photo_corners: each corner pad drawn at 2 x 2 in its own window, blended over the roads at the median colour of
/// the painted road pixels in its window (the flat paved colour where fewer than 6).
fn step_corners(img: &mut Array3<u8>, before: &Array3<u8>, sq: &Sq, polys: &PaintPolys, st: &PaintStyle) {
    let (w, h) = (sq.w as i64, sq.h as i64);
    let k = 2i64;
    let px_m = (w * k) as f64 / sq.size;
    let painted_in = img.clone();       // (rgb: the windows' colours come from the roads' paint, not the corners')
    let pk = PAVED_KIND as usize;
    let flat = [st.colors[[pk, 0]], st.colors[[pk, 1]], st.colors[[pk, 2]]];
    let op = st.colors[[pk, 3]];
    let top = sq.top();
    let floor_div = |a: i64, b: i64| a.div_euclid(b);
    for p in &polys.polys {
        if p.is_empty() {
            continue;
        }
        let c: Vec<f64> = p.iter().map(|q| (q[0] - sq.x0) * px_m).collect();
        let r: Vec<f64> = p.iter().map(|q| (top - q[1]) * px_m).collect();
        let (cmin, cmax) = c.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &v| (a.0.min(v), a.1.max(v)));
        let (rmin, rmax) = r.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &v| (a.0.min(v), a.1.max(v)));
        let c0 = (floor_div(cmin as i64, k) * k).max(0);
        let r0 = (floor_div(rmin as i64, k) * k).max(0);
        let c1 = (w * k).min(-floor_div(-((cmax + 1.0) as i64), k) * k);
        let r1 = (h * k).min(-floor_div(-((rmax + 1.0) as i64), k) * k);
        if c1 <= c0 || r1 <= r0 {
            continue;
        }
        let (ww, wh) = ((c1 - c0) as usize, (r1 - r0) as usize);
        let mut mask = vec![0u8; ww * wh];
        let local: Vec<(f64, f64)> = c.iter().zip(&r).map(|(&a, &b)| (a - c0 as f64, b - r0 as f64)).collect();
        pil::polygon(&mut pil::Canvas { px: &mut mask, w: ww as i32, h: wh as i32 }, &local, 255);
        let (sh, sw) = (wh / 2, ww / 2);
        let (pr0, pc0) = ((r0 / k) as usize, (c0 / k) as usize);
        // the window's coverage: the 2 x 2 mean / 255 (exact quarters)
        let a: Vec<f32> = (0..sh * sw).map(|q| {
            let (y, x) = (q / sw, q % sw);
            let n = [(0, 0), (0, 1), (1, 0), (1, 1)].iter()
                .filter(|&&(dy, dx)| mask[(2 * y + dy) * ww + 2 * x + dx] != 0).count();
            n as f32 * 255.0 / 4.0 / 255.0
        }).collect();
        let mut colour = flat;
        let mut sel: [Vec<f32>; 3] = [Vec::new(), Vec::new(), Vec::new()];
        for y in 0..sh {
            for x in 0..sw {
                let (j, i) = (pr0 + y, pc0 + x);
                if a[y * sw + x] < 0.5 && pix(&painted_in, j, i) != pix(before, j, i) {
                    let q = pix(&painted_in, j, i);
                    for ch in 0..3 {
                        sel[ch].push(q[ch] as f32);
                    }
                }
            }
        }
        if sel[0].len() >= 6 {
            colour = [0, 1, 2].map(|ch| {
                let v = &mut sel[ch];
                v.sort_by(|a, b| a.partial_cmp(b).unwrap());
                let n = v.len();
                if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 }
            });
        }
        for y in 0..sh {
            for x in 0..sw {
                let s = a[y * sw + x] * op;
                let (j, i) = (pr0 + y, pc0 + x);
                for ch in 0..3 {
                    let v = img[[j, i, ch]] as f32 * (1.0 - s) + colour[ch] * s;
                    img[[j, i, ch]] = v.round_ties_even().clamp(0.0, 255.0) as u8;
                }
            }
        }
    }
}

/// photo_roads' carriageways: the square's segments painted in the photo's own tone.
fn step_roads(img: &mut Array3<u8>, sq: &Sq, rd: &PaintRoads, st: &PaintStyle) {
    let s = rd.cut(sq.x0, sq.y0, sq.size, sq.w, sq.h);
    if s.kind.is_empty() {
        return;
    }
    let k = st.colors.nrows();
    let cov = crate::roadtone::line_cover(sq.h, sq.w, k, s.kind.len(), |q| {
        let g = s.segs[q];
        (g[0], g[1], g[2], g[3], s.hw[q], s.kind[q])
    });
    let (luma, sat, green) = st.tone_args();
    *img = crate::roadtone::tone_paint(img.view(), &cov, k, st.colors.view(), st.tone.view(), st.radius(sq.w, sq.size),
                                       luma, sat, green);
}

/// surface.paint_square's paint of the photo rgb (h, w, 3, row 0 north) over [x0, x0 + size]² of the anchor frame in
/// one call: fields, golf (four layers, None where empty), lots, pads under the roads; then, with roads (has_roads:
/// the square has road cells), the island fill, the carriageways and the corners (simple: the carriageways alone).
/// A part that is None is left out. Returns a new array.
#[pyfunction]
#[pyo3(signature = (rgb, x0, y0, size, style, roads=None, islands=None, corners=None, fields=None, golf=None,
                    lots=None, pads=None, has_roads=false, simple=false))]
#[allow(clippy::too_many_arguments)]
pub fn paint_photo<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, x0: f64, y0: f64, size: f64,
                        style: PyRef<'py, PaintStyle>, roads: Option<PyRef<'py, PaintRoads>>,
                        islands: Option<PyRef<'py, PaintPolys>>, corners: Option<PyRef<'py, PaintPolys>>,
                        fields: Option<PyRef<'py, PaintFields>>, golf: Option<Vec<Option<PyRef<'py, PaintPolys>>>>,
                        lots: Option<PyRef<'py, PaintPolys>>, pads: Option<PyRef<'py, PaintPolys>>, has_roads: bool,
                        simple: bool) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let src = rgb.as_array();
    if src.ndim() != 3 || src.shape()[2] != 3 || !(size > 0.0) {
        return Err(pyo3::exceptions::PyValueError::new_err("paint_photo: rgb (h, w, 3), size > 0"));
    }
    let sq = Sq { x0, y0, size, w: src.shape()[1], h: src.shape()[0] };
    let st: &PaintStyle = &style;
    let (rd, isl, cor, fl, lt, pd) = (roads.as_deref(), islands.as_deref(), corners.as_deref(), fields.as_deref(),
                                      lots.as_deref(), pads.as_deref());
    let gl: Vec<Option<&PaintPolys>> = golf.as_ref().map(|g| g.iter().map(|x| x.as_deref()).collect()).unwrap_or_default();
    if gl.len() > 4 {
        return Err(pyo3::exceptions::PyValueError::new_err("paint_photo: four golf layers at most"));
    }
    let mut img = src.to_owned();
    let out = py.detach(move || {
        if sq.w == 0 || sq.h == 0 {
            return img;
        }
        if !simple {
            if let Some(f) = fl {
                step_fields(&mut img, &sq, f, st);
            }
            step_golf(&mut img, &sq, st, &gl);
            if let Some(l) = lt {
                step_pads(&mut img, &sq, l, st);
            }
            if let Some(p) = pd {
                step_pads(&mut img, &sq, p, st);
            }
        }
        if !has_roads {
            return img;
        }
        if !simple {
            if let Some(p) = isl {
                step_islands(&mut img, &sq, p, st);
            }
        }
        let filled = if simple { None } else { Some(img.clone()) };
        if let Some(r) = rd {
            step_roads(&mut img, &sq, r, st);
        }
        if let (Some(p), Some(before)) = (cor, filled.as_ref()) {
            step_corners(&mut img, before, &sq, p, st);
        }
        img
    });
    Ok(out.into_pyarray(py))
}

/// Tests: Pillow's polygon fill (ImageDraw.polygon(xy, fill=255) on a w x h "L" image) and wide lines (ImageDraw.line(xy,
/// fill=255, width=width), width > 1) as this module draws them: (h, w) uint8.
#[pyfunction]
pub fn pil_raster<'py>(py: Python<'py>, w: usize, h: usize, xy: PyReadonlyArray2<'py, f64>, width: i32)
                       -> PyResult<Bound<'py, PyArray2<u8>>> {
    let a: ArrayView2<f64> = xy.as_array();
    if a.ncols() != 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("pil_raster: xy (k, 2)"));
    }
    let pts: Vec<(f64, f64)> = (0..a.nrows()).map(|i| (a[[i, 0]], a[[i, 1]])).collect();
    let mut px = vec![0u8; w * h];
    {
        let mut c = pil::Canvas { px: &mut px, w: w as i32, h: h as i32 };
        if width > 1 { pil::wide_lines(&mut c, &pts, width, 255) } else { pil::polygon(&mut c, &pts, 255) }
    }
    Ok(Array2::from_shape_vec((h, w), px).unwrap().into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PaintStyle>()?;
    m.add_class::<PaintRoads>()?;
    m.add_class::<PaintPolys>()?;
    m.add_class::<PaintFields>()?;
    m.add_function(wrap_pyfunction!(paint_photo, m)?)?;
    m.add_function(wrap_pyfunction!(pil_raster, m)?)?;
    Ok(())
}
