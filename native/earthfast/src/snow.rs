//! The snow kernel (winter W1, BNE_WINTER; ~/bnge-tests/winterplan/PLAN.md sections 2.1-2.10, 4.1): an hourly state
//! machine for the snow on open ground, on the forest floor and on roads by plow class (bridge decks as their own
//! variant), from one forcing record per UTC hour. The road model keeps loose snow, packed snow, an ice film, a water
//! film, salt, frost, wheel-track progress and the banks; it gives one condition per class (dry ... packed ice) that the
//! mod turns into ground-model values (winter.lua). Snow never enters plans, cell facts or the decision archives: the
//! state is applied live.
//!
//! Integers only. Temperatures in 0.01 degC, water (SWE, films, salt) in 0.01 mm, depths in 0.1 mm, density in kg/m3,
//! wind in 0.1 m/s, shortwave in W/m2, albedo in 1e-5. exp comes from fixed tables, no float and no libm, so the same
//! input gives the same bytes on every platform (the 1e-12 ARM / AVX differences of 2026-11-18 cannot happen here).
//! `selftest` hashes the states of a long random forcing series: tools and tests compare it across Linux / Windows.
//!
//! Pure: `state = f(forcing records, hour, site)`. No clock, no network, no global state.

/// Forcing record layout (one per UTC hour, NF i32 fields). MISSING where a source has no value.
pub const NF: usize = 14;
pub const F_T2M: usize = 0; // air temperature 2 m, 0.01 degC
pub const F_TD: usize = 1; // dew point 2 m, 0.01 degC
pub const F_TW: usize = 2; // wet-bulb 2 m, 0.01 degC (MISSING: T - (T - Td) / 3)
pub const F_PRECIP: usize = 3; // precipitation in the hour (liquid equivalent), 0.01 mm
pub const F_SNOWWE: usize = 4; // the model's snowfall water equivalent in the hour, 0.01 mm (MISSING: our own split)
pub const F_WIND: usize = 5; // wind 10 m, 0.1 m/s
pub const F_SW: usize = 6; // shortwave down, W/m2 (the hour's mean)
pub const F_TSURF: usize = 7; // soil / surface temperature at 0 cm, 0.01 degC (MISSING: the air's)
pub const F_ZFREEZE: usize = 8; // freezing-level height, m above sea level (MISSING: the default lapse rate)
pub const F_DEPTH: usize = 9; // the model's snow depth, mm (only the window's first hour is read: the pack's seed)
pub const F_RH: usize = 10; // relative humidity, %
pub const F_HOUR: usize = 11; // local hour 0..23 (traffic, day / night)
pub const F_MONTH: usize = 12; // month 1..12 (the sun's height on slopes)
pub const F_CLOUD: usize = 13; // cloud cover, %
pub const MISSING: i32 = i32::MIN;

/// Road conditions (the snowstate message's names: CONDITION_NAMES, winter.lua's slot table).
pub const DRY: u8 = 0;
pub const DAMP: u8 = 1;
pub const WET: u8 = 2;
pub const SALT_WET: u8 = 3;
pub const FROST: u8 = 4;
pub const BLACK_ICE: u8 = 5;
pub const ICE: u8 = 6;
pub const WET_ICE: u8 = 7;
pub const SLUSH: u8 = 8;
pub const DUSTING: u8 = 9;
pub const LOOSE: u8 = 10;
pub const PACKED: u8 = 11;
pub const PACKED_ICY: u8 = 12;
pub const CONDITION_NAMES: [&str; 13] = ["dry", "damp", "wet", "salt_wet", "frost", "black_ice", "ice", "wet_ice",
                                         "slush", "dusting", "loose", "packed", "packed_icy"];

/// Plow classes P0..P4 (motorway/trunk, primary/secondary, tertiary, residential, service/track) and the bridge variant.
pub const CLASSES: usize = 5;
/// Daily traffic per class (vehicles a day, both directions) [R1, from memory there].
pub const AADT: [i64; CLASSES] = [60_000, 15_000, 3_000, 500, 50];
/// Share of the day's traffic per local hour, permille (sums to 1000) [guess: a typical weekday curve].
pub const DAY_CURVE: [i64; 24] = [10, 6, 4, 4, 6, 14, 36, 64, 68, 56, 53, 55, 57, 58, 61, 68, 76, 77, 65, 50, 39, 32,
                                  24, 17];
/// Hours between plow passes per class while the trigger depth lies (the bare-lane targets: P0 3 h ... P3 24-36 h,
/// P4 never) [R1: MnDOT].
pub const CYCLE_H: [i32; CLASSES] = [3, 6, 12, 30, i32::MAX];
/// A pre-treatment / plow-pass salt dose, 0.01 mm of melt capacity [guess].
pub const SALT_DOSE: i32 = 300;
/// Hours a grit spread keeps working [guess].
pub const GRIT_H: i32 = 48;

/// The pack's elevation bands round the cell's own ground (m) [guess: the plan fixes 8 bands, not their spacing].
pub const BAND_DZ: [i32; 8] = [-300, -150, 0, 150, 300, 600, 1000, 1500];
/// Depth bin lower edges, 0.1 mm: D1 dusting from 0.5 mm, D2 1 cm, D3 5 cm, D4 15 cm, D5 30 cm, D6 60 cm (plan 2.4).
pub const BIN_EDGE: [i32; 7] = [0, 5, 100, 500, 1500, 3000, 6000];

// 51.3 * exp(T / 2.59) kg/m3 x 100 at T = -30 .. +5 degC in 0.5 degC steps (Hedstrom and Pomeroy's new-snow density
// less its 67.9 floor; generated once with Python's math.exp and rounded: no runtime exp)
const DENS_T: [i32; 71] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 6, 7, 9, 11, 13,
                           16, 19, 23, 28, 34, 41, 50, 61, 73, 89, 108, 131, 159, 193, 234, 283, 344, 417, 506, 614,
                           744, 903, 1095, 1328, 1611, 1954, 2370, 2875, 3487, 4229, 5130, 6222, 7547, 9155, 11104,
                           13469, 16337, 19815, 24035, 29153, 35361];
// 1 - exp(-passes / 120), permille, at passes = 0, 12, 24 ... 1200 (wheel tracks show after 50-200 passes [R1])
const TRACK_T: [i32; 101] = [0, 95, 181, 259, 330, 393, 451, 503, 551, 593, 632, 667, 699, 727, 753, 777, 798, 817,
                             835, 850, 865, 878, 889, 900, 909, 918, 926, 933, 939, 945, 950, 955, 959, 963, 967, 970,
                             973, 975, 978, 980, 982, 983, 985, 986, 988, 989, 990, 991, 992, 993, 993, 994, 994, 995,
                             995, 996, 996, 997, 997, 997, 998, 998, 998, 998, 998, 998, 999, 999, 999, 999, 999, 999,
                             999, 999, 999, 999, 999, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000,
                             1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000];
// How much a 25-degree slope facing the equator gains (and one facing the pole loses) of the flat ground's sun,
// permille, by |latitude| band 0-20 / 20-35 / 35-50 / 50-60 / 60+ [guess, from the sun's noon height]
const ASPECT_MAX: [i64; 5] = [150, 350, 550, 650, 700];
// ... scaled by how low the sun stands that month, permille, January .. December, northern hemisphere
const WINTERNESS: [i64; 12] = [1000, 800, 550, 300, 150, 50, 50, 150, 300, 550, 800, 1000];

fn clamp(v: i64, lo: i64, hi: i64) -> i64 {
    v.max(lo).min(hi)
}

/// Linear interpolation in a table sampled every `step` from `x0` (clamped to its ends).
fn table(t: &[i32], x0: i64, step: i64, x: i64) -> i64 {
    let r = clamp(x - x0, 0, step * (t.len() as i64 - 1));
    let i = (r / step) as usize;
    let f = r % step;
    if i + 1 >= t.len() {
        return t[t.len() - 1] as i64;
    }
    t[i] as i64 + (t[i + 1] as i64 - t[i] as i64) * f / step
}

/// New-snow density kg/m3 at air temperature t (0.01 degC) and wind (0.1 m/s): 67.9 + 51.3 exp(T / 2.59), clamped
/// 50..150, plus wind packing: +8 kg/m3 per m/s over 4 m/s, at most +60 [R1 gives +60 above 5 m/s; a step that blunt
/// halved the depth of every breezy storm in the replay].
pub fn new_density(t: i64, wind: i64) -> i64 {
    let r = clamp(6790 + table(&DENS_T, -3000, 50, t), 5000, 15000) / 100;
    r + clamp((wind - 40) * 8 / 10, 0, 60)
}

/// Wheel-track progress, permille: 1 - exp(-passes / 120).
pub fn track(passes: i64) -> i64 {
    table(&TRACK_T, 0, 12, passes)
}

/// Snow share of precipitation by wet-bulb temperature (0.01 degC), permille: all snow at or below 0 degC, none from
/// 1.5 degC, linear between [R1].
pub fn snow_share_wb(tw: i64) -> i64 {
    if tw <= 0 {
        1000
    } else if tw >= 150 {
        0
    } else {
        1000 - tw * 1000 / 150
    }
}

/// One hour's forcing at a site (downscaled from the cell, plan 2.3).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Hour {
    pub t: i64,
    pub tw: i64,
    pub td: i64,
    pub tsurf: i64,
    pub snow: i64,
    pub rain: i64,
    pub wind: i64,
    pub sw: i64,
    pub rh: i64,
    pub cloud: i64,
    pub hour: i64,
    pub month: i64,
}

/// A site: its ground elevation (m), the forcing cell's (m), latitude x 100, built-up share (permille), forest or open,
/// and its aspect (-1 facing the pole, 0 flat, +1 facing the equator).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Site {
    pub z: i32,
    pub z_cell: i32,
    pub lat_e2: i32,
    pub built: i32,
    pub forest: bool,
    pub aspect: i32,
}

fn field(r: &[i32], i: usize, default: i64) -> i64 {
    if r[i] == MISSING {
        default
    } else {
        r[i] as i64
    }
}

/// The lapse rate (0.01 degC per km) of a record: from its own freezing level, T / (z_freeze - z_cell), clamped
/// 3..8 K/km; 5.5 K/km without one or below freezing [R1; the clamp is the plan's guess].
pub fn lapse(r: &[i32], z_cell: i64) -> i64 {
    let t = field(r, F_T2M, 0);
    let zf = field(r, F_ZFREEZE, -100_000);
    if t > 0 && zf > z_cell + 50 {
        clamp(t * 1000 / (zf - z_cell), 300, 800)
    } else {
        550
    }
}

/// A record downscaled to a site (plan 2.3 steps 1, 3, 8: lapse rate, +6 % precipitation per 100 m (0.5..2.0), town
/// heat +1 degC times the built-up share; the snow share is the model's own split at the cell moved by the wet-bulb
/// shift to the site. Open-Meteo's snowfall water equivalent is ~0.7 of the precipitation even at -11 degC wet-bulb
/// (its fixed ratio again, the 2023 Minneapolis replay): the split is divided by 0.7. Without a split: wet-bulb only.
pub fn downscale(r: &[i32], s: &Site) -> Hour {
    let z_cell = s.z_cell as i64;
    let dz = s.z as i64 - z_cell;
    let shift = -lapse(r, z_cell) * dz / 1000 + s.built as i64 * 100 / 1000;
    let t_cell = field(r, F_T2M, 0);
    let td_cell = field(r, F_TD, t_cell - 300).min(t_cell);
    let tw_cell = if r[F_TW] == MISSING { t_cell - (t_cell - td_cell) / 3 } else { r[F_TW] as i64 };
    // (the model's 0 cm soil temperature is the soil's under a snowpack: it stayed +1.4 degC at Davos while the air
    // fell to -4 degC and 10 cm of snow came (2026-11-24 fixture); the surface the snow lands on is held within
    // 2.5 degC above the air)
    let tsurf_cell = field(r, F_TSURF, t_cell).min(t_cell + 250);
    let p = field(r, F_PRECIP, 0).max(0) * clamp(1000 + 60 * dz / 100, 500, 2000) / 1000;
    let tw = tw_cell + shift;
    let share = if r[F_SNOWWE] != MISSING && r[F_PRECIP] != MISSING && r[F_PRECIP] > 0 {
        let m = clamp(r[F_SNOWWE] as i64 * 1000 / r[F_PRECIP] as i64, 0, 1000);
        let m = (m * 10 / 7).min(1000); // (its 0.7 undone: all snow is 1; freezing rain, 0 snow, stays rain)
        clamp(m + snow_share_wb(tw) - snow_share_wb(tw_cell), 0, 1000)
    } else {
        snow_share_wb(tw)
    };
    let snow = p * share / 1000;
    Hour {
        t: t_cell + shift,
        tw,
        td: td_cell + shift,
        tsurf: tsurf_cell + shift,
        snow,
        rain: p - snow,
        wind: field(r, F_WIND, 0).max(0),
        sw: field(r, F_SW, 0).max(0),
        rh: clamp(field(r, F_RH, 80), 0, 100),
        cloud: clamp(field(r, F_CLOUD, 50), 0, 100),
        hour: clamp(field(r, F_HOUR, 12), 0, 23),
        month: clamp(field(r, F_MONTH, 1), 1, 12),
    }
}

/// The sun on a slope relative to flat ground, permille, for an aspect (-1 pole, 0 flat, +1 equator), latitude x 100
/// and month (plan 2.3 step 4, as a table by latitude band and month).
pub fn radiation_index(aspect: i32, lat_e2: i32, month: i64) -> i64 {
    if aspect == 0 {
        return 1000;
    }
    let la = (lat_e2 as i64).abs();
    let band = if la < 2000 { 0 } else if la < 3500 { 1 } else if la < 5000 { 2 } else if la < 6000 { 3 } else { 4 };
    let m = if lat_e2 >= 0 { month - 1 } else { (month + 5) % 12 };
    let a = ASPECT_MAX[band] * WINTERNESS[clamp(m, 0, 11) as usize] / 1000;
    if aspect > 0 {
        1000 + a
    } else {
        (1000 - a).max(150)
    }
}

/// The snowpack of open ground or the forest floor (plan 2.4).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Ground {
    pub swe: i32,     // ice, 0.01 mm
    pub rho: i32,     // kg/m3 (0 without snow)
    pub liquid: i32,  // held water, 0.01 mm
    pub albedo: i32,  // 1e-5
    pub age: i32,     // hours since the last snowfall of 1 mm water or more
    pub cold: i32,    // hours in a row below 0 degC (refreeze)
    pub crust: bool,
}

pub const ALBEDO_FRESH: i64 = 85_000;
pub const ALBEDO_WET_FLOOR: i64 = 50_000;

impl Ground {
    /// Depth, 0.1 mm.
    pub fn depth(&self) -> i64 {
        if self.rho <= 0 {
            0
        } else {
            self.swe as i64 * 100 / self.rho as i64
        }
    }

    pub fn wet(&self) -> bool {
        self.liquid > 0
    }

    /// The window's first hour seeds the pack with the model's snow depth (mm, at the cell's elevation), moved to the
    /// site: +15 % per 100 m above (at most 3x), -25 % per 100 m below [guess], at 250 kg/m3.
    pub fn seed(depth_mm: i64, dz: i64) -> Ground {
        if depth_mm <= 0 {
            return Ground { albedo: ALBEDO_FRESH as i32, ..Default::default() };
        }
        let f = if dz >= 0 { clamp(1000 + 150 * dz / 100, 1000, 3000) } else { clamp(1000 + 250 * dz / 100, 0, 1000) };
        let swe = depth_mm * 25 * f / 1000;
        if swe <= 0 {
            return Ground { albedo: ALBEDO_FRESH as i32, ..Default::default() };
        }
        Ground { swe: swe as i32, rho: 250, liquid: 0, albedo: 70_000, age: 72, cold: 0, crust: false }
    }

    /// One hour (plan 2.4): rain or snow, new-snow density, contact melt under 1 cm, settling, degree-day plus
    /// shortwave melt above -2 degC, rain on snow, albedo ageing, refreeze into a crust, sublimation. rad: the sun
    /// on the slope, permille.
    pub fn step(&mut self, h: &Hour, forest: bool, rad: i64) {
        let mut snow = if forest { h.snow * 600 / 1000 } else { h.snow };
        let rain = h.rain;
        let rho_n = new_density(h.t, h.wind);
        // contact melt while the cover is under 1 cm: 0.3 mm per degC of surface an hour (~28 W/m2 per degC of ground
        // heat; the plan's 0.6 melted every flake of the 2026-11-24 Davos storm, whose model depth reached 19 cm); a
        // rate over 2.5 cm/h sticks up to +2 degC of surface [R1; the coefficient is a guess, the replay tunes it]
        let rate = snow * 100 / rho_n;
        let ts = if rate > 250 { if h.tsurf <= 200 { h.tsurf.min(0) } else { h.tsurf - 200 } } else { h.tsurf };
        let mut swe = self.swe as i64;
        let mut liquid = self.liquid as i64;
        let mut rho = self.rho as i64;
        if self.depth() < 100 && ts > 0 {
            let cm = 30 * ts / 100;
            let m_new = snow.min(cm);
            snow -= m_new;
            let m_old = swe.min(cm - m_new);
            swe -= m_old;
            liquid = (liquid + m_old).min(swe * 5 / 100);
        }
        if snow > 0 {
            let d_old = if rho > 0 { swe * 100 / rho } else { 0 };
            let d_new = snow * 100 / rho_n;
            swe += snow;
            rho = clamp(swe * 100 / (d_old + d_new).max(1), 50, 550);
            let a = self.albedo as i64;
            self.albedo = (a + (ALBEDO_FRESH - a) * snow.min(500) / 500) as i32;
            if snow >= 100 {
                self.age = 0;
                if snow >= 500 {
                    self.crust = false;
                }
            }
        }
        if swe <= 0 {
            *self = Ground { albedo: ALBEDO_FRESH as i32, cold: if h.t < 0 { self.cold + 1 } else { 0 }, ..Default::default() };
            return;
        }
        // rain on snow: into a pack below 0 degC it freezes (freezing rain, ice pellets: mass, no depth); else held as
        // water, with its heat: 0.0125 mm per mm of rain per degC [R1]
        if h.t < 0 && rain > 0 {
            let d = swe * 100 / rho.max(1);
            swe += rain;
            rho = clamp(swe * 100 / d.max(1), 50, 550);
        } else {
            liquid += rain;
        }
        let mut melt = rain * h.t.max(0) / 8000;
        if h.t > -200 {
            // degree-day 2.0 mm/degC/day (the forest 1.15) + 0.0108 mm per W h/m2 absorbed (latent heat of fusion),
            // half of it melting (the rest is lost to the cold pack and long-wave cooling: at full efficiency the
            // replay's packs went weeks early), ramped in from -2 to 0 degC; under trees 30 % of the sun reaches the
            // snow [R1 ranges; the split and the efficiency are tuned by tools/snowreplay.py]
            let ddf = if forest { 115 } else { 200 };
            let m_t = ddf * h.t.max(0) / 2400;
            let sun = if forest { h.sw * 300 / 1000 } else { h.sw };
            let absorbed = sun * (100_000 - self.albedo as i64) / 100_000 * rad / 1000;
            let ramp = clamp(h.t + 200, 0, 200);
            melt += m_t + absorbed * 108 / 100 * ramp / 400;
        }
        let melt = melt.min(swe);
        swe -= melt;
        liquid += melt;
        // sublimation 0.24 mm a day, five times that in wind over 8 m/s with humidity under 50 % [R1]
        let sub = if h.wind > 80 && h.rh < 50 { 5 } else { 1 };
        swe -= sub.min(swe);
        // water held up to 5 % of the SWE; two hours below 0 degC freeze it into a crust [guess]
        liquid = liquid.min(swe * 5 / 100).max(0);
        self.cold = if h.t < 0 { self.cold + 1 } else { 0 };
        if self.cold >= 2 && liquid > 0 {
            swe += liquid;
            liquid = 0;
            self.crust = true;
        }
        if swe <= 0 {
            *self = Ground { albedo: ALBEDO_FRESH as i32, cold: self.cold, ..Default::default() };
            return;
        }
        // settling: toward 300 kg/m3 over 72 h dry (slower in deep cold: 96 h below -5 degC, 144 h below -10), 400
        // over 24 h wet [R1; the cold steps are tuned by the replay]
        let wet = liquid > 0 || h.t > 0;
        let dry_tau = if h.t < -1000 { 144 } else if h.t < -500 { 96 } else { 72 };
        let (target, tau) = if wet { (400, 24) } else { (300, dry_tau) };
        if rho < target {
            rho += ((target - rho) / tau).max(1);
        }
        // albedo: dry -0.008 a day, wet toward 0.5 at 0.24 a day [R1]
        let a = self.albedo as i64;
        let a = if wet { a - (a - ALBEDO_WET_FLOOR).max(0) / 100 } else { a - 33 };
        self.albedo = clamp(a, 40_000, ALBEDO_FRESH) as i32;
        self.swe = swe as i32;
        self.liquid = liquid as i32;
        self.rho = rho as i32;
        self.age = (self.age + 1).min(1_000_000);
    }
}

/// Depth bin D0..D6 of a depth (0.1 mm), changing from `prev` (-1: none) only when the depth passes the edge by 15 %.
pub fn depth_bin(depth: i64, prev: i64) -> i64 {
    let raw = (1..7).rev().find(|&b| depth >= BIN_EDGE[b] as i64).unwrap_or(0) as i64;
    if !(0..=6).contains(&prev) {
        return raw;
    }
    let mut b = prev;
    if raw > prev {
        while b < 6 && depth * 100 >= BIN_EDGE[(b + 1) as usize] as i64 * 115 {
            b += 1;
        }
    } else if raw < prev {
        while b > 0 && depth * 100 < BIN_EDGE[b as usize] as i64 * 85 {
            b -= 1;
        }
    }
    b
}

/// Cover 0..8 (eighths of the ground white) from a depth (0.1 mm): patchy under 1 cm, full from 5 cm.
pub fn cover(depth: i64) -> i64 {
    if depth < BIN_EDGE[1] as i64 {
        0
    } else if depth < 100 {
        1 + depth * 3 / 100
    } else if depth < 500 {
        4 + (depth - 100) * 4 / 400
    } else {
        8
    }
}

/// The plow regime of a Koppen-Geiger class (Beck et al. 2023, 1-30; 0 the sea): M0 (no fleet) .. M4 (packed-snow
/// policy), by the mean days with snow on the ground that climate has (plan 2.7's table) [guess on the mapping].
pub fn regime_of_koppen(k: i64) -> i64 {
    match k {
        1..=4 | 6 | 8 | 12 => 0,         // tropical, hot deserts and steppes, hot-summer Mediterranean, Cwb highlands
        5 | 9 | 11 | 13 | 14 | 15 => 1,  // cold deserts, warm-summer Mediterranean, Cw, humid subtropical, maritime
        7 | 10 | 17 | 18 | 21 | 25 => 2, // cold steppes, Csc, Ds hot/warm summer, Dwa, Dfa
        16 | 19 | 20 | 22 | 26 => 3,     // subpolar oceanic, Dsc/Dsd, Dwb, Dfb
        23 | 24 | 27..=30 => 4,          // Dwc/Dwd, Dfc/Dfd, tundra, ice cap
        _ => 0,
    }
}

fn plowed(regime: i64, class: usize) -> bool {
    match regime {
        1 => class <= 1,
        2..=4 => class <= 3,
        _ => false,
    }
}

fn salted(regime: i64, class: usize) -> bool {
    match regime {
        1 => class <= 1,
        2 => class <= 2,
        3 => class <= 1,
        4 => class == 0,
        _ => false,
    }
}

/// A road class's state (plan 2.7): loose snow in the lanes and at the edge, packed snow, ice and water films, salt,
/// frost, wheel-track passes and polish, hours since a plow pass, grit, banks.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Road {
    pub loose: i32,  // loose snow in the lanes, 0.01 mm water (depth at 100 kg/m3: the same number in 0.1 mm)
    pub edge: i32,   // loose snow at the edge (thrown aside, not driven on), 0.01 mm
    pub packed: i32, // packed snow, 0.01 mm (400 kg/m3)
    pub ice: i32,    // ice film, 0.01 mm
    pub water: i32,  // water film, 0.01 mm
    pub salt: i32,   // salt left, 0.01 mm of melt capacity
    pub frost: i32,  // hoar frost, 0.01 mm
    pub passes: i32, // vehicles since the last snowfall
    pub polish: i32, // vehicles over packed snow between -6 and 0 degC
    pub since_plow: i32,
    pub grit: i32,   // hours of grit left
    pub banks: i32,  // snow plowed or thrown aside, 0.01 mm
}

/// One road row of the output: condition, lane depth (0.1 mm), pavement temperature (0.01 degC), edge condition and
/// depth, grit, wheel-track progress (permille), banks (0.01 mm).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RoadOut {
    pub cond: u8,
    pub depth: i64,
    pub tp: i64,
    pub edge_cond: u8,
    pub edge_depth: i64,
    pub grit: bool,
    pub track: i64,
    pub banks: i64,
}

/// The pavement temperature of a class (plan 2.7): the surface's +1 degC by day, +0.5 above 500 vehicles an hour;
/// bridge decks 1 degC colder by day and 2 at night (no ground heat: they freeze first) [R1 offsets].
pub fn pavement(h: &Hour, veh: i64, bridge: bool) -> i64 {
    let day = h.sw > 20;
    let mut tp = h.tsurf + if day { 100 } else { 0 } + if veh > 500 { 50 } else { 0 };
    if bridge {
        tp -= if day { 100 } else { 200 };
    }
    tp
}

impl Road {
    /// One hour of a class's road (plan 2.7). snow_ahead: snow is forecast within 6 h (pre-treatment).
    pub fn step(&mut self, h: &Hour, class: usize, bridge: bool, regime: i64, snow_ahead: bool) {
        let veh = AADT[class] * DAY_CURVE[h.hour as usize] / 1000;
        let tp = pavement(h, veh, bridge);
        let (mut loose, mut edge, mut packed) = (self.loose as i64, self.edge as i64, self.packed as i64);
        let (mut ice, mut water, mut salt, mut frost) =
            (self.ice as i64, self.water as i64, self.salt as i64, self.frost as i64);
        let (mut passes, mut polish) = (self.passes as i64, self.polish as i64);
        let mut banks = self.banks as i64;
        let salts = salted(regime, class);
        let brine = |salt: i64| salt > 0 && tp > -900; // salt works above -9 degC [R1]
        // pre-treatment 6 h before forecast snow, and while it snows [R1]
        if salts && (snow_ahead || h.snow > 0) && salt < SALT_DOSE as i64 / 2 && tp > -900 {
            salt = SALT_DOSE as i64;
        }
        // snow: sticks on pavement at or below 0 degC, or on a heavy rate (> 2.5 cm/h) up to +2 degC; else contact
        // melt 1.0 mm per degC an hour into the water film [guess]
        let mut snow = h.snow;
        let heavy = snow * 100 / new_density(h.t, h.wind) > 250;
        let tpe = if heavy { if tp <= 200 { tp.min(0) } else { tp - 200 } } else { tp };
        if tpe > 0 {
            let m = snow.min(tpe);
            snow -= m;
            water += m;
        }
        loose += snow;
        edge += snow;
        if snow >= 100 {
            passes /= 2; // a fresh cover over the tracks
        }
        // rain: freezing rain (glaze) on pavement below 0 degC without brine, else the water film
        if h.rain > 0 {
            if tp < 0 && !brine(salt) {
                ice += h.rain;
            } else {
                water += h.rain;
                salt -= (h.rain / 4).min(salt); // diluted
            }
        }
        // salt melts up to 2 mm an hour, less as it gets colder: (T + 9) / 9 [R1 limit; the rate is the plan's]
        if brine(salt) {
            let mut cap = (200 * (tp + 900) / 900).min(salt * 2);
            let used0 = cap;
            for v in [&mut loose, &mut packed, &mut ice, &mut frost] {
                let m = (*v).min(cap);
                *v -= m;
                cap -= m;
                water += m;
            }
            let used = used0 - cap;
            let e = edge.min(used0 / 2);
            edge -= e;
            water += e;
            salt -= (used + e) / 2;
        }
        if water > 50 {
            salt -= salt / 50; // runoff carries it off
        }
        // traffic: churns loose snow out of the tracks where brine works or near 0 degC (half to water, half to the
        // edge: wet-black tracks within 1-3 h on busy salted roads [R1]); compacts it below -1 degC unsalted;
        // polishes packed snow toward ice between -6 and 0 degC; wears frost; sprays water off
        if veh > 0 {
            passes += veh;
            if loose > 0 {
                if brine(salt) || tp > -100 {
                    let moved = loose * veh / (veh + 600);
                    loose -= moved;
                    water += moved / 2;
                    edge += moved - moved / 2;
                } else {
                    let c = loose * veh / (veh + 300);
                    loose -= c;
                    packed += c;
                }
            }
            if packed > 0 && (-600..=0).contains(&tp) {
                polish += veh;
            }
            frost -= frost * veh / (veh + 200);
            water -= water * veh / (veh + 2000);
        }
        // plow: once the loose depth reaches the trigger (5 cm in M1-M2, 3 cm in M3-M4), one pass per cycle: 90 % of
        // it to the banks, 0.5 cm left; salted classes get a dose, the packed-snow regimes grit [R1; the pass is
        // the plan's guess]
        let mut since_plow = self.since_plow as i64 + 1;
        let mut grit = (self.grit as i64 - 1).max(0);
        let trigger = if regime <= 2 { 500 } else { 300 };
        // (the trigger counts packed snow too: traffic packs a fall on a busy cold road before it lies 3 cm loose)
        if plowed(regime, class) && loose + packed / 4 >= trigger && since_plow >= CYCLE_H[class] as i64 {
            let keep = loose.min((loose / 10).max(50));
            banks += loose - keep + edge * 9 / 10;
            loose = keep;
            edge /= 10;
            since_plow = 0;
            if salts {
                banks += packed / 2;
                packed /= 2; // the blade scrapes packed snow where the policy is bare pavement
                if tp > -900 {
                    salt = salt.max(SALT_DOSE as i64);
                }
            } else {
                // the packed-snow policy: scraped down to 3 cm (1200 at 400 kg/m3), gritted where the regime grits
                banks += (packed - 1200).max(0);
                packed = packed.min(1200);
                if regime >= 3 {
                    grit = GRIT_H as i64;
                }
            }
        }
        // melt above 0 degC: contact 1 mm per degC an hour, plus the sun on a 0.5 albedo cover; snow first, then the
        // films
        if tp > 0 {
            let mut m = tp;
            let mut sun = h.sw * 54 / 100;
            for v in [&mut loose, &mut packed, &mut edge] {
                let x = (*v).min(m + sun);
                let from_sun = x.min(sun);
                sun -= from_sun;
                m -= x - from_sun;
                *v -= x;
                water += x;
            }
            for v in [&mut ice, &mut frost] {
                let x = (*v).min(m);
                m -= x;
                *v -= x;
                water += x;
            }
        }
        // the water film: evaporates (sun, wind) and drains; freezes into black ice below 0 degC without brine
        let evap = 2 + h.sw / 100 + h.wind / 20 + if water > 50 { water / 10 } else { 0 };
        water -= evap.min(water);
        if tp < 0 && water > 0 && !brine(salt) {
            ice += water;
            water = 0;
        }
        // frost: pavement below 0 degC and below the dew point, calm (< 3 m/s), no precipitation [R1]
        if h.snow + h.rain == 0 && tp < 0 && tp < h.td && h.wind < 30 {
            frost += ((h.td - tp) / 100 + 1).min(5);
        }
        if loose + packed == 0 {
            polish = 0;
        }
        banks -= (if h.t > 0 { h.t / 5 } else { 0 }).min(banks); // banks melt slowly (surface over volume)
        *self = Road {
            loose: loose.max(0) as i32,
            edge: edge.max(0) as i32,
            packed: packed.max(0) as i32,
            ice: ice.max(0) as i32,
            water: water.max(0) as i32,
            salt: salt.max(0) as i32,
            frost: frost.max(0) as i32,
            passes: passes.min(1_000_000) as i32,
            polish: polish.min(1_000_000) as i32,
            since_plow: since_plow.min(1_000_000) as i32,
            grit: grit as i32,
            banks: banks.max(0) as i32,
        };
    }

    /// The lanes' condition, its depth (0.1 mm) and the edge's (plan 2.7 output) at pavement temperature tp.
    pub fn out(&self, tp: i64) -> RoadOut {
        let (loose, packed, ice, water) = (self.loose as i64, self.packed as i64, self.ice as i64, self.water as i64);
        let brine = self.salt > 0 && tp > -900;
        let wet = water > 0 || tp > -50 || brine;
        let packed_d = packed / 4;
        let snow_d = loose + packed_d;
        let cond = if ice >= 50 && snow_d < 30 || ice >= 20 && snow_d < 10 {
            if tp >= -50 && water >= 10 {
                WET_ICE
            } else if ice >= 150 {
                ICE
            } else {
                BLACK_ICE
            }
        } else if snow_d >= 10 {
            if packed >= 40 && (packed_d >= loose || loose < 100) {
                if self.polish >= 2000 { PACKED_ICY } else { PACKED }
            } else if loose < 100 {
                if wet { if brine { SALT_WET } else { WET } } else { DUSTING }
            } else if wet {
                SLUSH
            } else {
                LOOSE
            }
        } else if self.frost >= 3 && tp < 0 {
            FROST
        } else if water >= 20 {
            if brine { SALT_WET } else { WET }
        } else if water >= 3 {
            DAMP
        } else {
            DRY
        };
        let edge = self.edge as i64;
        let edge_cond = if edge < 10 {
            if water >= 20 { WET } else { DRY }
        } else if edge < 100 {
            DUSTING
        } else if wet {
            SLUSH
        } else {
            LOOSE
        };
        // slush lies at ~300 kg/m3: a third of the loose depth
        let depth = if cond == SLUSH { loose / 3 } else { snow_d };
        RoadOut {
            cond,
            depth,
            tp,
            edge_cond,
            edge_depth: if edge_cond == SLUSH { edge / 3 } else { edge },
            grit: self.grit > 0,
            track: track(self.passes as i64),
            banks: self.banks as i64,
        }
    }
}

/// The ground outputs of a site: depth (0.1 mm), SWE, density, held water, albedo permille, age h, crust, wet, the
/// depth bin (hysteresis from prev_bin), cover 0..8, air and surface temperatures (0.01 degC).
pub const GROUND_OUT: usize = 12;

fn ground_out(g: &Ground, h: &Hour, prev_bin: i64) -> [i64; GROUND_OUT] {
    let d = g.depth();
    [d, g.swe as i64, g.rho as i64, g.liquid as i64, g.albedo as i64 / 100, g.age as i64, g.crust as i64,
     g.wet() as i64, depth_bin(d, prev_bin), cover(d), h.t, h.tsurf]
}

/// Records [n x NF] as rows.
pub fn rows(f: &[i32]) -> Vec<&[i32]> {
    f.chunks_exact(NF).collect()
}

/// The ground pack of a site through the records up to `upto` (inclusive), seeded from the first record's depth.
pub fn run_ground(recs: &[&[i32]], upto: usize, s: &Site) -> (Ground, Hour) {
    let first = recs.first().map(|r| field(r, F_DEPTH, 0)).unwrap_or(0);
    let mut g = Ground::seed(first, s.z as i64 - s.z_cell as i64);
    let mut last = Hour::default();
    for r in recs.iter().take(upto + 1) {
        let h = downscale(r, s);
        g.step(&h, s.forest, radiation_index(s.aspect, s.lat_e2, h.month));
        last = h;
    }
    (g, last)
}

/// Does snow fall at the site within the 6 records after i?
fn snow_ahead(recs: &[&[i32]], i: usize, s: &Site) -> bool {
    recs.iter().skip(i + 1).take(6).any(|r| downscale(r, s).snow > 0)
}

/// The road classes P0..P4, then their bridge variants, through the records up to `upto` (inclusive) from clean
/// pavement (roads keep under 10 days of memory: the window replays them, plan 2.10).
pub fn run_roads(recs: &[&[i32]], upto: usize, s: &Site, regime: i64) -> [RoadOut; 2 * CLASSES] {
    let mut roads = [Road::default(); 2 * CLASSES];
    let mut last = Hour::default();
    for (i, r) in recs.iter().enumerate().take(upto + 1) {
        let h = downscale(r, s);
        let ahead = snow_ahead(recs, i, s);
        for (k, road) in roads.iter_mut().enumerate() {
            road.step(&h, k % CLASSES, k >= CLASSES, regime, ahead);
        }
        last = h;
    }
    let mut out = [RoadOut::default(); 2 * CLASSES];
    for (k, road) in roads.iter().enumerate() {
        let veh = AADT[k % CLASSES] * DAY_CURVE[last.hour as usize] / 1000;
        out[k] = road.out(pavement(&last, veh, k >= CLASSES));
    }
    out
}

/// The pack's band table of a cell: for each of BAND_DZ, the open ground's depth (0.1 mm) facing the pole and facing
/// the equator, and the forest floor's (flat).
pub fn run_bands(recs: &[&[i32]], upto: usize, z_cell: i32, lat_e2: i32) -> [[i64; 4]; 8] {
    let mut out = [[0i64; 4]; 8];
    for (b, dz) in BAND_DZ.iter().enumerate() {
        let base = Site { z: z_cell + dz, z_cell, lat_e2, built: 0, forest: false, aspect: -1 };
        let (pole, eq) = (run_ground(recs, upto, &base).0, run_ground(recs, upto, &Site { aspect: 1, ..base }).0);
        out[b][0] = pole.depth();
        out[b][1] = eq.depth();
        out[b][2] = run_ground(recs, upto, &Site { forest: true, aspect: 0, ..base }).0.depth();
        out[b][3] = pole.age.min(eq.age) as i64;
    }
    out
}

/// Cover 0..8 of a depth bin (the band table's, W2's look: a dusting half white, light cover nearly full).
pub const COVER_OF_BIN: [i64; 7] = [0, 4, 7, 8, 8, 8, 8];

/// Age class 0..3 of hours since the last snowfall: fresh (under a day), settled (3 days), old (10 days), dirty.
pub fn age_class(age_h: i64) -> i64 {
    if age_h < 24 {
        0
    } else if age_h < 72 {
        1
    } else if age_h < 240 {
        2
    } else {
        3
    }
}

/// W2's band table (snowcover.Table rows) from run_bands: per band (z m, cover 0..8, depth bin 0..6, age 0..3) of the
/// flat ground (the mean of both aspects: W2 moves the line by aspect itself); the bins with 15 % hysteresis from
/// prev_bins (-1: none). Only a bin or an age class moving changes a row, so the texture keys change only then.
pub fn band_table(bands: &[[i64; 4]; 8], z_cell: i32, prev_bins: &[i64]) -> [[i64; 4]; 8] {
    let mut out = [[0i64; 4]; 8];
    for b in 0..8 {
        let d = (bands[b][0] + bands[b][1]) / 2;
        let bin = depth_bin(d, prev_bins.get(b).copied().unwrap_or(-1));
        let age = if bin == 0 { 0 } else { age_class(bands[b][3]) };
        out[b] = [(z_cell + BAND_DZ[b]) as i64, COVER_OF_BIN[bin as usize], bin, age];
    }
    out
}

/// The share (eighths) of the ground's snow cover a road in this condition keeps (W2's Table.road per plow class).
pub fn road_share(cond: u8) -> i64 {
    match cond {
        LOOSE => 8,
        PACKED => 6,
        DUSTING | PACKED_ICY => 5,
        SLUSH => 3,
        _ => 0,
    }
}

/// Lake ice (plan 2.9) at a site through the records up to `upto`: freezing degree-days (0.01 degC day: cold hours
/// add, warm hours take away, never below 0), the ice thickness 2.7 sqrt(FDD) cm (x0.7 under more than 10 cm of snow)
/// in mm, and whole days below freezing in a row at the end. The window is 10 days: a lake frozen for weeks reads thin
/// here until the pack (W5) carries the season's degree-days.
pub fn lake_ice(recs: &[&[i32]], upto: usize, s: &Site, snow_depth: i64) -> [i64; 3] {
    let mut fdd: i64 = 0; // 0.01 degC hours
    let mut cold_run: i64 = 0;
    for r in recs.iter().take(upto + 1) {
        let h = downscale(r, s);
        fdd = (fdd - h.t).max(0);
        cold_run = if h.t < 0 { cold_run + 1 } else { 0 };
    }
    let fdd_days = fdd / 24; // 0.01 degC day
    // 2.7 sqrt(FDD degC day) cm = 27 sqrt(fdd_days / 100) mm = 2.7 sqrt(fdd_days) mm
    let mut ice = 27 * isqrt(fdd_days) / 10;
    if snow_depth > 1000 {
        ice = ice * 7 / 10;
    }
    [fdd_days, ice, cold_run / 24]
}

/// Lake ice thickness edges (mm) of W2's frozen-water share: 0 under 2 cm, 2 (a white rim) from 2 cm, 4 from 5 cm, 6
/// from 10 cm, 8 (frozen over, snow on it) from 15 cm [guess on the look; 25 cm is SAFE to drive on, plan 2.9].
pub const LAKE_EDGES: [i64; 4] = [20, 50, 100, 150];

/// W2's Table.ice: the share 0..8 of still water frozen and white, from the ice thickness (mm), changing from `prev`
/// (-1: none) only when the thickness passes an edge by 15 % (its texture keys follow it).
pub fn lake_share(ice_mm: i64, prev: i64) -> i64 {
    let level = |i: i64| LAKE_EDGES.iter().filter(|&&e| i >= e).count() as i64;
    let raw = level(ice_mm);
    if !(0..=4).contains(&(prev / 2)) || prev < 0 {
        return raw * 2;
    }
    let mut l = prev / 2;
    if raw > l {
        while l < 4 && ice_mm * 100 >= LAKE_EDGES[l as usize] * 115 {
            l += 1;
        }
    } else if raw < l {
        while l > 0 && ice_mm * 100 < LAKE_EDGES[(l - 1) as usize] * 85 {
            l -= 1;
        }
    }
    l * 2
}

/// The road condition the weather's wet-road part shows with winter off (BNE_WET_ROADS without BNE_WINTER): the water
/// film only (dry, damp, wet); snow, slush and ice are the winter part's, so a melting or salted cover reads wet and a
/// frozen one damp.
pub fn wet_only(cond: u8) -> u8 {
    match cond {
        DRY | DAMP | WET => cond,
        SALT_WET | SLUSH | WET_ICE | LOOSE | PACKED => WET,
        _ => DAMP,
    }
}

/// Integer square root (floor), exact on every platform.
pub fn isqrt(v: i64) -> i64 {
    if v <= 0 {
        return 0;
    }
    let mut x = v;
    let mut y = (x + 1) / 2;
    while y < x {
        x = y;
        y = (x + v / x) / 2;
    }
    x
}

/// A site's open-ground depth (0.1 mm) read from a band table: the two bands round its elevation blended by integer
/// weights, the aspect's column (flat: the mean of both).
pub fn band_depth(bands: &[[i64; 4]; 8], dz: i64, aspect: i32) -> i64 {
    let col = |b: usize| match aspect {
        a if a < 0 => bands[b][0],
        a if a > 0 => bands[b][1],
        _ => (bands[b][0] + bands[b][1]) / 2,
    };
    if dz <= BAND_DZ[0] as i64 {
        return col(0);
    }
    for b in 0..7 {
        let (lo, hi) = (BAND_DZ[b] as i64, BAND_DZ[b + 1] as i64);
        if dz <= hi {
            return col(b) + (col(b + 1) - col(b)) * (dz - lo) / (hi - lo);
        }
    }
    col(7)
}

/// Everything the snowstate message carries for one site: the ground outputs, the road rows, the band table, lake ice.
pub type StateParts = ([i64; GROUND_OUT], [RoadOut; 2 * CLASSES], [[i64; 4]; 8], [i64; 3]);

pub fn state(f: &[i32], upto: usize, s: &Site, regime: i64, prev_bin: i64) -> StateParts {
    let recs = rows(f);
    if recs.is_empty() {
        let g = Ground::default();
        return (ground_out(&g, &Hour::default(), prev_bin), [RoadOut::default(); 2 * CLASSES], [[0; 4]; 8], [0; 3]);
    }
    let upto = upto.min(recs.len() - 1);
    let (g, h) = run_ground(&recs, upto, s);
    let lake = lake_ice(&recs, upto, s, g.depth());
    (ground_out(&g, &h, prev_bin), run_roads(&recs, upto, s, regime), run_bands(&recs, upto, s.z_cell, s.lat_e2), lake)
}

/// The presets (plan 2.11: a preset is a fixed synthetic forcing record, deterministic by construction): records,
/// the regime they run with, the cell's elevation and latitude x 100. `light`: two cold dry days, then 6 h of light
/// snow (~3 cm) at -3 degC in an M2 climate. `ice`: a cold day, a rainy thaw, a clear frozen night (black ice) and
/// freezing drizzle (glaze) in an M0 climate (no fleet: an ice storm where nobody salts).
pub fn preset(name: &str) -> Option<(Vec<i32>, i64, i32, i32)> {
    // (hours, t, td, precip / h, snow share permille, wind, cloud, tsurf offset)
    let plan: &[(usize, i32, i32, i32, i32, i32, i32, i32)] = match name {
        "light" => &[(48, -400, -700, 0, 0, 30, 60, -100), (6, -300, -400, 50, 1000, 30, 100, -100),
                     (12, -400, -600, 0, 0, 20, 90, -100)],
        "ice" => &[(24, -600, -900, 0, 0, 20, 40, -100), (6, 200, 150, 100, 0, 30, 100, 100),
                   (8, -500, -600, 0, 0, 10, 0, -300), (4, -200, -250, 30, 0, 20, 100, -200),
                   (6, -400, -500, 0, 0, 10, 80, -300)],
        _ => return None,
    };
    let mut out = Vec::new();
    let mut hour = 0i32;
    for &(n, t, td, p, share, wind, cloud, toff) in plan {
        for _ in 0..n {
            let local = hour % 24;
            let day = (8..16).contains(&local);
            let mut r = [MISSING; NF];
            r[F_T2M] = t;
            r[F_TD] = td;
            r[F_PRECIP] = p;
            r[F_SNOWWE] = p * share / 1000;
            r[F_WIND] = wind;
            r[F_SW] = if day { 150 * (100 - cloud) / 100 + 20 } else { 0 };
            r[F_TSURF] = t + toff;
            r[F_ZFREEZE] = if t > 0 { 600 } else { 0 };
            r[F_DEPTH] = 0;
            r[F_RH] = 85;
            r[F_HOUR] = local;
            r[F_MONTH] = 1;
            r[F_CLOUD] = cloud;
            out.extend_from_slice(&r);
            hour += 1;
        }
    }
    let regime = if name == "ice" { 0 } else { 2 };
    Some((out, regime, 200, 4500))
}

/// FNV-1a 64 over a stream of integers (little-endian i64).
pub struct Fnv(pub u64);

impl Fnv {
    pub fn new() -> Fnv {
        Fnv(0xcbf2_9ce4_8422_2325)
    }
    pub fn add(&mut self, v: i64) {
        for b in v.to_le_bytes() {
            self.0 ^= b as u64;
            self.0 = self.0.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
}

impl Default for Fnv {
    fn default() -> Self {
        Fnv::new()
    }
}

fn splitmix(x: &mut u64) -> u64 {
    *x = x.wrapping_add(0x9e37_79b9_7f4a_7c15);
    let mut z = *x;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// n random forcing records from a seed (splitmix64; plausible winter ranges, a share of MISSING fields).
pub fn random_records(seed: u64, n: usize) -> Vec<i32> {
    let mut x = seed;
    let mut r = |lo: i64, hi: i64| lo + (splitmix(&mut x) % ((hi - lo + 1) as u64)) as i64;
    let mut out = Vec::with_capacity(n * NF);
    for i in 0..n {
        let t = r(-3000, 1500);
        let mut rec = [0i32; NF];
        rec[F_T2M] = t as i32;
        rec[F_TD] = (t - r(0, 1200)) as i32;
        rec[F_TW] = if r(0, 9) == 0 { MISSING } else { (t - r(0, 400)) as i32 };
        let p = if r(0, 2) == 0 { r(0, 800) } else { 0 };
        rec[F_PRECIP] = p as i32;
        rec[F_SNOWWE] = if r(0, 4) == 0 { MISSING } else { (p * r(0, 1000) / 1000) as i32 };
        rec[F_WIND] = r(0, 250) as i32;
        rec[F_SW] = r(0, 800) as i32;
        rec[F_TSURF] = if r(0, 9) == 0 { MISSING } else { (t + r(-500, 500)) as i32 };
        rec[F_ZFREEZE] = if r(0, 3) == 0 { MISSING } else { r(-200, 4000) as i32 };
        rec[F_DEPTH] = if i == 0 { r(0, 900) as i32 } else { r(0, 900) as i32 };
        rec[F_RH] = r(20, 100) as i32;
        rec[F_HOUR] = (i % 24) as i32;
        rec[F_MONTH] = r(1, 12) as i32;
        rec[F_CLOUD] = r(0, 100) as i32;
        out.extend_from_slice(&rec);
    }
    out
}

/// The determinism check: n random records stepped as one series through the ground pack (open and forest, three
/// aspects, five elevations) and every road class (each regime, plain and bridge), every hour's state hashed. The
/// same number on every platform (tools/snow_selftest, server/tests/test_snow.py's golden value).
pub fn selftest(seed: u64, n: usize) -> u64 {
    let f = random_records(seed, n);
    let recs = rows(&f);
    let mut hash = Fnv::new();
    let mut sites = Vec::new();
    for (zi, z) in [-400, 0, 300, 900, 2000].iter().enumerate() {
        for aspect in -1..=1 {
            for forest in [false, true] {
                sites.push(Site { z: 500 + z, z_cell: 500, lat_e2: [-6500, 4000, 4700, 6000, 1000][zi], built: 100 * zi as i32,
                                  forest, aspect });
            }
        }
    }
    let mut grounds: Vec<Ground> = sites.iter().map(|s| Ground::seed(field(recs[0], F_DEPTH, 0), s.z as i64 - 500))
                                        .collect();
    let road_site = Site { z: 520, z_cell: 500, lat_e2: 4500, built: 300, forest: false, aspect: 0 };
    let mut roads = [[Road::default(); 2 * CLASSES]; 5];
    let mut bins = vec![-1i64; sites.len()];
    for (i, r) in recs.iter().enumerate() {
        for (k, s) in sites.iter().enumerate() {
            let h = downscale(r, s);
            grounds[k].step(&h, s.forest, radiation_index(s.aspect, s.lat_e2, h.month));
            let o = ground_out(&grounds[k], &h, bins[k]);
            bins[k] = o[8];
            for v in o {
                hash.add(v);
            }
        }
        let h = downscale(r, &road_site);
        let ahead = snow_ahead(&recs, i, &road_site);
        for (regime, rs) in roads.iter_mut().enumerate() {
            for (k, road) in rs.iter_mut().enumerate() {
                road.step(&h, k % CLASSES, k >= CLASSES, regime as i64, ahead);
                let veh = AADT[k % CLASSES] * DAY_CURVE[h.hour as usize] / 1000;
                let o = road.out(pavement(&h, veh, k >= CLASSES));
                for v in [o.cond as i64, o.depth, o.tp, o.edge_cond as i64, o.edge_depth, o.grit as i64, o.track,
                          o.banks, road.loose as i64, road.packed as i64, road.ice as i64, road.water as i64,
                          road.salt as i64, road.frost as i64, road.polish as i64] {
                    hash.add(v);
                }
            }
        }
    }
    for s in sites.iter().step_by(5) {
        for v in lake_ice(&recs, recs.len() - 1, s, 0) {
            hash.add(v);
        }
    }
    hash.0
}

#[cfg(not(snow_standalone))]
pub mod py {
    //! The server's calls (server/snow.py).
    use super::*;
    use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;

    type StateOut = (Vec<i64>, Vec<Vec<i64>>, Vec<Vec<i64>>, Vec<i64>);

    /// The snow state of a site: forcing [n x NF] int32 (snow.rs F_* layout, MISSING = -2^31), the current hour's row
    /// `upto`, the site (elevation m, the forcing cell's elevation m, latitude x 100, built-up permille, aspect -1/0/1,
    /// forest), the plow regime 0..4 and the previous depth bin (-1: none). Returns (ground outputs (GROUND_OUT),
    /// road rows P0..P4 then bridges [cond, depth 0.1 mm, pavement 0.01 degC, edge cond, edge depth, grit, track
    /// permille, banks 0.01 mm], band rows [z m, depth facing the pole, facing the equator, forest floor (0.1 mm)]).
    #[pyfunction]
    #[allow(clippy::too_many_arguments)]
    pub fn snow_state(forcing: PyReadonlyArray2<'_, i32>, upto: usize, z_site: i32, z_cell: i32, lat_e2: i32,
                      built: i32, aspect: i32, forest: bool, regime: i64, prev_bin: i64) -> PyResult<StateOut> {
        let a = forcing.as_array();
        if a.ncols() != NF {
            return Err(PyValueError::new_err(format!("snow_state: forcing needs {NF} columns, has {}", a.ncols())));
        }
        let f: Vec<i32> = a.iter().copied().collect();
        let site = Site { z: z_site, z_cell, lat_e2, built: built.clamp(0, 1000), forest, aspect: aspect.signum() };
        let (g, roads, bands, lake) = state(&f, upto, &site, regime.clamp(0, 4), prev_bin);
        let roads = roads.iter().map(|o| vec![o.cond as i64, o.depth, o.tp, o.edge_cond as i64, o.edge_depth,
                                              o.grit as i64, o.track, o.banks]).collect();
        let bands = bands.iter().enumerate()
                         .map(|(b, r)| vec![(z_cell + BAND_DZ[b]) as i64, r[0], r[1], r[2], r[3]]).collect();
        Ok((g.to_vec(), roads, bands, lake.to_vec()))
    }

    /// A preset's synthetic forcing: (records [n x NF] int32, regime, cell elevation m, latitude x 100), or None.
    #[pyfunction]
    pub fn snow_preset<'py>(py: Python<'py>, name: &str)
                            -> Option<(Bound<'py, PyArray2<i32>>, i64, i32, i32)> {
        let (f, regime, z, lat) = preset(name)?;
        let n = f.len() / NF;
        let arr = numpy::ndarray::Array2::from_shape_vec((n, NF), f).ok()?.into_pyarray(py);
        Some((arr, regime, z, lat))
    }

    /// A site's open-ground depth (0.1 mm) from snow_state's band rows (as returned: [z, pole, equator, forest]),
    /// at dz m from the cell's elevation and aspect -1/0/1.
    #[pyfunction]
    pub fn snow_band_depth(bands: Vec<Vec<i64>>, dz: i64, aspect: i32) -> PyResult<i64> {
        Ok(band_depth(&band_rows(&bands)?, dz, aspect.signum()))
    }

    fn band_rows(bands: &[Vec<i64>]) -> PyResult<[[i64; 4]; 8]> {
        if bands.len() != 8 || bands.iter().any(|r| r.len() != 5) {
            return Err(PyValueError::new_err("snow bands: 8 rows of 5 [z, pole, equator, forest, age h]"));
        }
        let mut t = [[0i64; 4]; 8];
        for (b, r) in bands.iter().enumerate() {
            t[b] = [r[1], r[2], r[3], r[4]];
        }
        Ok(t)
    }

    /// W2's band table from snow_state's band rows (snowcover.Table rows: [z m, cover 0..8, depth bin 0..6, age
    /// 0..3] lowest first) with the bins' hysteresis from prev_bins (8 values, -1: none), and the road shares
    /// (eighths of the ground's cover kept) of the given road condition codes (P0..P4).
    #[pyfunction]
    pub fn snow_band_table(bands: Vec<Vec<i64>>, prev_bins: Vec<i64>, road_conds: Vec<u8>)
                           -> PyResult<(Vec<Vec<i64>>, Vec<i64>)> {
        let t = band_rows(&bands)?;
        let z_cell = (bands[2][0] - BAND_DZ[2] as i64) as i32;
        let rows = band_table(&t, z_cell, &prev_bins).iter().map(|r| r.to_vec()).collect();
        Ok((rows, road_conds.iter().map(|&c| road_share(c)).collect()))
    }

    /// W2's frozen-water share 0..8 from the ice thickness mm (snow_state's lake[1]) with hysteresis from prev.
    #[pyfunction]
    pub fn snow_lake_share(ice_mm: i64, prev: i64) -> i64 {
        lake_share(ice_mm, prev)
    }

    /// The wet-road part's condition code (winter off) of a road condition code.
    #[pyfunction]
    pub fn snow_wet_only(cond: u8) -> u8 {
        wet_only(cond)
    }

    /// The plow regime 0..4 of a Koppen-Geiger class (1-30).
    #[pyfunction]
    pub fn snow_regime(koppen: i64) -> i64 {
        regime_of_koppen(koppen)
    }

    /// The determinism hash of n random records from seed (the same on every platform).
    #[pyfunction]
    pub fn snow_selftest(seed: u64, n: usize) -> u64 {
        selftest(seed, n)
    }

    /// The condition names, by code.
    #[pyfunction]
    pub fn snow_conditions() -> Vec<&'static str> {
        CONDITION_NAMES.to_vec()
    }

    pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(snow_state, m)?)?;
        m.add_function(wrap_pyfunction!(snow_preset, m)?)?;
        m.add_function(wrap_pyfunction!(snow_regime, m)?)?;
        m.add_function(wrap_pyfunction!(snow_band_depth, m)?)?;
        m.add_function(wrap_pyfunction!(snow_band_table, m)?)?;
        m.add_function(wrap_pyfunction!(snow_lake_share, m)?)?;
        m.add_function(wrap_pyfunction!(snow_wet_only, m)?)?;
        m.add_function(wrap_pyfunction!(snow_selftest, m)?)?;
        m.add_function(wrap_pyfunction!(snow_conditions, m)?)?;
        m.add("SNOW_NF", NF)?;
        m.add("SNOW_MISSING", MISSING)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(t: i32, p: i32, share: i32, tsurf: i32, hour: i32) -> [i32; NF] {
        let mut r = [MISSING; NF];
        r[F_T2M] = t;
        r[F_TD] = t - 200;
        r[F_PRECIP] = p;
        r[F_SNOWWE] = p * share / 1000;
        r[F_WIND] = 20;
        r[F_SW] = if (8..16).contains(&hour) { 100 } else { 0 };
        r[F_TSURF] = tsurf;
        r[F_RH] = 85;
        r[F_HOUR] = hour;
        r[F_MONTH] = 1;
        r[F_CLOUD] = 80;
        r[F_DEPTH] = 0;
        r
    }

    fn series(spec: &[(usize, i32, i32, i32, i32)]) -> Vec<i32> {
        let mut out = Vec::new();
        let mut h = 0;
        for &(n, t, p, share, ts) in spec {
            for _ in 0..n {
                out.extend_from_slice(&rec(t, p, share, ts, h % 24));
                h += 1;
            }
        }
        out
    }

    const FLAT: Site = Site { z: 300, z_cell: 300, lat_e2: 4500, built: 0, forest: false, aspect: 0 };

    #[test]
    fn tables() {
        assert_eq!(DAY_CURVE.iter().sum::<i64>(), 1000);
        assert_eq!(new_density(0, 0), 119); // 67.9 + 51.3
        assert_eq!(new_density(-1000, 0), 68);
        assert_eq!(new_density(500, 0), 150); // clamped
        assert_eq!(new_density(-1000, 60), 84); // wind packs it
        assert_eq!(new_density(-1000, 200), 128);
        assert_eq!(track(0), 0);
        assert_eq!(track(120), 632);
        assert_eq!(snow_share_wb(-10), 1000);
        assert_eq!(snow_share_wb(75), 500);
        assert_eq!(snow_share_wb(200), 0);
    }

    #[test]
    fn cold_snowfall_accumulates() {
        // 10 h of 1 mm/h water at -5 degC on cold ground: ~10 mm SWE, ~10-14 cm of fresh snow
        let f = series(&[(10, -500, 100, 1000, -500)]);
        let (g, _) = run_ground(&rows(&f), 9, &FLAT);
        assert!(g.swe >= 980 && g.swe <= 1000, "{:?}", g);
        assert!(g.depth() > 1000 && g.depth() < 1500, "{}", g.depth());
        assert!(g.albedo as i64 >= 84_000);
    }

    #[test]
    fn warm_ground_melts_a_dusting() {
        // the same snow on +3 degC ground at +0.5 degC air: little or nothing stays
        let f = series(&[(3, 50, 50, 1000, 300)]);
        let (g, _) = run_ground(&rows(&f), 2, &FLAT);
        assert!(g.depth() < 20, "{:?}", g);
    }

    #[test]
    fn thaw_melts_and_refreezes() {
        let f = series(&[(10, -500, 100, 1000, -500), (12, 300, 0, 0, 200), (6, -500, 0, 0, -500)]);
        let recs = rows(&f);
        let (fresh, _) = run_ground(&recs, 9, &FLAT);
        let (thawed, _) = run_ground(&recs, 21, &FLAT);
        assert!(thawed.swe < fresh.swe, "melt: {:?} {:?}", fresh, thawed);
        assert!(thawed.rho > fresh.rho, "settled: {:?}", thawed);
        let (frozen, _) = run_ground(&recs, 27, &FLAT);
        if frozen.swe > 0 {
            assert!(frozen.crust && frozen.liquid == 0, "{:?}", frozen);
        }
    }

    #[test]
    fn elevation_and_aspect() {
        // a +1.5 degC rain at the cell turns to snow 600 m up; the pole-facing slope keeps more in the sun
        let f = series(&[(12, 150, 100, 1000, 150), (48, -100, 0, 0, -100)]);
        let recs = rows(&f);
        let low = run_ground(&recs, 59, &FLAT).0;
        let high = run_ground(&recs, 59, &Site { z: 900, ..FLAT }).0;
        assert!(high.swe > low.swe + 500, "{:?} {:?}", low, high);
        let f = series(&[(10, -300, 100, 1000, -300), (72, 0, 0, 0, -100)]);
        let recs = rows(&f);
        let north = run_ground(&recs, 81, &Site { aspect: -1, ..FLAT }).0;
        let south = run_ground(&recs, 81, &Site { aspect: 1, ..FLAT }).0;
        assert!(north.swe >= south.swe, "{:?} {:?}", north, south);
        assert!(radiation_index(-1, 4500, 1) < 1000 && radiation_index(1, -4500, 7) > 1000);
    }

    #[test]
    fn bins_hysteresis() {
        assert_eq!(depth_bin(0, -1), 0);
        assert_eq!(depth_bin(50, -1), 1);
        assert_eq!(depth_bin(100, -1), 2);
        assert_eq!(depth_bin(110, 1), 1); // not 15 % past the 1 cm edge yet
        assert_eq!(depth_bin(116, 1), 2);
        assert_eq!(depth_bin(90, 2), 2); // not 15 % under it
        assert_eq!(depth_bin(84, 2), 1);
        assert_eq!(depth_bin(7000, 0), 6);
        assert_eq!(depth_bin(0, 6), 0);
    }

    #[test]
    fn salted_motorway_clears_residential_packs() {
        // 8 h of snow at -4 degC (pavement -3) in an M2 climate, then 12 dry hours
        let f = series(&[(8, -400, 150, 1000, -300), (12, -400, 0, 0, -300)]);
        let out = run_roads(&rows(&f), 19, &FLAT, 2);
        let p0 = out[0];
        let p3 = out[3];
        assert!(matches!(p0.cond, DRY | DAMP | WET | SALT_WET), "P0 {:?}", p0);
        assert!(matches!(p3.cond, PACKED | LOOSE | PACKED_ICY), "P3 {:?}", p3);
        assert!(p3.depth > p0.depth);
        // a heavy fall in an M3 climate: the unsalted tertiary is plowed (banks) and gritted
        let f = series(&[(8, -800, 300, 1000, -700), (6, -800, 0, 0, -700)]);
        let out = run_roads(&rows(&f), 13, &FLAT, 3);
        assert!(out[2].banks > 0 && out[2].grit, "P2 {:?}", out[2]);
    }

    #[test]
    fn no_fleet_in_m0() {
        let f = series(&[(8, -400, 150, 1000, -300), (12, -400, 0, 0, -300)]);
        let out = run_roads(&rows(&f), 19, &FLAT, 0);
        assert_eq!(out[0].banks, 0);
        assert!(out[3].depth > 0);
    }

    #[test]
    fn bridges_freeze_first() {
        // a wet road (rain at +1 degC) then a clear night with pavement at +1 degC: the deck (2 degC colder) ices
        let f = series(&[(4, 200, 100, 0, 200), (6, 50, 0, 0, 100)]);
        let recs = rows(&f);
        // night hours: start at local 20
        let mut f2 = Vec::new();
        for (i, r) in recs.iter().enumerate() {
            let mut r = r.to_vec();
            r[F_HOUR] = ((20 + i) % 24) as i32;
            r[F_SW] = 0;
            f2.extend_from_slice(&r);
        }
        let out = run_roads(&rows(&f2), 9, &FLAT, 0);
        assert!(matches!(out[3].cond, WET | DAMP | DRY), "road {:?}", out[3]);
        assert!(matches!(out[CLASSES + 3].cond, BLACK_ICE | ICE | FROST), "deck {:?}", out[CLASSES + 3]);
    }

    #[test]
    fn freezing_rain_glazes() {
        let f = series(&[(4, -200, 80, 0, -300)]);
        let out = run_roads(&rows(&f), 3, &FLAT, 0);
        assert!(matches!(out[3].cond, ICE | BLACK_ICE), "{:?}", out[3]);
    }

    #[test]
    fn presets_run() {
        for (name, want) in [("light", [DUSTING, LOOSE, PACKED, SLUSH, WET, SALT_WET, DAMP]),
                             ("ice", [ICE, BLACK_ICE, WET_ICE, FROST, ICE, ICE, ICE])] {
            let (f, regime, z, lat) = preset(name).unwrap();
            let n = f.len() / NF;
            let site = Site { z, z_cell: z, lat_e2: lat, built: 0, forest: false, aspect: 0 };
            let (g, roads, bands, lake) = state(&f, n - 1, &site, regime, -1);
            assert!(lake[0] > 0, "{name}: cold hours give degree-days");
            assert!(roads.iter().take(CLASSES).any(|o| want.contains(&o.cond)), "{name}: {:?}", roads);
            if name == "light" {
                assert!(g[0] >= 100 && g[8] >= 2, "{name}: {:?}", g); // a light cover, D2
                assert!(bands[2][0] > 0);
            } else {
                assert!(roads.iter().all(|o| matches!(o.cond, ICE | BLACK_ICE | WET_ICE | FROST)), "{:?}", roads);
            }
        }
        assert!(preset("nope").is_none());
    }

    #[test]
    fn band_read() {
        let mut bands = [[0i64; 4]; 8];
        for (b, row) in bands.iter_mut().enumerate() {
            *row = [b as i64 * 100, b as i64 * 50, 0, 5];
        }
        assert_eq!(band_depth(&bands, 0, -1), 200);
        assert_eq!(band_depth(&bands, 75, -1), 250);
        assert_eq!(band_depth(&bands, 75, 0), (250 + 125) / 2);
        assert_eq!(band_depth(&bands, -1000, 1), 0);
        assert_eq!(band_depth(&bands, 9000, -1), 700);
    }

    #[test]
    fn w2_table_and_lake() {
        let mut bands = [[0i64; 4]; 8];
        for (b, row) in bands.iter_mut().enumerate() {
            *row = [b as i64 * 300, b as i64 * 300, 0, 30];
        }
        let t = band_table(&bands, 1000, &[-1; 8]);
        assert_eq!(t[0], [700, 0, 0, 0]); // no snow: no age either
        assert_eq!(t[1], [850, 7, 2, 1]); // 3 cm: light, D2, settled (30 h)
        assert_eq!(t[7][1..], [8, 4, 1]); // 21 cm: D4
        bands[1] = [450, 450, 0, 30]; // 4.5 cm after D3: not 15 % under its 5 cm edge, held
        assert_eq!(band_table(&bands, 1000, &[0, 3, 3, 3, 3, 3, 3, 4])[1][2], 3);
        assert_eq!(band_table(&bands, 1000, &[-1; 8])[1][2], 2);
        assert_eq!(road_share(LOOSE), 8);
        assert_eq!(road_share(SALT_WET), 0);
        assert_eq!(isqrt(0), 0);
        assert_eq!(isqrt(99), 9);
        assert_eq!(isqrt(100), 10);
        assert_eq!(isqrt(1 << 40), 1 << 20);
        // ten days at -10 degC: 100 degC days -> 2.7 * 10 = 27 cm
        let f = series(&[(240, -1000, 0, 0, -1000)]);
        let lake = lake_ice(&rows(&f), 239, &FLAT, 0);
        assert_eq!(lake, [10_000, 270, 10]);
        assert_eq!(lake_share(10, -1), 0);
        assert_eq!(lake_share(60, -1), 4);
        assert_eq!(lake_share(200, -1), 8);
        assert_eq!(lake_share(55, 2), 2); // 5.5 cm: not 15 % past the 5 cm edge from 2
        assert_eq!(lake_share(58, 2), 4);
        assert_eq!(lake_share(45, 4), 4); // not 15 % under it
        assert_eq!(lake_share(40, 4), 2);
        assert_eq!(wet_only(PACKED), WET);
        assert_eq!(wet_only(BLACK_ICE), DAMP);
        assert_eq!(wet_only(DAMP), DAMP);
        // a thaw takes it back
        let f = series(&[(48, -500, 0, 0, -500), (72, 500, 0, 0, 500)]);
        assert_eq!(lake_ice(&rows(&f), 119, &FLAT, 0)[0], 0);
    }

    #[test]
    fn regimes() {
        assert_eq!(regime_of_koppen(1), 0);
        assert_eq!(regime_of_koppen(15), 1);
        assert_eq!(regime_of_koppen(25), 2);
        assert_eq!(regime_of_koppen(26), 3);
        assert_eq!(regime_of_koppen(27), 4);
        assert_eq!(regime_of_koppen(0), 0);
    }

    #[test]
    fn deterministic() {
        assert_eq!(selftest(1, 500), selftest(1, 500));
        assert_ne!(selftest(1, 500), selftest(2, 500));
    }
}
