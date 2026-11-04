//! The terrain grid's road samples by distance (surface.paint_roads): PIL's thick lines put a road's material half a
//! sample off its line (pixel centres at +0.5; 1.4 m across a diagonal road on the 2 m grid) and rounded its width up
//! to an odd number of samples (an 11 m carriageway painted 14 m): the asphalt grain stood 3-4 m beyond one edge line
//! (Woodland Park, US-24, GitHub #402 / the user, 2026-10-28).

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// mask (h, w) set where a sample (col c, row r: at grid coordinate (c, r)) lies within hw[k] of segment k of segs
/// (m, 4) [c0, r0, c1, r1] (grid coordinates). Returns how many samples were newly set.
#[pyfunction]
pub fn mask_segments(py: Python<'_>, mut mask: numpy::PyReadwriteArray2<'_, bool>, segs: PyReadonlyArray2<'_, f64>,
                     hw: PyReadonlyArray1<'_, f64>) -> usize {
    let (segs, hw) = (segs.as_array(), hw.as_array());
    let mut mv = mask.as_array_mut();
    py.allow_threads(|| {
        let (h, w) = (mv.shape()[0] as i64, mv.shape()[1] as i64);
        let mut n = 0usize;
        for k in 0..segs.shape()[0] {
            let (c0, r0, c1, r1, rad) = (segs[[k, 0]], segs[[k, 1]], segs[[k, 2]], segs[[k, 3]], hw[k]);
            if !(rad > 0.0) { continue; }
            let cmin = ((c0.min(c1) - rad).floor() as i64).max(0);
            let cmax = ((c0.max(c1) + rad).ceil() as i64).min(w - 1);
            let rmin = ((r0.min(r1) - rad).floor() as i64).max(0);
            let rmax = ((r0.max(r1) + rad).ceil() as i64).min(h - 1);
            if cmin > cmax || rmin > rmax { continue; }
            let (dc, dr) = (c1 - c0, r1 - r0);
            let l2 = dc * dc + dr * dr;
            let r2 = rad * rad;
            for r in rmin..=rmax {
                for c in cmin..=cmax {
                    let (pc, pr) = (c as f64 - c0, r as f64 - r0);
                    let t = if l2 > 1e-12 { ((pc * dc + pr * dr) / l2).clamp(0.0, 1.0) } else { 0.0 };
                    let (ec, er) = (pc - t * dc, pr - t * dr);
                    if ec * ec + er * er <= r2 {
                        let cell = &mut mv[[r as usize, c as usize]];
                        if !*cell { *cell = true; n += 1; }
                    }
                }
            }
        }
        n
    })
}

/// Calls f(row, col, squared distance) for each pixel (centre at col + 0.5, row + 0.5) within rad of segment
/// [c0, r0, c1, r1] in a h x w grid.
fn each_near(h: i64, w: i64, s: [f64; 4], rad: f64, mut f: impl FnMut(usize, usize, f64)) {
    let [c0, r0, c1, r1] = s;
    let cmin = ((c0.min(c1) - rad - 0.5).floor() as i64).max(0);
    let cmax = ((c0.max(c1) + rad).ceil() as i64).min(w - 1);
    let rmin = ((r0.min(r1) - rad - 0.5).floor() as i64).max(0);
    let rmax = ((r0.max(r1) + rad).ceil() as i64).min(h - 1);
    if cmin > cmax || rmin > rmax { return; }
    let (dc, dr) = (c1 - c0, r1 - r0);
    let l2 = dc * dc + dr * dr;
    let r2 = rad * rad;
    for r in rmin..=rmax {
        for c in cmin..=cmax {
            let (pc, pr) = (c as f64 + 0.5 - c0, r as f64 + 0.5 - r0);
            let t = if l2 > 1e-12 { ((pc * dc + pr * dr) / l2).clamp(0.0, 1.0) } else { 0.0 };
            let (ec, er) = (pc - t * dc, pr - t * dr);
            let d2 = ec * ec + er * er;
            if d2 <= r2 { f(r as usize, c as usize, d2); }
        }
    }
}

/// The aerial photo's own road beside a drawn one (surface.photo_ghosts): the drawn carriageway is the map's width on
/// the map's line, the photo's pavement often wider (paved shoulders) or a few metres off it (a divided road's two
/// lines drawn closer than the photo's: Woodland Park, US-24), and that strip showed beside the painted road as a pale
/// band on one side (the user, 2026-10-28). rgb (h, w, 3), pixel centres at +0.5; segs (m, 4) [c0, r0, c1, r1] with
/// half widths hw (the drawn roads), ghost (m,) the band beyond each edge to clear (0: none), road (m,) its road's
/// number (0 .. n_roads). A band pixel on no road whose colour lies within tol (RGB distance) of the photo's mean on
/// its road (or within twice that next to such a pixel: the blurred rim) is the photo's pavement: it takes the photo
/// mirrored about the pavement's outer end along the road's normal (the ground beyond, its texture kept). Returns
/// (new rgb, mask of the replaced pixels).
#[pyfunction]
pub fn photo_ghosts<'py>(py: Python<'py>, rgb: numpy::PyReadonlyArray3<'py, u8>, segs: PyReadonlyArray2<'py, f64>,
                         hw: PyReadonlyArray1<'py, f64>, ghost: PyReadonlyArray1<'py, f64>,
                         road: PyReadonlyArray1<'py, i64>, n_roads: usize, tol: f64)
                         -> (Bound<'py, numpy::PyArray3<u8>>, Bound<'py, numpy::PyArray2<bool>>) {
    let (img, segs, hw, ghost, road) = (rgb.as_array(), segs.as_array(), hw.as_array(), ghost.as_array(), road.as_array());
    let (h, w) = (img.shape()[0], img.shape()[1]);
    let (out, mask) = py.allow_threads(|| {
        use numpy::ndarray::{Array2, Array3};
        let mut on = Array2::<bool>::from_elem((h, w), false);
        let mut sum = vec![[0f64; 3]; n_roads];
        let mut cnt = vec![0f64; n_roads];
        let seg = |k: usize| [segs[[k, 0]], segs[[k, 1]], segs[[k, 2]], segs[[k, 3]]];
        for k in 0..segs.shape()[0] {
            if !(hw[k] > 0.0) { continue; }
            let id = road[k] as usize;
            each_near(h as i64, w as i64, seg(k), hw[k], |r, c, _| {
                on[[r, c]] = true;
                if id < n_roads {
                    for ch in 0..3 { sum[id][ch] += img[[r, c, ch]] as f64; }
                    cnt[id] += 1.0;
                }
            });
        }
        // per band pixel: 0 none, 1 band, 2 pavement; its nearest segment's outward unit normal and squared distance
        let mut band = Array2::<u8>::zeros((h, w));
        // the road whose band a pixel is in (-1 none, -2 two roads: between two carriageways, a gore or a median, the
        // photo's pavement is real: kept)
        let mut owner = Array2::<i64>::from_elem((h, w), -1);
        let mut dist = Array2::<f64>::from_elem((h, w), f64::INFINITY);
        let mut seg_d = Array2::<f64>::from_elem((h, w), f64::INFINITY);
        let mut nrm = Array3::<f64>::zeros((h, w, 2));
        let t2 = tol * tol;
        for k in 0..segs.shape()[0] {
            let id = road[k] as usize;
            if !(ghost[k] > 0.0) || id >= n_roads || cnt[id] < 1.0 { continue; }
            let m = [sum[id][0] / cnt[id], sum[id][1] / cnt[id], sum[id][2] / cnt[id]];
            let [c0, r0, c1, r1] = seg(k);
            let (dc, dr) = (c1 - c0, r1 - r0);
            let l2 = dc * dc + dr * dr;
            each_near(h as i64, w as i64, seg(k), hw[k] + ghost[k], |r, c, d2s| {
                if on[[r, c]] { return; }
                let o = owner[[r, c]];
                if o == -1 { owner[[r, c]] = id as i64; } else if o >= 0 && o != id as i64 { owner[[r, c]] = -2; }
                let mut d2 = 0.0;
                for ch in 0..3 { let d = img[[r, c, ch]] as f64 - m[ch]; d2 += d * d; }
                if band[[r, c]] == 0 { band[[r, c]] = 1; }
                if d2 < dist[[r, c]] { dist[[r, c]] = d2; }
                if d2 <= t2 { band[[r, c]] = 2; }
                if d2s < seg_d[[r, c]] {
                    seg_d[[r, c]] = d2s;
                    let (pc, pr) = (c as f64 + 0.5 - c0, r as f64 + 0.5 - r0);
                    let t = if l2 > 1e-12 { ((pc * dc + pr * dr) / l2).clamp(0.0, 1.0) } else { 0.0 };
                    let (ec, er) = (pc - t * dc, pr - t * dr);
                    let n = (ec * ec + er * er).sqrt().max(1e-9);
                    nrm[[r, c, 0]] = ec / n;
                    nrm[[r, c, 1]] = er / n;
                }
            });
        }
        let mut grow = Vec::new();
        for r in 0..h {
            for c in 0..w {
                if band[[r, c]] != 1 || dist[[r, c]] > 4.0 * t2 { continue; }
                let mut near = false;
                for dr in -1i64..=1 { for dc in -1i64..=1 {
                    let (rr, cc) = (r as i64 + dr, c as i64 + dc);
                    if rr >= 0 && cc >= 0 && (rr as usize) < h && (cc as usize) < w
                        && band[[rr as usize, cc as usize]] == 2 { near = true; }
                } }
                if near { grow.push((r, c)); }
            }
        }
        for (r, c) in grow { band[[r, c]] = 2; }
        for r in 0..h { for c in 0..w { if owner[[r, c]] == -2 { band[[r, c]] = 1; } } }
        let mut out = img.to_owned();
        let mut mask = Array2::<bool>::from_elem((h, w), false);
        let at = |c: f64, r: f64| -> Option<(usize, usize)> {
            let (ci, ri) = (c.floor() as i64, r.floor() as i64);
            if ci < 0 || ri < 0 || ci as usize >= w || ri as usize >= h { None } else { Some((ri as usize, ci as usize)) }
        };
        for r in 0..h {
            for c in 0..w {
                if band[[r, c]] != 2 { continue; }
                let (nc, nr) = (nrm[[r, c, 0]], nrm[[r, c, 1]]);
                let (pc, pr) = (c as f64 + 0.5, r as f64 + 0.5);
                // the pavement's outer end along the normal (the first pixel off it), then the mirror beyond
                let mut t = 1.0;
                let mut src = None;
                while t <= 64.0 {
                    match at(pc + nc * t, pr + nr * t) {
                        Some((rr, cc)) if band[[rr, cc]] != 2 && !on[[rr, cc]] => {
                            src = at(pc + nc * 2.0 * t, pr + nr * 2.0 * t)
                                .filter(|&(r2, c2)| band[[r2, c2]] != 2 && !on[[r2, c2]])
                                .or(Some((rr, cc)));
                            break;
                        }
                        Some(_) => t += 1.0,
                        None => break,
                    }
                }
                if let Some((rr, cc)) = src {
                    for ch in 0..3 { out[[r, c, ch]] = img[[rr, cc, ch]]; }
                    mask[[r, c]] = true;
                }
            }
        }
        (out, mask)
    });
    (numpy::PyArray3::from_owned_array(py, out), numpy::PyArray2::from_owned_array(py, mask))
}

/// A broken line's dashes (markings.dashes): the polyline pts (k, 3) cut into pieces dash m long every dash + gap m,
/// the pattern starting phase m into its cycle at the polyline's first point (a road's lines start their cycle at
/// the road's start, so the dashes meet across tiles). Returns (points (n, 3) of every piece one after another,
/// starts (pieces + 1,)): each piece is its run of the polyline with the cut points interpolated (z too).
#[pyfunction]
pub fn dash_pieces<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, dash: f64, gap: f64, phase: f64)
                        -> (Bound<'py, numpy::PyArray2<f64>>, Bound<'py, numpy::PyArray1<i64>>) {
    let p = pts.as_array();
    let k = p.shape()[0];
    let mut out: Vec<[f64; 3]> = Vec::new();
    let mut starts: Vec<i64> = vec![0];
    let cycle = dash + gap;
    if k >= 2 && dash > 0.0 && cycle > 0.0 {
        let at = |i: usize| [p[[i, 0]], p[[i, 1]], if p.shape()[1] > 2 { p[[i, 2]] } else { 0.0 }];
        let lerp = |a: [f64; 3], b: [f64; 3], t: f64| [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
        // s: the distance along the pattern (0 .. cycle: in a dash below dash)
        let mut s = phase.rem_euclid(cycle);
        let mut open = s < dash;
        if open { out.push(at(0)); }
        for i in 0..k - 1 {
            let (a, b) = (at(i), at(i + 1));
            let len = ((b[0] - a[0]).powi(2) + (b[1] - a[1]).powi(2)).sqrt();
            if len < 1e-12 { continue; }
            let mut t0 = 0.0;           // metres into this segment done
            loop {
                let next = if open { dash - s } else { cycle - s };       // metres to the next switch
                if t0 + next > len {
                    s += len - t0;
                    break;
                }
                t0 += next;
                let q = lerp(a, b, t0 / len);
                if open {
                    out.push(q);
                    starts.push(out.len() as i64);
                    open = false;
                    s = dash;
                } else {
                    out.push(q);
                    open = true;
                    s = 0.0;
                }
            }
            if open { out.push(b); }
        }
        if open {
            if out.len() as i64 - *starts.last().unwrap() >= 2 { starts.push(out.len() as i64); } else { out.truncate(*starts.last().unwrap() as usize); }
        }
    }
    let n = out.len();
    let mut arr = numpy::ndarray::Array2::<f64>::zeros((n, 3));
    for (i, q) in out.iter().enumerate() { for c in 0..3 { arr[[i, c]] = q[c]; } }
    (numpy::PyArray2::from_owned_array(py, arr), numpy::PyArray1::from_vec(py, starts))
}

/// surface._hash01: a lattice point's pseudo-random value in [-1, 1] (uint64 arithmetic, wrapping as numpy's).
#[inline]
fn noise_hash(ix: i64, iy: i64, seed: u64) -> f64 {
    let mut h = (ix as u64).wrapping_mul(0x9E3779B185EBCA87)
        ^ (iy as u64).wrapping_mul(0xC2B2AE3D27D4EB4F)
        ^ seed.wrapping_mul(0x165667B19E3779F9);
    h ^= h >> 29;
    h = h.wrapping_mul(0xBF58476D1CE4E5B9);
    h ^= h >> 32;
    (h >> 11) as f64 / (1u64 << 53) as f64 * 2.0 - 1.0
}

/// surface._value_noises: smooth value noise in [-1, 1] at the points (x, y) (lattice units, flat arrays of one
/// length) for each seed, one array per seed. The Python's arithmetic in its order (smoothstep fx * fx * (3 - 2 fx),
/// top = (b - a) fx + a, bot = (d - c) fx + c, (bot - top) fy + top): the same values bit for bit.
#[pyfunction]
pub fn value_noises<'py>(py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>,
                         seeds: Vec<u64>) -> Vec<Bound<'py, numpy::PyArray1<f64>>> {
    use numpy::IntoPyArray;
    let (x, y) = (x.as_array(), y.as_array());
    let outs: Vec<Vec<f64>> = py.allow_threads(|| {
        let n = x.len().min(y.len());
        let mut outs: Vec<Vec<f64>> = seeds.iter().map(|_| Vec::with_capacity(n)).collect();
        for i in 0..n {
            let (xi, yi) = (x[i], y[i]);
            let (x0, y0) = (xi.floor(), yi.floor());
            let (fx, fy) = (xi - x0, yi - y0);
            let fx = fx * fx * (3.0 - 2.0 * fx);
            let fy = fy * fy * (3.0 - 2.0 * fy);
            let (ix, iy) = (x0 as i64, y0 as i64);
            for (s, out) in seeds.iter().zip(outs.iter_mut()) {
                let a = noise_hash(ix, iy, *s);
                let b = noise_hash(ix + 1, iy, *s);
                let c = noise_hash(ix, iy + 1, *s);
                let d = noise_hash(ix + 1, iy + 1, *s);
                let top = (b - a) * fx + a;
                let bot = (d - c) * fx + c;
                out.push((bot - top) * fy + top);
            }
        }
        outs
    });
    outs.into_iter().map(|v| v.into_pyarray(py)).collect()
}

/// The 2 x 2 sub-pixel centres (p + 0.25, p + 0.75 along each axis) of an h x w photo inside any of pads (each a
/// closed ring as (cols, rows) in photo pixels, even-odd within a ring, the union over rings): one bit per sub-pixel
/// (bit 2 * row + col) over the pads' clipped bounding box. Returns (r0, c0, rows, cols, bits) or None.
fn pad_bits(h: usize, w: usize, pads: &[(Vec<f64>, Vec<f64>)]) -> Option<(usize, usize, usize, usize, Vec<u8>)> {
    let (mut c0, mut r0, mut c1, mut r1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for (cs, rs) in pads {
        for (&c, &r) in cs.iter().zip(rs) {
            c0 = c0.min(c); c1 = c1.max(c); r0 = r0.min(r); r1 = r1.max(r);
        }
    }
    let bc0 = (c0.floor().max(0.0)) as usize;
    let br0 = (r0.floor().max(0.0)) as usize;
    let bc1 = ((c1.ceil() + 1.0).min(w as f64).max(0.0)) as usize;
    let br1 = ((r1.ceil() + 1.0).min(h as f64).max(0.0)) as usize;
    if !(c1 >= c0) || bc1 <= bc0 || br1 <= br0 {
        return None;
    }
    let (nr, nc) = (br1 - br0, bc1 - bc0);
    let mut bits = vec![0u8; nr * nc];
    let mut xs: Vec<f64> = Vec::new();
    for (cs, rs) in pads {
        let n = cs.len().min(rs.len());
        if n < 3 { continue; }
        let (mut pr0, mut pr1) = (f64::INFINITY, f64::NEG_INFINITY);
        for &r in &rs[..n] { pr0 = pr0.min(r); pr1 = pr1.max(r); }
        let sr0 = (((pr0 - br0 as f64) * 2.0 - 0.5).floor().max(0.0)) as usize;
        let sr1 = ((((pr1 - br0 as f64) * 2.0 - 0.5).ceil() + 1.0).min((2 * nr) as f64).max(0.0)) as usize;
        for sr in sr0..sr1 {
            let ty = br0 as f64 + (sr as f64 + 0.5) / 2.0;          // the sub-row's centre in photo rows
            xs.clear();
            for k in 0..n {
                let (xa, ya) = (cs[k], rs[k]);
                let (xb, yb) = if k + 1 < n { (cs[k + 1], rs[k + 1]) } else { (cs[0], rs[0]) };
                if (ya >= ty) != (yb >= ty) {
                    xs.push(xa + (ty - ya) * (xb - xa) / (yb - ya));
                }
            }
            if xs.len() < 2 { continue; }
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let row = sr / 2;
            let bit_row = (sr % 2) * 2;
            for pair in xs.chunks(2) {
                if pair.len() < 2 { break; }
                // sub-columns whose centre (q + 0.5) / 2 lies in [xa, xb) (photo columns, from bc0)
                let qa = ((pair[0] - bc0 as f64) * 2.0 - 0.5).ceil().max(0.0) as usize;
                let qb = (((pair[1] - bc0 as f64) * 2.0 - 0.5).ceil().max(0.0) as usize).min(2 * nc);
                for q in qa..qb {
                    bits[row * nc + q / 2] |= 1 << (bit_row + q % 2);
                }
            }
        }
    }
    Some((br0, bc0, nr, nc, bits))
}

fn pads_of(pads: &[(PyReadonlyArray1<'_, f64>, PyReadonlyArray1<'_, f64>)]) -> Vec<(Vec<f64>, Vec<f64>)> {
    pads.iter().map(|(c, r)| (c.as_array().to_vec(), r.as_array().to_vec())).collect()
}

/// surface._pads_px's coverage: an h x w uint8 of the 2 x 2 sub-pixels each photo pixel has inside the pads
/// (0..4), or None when none is in the photo.
#[pyfunction]
pub fn pad_cover<'py>(py: Python<'py>, h: usize, w: usize, pads: Vec<(PyReadonlyArray1<'py, f64>, PyReadonlyArray1<'py, f64>)>)
                      -> Option<Bound<'py, numpy::PyArray2<u8>>> {
    use numpy::IntoPyArray;
    let pv = pads_of(&pads);
    let out = py.allow_threads(|| {
        let (r0, c0, nr, nc, bits) = pad_bits(h, w, &pv)?;
        let mut out = vec![0u8; h * w];
        let mut any = false;
        for r in 0..nr {
            for c in 0..nc {
                let b = bits[r * nc + c];
                if b != 0 { out[(r0 + r) * w + c0 + c] = b.count_ones() as u8; any = true; }
            }
        }
        if any { Some(out) } else { None }
    })?;
    Some(numpy::ndarray::Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

/// surface.photo_pads's blend in place: each photo pixel the pads cover (a = its covered sub-pixels / 4) becomes
/// round(rgb * (1 - a op) + colour * a op) in float32, ties to even (numpy's). Returns the pixels changed.
#[pyfunction]
pub fn paint_pads(py: Python<'_>, mut rgb: numpy::PyReadwriteArray3<'_, u8>,
                  pads: Vec<(PyReadonlyArray1<'_, f64>, PyReadonlyArray1<'_, f64>)>, colour: [f32; 3], op: f32) -> usize {
    let pv = pads_of(&pads);
    let mut img = rgb.as_array_mut();
    let (h, w) = (img.shape()[0], img.shape()[1]);
    py.allow_threads(|| {
        let Some((r0, c0, nr, nc, bits)) = pad_bits(h, w, &pv) else { return 0 };
        let mut n = 0usize;
        for r in 0..nr {
            for c in 0..nc {
                let b = bits[r * nc + c];
                if b == 0 { continue; }
                let a = b.count_ones() as f32 * 255.0 / 4.0 / 255.0;     // (the Python's mean of 0 / 255 samples)
                let sub = a * op;
                for k in 0..3 {
                    let px = &mut img[[r0 + r, c0 + c, k]];
                    let v = (*px as f32 * (1.0 - sub) + colour[k] * sub).round_ties_even();
                    *px = v.clamp(0.0, 255.0) as u8;
                }
                n += 1;
            }
        }
        n
    })
}
