//! Where each German Autobahn board stands along its junction's approach (spec C): s is metres upstream of the junction
//! node, the reference point ref_s = 125 m before the painted gore (Hessen LWBA 4.2.1, "im Regelfall"). The chain:
//! Anschlussstelle 1000 m Zeichen 448, 500 m Zeichen 449, 300/200/100 m Zeichen 450, Wegweiser at 0 m, Zeichen 333 in
//! the gore; Kreuz / Dreieck 2000 m Zeichen 448, 1000 m and 500 m Zeichen 449, Zeichen 450, Wegweiser at the split,
//! fork boards on the ramp, no Zeichen 333 (StVO Anlage 3; the pre-2009 StVO § 42 Abs. 8 distances; VzKat 2017; BASt
//! dWiSta 2022 Abb. 14; Hessen Regelpläne 1/2/5/6). Overhead on more than two lanes and at every Kreuz / Dreieck (RWBA
//! 2000 4.5, the user's decision); beside the road on two lanes.
use super::content::{Content, Junction};


pub(crate) const REFERENCE_M: f64 = 125.0;   // the reference point before the painted gore
pub(crate) const AFTER_EXIT_M: f64 = 100.0;  // dense junctions: boards at least this far after the previous exit
pub(crate) const MIN_GAP_M: f64 = 150.0;     // between two boards of one chain
pub(crate) const ROUND_M: f64 = 50.0;        // a shortened distance is printed rounded down to this
pub(crate) const COUNTDOWN_GAP_M: f64 = 25.0; // a countdown board this far from any other board of the chain

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Board {
    Announce,      // Zeichen 448
    Advance(u32),  // Zeichen 449 at its nominal distance (1000 / 500)
    Countdown(u8), // Zeichen 450 with 3 / 2 / 1 bars
    Direction,     // Wegweiser (Zeichen 332 type) at the reference point
    Gore,          // Zeichen 333 "Ausfahrt"
    Fork,          // Gabelungswegweiser on a Kreuz ramp
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Mount {
    Portal,
    Cantilever,
    Side,
    Post,
    GoreSign,
    Ground,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Station {
    pub board: Board,
    pub s: f64,                 // metres upstream of the junction node (NaN: on the ramp, at the fork)
    pub printed_m: Option<u32>, // the distance printed on an announcement / advance board
    pub mount: Mount,
}

/// Samples along the approach: s (metres upstream of the junction node, ascending) and the main carriageway's lanes.
pub(crate) struct Profile<'a> {
    pub s: &'a [f64],
    pub lanes: &'a [f64],
}

fn lanes_at(p: &Profile, s: f64) -> f64 {
    let mut best = (f64::INFINITY, 0.0);
    for (i, &x) in p.s.iter().enumerate() {
        let d = (x - s).abs();
        if d < best.0 {
            best = (d, p.lanes.get(i).copied().unwrap_or(0.0));
        }
    }
    best.1
}

fn skip_at(s: f64, skips: &[(f64, f64)]) -> Option<(f64, f64)> {
    skips.iter().copied().find(|&(a, b)| s > a.min(b) && s < a.max(b)).map(|(a, b)| (a.min(b), a.max(b)))
}

/// The positions of the big boards (nominal s, outermost first) under `upper` (the dense bound), each MIN_GAP_M below
/// the one outside it, off the skips (decks, tunnels: moved upstream when that stays under the bound, else
/// downstream), the innermost MIN_GAP_M outside the reference point; None when they do not fit.
fn fit(nominal: &[f64], limit: f64, ref_s: f64, skips: &[(f64, f64)]) -> Option<Vec<f64>> {
    let mut out = Vec::with_capacity(nominal.len());
    let mut upper = limit;
    for &n in nominal {
        let mut s = n.min(upper);
        if let Some((a, b)) = skip_at(s, skips) {
            s = if b <= upper { b } else { a };
        }
        if s < ref_s + MIN_GAP_M || skip_at(s, skips).is_some() {
            return None;
        }
        out.push(s);
        upper = s - MIN_GAP_M;
    }
    Some(out)
}

/// The stations of one junction's chain, outermost first (the fork, on the ramp, last).
pub(crate) fn stations(kind: Junction, has: &Content, ref_s: f64, gore_s: f64, prev_exit_s: Option<f64>,
                       profile: &Profile, skips: &[(f64, f64)]) -> Vec<Station> {
    stations_follow(kind, has, ref_s, gore_s, prev_exit_s, false, profile, skips)
}

/// stations, with follow: a later exit of the same junction on this carriageway (a cloverleaf's second exit, whose
/// prev_exit_s is the first): the junction's chain stands before its first exit, so this one gets only its 500 m
/// board (under the first exit) and its direction sign, no announcement or countdown of its own.
#[allow(clippy::too_many_arguments)]
pub(crate) fn stations_follow(kind: Junction, has: &Content, ref_s: f64, gore_s: f64, prev_exit_s: Option<f64>,
                              follow: bool, profile: &Profile, skips: &[(f64, f64)]) -> Vec<Station> {
    let kreuz = matches!(kind, Junction::Kreuz | Junction::Dreieck);
    // the bound: the previous junction's exit, and the approach's own end (a walk that stopped early: no station
    // past its last sample)
    let end = profile.s.iter().copied().filter(|v| v.is_finite()).fold(f64::NEG_INFINITY, f64::max);
    let end = if end.is_finite() { end } else { f64::INFINITY };
    let limit = prev_exit_s.map(|p| p - AFTER_EXIT_M).unwrap_or(f64::INFINITY).min(end);
    let advance = has.through.is_some() || !has.exits.is_empty();
    // the big boards by priority: the announcement, the 500 m advance board, a Kreuz's 1000 m one
    let mut wanted: Vec<(Board, u32)> = Vec::new();
    if has.name.is_some() && !follow {
        wanted.push((Board::Announce, if kreuz { 2000 } else { 1000 }));
    }
    if advance {
        wanted.push((Board::Advance(500), 500));
        // a Kreuz's 1000 m board only where it fits unsqueezed: in a dense place its 500 m board does its job
        if kreuz && !follow && ref_s + 1000.0 <= limit {
            wanted.push((Board::Advance(1000), 1000));
        }
    }
    let mut kept: Vec<(Board, u32)> = Vec::new();
    let mut placed: Vec<f64> = Vec::new();
    for w in &wanted {
        let mut trial = kept.clone();
        trial.push(*w);
        trial.sort_by(|a, b| b.1.cmp(&a.1));
        let nominal: Vec<f64> = trial.iter().map(|(_, d)| ref_s + *d as f64).collect();
        if let Some(pos) = fit(&nominal, limit, ref_s, skips) {
            kept = trial;
            placed = pos;
        }
    }
    let overhead = |s: f64| kreuz || lanes_at(profile, s) > 2.0;
    let mut out: Vec<Station> = Vec::new();
    for ((board, d), s) in kept.iter().zip(placed.iter()) {
        let nominal = ref_s + *d as f64;
        let printed = if *s >= nominal - 0.5 { *d } else { (((s - ref_s) / ROUND_M).floor() * ROUND_M).max(0.0) as u32 };
        let mount = match board {
            Board::Announce if kreuz => Mount::Portal,
            _ if !overhead(*s) => Mount::Side,
            Board::Announce => Mount::Cantilever,
            _ => Mount::Portal,
        };
        out.push(Station { board: *board, s: *s, printed_m: Some(printed), mount });
    }
    let boards_at: Vec<f64> = placed.iter().copied().chain(advance.then_some(ref_s)).collect();
    for (bars, d) in [(3u8, 300.0), (2, 200.0), (1, 100.0)] {
        if follow {
            break;
        }
        let s = ref_s + d;
        // a countdown board never stands in a gantry or another board (COUNTDOWN_GAP_M)
        if s <= limit && skip_at(s, skips).is_none() && boards_at.iter().all(|b| (b - s).abs() >= COUNTDOWN_GAP_M) {
            out.push(Station { board: Board::Countdown(bars), s, printed_m: None, mount: Mount::Post });
        }
    }
    if advance {
        let inner = placed.last().copied().unwrap_or(f64::INFINITY) - MIN_GAP_M;
        let mut s = ref_s;
        if let Some((a, b)) = skip_at(s, skips) {
            s = if b <= inner { b } else { a };
        }
        if skip_at(s, skips).is_none() && s > gore_s {
            out.push(Station { board: Board::Direction, s, printed_m: None,
                               mount: if overhead(s) { Mount::Portal } else { Mount::Side } });
        }
    }
    if !kreuz {
        out.push(Station { board: Board::Gore, s: gore_s, printed_m: None, mount: Mount::GoreSign });
    }
    out.sort_by(|a, b| b.s.partial_cmp(&a.s).unwrap_or(std::cmp::Ordering::Equal));
    if kreuz && has.forks.len() >= 2 {
        out.push(Station { board: Board::Fork, s: f64::NAN, printed_m: None, mount: Mount::Ground });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::deautobahn::content::{Block, Entry};

    fn has(kind: Junction, name: bool, forks: bool) -> Content {
        let b = Block { entries: vec![Entry { text: "X".into(), ..Default::default() }], plates: vec![], lanes: vec![] };
        Content { kind, number: Some("1".into()), name: if name { Some("N".into()) } else { None },
                  through: Some(b.clone()), exits: vec![b.clone()],
                  forks: if forks { vec![b.clone(), b] } else { vec![] } }
    }

    fn lanes(n: f64) -> (Vec<f64>, Vec<f64>) {
        let s: Vec<f64> = (0..300).map(|i| i as f64 * 10.0).collect();
        let l = vec![n; s.len()];
        (s, l)
    }

    fn run(kind: Junction, c: &Content, n: f64, prev: Option<f64>, skips: &[(f64, f64)]) -> Vec<Station> {
        let (s, l) = lanes(n);
        stations(kind, c, 125.0, 0.0, prev, &Profile { s: &s, lanes: &l }, skips)
    }

    fn find(st: &[Station], b: Board) -> Option<&Station> {
        st.iter().find(|x| x.board == b)
    }

    #[test]
    fn exit_chain_three_lanes() {
        let st = run(Junction::Exit, &has(Junction::Exit, true, false), 3.0, None, &[]);
        let got: Vec<(Board, f64, Mount, Option<u32>)> = st.iter().map(|x| (x.board, x.s, x.mount, x.printed_m)).collect();
        assert_eq!(got, vec![
            (Board::Announce, 1125.0, Mount::Cantilever, Some(1000)),
            (Board::Advance(500), 625.0, Mount::Portal, Some(500)),
            (Board::Countdown(3), 425.0, Mount::Post, None),
            (Board::Countdown(2), 325.0, Mount::Post, None),
            (Board::Countdown(1), 225.0, Mount::Post, None),
            (Board::Direction, 125.0, Mount::Portal, None),
            (Board::Gore, 0.0, Mount::GoreSign, None),
        ]);
    }

    #[test]
    fn chain_fits_a_short_approach() {
        // the approach walk stopped 290 m out (an end, a bend, an ambiguous fork): no station past its last sample,
        // where they would all land on that sample (Codex review of #999); the chain squeezed as at a close junction
        let s: Vec<f64> = (0..30).map(|i| i as f64 * 10.0).collect();
        let l = vec![3.0; s.len()];
        let st = stations(Junction::Exit, &has(Junction::Exit, true, false), 125.0, 0.0, None,
                          &Profile { s: &s, lanes: &l }, &[]);
        assert!(st.iter().all(|x| !(x.s > 290.0)), "{:?}", st.iter().map(|x| (x.board, x.s)).collect::<Vec<_>>());
        let a = find(&st, Board::Announce).unwrap();
        assert_eq!((a.s, a.printed_m), (290.0, Some(150)));
        assert!(find(&st, Board::Countdown(3)).is_none() && find(&st, Board::Countdown(1)).is_some());
        assert!(find(&st, Board::Direction).is_some());
    }

    #[test]
    fn exit_chain_two_lanes_is_side() {
        let st = run(Junction::Exit, &has(Junction::Exit, true, false), 2.0, None, &[]);
        for b in [Board::Announce, Board::Advance(500), Board::Direction] {
            assert_eq!(find(&st, b).unwrap().mount, Mount::Side, "{b:?}");
        }
    }

    #[test]
    fn kreuz_chain_always_portal() {
        let st = run(Junction::Kreuz, &has(Junction::Kreuz, true, true), 2.0, None, &[]);
        let a = find(&st, Board::Announce).unwrap();
        assert_eq!((a.s, a.mount, a.printed_m), (2125.0, Mount::Portal, Some(2000)));
        assert_eq!(find(&st, Board::Advance(1000)).unwrap().s, 1125.0);
        assert_eq!(find(&st, Board::Advance(500)).unwrap().mount, Mount::Portal);
        assert_eq!(find(&st, Board::Direction).unwrap().mount, Mount::Portal);
        assert!(find(&st, Board::Gore).is_none());
        let fork = find(&st, Board::Fork).unwrap();
        assert!(fork.s.is_nan() && fork.mount == Mount::Ground);
    }

    #[test]
    fn dense_a93_kreuz() {
        // A93 southbound at Kreuz Regensburg: Kumpfmühl's exit leaves 618 m before the first Kreuz exit
        let (s, l) = lanes(2.0);
        let st = stations(Junction::Kreuz, &has(Junction::Kreuz, true, false), 150.0, 25.0, Some(618.0),
                          &Profile { s: &s, lanes: &l }, &[]);
        let a = find(&st, Board::Announce).unwrap();
        assert_eq!((a.s, a.printed_m), (518.0, Some(350)));
        let v = find(&st, Board::Advance(500)).unwrap();
        assert_eq!((v.s, v.printed_m), (368.0, Some(200)));
        assert!(find(&st, Board::Advance(1000)).is_none());
        assert_eq!(find(&st, Board::Direction).unwrap().s, 150.0);
        assert!(st.iter().all(|x| x.s.is_nan() || x.s <= 518.0));
    }

    #[test]
    fn dense_kreuz_drops_the_1000m_board() {
        // A93 southbound at Kreuz Regensburg with the gore 60 m down the ramp: the bound leaves room for three boards,
        // but a squeezed chain keeps the announcement and one advance board, as the real one does
        let (s, l) = lanes(2.0);
        let st = stations(Junction::Kreuz, &has(Junction::Kreuz, true, false), 65.0, -60.0, Some(618.0),
                          &Profile { s: &s, lanes: &l }, &[]);
        assert!(find(&st, Board::Advance(1000)).is_none());
        let a = find(&st, Board::Announce).unwrap();
        let v = find(&st, Board::Advance(500)).unwrap();
        assert_eq!((a.s, a.printed_m, v.s, v.printed_m), (518.0, Some(450), 368.0, Some(300)));
    }

    #[test]
    fn follow_exit_gets_no_chain_of_its_own() {
        // a cloverleaf's second exit of the same Kreuz on this carriageway, its first exit 450 m upstream: no second
        // announcement, no second countdown, the 500 m board squeezed under the first exit, the direction sign
        let (s, l) = lanes(2.0);
        let c = has(Junction::Kreuz, true, false);
        let st = stations_follow(Junction::Kreuz, &c, 65.0, -60.0, Some(450.0), true, &Profile { s: &s, lanes: &l }, &[]);
        assert!(find(&st, Board::Announce).is_none() && find(&st, Board::Advance(1000)).is_none());
        assert!(st.iter().all(|x| !matches!(x.board, Board::Countdown(_))));
        let v = find(&st, Board::Advance(500)).unwrap();
        assert_eq!((v.s, v.printed_m), (350.0, Some(250)));
        assert!(find(&st, Board::Direction).is_some());
    }

    #[test]
    fn countdown_keeps_off_a_big_board() {
        // the dense A93 case: the 500 m board squeezed to s = 368 m, the 200 m countdown at 350 m: one of them in the
        // other; the countdown gives way
        let (s, l) = lanes(2.0);
        let st = stations(Junction::Kreuz, &has(Junction::Kreuz, true, false), 150.0, 25.0, Some(618.0),
                          &Profile { s: &s, lanes: &l }, &[]);
        let boards: Vec<f64> = st.iter().filter(|x| !matches!(x.board, Board::Countdown(_))).map(|x| x.s).collect();
        for c in st.iter().filter(|x| matches!(x.board, Board::Countdown(_))) {
            assert!(boards.iter().all(|b| (b - c.s).abs() >= 25.0), "{:?} at {}", c.board, c.s);
        }
        assert!(find(&st, Board::Countdown(2)).is_none());
    }

    #[test]
    fn drop_within_150() {
        let st = run(Junction::Exit, &has(Junction::Exit, true, false), 3.0, Some(450.0), &[]);
        let a = find(&st, Board::Announce).unwrap();
        assert_eq!((a.s, a.printed_m), (350.0, Some(200)));
        assert!(find(&st, Board::Advance(500)).is_none());
        assert!(find(&st, Board::Countdown(3)).is_none() && find(&st, Board::Countdown(2)).is_some());
    }

    #[test]
    fn deck_moves_upstream() {
        let st = run(Junction::Exit, &has(Junction::Exit, true, false), 3.0, None, &[(1100.0, 1200.0)]);
        let a = find(&st, Board::Announce).unwrap();
        assert_eq!((a.s, a.printed_m), (1200.0, Some(1000)));
    }

    #[test]
    fn deck_moves_downstream_with_shorter_distance() {
        let st = run(Junction::Exit, &has(Junction::Exit, true, false), 3.0, Some(1300.0), &[(1100.0, 1250.0)]);
        let a = find(&st, Board::Announce).unwrap();
        assert_eq!((a.s, a.printed_m), (1100.0, Some(950)));
    }

    #[test]
    fn deck_and_dense_drops() {
        let st = run(Junction::Exit, &has(Junction::Exit, true, false), 3.0, Some(1300.0), &[(250.0, 1250.0)]);
        assert!(find(&st, Board::Announce).is_none());
        assert!(find(&st, Board::Advance(500)).is_none());
        assert!(find(&st, Board::Direction).is_some());
    }

    #[test]
    fn no_name_no_announce() {
        let st = run(Junction::Exit, &has(Junction::Exit, false, false), 3.0, None, &[]);
        assert!(find(&st, Board::Announce).is_none());
        assert!(find(&st, Board::Advance(500)).is_some());
    }

    #[test]
    fn no_advance_without_blocks() {
        let mut c = has(Junction::Exit, true, false);
        c.through = None;
        c.exits.clear();
        let st = run(Junction::Exit, &c, 3.0, None, &[]);
        assert!(find(&st, Board::Advance(500)).is_none() && find(&st, Board::Direction).is_none());
        assert!(find(&st, Board::Announce).is_some() && find(&st, Board::Gore).is_some());
    }
}
