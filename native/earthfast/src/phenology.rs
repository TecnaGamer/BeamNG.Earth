//! The deciduous trees' leaf calendar at a place (the user, 2026-11-27: "trees should gradually lose their leaves"):
//! per species group the days of year (northern-hemisphere equivalent: the southern hemisphere's dates are half a year
//! later, the caller shifts its day) of bud break, full leaf, first autumn colour, the start of leaf fall, the autumn
//! midpoint and bare. A climatology, not this year's weather: the same for every player at a place and date.
//!
//! The model (constants and sources: docs/research/2026-11-27-leaf-phenology.md):
//! - a reference calendar at 45 degrees (continental humid: Koppen Dfa / Dfb), sea level, per species: spring after
//!   MODIS MCD12Q2 Greenup / Maturity (15 % / 90 % of the EVI2 amplitude) and the PEP725 / USA-NPN leaf-out order
//!   (birch, poplar, beech, oak), autumn after MCD12Q2 Senescence / MidGreendown / Dormancy and the colouring order
//!   (birch and poplar first, oak last);
//! - latitude: spring 3.5 days later per degree poleward (Hopkins' bioclimatic law: 4; MODIS green-up 3-4) up to 55
//!   degrees, 2 beyond; autumn 2.5 days earlier per degree, 1 beyond;
//! - elevation: spring 2.5 days later per 100 m (Vitasse et al. 2009: 1.1-3.4; Hopkins: 3.3), autumn 1.5 days earlier;
//! - climate (the Koppen-Geiger class, Beck et al. 2023 legend 1-30): maritime Cfb / Cfc leaf out 22 days earlier and
//!   hold leaves 23 days later than the continental reference at the same latitude (PEP725 Germany: beech leaf
//!   unfolding DOY 116.6 at ~50.5 N); Mediterranean, humid subtropical, dry-winter and subarctic classes their own
//!   offsets; the tropics no cycle; hot deserts and steppes a quarter of the trees (planted, irrigated: seasonlook.lua
//!   CLIMATE_LEAF).
//! Pure arithmetic (+ - * / on f64, no libm): bit-identical on every CPU.
use pyo3::prelude::*;

/// species groups, the mod's (baretrees.lua TINT_SPECIES order and its poplars)
pub const SPECIES: [&str; 4] = ["oak", "beech", "birch", "poplar"];
/// the reference (45 N, Dfa / Dfb, sea level) bud break, its days to full leaf, first colour, days from first colour to
/// the start of leaf fall, the midpoint and bare, per SPECIES
const BUD: [f64; 4] = [118.0, 112.0, 106.0, 109.0];
const LEAF_DAYS: [f64; 4] = [28.0, 22.0, 18.0, 20.0];
const COLOUR: [f64; 4] = [270.0, 262.0, 252.0, 257.0];
const DROP_AFTER: [f64; 4] = [14.0, 12.0, 8.0, 10.0];
const HALF_AFTER: [f64; 4] = [25.0, 23.0, 20.0, 21.0];
const BARE_AFTER: [f64; 4] = [47.0, 43.0, 36.0, 38.0];
const REF_LAT: f64 = 45.0;
const SPRING_PER_DEG: f64 = 3.5;
const SPRING_PER_DEG_HIGH: f64 = 2.0;
const HIGH_LAT: f64 = 55.0;
const AUTUMN_PER_DEG: f64 = 2.5;
const AUTUMN_PER_DEG_HIGH: f64 = 1.0;
pub const SPRING_PER_M: f64 = 0.025;
pub const AUTUMN_PER_M: f64 = -0.015;
/// a tree's own spread (days, one standard deviation) about the place's dates: spring, autumn (PEP725 / NPN: trees of
/// one site leaf out within about a week, colour and drop over about two)
pub const SPREAD: (f64, f64) = (6.0, 9.0);

/// (spring days, autumn days) of a Koppen-Geiger class (1-30, Beck et al. 2023) against the continental reference
fn climate_offset(k: i32) -> (f64, f64) {
    match k {
        8..=10 => (-10.0, 12.0),   // Cs: Mediterranean
        11..=13 => (0.0, 5.0),     // Cw: dry-winter subtropical, subtropical highland
        14 => (8.0, 5.0),          // Cfa: humid subtropical (the latitude's gradient alone leafs it out too early)
        15 | 16 => (-22.0, 23.0),  // Cfb, Cfc: maritime
        17..=20 => (0.0, -3.0),    // Ds
        21..=24 => (3.0, -3.0),    // Dw: dry-winter continental
        27..=30 => (-12.0, 8.0),   // Dfc, Dfd, E: subarctic, polar (the slopes past 55 degrees are gentler)
        4..=7 => (0.0, 5.0),       // B: arid (planted trees)
        _ => (0.0, 0.0),           // Dfa, Dfb and unknown (0: the sea)
    }
}

/// The share (0-1) of a place's deciduous trees that follow the leaf cycle: none in the tropics (and within 23
/// degrees), a quarter in hot deserts and steppes, all elsewhere.
pub fn participation(lat: f64, koppen: i32) -> f64 {
    if lat.abs() < 23.0 {
        return 0.0;
    }
    match koppen {
        1..=3 => 0.0,
        4 | 6 => 0.25,
        _ => 1.0,
    }
}

/// [bud break, full leaf, first colour, leaf fall starts, midpoint, bare] (northern-equivalent days of year) per SPECIES
/// at latitude lat, ground elevation elev_m (metres; below the sea counts as 0, above 4000 as 4000) and Koppen class.
pub fn calendar(lat: f64, elev_m: f64, koppen: i32) -> [[f64; 6]; 4] {
    let a = lat.abs();
    let poleward = a - REF_LAT;
    let spring_lat = if a <= HIGH_LAT {
        poleward * SPRING_PER_DEG
    } else {
        (HIGH_LAT - REF_LAT) * SPRING_PER_DEG + (a - HIGH_LAT) * SPRING_PER_DEG_HIGH
    };
    let autumn_lat = if a <= HIGH_LAT {
        -poleward * AUTUMN_PER_DEG
    } else {
        -(HIGH_LAT - REF_LAT) * AUTUMN_PER_DEG - (a - HIGH_LAT) * AUTUMN_PER_DEG_HIGH
    };
    let e = elev_m.clamp(0.0, 4000.0);
    let (cs, ca) = climate_offset(koppen);
    let spring = spring_lat + e * SPRING_PER_M + cs;
    let autumn = autumn_lat + e * AUTUMN_PER_M + ca;
    let mut out = [[0.0; 6]; 4];
    for i in 0..4 {
        let bud = BUD[i] + spring;
        let col = COLOUR[i] + autumn;
        // (a short season, far north or high: the autumn never before full leaf plus 30 days)
        let col = col.max(bud + LEAF_DAYS[i] + 30.0);
        out[i] = [bud, bud + LEAF_DAYS[i], col, col + DROP_AFTER[i], col + HALF_AFTER[i], col + BARE_AFTER[i]];
    }
    out
}

/// Python: (participation, [[6 days] x 4 species], (spring days per m, autumn days per m), (spring spread, autumn
/// spread)) for anchor_ready's "leaf" (server leafcycle.py; the mod's baretrees.lua takes each tree's own dates).
#[pyfunction]
pub fn leaf_calendar(lat: f64, elev_m: f64, koppen: i32) -> (f64, Vec<Vec<f64>>, (f64, f64), (f64, f64)) {
    let cal = calendar(lat, elev_m, koppen);
    (
        participation(lat, koppen),
        cal.iter().map(|r| r.iter().map(|d| (d * 10.0).round_ties_even() / 10.0).collect()).collect(),
        (SPRING_PER_M, AUTUMN_PER_M),
        SPREAD,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reference_and_gradients() {
        let c = calendar(45.0, 0.0, 26);
        assert_eq!(c[1][0], 112.0); // beech bud break at the reference
        let north = calendar(50.0, 0.0, 26);
        assert_eq!(north[1][0] - c[1][0], 17.5); // 3.5 days a degree in spring
        assert_eq!(north[1][2] - c[1][2], -12.5); // 2.5 days a degree earlier in autumn
        let high = calendar(45.0, 1000.0, 26);
        assert!((high[1][0] - c[1][0] - 25.0).abs() < 1e-9 && (high[1][2] - c[1][2] + 15.0).abs() < 1e-9);
        // species order: birch leafs out first and colours first, oak last
        assert!(c[2][0] < c[3][0] && c[3][0] < c[1][0] && c[1][0] < c[0][0]);
        assert!(c[2][2] < c[3][2] && c[3][2] < c[1][2] && c[1][2] < c[0][2]);
        for r in c {
            assert!(r.windows(2).all(|w| w[0] < w[1]));
        }
    }

    /// against observed means: PEP725 Germany beech leaf unfolding 116.6 (~50.5 N, ~300 m, Cfb), oak 122.1, birch 107.1;
    /// MCD12Q2 at 40 N (Cfa / Dfa, eastern USA): green-up ~95-105, dormancy ~310-325
    #[test]
    fn observed_places() {
        let de = calendar(50.5, 300.0, 15);
        assert!((de[1][0] - 116.6).abs() < 6.0, "beech {}", de[1][0]);
        assert!((de[0][0] - 122.1).abs() < 6.0, "oak {}", de[0][0]);
        assert!((de[2][0] - 107.1).abs() < 6.0, "birch {}", de[2][0]);
        assert!(de[1][5] > 295.0 && de[1][5] < 320.0, "beech bare {}", de[1][5]);
        let us = calendar(40.0, 100.0, 14);
        assert!(us[1][0] > 90.0 && us[1][0] < 110.0, "{}", us[1][0]);
        assert!(us[1][5] > 305.0 && us[1][5] < 330.0, "{}", us[1][5]);
        // the boreal birch (60 N, Dfc): green-up ~125-150, bare ~265-285
        let fi = calendar(60.0, 100.0, 27);
        assert!(fi[2][0] > 125.0 && fi[2][0] < 150.0, "{}", fi[2][0]);
        assert!(fi[2][5] > 260.0 && fi[2][5] < 290.0, "{}", fi[2][5]);
    }

    #[test]
    fn tropics_and_deserts() {
        assert_eq!(participation(10.0, 14), 0.0);
        assert_eq!(participation(-35.0, 1), 0.0);
        assert_eq!(participation(33.5, 4), 0.25);
        assert_eq!(participation(-38.0, 15), 1.0);
        // a short season never colours before its leaves are full
        let c = calendar(70.0, 2000.0, 29);
        for r in c {
            assert!(r[2] >= r[1] + 30.0);
        }
    }
}
