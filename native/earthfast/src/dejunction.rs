//! German right of way at a junction without signals (server/designs.py is the glue): the sign each arm of one
//! junction gets, and the 1002 plate of a bent priority road. Pure: the arms' directions and codes in, a code per arm
//! out. The Vorfahrtstraße stretches (de_stretch) are a property of a road, not of a junction.
//!
//! The rules:
//! - Right before left where nothing else says otherwise (StVO § 8 Abs. 1 Satz 1), and inside a Tempo 30 zone as its
//!   rule (StVO § 45 Abs. 1c Satz 4: "An Kreuzungen und Einmündungen innerhalb der Zone muss grundsätzlich die
//!   Vorfahrtregel nach § 8 Absatz 1 Satz 1 ('rechts vor links') gelten").
//! - Zeichen 205 "Vorfahrt gewähren." and 206 "Halt. Vorfahrt gewähren." (StVO Anlage 2 lfd. Nr. 2 and 3). A junction
//!   that departs from right before left is signed on both roads (VwV-StVO zu den Zeichen 205 und 206 Rn. 6: "sowohl
//!   positiv als auch negativ zu beschildern"), so the arms off a 301 / 306 pair get 205. A stop sign is only ever
//!   the map's (the design, B1: rule-placed stops are not invented).
//! - Where a junction departs from right before left at all (a mapped 205 / 206 on an arm coming in, or a link's
//!   give-way) it is no right-before-left junction for its other arms either: they take a priority pair and 205 (zu
//!   den Zeichen 205 und 206 Rn. 6, above), in a 30 zone too, where Zeichen 301 is the means of that departure
//!   (VwV-StVO zu § 45 Abs. 1 bis 1e Rn. 41: "kann abweichend von der Grundregel 'rechts vor links' die Vorfahrt
//!   durch Zeichen 301 angeordnet werden").
//! - In a 30 zone (every arm coming in in it) a pair is 301, never 306: the zone holds no Vorfahrtstraße (StVO § 45
//!   Abs. 1c Satz 2: "Die Zonen-Anordnung darf sich weder auf Straßen des überörtlichen Verkehrs (Bundes-, Landes-
//!   und Kreisstraßen) noch auf weitere Vorfahrtstraßen (Zeichen 306) erstrecken"), whatever the roads' classes say.
//! - Zeichen 301 "Vorfahrt" (StVO Anlage 3 lfd. Nr. 1) and 306 "Vorfahrtstraße" (lfd. Nr. 2). In town every road of
//!   more than local traffic and the main roads get 306 (VwV-StVO zu den Zeichen 306 und 307 Rn. 1: "die Vorfahrt für
//!   alle Straßen des überörtlichen Verkehrs (Bundes-, Landes- und Kreisstraßen) und weitere für den innerörtlichen
//!   Verkehr wesentliche Hauptverkehrsstraßen grundsätzlich unter Verwendung des Zeichens 306"), not 301 (zu Zeichen
//!   301 Rn. 4: "für Ortsdurchfahrten und Hauptverkehrsstraßen nicht anzuordnen"). So a stretch (de_stretch, in and
//!   out of town alike) is a mapped priority_road=designated or yes_unposted, else trunk / primary / secondary /
//!   tertiary, a tertiary with a ref or without (a Kreisstraße, or a town's main road or a Gemeindeverbindungsstraße
//!   that OpenStreetMap ranks with them: 301 at each junction of Barbing's Regensburger Straße, four in a row, was the
//!   user's report, 2026-11-07), unless the map says priority_road=end; 301 is for a priority found by rank alone.
//! - The 1002 plate ("Verlauf der Vorfahrtstraße") exists only with a Vorfahrtstraße: under its 306 (StVO Anlage 3
//!   lfd. Nr. 2.1: "Das Zusatzzeichen zum Zeichen 306 zeigt den Verlauf der Vorfahrtstraße an") and under the 205 /
//!   206 of that junction (Anlage 2 zu Nr. 2 und 3: "...zusammen mit den Zeichen 205 oder 206 den Verlauf der
//!   Vorfahrtstraße..."); never with 301 (VwV-StVO zu Zeichen 301 Rn. 3: "Das Zusatzzeichen für die abknickende
//!   Vorfahrt (hinter Zeichen 306) darf nicht zusammen mit dem Zeichen 301 angeordnet werden"). So a pair found by
//!   rank keeps 301 and its junction has no plate.
//! - The class rules are the design's (docs/research/2026-11-06-german-junctions-design.md, B1): residential-type
//!   roads meeting are right before left; two equal roads above that are never left so (German practice signs them).
//!
//! The codes (designs.py has the same): class living_street 0, residential 1, unclassified 2, tertiary 3, secondary
//! 4, primary 5, trunk 6, motorway 7 (anything else 0); ref rank 0 none, 1 Kreisstraße, 2 Staats- / Landesstraße, 3
//! Bundesstraße; priority 0 none, 1 designated, 2 end, 3 yes_unposted; an arm's flags ZONE30 | STRETCH | OUTBOUND |
//! RING; mapped -1 none, 0 give-way, 1 stop; what NONE, Z205, Z206, Z306, Z301, RVL; plate -1 or an index into
//! PATTERNS (designs.PLATES).
//!
//! The derivation (junction), in order (the design's B1); fewer than three arms are no junction (nothing). "Remaining"
//! arms are the unmapped ones, OUTBOUND ones included: a one-way road through leaves the junction on one of them (a
//! town's one-way street, a divided road's carriageway), and a side road coming in still gives way to it. An OUTBOUND
//! arm itself never gets a sign or a plate.
//! 1. Mapped: an arm traffic comes in on (not OUTBOUND) with a mapped sign gets 205 / 206. Then a junction with two
//!    RING arms (on a roundabout) follows furniture.junction_controls' ring rule, the map's signs first: every other
//!    unmapped arm coming in 205, nothing on the ring, no plates (the glue hands rings to furniture before it calls
//!    this). Then, when an arm is mapped and exactly two arms remain, at least one coming in, they are the priority
//!    pair (an OUTBOUND arm counted, as in 2 and 4: else a mapped side road where a one-way or divided road runs
//!    through would hand the priority to the opposite side road).
//! 2. Stretch: two or more remaining arms on a stretch (STRETCH) whose best pair is two arms: that pair. One stretch
//!    arm alone (a stretch ending where its road drops a class) is no pair: on to 3.
//! 3. Right before left, only where no arm coming in is mapped: every arm coming in in a 30 zone, or every one
//!    residential-type (living_street, residential; unclassified in town): they get RVL, no pair. A mapped one sends
//!    the junction on to 4 (zu den Zeichen 205 und 206 Rn. 6: signed on both roads; in a 30 zone by 301, zu § 45 Abs. 1
//!    bis 1e Rn. 41).
//! 4. Rank: the best pair of the remaining arms; one arm coming in alone (every other one mapped or leaving) is the
//!    priority road by itself.
//! The pair gets 306 when both its arms are on a stretch (always from 2; from 4 only a lone arm on a stretch) and the
//! junction is not in a 30 zone, else 301, and the other unmapped arms coming in get 205.
//! - The best pair of some candidate arms: an OUTBOUND arm ranks no higher than the best candidate coming in (it only
//!    shows where a road through leads), and a pair needs an arm coming in. Then the two highest (cls, ref_rank) ranks
//!    (one top arm and its tied seconds, or the tied tops); of those pairs, the ones within STRAIGHT_SLACK of the most
//!    negative u·u (the straightest), fewer OUTBOUND arms first (a divided road's inbound carriageway, not its outbound
//!    twin, pairs with the road ahead), then the most negative u·u, then the arms' order.
//! - Plates: only when the pair is 306 and bent, i.e. its u·u is more than STRAIGHT_SLACK above the straightest pair of
//!    the junction's arms with one coming in (the pairs a priority pair can be, OUTBOUND arms included: a stretch that
//!    turns where a one-way leaves straight on is bent). Then every arm signed 306, 205 or 206 there gets the plate of
//!    its own view (plate_of). No plate with more than PLATE_ARMS_MAX arms.
//!
//! An arm's view (plate_of): along its approach inc = -u, with the drivers' right (inc.y, -inc.x) as
//! furniture.approach_pose (x east, y north), each other arm (OUTBOUND ones too: they are roads on the plate) is
//! ahead, right or left by the nearest 90 degrees. Two in one slot is no plate, and so is a picture PATTERNS does not
//! have (e.g. the pair straight on from this view). An arm behind (within 45 degrees of this arm itself) is no plate
//! either, not left or right by the nearest 90 degrees: forced into left or right, the plate would draw at a right
//! angle a road that runs back beside the approach, a wrong picture of the junction, which is worse than no plate.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub const NONE: u8 = 0;         // what: no sign (an arm leaving, the ring itself, fewer than three arms)
pub const Z205: u8 = 1;
pub const Z206: u8 = 2;
pub const Z306: u8 = 3;
pub const Z301: u8 = 4;
pub const RVL: u8 = 5;          // right before left: no sign, the AI stops

pub const ZONE30: u8 = 1;       // flags: the arm is in a 30 zone
pub const STRETCH: u8 = 2;      // ... on a Vorfahrtstraße stretch (de_stretch)
pub const OUTBOUND: u8 = 4;     // ... a one-way leaving the junction
pub const RING: u8 = 8;         // ... a roundabout's ring

const RESIDENTIAL: u8 = 1;      // the class codes (living_street 0)
const UNCLASSIFIED: u8 = 2;
const TERTIARY: u8 = 3;
const TRUNK: u8 = 6;
const DESIGNATED: u8 = 1;       // the priority_road codes
const END: u8 = 2;
const YES_UNPOSTED: u8 = 3;

const STRAIGHT_SLACK: f64 = 0.05;   // u·u a pair may be above the straightest and still count as straight
const PLATE_ARMS_MAX: usize = 4;    // the 1002 pictures have up to four roads

const D: u8 = 1;                // a plate's slots: the approach (down on the plate), left, ahead, right
const L: u8 = 2;
const A: u8 = 4;
const R: u8 = 8;
/// The 1002 pictures (designs.PLATES, in this order): (the other arms' slots, the priority pair's slots).
pub const PATTERNS: [(u8, u8); 10] = [
    (L | R, D | L), (L | R, D | R), (L | A, D | L), (L | A, L | A), (R | A, D | R), (R | A, R | A),
    (L | A | R, D | L), (L | A | R, D | R), (L | A | R, L | A), (L | A | R, A | R),
];

/// Is a road a Vorfahrtstraße stretch: mapped as one (priority_road designated / yes_unposted), or, unless mapped as
/// its end, trunk / primary / secondary / tertiary (VwV-StVO zu den Zeichen 306 und 307 Rn. 1: the Bundes-, Landes-
/// and Kreisstraßen and the main roads; a tertiary without a ref is a town's main road or a Gemeindeverbindungsstraße,
/// and in town 301 is "für Ortsdurchfahrten und Hauptverkehrsstraßen nicht anzuordnen", zu Zeichen 301 Rn. 4). The ref
/// rank still ranks two stretches (junction). Motorways have no junctions at grade.
pub fn stretch(cls: u8, _ref_rank: u8, prio: u8) -> bool {
    prio == DESIGNATED || prio == YES_UNPOSTED || (prio != END && (TERTIARY..=TRUNK).contains(&cls))
}

fn dot(a: [f64; 2], b: [f64; 2]) -> f64 {
    a[0] * b[0] + a[1] * b[1]
}

fn unit(v: [f64; 2]) -> [f64; 2] {
    let l = v[0].dhypot(v[1]);
    if l.is_finite() && l > 1e-12 { [v[0] / l, v[1] / l] } else { [0.0, 0.0] }    // (no NaN in the comparisons)
}

/// The priority pair of the candidate arms (in arm order; the module doc): two arms, one alone, or none. An OUTBOUND
/// arm ranks no higher than the best candidate coming in, and a pair needs an arm coming in; then the two highest
/// ranks, within STRAIGHT_SLACK of the straightest the fewer OUTBOUND arms, the straightest, the arms' order.
fn best_pair(u: &[[f64; 2]], rank: &[u16], out: &[bool], cands: &[usize]) -> Vec<usize> {
    let Some(top_in) = cands.iter().filter(|&&i| !out[i]).map(|&i| rank[i]).max() else {
        return Vec::new();      // nothing comes in: no one to sign
    };
    if cands.len() < 2 {
        return cands.to_vec();  // the one arm coming in
    }
    let r = |i: usize| if out[i] { rank[i].min(top_in) } else { rank[i] };
    let mut top: Vec<u16> = cands.iter().map(|&i| r(i)).collect();
    top.sort_unstable_by(|a, b| b.cmp(a));
    let mut pairs = Vec::new();         // (i, j, u·u, outbound arms)
    for (k, &i) in cands.iter().enumerate() {
        for &j in &cands[k + 1..] {
            if (r(i).max(r(j)), r(i).min(r(j))) == (top[0], top[1]) && !(out[i] && out[j]) {
                pairs.push((i, j, dot(u[i], u[j]), out[i] as u8 + out[j] as u8));
            }
        }
    }
    let straightest = pairs.iter().map(|p| p.2).fold(f64::INFINITY, f64::min);
    let best = pairs.iter().filter(|p| p.2 <= straightest + STRAIGHT_SLACK)
        .min_by(|a, b| a.3.cmp(&b.3).then(a.2.total_cmp(&b.2)))     // (the first of equals)
        .expect("an arm coming in holds the top rank: it and a top-two partner are a pair");
    vec![best.0, best.1]
}

/// Arm i's 1002 plate (an index into PATTERNS, -1 none) at a junction whose priority pair is (p, q): the module doc.
fn plate_of(u: &[[f64; 2]], i: usize, (p, q): (usize, usize)) -> i8 {
    let inc = [-u[i][0], -u[i][1]];
    let right = [inc[1], -inc[0]];
    let mut slot = vec![D; u.len()];
    let mut others = 0u8;
    for (j, &v) in u.iter().enumerate() {
        if j == i {
            continue;
        }
        let s = match (dot(v, right).datan2(dot(v, inc)) / std::f64::consts::FRAC_PI_2).round() as i32 {
            0 => A,
            1 => R,
            -1 => L,
            _ => return -1,     // behind: on the approach's side
        };
        if others & s != 0 {
            return -1;          // two in one slot
        }
        others |= s;
        slot[j] = s;
    }
    let pair = slot[p] | slot[q];
    PATTERNS.iter().position(|&(o, k)| o == others && k == pair).map_or(-1, |k| k as i8)
}

/// One junction's (what, plate) per arm (the module doc's derivation). u: each arm's direction away from the junction
/// (normalised here); cls, ref_rank: its road's codes; flags: ZONE30 | STRETCH | OUTBOUND | RING; mapped: -1 none, 0
/// give-way, 1 stop (all of u's length: de_junction checks it); urban: every arm in town. Fewer than three arms are
/// no junction: nothing.
pub(crate) fn junction(u: &[[f64; 2]], cls: &[u8], ref_rank: &[u8], flags: &[u8], mapped: &[i8], urban: bool)
                       -> (Vec<u8>, Vec<i8>) {
    let n = u.len();
    debug_assert!(cls.len() == n && ref_rank.len() == n && flags.len() == n && mapped.len() == n);
    let mut what = vec![NONE; n];
    let mut plate = vec![-1i8; n];
    if n < 3 {
        return (what, plate);
    }
    let u: Vec<[f64; 2]> = u.iter().map(|&v| unit(v)).collect();
    let rank: Vec<u16> = (0..n).map(|i| ((cls[i] as u16) << 8) | ref_rank[i] as u16).collect();
    let out: Vec<bool> = flags.iter().map(|&f| f & OUTBOUND != 0).collect();
    let has = |i: usize, f: u8| flags[i] & f != 0;
    let coming: Vec<usize> = (0..n).filter(|&i| !out[i]).collect();
    let zone = coming.iter().all(|&i| has(i, ZONE30));     // a 30 zone's junction: no 306 (the module doc)
    // 1. the map's signs
    let mut rem = Vec::with_capacity(n);
    for i in 0..n {
        if !out[i] && mapped[i] >= 0 {
            what[i] = if mapped[i] > 0 { Z206 } else { Z205 };
        } else {
            rem.push(i);
        }
    }
    let open: Vec<usize> = rem.iter().copied().filter(|&i| !out[i]).collect();
    if (0..n).filter(|&i| has(i, RING)).count() >= 2 {
        for &i in &open {
            if !has(i, RING) {
                what[i] = Z205;
            }
        }
        return (what, plate);
    }
    let on_stretch: Vec<usize> = rem.iter().copied().filter(|&i| has(i, STRETCH)).collect();
    let by_stretch = if on_stretch.len() >= 2 { best_pair(&u, &rank, &out, &on_stretch) } else { Vec::new() };
    let pair: Vec<usize> = if rem.len() == 2 && !open.is_empty() {
        rem.clone()             // the two the map left (of n >= 3; a road through's OUTBOUND arm counts, as in 2 and 4)
    } else if by_stretch.len() == 2 {
        by_stretch              // 2. a Vorfahrtstraße
    } else if !coming.iter().any(|&i| mapped[i] >= 0)
        && (zone || coming.iter().all(|&i| cls[i] <= RESIDENTIAL || (cls[i] == UNCLASSIFIED && urban))) {
        // 3. right before left (none where the map departs from it on an arm coming in: signed on both roads)
        for &i in &open {
            what[i] = RVL;
        }
        return (what, plate);
    } else {
        best_pair(&u, &rank, &out, &rem)    // 4. the roads' rank (one arm coming in alone: everyone yields to it)
    };
    let sign = if !zone && !pair.is_empty() && pair.iter().all(|&i| has(i, STRETCH)) { Z306 } else { Z301 };
    for &i in &open {
        what[i] = if pair.contains(&i) { sign } else { Z205 };
    }
    // the 1002 plates of a bent Vorfahrtstraße: against the straightest pair of the arms with one coming in (the
    // pairs a priority pair can be)
    if sign == Z306 && pair.len() == 2 && n <= PLATE_ARMS_MAX {
        let mut straightest = f64::INFINITY;
        for i in 0..n {
            for j in i + 1..n {
                if !(out[i] && out[j]) {
                    straightest = straightest.min(dot(u[i], u[j]));
                }
            }
        }
        if dot(u[pair[0]], u[pair[1]]) > straightest + STRAIGHT_SLACK {
            for i in 0..n {
                if matches!(what[i], Z205 | Z206 | Z306) {
                    plate[i] = plate_of(&u, i, (pair[0], pair[1]));
                }
            }
        }
    }
    (what, plate)
}

/// de_stretch(cls u8 (n,), ref_rank u8 (n,), prio u8 (n,)) -> bool (n,): stretch for each road.
#[pyfunction]
pub fn de_stretch<'py>(py: Python<'py>, cls: PyReadonlyArray1<'py, u8>, ref_rank: PyReadonlyArray1<'py, u8>,
                       prio: PyReadonlyArray1<'py, u8>) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (c, r, p) = (cls.as_array(), ref_rank.as_array(), prio.as_array());
    if r.len() != c.len() || p.len() != c.len() {
        return Err(PyValueError::new_err("de_stretch: array sizes differ"));
    }
    let v: Vec<bool> = (0..c.len()).map(|i| stretch(c[i], r[i], p[i])).collect();
    Ok(v.into_pyarray(py))
}

/// de_junction(u f64 (n, 2), cls u8 (n,), ref_rank u8 (n,), flags u8 (n,), mapped i8 (n,), urban) -> (what u8 (n,),
/// plate i8 (n,)): junction for one junction's arms, in their order.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn de_junction<'py>(py: Python<'py>, u: PyReadonlyArray2<'py, f64>, cls: PyReadonlyArray1<'py, u8>,
                        ref_rank: PyReadonlyArray1<'py, u8>, flags: PyReadonlyArray1<'py, u8>,
                        mapped: PyReadonlyArray1<'py, i8>, urban: bool)
                        -> PyResult<(Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<i8>>)> {
    let ua = u.as_array();
    let n = ua.nrows();
    if ua.ncols() != 2 || cls.as_array().len() != n || ref_rank.as_array().len() != n || flags.as_array().len() != n
        || mapped.as_array().len() != n
    {
        return Err(PyValueError::new_err("de_junction: u must be (n, 2) and the other arrays (n,)"));
    }
    let uv: Vec<[f64; 2]> = (0..n).map(|i| [ua[[i, 0]], ua[[i, 1]]]).collect();
    let (what, plate) = junction(&uv, &cls.as_array().to_vec(), &ref_rank.as_array().to_vec(),
                                 &flags.as_array().to_vec(), &mapped.as_array().to_vec(), urban);
    Ok((what.into_pyarray(py), plate.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    const RES: u8 = 1;
    const UNC: u8 = 2;
    const TER: u8 = 3;
    const SEC: u8 = 4;
    const PRI: u8 = 5;
    const E: [f64; 2] = [1.0, 0.0];
    const W: [f64; 2] = [-1.0, 0.0];
    const N: [f64; 2] = [0.0, 1.0];
    const S: [f64; 2] = [0.0, -1.0];

    /// An arm: (direction away from the junction, class, ref rank, flags, mapped).
    type Arm = ([f64; 2], u8, u8, u8, i8);

    fn arm(u: [f64; 2], cls: u8) -> Arm {
        (u, cls, 0, 0, -1)
    }

    fn with(a: Arm, flags: u8) -> Arm {
        (a.0, a.1, a.2, a.3 | flags, a.4)
    }

    fn unit(x: f64, y: f64) -> [f64; 2] {
        let l = x.dhypot(y);
        [x / l, y / l]
    }

    fn run(arms: &[Arm], urban: bool) -> (Vec<u8>, Vec<i8>) {
        let u: Vec<[f64; 2]> = arms.iter().map(|a| a.0).collect();
        let cls: Vec<u8> = arms.iter().map(|a| a.1).collect();
        let rr: Vec<u8> = arms.iter().map(|a| a.2).collect();
        let fl: Vec<u8> = arms.iter().map(|a| a.3).collect();
        let mp: Vec<i8> = arms.iter().map(|a| a.4).collect();
        junction(&u, &cls, &rr, &fl, &mp, urban)
    }

    #[test]
    fn stretch_rules() {
        assert!(stretch(SEC, 3, 0));            // a B road, secondary
        assert!(stretch(TER, 0, 0));            // a tertiary without a ref (a town's main road: Barbing's
                                                // Regensburger Straße)
        assert!(stretch(TER, 1, 0));            // a tertiary Kreisstraße
        assert!(stretch(RES, 0, 1));            // priority_road=designated
        assert!(stretch(RES, 0, 3));            // priority_road=yes_unposted
        assert!(!stretch(PRI, 0, 2));           // priority_road=end
        assert!(!stretch(TER, 2, 2));
        assert!(stretch(6, 0, 0));              // trunk
        assert!(!stretch(7, 0, 0));             // motorway: no junctions at grade
        assert!(!stretch(UNC, 0, 0));
    }

    #[test]
    fn side_road_gives_way_to_a_stretch() {
        let t = [with(arm(E, SEC), STRETCH), with(arm(W, SEC), STRETCH), arm(S, RES)];
        for urban in [true, false] {
            assert_eq!(run(&t, urban), (vec![Z306, Z306, Z205], vec![-1, -1, -1]));
        }
    }

    #[test]
    fn two_residentials_are_right_before_left() {
        let x = [arm(E, RES), arm(N, RES), arm(W, RES), arm(S, 0)];     // a living street the fourth
        assert_eq!(run(&x, true), (vec![RVL; 4], vec![-1; 4]));
        // unclassified is residential-type only in town
        let x = [arm(E, RES), arm(N, UNC), arm(W, RES), arm(S, UNC)];
        assert_eq!(run(&x, true).0, vec![RVL; 4]);
        assert_eq!(run(&x, false).0, vec![Z205, Z301, Z205, Z301]);
    }

    #[test]
    fn zone30_overrides_class() {
        let x = [with(arm(E, UNC), ZONE30), with(arm(N, UNC), ZONE30), with(arm(W, UNC), ZONE30),
                 with(arm(S, UNC), ZONE30)];
        assert_eq!(run(&x, false), (vec![RVL; 4], vec![-1; 4]));
        // one arm outside the zone: the class decides (rural unclassified, ranked: E-W and N-S tie, the arms' order)
        let x = [with(arm(E, UNC), ZONE30), with(arm(N, UNC), ZONE30), with(arm(W, UNC), ZONE30), arm(S, UNC)];
        assert_eq!(run(&x, false).0, vec![Z301, Z205, Z301, Z205]);
    }

    #[test]
    fn rural_single_junction_gets_301() {
        let t = [arm(E, TER), arm(W, TER), arm(S, UNC)];
        assert_eq!(run(&t, false), (vec![Z301, Z301, Z205], vec![-1, -1, -1]));
        assert_eq!(run(&t, true).0, vec![Z301, Z301, Z205]);
    }

    #[test]
    fn equal_secondaries_never_rvl() {
        // N-S a little bent (u.u -0.995), E-W straight (-1): E-W is the pair
        let x = [arm(E, SEC), arm(N, SEC), arm(W, SEC), arm(unit(0.1, -1.0), SEC)];
        for urban in [true, false] {
            let (what, plate) = run(&x, urban);
            assert_eq!(what, vec![Z301, Z205, Z301, Z205]);
            assert_eq!(what.iter().filter(|&&w| w == Z301).count(), 2);
            assert_eq!(plate, vec![-1; 4]);
        }
    }

    #[test]
    fn ref_rank_breaks_a_class_tie() {
        // three tertiaries (no stretch flags given): S's ref ranks first, W and E tie for second; E is bent away
        // from S a little more than W (S.E 0.0995 against S.W 0), so W-S is the pair
        let t = [arm(W, TER), arm(unit(1.0, -0.1), TER), (S, TER, 1, 0, -1)];
        assert_eq!(run(&t, false).0, vec![Z301, Z205, Z301]);
        // a Bundesstraße ranks over a Kreisstraße of the same class: S pairs with the first of the tied W and E
        let t = [(W, SEC, 1, 0, -1), (E, SEC, 1, 0, -1), (S, SEC, 3, 0, -1)];
        assert_eq!(run(&t, false).0, vec![Z301, Z205, Z301]);
    }

    #[test]
    fn mapped_stop_wins() {
        let t = [arm(E, RES), arm(W, RES), (S, RES, 0, 0, 1)];
        assert_eq!(run(&t, true), (vec![Z301, Z301, Z206], vec![-1, -1, -1]));
        // a mapped give-way, and the remaining pair both on a stretch: 306
        let t = [with(arm(E, SEC), STRETCH), with(arm(W, SEC), STRETCH), (S, SEC, 0, STRETCH, 0)];
        assert_eq!(run(&t, true).0, vec![Z306, Z306, Z205]);
    }

    #[test]
    fn rvl_after_a_mapped_sign() {
        // a residential crossroads with a mapped stop on S: no right before left among the other three (signed on both
        // roads, zu den Zeichen 205 und 206 Rn. 6): W-E, the straightest, is the pair by rank, N gives way
        let x = [arm(W, RES), arm(E, RES), arm(N, RES), (S, RES, 0, 0, 1)];
        assert_eq!(run(&x, true), (vec![Z301, Z301, Z205, Z206], vec![-1; 4]));
        assert_eq!(run(&x, false).0, vec![Z301, Z301, Z205, Z206]);
        // a tertiary stem with a stop: the same
        let x = [arm(W, RES), arm(E, RES), arm(N, RES), (S, TER, 0, 0, 1)];
        assert_eq!(run(&x, true).0, vec![Z301, Z301, Z205, Z206]);
        // a mapped give-way on N: W-E the pair, S gives way; on W: N-S the pair, E gives way
        let x = [arm(W, RES), arm(E, RES), (N, RES, 0, 0, 0), arm(S, RES)];
        assert_eq!(run(&x, true).0, vec![Z301, Z301, Z205, Z205]);
        let x = [(W, RES, 0, 0, 0), arm(E, RES), arm(N, RES), arm(S, RES)];
        assert_eq!(run(&x, true).0, vec![Z205, Z205, Z301, Z301]);
        // a mapped sign on an arm leaving is nobody's: right before left stays
        let x = [arm(W, RES), arm(E, RES), arm(N, RES), (S, RES, 0, OUTBOUND, 1)];
        assert_eq!(run(&x, true).0, vec![RVL, RVL, RVL, NONE]);
    }

    #[test]
    fn zone_crossroads_with_a_mapped_give_way() {
        // a 30 zone's crossroads, a mapped 205 on S: 301 for W-E (zu § 45 Abs. 1 bis 1e Rn. 41), N 205; no plates
        let z = |a: Arm| with(a, ZONE30);
        let x = [z(arm(W, RES)), z(arm(E, RES)), z(arm(N, RES)), z((S, RES, 0, 0, 0))];
        assert_eq!(run(&x, true), (vec![Z301, Z301, Z205, Z205], vec![-1; 4]));
        // nothing mapped: right before left, as before
        let x = [z(arm(W, RES)), z(arm(E, RES)), z(arm(N, RES)), z(arm(S, RES))];
        assert_eq!(run(&x, true).0, vec![RVL; 4]);
        // W-E stretches by class inside the zone: 301, never 306, and no plates even where they bend (W-N)
        let x = [z(with(arm(W, SEC), STRETCH)), z(with(arm(E, SEC), STRETCH)), z(arm(N, RES)), z((S, RES, 0, 0, 1))];
        assert_eq!(run(&x, true), (vec![Z301, Z301, Z205, Z206], vec![-1; 4]));
        let x = [z(with(arm(W, SEC), STRETCH)), z(with(arm(N, SEC), STRETCH)), z(arm(E, RES)), z((S, RES, 0, 0, 1))];
        assert_eq!(run(&x, true), (vec![Z301, Z301, Z205, Z206], vec![-1; 4]));
        // the same outside a zone: 306 (and the bent one's plates)
        let x = [with(arm(W, SEC), STRETCH), with(arm(E, SEC), STRETCH), arm(N, RES), (S, RES, 0, 0, 1)];
        assert_eq!(run(&x, true).0, vec![Z306, Z306, Z205, Z206]);
        // a stretch pair in a zone with nothing mapped: 301 too
        let t = [z(with(arm(W, SEC), STRETCH)), z(with(arm(E, SEC), STRETCH)), z(arm(S, RES))];
        assert_eq!(run(&t, true).0, vec![Z301, Z301, Z205]);
    }

    #[test]
    fn zone_t_with_a_mapped_give_way() {
        // a 30 zone's T, a mapped 205 on its stem S: the two arms left are the pair (step 1), 301
        let z = |a: Arm| with(a, ZONE30);
        let t = [z(arm(W, RES)), z(arm(E, RES)), z((S, RES, 0, 0, 0))];
        assert_eq!(run(&t, true), (vec![Z301, Z301, Z205], vec![-1; 3]));
        // a stretch by class through it: 301 still
        let t = [z(with(arm(W, TER), STRETCH)), z(with(arm(E, TER), STRETCH)), z((S, RES, 0, 0, 0))];
        assert_eq!(run(&t, true).0, vec![Z301, Z301, Z205]);
    }

    #[test]
    fn bent_priority_has_plates() {
        // W-S the stretch, bent (u.u 0 against W-E's -1); E residential. W: S right, E ahead, the pair the approach
        // and the right: RA:DR (4). S: W left, E right, the pair the approach and the left: LR:DL (0). E: S left,
        // W ahead, the pair ahead and left: LA:LA (3).
        let t = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), arm(E, RES)];
        assert_eq!(run(&t, true), (vec![Z306, Z306, Z205], vec![4, 0, 3]));
        // the mirror, W-N: W LA:DL (2), N LR:DR (1), E RA:RA (5)
        let t = [with(arm(W, SEC), STRETCH), with(arm(N, SEC), STRETCH), arm(E, RES)];
        assert_eq!(run(&t, true), (vec![Z306, Z306, Z205], vec![2, 1, 5]));
        // a four-way, W-S: W LAR:DR (7), S LAR:DL (6), E LAR:LA (8), N LAR:AR (9)
        let x = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), arm(E, RES), arm(N, RES)];
        assert_eq!(run(&x, false), (vec![Z306, Z306, Z205, Z205], vec![7, 6, 8, 9]));
        // a straight stretch has none
        let x = [with(arm(W, SEC), STRETCH), with(arm(E, SEC), STRETCH), arm(S, RES), arm(N, RES)];
        assert_eq!(run(&x, false).1, vec![-1; 4]);
    }

    #[test]
    fn bent_301_has_no_plates() {
        // a rural T, no stretch: the two tertiaries W-S are the pair by rank, bent; 301 has no 1002 plate
        let t = [arm(W, TER), arm(S, TER), arm(E, UNC)];
        assert_eq!(run(&t, false), (vec![Z301, Z301, Z205], vec![-1, -1, -1]));
        // the same with a mapped stop on E: the remaining pair is not both on a stretch, 301, no plates
        let t = [arm(W, TER), arm(S, TER), (E, UNC, 0, 0, 1)];
        assert_eq!(run(&t, false), (vec![Z301, Z301, Z206], vec![-1, -1, -1]));
    }

    #[test]
    fn mapped_stop_on_bent_stretch_has_plate() {
        let t = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), (E, RES, 0, 0, 1)];
        assert_eq!(run(&t, true), (vec![Z306, Z306, Z206], vec![4, 0, 3]));
    }

    #[test]
    fn plates_need_a_clear_picture() {
        // a skewed cross, the stretch W to 30 degrees right of E: bent by u.u (-0.866 against N-S's -1), but every
        // view sees it straight on (W: LAR:DA) or across (S: LAR:LR), which PATTERNS has not: no plates
        let x = [with(arm(W, SEC), STRETCH), with(arm(unit(0.866, -0.5), SEC), STRETCH), arm(S, RES), arm(N, RES)];
        assert_eq!(run(&x, true), (vec![Z306, Z306, Z205, Z205], vec![-1; 4]));
        // a second side road 15 degrees off S: shares a slot from W and E, is behind from S, and S behind from it
        let x = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), arm(E, RES), arm(unit(0.26, -0.966), RES)];
        assert_eq!(run(&x, true), (vec![Z306, Z306, Z205, Z205], vec![-1; 4]));
        // five arms: no plate
        let x = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), arm(E, RES), arm(N, RES),
                 arm(unit(1.0, 1.0), RES)];
        assert_eq!(run(&x, true), (vec![Z306, Z306, Z205, Z205, Z205], vec![-1; 5]));
    }

    #[test]
    fn outbound_gets_nothing() {
        // a one-way side road leaving the junction
        let t = [with(arm(E, SEC), STRETCH), with(arm(W, SEC), STRETCH), with(arm(S, RES), OUTBOUND)];
        assert_eq!(run(&t, true), (vec![Z306, Z306, NONE], vec![-1, -1, -1]));
        // a mapped stop on it is not for anyone coming in
        let t = [arm(E, TER), arm(W, TER), (S, RES, 0, OUTBOUND, 1)];
        assert_eq!(run(&t, false).0, vec![Z301, Z301, NONE]);
        // residential, an outbound stem and nothing mapped: right before left, no pair of the two others
        let t = [arm(E, RES), arm(W, RES), with(arm(S, RES), OUTBOUND)];
        assert_eq!(run(&t, true).0, vec![RVL, RVL, NONE]);
    }

    #[test]
    fn one_way_road_through() {
        // a town's one-way tertiary W in, E out; the residential S gives way to it
        let t = [arm(W, TER), with(arm(E, TER), OUTBOUND), arm(S, RES)];
        assert_eq!(run(&t, true), (vec![Z301, NONE, Z205], vec![-1, -1, -1]));
        // a divided stretch whose two carriageways meet the junction from the west: the inbound one pairs with E
        // (its u.u -0.99875, the outbound twin's -0.9998: within STRAIGHT_SLACK, the fewer outbound arms first)
        let x = [with(arm(unit(-1.0, 0.05), SEC), STRETCH), with(arm(unit(-1.0, -0.02), SEC), STRETCH | OUTBOUND),
                 with(arm(E, SEC), STRETCH), arm(S, RES)];
        assert_eq!(run(&x, true), (vec![Z306, NONE, Z306, Z205], vec![-1; 4]));
    }

    #[test]
    fn stretch_before_rvl_and_rank() {
        // residential roads in town, W-E mapped priority_road=designated: a Vorfahrtstraße, not right before left
        let x = [with(arm(W, RES), STRETCH), with(arm(E, RES), STRETCH), arm(N, RES), arm(S, RES)];
        assert_eq!(run(&x, true).0, vec![Z306, Z306, Z205, Z205]);
        // two tertiary Kreisstraßen W-E and a secondary S mapped priority_road=end: the stretch, not the rank
        let t = [(W, TER, 1, STRETCH, -1), (E, TER, 1, STRETCH, -1), arm(S, SEC)];
        assert_eq!(run(&t, false).0, vec![Z306, Z306, Z205]);
        // two stretches crossing: the higher (primary N-S) goes through, the secondary W-E gives way
        let x = [with(arm(W, SEC), STRETCH), with(arm(N, PRI), STRETCH), with(arm(E, SEC), STRETCH),
                 with(arm(S, PRI), STRETCH)];
        assert_eq!(run(&x, false), (vec![Z205, Z306, Z205, Z306], vec![-1; 4]));
    }

    #[test]
    fn bent_counts_a_one_way_leaving() {
        // the stretch W-S bends where E, straight on from W, is a one-way leaving: W-E is the straightest pair (an arm
        // coming in, W), so the stretch is bent and its arms get plates; E itself none. W: RA:DR (4), S: LR:DL (0).
        let t = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), with(arm(E, RES), OUTBOUND)];
        assert_eq!(run(&t, true), (vec![Z306, Z306, NONE], vec![4, 0, -1]));
        // E two-way: the same plates, and E's own (bent_priority_has_plates)
        let t = [with(arm(W, SEC), STRETCH), with(arm(S, SEC), STRETCH), arm(E, RES)];
        assert_eq!(run(&t, true).1, vec![4, 0, 3]);
        // two one-ways leaving straight across (N-S, u.u -1) are no pair through: the stretch W-E (u.u -0.928) stays
        // the straightest, not bent
        let x = [with(arm(W, SEC), STRETCH), with(arm(unit(1.0, 0.4), SEC), STRETCH), with(arm(N, RES), OUTBOUND),
                 with(arm(S, RES), OUTBOUND)];
        assert_eq!(run(&x, true), (vec![Z306, Z306, NONE, NONE], vec![-1; 4]));
    }

    #[test]
    fn one_way_through_with_one_mapped_side_road() {
        // a town's one-way tertiary W in, E out; N mapped give-way, S unmapped: the road through keeps the priority
        let x = [arm(W, TER), with(arm(E, TER), OUTBOUND), (N, RES, 0, 0, 0), arm(S, RES)];
        assert_eq!(run(&x, true), (vec![Z301, NONE, Z205, Z205], vec![-1; 4]));
    }

    #[test]
    fn divided_stretch_with_one_mapped_side_road() {
        // a divided stretch's carriageway W in, E out; a mapped stop on N, the median connector S unmapped
        let x = [with(arm(W, SEC), STRETCH), with(arm(E, SEC), STRETCH | OUTBOUND), (N, RES, 0, 0, 1), arm(S, RES)];
        assert_eq!(run(&x, false), (vec![Z306, NONE, Z206, Z205], vec![-1; 4]));
    }

    #[test]
    fn mapped_side_roads_leave_a_one_way_through() {
        // both side roads mapped stops, the residential road through one-way (W in, E out): the two remaining arms
        // (OUTBOUND counted, as steps 2 and 4 count it) are the pair, not right before left for W alone
        let x = [arm(W, RES), with(arm(E, RES), OUTBOUND), (N, RES, 0, 0, 1), (S, RES, 0, 0, 1)];
        assert_eq!(run(&x, true), (vec![Z301, NONE, Z206, Z206], vec![-1; 4]));
    }

    #[test]
    fn outbound_never_outranks_the_arms_coming_in() {
        // a fork: the stretch W comes in, two primary one-ways leave; W is the priority road, never 205 to nobody
        let t = [with(arm(W, SEC), STRETCH), with(arm(unit(1.0, 0.7), PRI), STRETCH | OUTBOUND),
                 with(arm(unit(1.0, -0.7), PRI), STRETCH | OUTBOUND)];
        assert_eq!(run(&t, true), (vec![Z306, NONE, NONE], vec![-1; 3]));
        // two stretch one-ways leaving straight across (N-S, the straightest) are no pair: W pairs with one of them
        let t = [with(arm(W, SEC), STRETCH), with(arm(N, SEC), STRETCH | OUTBOUND),
                 with(arm(S, SEC), STRETCH | OUTBOUND)];
        assert_eq!(run(&t, true), (vec![Z306, NONE, NONE], vec![-1; 3]));
        // two tertiaries and a primary one-way leaving S: the primary ranks as a tertiary (the best arm coming in),
        // so W-E, the straightest, is the pair, not S with whichever tertiary comes first
        let t = [arm(W, TER), arm(E, TER), with(arm(S, PRI), OUTBOUND)];
        assert_eq!(run(&t, false), (vec![Z301, Z301, NONE], vec![-1; 3]));
        // a secondary stretch W goes on as a primary one-way E: W pairs with it (capped, not dropped: else W and the
        // residential S would be the pair), S gives way
        let t = [with(arm(W, SEC), STRETCH), with(arm(E, PRI), STRETCH | OUTBOUND), arm(S, RES)];
        assert_eq!(run(&t, true), (vec![Z306, NONE, Z205], vec![-1; 3]));
    }

    #[test]
    fn stretch_end_falls_to_rank() {
        // the secondary W (a stretch) goes on as the unclassified E: no stretch pair, the rank decides
        let t = [with(arm(W, SEC), STRETCH), arm(E, UNC), arm(S, RES)];
        assert_eq!(run(&t, true), (vec![Z301, Z301, Z205], vec![-1, -1, -1]));
    }

    #[test]
    fn ring_entries_give_way() {
        let x = [with(arm(W, TER), RING), with(arm(N, TER), RING | OUTBOUND), arm(S, RES), with(arm(E, RES), OUTBOUND)];
        assert_eq!(run(&x, true), (vec![NONE, NONE, Z205, NONE], vec![-1; 4]));
    }

    #[test]
    fn degenerate() {
        assert_eq!(run(&[], true), (vec![], vec![]));
        assert_eq!(run(&[arm(E, SEC)], true), (vec![NONE], vec![-1]));
        assert_eq!(run(&[arm(E, SEC), (W, SEC, 0, 0, 1)], true), (vec![NONE, NONE], vec![-1, -1]));  // no junction
        // every arm mapped
        let t = [(E, RES, 0, 0, 0), (W, RES, 0, 0, 1), (S, RES, 0, 0, 0)];
        assert_eq!(run(&t, true), (vec![Z205, Z206, Z205], vec![-1, -1, -1]));
        // the arm coming in mapped, the two left both leaving (a bent stretch): no pair, so no plate on the 206
        let t = [(W, SEC, 0, 0, 1), with(arm(N, SEC), STRETCH | OUTBOUND), with(arm(E, SEC), STRETCH | OUTBOUND)];
        assert_eq!(run(&t, true), (vec![Z206, NONE, NONE], vec![-1; 3]));
        // one arm left unmapped: it alone is the priority road
        let t = [arm(W, TER), (E, TER, 0, 0, 0), (S, RES, 0, 0, 0)];
        assert_eq!(run(&t, false).0, vec![Z301, Z205, Z205]);
        // not unit vectors, a zero one, a NaN and an infinite one: still a code each, no panic
        let t = [arm([-3.0, 0.0], SEC), arm([0.0, 0.0], SEC), arm([0.0, -2.0], RES)];
        assert_eq!(run(&t, false).0, vec![Z301, Z301, Z205]);
        let t = [arm([f64::NAN, 0.0], SEC), arm([f64::INFINITY, 1.0], SEC), arm([0.0, -2.0], SEC)];
        assert_eq!(run(&t, false).0.len(), 3);
    }
}
