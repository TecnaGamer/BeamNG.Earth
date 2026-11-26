//! The browsable report beside a scan (SCAN/report/index.html): the issues per mile by kind and severity, the ranked
//! issues with mileposts and map links, plan views of the top N (the built scene as PNG, north up, the issue ringed;
//! the aerial beside it at the same scale where the glue fetched one: report/aerial/NAME.jpg, listed in
//! report/aerials.tsv for it) and, with a second state, the before / after comparison.
//!
//!   roadscan report SCAN_DIR [--against SCAN_DIR] [--top N] [--title T] [--miles M] [--px PX]
use crate::rank::{self, Row};
use crate::scene::Packs;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;

const SEV: [&str; 4] = ["trace", "minor", "major", "critical"];
const LEVEL: [&str; 3] = ["ground", "bridge", "tunnel"];

fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#39;")
}

fn maplink(lat: f64, lon: f64) -> String {
    format!("<a href='https://www.google.com/maps/@{:.6},{:.6},80m/data=!3m1!1e3'>{:.6},{:.6}</a>", lat, lon, lat, lon)
}

fn mile(r: &Row) -> String {
    if r.mile.is_finite() { format!("{:.2}{}", r.mile, if r.on_route { "" } else { " (off)" }) } else { String::new() }
}

/// The view's half size (m): the issue's extent and a margin, 30 to 120 m.
pub fn half_of(r: &Row) -> f64 {
    let ext = ((r.bbox[2] - r.bbox[0]) * 111_320.0).abs().max(((r.bbox[3] - r.bbox[1]) * 111_320.0 * r.lat.to_radians().cos()).abs());
    (ext / 2.0 + 20.0).clamp(30.0, 120.0).round()
}

pub fn aerial_name(r: &Row) -> String { format!("{:.6}_{:.6}_{}.jpg", r.lat, r.lon, half_of(r) as i64) }

pub struct Opts<'a> { pub against: Option<&'a Path>, pub top: usize, pub title: String, pub miles: f64, pub px: usize }

/// packs: the scan's; before: the --against state's (its built scene shown between the aerial and this one's).
pub fn write(scan: &Path, rows: &[Row], o: &Opts, packs: &mut Packs, mut before: Option<&mut Packs>) -> Result<String, String> {
    let rep = scan.join("report");
    std::fs::create_dir_all(rep.join("img")).map_err(|e| e.to_string())?;
    let mut h = String::new();
    let _ = write!(h, "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>\
<title>{}</title><style>body{{font:14px system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}}\
table{{border-collapse:collapse;margin:8px 0}}td,th{{border:1px solid #ccc;padding:3px 6px;text-align:left}}th{{background:#eee}}\
.s3{{background:#f8d0d0}}.s2{{background:#fbe8c8}}.s1{{background:#f4f4dc}}.card{{margin:14px 0;padding:8px;background:#fff;border:1px solid #ddd}}\
.views img{{width:32%;max-width:480px;margin-right:1%;vertical-align:top}}</style></head><body><h1>{}</h1>",
        esc(&o.title), esc(&o.title));
    let tiles = std::fs::read_dir(scan.join("tiles")).map(|r| r.filter_map(|e| e.ok())
        .filter(|e| e.path().extension().map_or(false, |x| x == "tsv")).count()).unwrap_or(0);
    let per_mile = o.miles.is_finite() && o.miles > 0.0;
    let _ = write!(h, "<p>{} tiles scanned{}; {} issues (scanner version {}). Severity: <span class=s3>critical</span> (a car is \
stopped, falls or is thrown), <span class=s2>major</span> (felt or seen at once), <span class=s1>minor</span>.</p>",
        tiles, if per_mile { format!(", {:.2} miles of road", o.miles) } else { String::new() }, rows.len(), crate::SCAN_VERSION);
    // by kind
    let mut by: BTreeMap<&str, [usize; 4]> = BTreeMap::new();
    for r in rows { by.entry(r.kind.as_str()).or_default()[r.sev.min(3) as usize] += 1; }
    let mut kinds: Vec<(&&str, &[usize; 4])> = by.iter().collect();
    kinds.sort_by(|a, b| b.1[3].cmp(&a.1[3]).then(b.1[2].cmp(&a.1[2])).then(a.0.cmp(b.0)));
    let _ = write!(h, "<h2>By kind</h2><table><tr><th>kind</th><th>critical</th><th>major</th><th>minor</th>{}<th>what</th></tr>",
                   if per_mile { "<th>per mile</th>" } else { "" });
    for (k, c) in kinds {
        let pm = if per_mile { format!("<td>{:.1}</td>", (c[1] + c[2] + c[3]) as f64 / o.miles) } else { String::new() };
        let _ = write!(h, "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>{}<td>{}</td></tr>", esc(k), c[3], c[2], c[1], pm,
                       esc(rank::describe(k).0));
    }
    h.push_str("</table>");
    // before / after
    if let Some(a_dir) = o.against {
        let a = crate::read_issues(a_dir);
        let (ta, tb) = (crate::scanned(a_dir), crate::scanned(scan));
        let both = |t: &str| ta.contains(t) && tb.contains(t);
        let (pairs, _) = rank::compare(&a, rows, &both);
        let mut list: Vec<(&str, &Row, Option<&Row>, Option<&Row>)> = Vec::new();
        for &(i, j) in &pairs {
            let st = match (i, j) { (Some(i), Some(j)) => rank::status(&a[i], &rows[j]), (None, Some(_)) => "new", _ => "gone" };
            if st == "same" { continue; }
            let x = j.map(|j| &rows[j]).or(i.map(|i| &a[i])).unwrap();
            list.push((st, x, i.map(|i| &a[i]), j.map(|j| &rows[j])));
        }
        let order = |s: &str| ["new", "worse", "better", "gone"].iter().position(|&x| x == s).unwrap_or(4);
        list.sort_by(|p, q| order(p.0).cmp(&order(q.0)).then(q.1.sev.cmp(&p.1.sev)).then(p.1.rank.cmp(&q.1.rank)));
        let bad = list.iter().filter(|p| matches!(p.0, "new" | "worse") && p.1.sev >= 2).count();
        let _ = write!(h, "<h2>Against {}</h2><p>{} tiles in both; <b>{}</b> new or worse issues of severity major or critical.</p>\
<table><tr><th>status</th><th>kind</th><th>sev</th><th>before</th><th>after</th><th>where</th></tr>",
            esc(&a_dir.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()), ta.intersection(&tb).count(), bad);
        for (st, x, pa, pb) in list.iter().take(400) {
            let m = |r: &Option<&Row>| r.map(|r| format!("{} ({} cells)", rank::num(r.mag), r.n)).unwrap_or_default();
            let _ = write!(h, "<tr class=s{}><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", x.sev, st,
                           esc(&x.kind), SEV[x.sev.min(3) as usize], m(pa), m(pb), maplink(x.lat, x.lon));
        }
        h.push_str("</table>");
    }
    // the worst, with their views
    let mut wanted = String::from("name\tlat\tlon\thalf\n");
    let _ = write!(h, "<h2>Worst {}</h2><p>Left: the finest public aerial; right: the built scene (terrain by height, holes red; \
roads grey, sidewalks light, markings white, deck tops orange over their sides brown, rails purple, barriers red-brown, tunnels \
translucent blue; mapped centre lines: ground cyan, bridge yellow, tunnel magenta dashed). The issue is ringed in magenta.</p>",
        o.top.min(rows.len()));
    for r in rows.iter().take(o.top) {
        let half = half_of(r);
        let png = format!("img/{:04}.png", r.rank);
        let built = match crate::plan::render(packs, r.lat, r.lon, half, o.px, &[(r.lat, r.lon)]) {
            Ok(ppm) => crate::png::from_ppm(&ppm).ok_or("bad PPM".to_string())
                .and_then(|b| std::fs::write(rep.join(&png), b).map_err(|e| e.to_string())),
            Err(e) => Err(e),
        };
        let was = match before.as_deref_mut() {
            Some(bp) => {
                let png0 = format!("img/{:04}_before.png", r.rank);
                match crate::plan::render(bp, r.lat, r.lon, half, o.px, &[(r.lat, r.lon)]).ok().and_then(|p| crate::png::from_ppm(&p))
                    .and_then(|b| std::fs::write(rep.join(&png0), b).ok()) {
                    Some(()) => format!("<img src='{}' alt='before'>", png0),
                    None => "(no view before)".to_string(),
                }
            }
            None => String::new(),
        };
        let an = aerial_name(r);
        let _ = writeln!(wanted, "{}\t{:.7}\t{:.7}\t{}", an, r.lat, r.lon, half);
        let (what, unit) = rank::describe(&r.kind);
        let _ = write!(h, "<div class='card s{}'><b>#{} {}: {}</b> {} {}, {} cells; {}<br>{} mile {}, {} {}, tile {}, way {}<br>\
<div class=views><img src='aerial/{}' alt='(no aerial)'>{}{}</div>{:.0} ft across{}</div>",
            r.sev, r.rank, SEV[r.sev.min(3) as usize], esc(&r.kind), rank::num(r.mag), esc(unit), r.n, esc(what), maplink(r.lat, r.lon),
            mile(r), esc(&r.cls), LEVEL[r.level.min(2) as usize], esc(&r.tile), esc(&r.line), esc(&an), was,
            match built { Ok(()) => format!("<img src='{}' alt='built'>", png), Err(e) => format!("(no view: {})", esc(&e)) },
            2.0 * half * 3.28084, if before.is_some() { " (aerial | before | after)" } else { " (aerial | built)" });
    }
    std::fs::write(rep.join("aerials.tsv"), wanted).map_err(|e| e.to_string())?;
    // every issue
    h.push_str("<h2>All issues</h2><table><tr><th>#</th><th>sev</th><th>kind</th><th>magnitude</th><th>cells</th><th>mile</th>\
<th>class</th><th>level</th><th>where</th></tr>");
    for r in rows.iter().take(3000) {
        let _ = write!(h, "<tr class=s{}><td>{}</td><td>{}</td><td>{}</td><td>{} {}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            r.sev, r.rank, SEV[r.sev.min(3) as usize], esc(&r.kind), rank::num(r.mag), esc(rank::describe(&r.kind).1), r.n, mile(r),
            esc(&r.cls), LEVEL[r.level.min(2) as usize], maplink(r.lat, r.lon));
    }
    h.push_str("</table></body></html>\n");
    let page = rep.join("index.html");
    std::fs::write(&page, h).map_err(|e| e.to_string())?;
    Ok(page.to_string_lossy().to_string())
}
