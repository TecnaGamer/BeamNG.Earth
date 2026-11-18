//! OpenStreetMap over machine-learned building footprints (osmml.py; the user, 2026-11-18). Overture's buildings
//! theme merges OSM with Microsoft's, Google's and Shi et al.'s machine-learned outlines where OSM has none. Where OSM
//! maps the buildings well, an ML-only outline there is no gap filled but an old one: Overture 2026-11-18.0 put ~500
//! Microsoft outlines of demolished rowhouses on Fells Point's vacant lots and building sites. Where OSM maps little
//! (a small town nobody traced, a partly mapped suburb, an unmapped apartment complex) they are the buildings.
//!
//! One map cell's buildings layer at a time, from its own rows only (never a neighbour cell's: the same outcome
//! whatever is loaded, in whatever order). Each outline's place is its bounding box's centre in the cell's metres
//! (east and north of its south-west corner, one cos for the cell), binned in BIN_M squares. An ML outline is
//! dropped when, in the (2 HALF_BINS + 1)^2 bins round its own, OSM has at least MIN_OSM outlines and at least
//! SHARE_NUM / SHARE_DEN of the building area OSM and ML outlines cover together (outer rings, whole m^2): OSM maps
//! the buildings there. Integer sums: the same verdicts on every machine. Area, not counts: a block of small
//! rowhouses would outvote an unmapped complex of large apartment blocks next to it.
//!
//! And an ML outline mostly on OSM buildings (at least DUP_NUM / DUP_DEN of its own footprint, holes out, under OSM
//! footprints, holes out) is a duplicate Overture's conflation left, dropped wherever it is. Its footprint's share is
//! counted on a DUP_GRID x DUP_GRID lattice of points over its box (integer counts: the same verdicts on every
//! machine), against the union of the OSM outlines its box meets: one OSM outline or a row of OSM rowhouses it spans.
//! Of the ML outline's area, not the OSM one's: a small OSM shed or entrance at a large ML warehouse's edge does not
//! drop the warehouse (Codex, 2026-11-18). A building in the other's courtyard is no duplicate.
//!
//! An outline is in every cell its box reaches (overture's unclipped buildings), and each cell sees only its own
//! rows: two cells could judge one outline otherwise. Only its owner (the cell its box centre lies in: Features.own)
//! judges an ML outline; its copies in the other cells are left out (ELSEWHERE), so every reader that has the owner
//! cell gets the owner's verdict and no copy brings back an outline its owner dropped. (They still count in the
//! cell's sums: they are there.)
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub const BIN_M: f64 = 25.0;
pub const HALF_BINS: i64 = 6; // (a 325 m square: Fells Point's cleared blocks are 100-200 m, an unmapped complex 300 m)
pub const MIN_OSM: i64 = 5;
pub const SHARE_NUM: i64 = 7;
pub const SHARE_DEN: i64 = 10;
pub const DUP_GRID: usize = 32; // lattice points a side over an ML outline's box (its footprint's share on OSM's)
pub const DUP_NUM: i64 = 1;
pub const DUP_DEN: i64 = 2;
const M_PER_DEG_LAT: f64 = 110540.0;
const M_PER_DEG_LON: f64 = 111320.0;

/// Source codes (osmml.py's source column).
pub const OTHER: i8 = 0;
pub const OSM: i8 = 1;
pub const ML: i8 = 2;

/// Verdicts.
pub const KEPT: i8 = 0;
pub const COVERED: i8 = 1;
pub const DUPLICATE: i8 = 2;
pub const ELSEWHERE: i8 = 3;

type P = [f64; 2];

fn point_in_ring(p: P, r: &[P]) -> bool {
    let mut inside = false;
    let n = r.len();
    if n < 3 {
        return false;
    }
    let mut j = n - 1;
    for i in 0..n {
        let (a, b) = (r[i], r[j]);
        if (a[1] > p[1]) != (b[1] > p[1]) && p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0] {
            inside = !inside;
        }
        j = i;
    }
    inside
}

/// The shoelace area of a ring (closed or open), m^2.
fn ring_area(r: &[P]) -> f64 {
    let n = r.len();
    if n < 3 {
        return 0.0;
    }
    let mut s = 0.0;
    for k in 0..n {
        let a = r[k];
        let b = r[(k + 1) % n];
        s += a[0] * b[1] - b[0] * a[1];
    }
    (s / 2.0).abs()
}

/// The rows of one buildings layer in the cell's metres.
pub struct Layer {
    pub xy: Vec<P>,
    pub part_off: Vec<usize>,
    pub ring_off: Vec<usize>,
    pub coord_off: Vec<usize>,
    pub source: Vec<i8>,
    pub own: Vec<bool>,
}

impl Layer {
    fn n(&self) -> usize {
        self.source.len()
    }

    /// The outer ring of part p.
    fn outer(&self, p: usize) -> &[P] {
        let r = self.ring_off[p];
        if self.ring_off[p + 1] == r {
            return &[];
        }
        &self.xy[self.coord_off[r]..self.coord_off[r + 1]]
    }

    /// Is p inside part `part` (its outer ring, not in a hole)?
    fn in_part(&self, p: P, part: usize) -> bool {
        let (r0, r1) = (self.ring_off[part], self.ring_off[part + 1]);
        if r1 == r0 || !point_in_ring(p, self.outer(part)) {
            return false;
        }
        !(r0 + 1..r1).any(|r| point_in_ring(p, &self.xy[self.coord_off[r]..self.coord_off[r + 1]]))
    }

    fn parts(&self, i: usize) -> std::ops::Range<usize> {
        self.part_off[i]..self.part_off[i + 1]
    }

    /// Feature i's coordinates (every ring of every part).
    fn coords(&self, i: usize) -> &[P] {
        let c0 = self.coord_off[self.ring_off[self.part_off[i]]];
        let c1 = self.coord_off[self.ring_off[self.part_off[i + 1]]];
        &self.xy[c0..c1]
    }
}

fn bbox(c: &[P]) -> Option<[f64; 4]> {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for p in c {
        b[0] = b[0].min(p[0]);
        b[1] = b[1].min(p[1]);
        b[2] = b[2].max(p[0]);
        b[3] = b[3].max(p[1]);
    }
    if b[0].is_finite() && b[1].is_finite() && b[2].is_finite() && b[3].is_finite() {
        Some(b)
    } else {
        None
    }
}

/// The verdict per row (KEPT / COVERED / DUPLICATE) of a layer whose cell spans w x h metres from (0, 0).
pub fn verdicts(l: &Layer, w: f64, h: f64) -> Vec<i8> {
    let n = l.n();
    let mut out = vec![KEPT; n];
    if !l.source.iter().any(|&s| s == ML) {
        return out;
    }
    let pad = HALF_BINS + 1;
    let span = (w.max(h) / BIN_M).ceil();
    let nb = (if span.is_finite() { span.clamp(1.0, 4096.0) as i64 } else { 1 }) + 2 * pad;
    let bin = |v: f64| -> i64 { ((v / BIN_M).floor() as i64).saturating_add(pad).clamp(0, nb - 1) };
    let boxes: Vec<Option<[f64; 4]>> = (0..n).map(|i| bbox(l.coords(i))).collect();
    let cell = |i: usize| boxes[i].map(|b| (bin((b[0] + b[2]) / 2.0), bin((b[1] + b[3]) / 2.0)));
    // integral images of the OSM count, the OSM area and the ML area (whole m^2 per outline)
    let s = (nb + 1) as usize;
    let mut sums = vec![[0i64; 3]; s * s];
    for i in 0..n {
        if l.source[i] != OSM && l.source[i] != ML {
            continue;
        }
        let Some((gx, gy)) = cell(i) else { continue };
        let a: f64 = l.parts(i).map(|p| ring_area(l.outer(p))).sum();
        let a = if a.is_finite() { a.round().min(1e12) as i64 } else { 0 };
        let k = (gy as usize + 1) * s + gx as usize + 1;
        if l.source[i] == OSM {
            sums[k][0] += 1;
            sums[k][1] += a;
        } else {
            sums[k][2] += a;
        }
    }
    for y in 1..s {
        for x in 1..s {
            for c in 0..3 {
                sums[y * s + x][c] += sums[(y - 1) * s + x][c] + sums[y * s + x - 1][c] - sums[(y - 1) * s + x - 1][c];
            }
        }
    }
    let window = |gx: i64, gy: i64| -> [i64; 3] {
        let (x0, x1) = ((gx - HALF_BINS).max(0) as usize, (gx + HALF_BINS + 1).min(nb) as usize);
        let (y0, y1) = ((gy - HALF_BINS).max(0) as usize, (gy + HALF_BINS + 1).min(nb) as usize);
        let mut o = [0i64; 3];
        for c in 0..3 {
            o[c] = sums[y1 * s + x1][c] - sums[y0 * s + x1][c] - sums[y1 * s + x0][c] + sums[y0 * s + x0][c];
        }
        o
    };
    for i in 0..n {
        if l.source[i] != ML {
            continue;
        }
        if !l.own[i] {
            out[i] = ELSEWHERE;
            continue;
        }
        let Some((gx, gy)) = cell(i) else { continue };
        let [o, oa, ma] = window(gx, gy);
        if o >= MIN_OSM && (oa as i128) * (SHARE_DEN as i128) >= (SHARE_NUM as i128) * ((oa + ma) as i128) {
            out[i] = COVERED;
        }
    }
    // duplicates: the OSM outlines by the bins their boxes cover
    let mut grid: Vec<Vec<u32>> = vec![Vec::new(); (nb * nb) as usize];
    for i in 0..n {
        if l.source[i] != OSM {
            continue;
        }
        let Some(b) = boxes[i] else { continue };
        let (x0, y0, x1, y1) = (bin(b[0]), bin(b[1]), bin(b[2]), bin(b[3]));
        for y in y0..=y1 {
            for x in x0..=x1 {
                grid[(y * nb + x) as usize].push(i as u32);
            }
        }
    }
    let mut stamp = vec![usize::MAX; n];
    let mut near: Vec<usize> = Vec::new();
    for i in 0..n {
        if l.source[i] != ML || out[i] != KEPT || l.part_off[i + 1] == l.part_off[i] {
            continue;
        }
        let Some(b) = boxes[i] else { continue };
        // the OSM outlines whose boxes meet its box
        near.clear();
        let (x0, y0, x1, y1) = (bin(b[0]), bin(b[1]), bin(b[2]), bin(b[3]));
        for y in y0..=y1 {
            for x in x0..=x1 {
                for &j in &grid[(y * nb + x) as usize] {
                    let j = j as usize;
                    if stamp[j] == i {
                        continue;
                    }
                    stamp[j] = i;
                    let Some(bj) = boxes[j] else { continue };
                    if bj[0] > b[2] || bj[2] < b[0] || bj[1] > b[3] || bj[3] < b[1] {
                        continue;
                    }
                    near.push(j);
                }
            }
        }
        if near.is_empty() {
            continue;
        }
        if dup_share(l, i, b, &near, &boxes) {
            out[i] = DUPLICATE;
        }
    }
    out
}

/// Is at least DUP_NUM / DUP_DEN of ML outline i's footprint (box b) on the OSM outlines near? Counted on the
/// DUP_GRID^2 lattice of cell centres over b: its points on i's footprint, and of those the ones on any of near's.
fn dup_share(l: &Layer, i: usize, b: [f64; 4], near: &[usize], boxes: &[Option<[f64; 4]>]) -> bool {
    let g = DUP_GRID as f64;
    let (dx, dy) = ((b[2] - b[0]) / g, (b[3] - b[1]) / g);
    let (mut on_ml, mut on_both) = (0i64, 0i64);
    for ky in 0..DUP_GRID {
        let y = b[1] + (ky as f64 + 0.5) * dy;
        for kx in 0..DUP_GRID {
            let p = [b[0] + (kx as f64 + 0.5) * dx, y];
            if !l.parts(i).any(|q| l.in_part(p, q)) {
                continue;
            }
            on_ml += 1;
            let on_osm = near.iter().any(|&j| {
                boxes[j].is_some_and(|bj| p[0] >= bj[0] && p[0] <= bj[2] && p[1] >= bj[1] && p[1] <= bj[3])
                    && l.parts(j).any(|q| l.in_part(p, q))
            });
            if on_osm {
                on_both += 1;
            }
        }
    }
    on_ml > 0 && on_both * DUP_DEN >= on_ml * DUP_NUM
}

fn offsets(a: &PyReadonlyArray1<'_, i64>, what: &str, len: usize) -> PyResult<Vec<usize>> {
    let v = a.as_array();
    let mut out = Vec::with_capacity(v.len());
    let mut last = 0i64;
    for &x in v.iter() {
        if x < last || x as u64 > len as u64 {
            return Err(PyValueError::new_err(format!("osm_over_ml: {what} must ascend within its target")));
        }
        out.push(x as usize);
        last = x;
    }
    if out.is_empty() {
        return Err(PyValueError::new_err(format!("osm_over_ml: {what} is empty")));
    }
    Ok(out)
}

/// osmml.verdicts: per row of one map cell's buildings layer KEPT (0), COVERED (1: an ML outline where OSM maps the
/// buildings), DUPLICATE (2: an ML outline overlapping an OSM one) or ELSEWHERE (3: an ML outline another cell owns).
/// lonlat (N, 2); the layer's offsets (mapgeom.Features); source per feature (0 other, 1 OpenStreetMap, 2
/// machine-learned); own per feature (Features.own: its box centre is in this cell); bounds the cell's (lon0, lat0,
/// lon1, lat1).
#[pyfunction]
pub fn osm_over_ml<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>, part_off: PyReadonlyArray1<'py, i64>,
                        ring_off: PyReadonlyArray1<'py, i64>, coord_off: PyReadonlyArray1<'py, i64>,
                        source: PyReadonlyArray1<'py, i8>, own: PyReadonlyArray1<'py, bool>,
                        bounds: (f64, f64, f64, f64))
                        -> PyResult<Bound<'py, PyArray1<i8>>> {
    let ll = lonlat.as_array();
    if ll.ncols() != 2 {
        return Err(PyValueError::new_err("osm_over_ml: lonlat must be (N, 2)"));
    }
    let (lon0, lat0, lon1, lat1) = bounds;
    if !(lon0.is_finite() && lat0.is_finite() && lon1.is_finite() && lat1.is_finite()) {
        return Err(PyValueError::new_err("osm_over_ml: the cell's bounds must be finite"));
    }
    let kx = libm::cos(((lat0 + lat1) / 2.0).to_radians()) * M_PER_DEG_LON;
    let xy: Vec<P> = ll.rows().into_iter().map(|r| [(r[0] - lon0) * kx, (r[1] - lat0) * M_PER_DEG_LAT]).collect();
    let coord_off = offsets(&coord_off, "coord_off", xy.len())?;
    let ring_off = offsets(&ring_off, "ring_off", coord_off.len() - 1)?;
    let part_off = offsets(&part_off, "part_off", ring_off.len() - 1)?;
    let source: Vec<i8> = source.as_array().iter().copied().collect();
    if source.len() + 1 != part_off.len() {
        return Err(PyValueError::new_err("osm_over_ml: source needs a value per feature"));
    }
    let own: Vec<bool> = own.as_array().iter().copied().collect();
    if own.len() != source.len() {
        return Err(PyValueError::new_err("osm_over_ml: own needs a value per feature"));
    }
    let l = Layer { xy, part_off, ring_off, coord_off, source, own };
    let w = (lon1 - lon0) * kx;
    let h = (lat1 - lat0) * M_PER_DEG_LAT;
    Ok(verdicts(&l, w, h).into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn square(x: f64, y: f64, s: f64) -> Vec<P> {
        vec![[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]
    }

    fn layer(rings: &[(Vec<P>, i8)]) -> Layer {
        let mut l = Layer { xy: vec![], part_off: vec![0], ring_off: vec![0], coord_off: vec![0], source: vec![],
                            own: vec![] };
        for (r, s) in rings {
            l.xy.extend_from_slice(r);
            l.coord_off.push(l.xy.len());
            l.ring_off.push(l.coord_off.len() - 1);
            l.part_off.push(l.ring_off.len() - 1);
            l.source.push(*s);
            l.own.push(true);
        }
        l
    }

    #[test]
    fn covered_where_osm_maps_the_buildings() {
        // a street of 10 mapped houses and one ML outline on a lot between them
        let mut rings: Vec<(Vec<P>, i8)> = (0..10).map(|k| (square(500.0 + 20.0 * k as f64, 500.0, 12.0), OSM)).collect();
        rings.push((square(600.0, 540.0, 10.0), ML));
        let v = verdicts(&layer(&rings), 1900.0, 1900.0);
        assert_eq!(v[10], COVERED);
        assert!(v[..10].iter().all(|&x| x == KEPT));
    }

    #[test]
    fn kept_where_osm_maps_little() {
        // 3 mapped houses (under MIN_OSM) and 20 ML ones: a town nobody traced
        let mut rings: Vec<(Vec<P>, i8)> = (0..3).map(|k| (square(500.0 + 20.0 * k as f64, 500.0, 12.0), OSM)).collect();
        rings.extend((0..20).map(|k| (square(500.0 + 20.0 * (k % 5) as f64, 530.0 + 20.0 * (k / 5) as f64, 12.0), ML)));
        assert!(verdicts(&layer(&rings), 1900.0, 1900.0).iter().all(|&x| x == KEPT));
        // 10 mapped houses beside an unmapped complex of large blocks: OSM has under SHARE of the area
        let mut rings: Vec<(Vec<P>, i8)> = (0..10).map(|k| (square(500.0 + 20.0 * k as f64, 500.0, 12.0), OSM)).collect();
        rings.extend((0..4).map(|k| (square(500.0 + 50.0 * k as f64, 560.0, 40.0), ML)));
        assert!(verdicts(&layer(&rings), 1900.0, 1900.0).iter().all(|&x| x == KEPT));
    }

    #[test]
    fn duplicate_anywhere() {
        let rings = vec![(square(100.0, 100.0, 20.0), OSM), (square(105.0, 105.0, 10.0), ML),
                         (square(300.0, 300.0, 10.0), ML)];
        let v = verdicts(&layer(&rings), 1900.0, 1900.0);
        assert_eq!(v, vec![KEPT, DUPLICATE, KEPT]);
    }

    #[test]
    fn small_osm_at_a_large_ml_building_no_duplicate() {
        // a sparse cell: a 100 m ML warehouse with a 6 m OSM entrance shed at its edge (and one inside it), no other
        // OSM: the warehouse stays (the shed is under DUP_NUM / DUP_DEN of its footprint)
        let rings = vec![(square(200.0, 200.0, 100.0), ML), (square(197.0, 240.0, 6.0), OSM),
                         (square(250.0, 250.0, 6.0), OSM)];
        assert_eq!(verdicts(&layer(&rings), 1900.0, 1900.0), vec![KEPT, KEPT, KEPT]);
        // its vertex mean on the shed: still no duplicate
        let rings = vec![(square(200.0, 200.0, 100.0), ML), (square(245.0, 245.0, 10.0), OSM)];
        assert_eq!(verdicts(&layer(&rings), 1900.0, 1900.0), vec![KEPT, KEPT]);
    }

    #[test]
    fn duplicate_from_half_its_footprint() {
        // an ML outline 60 % on an OSM one: a duplicate; 40 %: kept
        let rings = vec![(square(100.0, 100.0, 20.0), OSM), (square(108.0, 100.0, 20.0), ML)];
        assert_eq!(verdicts(&layer(&rings), 1900.0, 1900.0), vec![KEPT, DUPLICATE]);
        let rings = vec![(square(100.0, 100.0, 20.0), OSM), (square(112.0, 100.0, 20.0), ML)];
        assert_eq!(verdicts(&layer(&rings), 1900.0, 1900.0), vec![KEPT, KEPT]);
        // one ML outline over a row of three OSM rowhouses (each a third of it): a duplicate of them together
        let rings = vec![(square(100.0, 100.0, 10.0), OSM), (square(110.0, 100.0, 10.0), OSM),
                         (square(120.0, 100.0, 10.0), OSM), (vec![[100.0, 100.0], [130.0, 100.0], [130.0, 110.0],
                                                                  [100.0, 110.0], [100.0, 100.0]], ML)];
        assert_eq!(verdicts(&layer(&rings), 1900.0, 1900.0), vec![KEPT, KEPT, KEPT, DUPLICATE]);
    }

    #[test]
    fn courtyard_no_duplicate_and_copies_elsewhere() {
        // an OSM block round a courtyard (outer 0..60, hole 10..50) and an ML building in the yard
        let mut l = layer(&[]);
        for r in [square(0.0, 0.0, 60.0), square(10.0, 10.0, 40.0)] {
            l.xy.extend_from_slice(&r);
            l.coord_off.push(l.xy.len());
        }
        l.ring_off.push(2);
        l.part_off.push(1);
        l.source.push(OSM);
        l.own.push(true);
        l.xy.extend_from_slice(&square(25.0, 25.0, 10.0));
        l.coord_off.push(l.xy.len());
        l.ring_off.push(3);
        l.part_off.push(2);
        l.source.push(ML);
        l.own.push(true);
        assert_eq!(verdicts(&l, 1900.0, 1900.0), vec![KEPT, KEPT]);
        // and the other way round: an ML block round a courtyard with an OSM shed in it
        l.source = vec![ML, OSM];
        assert_eq!(verdicts(&l, 1900.0, 1900.0), vec![KEPT, KEPT]);
        l.source = vec![OSM, ML];
        l.own[1] = false;
        assert_eq!(verdicts(&l, 1900.0, 1900.0), vec![KEPT, ELSEWHERE]);
    }

    #[test]
    fn other_sources_untouched() {
        let mut rings: Vec<(Vec<P>, i8)> = (0..10).map(|k| (square(500.0 + 20.0 * k as f64, 500.0, 12.0), OSM)).collect();
        rings.push((square(600.0, 540.0, 10.0), OTHER));
        assert!(verdicts(&layer(&rings), 1900.0, 1900.0).iter().all(|&x| x == KEPT));
    }
}
