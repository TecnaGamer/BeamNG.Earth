//! What stands around a building: how many of its kind stand near it (series, the same block repeated in a
//! micro-district), the nearest road, how the building lies to it.
use super::features::Feat;
use crate::dm::Dm;
use std::collections::HashMap;

pub const SERIAL_R_M: f64 = 150.0;
const BIN_M: f64 = 48.0;
const ROAD_BIN_M: f64 = 64.0;
pub const ROAD_R_M: f64 = 120.0;

pub struct Around {
    /// Buildings within SERIAL_R_M with the same width (+-1.2 m), floors (+-0) and an axis parallel or square to this one's.
    pub serial: u32,
    /// Distance to the nearest road (ROAD_R_M + 1 where none), the angle between the long axis and that road in [0, 90]
    /// degrees, and which side of the long axis the road is on (+1 left of u, -1 right, 0 none).
    pub road_d: f64,
    pub road_ang: f64,
    pub road_side: f64,
}

fn axis_diff(a: &Feat, b: &Feat) -> f64 {
    // degrees between two long axes, as lines (0..90)
    let dot = (a.ux * b.ux + a.uy * b.uy).abs().min(1.0);
    dot.dacos().to_degrees()
}

fn bin(v: f64, size: f64) -> i64 {
    (v / size).floor() as i64
}

/// `floors` per building (0 unknown); roads as segments x0, y0, x1, y1.
pub fn around(feats: &[Option<Feat>], floors: &[i32], roads: &[[f64; 4]]) -> Vec<Around> {
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for (i, f) in feats.iter().enumerate() {
        if let Some(f) = f {
            grid.entry((bin(f.cx, BIN_M), bin(f.cy, BIN_M))).or_default().push(i);
        }
    }
    let mut rgrid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for (k, s) in roads.iter().enumerate() {
        let (x0, x1) = (s[0].min(s[2]) - ROAD_R_M, s[0].max(s[2]) + ROAD_R_M);
        let (y0, y1) = (s[1].min(s[3]) - ROAD_R_M, s[1].max(s[3]) + ROAD_R_M);
        for bx in bin(x0, ROAD_BIN_M)..=bin(x1, ROAD_BIN_M) {
            for by in bin(y0, ROAD_BIN_M)..=bin(y1, ROAD_BIN_M) {
                rgrid.entry((bx, by)).or_default().push(k);
            }
        }
    }
    let reach = (SERIAL_R_M / BIN_M).ceil() as i64;
    feats
        .iter()
        .enumerate()
        .map(|(i, f)| {
            let mut out = Around { serial: 0, road_d: ROAD_R_M + 1.0, road_ang: 90.0, road_side: 0.0 };
            let Some(f) = f else { return out };
            let (bx, by) = (bin(f.cx, BIN_M), bin(f.cy, BIN_M));
            for gx in bx - reach..=bx + reach {
                for gy in by - reach..=by + reach {
                    let Some(list) = grid.get(&(gx, gy)) else { continue };
                    for &j in list {
                        if j == i {
                            continue;
                        }
                        let Some(g) = feats[j].as_ref() else { continue };
                        if floors[i] <= 0 || floors[i] != floors[j] || (f.arm - g.arm).abs() > 1.2 {
                            continue;
                        }
                        if (f.cx - g.cx).dhypot(f.cy - g.cy) > SERIAL_R_M {
                            continue;
                        }
                        let a = axis_diff(f, g);
                        if a < 6.0 || a > 84.0 {
                            out.serial += 1;
                        }
                    }
                }
            }
            if let Some(list) = rgrid.get(&(bin(f.cx, ROAD_BIN_M), bin(f.cy, ROAD_BIN_M))) {
                for &k in list {
                    let s = &roads[k];
                    let (dx, dy) = (s[2] - s[0], s[3] - s[1]);
                    let len2 = dx * dx + dy * dy;
                    if len2 < 1e-9 {
                        continue;
                    }
                    let t = (((f.cx - s[0]) * dx + (f.cy - s[1]) * dy) / len2).clamp(0.0, 1.0);
                    let (px, py) = (s[0] + t * dx, s[1] + t * dy);
                    let d = (f.cx - px).dhypot(f.cy - py);
                    if d < out.road_d {
                        let len = len2.sqrt();
                        let dot = ((dx * f.ux + dy * f.uy) / len).abs().min(1.0);
                        out.road_d = d;
                        out.road_ang = dot.dacos().to_degrees();
                        let cr = f.ux * (py - f.cy) - f.uy * (px - f.cx);
                        out.road_side = if cr > 0.0 { 1.0 } else { -1.0 };
                    }
                }
            }
            // (the road is measured from the centre: the wall is half the width nearer)
            if out.road_d <= ROAD_R_M {
                out.road_d = (out.road_d - f.w * 0.5).max(0.0);
            }
            out
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::super::features::features;
    use super::*;

    fn feat(cx: f64, cy: f64, l: f64, w: f64) -> Option<Feat> {
        let xs = [cx - l / 2.0, cx + l / 2.0, cx + l / 2.0, cx - l / 2.0];
        let ys = [cy - w / 2.0, cy - w / 2.0, cy + w / 2.0, cy + w / 2.0];
        features(&xs, &ys)
    }

    #[test]
    fn a_row_of_the_same_block_is_a_series() {
        let feats = vec![feat(0.0, 0.0, 60.0, 11.5), feat(0.0, 40.0, 62.0, 11.5), feat(0.0, 80.0, 60.0, 11.5), feat(500.0, 0.0, 60.0, 11.5)];
        let a = around(&feats, &[5, 5, 5, 5], &[]);
        assert_eq!(a[0].serial, 2);
        assert_eq!(a[1].serial, 2);
        assert_eq!(a[3].serial, 0, "too far");
    }

    #[test]
    fn a_different_floor_count_is_not_the_same_series() {
        let feats = vec![feat(0.0, 0.0, 60.0, 11.5), feat(0.0, 40.0, 60.0, 11.5)];
        assert_eq!(around(&feats, &[5, 9], &[])[0].serial, 0);
    }

    #[test]
    fn the_nearest_road_and_its_side() {
        let feats = vec![feat(0.0, 0.0, 60.0, 12.0)];
        // a road 30 m north of the centre, parallel to the long (x) axis
        let a = around(&feats, &[5], &[[-100.0, 30.0, 100.0, 30.0]]);
        assert!((a[0].road_d - 24.0).abs() < 1e-6, "{}", a[0].road_d);
        assert!(a[0].road_ang < 1.0);
        assert_eq!(a[0].road_side.abs(), 1.0);
        // a road across the building's end
        let b = around(&feats, &[5], &[[-60.0, -100.0, -60.0, 100.0]]);
        assert!(b[0].road_ang > 89.0);
        assert!((around(&feats, &[5], &[])[0].road_d - (ROAD_R_M + 1.0)).abs() < 1e-9);
    }
}
