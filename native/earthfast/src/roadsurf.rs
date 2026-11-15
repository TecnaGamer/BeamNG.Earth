//! roadsurf.rs: a minor road's surface read from the aerial photo the ground photo was made from (server/roadsurf.py; the
//! user, 2026-11-12: "fix any instances of the road material being incorrect"). OpenStreetMap leaves most residential,
//! service and unclassified roads without a surface tag; where the mapped ones round them are mostly unpaved the
//! regional rule (roads.regional_unpaved) can only draw all of them gravel or all paved, and in a mixed county (Hunt
//! County, Texas, GitHub #834) either way is wrong for many. Here each road is measured along its centre line in the
//! z17 tiles already on the disk (never fetched here), and judged against the same photo's own reference roads: the ones
//! mapped paved (or primary .. tertiary) and the ones mapped unpaved. A photo's colour cast, season and exposure do
//! not decide: only which references a road looks like.
//!
//! road_photo_surface reads the tiles itself (std::fs, zune-jpeg), no GIL: the caller hands over the roads' lines and
//! roles and gets each road's numbers and verdict.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

const TILE: usize = 256;
const NF: usize = 4;            // luma, saturation, excess green, luma spread

enum Tile {
    Missing,
    Nodata,
    Rgb(Vec<u8>),
}

fn load_tile(root: &str, z: u32, x: i64, y: i64) -> Tile {
    let p = std::path::Path::new(root).join(z.to_string()).join(x.to_string()).join(format!("{y}.jpg"));
    match std::fs::read(&p) {
        Ok(b) => match crate::fartex::decode_jpeg(&b) {
            Ok(px) => Tile::Rgb(px),
            Err(_) => Tile::Nodata,
        },
        Err(_) => Tile::Missing,
    }
}

/// Median of v (sorted in place); NaN when empty.
fn median(v: &mut [f32]) -> f32 {
    if v.is_empty() {
        return f32::NAN;
    }
    v.sort_unstable_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { 0.5 * (v[n / 2 - 1] + v[n / 2]) }
}

#[derive(Default, Clone)]
struct Samples {
    lum: Vec<f32>,
    sat: Vec<f32>,
    exg: Vec<f32>,
}

impl Samples {
    /// (luma, saturation, excess green, luma spread) medians, or None under min_n samples.
    fn stats(&mut self, min_n: usize) -> Option<[f32; NF]> {
        if self.lum.len() < min_n {
            return None;
        }
        let ml = median(&mut self.lum.clone());
        let mut dev: Vec<f32> = self.lum.iter().map(|v| (v - ml).abs()).collect();
        Some([ml, median(&mut self.sat), median(&mut self.exg), median(&mut dev)])
    }
}

fn prm_of(prm: &HashMap<String, f64>, k: &str) -> PyResult<f64> {
    prm.get(k).copied().ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(format!("road_photo_surface: prm {k}")))
}

/// Each road's colour numbers along its centre line and beside it, and its verdict against the references.
///
/// root: the source's tile folder (its z / x / y.jpg); lonlat (P, 2) the points of Q polylines (poly_off (Q + 1)), each
/// one piece of road poly_road[q] (0 .. R); role (R,): 0 a road to judge, 1 a paved reference, 2 an unpaved one, -1 none.
/// offsets: m across the line for the road's samples. prm: step (m along), end (m left out at each piece's ends: a
/// junction's other road), dark (luma under it left out: shade), min_n (samples a road needs), min_have (share of its
/// samples with a tile on the disk), min_refs (references of each kind), margin (the log-likelihood margin a verdict
/// needs), outlier (the winning kind's squared distance in its spreads at most: a road like neither is no verdict),
/// verge (m out either side where its verges are read), vis_min (a road nearer its verges than this, in `scale` units,
/// does not stand out: on vegetation-like verges, a field track, unpaved), verge_sat / verge_exg (verges at least this
/// saturated or green are vegetation), verge_l (the verges and the road at least this bright: under a canopy both are
/// the trees). floor (4): the least spread of a reference kind per number; scale (4): the units
/// of the verge distance.
///
/// The references of each kind: their numbers' medians and spreads (1.4826 MAD over the references, at least floor);
/// a road's log-likelihood under each (independent normals); the nearer kind by `margin` is its verdict, within
/// `outlier` of that kind.
///
/// Returns (feats (R, 9) f32: samples kept, luma, saturation, excess green, luma spread, share of samples whose tile
/// was on the disk, the verges' luma, saturation, excess green; verdict (R,) i8: 1 paved, 2 unpaved, 0 measured but
/// neither clearly, -1 not measured (too few samples, tiles not on the disk) or not a road to judge; refs (17,) f32: the
/// paved and the unpaved references' medians (4 each) and spreads (4 each), references used of each kind packed as
/// paved * 1000 + unpaved; tiles read (decoded)).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn road_photo_surface<'py>(py: Python<'py>, root: String, zoom: u32, lonlat: PyReadonlyArray2<'py, f64>,
                               poly_off: PyReadonlyArray1<'py, i64>, poly_road: PyReadonlyArray1<'py, i64>,
                               role: PyReadonlyArray1<'py, i8>, offsets: Vec<f64>, prm: HashMap<String, f64>,
                               floor: [f32; NF], scale: [f32; NF])
                               -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray1<i8>>, Bound<'py, PyArray1<f32>>,
                                            usize)> {
    let ll = lonlat.as_array();
    let po = poly_off.as_array();
    let pr = poly_road.as_array();
    let rl = role.as_array();
    let r = rl.len();
    let q = pr.len();
    if ll.ncols() != 2 || po.len() != q + 1 || (q > 0 && po[q] as usize != ll.nrows())
        || pr.iter().any(|&v| v < 0 || v as usize >= r) || po.iter().any(|&v| v < 0) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "road_photo_surface: lonlat (P, 2), poly_off (Q + 1) ending at P, poly_road (Q) in 0 .. R"));
    }
    let step = prm_of(&prm, "step")?.max(0.25);
    let end_m = prm_of(&prm, "end")?;
    let dark = prm_of(&prm, "dark")? as f32;
    let min_n = prm_of(&prm, "min_n")? as usize;
    let min_have = prm_of(&prm, "min_have")? as f32;
    let min_refs = (prm_of(&prm, "min_refs")? as usize).max(1);
    let margin = prm_of(&prm, "margin")? as f32;
    let outlier = prm_of(&prm, "outlier")? as f32;
    let verge_m = prm_of(&prm, "verge")?;
    let vis_min = prm_of(&prm, "vis_min")? as f32;
    let verge_sat = prm_of(&prm, "verge_sat")? as f32;
    let verge_exg = prm_of(&prm, "verge_exg")? as f32;
    let verge_l = prm_of(&prm, "verge_l")? as f32;
    let ll = ll.to_owned();
    let po: Vec<i64> = po.to_vec();
    let pr: Vec<i64> = pr.to_vec();
    let rl: Vec<i8> = rl.to_vec();
    let out = py.detach(move || {
        let n = (1u64 << zoom) as f64 * TILE as f64;
        let mut tiles: HashMap<(i64, i64), Tile> = HashMap::new();
        let mut road = vec![Samples::default(); r];
        let mut verge = vec![Samples::default(); r];
        let mut asked = vec![0usize; r];
        let mut missing = vec![0usize; r];
        // (offsets across: the road's own, then the two verges, flagged)
        let mut across: Vec<(f64, bool)> = offsets.iter().map(|&o| (o, false)).collect();
        if verge_m > 0.0 {
            across.push((-verge_m, true));
            across.push((verge_m, true));
        }
        for k in 0..q {
            let ri = pr[k] as usize;
            if rl[ri] < 0 {
                continue;
            }
            let (a, b) = (po[k] as usize, po[k + 1] as usize);
            if b < a + 2 {
                continue;
            }
            let c = ll[[a, 1]].to_radians().dcos().max(1e-6);
            let (kx, ky) = (111320.0 * c, 110540.0);
            let mut cum = vec![0f64; b - a];
            for i in a + 1..b {
                let dx = (ll[[i, 0]] - ll[[i - 1, 0]]) * kx;
                let dy = (ll[[i, 1]] - ll[[i - 1, 1]]) * ky;
                cum[i - a] = cum[i - a - 1] + (dx * dx + dy * dy).sqrt();
            }
            let len = cum[b - a - 1];
            if len <= 0.0 {
                continue;
            }
            let skip = end_m.min(0.25 * len);
            let mut s = skip;
            let mut seg = 0usize;
            while s <= len - skip {
                while seg + 2 < b - a && cum[seg + 1] < s {
                    seg += 1;
                }
                let (i0, i1) = (a + seg, a + seg + 1);
                let sl = cum[seg + 1] - cum[seg];
                if sl <= 1e-9 {
                    s += step;
                    continue;
                }
                let t = ((s - cum[seg]) / sl).clamp(0.0, 1.0);
                let (ex, ny) = ((ll[[i1, 0]] - ll[[i0, 0]]) * kx / sl, (ll[[i1, 1]] - ll[[i0, 1]]) * ky / sl);
                let (lon, lat) = (ll[[i0, 0]] + t * (ll[[i1, 0]] - ll[[i0, 0]]),
                                  ll[[i0, 1]] + t * (ll[[i1, 1]] - ll[[i0, 1]]));
                for &(o, is_verge) in &across {
                    // (the normal: left of the direction, (-north, east))
                    let (plon, plat) = (lon + (-ny * o) / kx, lat + (ex * o) / ky);
                    let px = (plon + 180.0) / 360.0 * n;
                    let py_ = (1.0 - plat.to_radians().dtan().dasinh() / std::f64::consts::PI) / 2.0 * n;
                    if !px.is_finite() || !py_.is_finite() {
                        continue;
                    }
                    if !is_verge {
                        asked[ri] += 1;
                    }
                    let (tx, ty) = ((px / TILE as f64).floor() as i64, (py_ / TILE as f64).floor() as i64);
                    let tile = tiles.entry((tx, ty)).or_insert_with(|| load_tile(&root, zoom, tx, ty));
                    let rgb = match tile {
                        Tile::Missing => {
                            if !is_verge {
                                missing[ri] += 1;
                            }
                            continue;
                        }
                        Tile::Nodata => continue,
                        Tile::Rgb(rgb) => rgb,
                    };
                    let ix = ((px - (tx as f64) * TILE as f64) as usize).min(TILE - 1);
                    let iy = ((py_ - (ty as f64) * TILE as f64) as usize).min(TILE - 1);
                    let o3 = (iy * TILE + ix) * 3;
                    let (cr, cg, cb) = (rgb[o3] as f32, rgb[o3 + 1] as f32, rgb[o3 + 2] as f32);
                    let mx = cr.max(cg).max(cb);
                    let mn = cr.min(cg).min(cb);
                    if mx < 10.0 || mn > 248.0 {
                        continue;           // (an export's black / white outside its data)
                    }
                    let l = 0.299 * cr + 0.587 * cg + 0.114 * cb;
                    if l < dark {
                        continue;           // (shade, canopy)
                    }
                    let dst = if is_verge { &mut verge[ri] } else { &mut road[ri] };
                    dst.lum.push(l);
                    dst.sat.push((mx - mn) / mx.max(1.0));
                    dst.exg.push((2.0 * cg - cr - cb) / (cr + cg + cb).max(1.0));
                }
                s += step;
            }
        }
        let read = tiles.values().filter(|t| matches!(t, Tile::Rgb(_))).count();
        let mut feats = vec![[f32::NAN; 9]; r];
        let mut fs: Vec<Option<[f32; NF]>> = vec![None; r];
        for i in 0..r {
            let have = if asked[i] > 0 { (asked[i] - missing[i]) as f32 / asked[i] as f32 } else { 0.0 };
            feats[i][0] = road[i].lum.len() as f32;
            feats[i][5] = have;
            if have >= min_have {
                if let Some(f) = road[i].stats(min_n) {
                    feats[i][1..5].copy_from_slice(&f);
                    fs[i] = Some(f);
                }
            }
            if let Some(v) = verge[i].stats(min_n) {
                feats[i][6..9].copy_from_slice(&v[..3]);
            }
        }
        // the references of each kind: medians and spreads per number
        let model = |kind: i8| -> (Option<([f32; NF], [f32; NF])>, usize) {
            let mut cols: [Vec<f32>; NF] = Default::default();
            for i in 0..r {
                if rl[i] == kind {
                    if let Some(f) = fs[i] {
                        for c in 0..NF {
                            cols[c].push(f[c]);
                        }
                    }
                }
            }
            let m = cols[0].len();
            if m < min_refs {
                return (None, m);
            }
            let mut mu = [0f32; NF];
            let mut sd = [0f32; NF];
            for c in 0..NF {
                mu[c] = median(&mut cols[c]);
                let mut dev: Vec<f32> = cols[c].iter().map(|v| (v - mu[c]).abs()).collect();
                sd[c] = (1.4826 * median(&mut dev)).max(floor[c]);
            }
            (Some((mu, sd)), m)
        };
        let (mp, np) = model(1);
        let (mu_, nu) = model(2);
        let mut verdict = vec![-1i8; r];
        let mut refs = vec![f32::NAN; 17];
        refs[16] = (np * 1000 + nu) as f32;
        let (Some((cp, sp)), Some((cu, su))) = (mp, mu_) else {
            for i in 0..r {
                if rl[i] == 0 && fs[i].is_some() {
                    verdict[i] = 0;
                }
            }
            let flat: Vec<f32> = feats.iter().flat_map(|f| f.iter().copied()).collect();
            return (flat, verdict, refs, read);
        };
        refs[..4].copy_from_slice(&cp);
        refs[4..8].copy_from_slice(&cu);
        refs[8..12].copy_from_slice(&sp);
        refs[12..16].copy_from_slice(&su);
        // (squared distance in the kind's spreads, and the log-likelihood's normalising term)
        let d2 = |f: &[f32; NF], mu: &[f32; NF], sd: &[f32; NF]| -> f32 {
            (0..NF).map(|c| ((f[c] - mu[c]) / sd[c]).powi(2)).sum::<f32>()
        };
        let lnorm = |sd: &[f32; NF]| -> f32 { (0..NF).map(|c| 2.0 * sd[c].dln()).sum::<f32>() };
        let (np_, nu_) = (lnorm(&sp), lnorm(&su));
        for i in 0..r {
            if rl[i] != 0 {
                continue;
            }
            let Some(f) = fs[i] else { continue };
            // a road that does not stand out from vegetation-like verges: a field track, not a paved road
            let (vl, vs, ve) = (feats[i][6], feats[i][7], feats[i][8]);
            if vl.is_finite() && vl >= verge_l && f[0] >= verge_l && (vs >= verge_sat || ve >= verge_exg) {
                let dv = (((f[0] - vl) / scale[0]).powi(2) + ((f[1] - vs) / scale[1]).powi(2)
                    + ((f[2] - ve) / scale[2]).powi(2)).sqrt();
                if dv < vis_min {
                    verdict[i] = 2;
                    continue;
                }
            }
            let (dp, du) = (d2(&f, &cp, &sp), d2(&f, &cu, &su));
            let (lp, lu) = (dp + np_, du + nu_);       // (-2 log-likelihood, up to a constant)
            verdict[i] = if (lp - lu).abs() < margin {
                0
            } else if lp < lu {
                if dp <= outlier { 1 } else { 0 }
            } else if du <= outlier {
                2
            } else {
                0
            };
        }
        let flat: Vec<f32> = feats.iter().flat_map(|f| f.iter().copied()).collect();
        (flat, verdict, refs, read)
    });
    let (flat, verdict, refs, read) = out;
    let feats = numpy::ndarray::Array2::from_shape_vec((r, 9), flat).expect("r x 9");
    Ok((feats.into_pyarray(py), verdict.into_pyarray(py), refs.into_pyarray(py), read))
}
