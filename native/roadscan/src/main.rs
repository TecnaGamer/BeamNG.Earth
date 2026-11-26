//! roadscan: the corridor road scanner's core (tools/roadscan/roadscan.py is the glue: corridor, builds, packs, report).
//!
//!   roadscan scan INDEX OUTDIR [--threads N] [--tiles KEY,KEY] [--force]
//!       every tile of INDEX (pack.rs) scanned: its scene with its neighbours (scene.rs), the M0 audit's located cells
//!       (roadsys audit::audit_findings) and the scanner's own checks (checks.rs), the findings inside the tile's own
//!       quad written to OUTDIR/tiles/KEY.tsv (and its audit metrics to KEY.audit.json); tiles already scanned are kept
//!       (resumable); one thread per tile, the output the same with any thread count.
//!   roadscan rank OUTDIR [--route ROUTE.bin] [--halfwidth M]
//!       every tile's findings clustered into issues (rank.rs): OUTDIR/issues.json, issues.tsv, summary.tsv.
//!   roadscan compare A_DIR B_DIR OUTDIR
//!       two states' issues on the same corridor (only tiles scanned in both): OUTDIR/compare.json, compare.tsv;
//!       exits 1 when B has new or worse issues of severity 2 or more.
//!   roadscan report SCAN_DIR [--against SCAN_DIR] [--top N] [--title T] [--miles M] [--px PX]
//!       the browsable report (report.rs): SCAN_DIR/report/index.html, the aerials it wants in report/aerials.tsv.
//!   roadscan tiles LINES.txt OUT.tsv [--halfwidth M] [--clip A,B] [--route-out ROUTE.bin] [--route-txt ROUTE.txt]
//!       a corridor's geotiles (corridor.rs).
//!   roadscan plan INDEX LAT LON HALF_M PX OUT.ppm [LAT,LON ...]
//!       a plan view of the built scene round a point, the points marked (plan.rs).
#![allow(dead_code)]     // (format fields read by some commands only)
mod checks;
mod corridor;
mod pack;
mod plan;
mod png;
mod rank;
mod report;
mod scene;

use roadsys::audit::{audit_findings, Finding};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

/// Raised whenever a finding's meaning or a threshold changes; compare refuses two versions.
pub const SCAN_VERSION: u32 = 2;

fn die(msg: String) -> ! { eprintln!("roadscan: {}", msg); std::process::exit(2) }

fn write_atomic(path: &Path, data: &[u8]) -> Result<(), String> {
    let tmp = path.with_extension(format!("tmp{}", std::process::id()));
    std::fs::write(&tmp, data).map_err(|e| format!("{}: {}", tmp.display(), e))?;
    std::fs::rename(&tmp, path).map_err(|e| format!("{}: {}", path.display(), e))
}

/// One tile's findings (its own quad), as TSV lines, and its audit metrics.
fn scan_tile(packs: &mut scene::Packs, ti: usize) -> Result<(String, String), String> {
    let b = scene::build(packs, ti, scene::GROW)?;
    let (metrics, found) = audit_findings(&b.scene);
    let mut mine: Vec<Finding> = found.iter().filter(|f| f.code != "SA" && b.owned(f.x, f.y)).copied().collect();
    checks::mark_kerbs(&b, &found, &mut mine);
    checks::run(&b, &found, &mut mine);
    let mut s = String::new();
    for f in &mine {
        let (lat, lon) = b.latlon(f.x, f.y);
        let (id, cls) = b.lines.get(f.line as usize).map(|l| (l.id.as_str(), l.cls.as_str())).unwrap_or(("", ""));
        let r = rank::Rec { code: f.code.into(), lat, lon, z: f.z, v: f.v, level: f.level, kind: f.kind, mask: f.mask,
                            line: id.replace(['\t', '\n'], " "), cls: cls.replace(['\t', '\n'], " "), tile: b.key.clone() };
        s.push_str(&rank::rec_line(&r));
        s.push('\n');
    }
    Ok((s, metrics.json()))
}

fn cmd_scan(args: &[String]) {
    let index = PathBuf::from(&args[0]);
    let out = PathBuf::from(&args[1]);
    let opt = |f: &str| args.iter().position(|a| a == f).and_then(|i| args.get(i + 1)).cloned();
    let threads: usize = opt("--threads").and_then(|v| v.parse().ok()).unwrap_or(2).max(1);
    let force = args.iter().any(|a| a == "--force");
    let ix = pack::read_index(&index).unwrap_or_else(|e| die(e));
    let want: Option<Vec<String>> = opt("--tiles").map(|v| v.split(',').map(String::from).collect());
    let todo: Vec<usize> = (0..ix.tiles.len()).filter(|&i| want.as_ref().map_or(true, |w| w.contains(&ix.tiles[i].key)))
        .filter(|&i| force || !out.join("tiles").join(format!("{}.tsv", ix.tiles[i].key)).exists()).collect();
    std::fs::create_dir_all(out.join("tiles")).unwrap_or_else(|e| die(format!("{}: {}", out.display(), e)));
    let next = AtomicUsize::new(0);
    let done = AtomicUsize::new(0);
    let failed = AtomicUsize::new(0);
    let t0 = std::time::Instant::now();
    std::thread::scope(|sc| {
        for _ in 0..threads.min(todo.len().max(1)) {
            sc.spawn(|| {
                let mut packs = scene::Packs::new(&ix);
                loop {
                    let k = next.fetch_add(1, Ordering::SeqCst);
                    if k >= todo.len() { break; }
                    let ti = todo[k];
                    let key = &ix.tiles[ti].key;
                    let dir = out.join("tiles");
                    match scan_tile(&mut packs, ti) {
                        Ok((tsv, metrics)) => {
                            let r = write_atomic(&dir.join(format!("{}.audit.json", key)), metrics.as_bytes())
                                .and_then(|_| write_atomic(&dir.join(format!("{}.tsv", key)), format!("{}\n{}", rank::HEADER, tsv).as_bytes()));
                            if let Err(e) = r { eprintln!("{}: {}", key, e); failed.fetch_add(1, Ordering::SeqCst); }
                            let _ = std::fs::remove_file(dir.join(format!("{}.err", key)));
                        }
                        Err(e) => {
                            eprintln!("{}: {}", key, e);
                            let _ = std::fs::write(dir.join(format!("{}.err", key)), e);
                            failed.fetch_add(1, Ordering::SeqCst);
                        }
                    }
                    let d = done.fetch_add(1, Ordering::SeqCst) + 1;
                    if d % 50 == 0 || d == todo.len() {
                        eprintln!("scanned {}/{} tiles, {:.0} s", d, todo.len(), t0.elapsed().as_secs_f64());
                    }
                }
            });
        }
    });
    let summary = format!("{{\"scan_version\":{},\"audit_version\":{},\"tiles\":{},\"scanned\":{},\"failed\":{},\"seconds\":{:.1},\"threads\":{}}}\n",
        SCAN_VERSION, roadsys::audit::AUDIT_VERSION, ix.tiles.len(), todo.len(), failed.load(Ordering::SeqCst),
        t0.elapsed().as_secs_f64(), threads);
    write_atomic(&out.join("scan.json"), summary.as_bytes()).unwrap_or_else(|e| die(e));
    if failed.load(Ordering::SeqCst) > 0 { std::process::exit(1); }
}

fn read_tiles(dir: &Path) -> Vec<rank::Rec> {
    let mut names: Vec<PathBuf> = std::fs::read_dir(dir.join("tiles")).map(|r| r.filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map_or(false, |x| x == "tsv")).collect()).unwrap_or_default();
    names.sort();
    let mut recs = Vec::new();
    for p in names {
        let s = std::fs::read_to_string(&p).unwrap_or_else(|e| die(format!("{}: {}", p.display(), e)));
        recs.extend(s.lines().filter_map(rank::parse_rec));
    }
    recs
}

fn cmd_rank(args: &[String]) {
    let dir = PathBuf::from(&args[0]);
    let opt = |f: &str| args.iter().position(|a| a == f).and_then(|i| args.get(i + 1)).cloned();
    let route = opt("--route").map(|p| rank::Route::new(&pack::read_route(Path::new(&p)).unwrap_or_else(|e| die(e))));
    let halfwidth: f64 = opt("--halfwidth").and_then(|v| v.parse().ok()).unwrap_or(40.0);
    let recs = read_tiles(&dir);
    let tiles: std::collections::BTreeSet<&str> = recs.iter().map(|r| r.tile.as_str()).collect();
    let n_tiles = std::fs::read_dir(dir.join("tiles")).map(|r| r.filter_map(|e| e.ok())
        .filter(|e| e.path().extension().map_or(false, |x| x == "tsv")).count()).unwrap_or(0);
    let issues = rank::rank(&recs, route.as_ref(), halfwidth);
    let mut by: std::collections::BTreeMap<&str, [usize; 4]> = std::collections::BTreeMap::new();
    for i in &issues { by.entry(i.kind).or_default()[i.sev as usize] += 1; }
    let mut j = format!("{{\"scan_version\":{},\"tiles\":{},\"tiles_with_findings\":{},\"findings\":{},\"route_miles\":{},\"halfwidth_m\":{},\"by_kind\":{{",
        SCAN_VERSION, n_tiles, tiles.len(), recs.len(), rank::num(route.as_ref().map_or(f64::NAN, |r| r.len_m() / 1609.344)), halfwidth);
    for (k, (kind, c)) in by.iter().enumerate() {
        if k > 0 { j.push(','); }
        j.push_str(&format!("{}:{{\"minor\":{},\"major\":{},\"critical\":{}}}", rank::json_str(kind), c[1], c[2], c[3]));
    }
    j.push_str("},\"issues\":[\n");
    let mut t = format!("{}\n", rank::ISSUE_HEADER);
    for (k, i) in issues.iter().enumerate() {
        if k > 0 { j.push_str(",\n"); }
        j.push_str(&rank::issue_json(i, k + 1));
        t.push_str(&rank::issue_tsv(i, k + 1));
        t.push('\n');
    }
    j.push_str("\n]}\n");
    let mut s = String::from("kind\tminor\tmajor\tcritical\n");
    for (kind, c) in &by { s.push_str(&format!("{}\t{}\t{}\t{}\n", kind, c[1], c[2], c[3])); }
    write_atomic(&dir.join("issues.json"), j.as_bytes()).unwrap_or_else(|e| die(e));
    write_atomic(&dir.join("issues.tsv"), t.as_bytes()).unwrap_or_else(|e| die(e));
    write_atomic(&dir.join("summary.tsv"), s.as_bytes()).unwrap_or_else(|e| die(e));
    eprintln!("{} findings in {} tiles -> {} issues", recs.len(), n_tiles, issues.len());
}

pub(crate) fn read_issues(dir: &Path) -> Vec<rank::Row> {
    let p = dir.join("issues.tsv");
    std::fs::read_to_string(&p).unwrap_or_else(|e| die(format!("{}: {}", p.display(), e))).lines().filter_map(rank::parse_issue).collect()
}

pub(crate) fn scanned(dir: &Path) -> std::collections::BTreeSet<String> {
    std::fs::read_dir(dir.join("tiles")).map(|r| r.filter_map(|e| e.ok()).filter_map(|e| {
        let n = e.file_name().to_string_lossy().to_string();
        n.strip_suffix(".tsv").map(String::from)
    }).collect()).unwrap_or_default()
}

fn version_of(dir: &Path) -> String {
    std::fs::read_to_string(dir.join("scan.json")).ok().and_then(|s| s.split("\"scan_version\":").nth(1).map(|v| v.split(',').next().unwrap_or("").to_string()))
        .unwrap_or_default()
}

fn cmd_compare(args: &[String]) {
    let (a_dir, b_dir, out) = (PathBuf::from(&args[0]), PathBuf::from(&args[1]), PathBuf::from(&args[2]));
    if version_of(&a_dir) != version_of(&b_dir) { die(format!("scan versions differ ({} vs {}): scan both with one scanner", version_of(&a_dir), version_of(&b_dir))); }
    let (a, b) = (read_issues(&a_dir), read_issues(&b_dir));
    let (ta, tb) = (scanned(&a_dir), scanned(&b_dir));
    let both = |t: &str| ta.contains(t) && tb.contains(t);
    let (pairs, summary) = rank::compare(&a, &b, &both);
    std::fs::create_dir_all(&out).unwrap_or_else(|e| die(format!("{}: {}", out.display(), e)));
    let mut j = format!("{{\"a\":{},\"b\":{},\"tiles_both\":{},\"pairs\":[\n", rank::json_str(&a_dir.to_string_lossy()),
                        rank::json_str(&b_dir.to_string_lossy()), ta.intersection(&tb).count());
    let mut bad = 0;
    let row = |r: &rank::Row| format!("{{\"rank\":{},\"kind\":{},\"severity\":{},\"lat\":{:.7},\"lon\":{:.7},\"magnitude\":{},\"cells\":{},\"mile\":{},\"tile\":{}}}",
        r.rank, rank::json_str(&r.kind), r.sev, r.lat, r.lon, rank::num(r.mag), r.n, rank::num(r.mile), rank::json_str(&r.tile));
    for (k, &(i, jb)) in pairs.iter().enumerate() {
        if k > 0 { j.push_str(",\n"); }
        let status = match (i, jb) {
            (Some(i), Some(jb)) => rank::status(&a[i], &b[jb]),
            (None, Some(_)) => "new",
            _ => "gone",
        };
        if matches!(status, "worse" | "new") && jb.map_or(false, |jb| b[jb].sev >= 2) { bad += 1; }
        j.push_str(&format!("{{\"status\":\"{}\",\"a\":{},\"b\":{}}}", status, i.map_or("null".into(), |i| row(&a[i])), jb.map_or("null".into(), |jb| row(&b[jb]))));
    }
    j.push_str(&format!("\n],\"regressions\":{}}}\n", bad));
    write_atomic(&out.join("compare.json"), j.as_bytes()).unwrap_or_else(|e| die(e));
    write_atomic(&out.join("compare.tsv"), format!("kind\tstatus\tminor\tmajor\tcritical\n{}", summary).as_bytes()).unwrap_or_else(|e| die(e));
    eprint!("{}", summary);
    eprintln!("{} new or worse issues of severity 2+", bad);
    if bad > 0 { std::process::exit(1); }
}

fn cmd_report(args: &[String]) {
    let scan = PathBuf::from(&args[0]);
    let opt = |f: &str| args.iter().position(|a| a == f).and_then(|i| args.get(i + 1)).cloned();
    let against = opt("--against").map(PathBuf::from);
    let o = report::Opts { against: against.as_deref(), top: opt("--top").and_then(|v| v.parse().ok()).unwrap_or(30),
                           title: opt("--title").unwrap_or_else(|| scan.to_string_lossy().to_string()),
                           miles: opt("--miles").and_then(|v| v.parse().ok()).unwrap_or(f64::NAN),
                           px: opt("--px").and_then(|v| v.parse().ok()).unwrap_or(480).clamp(64, 2048) };
    let ix = pack::read_index(&scan.join("index.bin")).unwrap_or_else(|e| die(e));
    let mut packs = scene::Packs::new(&ix);
    let rows = read_issues(&scan);
    let ix0 = against.as_ref().map(|a| pack::read_index(&a.join("index.bin")).unwrap_or_else(|e| die(e)));
    let mut before = ix0.as_ref().map(scene::Packs::new);
    println!("{}", report::write(&scan, &rows, &o, &mut packs, before.as_mut()).unwrap_or_else(|e| die(e)));
}

fn cmd_tiles(args: &[String]) {
    let opt = |f: &str| args.iter().position(|a| a == f).and_then(|i| args.get(i + 1)).cloned();
    let text = std::fs::read_to_string(&args[0]).unwrap_or_else(|e| die(format!("{}: {}", args[0], e)));
    let mut lines = corridor::read_lines(&text).unwrap_or_else(|e| die(e));
    if lines.is_empty() { die("no line of two points or more".into()); }
    if let Some(c) = opt("--clip") {
        let mut w = c.split(',').map(|v| v.parse::<f64>().unwrap_or_else(|_| die(format!("--clip A,B: {}", c))));
        let (a, b) = (w.next().unwrap_or(0.0), w.next().unwrap_or(f64::INFINITY));
        lines[0] = corridor::clip(&lines[0], a, b);
        if lines[0].len() < 2 { die(format!("nothing of the route between miles {} and {}", a, b)); }
    }
    let hw: f64 = opt("--halfwidth").and_then(|v| v.parse().ok()).unwrap_or(40.0);
    write_atomic(Path::new(&args[1]), corridor::run(&lines, hw).as_bytes()).unwrap_or_else(|e| die(e));
    if let Some(r) = opt("--route-out") { write_atomic(Path::new(&r), &corridor::route_bin(&lines[0])).unwrap_or_else(|e| die(e)); }
    if let Some(r) = opt("--route-txt") {
        let s: String = lines[0].iter().map(|p| format!("{:.7},{:.7}\n", p.0, p.1)).collect();
        write_atomic(Path::new(&r), s.as_bytes()).unwrap_or_else(|e| die(e));
    }
}

fn cmd_plan(args: &[String]) {
    let ix = pack::read_index(Path::new(&args[0])).unwrap_or_else(|e| die(e));
    let num = |s: &str| s.parse::<f64>().unwrap_or_else(|_| die(format!("not a number: {}", s)));
    let (lat, lon, half) = (num(&args[1]), num(&args[2]), num(&args[3]));
    let px = num(&args[4]) as usize;
    if !(16..=4096).contains(&px) { die("PX: 16..4096".into()); }
    let marks: Vec<(f64, f64)> = args[6..].iter().filter_map(|m| { let mut w = m.split(','); Some((w.next()?.parse().ok()?, w.next()?.parse().ok()?)) }).collect();
    let mut packs = scene::Packs::new(&ix);
    let img = plan::render(&mut packs, lat, lon, half, px, &marks).unwrap_or_else(|e| die(e));
    write_atomic(Path::new(&args[5]), &img).unwrap_or_else(|e| die(e));
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let usage = "usage: roadscan scan INDEX OUTDIR [--threads N] [--tiles K,..] [--force] | rank OUTDIR [--route R.bin] [--halfwidth M] | compare A B OUT | plan INDEX LAT LON HALF PX OUT.ppm [LAT,LON ..]";
    match args.get(1).map(String::as_str) {
        Some("scan") if args.len() >= 4 => cmd_scan(&args[2..]),
        Some("rank") if args.len() >= 3 => cmd_rank(&args[2..]),
        Some("compare") if args.len() == 5 => cmd_compare(&args[2..]),
        Some("plan") if args.len() >= 8 => cmd_plan(&args[2..]),
        Some("tiles") if args.len() >= 4 => cmd_tiles(&args[2..]),
        Some("report") if args.len() >= 3 => cmd_report(&args[2..]),
        Some("version") => println!("{} {}", SCAN_VERSION, roadsys::audit::AUDIT_VERSION),
        _ => { eprintln!("{}", usage); std::process::exit(2); }
    }
}

#[cfg(test)]
mod tests {
    //! A synthetic corridor written in the export's format: two tiles side by side in two frames 0.2 m apart, flat
    //! ground at z 10 with a 1 m tall deck (a ramp up, a flat top and a 0.2 m step down at its far end) on a two-lane
    //! road along x, a hole in the terrain, and a hump. The scan must place each defect once, at the right tile.
    use super::*;
    use roadsys::audit::{FLAG_DECK_TOP, KIND_DECK, KIND_ROAD};

    fn put_str(b: &mut Vec<u8>, s: &str) { b.extend((s.len() as u16).to_le_bytes()); b.extend(s.as_bytes()); }
    fn put_f(b: &mut Vec<u8>, v: f64) { b.extend(v.to_le_bytes()); }

    struct T { key: &'static str, x0: f64, tris: Vec<(u8, u8, bool, [[f64; 3]; 3])>, hole: Option<(i64, i64)>, hump: f64 }

    fn quad_tris(x0: f64, x1: f64, y0: f64, y1: f64, z0: f64, z1: f64) -> Vec<[[f64; 3]; 3]> {
        vec![[[x0, y0, z0], [x1, y0, z1], [x1, y1, z1]], [[x0, y0, z0], [x1, y1, z1], [x0, y1, z0]]]
    }

    fn pack_of(t: &T, offset: f64) -> Vec<u8> {
        let mut b = b"RSCANPK1".to_vec();
        put_f(&mut b, 0.0); put_f(&mut b, 0.0);
        b.extend(1u32.to_le_bytes());
        put_str(&mut b, t.key);
        // the tile 100 x 40 m from x0, in a frame shifted by offset along x
        for (x, y) in [(t.x0, -20.0), (t.x0 + 100.0, -20.0), (t.x0 + 100.0, 20.0), (t.x0, 20.0)] { put_f(&mut b, x - offset); put_f(&mut b, y); }
        let n = 160usize;
        let (ox, oy) = ((t.x0 - offset) as i64 - 30, -80i64);
        b.extend((n as u32).to_le_bytes()); b.extend(ox.to_le_bytes()); b.extend(oy.to_le_bytes());
        for j in 0..n { for i in 0..n {
            let x = (ox + i as i64) as f64 + offset;
            let _ = j;
            let hump = if t.hump > 0.0 && (x - t.hump).abs() < 4.0 { 0.4 * (1.0 - (x - t.hump).abs() / 4.0) } else { 0.0 };
            b.extend((10.0f32 + hump as f32).to_le_bytes());
        } }
        for j in 0..n { for i in 0..n {
            let h = t.hole.map_or(false, |(hx, hy)| (ox + i as i64 - (hx - offset as i64)).abs() <= 1 && (oy + j as i64 - hy).abs() <= 1);
            b.push(h as u8);
        } }
        for _ in 0..n * n { b.push(1); }
        let org = [t.x0 - offset, 0.0, 10.0];
        for v in org { put_f(&mut b, v); }
        b.extend((t.tris.len() as u32).to_le_bytes());
        for (kind, flags, coll, p) in &t.tris {
            b.push(*kind); b.push(*coll as u8); b.extend(1u32.to_le_bytes()); b.push(*flags);
            for v in p { for (c, x) in [v[0] - offset, v[1], v[2]].iter().enumerate() { b.extend(((x - org[c]) as f32).to_le_bytes()); } }
        }
        // one two-lane ground line along y = 0 and the deck's bridge line
        b.extend(2u32.to_le_bytes());
        for (id, level, xa, xb) in [("w1", 0u8, -200.0, 400.0), ("w2", 1u8, 120.0, 150.0)] {
            put_str(&mut b, id); b.push(level); put_f(&mut b, 8.0); put_f(&mut b, 7.0); b.push(2); b.push(0); b.push(1);
            put_str(&mut b, "primary"); b.extend(2u32.to_le_bytes());
            put_f(&mut b, xa - offset); put_f(&mut b, 0.0); put_f(&mut b, xb - offset); put_f(&mut b, 0.0);
        }
        b
    }

    #[test]
    fn synthetic_corridor() {
        let dir = std::env::temp_dir().join(format!("roadscan-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let skin = |x0: f64, x1: f64| -> Vec<(u8, u8, bool, [[f64; 3]; 3])> {
            quad_tris(x0, x1, -4.0, 4.0, 10.01, 10.01).into_iter().map(|p| (KIND_ROAD, 0, false, p)).collect()
        };
        let mut a = T { key: "g17_10_20", x0: 0.0, tris: skin(0.0, 100.0), hole: Some((40, 0)), hump: 70.0 };
        // tile b: a deck from x 120 to 150 at z 10.0 -> 11 (ramp 120..130), flat, ending 0.2 m over the ground at 150
        let mut b = T { key: "g17_11_20", x0: 100.0, tris: skin(100.0, 200.0), hole: None, hump: 0.0 };
        for p in quad_tris(120.0, 130.0, -4.0, 4.0, 10.0, 10.2).into_iter().chain(quad_tris(130.0, 150.0, -4.0, 4.0, 10.2, 10.2)) {
            b.tris.push((KIND_DECK, FLAG_DECK_TOP, true, p));
        }
        a.tris.shrink_to_fit();
        let (pa, pb) = (pack_of(&a, 0.0), pack_of(&b, 0.2));
        std::fs::write(dir.join("a.pack"), pa).unwrap();
        std::fs::write(dir.join("b.pack"), pb).unwrap();
        // the index: b's frame is a's shifted 0.2 m along x (placements both ways)
        let mut ix = b"RSCANIX1".to_vec();
        ix.extend(2u32.to_le_bytes());
        for p in ["a.pack", "b.pack"] { put_str(&mut ix, p); put_f(&mut ix, 0.0); put_f(&mut ix, 0.0); }
        ix.extend(2u32.to_le_bytes());
        let m = 1.0 / 111320.0;
        for (k, key, off) in [(0u32, "g17_10_20", 0.0), (1, "g17_11_20", 0.2)] {
            put_str(&mut ix, key); ix.extend(k.to_le_bytes()); ix.extend(0u32.to_le_bytes());
            for v in [40.0, 0.0, m, -75.0, m * 1.3, 0.0] { put_f(&mut ix, v + if v == -75.0 { off * m * 1.3 } else { 0.0 }); }
        }
        ix.extend(2u32.to_le_bytes());
        for (s, d, tx) in [(0u32, 1u32, -0.2), (1, 0, 0.2)] {
            ix.extend(s.to_le_bytes()); ix.extend(d.to_le_bytes());
            for v in [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, tx, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] { put_f(&mut ix, v); }
        }
        std::fs::write(dir.join("index.bin"), ix).unwrap();
        let index = pack::read_index(&dir.join("index.bin")).unwrap();
        let mut packs = scene::Packs::new(&index);
        let (ta, _) = scan_tile(&mut packs, 0).unwrap();
        let (tb, _) = scan_tile(&mut packs, 1).unwrap();
        let recs: Vec<rank::Rec> = ta.lines().chain(tb.lines()).filter_map(rank::parse_rec).collect();
        let issues = rank::rank(&recs, None, 40.0);
        let kinds: Vec<(&str, &str)> = issues.iter().map(|i| (i.kind, i.tile.as_str())).collect();
        // the hole in tile a; the hump (a crest at 50 mph) in tile a; the deck's 0.2 m end step in tile b
        assert!(kinds.contains(&("hole", "g17_10_20")), "{:?}", kinds);
        assert!(kinds.iter().any(|k| (k.0 == "crest" || k.0 == "dip") && k.1 == "g17_10_20"), "{:?}", kinds);
        let ends: Vec<&rank::Issue> = issues.iter().filter(|i| i.kind == "deck_end_step").collect();
        assert!(!ends.is_empty(), "{:?}", kinds);
        assert!(ends.iter().all(|i| i.tile == "g17_11_20" && (i.mag.abs() - 0.2).abs() < 0.03), "{:?}", ends);
        // nothing at the seam between the two frames (0.2 m apart, placed): no steps or holes near x = 100
        assert!(!recs.iter().any(|r| (r.code == "BSTEP" || r.code == "HOLE") && ((r.lon + 75.0) / (m * 1.3) - 100.0).abs() < 3.0), "{:?}", recs);
        // the same output twice (the scan is a pure function of its inputs)
        let mut packs2 = scene::Packs::new(&index);
        assert_eq!(scan_tile(&mut packs2, 1).unwrap().0, tb);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
