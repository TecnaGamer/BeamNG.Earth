//! A corridor's geotiles: every zoom-17 tile (the slippy-map numbering geotiles.py uses) within a half width of the
//! corridor's lines, each densified every SPACING_M, in the order the lines first reach them; the first line (the
//! route) optionally clipped to a stretch between two mileposts first.
//!
//!   roadscan tiles LINES.txt OUT.tsv [--halfwidth M] [--clip A,B] [--route-out ROUTE.bin]
//! LINES.txt: "lat,lon" per line, a blank line between two polylines. OUT.tsv: key gx gy s_m lat lon (the tile's
//! centre: geotiles.info's), the last line "# miles M". --clip A,B keeps miles A to B of the first line.
use std::collections::BTreeMap;
use std::fmt::Write as _;

pub const Z: u32 = 17;
const SPACING_M: f64 = 10.0;
const M_PER_DEG: f64 = 111_320.0;

fn n() -> f64 { (1u64 << Z) as f64 }

/// (x, y) of the tile under a point.
pub fn tile_of(lat: f64, lon: f64) -> (i64, i64) {
    let x = ((lon + 180.0) / 360.0 * n()).floor() as i64;
    let y = ((1.0 - lat.to_radians().tan().asinh() / std::f64::consts::PI) / 2.0 * n()).floor() as i64;
    (x, y)
}

fn lat_of(y: f64) -> f64 { (std::f64::consts::PI * (1.0 - 2.0 * y / n())).sinh().atan().to_degrees() }
fn lon_of(x: f64) -> f64 { x / n() * 360.0 - 180.0 }

/// (south, west, north, east) of a tile.
pub fn bounds(x: i64, y: i64) -> (f64, f64, f64, f64) {
    (lat_of((y + 1) as f64), lon_of(x as f64), lat_of(y as f64), lon_of((x + 1) as f64))
}

pub fn centre(x: i64, y: i64) -> (f64, f64) { (lat_of(y as f64 + 0.5), lon_of(x as f64 + 0.5)) }

pub fn dist(a: (f64, f64), b: (f64, f64)) -> f64 { crate::rank::dist_m(a, b) }

/// m from a point to a tile's lat / lon box (0 inside).
fn box_dist(lat: f64, lon: f64, x: i64, y: i64) -> f64 {
    let (s, w, nn, e) = bounds(x, y);
    let dy = if lat < s { s - lat } else if lat > nn { lat - nn } else { 0.0 } * M_PER_DEG;
    let dx = if lon < w { w - lon } else if lon > e { lon - e } else { 0.0 } * M_PER_DEG * lat.to_radians().cos();
    (dx * dx + dy * dy).sqrt()
}

/// Miles a to b of a polyline (cut at the mileposts).
pub fn clip(line: &[(f64, f64)], a_mi: f64, b_mi: f64) -> Vec<(f64, f64)> {
    let (a, b) = (a_mi * 1609.344, b_mi * 1609.344);
    let mut out = Vec::new();
    let mut s = 0.0;
    for w in line.windows(2) {
        let d = dist(w[0], w[1]);
        let (s0, s1) = (s, s + d);
        s = s1;
        if s1 < a || s0 > b || d <= 0.0 { continue; }
        let at = |t: f64| (w[0].0 + (w[1].0 - w[0].0) * t, w[0].1 + (w[1].1 - w[0].1) * t);
        if out.is_empty() { out.push(at(((a - s0) / d).clamp(0.0, 1.0))); }
        out.push(at(((b - s0) / d).clamp(0.0, 1.0)));
    }
    out.dedup();
    out
}

/// {key: (gx, gy, metres along at the first touch)} of the tiles within halfwidth of the lines.
pub fn tiles_along(lines: &[Vec<(f64, f64)>], halfwidth: f64) -> BTreeMap<(i64, i64), f64> {
    let mut got: BTreeMap<(i64, i64), f64> = BTreeMap::new();
    let mut base = 0.0;
    for line in lines {
        let mut s = 0.0;
        for (k, a) in line.iter().enumerate() {
            let b = line.get(k + 1).copied().unwrap_or(*a);
            let d = dist(*a, b);
            let steps = ((d / SPACING_M) as usize).max(1);
            for i in 0..steps {
                let t = i as f64 / steps as f64;
                let (la, lo) = (a.0 + (b.0 - a.0) * t, a.1 + (b.1 - a.1) * t);
                let (cx, cy) = tile_of(la, lo);
                // (a tile is at least 100 m across up to 70 degrees: two rings reach any half width under that)
                let r = (halfwidth / 100.0).ceil() as i64 + 1;
                for x in cx - r..=cx + r {
                    for y in cy - r..=cy + r {
                        if box_dist(la, lo, x, y) <= halfwidth { got.entry((x, y)).or_insert(base + s + d * t); }
                    }
                }
            }
            s += d;
        }
        base += s;
    }
    got
}

pub fn read_lines(text: &str) -> Result<Vec<Vec<(f64, f64)>>, String> {
    let mut out = vec![Vec::new()];
    for ln in text.lines() {
        let ln = ln.trim();
        if ln.starts_with('#') { continue; }
        if ln.is_empty() { if !out.last().unwrap().is_empty() { out.push(Vec::new()); } continue; }
        let mut w = ln.split(',');
        let (Some(a), Some(b)) = (w.next(), w.next()) else { return Err(format!("not lat,lon: {}", ln)) };
        let (la, lo): (f64, f64) = (a.trim().parse().map_err(|_| format!("bad latitude: {}", ln))?, b.trim().parse().map_err(|_| format!("bad longitude: {}", ln))?);
        if !(-85.0..=85.0).contains(&la) || !(-180.0..=180.0).contains(&lo) { return Err(format!("out of range: {}", ln)); }
        out.last_mut().unwrap().push((la, lo));
    }
    out.retain(|l| l.len() >= 2);
    Ok(out)
}

pub fn route_bin(pts: &[(f64, f64)]) -> Vec<u8> {
    let mut b = b"RSROUTE1".to_vec();
    b.extend((pts.len() as u32).to_le_bytes());
    for p in pts { b.extend(p.0.to_le_bytes()); b.extend(p.1.to_le_bytes()); }
    b
}

/// The corridor's tiles as TSV (in the order the lines reach them) and its miles.
pub fn run(lines: &[Vec<(f64, f64)>], halfwidth: f64) -> String {
    let got = tiles_along(lines, halfwidth);
    let mut v: Vec<((i64, i64), f64)> = got.into_iter().collect();
    v.sort_by(|a, b| a.1.total_cmp(&b.1).then(a.0.cmp(&b.0)));
    let mut s = String::from("key\tgx\tgy\ts_m\tlat\tlon\n");
    for ((x, y), d) in &v {
        let (la, lo) = centre(*x, *y);
        let _ = writeln!(s, "g{}_{}_{}\t{}\t{}\t{:.1}\t{:.9}\t{:.9}", Z, x, y, x, y, d, la, lo);
    }
    let miles: f64 = lines.iter().map(|l| l.windows(2).map(|w| dist(w[0], w[1])).sum::<f64>()).sum::<f64>() / 1609.344;
    let _ = writeln!(s, "# miles {:.3}", miles);
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slippy_numbering() {
        // geotiles.tile_of / centre (the server's) for Fort McHenry and Westwood
        assert_eq!(tile_of(39.2642, -76.5894), (37650, 49968));
        assert_eq!(tile_of(34.04715, -118.44662), (22410, 52338));
        let (la, lo) = centre(37650, 49968);
        assert!((la - 39.265221194601).abs() < 1e-9 && (lo + 76.58981323242188).abs() < 1e-9);
    }

    #[test]
    fn along_a_line() {
        let line = vec![(39.28, -76.60), (39.28, -76.59)];       // ~0.9 km east
        let got = tiles_along(&[line.clone()], 40.0);
        assert_eq!(got, tiles_along(&[line], 40.0));
        let mut v: Vec<_> = got.iter().collect();
        v.sort_by(|a, b| a.1.total_cmp(b.1));
        let xs: Vec<i64> = v.iter().map(|k| (k.0).0).collect();
        assert!(xs.windows(2).all(|w| w[0] <= w[1]));
        assert!(got.len() >= 3);
    }

    #[test]
    fn clipping() {
        let d = 1609.344 / M_PER_DEG;
        let line = vec![(40.0, -75.0), (40.0 + 3.0 * d, -75.0)];
        let c = clip(&line, 1.0, 2.0);
        assert_eq!(c.len(), 2);
        assert!((dist(c[0], c[1]) - 1609.344).abs() < 0.5);
        assert!((dist(line[0], c[0]) - 1609.344).abs() < 0.5);
    }
}
