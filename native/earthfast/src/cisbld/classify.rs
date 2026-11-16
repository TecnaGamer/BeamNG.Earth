//! Which kind of housing a building is, in the countries of the former Soviet Union (plan 048).
//!
//! The data rarely says (OSM start_date on a multi-storey block: ~0 % in Kyiv, Kharkiv, Almaty, 15 % in Minsk; no
//! series anywhere), so the kind is read from what the building IS: floors (the standard 5 / 9 / 12 / 16), the depth of
//! the body (10-14 m: one flat's width twice), a plain box, a length that is whole stair sections, the same block
//! standing again nearby, a free layout against the street, and GHS-OBAT's epoch where there is one. A known year
//! decides at once. Every number below is a constant calibrated on the OSM buildings that do have a year
//! (tools/cis_calibrate.py); nothing is random.
use super::context::Around;
use super::features::Feat;
use crate::dm::Dm;

pub const NONE: i32 = 0;
pub const HOUSE: i32 = 1;
pub const STALINKA: i32 = 2;
pub const KHRUSH_BRICK: i32 = 3;
pub const KHRUSH_PANEL: i32 = 4;
pub const PANEL9: i32 = 5;
pub const PANEL16: i32 = 6;
pub const TOWER: i32 = 7;
pub const BRICK_MID: i32 = 8;
pub const LATE_SOVIET: i32 = 9;
pub const NEW_MONO: i32 = 10;
pub const NEW_LOW: i32 = 11;
pub const NEW_PANEL: i32 = 12;

/// A building's data, as the server read it. NaN / 0 = not known.
#[derive(Clone, Copy)]
pub struct Info {
    /// building:levels / num_floors, NaN unknown.
    pub floors: f64,
    pub height: f64,
    /// start_date year, NaN unknown.
    pub year: f64,
    /// 0 unknown, 1 concrete / panel, 2 brick, 3 glass / metal, 4 plaster / other.
    pub material: i32,
    /// GHS-OBAT epoch 1..5, 0 unknown.
    pub obat: i32,
    /// 0 apartments / residential / unspecified, 1 house, 2 not housing (shops, offices, industry, public).
    pub class: i32,
    /// Buildings sharing a wall with it.
    pub touching: i32,
    /// 0 other, 1 UA, 2 BY, 3 RU, 4 MD, 5 KZ, 6 Caucasus / Central Asia, 7 Baltics.
    pub country: i32,
}

pub struct Verdict {
    pub kit: i32,
    pub soviet: bool,
    /// 0..1: how sure.
    pub conf: f64,
    pub floors: i32,
    pub entrances: i32,
    pub tech: bool,
    pub section_m: f64,
}

/// Nominal stair-section length (m) of a kit; 0 = one entrance (towers) or not by sections.
pub fn section_m(kit: i32) -> f64 {
    match kit {
        STALINKA => 17.0,
        KHRUSH_BRICK => 14.0,
        KHRUSH_PANEL => 15.5,
        PANEL9 => 18.0,
        PANEL16 => 17.0,
        BRICK_MID => 17.0,
        LATE_SOVIET => 17.0,
        NEW_MONO => 28.0,
        NEW_LOW => 20.0,
        NEW_PANEL => 21.0,
        _ => 0.0,
    }
}

pub fn floors_of(i: &Info) -> i32 {
    if i.floors.is_finite() && i.floors >= 1.0 {
        return (i.floors.round() as i32).clamp(1, 120);
    }
    if i.height.is_finite() && i.height >= 2.5 {
        return ((i.height / 2.85).round() as i32).clamp(1, 120);
    }
    0
}

fn sigmoid(s: f64) -> f64 {
    1.0 / (1.0 + (-s).dexp())
}

/// Log-odds that a block of this shape and surroundings is Soviet rather than new (> 0: Soviet).
pub fn soviet_score(nf: i32, i: &Info, f: &Feat, a: &Around) -> f64 {
    let mut s = 0.0;
    // floors: the standard series heights
    s += match nf {
        5 | 9 => 1.5,
        12 | 16 => 1.2,
        4 => 0.6,
        6 => 0.2,
        7 | 8 | 10 | 11 | 13 | 15 | 17..=19 => -0.8,
        n if n >= 20 => -1.2,
        _ => 0.0,
    };
    // depth of the body: the old series are 10-14 m, new monolithic blocks are deeper and more complex
    s += if f.arm < 9.0 {
        -0.3
    } else if f.arm <= 14.0 {
        1.0
    } else if f.arm <= 16.0 {
        0.0
    } else {
        -1.0
    };
    // a plain box; an L or a U of slabs is still Soviet, a many-cornered outline is not
    s += if f.rect >= 0.93 {
        0.8
    } else if f.rect >= 0.85 {
        0.3
    } else if f.rect >= 0.55 {
        -0.3
    } else {
        -1.0
    };
    if f.corners > 10 {
        s -= 1.0;
    }
    // length in whole stair sections
    for sec in [13.5, 15.5, 17.5, 19.0] {
        let k = (f.l / sec).round();
        if k >= 2.0 && (f.l - k * sec).abs() < 1.2 {
            s += 0.5;
            break;
        }
    }
    s += match a.serial {
        0 => 0.0,
        1 | 2 => 0.3,
        _ => 0.8,
    };
    // a free layout: set back from the street and not lying along it (a street-front block does)
    if a.road_d >= 15.0 && a.road_ang > 10.0 {
        s += 0.6;
    }
    s += match i.obat {
        1 => 0.4,
        2 => 0.8,
        4 => -1.8,
        5 => -2.5,
        _ => 0.0,
    };
    s += match i.material {
        1 => 1.0,
        2 => 0.2,
        3 => -1.5,
        _ => 0.0,
    };
    s
}

/// How much a block looks like a Stalinist one (a street-front perimeter block of 3-8 storeys).
fn stalinka_score(nf: i32, i: &Info, f: &Feat, a: &Around) -> f64 {
    let mut s = 0.0;
    if (3..=8).contains(&nf) {
        s += if (4..=6).contains(&nf) { 1.0 } else { 0.5 };
    }
    if (12.0..=17.0).contains(&f.arm) {
        s += 0.8;
    }
    if i.touching >= 1 {
        s += 1.0;
    }
    if a.road_d < 10.0 {
        s += 1.0;
        if a.road_ang < 12.0 {
            s += 0.8;
        }
    }
    if i.obat <= 1 {
        s += 0.3;
    } else {
        s -= 1.0;
    }
    if a.serial >= 3 {
        s -= 1.0;
    }
    s
}

fn entrances(kit: i32, l: f64) -> i32 {
    let sec = section_m(kit);
    if sec <= 0.0 {
        return 1;
    }
    ((l / sec).round() as i32).clamp(1, 12)
}

fn verdict(kit: i32, soviet: bool, conf: f64, nf: i32, f: &Feat) -> Verdict {
    Verdict {
        kit,
        soviet,
        conf: conf.clamp(0.0, 1.0),
        floors: nf,
        entrances: entrances(kit, f.l),
        tech: soviet && nf >= 9 && kit != STALINKA,
        section_m: section_m(kit),
    }
}

fn soviet_kind(nf: i32, i: &Info, f: &Feat, serial: u32) -> i32 {
    let squareish = f.l / f.w.max(1.0) < 1.6 && f.l < 32.0;
    let brick = i.material == 2;
    match nf {
        0..=3 => HOUSE,
        4..=6 => {
            if brick {
                KHRUSH_BRICK
            } else if i.material == 1 || serial >= 1 || i.country == 2 {
                KHRUSH_PANEL
            } else {
                // 60 % panel, 40 % brick over the country, decided from the footprint so it repeats
                if ((f.l * 10.0).round() as i64 + (f.w * 10.0).round() as i64) % 5 < 3 { KHRUSH_PANEL } else { KHRUSH_BRICK }
            }
        }
        7..=8 => BRICK_MID,
        9..=11 => {
            if squareish {
                TOWER
            } else if brick {
                BRICK_MID
            } else {
                PANEL9
            }
        }
        _ => {
            if squareish {
                TOWER
            } else {
                PANEL16
            }
        }
    }
}

fn new_kind(nf: i32, i: &Info, f: &Feat) -> i32 {
    if nf >= 17 && f.rect >= 0.93 && f.arm <= 15.0 && matches!(i.country, 2 | 3 | 5) {
        NEW_PANEL
    } else if nf >= 10 || f.corners > 8 || f.arm > 15.0 {
        NEW_MONO
    } else {
        NEW_LOW
    }
}

/// The verdict for one building. `None` kit = leave it to the rest of the code.
pub fn classify(i: &Info, f: &Feat, a: &Around) -> Verdict {
    let nf = floors_of(i);
    let none = |nf| verdict(NONE, false, 0.0, nf, f);
    if i.class == 2 {
        return none(nf);
    }
    // a house: low, small, alone or in a street row
    if nf <= 2 && f.area < 350.0 && (i.class == 1 || nf >= 1 || f.area < 200.0) && f.l < 40.0 {
        return verdict(HOUSE, true, 0.6, nf.max(1), f);
    }
    if nf <= 0 {
        // no floors and no height: only a small footprint says anything
        return if f.area < 120.0 { verdict(HOUSE, true, 0.4, 1, f) } else { none(nf) };
    }
    if nf == 3 && f.area < 200.0 {
        return verdict(HOUSE, true, 0.4, nf, f);
    }
    if nf < 3 {
        return none(nf);
    }
    // a known year decides
    if i.year.is_finite() && (1800.0..=2100.0).contains(&i.year) {
        let y = i.year;
        if y < 1933.0 {
            return none(nf);
        }
        if y < 1958.0 {
            return verdict(STALINKA, true, 0.9, nf, f);
        }
        if y < 1992.0 {
            let k = soviet_kind(nf, i, f, a.serial);
            return verdict(if k == HOUSE { KHRUSH_PANEL } else { k }, true, 0.9, nf, f);
        }
        if y < 2000.0 {
            return verdict(LATE_SOVIET, true, 0.8, nf, f);
        }
        return verdict(new_kind(nf, i, f), false, 0.9, nf, f);
    }
    let s = soviet_score(nf, i, f, a);
    if s > 0.0 {
        // Stalinist only where the whole pattern is there
        if stalinka_score(nf, i, f, a) >= 3.6 && (3..=8).contains(&nf) {
            return verdict(STALINKA, true, sigmoid(s).min(0.75), nf, f);
        }
        let k = soviet_kind(nf, i, f, a.serial);
        verdict(k, true, sigmoid(s), nf, f)
    } else {
        // new: below 4 floors a low block is as often a Soviet two-storey barrack or a village building: leave it
        if nf < 4 {
            return none(nf);
        }
        verdict(new_kind(nf, i, f), false, sigmoid(-s), nf, f)
    }
}

#[cfg(test)]
mod tests {
    use super::super::features::features;
    use super::*;

    fn info(nf: f64) -> Info {
        Info { floors: nf, height: f64::NAN, year: f64::NAN, material: 0, obat: 0, class: 0, touching: 0, country: 1 }
    }

    fn rect(l: f64, w: f64) -> Feat {
        features(&[0.0, l, l, 0.0], &[0.0, 0.0, w, w]).unwrap()
    }

    fn free_serial() -> Around {
        Around { serial: 3, road_d: 25.0, road_ang: 40.0, road_side: 1.0 }
    }

    fn alone() -> Around {
        Around { serial: 0, road_d: 80.0, road_ang: 90.0, road_side: 0.0 }
    }

    #[test]
    fn a_five_storey_long_box_in_a_series_is_a_khrushchyovka() {
        let v = classify(&info(5.0), &rect(62.0, 11.5), &free_serial());
        assert!(v.soviet && (v.kit == KHRUSH_PANEL || v.kit == KHRUSH_BRICK), "{}", v.kit);
        assert!(v.entrances == 4 && !v.tech, "{} {}", v.entrances, v.tech);
    }

    #[test]
    fn a_nine_storey_slab_has_a_tech_storey_and_sections() {
        let v = classify(&info(9.0), &rect(72.0, 12.5), &free_serial());
        assert_eq!(v.kit, PANEL9);
        assert!(v.tech && v.entrances == 4);
    }

    #[test]
    fn a_square_sixteen_storey_block_is_a_tower_with_one_entrance() {
        let v = classify(&info(16.0), &rect(24.0, 24.0), &free_serial());
        assert_eq!(v.kit, TOWER);
        assert_eq!(v.entrances, 1);
    }

    #[test]
    fn a_long_sixteen_storey_slab_is_panel16() {
        assert_eq!(classify(&info(16.0), &rect(54.0, 13.2), &free_serial()).kit, PANEL16);
    }

    #[test]
    fn a_deep_nonstandard_tall_block_alone_is_new() {
        let v = classify(&info(23.0), &rect(38.0, 22.0), &alone());
        assert!(!v.soviet && v.kit == NEW_MONO, "{} {}", v.soviet, v.kit);
    }

    #[test]
    fn obat_epoch_five_turns_an_ambiguous_block_new() {
        let mut i = info(10.0);
        let a = Around { serial: 0, road_d: 10.0, road_ang: 5.0, road_side: 1.0 };
        let old = classify(&i, &rect(48.0, 15.0), &a);
        i.obat = 5;
        let new = classify(&i, &rect(48.0, 15.0), &a);
        assert!(!new.soviet, "epoch 5 is new");
        assert!(soviet_score(10, &i, &rect(48.0, 15.0), &a) < soviet_score(10, &info(10.0), &rect(48.0, 15.0), &a));
        let _ = old;
    }

    #[test]
    fn a_known_year_decides() {
        let mut i = info(5.0);
        i.year = 1952.0;
        assert_eq!(classify(&i, &rect(60.0, 14.0), &alone()).kit, STALINKA);
        i.year = 1966.0;
        assert!(classify(&i, &rect(60.0, 11.5), &alone()).soviet);
        i.year = 2012.0;
        assert!(!classify(&i, &rect(60.0, 14.0), &alone()).soviet);
        i.year = 1905.0;
        assert_eq!(classify(&i, &rect(30.0, 14.0), &alone()).kit, NONE);
    }

    #[test]
    fn a_street_front_perimeter_block_is_a_stalinka() {
        let mut i = info(5.0);
        i.touching = 2;
        let a = Around { serial: 0, road_d: 4.0, road_ang: 3.0, road_side: 1.0 };
        let v = classify(&i, &rect(70.0, 15.0), &a);
        assert_eq!(v.kit, STALINKA);
        assert!(!v.tech);
    }

    #[test]
    fn small_buildings_are_houses_and_shops_are_left_alone() {
        assert_eq!(classify(&info(1.0), &rect(11.0, 9.0), &alone()).kit, HOUSE);
        let mut shop = info(2.0);
        shop.class = 2;
        assert_eq!(classify(&shop, &rect(30.0, 20.0), &alone()).kit, NONE);
        assert_eq!(classify(&info(f64::NAN), &rect(10.0, 8.0), &alone()).kit, HOUSE);
        assert_eq!(classify(&info(f64::NAN), &rect(40.0, 30.0), &alone()).kit, NONE);
    }

    #[test]
    fn height_stands_in_for_floors() {
        let mut i = info(f64::NAN);
        i.height = 26.0;
        assert_eq!(floors_of(&i), 9);
    }
}
