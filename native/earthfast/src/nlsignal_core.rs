//! Local road-context matching in the same metric ENU frame as the junction.
//! No source-to-road identity is invented: arrows need both heading and lane-count agreement.

#[derive(Clone)]
pub struct Line {
    pub kind: u8, // 0: WKD inside section; 1: OSM road
    pub points: Vec<[f64; 2]>,
    pub oneway: i8,
    pub turns: [String; 3], // forward, backward, one-way
}

pub struct Query {
    pub xy: [f64; 2],
    pub travel: [f64; 2],
    pub lanes: usize,
}

#[derive(Debug, PartialEq)]
pub struct Match {
    pub zone: i8, // -1 unknown, 0 outside built-up section, 1 inside
    pub urban: i64,
    pub road: i64,
    pub turns: i64,
    pub road_forward: i8,
    pub turns_forward: i8,
    pub urban_distance2: f64,
    pub road_distance2: f64,
}

fn nearest(q: &Query, line: &Line) -> (f64, i8, bool) {
    let mut best = f64::INFINITY;
    let mut direction = 0;
    let mut aligned = false;
    let qn = q.travel[0] * q.travel[0] + q.travel[1] * q.travel[1];
    for pair in line.points.windows(2) {
        let (a, b) = (pair[0], pair[1]);
        let d = [b[0] - a[0], b[1] - a[1]];
        let n = d[0] * d[0] + d[1] * d[1];
        if n < 1e-9 { continue; }
        let t = (((q.xy[0] - a[0]) * d[0] + (q.xy[1] - a[1]) * d[1]) / n).clamp(0., 1.);
        let e = [q.xy[0] - a[0] - t * d[0], q.xy[1] - a[1] - t * d[1]];
        let distance = e[0] * e[0] + e[1] * e[1];
        if distance < best {
            best = distance;
            let dot = q.travel[0] * d[0] + q.travel[1] * d[1];
            direction = if dot >= 0. { 1 } else { -1 };
            aligned = dot * dot > 0.81 * qn * n;
        }
    }
    (best, direction, aligned)
}

fn turn_text(line: &Line, direction: i8) -> &str {
    if line.oneway != 0 { &line.turns[2] }
    else if direction > 0 { &line.turns[0] }
    else { &line.turns[1] }
}

pub fn matches(queries: &[Query], lines: &[Line], complete_wkd: bool) -> Result<Vec<Match>, &'static str> {
    if lines.iter().any(|l| l.kind > 1 || !(-1..=1).contains(&l.oneway) || l.points.len() < 2
        || !l.points.iter().flatten().all(|v| v.is_finite() && v.abs() < 1e9)
        || l.turns.iter().any(|t| t.len() > 4096)) {
        return Err("invalid signal context line");
    }
    let mut results = Vec::with_capacity(queries.len());
    for q in queries {
        let mut m = Match { zone: -1, urban: -1, road: -1, turns: -1, road_forward: 0,
                            turns_forward: 0, urban_distance2: f64::INFINITY, road_distance2: f64::INFINITY };
        if !q.xy.iter().chain(q.travel.iter()).all(|v| v.is_finite())
            || q.travel[0] * q.travel[0] + q.travel[1] * q.travel[1] < 0.5 || q.lanes > 16 {
            results.push(m);continue;
        }
        let mut turn_distance = f64::INFINITY;
        let mut ambiguous_turns = false;
        for (i, line) in lines.iter().enumerate() {
            let (distance, direction, aligned) = nearest(q, line);
            if line.kind == 0 {
                if distance < m.urban_distance2 { m.urban_distance2 = distance;m.urban = i as i64; }
                continue;
            }
            if distance > 225. || !aligned || line.oneway != 0 && direction != line.oneway { continue; }
            if distance < m.road_distance2 {
                m.road_distance2 = distance;m.road = i as i64;m.road_forward = direction;
            }
            let text = turn_text(line, direction);
            if text.is_empty() || q.lanes == 0 || text.split('|').count() != q.lanes { continue; }
            if distance + 1e-6 < turn_distance {
                turn_distance = distance;m.turns = i as i64;m.turns_forward = direction;ambiguous_turns = false;
            } else if (distance - turn_distance).abs() <= 1e-6 && m.turns >= 0
                && text != turn_text(&lines[m.turns as usize], m.turns_forward) {
                ambiguous_turns = true;
            }
        }
        if ambiguous_turns { m.turns = -1;m.turns_forward = 0; }
        if m.urban_distance2 <= 400. { m.zone = 1; }
        else if complete_wkd { m.zone = 0; }
        results.push(m);
    }
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn road(oneway: i8, turns: [&str; 3]) -> Line {
        Line { kind: 1, points: vec![[-50., 0.], [50., 0.]], oneway, turns: turns.map(String::from) }
    }
    fn q(dx: f64, dy: f64, lanes: usize) -> Query { Query { xy: [0., 1.], travel: [dx, dy], lanes } }
    #[test] fn one_way_and_lane_count_must_agree() {
        let lines = vec![road(-1, ["", "", "left|through"])];
        assert_eq!(matches(&[q(-1., 0., 2)], &lines, true).unwrap()[0].turns, 0);
        assert_eq!(matches(&[q(1., 0., 2)], &lines, true).unwrap()[0].road, -1);
        assert_eq!(matches(&[q(-1., 0., 3)], &lines, true).unwrap()[0].turns, -1);
    }
    #[test] fn two_way_headings_choose_separate_turn_fields() {
        let lines = vec![road(0, ["left|through", "through|right", ""])];
        let got = matches(&[q(1.,0.,2),q(-1.,0.,2)], &lines, true).unwrap();
        assert_eq!(got[0].turns_forward, 1);assert_eq!(got[1].turns_forward, -1);
    }
    #[test] fn transverse_or_far_roads_do_not_supply_arrows() {
        let lines = vec![road(1, ["", "", "through"])];
        assert_eq!(matches(&[q(0.,1.,1)], &lines, true).unwrap()[0].road, -1);
        let mut query = q(1.,0.,1);query.xy[1] = 16.;
        assert_eq!(matches(&[query], &lines, true).unwrap()[0].road, -1);
    }
    #[test] fn missing_wkd_does_not_claim_bubeko() {
        assert_eq!(matches(&[q(1.,0.,1)], &[], false).unwrap()[0].zone, -1);
        assert_eq!(matches(&[q(1.,0.,1)], &[], true).unwrap()[0].zone, 0);
        let mut line = road(0, ["", "", ""]);line.kind = 0;
        assert_eq!(matches(&[q(1.,0.,1)], &[line], false).unwrap()[0].zone, 1);
    }
    #[test] fn conflicting_equally_near_arrows_remain_unknown() {
        let lines = vec![road(1,["","","left"]),road(1,["","","right"])];
        assert_eq!(matches(&[q(1.,0.,1)], &lines, true).unwrap()[0].turns, -1);
    }
    #[test] fn invalid_geometry_fails_without_a_partial_match() {
        let mut line = road(1,["","","through"]);line.points[0][0] = f64::NAN;
        assert!(matches(&[q(1.,0.,1)], &[line], true).is_err());
        assert_eq!(matches(&[q(0.,0.,1)], &[], true).unwrap()[0].zone, -1);
    }
}
