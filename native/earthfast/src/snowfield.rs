//! The snow field's band tables (local weather S1, ~/bnge-tests/localweather/PLAN_FINAL.md S-1 to S-4): for one forcing
//! cell, the snow on the ground at every 150 m of height where the cell has ground, facing the pole, facing the
//! equator and on the forest floor, from the snow kernel (snow.rs) and the satellites' observed snow cover
//! (snowobs.rs). Replaces W2's 8 bands relative to the model cell's own height (snow.rs BAND_DZ, held past the ends):
//!
//!   - S-2 bands on a fixed absolute grid: row k at k x 150 m, from one row below the cell's lowest ground to one above
//!     its highest (at most MAX_ROWS), so neighbouring cells share their rows and a table never claims snow at heights
//!     where its cell has no ground;
//!   - S-3 the window's first-hour seed from the region: the model snow depth against the model ground height over
//!     the ring's cells of the same model, fitted non-decreasing with height and held above the highest cell (never
//!     extrapolated upward);
//!   - S-4 aspect from the kernel's own runs facing the pole and the equator (the sun on the slope), not a fixed shift:
//!     fresh snow lies on every aspect, only melt separates them;
//!   - S-1 observed snow cover, both ways: the satellites saying bare two days running with no snowfall in the last
//!     36 h at that height take the kernel's snow away; saying snow where the kernel has none gives a thin cover
//!     ("observed, depth unknown": D1 or D2 by the observed share). The kernel's own fresh snowfall always counts (the
//!     maps are a day late and blind under cloud).
//!
//! The rule (the user, 2026-11-27): snow only where real data says it snowed or lies; never from the season, the
//! calendar or the climate. No input here is either.
//!
//! Integers only (as snow.rs): the same records and counts give the same rows on every platform. Rows are the snow
//! state's cache key material (the textures are keyed by the tables), so everything that changes them is an input.

use crate::snow::{self, Ground, Site, BIN_EDGE, COVER_OF_BIN, MISSING, NF};
use crate::snowobs::{band_of, snowline, BAND_M};

/// Rows a table holds at most (6,000 m of relief: more than any 0.25-degree cell has).
pub const MAX_ROWS: usize = 40;
/// Rows past the cell's lowest and highest ground (the look's interpolation reaches them at the extremes).
pub const ROW_PAD: i32 = 1;

/// A band's observation counts at least this many pixels to stand on its own; fewer read the cell's line.
pub const OBS_MIN_PIX: u32 = 3;
/// The cell's line is used only when it holds at least this many pixels ...
pub const LINE_MIN_PIX: u32 = 10;
/// ... and misclassifies at most this share of them (permille): a patchy or inverted cover has no line.
pub const LINE_MAX_ERR: u32 = 300;
/// Observed bare: a snow share below half (permille) ...
pub const BARE_MAX: i64 = 499;
/// ... observed snow where the kernel has none: at least three quarters.
pub const SNOW_MIN: i64 = 750;
/// The kernel's snowfall in the last FRESH_H hours at a band (0.01 mm water) from which its snow stands whatever the
/// maps say (5 mm water, about 5 cm of new snow: the maps are a day late; a smaller amount kept the kernel's too low
/// snowline, P-A).
pub const FRESH_SNOW: i64 = 500;
/// The IMS rows read this many rows higher (a 150 m bias correction: the 1 km pixels' snowline lies below Sentinel-2's,
/// most on slopes facing the sun and in the melt season, P-A 2026-11-27).
pub const OBS_SHIFT_ROWS: i32 = 1;
pub const FRESH_H: usize = 36;
/// The thin cover the maps' snow gives where the kernel has none (depth 0.1 mm): D1 (5 mm) under SNOW_FULL, else D2
/// (2 cm).
pub const THIN_D1: i64 = 50;
pub const THIN_D2: i64 = 200;
pub const SNOW_FULL: i64 = 750;

/// Fused-row flags (bits): the maps vetoed the kernel's snow, the maps added a thin cover; three bits per column (pole
/// 0..2, equator 3..5).
pub const FLAG_VETO: i64 = 1;
pub const FLAG_ADDED: i64 = 2;
/// ... and the maps saw the column bare two days running with no fresh snow (whether or not the kernel had any).
pub const FLAG_BARE: i64 = 4;

/// The rows of a cell: (k_lo, n): rows k_lo..k_lo+n at k x BAND_M m, from ROW_PAD below the band of its lowest ground
/// to ROW_PAD above its highest, the top kept when that is more than MAX_ROWS.
pub fn band_rows(zmin: i32, zmax: i32) -> (i32, usize) {
    let (lo, hi) = (zmin.min(zmax), zmin.max(zmax));
    let k_hi = band_of(hi) + ROW_PAD;
    let k_lo = (band_of(lo) - ROW_PAD).max(k_hi - MAX_ROWS as i32 + 1);
    (k_lo, (k_hi - k_lo + 1) as usize)
}

/// The region's seed profile (S-3): knots (z m, depth mm) non-decreasing in both.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SeedProfile {
    pub z: Vec<i64>,
    pub d: Vec<i64>,
}

/// The seed profile of the ring's cells (model ground height m, model snow depth mm at the window's first hour, one
/// model): sorted by height, pooled where the depth falls with height (pool-adjacent-violators, integer means: an
/// isotonic fit), one knot per pool at its mean height. Cells without a value (MISSING) are left out.
pub fn seed_profile(points: &[(i32, i32)]) -> SeedProfile {
    let mut p: Vec<(i64, i64)> = points.iter().filter(|q| q.0 != MISSING && q.1 != MISSING)
                                       .map(|&(z, d)| (z as i64, (d as i64).max(0))).collect();
    p.sort();
    // pools: (sum z, sum d, count)
    let mut pools: Vec<(i64, i64, i64)> = Vec::new();
    for (z, d) in p {
        pools.push((z, d, 1));
        while pools.len() >= 2 {
            let b = pools[pools.len() - 1];
            let a = pools[pools.len() - 2];
            // a's mean depth above b's: merge
            if a.1 * b.2 > b.1 * a.2 {
                pools.pop();
                let last = pools.len() - 1;
                pools[last] = (a.0 + b.0, a.1 + b.1, a.2 + b.2);
            } else {
                break;
            }
        }
    }
    let mut out = SeedProfile::default();
    for (sz, sd, n) in pools {
        let (z, d) = (sz.div_euclid(n), sd / n);
        if let Some(&lz) = out.z.last() {
            if z <= lz {
                // (two pools at one height after rounding: keep the higher depth, the later pool's)
                let last = out.d.len() - 1;
                out.d[last] = out.d[last].max(d);
                continue;
            }
        }
        out.z.push(z);
        out.d.push(d);
    }
    out
}

/// The seed depth (mm) at a height: between knots linear; below the lowest knot its depth less 25 % per 100 m (snow.rs
/// Ground::seed's rule below the cell); above the highest knot held (no extrapolation upward: the maps take over).
pub fn seed_at(p: &SeedProfile, z: i64) -> i64 {
    let n = p.z.len();
    if n == 0 {
        return 0;
    }
    if z <= p.z[0] {
        let f = (1000 + 250 * (z - p.z[0]) / 100).clamp(0, 1000);
        return p.d[0] * f / 1000;
    }
    for i in 1..n {
        if z <= p.z[i] {
            let (z0, z1, d0, d1) = (p.z[i - 1], p.z[i], p.d[i - 1], p.d[i]);
            return d0 + (d1 - d0) * (z - z0) / (z1 - z0);
        }
    }
    p.d[n - 1]
}

/// One band's kernel run: the open ground's depth facing the pole and the equator and the forest floor's (0.1 mm),
/// hours since the last snowfall (the lesser of the two aspects'), and the snowfall of the last FRESH_H hours on open
/// ground there (0.01 mm water).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BandRun {
    pub depth: [i64; 3],
    pub age: i64,
    pub fresh: i64,
}

/// The kernel at height z (m) of a cell (its model ground z_cell m, latitude x 100) through the records up to `upto`,
/// from a seed depth (mm) at that height: one downscaled hour feeds the three packs.
pub fn run_band(recs: &[&[i32]], upto: usize, z: i32, z_cell: i32, lat_e2: i32, seed_mm: i64) -> BandRun {
    let site = Site { z, z_cell, lat_e2, built: 0, forest: false, aspect: 0 };
    let mut g = [Ground::seed(seed_mm, 0); 3];
    let mut fresh = 0i64;
    let last = upto.min(recs.len().saturating_sub(1));
    for (i, r) in recs.iter().enumerate().take(last + 1) {
        let h = snow::downscale(r, &site);
        g[0].step(&h, false, snow::radiation_index(-1, lat_e2, h.month));
        g[1].step(&h, false, snow::radiation_index(1, lat_e2, h.month));
        g[2].step(&h, true, 1000);
        if i + FRESH_H > last {
            fresh += h.snow;
        }
    }
    BandRun { depth: [g[0].depth(), g[1].depth(), g[2].depth()], age: g[0].age.min(g[1].age) as i64, fresh }
}

/// What the maps say at row k for one column (0 pole, 1 equator): the snow share (permille) of the row's pixels when
/// it has OBS_MIN_PIX of them, else from the cell's line (1000 above it by a row or more, 0 below, nothing next to
/// it), or nothing (no map, no reliable line). counts: rows k0.. of snowobs::band_counts.
pub fn obs_share(counts: &[[u32; 4]], k0: i32, k: i32, col: usize, line: Option<i32>) -> Option<i64> {
    let i = k - k0;
    if i >= 0 && (i as usize) < counts.len() {
        let r = counts[i as usize];
        let (b, s) = (r[2 * col] as i64, r[2 * col + 1] as i64);
        if b + s >= OBS_MIN_PIX as i64 {
            return Some(s * 1000 / (b + s));
        }
    }
    let l = line? as i64;
    let z = (k * BAND_M) as i64;
    if z >= l + BAND_M as i64 {
        Some(1000)
    } else if z <= l - BAND_M as i64 {
        Some(0)
    } else {
        None
    }
}

/// The cell's reliable observed line per column (None: no map, too few pixels, too patchy).
pub fn obs_lines(counts: &[[u32; 4]], k0: i32) -> [Option<i32>; 2] {
    let mut out = [None; 2];
    if counts.is_empty() {
        return out;
    }
    let n = counts.len() as i32;
    for (col, o) in out.iter_mut().enumerate() {
        let (l, err, total) = snowline(counts, k0, col);
        if total >= LINE_MIN_PIX && err * 1000 <= LINE_MAX_ERR * total {
            // (a cell seen all bare (the line above its rows) is bare at every row, its summit too: the 1 km pixel
            // holding the summit was bare; seen all white, white at every row)
            *o = Some(if l >= (k0 + n) * BAND_M - BAND_M / 2 { LINE_NONE } else if l <= k0 * BAND_M - BAND_M / 2 {
                LINE_ALL
            } else {
                l
            });
        }
    }
    out
}

/// obs_lines' line of a cell seen without snow / all white.
pub const LINE_NONE: i32 = i32::MAX / 4;
pub const LINE_ALL: i32 = i32::MIN / 4;

/// A column's depth bin across evaluations (S-7: an hourly table must not re-texture tiles every hour across a bin
/// edge): the bin shown, a candidate and how many evaluations in a row it has held.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BinState {
    pub bin: i64,
    pub cand: i64,
    pub runs: i64,
}

impl BinState {
    pub const NONE: BinState = BinState { bin: -1, cand: -1, runs: 0 };
}

/// The bin to show after this evaluation's raw bin (already with the depth's 15 % hysteresis): no state yet, or a move
/// of two bins or more, takes it at once; a one-bin move only when it held two evaluations in a row.
pub fn settle(prev: BinState, raw: i64) -> BinState {
    if prev.bin < 0 || raw == prev.bin {
        return BinState { bin: raw, cand: -1, runs: 0 };
    }
    let runs = if raw == prev.cand { prev.runs + 1 } else { 1 };
    if runs >= 2 || (raw - prev.bin).abs() >= 2 {
        BinState { bin: raw, cand: -1, runs: 0 }
    } else {
        BinState { bin: prev.bin, cand: raw, runs }
    }
}

/// The fusion's thresholds (S-1; probe P-A chose them against Sentinel-2, tools/snowobs_probe.py tables): a column
/// seen bare (snow share at most bare_max permille, the day before too when two_day) with less than fresh_snow of the
/// kernel's snowfall in FRESH_H hours loses the kernel's snow; seen white (snow_min) where the kernel has none gets
/// the thin cover; the observed rows read shift_rows higher. P-A (35 mountain place-days, 130 lines with a clear
/// Sentinel-2 pass): the plan's rule (25 %, two days, 1 mm, no shift) put the line 407 m below Sentinel-2's (27 %
/// within 150 m); this one 164 m below (66 % within 150 m, 83 % within 300 m); the second day made it worse.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Fusion {
    pub bare_max: i64,
    pub snow_min: i64,
    pub two_day: bool,
    pub fresh_snow: i64,
    pub shift_rows: i32,
}

impl Fusion {
    pub const DEFAULT: Fusion = Fusion { bare_max: BARE_MAX, snow_min: SNOW_MIN, two_day: false, fresh_snow: FRESH_SNOW,
                                         shift_rows: OBS_SHIFT_ROWS };
}

/// One column's fused depth (0.1 mm), cover (0..8), bin state and flags from the kernel's depth and fresh snowfall
/// and the maps' shares today and the day before (S-1), the bin settled from the previous evaluation's (S-7).
pub fn fuse(depth: i64, fresh: i64, today: Option<i64>, before: Option<i64>, prev: BinState)
            -> (i64, i64, BinState, i64) {
    fuse_with(depth, fresh, today, before, prev, &Fusion::DEFAULT)
}

pub fn fuse_with(depth: i64, fresh: i64, today: Option<i64>, before: Option<i64>, prev: BinState, fp: &Fusion)
                 -> (i64, i64, BinState, i64) {
    let bare = |s: Option<i64>| matches!(s, Some(v) if v <= fp.bare_max);
    let (mut d, mut flags) = (depth, 0i64);
    let seen_bare = bare(today) && (!fp.two_day || bare(before)) && fresh < fp.fresh_snow;
    if seen_bare {
        flags |= FLAG_BARE;
        if d > 0 {
            d = 0;
            flags |= FLAG_VETO;
        }
    }
    let added = d < BIN_EDGE[1] as i64 && !seen_bare && matches!(today, Some(s) if s >= fp.snow_min);
    if added {
        d = if today.unwrap_or(0) >= SNOW_FULL { THIN_D2 } else { THIN_D1 };
        flags |= FLAG_ADDED;
    }
    let st = settle(prev, snow::depth_bin(d, prev.bin));
    let mut cover = COVER_OF_BIN[st.bin as usize];
    if added && st.bin > 0 {
        // (the observed share as the cover: half the pixels white is half the ground white)
        cover = ((today.unwrap_or(0) * 8 + 500) / 1000).clamp(1, cover);
    }
    (d, cover, st, flags)
}

/// Columns of a table row (ROW_COLS): z m, then facing the pole cover 0..8 and depth bin 0..6, facing the equator the
/// same, the forest floor's depth bin, the age class 0..3 of the open ground's snow, the flags.
pub const ROW_COLS: usize = 8;

/// A cell's band table (S-1 to S-4): rows k_lo.. (band_rows of its ground zmin..zmax m) for the records up to `upto`
/// (snow.rs layout) of a cell at z_cell m and latitude x 100, seeded from the region's profile; the maps' counts today
/// and the day before (snowobs::band_counts from their rows k0_*; empty: no map); prev: the previous evaluation's
/// bin states by row k ((k, [pole, equator, forest])). Returns (k_lo, rows of ROW_COLS, the rows' new bin states,
/// each row's kernel run (for the logs and tests)).
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn cell_table(f: &[i32], upto: usize, z_cell: i32, lat_e2: i32, zmin: i32, zmax: i32, seed: &SeedProfile,
                  today: &[[u32; 4]], k0_today: i32, before: &[[u32; 4]], k0_before: i32,
                  prev: &[(i32, [BinState; 3])])
                  -> (i32, Vec<[i64; ROW_COLS]>, Vec<(i32, [BinState; 3])>, Vec<BandRun>) {
    cell_table_with(f, upto, z_cell, lat_e2, zmin, zmax, seed, today, k0_today, before, k0_before, prev,
                    &Fusion::DEFAULT)
}

/// cell_table with the fusion's thresholds.
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn cell_table_with(f: &[i32], upto: usize, z_cell: i32, lat_e2: i32, zmin: i32, zmax: i32, seed: &SeedProfile,
                       today: &[[u32; 4]], k0_today: i32, before: &[[u32; 4]], k0_before: i32,
                       prev: &[(i32, [BinState; 3])], fp: &Fusion)
                       -> (i32, Vec<[i64; ROW_COLS]>, Vec<(i32, [BinState; 3])>, Vec<BandRun>) {
    let recs = snow::rows(f);
    let (k_lo, n) = band_rows(zmin, zmax);
    let (k0_today, k0_before) = (k0_today + fp.shift_rows, k0_before + fp.shift_rows);
    let (lt, lb) = (obs_lines(today, k0_today), obs_lines(before, k0_before));
    let mut rows = Vec::with_capacity(n);
    let mut runs = Vec::with_capacity(n);
    let mut states = Vec::with_capacity(n);
    for i in 0..n {
        let k = k_lo + i as i32;
        let z = k * BAND_M;
        let run = if recs.is_empty() { BandRun::default() } else {
            run_band(&recs, upto, z, z_cell, lat_e2, seed_at(seed, z as i64))
        };
        let pb = prev.iter().find(|p| p.0 == k).map(|p| p.1).unwrap_or([BinState::NONE; 3]);
        let mut st = [BinState::NONE; 3];
        let mut row = [0i64; ROW_COLS];
        row[0] = z as i64;
        let mut flags = 0i64;
        let mut any = false;
        for col in 0..2 {
            let t = obs_share(today, k0_today, k, col, lt[col]);
            let b = obs_share(before, k0_before, k, col, lb[col]);
            let (_d, cover, s, fl) = fuse_with(run.depth[col], run.fresh, t, b, pb[col], fp);
            row[1 + 2 * col] = cover;
            row[2 + 2 * col] = s.bin;
            st[col] = s;
            flags |= fl << (3 * col);
            any |= s.bin > 0;
        }
        // the forest floor: the maps see the canopy, not the floor; it follows the open ground (both columns seen
        // bare two days running: none under the trees either) and keeps the kernel's depth otherwise
        let fd = if flags & FLAG_BARE != 0 && flags & (FLAG_BARE << 3) != 0 { 0 } else { run.depth[2] };
        st[2] = settle(pb[2], snow::depth_bin(fd, pb[2].bin));
        row[5] = st[2].bin;
        row[6] = if any { snow::age_class(run.age) } else { 0 };
        row[7] = flags;
        rows.push(row);
        runs.push(run);
        states.push((k, st));
    }
    // the maps' thin cover is "observed, depth unknown": never deeper than the kernel's own snow higher up (a D2 slope
    // under a D1 summit read as a whiter slope than the peak: Pikes Peak, 2026-11-27's IMS)
    for col in 0..2 {
        let mut cap: Option<i64> = None;
        for i in (0..rows.len()).rev() {
            let added = (rows[i][7] >> (3 * col)) & FLAG_ADDED != 0;
            let bin = rows[i][2 + 2 * col];
            if added {
                if let Some(c) = cap {
                    if bin > c {
                        rows[i][2 + 2 * col] = c;
                        rows[i][1 + 2 * col] = rows[i][1 + 2 * col].min(COVER_OF_BIN[c as usize]);
                        states[i].1[col].bin = c;
                    }
                }
            } else if bin > 0 {
                cap = Some(bin);
            }
        }
    }
    (k_lo, rows, states, runs)
}

/// A table's own snowline for a column (0 pole, 1 equator): the snowobs line through its rows, each row counted snow
/// at a cover of 4 or more (half white). (line m, misclassified rows).
pub fn table_line(k_lo: i32, rows: &[[i64; ROW_COLS]], col: usize) -> (i32, u32) {
    let counts: Vec<[u32; 4]> = rows.iter().map(|r| {
        let s = (r[1 + 2 * col] >= 4) as u32;
        [1 - s, s, 1 - s, s]
    }).collect();
    let (l, e, _) = snowline(&counts, k_lo, 0);
    (l, e)
}

/// A hash of many cells' tables from random forcing and random maps (the Linux / Windows determinism check, as
/// snow.rs selftest).
pub fn selftest(seed: u64, cells: usize) -> u64 {
    let mut h = snow::Fnv::new();
    let mut x = seed;
    let mut next = |m: u64| -> u64 {
        x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = x;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        (z ^ (z >> 31)) % m.max(1)
    };
    for c in 0..cells {
        let f = snow::random_records(seed.wrapping_add(c as u64), 246);
        let z_cell = next(3000) as i32;
        let zmin = z_cell - next(800) as i32;
        let zmax = z_cell + next(2500) as i32;
        let lat_e2 = next(15000) as i32 - 7500;
        let pts: Vec<(i32, i32)> = (0..9).map(|_| (z_cell + next(1200) as i32 - 600, next(800) as i32)).collect();
        let seed_p = seed_profile(&pts);
        let (k0, n) = band_rows(zmin - 300, zmax + 300);
        let today: Vec<[u32; 4]> = (0..n).map(|_| [next(6) as u32, next(6) as u32, next(6) as u32, next(6) as u32])
                                         .collect();
        let before: Vec<[u32; 4]> = (0..n).map(|_| [next(4) as u32, next(4) as u32, next(4) as u32, next(4) as u32])
                                          .collect();
        let (k_lo, rows, _st, runs) = cell_table(&f, 239, z_cell, lat_e2, zmin, zmax, &seed_p, &today, k0, &before,
                                                   k0, &[]);
        h.add(k_lo as i64);
        for (r, run) in rows.iter().zip(&runs) {
            for v in r {
                h.add(*v);
            }
            for v in run.depth {
                h.add(v);
            }
            h.add(run.fresh);
        }
    }
    h.0
}

/// Records [n x NF] from a flat slice, for the bindings' checks.
pub fn check_records(f: &[i32]) -> Result<(), String> {
    if f.len() % NF != 0 {
        return Err(format!("snowfield: forcing needs {NF} columns"));
    }
    Ok(())
}

#[cfg(not(snow_standalone))]
pub mod py {
    //! The server's and the tools' calls (server/snowfield.py, tools/snowobs_probe.py).
    use super::*;
    use numpy::PyReadonlyArray2;
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;

    type TableOut = (i32, Vec<Vec<i64>>, Vec<Vec<i64>>, Vec<Vec<i64>>);

    fn counts_of(a: Option<PyReadonlyArray2<'_, i64>>) -> PyResult<Vec<[u32; 4]>> {
        let Some(a) = a else { return Ok(Vec::new()) };
        let v = a.as_array();
        if v.ncols() != 4 {
            return Err(PyValueError::new_err("snowfield: observation counts need 4 columns"));
        }
        Ok(v.rows().into_iter().map(|r| [r[0].clamp(0, u32::MAX as i64) as u32, r[1].clamp(0, u32::MAX as i64) as u32,
                                          r[2].clamp(0, u32::MAX as i64) as u32, r[3].clamp(0, u32::MAX as i64) as u32])
            .collect())
    }

    /// A cell's band table: forcing [n x NF] int32 (snow.rs layout), the current row, the cell (model ground m,
    /// latitude x 100, its DEM ground's lowest and highest m), the region's seed points [(model ground m, model snow
    /// depth mm)], the maps' band counts today and the day before ([rows x 4] int64 from their first row k, or None),
    /// the previous evaluation's bin states [[k, then (bin, candidate, runs) facing the pole, the equator and on the
    /// forest floor]] (S-7). Returns (k_lo, rows [z, pole cover, pole bin, equator cover, equator bin, forest bin,
    /// age class, flags], the new bin states (as prev), runs [pole depth, equator depth, forest depth (0.1 mm), age h,
    /// fresh snowfall 0.01 mm]). fusion: (bare_max, snow_min permille, two_day, fresh_snow 0.01 mm, the observed rows'
    /// shift) for the probe's tuning (None: the defaults, Fusion::DEFAULT).
    #[pyfunction]
    #[pyo3(signature = (forcing, upto, z_cell, lat_e2, zmin, zmax, seed, today=None, k0_today=0, before=None,
                        k0_before=0, prev=Vec::new(), fusion=None))]
    #[allow(clippy::too_many_arguments)]
    pub fn snowfield_table(forcing: PyReadonlyArray2<'_, i32>, upto: usize, z_cell: i32, lat_e2: i32, zmin: i32,
                           zmax: i32, seed: Vec<(i32, i32)>, today: Option<PyReadonlyArray2<'_, i64>>, k0_today: i32,
                           before: Option<PyReadonlyArray2<'_, i64>>, k0_before: i32,
                           prev: Vec<Vec<i64>>, fusion: Option<(i64, i64, bool, i64, i32)>) -> PyResult<TableOut> {
        let a = forcing.as_array();
        if a.ncols() != NF {
            return Err(PyValueError::new_err(format!("snowfield_table: forcing needs {NF} columns, has {}",
                                                     a.ncols())));
        }
        let f: Vec<i32> = a.iter().copied().collect();
        let (t, b) = (counts_of(today)?, counts_of(before)?);
        let mut pv = Vec::with_capacity(prev.len());
        for p in &prev {
            if p.len() != 10 {
                return Err(PyValueError::new_err("snowfield_table: a bin state is [k, 3 x (bin, candidate, runs)]"));
            }
            let s = |i: usize| BinState { bin: p[1 + 3 * i].clamp(-1, 6), cand: p[2 + 3 * i].clamp(-1, 6),
                                          runs: p[3 + 3 * i].clamp(0, 1000) };
            pv.push((p[0] as i32, [s(0), s(1), s(2)]));
        }
        let fp = fusion.map_or(Fusion::DEFAULT, |(bare_max, snow_min, two_day, fresh_snow, shift)| Fusion {
            bare_max: bare_max.clamp(0, 1000), snow_min: snow_min.clamp(0, 1001), two_day, fresh_snow: fresh_snow.max(0),
            shift_rows: shift.clamp(-4, 4),
        });
        let (k_lo, rows, states, runs) = cell_table_with(&f, upto, z_cell, lat_e2, zmin, zmax, &seed_profile(&seed),
                                                         &t, k0_today, &b, k0_before, &pv, &fp);
        Ok((k_lo, rows.iter().map(|r| r.to_vec()).collect(),
            states.iter().map(|(k, s)| {
                let mut v = vec![*k as i64];
                for x in s {
                    v.extend_from_slice(&[x.bin, x.cand, x.runs]);
                }
                v
            }).collect(),
            runs.iter().map(|r| vec![r.depth[0], r.depth[1], r.depth[2], r.age, r.fresh]).collect()))
    }

    /// The band rows (k_lo, n) of a cell's ground zmin..zmax m.
    #[pyfunction]
    pub fn snowfield_rows(zmin: i32, zmax: i32) -> (i32, usize) {
        band_rows(zmin, zmax)
    }

    /// A table's own snowline for a column (0 pole, 1 equator): (line m, misclassified rows).
    #[pyfunction]
    pub fn snowfield_line(k_lo: i32, rows: Vec<Vec<i64>>, col: usize) -> PyResult<(i32, u32)> {
        let mut r = Vec::with_capacity(rows.len());
        for row in rows {
            if row.len() != ROW_COLS {
                return Err(PyValueError::new_err(format!("snowfield_line: rows need {ROW_COLS} columns")));
            }
            let mut a = [0i64; ROW_COLS];
            a.copy_from_slice(&row);
            r.push(a);
        }
        Ok(table_line(k_lo, &r, col.min(1)))
    }

    /// The determinism check's hash (Linux / Windows must agree).
    #[pyfunction]
    pub fn snowfield_selftest(seed: u64, cells: usize) -> u64 {
        selftest(seed, cells)
    }

    pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(snowfield_table, m)?)?;
        m.add_function(wrap_pyfunction!(snowfield_rows, m)?)?;
        m.add_function(wrap_pyfunction!(snowfield_line, m)?)?;
        m.add_function(wrap_pyfunction!(snowfield_selftest, m)?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snow::{F_CLOUD, F_DEPTH, F_HOUR, F_MONTH, F_PRECIP, F_RH, F_SNOWWE, F_SW, F_T2M, F_TD, F_TSURF, F_WIND};

    fn rec(t: i32, p: i32, share: i32, depth_mm: i32) -> [i32; NF] {
        let mut r = [MISSING; NF];
        r[F_T2M] = t;
        r[F_TD] = t - 200;
        r[F_PRECIP] = p;
        r[F_SNOWWE] = p * share / 1000 * 7 / 10;
        r[F_WIND] = 20;
        r[F_SW] = 150;
        r[F_TSURF] = t;
        r[F_RH] = 70;
        r[F_HOUR] = 12;
        r[F_MONTH] = 1;
        r[F_CLOUD] = 30;
        r[F_DEPTH] = depth_mm;
        r
    }

    fn window(spec: &[(usize, i32, i32, i32)], depth_mm: i32) -> Vec<i32> {
        let mut out = Vec::new();
        for &(n, t, p, share) in spec {
            for _ in 0..n {
                out.extend_from_slice(&rec(t, p, share, if out.is_empty() { depth_mm } else { 0 }));
            }
        }
        out
    }

    #[test]
    fn rows_on_the_absolute_grid() {
        assert_eq!(band_rows(1820, 4300), (11, 20)); // 1,650 .. 4,500 m
        // neighbours share their rows' heights: any two cells' rows are the same multiples of 150 m
        let (a, _) = band_rows(100, 900);
        assert_eq!(a, 0);
        // too much relief: the top kept
        let (k, n) = band_rows(0, 8800);
        assert_eq!(n, MAX_ROWS);
        assert_eq!(k + n as i32 - 1, band_of(8800) + ROW_PAD);
    }

    #[test]
    fn seed_profile_is_isotonic_and_held_above() {
        // model cells: deeper higher up, one low cell with drifted snow out of order
        let p = seed_profile(&[(1000, 100), (1200, 40), (1500, 200), (2400, 600), (MISSING, 5)]);
        for w in p.d.windows(2) {
            assert!(w[0] <= w[1]);
        }
        assert_eq!(p.z.len(), 3);
        assert_eq!(p.d[0], 70); // (1000, 100) and (1200, 40) pooled at 1,100 m
        assert_eq!(seed_at(&p, 4000), 600); // held, not lapsed upward
        assert_eq!(seed_at(&p, 1100), 70);
        assert_eq!(seed_at(&p, 900), 70 * 500 / 1000); // 200 m below: -50 %
        assert_eq!(seed_at(&SeedProfile::default(), 3000), 0);
    }

    #[test]
    fn mild_winter_dry_window_is_bare_everywhere() {
        // ten dry January days at +6 degC over a 100 m cell (Atlanta-like): no row has snow, kernel or maps
        let f = window(&[(246, 600, 0, 0)], 0);
        let (k_lo, rows, _, _) = cell_table(&f, 239, 100, 3375, 50, 350, &seed_profile(&[(100, 0)]), &[], 0, &[], 0, &[]);
        assert_eq!(k_lo, -1);
        assert!(rows.iter().all(|r| r[1] == 0 && r[3] == 0 && r[5] == 0), "{rows:?}");
        // the maps saying bare change nothing but the flags (the logs'; never in a texture key)
        let bare = vec![[20u32, 0, 20, 0]; rows.len()];
        let (_, rows2, _, _) = cell_table(&f, 239, 100, 3375, 50, 350, &seed_profile(&[(100, 0)]), &bare, k_lo, &bare,
                                       k_lo, &[]);
        let look = |r: &Vec<[i64; ROW_COLS]>| r.iter().map(|x| x[..7].to_vec()).collect::<Vec<_>>();
        assert_eq!(look(&rows), look(&rows2));
        assert!(rows2.iter().all(|r| r[7] == FLAG_BARE | FLAG_BARE << 3));
    }

    #[test]
    fn dry_spell_peaks_keep_observed_old_snow() {
        // ten dry, mild days at a 1,500 m model cell with no model snow: the kernel has nothing up to 4,000 m; the
        // maps see snow from 2,700 m on the pole side and 3,000 m facing the sun: thin observed cover there, none below
        // (read OBS_SHIFT_ROWS higher: 2,850 and 3,150 m)
        let f = window(&[(246, 300, 0, 0)], 0);
        let (k_lo, n) = band_rows(1200, 4000);
        let counts: Vec<[u32; 4]> = (0..n).map(|i| {
            let z = (k_lo + i as i32) * BAND_M;
            let p = if z >= 2700 { [0, 6] } else { [6, 0] };
            let e = if z >= 3000 { [0, 6] } else { [6, 0] };
            [p[0], p[1], e[0], e[1]]
        }).collect();
        let (k, rows, _, runs) = cell_table(&f, 239, 1500, 4600, 1200, 4000, &seed_profile(&[(1500, 0)]), &counts,
                                         k_lo, &counts, k_lo, &[]);
        assert!(runs.iter().all(|r| r.depth == [0, 0, 0]));
        let (lp, _) = table_line(k, &rows, 0);
        let (le, _) = table_line(k, &rows, 1);
        assert_eq!((lp, le), (2625 + BAND_M * OBS_SHIFT_ROWS, 2925 + BAND_M * OBS_SHIFT_ROWS));
        let from = 2700 + BAND_M as i64 * OBS_SHIFT_ROWS as i64;
        assert!(rows.iter().filter(|r| r[0] >= from).all(|r| r[2] == 2 && r[7] & FLAG_ADDED != 0), "{rows:?}");
        // without the shift: the maps' own heights
        let fp = Fusion { shift_rows: 0, ..Fusion::DEFAULT };
        let (k, rows, _, _) = cell_table_with(&f, 239, 1500, 4600, 1200, 4000, &seed_profile(&[(1500, 0)]), &counts,
                                              k_lo, &counts, k_lo, &[], &fp);
        assert_eq!((table_line(k, &rows, 0).0, table_line(k, &rows, 1).0), (2625, 2925));
    }

    #[test]
    fn observed_bare_vetoes_old_model_snow_but_not_fresh_snow() {
        // the model's seed has 20 cm everywhere, then nine mild dry days: the kernel still holds snow at 1,800 m
        let f = window(&[(240, 150, 0, 0), (6, 150, 0, 0)], 200);
        let seed = seed_profile(&[(1000, 200)]);
        let (k_lo, n) = band_rows(1000, 1800);
        let bare: Vec<[u32; 4]> = vec![[8, 0, 8, 0]; n];
        let (_, base, _, runs) = cell_table(&f, 245, 1000, 4600, 1000, 1800, &seed, &[], 0, &[], 0, &[]);
        assert!(runs.last().unwrap().depth[0] > 0 && base.last().unwrap()[1] > 0);
        // bare two days running: vetoed
        let (_, v, _, _) = cell_table(&f, 245, 1000, 4600, 1000, 1800, &seed, &bare, k_lo, &bare, k_lo, &[]);
        assert!(v.iter().all(|r| r[1] == 0 && r[3] == 0 && r[5] == 0), "{v:?}");
        assert!(v.last().unwrap()[7] & FLAG_VETO != 0);
        // the pixels reach only the lower rows (the summit's 1 km pixel among them, its mean lower): the rows above
        // them, the padding row too, are bare as well (the cell seen all bare)
        let low: Vec<[u32; 4]> = (0..n).map(|i| if i + 3 < n { [8, 0, 8, 0] } else { [0; 4] }).collect();
        let (_, v2, _, _) = cell_table(&f, 245, 1000, 4600, 1000, 1800, &seed, &low, k_lo, &low, k_lo, &[]);
        assert!(v2.iter().all(|r| r[1] == 0 && r[3] == 0 && r[5] == 0), "{v2:?}");
        // ... and a cell seen all white is white to its lowest row
        let white: Vec<[u32; 4]> = (0..n).map(|i| if i >= 2 { [0, 8, 0, 8] } else { [0; 4] }).collect();
        let dry = window(&[(246, 600, 0, 0)], 0);
        let (_, w2, _, _) = cell_table(&dry, 245, 1000, 4600, 1000, 1800, &seed_profile(&[(1000, 0)]), &white, k_lo,
                                       &white, k_lo, &[]);
        assert!(w2.iter().all(|r| r[2] > 0 && r[4] > 0), "{w2:?}");
        // bare today only (yesterday unknown): vetoed by the default (one day, P-A), kept by a two-day rule
        let (_, k1, _, _) = cell_table(&f, 245, 1000, 4600, 1000, 1800, &seed, &bare, k_lo, &[], 0, &[]);
        assert!(k1.iter().all(|r| r[1] == 0 && r[3] == 0), "{k1:?}");
        let two = Fusion { two_day: true, ..Fusion::DEFAULT };
        let (_, k2, _, _) = cell_table_with(&f, 245, 1000, 4600, 1000, 1800, &seed, &bare, k_lo, &[], 0, &[], &two);
        assert_eq!(k2.last().unwrap()[2], base.last().unwrap()[2]);
        // fresh snow in the last day (8 h of snow at -3 degC): stands though the maps are a day late
        let g = window(&[(232, 150, 0, 0), (8, -300, 100, 1000), (6, -300, 0, 0)], 200);
        let (_, fr, _, _) = cell_table(&g, 245, 1000, 4600, 1000, 1800, &seed, &bare, k_lo, &bare, k_lo, &[]);
        assert!(fr.iter().all(|r| r[2] > 0 && r[7] & FLAG_VETO == 0), "{fr:?}");
    }

    #[test]
    fn aspect_from_the_runs_not_a_shift() {
        // a fresh storm yesterday: both aspects white alike (no invented split on the day of a storm)
        let f = window(&[(200, -500, 0, 0), (24, -400, 80, 1000), (22, -600, 0, 0)], 0);
        let (_, rows, _, runs) = cell_table(&f, 245, 1000, 4600, 900, 1400, &seed_profile(&[(1000, 0)]), &[], 0, &[], 0,
                                         &[]);
        assert!(rows.iter().all(|r| r[1] == r[3] && r[2] == r[4]), "{rows:?}");
        assert!(runs.iter().all(|r| r.depth[0] > 0));
        // weeks of sun later in spring: the equator side melts first
        let g = window(&[(150, -500, 0, 0), (24, -400, 80, 1000), (72, 100, 0, 0)], 0);
        let (_, _, _, runs) = cell_table(&g, 245, 1000, 4600, 900, 1400, &seed_profile(&[(1000, 0)]), &[], 0, &[], 0,
                                      &[]);
        assert!(runs.iter().any(|r| r.depth[0] > r.depth[1]), "{runs:?}");
    }

    #[test]
    fn observed_cover_never_deeper_than_the_kernels_above() {
        // a cold dry window, the model's dusting only near the top (15 mm at 4,000 m, none at 3,000 m), the maps white
        // everywhere: the rows the maps fill in below stay a dusting too, not a 2 cm cover under a dusted summit
        let f = window(&[(246, -1000, 0, 0)], 0);
        let seed = seed_profile(&[(3000, 0), (4000, 15)]);
        let (k_lo, n) = band_rows(2500, 4200);
        let white: Vec<[u32; 4]> = vec![[0, 8, 0, 8]; n];
        let (_, rows, st, _) = cell_table(&f, 245, 3000, 3900, 2500, 4200, &seed, &white, k_lo, &white, k_lo, &[]);
        assert!(rows.iter().any(|r| r[7] & FLAG_ADDED != 0), "{rows:?}");
        assert!(rows.iter().any(|r| r[7] & FLAG_ADDED == 0 && r[2] == 1), "{rows:?}");
        assert!(rows.iter().all(|r| r[2] <= 1 && r[4] <= 1), "{rows:?}");
        assert!(st.iter().all(|s| s.1[0].bin <= 1));
        // with no kernel snow anywhere the maps' cover stands as observed (D2)
        let (_, rows, _, _) = cell_table(&f, 245, 3000, 3900, 2500, 4200, &seed_profile(&[(3000, 0)]), &white, k_lo,
                                         &white, k_lo, &[]);
        assert!(rows.iter().all(|r| r[2] == 2), "{rows:?}");
    }

    #[test]
    fn bins_settle_over_two_evaluations() {
        let s0 = settle(BinState::NONE, 2);
        assert_eq!(s0.bin, 2);
        // one bin up once: held; again: taken
        let s1 = settle(s0, 3);
        assert_eq!((s1.bin, s1.cand, s1.runs), (2, 3, 1));
        assert_eq!(settle(s1, 3).bin, 3);
        // a flicker back and forth never moves the bin
        let s2 = settle(settle(s1, 2), 3);
        assert_eq!(s2.bin, 2);
        // two bins at once (a storm, a veto): taken at once
        assert_eq!(settle(s0, 0).bin, 0);
        assert_eq!(settle(s0, 4).bin, 4);
    }

    #[test]
    fn selftest_is_stable() {
        assert_eq!(selftest(1, 8), selftest(1, 8));
        assert_ne!(selftest(1, 8), selftest(2, 8));
    }
}
