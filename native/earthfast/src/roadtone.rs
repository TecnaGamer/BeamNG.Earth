//! surface.photo_roads' paint in the photo's own tone (the user, 2026-11-11: "look into other ways to get the ground
//! collor, and material to be right" -> "start with 1"): a road kind's colour at a pixel is the photo's own pavement
//! around it (its pixels under that kind, markings, shadow, greenery and cars left out, averaged over a box of
//! `radius` pixels), pulled into the kind's tone range and mixed with its flat colour, instead of one flat colour per
//! kind. Each road keeps its real shade (old asphalt, patches, concrete), and the same photo pixels give the same tone
//! on both sides of a tile edge.

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;

/// A sample further than this (8-bit luma) from the first pass's tone at its cell is left out of the second.
const ROBUST_LUMA: f32 = 22.0;

/// Box sums (radius r, clipped at the edges) of the planes v (n planes of h x w), by summed-area tables.
fn box_sum(v: &[f32], h: usize, w: usize, r: usize) -> Vec<f32> {
    let mut sat = vec![0f64; (h + 1) * (w + 1)];
    for j in 0..h {
        let mut row = 0f64;
        for i in 0..w {
            row += v[j * w + i] as f64;
            sat[(j + 1) * (w + 1) + i + 1] = sat[j * (w + 1) + i + 1] + row;
        }
    }
    let mut out = vec![0f32; h * w];
    for j in 0..h {
        let j0 = j.saturating_sub(r);
        let j1 = (j + r + 1).min(h);
        for i in 0..w {
            let i0 = i.saturating_sub(r);
            let i1 = (i + r + 1).min(w);
            let s = sat[j1 * (w + 1) + i1] - sat[j0 * (w + 1) + i1] - sat[j1 * (w + 1) + i0] + sat[j0 * (w + 1) + i0];
            out[j * w + i] = s as f32;
        }
    }
    out
}

/// The photo rgb (h, w, 3) with thick lines painted in as earthfast.paint_lines (segs (m, 4) pixel ends, hw (m,) half
/// widths, kind (m,) into colors (k, 4: r g b, opacity), the last kind wins where they overlap), each kind's colour
/// taken from the photo: tone (k, 5) per kind: (luma min, luma max, saturation max, share of the photo's tone (0: the
/// flat colour), the least samples in the box for any photo tone). A pixel is a sample of a kind when the kind covers
/// it whole and no later kind does, its luma is in [sample_luma.0, sample_luma.1] (markings and shadow out), it is not
/// green (g over both r and b by green_margin) and its saturation is under sample_sat (cars). The samples' mean over
/// the box of radius pixels is pulled to the kind's luma range and saturation cap, then mixed with the flat colour.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn paint_lines_toned<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, segs: PyReadonlyArray2<'py, f64>,
                              hw: PyReadonlyArray1<'py, f64>, kind: PyReadonlyArray1<'py, u8>,
                              colors: PyReadonlyArray2<'py, f32>, tone: PyReadonlyArray2<'py, f32>, radius: usize,
                              sample_luma: (f32, f32), sample_sat: f32, green_margin: f32)
                              -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let sg = segs.as_array();
    let hw = hw.as_array();
    let kd = kind.as_array();
    let col = colors.as_array();
    let tn = tone.as_array();
    let m = sg.nrows();
    if sg.ncols() != 4 || hw.len() != m || kd.len() != m || col.ncols() != 4 || img.shape()[2] != 3
        || tn.nrows() != col.nrows() || tn.ncols() != 5 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "paint_lines_toned: rgb (h, w, 3), segs (m, 4), hw (m,), kind (m,), colors (k, 4), tone (k, 5)"));
    }
    let k = col.nrows();
    if kd.iter().any(|&v| v as usize >= k) {
        return Err(pyo3::exceptions::PyValueError::new_err("paint_lines_toned: a kind past the colours"));
    }
    let out = py.detach(|| {
        let (h, w) = (img.shape()[0], img.shape()[1]);
        let mut cov = vec![0f32; k * h * w];
        for s in 0..m {
            let (ax, ay, bx, by, r) = (sg[[s, 0]], sg[[s, 1]], sg[[s, 2]], sg[[s, 3]], hw[s]);
            if !(r > 0.0) || !ax.is_finite() || !ay.is_finite() || !bx.is_finite() || !by.is_finite() {
                continue;
            }
            let pad = r + 1.0;
            let c0 = (ax.min(bx) - pad).floor().max(0.0);
            let c1 = (ax.max(bx) + pad).ceil().min(w as f64);
            let r0 = (ay.min(by) - pad).floor().max(0.0);
            let r1 = (ay.max(by) + pad).ceil().min(h as f64);
            if c0 >= c1 || r0 >= r1 {
                continue;
            }
            let (dx, dy) = (bx - ax, by - ay);
            let l2 = dx * dx + dy * dy;
            let base = kd[s] as usize * h * w;
            for j in r0 as usize..r1 as usize {
                let py_ = j as f64 + 0.5;
                for i in c0 as usize..c1 as usize {
                    let px_ = i as f64 + 0.5;
                    let t = if l2 > 0.0 { (((px_ - ax) * dx + (py_ - ay) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
                    let (ex, ey) = (px_ - ax - t * dx, py_ - ay - t * dy);
                    let c = (r - (ex * ex + ey * ey).sqrt() + 0.5).clamp(0.0, 1.0) as f32;
                    let v = &mut cov[base + j * w + i];
                    if c > *v { *v = c; }
                }
            }
        }
        tone_paint(img, &cov, k, col, tn, radius, sample_luma, sample_sat, green_margin)
    });
    Ok(out.into_pyarray(py))
}

/// paint_lines_toned's toning for one surface given as a coverage mask: the photo rgb (h, w, 3) with cover (h, w;
/// 0..1) painted in color (4: r g b, opacity) in the photo's own tone (tone (5): as paint_lines_toned's per kind).
/// surface.photo_pads: parking lots, gas station pads (the user, 2026-11-11: "utiluze and revfine this method of
/// texturing the ground more").
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn paint_cover_toned<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, cover: PyReadonlyArray2<'py, f32>,
                              color: PyReadonlyArray1<'py, f32>, tone: PyReadonlyArray1<'py, f32>, radius: usize,
                              sample_luma: (f32, f32), sample_sat: f32, green_margin: f32)
                              -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let cv = cover.as_array();
    let c = color.as_array();
    let t = tone.as_array();
    if img.shape()[2] != 3 || cv.shape() != &img.shape()[..2] || c.len() != 4 || t.len() != 5 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "paint_cover_toned: rgb (h, w, 3), cover (h, w), color (4,), tone (5,)"));
    }
    let col = numpy::ndarray::Array2::from_shape_vec((1, 4), c.to_vec()).unwrap();
    let tn = numpy::ndarray::Array2::from_shape_vec((1, 5), t.to_vec()).unwrap();
    let cov: Vec<f32> = cv.iter().map(|v| v.clamp(0.0, 1.0)).collect();
    let out = py.detach(|| tone_paint(img, &cov, 1, col.view(), tn.view(), radius, sample_luma, sample_sat, green_margin));
    Ok(out.into_pyarray(py))
}

/// The toning and painting shared by paint_lines_toned and paint_cover_toned: cov holds k coverage planes (h x w).
#[allow(clippy::too_many_arguments)]
fn tone_paint(img: numpy::ndarray::ArrayView3<u8>, cov: &[f32], k: usize, col: numpy::ndarray::ArrayView2<f32>,
              tn: numpy::ndarray::ArrayView2<f32>, radius: usize, sample_luma: (f32, f32), sample_sat: f32,
              green_margin: f32) -> numpy::ndarray::Array3<u8> {
    let (h, w) = (img.shape()[0], img.shape()[1]);
    // each kind's samples (whole under it, no later kind over it), summed on a grid of f x f pixel cells (the
    // tone varies over metres: a 4-16x smaller box filter), their box means interpolated back per pixel
    let f = (radius / 3).max(1);
    let (gh, gw) = ((h + f - 1) / f, (w + f - 1) / f);
    let rc = ((radius + f / 2) / f).max(1);
    // (one pass: a pixel is a sample of the last kind over it, when that kind covers it whole; the second pass
    // keeps only the samples within ROBUST_LUMA of the first pass's tone at their cell: parked cars, debris and
    // old markings left dark or pale smudges in a plain mean)
    let gather = |first: Option<&Vec<Option<[Vec<f32>; 4]>>>| -> Vec<Option<[Vec<f32>; 4]>> {
        let mut planes: Vec<[Vec<f32>; 4]> = (0..k).map(|q| if tn[[q, 3]] > 0.0 {
            [vec![0f32; gh * gw], vec![0f32; gh * gw], vec![0f32; gh * gw], vec![0f32; gh * gw]]
        } else {
            [Vec::new(), Vec::new(), Vec::new(), Vec::new()]
        }).collect();
        let mut seen = vec![false; k];
        for p in 0..h * w {
            let mut top = usize::MAX;
            for q in (0..k).rev() {
                if cov[q * h * w + p] > 0.0 {
                    top = q;
                    break;
                }
            }
            if top == usize::MAX || cov[top * h * w + p] < 1.0 || planes[top][0].is_empty() {
                continue;
            }
            let (j, i) = (p / w, p % w);
            let (r, g, b) = (img[[j, i, 0]] as f32, img[[j, i, 1]] as f32, img[[j, i, 2]] as f32);
            let luma = 0.299 * r + 0.587 * g + 0.114 * b;
            let mx = r.max(g).max(b);
            let sat = if mx > 0.0 { (mx - r.min(g).min(b)) / mx } else { 0.0 };
            if luma < sample_luma.0 || luma > sample_luma.1 || sat > sample_sat
                || (g > r + green_margin && g > b + green_margin) {
                continue;
            }
            let c = (j / f) * gw + i / f;
            if let Some(first) = first {
                if let Some(t) = &first[top] {
                    if t[0][c].is_finite() {
                        let l0 = 0.299 * t[0][c] + 0.587 * t[1][c] + 0.114 * t[2][c];
                        if (luma - l0).abs() > ROBUST_LUMA {
                            continue;
                        }
                    }
                }
            }
            let pl = &mut planes[top];
            pl[0][c] += 1.0;
            pl[1][c] += r;
            pl[2][c] += g;
            pl[3][c] += b;
            seen[top] = true;
        }
        let mut tones: Vec<Option<[Vec<f32>; 4]>> = Vec::with_capacity(k);
        for (q, planes) in planes.into_iter().enumerate() {
            if !seen[q] {
                tones.push(None);
                continue;
            }
            let [n, sr, sg_, sb] = planes;
            let n = box_sum(&n, gh, gw, rc);
            // (each cell's box mean, nan where too few samples: the flat colour there)
            let least = tn[[q, 4]].max(1.0);
            let (sr, sg_, sb) = (box_sum(&sr, gh, gw, rc), box_sum(&sg_, gh, gw, rc), box_sum(&sb, gh, gw, rc));
            let mut mr = vec![f32::NAN; gh * gw];
            let mut mg = vec![f32::NAN; gh * gw];
            let mut mb = vec![f32::NAN; gh * gw];
            for c in 0..gh * gw {
                if n[c] >= least {
                    mr[c] = sr[c] / n[c];
                    mg[c] = sg_[c] / n[c];
                    mb[c] = sb[c] / n[c];
                }
            }
            tones.push(Some([mr, mg, mb, n]));
        }
        tones
    };
    let first = gather(None);
    let tones = gather(Some(&first));
    // a pixel's tone: the cells' means round it, bilinear over the ones that have one
    let tone_at = |t: &[Vec<f32>; 4], j: usize, i: usize| -> Option<([f32; 3], f32)> {
        let gy = ((j as f32 + 0.5) / f as f32 - 0.5).clamp(0.0, (gh - 1) as f32);
        let gx = ((i as f32 + 0.5) / f as f32 - 0.5).clamp(0.0, (gw - 1) as f32);
        let (y0, x0) = (gy.floor() as usize, gx.floor() as usize);
        let (y1, x1) = ((y0 + 1).min(gh - 1), (x0 + 1).min(gw - 1));
        let (fy, fx) = (gy - y0 as f32, gx - x0 as f32);
        let mut acc = [0f32; 3];
        let mut wsum = 0f32;
        for &(yy, xx, wt) in &[(y0, x0, (1.0 - fy) * (1.0 - fx)), (y0, x1, (1.0 - fy) * fx), (y1, x0, fy * (1.0 - fx)),
                               (y1, x1, fy * fx)] {
            let c = yy * gw + xx;
            if wt > 0.0 && t[0][c].is_finite() {
                acc[0] += t[0][c] * wt;
                acc[1] += t[1][c] * wt;
                acc[2] += t[2][c] * wt;
                wsum += wt;
            }
        }
        // (and the share of the weight that had a tone: the flat colour fades in where the samples end, no step)
        if wsum > 1e-6 { Some(([acc[0] / wsum, acc[1] / wsum, acc[2] / wsum], wsum)) } else { None }
    };
    let mut out = img.to_owned();
    for q in 0..k {
        let base = q * h * w;
        let flat = [col[[q, 0]], col[[q, 1]], col[[q, 2]]];
        let alpha = col[[q, 3]];
        let (lmin, lmax, smax, share) = (tn[[q, 0]], tn[[q, 1]], tn[[q, 2]], tn[[q, 3]]);
        for p in 0..h * w {
            let a = cov[base + p] * alpha;
            if a <= 0.0 { continue; }
            let mut c = flat;
            if let Some(tq) = &tones[q] {
                if let Some((mut t, cover)) = tone_at(tq, p / w, p % w) {
                    // (pulled to the kind's range: luma first, then the saturation toward its grey)
                    let luma = 0.299 * t[0] + 0.587 * t[1] + 0.114 * t[2];
                    let want = luma.clamp(lmin, lmax);
                    if luma > 0.0 {
                        for v in t.iter_mut() { *v *= want / luma; }
                    }
                    let mx = t[0].max(t[1]).max(t[2]);
                    let sat = if mx > 0.0 { (mx - t[0].min(t[1]).min(t[2])) / mx } else { 0.0 };
                    if sat > smax && sat > 0.0 {
                        let f = smax / sat;
                        for v in t.iter_mut() { *v = want + (*v - want) * f; }
                    }
                    for ch in 0..3 { c[ch] = flat[ch] + (t[ch] - flat[ch]) * share * cover; }
                }
            }
            let (j, i) = (p / w, p % w);
            for ch in 0..3 {
                let v = out[[j, i, ch]] as f32 * (1.0 - a) + c[ch] * a;
                out[[j, i, ch]] = (v + 0.5).clamp(0.0, 255.0) as u8;
            }
        }
    }
    out
}
