//! The world server's RAM budget (server/budget.py, memwatch.py; the settings page's "Max RAM for BeamNG.Earth"):
//! the limit for the whole server (its main process and every worker together), its tier, the desktop's sizes by it,
//! and the watchdog's step.
//!
//! Units: MB of 2^20 bytes throughout. The limit is the player's choice, or Auto:
//!   below BIG_GB of RAM, AUTO_FRAC of it (the game holds ~6.5-7.5 GB, the system and other programs the rest), at
//!   least AUTO_MIN_MB: a Steam Deck 4.8 GB, a 16 GB PC 5.3 GB, where the server's 8-12 GB swapped the game out.
//!   From BIG_GB up, BIG_FRAC of it, at most AUTO_MAX_MB (32 GB: 17.9 GB, 64 GB and up: 24 GB): there memory is not
//!   what makes the game hitch (the players' reports, 2026-11-12: 335 hitches over 100 ms an hour at 32 GB, 357 at 64,
//!   the swap-tagged ones on 8-16 GB machines), and a third of the RAM cost world generation 11 % (32 GB) to 31-41 %
//!   (64 GB, a 16 GB cap) in game-less cold city drives; the cap still bounds the growth (try598's server reached 20.3
//!   GB on the 32 GB profile and 22.8-29 GB on 64 GB). Auto puts every machine on the tier the RAM alone gave it before
//!   the setting existed (TIER_GB: the old thresholds times AUTO_FRAC), so the setting is one model with the old tiers
//!   at its low end.
//! The watchdog (ram_watch, every few seconds in the server's main process): the server's measured total (Linux PSS +
//! swapped PSS, Windows private bytes) against the limit, lowered while the system itself is short of memory (less
//! than the reserve available); a soft step asks every process to drop its caches, a hard one replaces the processes
//! of the pool holding the largest one (its jobs finish in the old processes, which then exit), with hysteresis and
//! gaps between the steps (like Kubernetes' soft / hard eviction thresholds and grace periods).

use pyo3::prelude::*;

pub const AUTO_FRAC: f64 = 0.33;
pub const AUTO_MIN_MB: f64 = 1536.0;
/// A big machine (GB of 10^9 bytes: a "24 GB" PC shows ~23.5-25.7) and its share, up to AUTO_MAX_MB
pub const BIG_GB: f64 = 22.0;
pub const BIG_FRAC: f64 = 0.6;
pub const AUTO_MAX_MB: f64 = 24576.0;
/// An explicit limit is at least this, and at most EXPLICIT_MAX_FRAC of the RAM
pub const EXPLICIT_MIN_MB: f64 = 1024.0;
pub const EXPLICIT_MAX_FRAC: f64 = 0.9;
/// The tiers' RAM thresholds before the setting (budget.py EMERGENCY_GB, LEAN_GB, SMALL_GB: GB of 10^9 bytes)
const TIER_GB: [(f64, &str); 3] = [(6.0, "emergency"), (10.0, "lean"), (20.0, "small")];

fn gb_to_mb(gb: f64) -> f64 {
    gb * 1e9 / 1048576.0
}

/// Auto's limit for a machine with total_mb of RAM (MB).
#[pyfunction]
pub fn ram_auto_mb(total_mb: f64) -> f64 {
    if !(total_mb > 0.0) {
        return AUTO_MIN_MB;
    }
    if total_mb >= gb_to_mb(BIG_GB) {
        return (BIG_FRAC * total_mb).min(AUTO_MAX_MB);
    }
    (AUTO_FRAC * total_mb).max(AUTO_MIN_MB)
}

/// The limit in MB: the setting in GB (of 2^30 bytes; 0 or less: Auto) on a machine with total_mb of RAM (0: unknown).
#[pyfunction]
pub fn ram_limit_mb(setting_gb: f64, total_mb: f64) -> f64 {
    if !(setting_gb > 0.0) || !setting_gb.is_finite() {
        return ram_auto_mb(total_mb);
    }
    let mut mb = (setting_gb * 1024.0).max(EXPLICIT_MIN_MB);
    if total_mb > 0.0 {
        mb = mb.min((EXPLICIT_MAX_FRAC * total_mb).max(EXPLICIT_MIN_MB));
    }
    mb
}

/// The tier of a limit: "emergency", "lean", "small" or "desktop" (Auto: the tier the RAM alone gave before).
#[pyfunction]
pub fn ram_tier(limit_mb: f64) -> &'static str {
    for (gb, name) in TIER_GB {
        if limit_mb < AUTO_FRAC * gb_to_mb(gb) - 0.5 {
            return name;
        }
    }
    "desktop"
}

/// The desktop tier's sizes for a limit: (decoded DEM tiles a process, map data MB a process, a worker's shed ceiling
/// MB, its pool's rotation ceiling MB, road-tile processes at most, far-tile processes at most). From a 6.5 GB limit
/// (a 20 GB PC's Auto) to 16 GB and more (a 32 GB PC's Auto: 17.9 GB): DEM ~200 -> 512 tiles, map data 160 -> 256 MB,
/// ceilings 1350 / 1800 -> 3000 / 4000 MB (the RAM-keyed ceilings before: 2000 / 2800 below 40 GB, 3000 / 3800 below
/// 56 GB, none above).
#[pyfunction]
pub fn ram_desktop_sizes(limit_mb: f64) -> (i64, i64, i64, i64, i64, i64) {
    let l = limit_mb.max(0.0);
    let dem = (l / 32.0).clamp(192.0, 512.0) as i64;
    let mapdata = (l / 64.0).clamp(160.0, 256.0) as i64;
    let rotate = (0.24 * l + 250.0).clamp(1800.0, 4000.0);
    let shed = 0.75 * rotate;
    let road = (((l - 3072.0) / 1229.0).floor() as i64).max(2);
    let far = (((l - 4096.0) / 820.0).floor() as i64).max(1);
    (dem, mapdata, shed as i64, rotate as i64, road, far)
}

/// The watchdog's thresholds and gaps
pub const SOFT_FRAC: f64 = 0.9; // at this share of the limit the caches are asked to go ...
pub const HARD_HOLD_S: f64 = 20.0; // ... over the limit this long: a pool's processes replaced ...
pub const SHED_GAP_S: f64 = 60.0; // ... at most one shed request a minute ...
pub const SHED_SETTLE_S: f64 = 15.0; // ... and a replacement only once the last shed had this long to act ...
pub const ROTATE_GAP_S: f64 = 60.0; // ... at most one replacement a minute
pub const ROTATE_FLOOR_MB: f64 = 400.0; // (a pool whose processes are smaller is not worth replacing)
pub const SHORT_FLOOR_FRAC: f64 = 0.4; // the system short of memory lowers the limit at most to this share of it

/// The limit in force (MB): the limit, lowered by what the system lacks while less than reserve_mb is available
/// (avail_mb < 0: unknown), never below SHORT_FLOOR_FRAC of it.
#[pyfunction]
pub fn ram_effective_mb(limit_mb: f64, total_mb: f64, avail_mb: f64, reserve_mb: f64) -> f64 {
    if avail_mb < 0.0 || avail_mb >= reserve_mb {
        return limit_mb;
    }
    let short = reserve_mb - avail_mb;
    limit_mb.min((total_mb - short).max(SHORT_FLOOR_FRAC * limit_mb))
}

/// One watchdog step. limit_mb: the limit; total_mb: the server's measured memory now; avail_mb / reserve_mb: the
/// system's available memory and the reserve kept for the game and the system (avail < 0: unknown); over_s: how long
/// the total has been over the limit in force (0: not over); since_shed_s / since_rotate_s: since the last shed
/// request / pool replacement; pools: (its largest process's MB, has a backlog) per pool (one with a backlog is not
/// replaced: its queued jobs would run in the old processes beside the new ones).
/// Returns (action, pool index, the limit in force): "ok" (under the soft threshold), "shed" (ask every process to
/// drop its caches), "rotate" (replace that pool's processes), "wait" (over, but the gaps hold).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn ram_watch(
    limit_mb: f64,
    total_mb: f64,
    avail_mb: f64,
    reserve_mb: f64,
    over_s: f64,
    since_shed_s: f64,
    since_rotate_s: f64,
    pools: Vec<(f64, bool)>,
) -> (String, i64, f64) {
    let eff = ram_effective_mb(limit_mb, total_mb, avail_mb, reserve_mb);
    if !(limit_mb > 0.0) || total_mb < SOFT_FRAC * eff {
        return ("ok".into(), -1, eff);
    }
    if total_mb >= eff && over_s >= HARD_HOLD_S && since_rotate_s >= ROTATE_GAP_S && since_shed_s >= SHED_SETTLE_S {
        let mut best: Option<(usize, f64)> = None;
        for (i, &(mb, backlog)) in pools.iter().enumerate() {
            if backlog || !(mb >= ROTATE_FLOOR_MB) {
                continue;
            }
            if best.map_or(true, |(_, b)| mb > b) {
                best = Some((i, mb));
            }
        }
        if let Some((i, _)) = best {
            return ("rotate".into(), i as i64, eff);
        }
    }
    if since_shed_s >= SHED_GAP_S {
        return ("shed".into(), -1, eff);
    }
    ("wait".into(), -1, eff)
}

#[cfg(test)]
mod tests {
    use super::*;

    const GB: f64 = 1e9 / 1048576.0;

    #[test]
    fn auto_keeps_the_old_tiers() {
        // (RAM in GB of 10^9 as the old thresholds read it, the tier it had)
        for (ram, tier) in [(4.0, "emergency"), (5.9, "emergency"), (7.7, "lean"), (8.4, "lean"), (9.9, "lean"),
                            (12.0, "small"), (14.8, "small"), (16.7, "small"), (19.9, "small"), (20.0, "desktop"),
                            (24.0, "desktop"), (32.0, "desktop"), (64.0, "desktop")] {
            assert_eq!(ram_tier(ram_limit_mb(0.0, ram * GB)), tier, "{ram} GB");
        }
    }

    #[test]
    fn auto_share_floor_and_cap() {
        assert_eq!(ram_auto_mb(0.0), AUTO_MIN_MB);
        assert_eq!(ram_auto_mb(4096.0), AUTO_MIN_MB);
        assert!((ram_auto_mb(16.0 * GB) - 0.33 * 16.0 * GB).abs() < 0.01, "a 16 GB PC: a third");
        assert!((ram_auto_mb(14800.0) - 4884.0).abs() < 0.01, "a Steam Deck");
        assert!((ram_auto_mb(32.0 * GB) - 0.6 * 32.0 * GB).abs() < 0.01, "a 32 GB PC: 60 %");
        assert_eq!(ram_auto_mb(64.0 * GB), AUTO_MAX_MB);
        assert_eq!(ram_auto_mb(262144.0), AUTO_MAX_MB);
        assert!(ram_auto_mb(24.0 * GB) > ram_auto_mb(21.9 * GB), "never less for more RAM");
    }

    #[test]
    fn explicit_limits() {
        assert_eq!(ram_limit_mb(4.0, 65536.0), 4096.0);
        assert_eq!(ram_limit_mb(0.5, 65536.0), EXPLICIT_MIN_MB);
        assert_eq!(ram_limit_mb(48.0, 16384.0), 0.9 * 16384.0); // (never past most of the RAM)
        assert_eq!(ram_limit_mb(6.0, 0.0), 6144.0); // (unknown RAM)
        assert_eq!(ram_tier(ram_limit_mb(2.0, 65536.0)), "lean");
        assert_eq!(ram_tier(ram_limit_mb(4.0, 65536.0)), "small");
        assert_eq!(ram_tier(ram_limit_mb(8.0, 65536.0)), "desktop");
        assert_eq!(ram_limit_mb(f64::NAN, 8192.0), ram_auto_mb(8192.0));
    }

    #[test]
    fn desktop_sizes_follow_the_limit() {
        let (dem, md, shed, rot, road, far) = ram_desktop_sizes(AUTO_MAX_MB);
        assert_eq!((dem, md, shed, rot), (512, 256, 3000, 4000));
        assert!(road >= 4 && far >= 10);
        let (dem, md, shed, rot, road, far) = ram_desktop_sizes(ram_auto_mb(32.0 * GB));
        assert!(dem >= 500 && md >= 250 && shed >= 2900 && rot >= 3900, "a 32 GB PC: about the desktop sizes");
        assert!(road >= 4 && far >= 10, "no fewer processes than the CPU allows");
        let (_, _, _, _, road, far) = ram_desktop_sizes(ram_auto_mb(24.0 * GB));
        assert!(road >= 4 && far >= 10);
        let (_, _, _, _, road, far) = ram_desktop_sizes(ram_auto_mb(20.0 * GB));
        assert!(road >= 2 && far >= 2);
    }

    #[test]
    fn short_system_lowers_the_limit() {
        assert_eq!(ram_effective_mb(8000.0, 6000.0, 3000.0, 2000.0), 8000.0);
        assert_eq!(ram_effective_mb(8000.0, 6000.0, -1.0, 2000.0), 8000.0);
        assert_eq!(ram_effective_mb(8000.0, 6000.0, 500.0, 2000.0), 4500.0);
        assert_eq!(ram_effective_mb(8000.0, 3000.0, 0.0, 2000.0), 3200.0); // (the floor)
    }

    #[test]
    fn watch_steps() {
        let pools = vec![(1200.0, false), (2500.0, true), (1800.0, false)];
        assert_eq!(ram_watch(10000.0, 8000.0, -1.0, 0.0, 0.0, 999.0, 999.0, pools.clone()).0, "ok");
        // soft: shed, then wait for the gap
        assert_eq!(ram_watch(10000.0, 9500.0, -1.0, 0.0, 0.0, 999.0, 999.0, pools.clone()).0, "shed");
        assert_eq!(ram_watch(10000.0, 9500.0, -1.0, 0.0, 0.0, 10.0, 999.0, pools.clone()).0, "wait");
        // hard, held: the largest pool without a backlog
        let r = ram_watch(10000.0, 11000.0, -1.0, 0.0, 30.0, 20.0, 999.0, pools.clone());
        assert_eq!((r.0.as_str(), r.1), ("rotate", 2));
        // not held long enough, or a replacement just made, or the shed just asked
        assert_eq!(ram_watch(10000.0, 11000.0, -1.0, 0.0, 5.0, 999.0, 999.0, pools.clone()).0, "shed");
        assert_eq!(ram_watch(10000.0, 11000.0, -1.0, 0.0, 30.0, 999.0, 10.0, pools.clone()).0, "shed");
        assert_eq!(ram_watch(10000.0, 11000.0, -1.0, 0.0, 30.0, 5.0, 999.0, pools.clone()).0, "wait");
        // every pool backlogged or small: shed only
        assert_eq!(ram_watch(10000.0, 11000.0, -1.0, 0.0, 30.0, 999.0, 999.0, vec![(3000.0, true), (300.0, false)]).0,
                   "shed");
        // the system short of memory: over a lowered limit
        let r = ram_watch(10000.0, 6000.0, 500.0, 2000.0, 0.0, 999.0, 999.0, pools);
        assert_eq!((r.0.as_str(), r.2), ("shed", 4500.0));
        assert_eq!(ram_watch(0.0, 6000.0, -1.0, 0.0, 99.0, 999.0, 999.0, vec![]).0, "ok");
    }
}
