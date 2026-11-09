//! What each German Autobahn board says, from the sign text as mapped in OpenStreetMap (German convention:
//! `destination` = the text on the sign, in sign order). Positional lists keep their empty slots; a single country or
//! symbol value is never spread over several names; " / " stays inside an entry; nothing is capped.


/// The raw fields of one junction, in this order (server/deautobahn.py RAW_FIELDS; "" = not mapped).
pub(crate) const RAW_FIELDS: &[&str] = &[
    "node_ref", "node_name", "node_noref",
    "lanes_destination", "lanes_destination_ref", "lanes_destination_symbol",
    "lanes_destination_colour", "lanes_destination_country", "lanes_turn",
    "link_destination", "link_destination_ref", "link_destination_symbol", "link_destination_colour",
    "link_destination_country", "link_destination_to", "link_destination_ref_to",
    "through_destination", "through_destination_ref", "through_destination_symbol",
    "through_destination_colour", "through_destination_country",
    "main_ref", "main_int_ref", "next_name", "next_ref",
    "branch0_destination", "branch0_destination_ref", "branch1_destination", "branch1_destination_ref",
];

fn f<'a>(raw: &[&'a str], name: &str) -> &'a str {
    RAW_FIELDS.iter().position(|k| *k == name).and_then(|i| raw.get(i).copied()).unwrap_or("").trim()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Junction {
    Exit,
    Kreuz,
    Dreieck,
    Rest,
}

/// One destination on a board: its text (empty when the slot carries only pictograms), its pictograms
/// (destination:symbol, several separated by ','), a white insert (destination:colour=white) and a country oval.
#[derive(Clone, Debug, PartialEq, Default)]
pub(crate) struct Entry {
    pub text: String,
    pub symbols: Vec<String>,
    pub white: bool,
    pub country: Option<String>,
}

/// The destinations of one direction: entries in sign order, route numbers ("A 3", "B 15", "E 56") and the lanes it
/// serves (0 = leftmost; empty when not known from destination:lanes).
#[derive(Clone, Debug, PartialEq, Default)]
pub(crate) struct Block {
    pub entries: Vec<Entry>,
    pub plates: Vec<String>,
    pub lanes: Vec<usize>,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Content {
    pub kind: Junction,
    pub number: Option<String>,
    pub name: Option<String>,
    pub through: Option<Block>,
    pub exits: Vec<Block>,
    pub forks: Vec<Block>,
}

/// A ';' list with its empty slots kept (positional: destination:symbol/colour/country follow the destinations' order).
pub(crate) fn split_list(v: &str) -> Vec<String> {
    if v.trim().is_empty() {
        return Vec::new();
    }
    v.split(';').map(|s| s.trim().to_string()).collect()
}

/// A '|' list of lanes (left to right), empty slots kept.
pub(crate) fn split_lanes(v: &str) -> Vec<String> {
    if v.trim().is_empty() {
        return Vec::new();
    }
    v.split('|').map(|s| s.trim().to_string()).collect()
}

fn lower(s: &str) -> String {
    s.to_lowercase()
}

/// A junction is a rest area by its name (Rastplatz, Raststätte, Rasthof, Parkplatz, Tank- und Rast, Autohof) or when
/// it has no number and its link no destination; a Kreuz or Dreieck by its link chain joining another motorway (the
/// glue's other_motorway), by its name ("Kreuz" / "Dreieck" as a word or a word's end: "Kamener Kreuz",
/// "Mönchhof-Dreieck") or by its link naming another A-number (spec A).
pub(crate) fn classify(raw: &[&str], other_motorway: bool) -> Junction {
    let name = lower(f(raw, "node_name"));
    let rest = ["rastplatz", "raststätte", "raststaette", "rasthof", "parkplatz", "tank- und rast", "autohof"];
    if rest.iter().any(|r| name.starts_with(r)) {
        return Junction::Rest;
    }
    if f(raw, "node_noref") == "yes" && f(raw, "link_destination").is_empty() && f(raw, "lanes_destination").is_empty() {
        return Junction::Rest;
    }
    let words: Vec<&str> = name.split(|c: char| c.is_whitespace() || c == '-').filter(|w| !w.is_empty()).collect();
    if words.iter().any(|w| w.ends_with("dreieck")) {
        return Junction::Dreieck;
    }
    if words.iter().any(|w| w.ends_with("kreuz")) || other_motorway {
        return Junction::Kreuz;
    }
    let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
    let main = norm(f(raw, "main_ref"));
    let refs = split_list(f(raw, "link_destination_ref")).into_iter()
        .chain(split_lanes(f(raw, "lanes_destination_ref")).into_iter().flat_map(|l| split_list(&l)));
    for r in refs {
        let r = norm(&r);
        let a_number = r.starts_with('A') && r[1..].trim_start().chars().next().is_some_and(|c| c.is_ascii_digit());
        if a_number && !main.is_empty() && r != main {
            return Junction::Kreuz;
        }
    }
    Junction::Exit
}

/// Positional attributes: one value per entry, or a single value for a single entry; anything else marks nothing.
fn positional(list: &str, n: usize) -> Vec<String> {
    let v = split_list(list);
    if v.len() == n {
        v
    } else {
        vec![String::new(); n]
    }
}

fn entries(dest: &str, symbol: &str, colour: &str, country: &str) -> Vec<Entry> {
    let names = split_list(dest);
    let n = names.len();
    let (sym, col, cty) = (positional(symbol, n), positional(colour, n), positional(country, n));
    let mut out: Vec<Entry> = Vec::new();
    for i in 0..n {
        let symbols: Vec<String> = sym[i].split(',').map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty() && s != "none").collect();
        let text = names[i].clone();
        if text.is_empty() && symbols.is_empty() {
            continue;
        }
        let c = cty[i].trim();
        let e = Entry {
            text,
            symbols,
            white: col[i].trim().eq_ignore_ascii_case("white"),
            country: if c.is_empty() || c == "none" { None } else { Some(c.to_string()) },
        };
        if !out.contains(&e) {
            out.push(e);
        }
    }
    out
}

fn plates(refs: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for r in split_list(refs) {
        let r = r.split_whitespace().collect::<Vec<_>>().join(" ");
        if !r.is_empty() && !out.contains(&r) {
            out.push(r);
        }
    }
    out
}

/// Whether a turn:lanes value leads off the carriageway on the exit's side (side +1 right, -1 left).
fn exit_turn(turn: &str, side: i32) -> bool {
    let key = if side >= 0 { "right" } else { "left" };
    turn.split(';').any(|t| t.contains(key))
}

fn through_turn(turn: &str) -> bool {
    turn.is_empty() || turn.split(';').any(|t| t == "through" || t == "none" || t.is_empty())
}

/// The blocks of destination:lanes: (the through lanes' block, the exit lanes' blocks by equal destination).
fn lane_blocks(raw: &[&str], side: i32) -> (Option<Block>, Vec<Block>) {
    let dest = split_lanes(f(raw, "lanes_destination"));
    let n = dest.len();
    if n == 0 {
        return (None, Vec::new());
    }
    let pick = |name: &str| -> Vec<String> {
        let v = split_lanes(f(raw, name));
        if v.len() == n { v } else { vec![String::new(); n] }
    };
    let (refs, sym, col, cty) = (pick("lanes_destination_ref"), pick("lanes_destination_symbol"),
                                 pick("lanes_destination_colour"), pick("lanes_destination_country"));
    let turn = split_lanes(f(raw, "lanes_turn"));
    let exits: Vec<bool> = if turn.len() == n {
        turn.iter().map(|t| exit_turn(t, side)).collect()
    } else {
        // no turn:lanes: the lanes on the exit's side whose destination differs from the far side's
        let far = if side >= 0 { &dest[0] } else { &dest[n - 1] };
        dest.iter().map(|d| d != far).collect()
    };
    let throughs: Vec<bool> = (0..n).map(|i| !exits[i] || (turn.len() == n && through_turn(&turn[i]) && turn[i].contains(';'))).collect();
    let block = |lanes: Vec<usize>, i: usize, fallback_plate: &str| -> Block {
        let mut p = plates(&refs[i]);
        if p.is_empty() && !fallback_plate.is_empty() {
            p = plates(fallback_plate);
        }
        Block { entries: entries(&dest[i], &sym[i], &col[i], &cty[i]), plates: p, lanes }
    };
    let main_ref = f(raw, "main_ref");
    let through = (0..n).find(|&i| throughs[i] && !dest[i].is_empty()).map(|first| {
        let lanes: Vec<usize> = (0..n).filter(|&i| throughs[i] && dest[i] == dest[first]).collect();
        block(lanes, first, main_ref)
    });
    let mut out: Vec<Block> = Vec::new();
    let mut seen: Vec<&String> = Vec::new();
    for i in 0..n {
        if !exits[i] || dest[i].is_empty() || seen.contains(&&dest[i]) {
            continue;
        }
        seen.push(&dest[i]);
        let lanes: Vec<usize> = (0..n).filter(|&j| exits[j] && dest[j] == dest[i]).collect();
        out.push(block(lanes, i, ""));
    }
    (through.filter(|b| !b.entries.is_empty()), out.into_iter().filter(|b| !b.entries.is_empty()).collect())
}

/// What the junction's boards say (spec B), or None when it gets no board: a rest area, or nothing mapped at all.
/// side: +1 for an exit to the right, -1 to the left.
pub(crate) fn content(raw: &[&str], kind: Junction, side: i32) -> Option<Content> {
    if kind == Junction::Rest {
        return None;
    }
    let number = if f(raw, "node_noref") == "yes" || f(raw, "node_ref").is_empty() {
        None
    } else {
        Some(f(raw, "node_ref").replace(';', "/"))
    };
    let name = Some(f(raw, "node_name").to_string()).filter(|s| !s.is_empty());
    let (mut through, mut exits) = lane_blocks(raw, side);
    if exits.is_empty() {
        let e = entries(f(raw, "link_destination"), f(raw, "link_destination_symbol"),
                        f(raw, "link_destination_colour"), f(raw, "link_destination_country"));
        if !e.is_empty() {
            exits.push(Block { entries: e, plates: plates(f(raw, "link_destination_ref")), lanes: Vec::new() });
        } else if let Some(n) = &name {
            exits.push(Block { entries: entries(n, "", "", ""), plates: Vec::new(), lanes: Vec::new() });
        }
    }
    if through.is_none() {
        let e = entries(f(raw, "through_destination"), f(raw, "through_destination_symbol"),
                        f(raw, "through_destination_colour"), f(raw, "through_destination_country"));
        let mut p = plates(f(raw, "through_destination_ref"));
        if p.is_empty() {
            p = plates(f(raw, "main_ref"));
        }
        if !e.is_empty() {
            through = Some(Block { entries: e, plates: p, lanes: Vec::new() });
        } else if !f(raw, "next_name").is_empty() {
            through = Some(Block { entries: entries(f(raw, "next_name"), "", "", ""), plates: plates(f(raw, "main_ref")),
                                   lanes: Vec::new() });
        }
    }
    let mut forks = Vec::new();
    let (b0, b1) = (f(raw, "branch0_destination"), f(raw, "branch1_destination"));
    if !b0.is_empty() && !b1.is_empty() {
        for (d, r) in [(b0, f(raw, "branch0_destination_ref")), (b1, f(raw, "branch1_destination_ref"))] {
            forks.push(Block { entries: entries(d, "", "", ""), plates: plates(r), lanes: Vec::new() });
        }
    }
    if name.is_none() && exits.is_empty() && through.is_none() {
        return None;
    }
    Some(Content { kind, number, name, through, exits, forks })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn raw(pairs: &[(&str, &str)]) -> Vec<String> {
        RAW_FIELDS
            .iter()
            .map(|f| pairs.iter().find(|(k, _)| k == f).map(|(_, v)| v.to_string()).unwrap_or_default())
            .collect()
    }

    fn view(v: &[String]) -> Vec<&str> {
        v.iter().map(|s| s.as_str()).collect()
    }

    fn texts(b: &Block) -> Vec<&str> {
        b.entries.iter().map(|e| e.text.as_str()).collect()
    }

    #[test]
    fn empty_slot_keeps_symbol_position() {
        let r = raw(&[("node_name", "Regensburg-Ost"), ("node_ref", "101"),
                      ("link_destination", "Regensburg-Ost;;Landshut;Obertraubling"),
                      ("link_destination_symbol", ";ferry,gvz;;")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        let b = &c.exits[0];
        assert_eq!(texts(b), vec!["Regensburg-Ost", "", "Landshut", "Obertraubling"]);
        assert_eq!(b.entries[1].symbols, vec!["ferry".to_string(), "gvz".to_string()]);
        assert!(b.entries[0].symbols.is_empty() && b.entries[2].symbols.is_empty());
    }

    #[test]
    fn country_not_broadcast() {
        let r = raw(&[("link_destination", "Dresden;Hof;Prag;Regensburg;Nürnberg"), ("link_destination_country", "CZ"),
                      ("node_name", "Kreuz Regensburg")]);
        let c = content(&view(&r), Junction::Kreuz, 1).unwrap();
        assert!(c.exits[0].entries.iter().all(|e| e.country.is_none()));
        let one = raw(&[("link_destination", "Linz"), ("link_destination_country", "A"), ("node_name", "X")]);
        let c = content(&view(&one), Junction::Exit, 1).unwrap();
        assert_eq!(c.exits[0].entries[0].country.as_deref(), Some("A"));
    }

    #[test]
    fn slash_inside_entry() {
        let r = raw(&[("link_destination", "Ingolstadt-Nord / Eichstätt;Lenting"), ("node_name", "Ingolstadt-Nord")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        assert_eq!(texts(&c.exits[0]), vec!["Ingolstadt-Nord / Eichstätt", "Lenting"]);
    }

    #[test]
    fn two_refs() {
        let r = raw(&[("node_ref", "8;7"), ("node_name", "Wiesbaden"), ("link_destination", "Wiesbaden")]);
        assert_eq!(content(&view(&r), Junction::Exit, 1).unwrap().number.as_deref(), Some("8/7"));
    }

    #[test]
    fn noref_has_name_no_number() {
        let r = raw(&[("node_ref", "12"), ("node_noref", "yes"), ("node_name", "Mitte"), ("link_destination", "Mitte")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        assert_eq!((c.number, c.name.as_deref()), (None, Some("Mitte")));
    }

    #[test]
    fn rest_area_is_rest() {
        for name in ["Rastplatz Erlgrund", "Raststätte Holledau", "Rasthof Holledau", "Parkplatz Wolfsberg",
                     "Autohof Regensburg"] {
            let r = raw(&[("node_name", name), ("link_destination", "x")]);
            assert_eq!(classify(&view(&r), false), Junction::Rest, "{name}");
            assert!(content(&view(&r), Junction::Rest, 1).is_none());
        }
        let r = raw(&[("node_noref", "yes")]);
        assert_eq!(classify(&view(&r), false), Junction::Rest);
    }

    #[test]
    fn kreuz_by_name_and_by_ref() {
        let by_name = raw(&[("node_name", "Kreuz Regensburg"), ("node_ref", "99")]);
        assert_eq!(classify(&view(&by_name), false), Junction::Kreuz);
        let dreieck = raw(&[("node_name", "Dreieck Holledau"), ("node_ref", "65")]);
        assert_eq!(classify(&view(&dreieck), false), Junction::Dreieck);
        let by_ref = raw(&[("node_name", "Regensburg"), ("main_ref", "A 3"), ("link_destination_ref", "A 93")]);
        assert_eq!(classify(&view(&by_ref), false), Junction::Kreuz);
        let same_ref = raw(&[("node_name", "Regensburg-Ost"), ("main_ref", "A 3"), ("link_destination_ref", "A 3;B 15")]);
        assert_eq!(classify(&view(&same_ref), false), Junction::Exit);
        let geometry = raw(&[("node_name", "Holledau")]);
        assert_eq!(classify(&view(&geometry), true), Junction::Kreuz);
        let not_kreuz = raw(&[("node_name", "Kreuzwertheim")]);
        assert_eq!(classify(&view(&not_kreuz), false), Junction::Exit);
    }

    #[test]
    fn adjective_kreuz() {
        for (name, kind) in [("Kamener Kreuz", Junction::Kreuz), ("Mönchhof-Dreieck", Junction::Dreieck),
                             ("Autobahnkreuz Nürnberg", Junction::Kreuz), ("Bad Kreuznach", Junction::Exit)] {
            let r = raw(&[("node_name", name)]);
            assert_eq!(classify(&view(&r), false), kind, "{name}");
        }
    }

    #[test]
    fn exit_lanes_from_turn_lanes() {
        let r = raw(&[
            ("node_name", "Neutraubling"), ("node_ref", "102"), ("main_ref", "A 3"),
            ("lanes_destination", "Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Linz;Passau;Rosenhof|Neutraubling;Donaustauf;Barbing"),
            ("lanes_destination_country", "A;;|A;;|A;;|"), ("lanes_turn", "none|none|none|slight_right"),
            ("link_destination", "Neutraubling;Donaustauf;Barbing")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        let t = c.through.as_ref().unwrap();
        assert_eq!(texts(t), vec!["Linz", "Passau", "Rosenhof"]);
        assert_eq!(t.entries[0].country.as_deref(), Some("A"));
        assert_eq!(t.lanes, vec![0, 1, 2]);
        assert_eq!(t.plates, vec!["A 3".to_string()]);
        assert_eq!(c.exits.len(), 1);
        assert_eq!(texts(&c.exits[0]), vec!["Neutraubling", "Donaustauf", "Barbing"]);
        assert_eq!(c.exits[0].lanes, vec![3]);
        assert_eq!((c.number.as_deref(), c.name.as_deref()), (Some("102"), Some("Neutraubling")));
    }

    #[test]
    fn kreuz_two_directions_on_two_lanes() {
        let r = raw(&[
            ("node_name", "Kreuz Regensburg"), ("node_ref", "99"), ("main_ref", "A 3"),
            ("lanes_destination", "Nürnberg;Sinzing|Nürnberg;Sinzing|Hof;Weiden;Regensburg;München;Ingolstadt|Hof;Weiden;Regensburg;München;Ingolstadt"),
            ("lanes_destination_ref", "A 3|A 3|A 93|A 93"), ("lanes_turn", "through|through|slight_right|slight_right"),
            ("branch0_destination", "Hof;Weiden;Regensburg"), ("branch0_destination_ref", "A 93"),
            ("branch1_destination", "München;Ingolstadt"), ("branch1_destination_ref", "A 93")]);
        let c = content(&view(&r), Junction::Kreuz, 1).unwrap();
        assert_eq!(texts(c.through.as_ref().unwrap()), vec!["Nürnberg", "Sinzing"]);
        assert_eq!(c.exits[0].lanes, vec![2, 3]);
        assert_eq!(c.exits[0].plates, vec!["A 93".to_string()]);
        assert_eq!(texts(&c.exits[0]).len(), 5);
        assert_eq!(c.forks.len(), 2);
        assert_eq!(texts(&c.forks[1]), vec!["München", "Ingolstadt"]);
    }

    #[test]
    fn through_falls_back_to_next_junction() {
        let r = raw(&[("node_name", "Neutraubling"), ("link_destination", "Neutraubling;Barbing"), ("main_ref", "A 3"),
                      ("next_name", "Regensburg-Ost"), ("next_ref", "101")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        let t = c.through.unwrap();
        assert_eq!(texts(&t), vec!["Regensburg-Ost"]);
        assert_eq!(t.plates, vec!["A 3".to_string()]);
    }

    #[test]
    fn no_through_when_nothing_mapped() {
        let r = raw(&[("node_name", "Neutraubling"), ("link_destination", "Neutraubling;Barbing")]);
        assert!(content(&view(&r), Junction::Exit, 1).unwrap().through.is_none());
    }

    #[test]
    fn no_cap_on_entries() {
        let r = raw(&[("node_name", "Lenting"), ("link_destination", "Lenting;Audi GVZ;Gaimershaim;Hepberg;Kösching")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        assert_eq!(texts(&c.exits[0]), vec!["Lenting", "Audi GVZ", "Gaimershaim", "Hepberg", "Kösching"]);
    }

    #[test]
    fn white_insert() {
        let r = raw(&[("node_name", "Regensburg-Ost"), ("link_destination", "Regensburg-Ost;Landshut;GVZ;Obertraubling"),
                      ("link_destination_colour", "none;none;white;none")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        let w: Vec<bool> = c.exits[0].entries.iter().map(|e| e.white).collect();
        assert_eq!(w, vec![false, false, true, false]);
    }

    #[test]
    fn exit_falls_back_to_junction_name() {
        let r = raw(&[("node_name", "Rosenhof"), ("node_ref", "103")]);
        let c = content(&view(&r), Junction::Exit, 1).unwrap();
        assert_eq!(texts(&c.exits[0]), vec!["Rosenhof"]);
    }

    #[test]
    fn nothing_at_all_is_no_content() {
        let r = raw(&[("main_ref", "A 3")]);
        assert!(content(&view(&r), Junction::Exit, 1).is_none());
    }
}
