//! roadsys CLI: the PT3 prototype runner over exported place files, and the M0 audit (tools/widecorpus/checks.py).
//!
//!   roadsys PLACE.bin OUTDIR [--reverse] [--repeat N] [--cold] [--cold-repeat N] [--halo M] [--tag NAME]
//!   roadsys audit IN.bin OUT.json
//!
//! Place mode writes OUTDIR/<name><tag>.json (timings, stats, checks, tile hashes), OUTDIR/<name><tag>.tiles (every
//! tile's canonical bytes), OUTDIR/<name><tag>.plan (the place's faces and markings for the plan views).
use roadsys::*;
use roadsys::cpu_now;

use std::fmt::Write as _;

const TILE_M: f64 = 256.0;

fn pct(v: &mut Vec<f64>, q: f64) -> f64 {
    if v.is_empty() { return 0.0; }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let k = ((v.len() as f64 - 1.0) * q).round() as usize;
    v[k.min(v.len() - 1)]
}

fn stat3(name: &str, v: &[f64]) -> String {
    let mut w = v.to_vec();
    let (p50, p95) = (pct(&mut w, 0.5), pct(&mut w, 0.95));
    let mx = w.last().copied().unwrap_or(0.0);
    format!("\"{}\":{{\"p50_ms\":{:.3},\"p95_ms\":{:.3},\"max_ms\":{:.3}}}", name, p50 * 1e3, p95 * 1e3, mx * 1e3)
}

struct Run {
    net: net::Net,
    lay: part::Layout,
    prof: prof::Profiles,
    built: part::Built,
    marks: mark::Marks,
    mark_cpu: Vec<f64>,
    t_net: f64,
    t_lay: f64,
    t_prof: f64,
    t_mesh: f64,
    t_mark: f64,
}

fn run_all(place: &io::Place, order: &[usize], sel_box: Option<[f64; 4]>) -> Run {
    let t0 = cpu_now();
    let mut net = net::build_opts(place, order, net::Opts { areas: !place.areas.is_empty(), ..Default::default() });
    let t1 = cpu_now();
    let lay = part::layout(&mut net);
    let t2 = cpu_now();
    let sel: Option<Vec<bool>> = sel_box.map(|b| net.strokes.iter().map(|st| st.line.p.iter().any(|p| p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3])
        || st.line.p.windows(2).any(|w| seg_box(w[0], w[1], b))).collect());
    let dem = &place.dem;
    let prof = prof::solve(&net, &|pts: &[geom::P2]| pts.iter().map(|p| dem.at(*p)).collect(), sel.as_deref());
    let t3 = cpu_now();
    let built = part::mesh(&net, &lay, &prof, sel.as_deref());
    let t4 = cpu_now();
    let (marks, mark_cpu) = mark::marks(&net, &built.mesh, &built.rows, &place.style, sel.as_deref());
    let t5 = cpu_now();
    Run { net, lay, prof, built, marks, mark_cpu, t_net: t1 - t0, t_lay: t2 - t1, t_prof: t3 - t2, t_mesh: t4 - t3, t_mark: t5 - t4 }
}

fn seg_box(a: geom::P2, b: geom::P2, bx: [f64; 4]) -> bool {
    // a segment crossing the box with no vertex inside it
    let (x0, x1) = (a[0].min(b[0]), a[0].max(b[0]));
    let (y0, y1) = (a[1].min(b[1]), a[1].max(b[1]));
    if x1 < bx[0] || x0 > bx[2] || y1 < bx[1] || y0 > bx[3] { return false; }
    let c = [[bx[0], bx[1]], [bx[2], bx[1]], [bx[2], bx[3]], [bx[0], bx[3]]];
    (0..4).any(|k| geom::seg_x(a, b, c[k], c[(k + 1) % 4]).is_some())
}

/// (tools) the deck-end skew audit's lines (main's "endaudit").
fn endaudit(place: &io::Place) {
    use geom::*;
    let lines: Vec<Line> = place.segs.iter().map(|s| Line::new(s.pts.clone())).collect();
    // every mapped bridge range's ends (a joint: another bridge range's end within 0.75 m)
    let mut ends: Vec<(usize, f64, bool, P2)> = Vec::new();     // (seg, arc, at the range's start, point)
    for (i, s) in place.segs.iter().enumerate() {
        let l = lines[i].len();
        if s.pts.len() < 2 || l < 1.0 { continue; }
        for &(a, b) in &s.bridge {
            ends.push((i, a * l, true, lines[i].at(a * l).0));
            ends.push((i, b * l, false, lines[i].at(b * l).0));
        }
    }
    for (k, &(i, t, start, p)) in ends.iter().enumerate() {
        let joint = ends.iter().enumerate().any(|(k2, e)| k2 != k && norm(sub(e.3, p)) <= 0.75);
        let s = &place.segs[i];
        let ln = &lines[i];
        let (rs, re) = s.bridge.iter().map(|&(a, b)| (a * ln.len(), b * ln.len())).find(|&(a, b)| if start { (a - t).abs() < 1e-9 } else { (b - t).abs() < 1e-9 }).unwrap();
        let span = re - rs;
        // the inward polyline: from the end along the range, 80 m at most
        let reach_max = 80.0f64.min(span);
        let mut pts: Vec<P2> = Vec::new();
        let nstep = (reach_max / 1.0).ceil().max(1.0) as usize;
        for j in 0..=nstep {
            let a = reach_max * j as f64 / nstep as f64;
            pts.push(ln.at(if start { t + a } else { t - a }).0);
        }
        let inward = unit(sub(pts[pts.len().min(6) - 1], pts[0])).unwrap_or([1.0, 0.0]);
        let mut best: Option<(f64, f64, String, String, f64, P2)> = None;  // (reach, sin, kind, class, crossed width, crossed dir)
        let mut test = |q: &[P2], kind: &str, cls: &str, w: f64, on_struct: &dyn Fn(f64) -> bool, qlen: f64| {
            let mut acc = 0.0;
            for j in 0..q.len().saturating_sub(1) {
                let (q0, q1) = (q[j], q[j + 1]);
                let ql = norm(sub(q1, q0));
                for m in 0..pts.len() - 1 {
                    if let Some((u, v)) = seg_x(pts[m], pts[m + 1], q0, q1) {
                        if on_struct((acc + v * ql) / qlen.max(1e-9)) { continue; }
                        let reach = reach_max * (m as f64 + u) / nstep as f64;
                        if reach <= 1.0 { continue; }
                        let md = unit(sub(pts[m + 1], pts[m])).unwrap_or(inward);
                        let cd = unit(sub(q1, q0)).unwrap_or([1.0, 0.0]);
                        let sn = cross(md, cd).abs();
                        if sn < 0.2588 { continue; }      // (along it: under 15 deg)
                        if best.as_ref().is_none_or(|b| reach < b.0) { best = Some((reach, sn, kind.to_string(), cls.to_string(), w, cd)); }
                    }
                }
                acc += ql;
            }
        };
        for (o, so) in place.segs.iter().enumerate() {
            if o == i || so.fid == s.fid || so.pts.len() < 2 { continue; }
            let ol = lines[o].len();
            let br: Vec<(f64, f64)> = so.bridge.iter().chain(so.tunnel.iter()).copied().collect();
            let in_tunnel: Vec<(f64, f64)> = so.tunnel.clone();
            let on = move |f: f64| br.iter().any(|&(a, b)| f >= a - 1e-6 && f <= b + 1e-6) && !in_tunnel.iter().any(|&(a, b)| f >= a && f <= b);
            test(&so.pts, "road", &so.cls_s, so.pave, &on, ol);
        }
        for (q, _clear) in &place.rails {
            let ql = Line::new(q.clone()).len();
            test(q, "rail", "rail", 0.0, &|_f: f64| false, ql);
        }
        let mut o = String::new();
        let _ = write!(o, "{{\"place\":[{:.7},{:.7}],\"fid\":\"{}\",\"cls\":\"{}\",\"p\":[{:.2},{:.2}],\"in\":[{:.5},{:.5}],\"pave\":{:.2},\"span\":{:.1},\"joint\":{}",
                       place.lat, place.lon, s.fid, s.cls_s, p[0], p[1], inward[0], inward[1], s.pave, span, joint);
        if let Some((reach, sn, kind, cls, w, cd)) = best {
            let _ = write!(o, ",\"x\":{{\"reach\":{:.2},\"sin\":{:.4},\"kind\":\"{}\",\"cls\":\"{}\",\"w\":{:.2},\"d\":{:.2},\"dir\":[{:.5},{:.5}]}}", reach, sn, kind, cls, w, reach * sn, cd[0], cd[1]);
        }
        o.push('}');
        println!("{}", o);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 4 && args[1] == "tile" {
        // a shadow tile's dumped input (ROADSYS_DUMP in the server) built again: OUTDIR/<tile>.json and .bin
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(1) });
        let t = io::read_tile(&b).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
        let used = std::cell::Cell::new(false);
        let g = |pts: &[geom::P2]| -> Vec<f64> {
            if pts.len() != t.ground.len() { eprintln!("ground: {} points asked, {} recorded", pts.len(), t.ground.len()); std::process::exit(1); }
            used.set(true);
            t.ground.clone()
        };
        let out = product::build(&t.place, t.quad, t.id, t.frame, &g, net::Opts { cut_grid: t.cut.0, cut_offset: t.cut.1, areas: t.areas_on, ..Default::default() });
        std::fs::create_dir_all(&args[3]).ok();
        let name = format!("{}_{}", t.id.0, t.id.1);
        std::fs::write(format!("{}/{}.json", args[3], name), &out.json).ok();
        std::fs::write(format!("{}/{}.bin", args[3], name), &out.bytes).ok();
        println!("{} {:016x} {}", name, out.hash, used.get());
        return;
    }
    if args.len() == 4 && args[1] == "square" {
        // a profile square's dumped input (roadsys_square dump=) solved again: OUTDIR/<i>_<j>.json and .sq
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(1) });
        let t = io::read_tile(&b).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
        // (the recorded ground heights, then the measured deck heights run by run)
        // (T1: the ground is asked more than once (the railway lines' ground, the tunnel record's side ground), each call
        // recorded after the last in the dump, the measured decks after them all: read in turn, the decks from the end)
        let ng = std::cell::Cell::new(0usize);
        let g = |pts: &[geom::P2]| -> Vec<f64> {
            let k = ng.get();
            if k + pts.len() > t.ground.len() { eprintln!("ground: {} points asked at {}, {} recorded", pts.len(), k, t.ground.len()); std::process::exit(1); }
            ng.set(k + pts.len());
            t.ground[k..k + pts.len()].to_vec()
        };
        let o = |runs: &[prof::ObsRun]| -> Vec<Vec<f64>> {
            let total: usize = runs.iter().map(|r| r.pts.len()).sum();
            let mut k = t.ground.len().saturating_sub(total);
            runs.iter().map(|r| { let n = r.pts.len(); let v = if k + n <= t.ground.len() { t.ground[k..k + n].to_vec() } else { vec![f64::NAN; n] }; k += n; v }).collect()
        };
        // (ROADSYS_TUNNELS=1: a square dumped with the tunnel record, BNE_ROADSYS_TUNNELS: replayed with it)
        let tunnels = std::env::var("ROADSYS_TUNNELS").map(|v| v == "1").unwrap_or(false);
        let out = square::solve_obs(&t.place, (t.id.0 as i64, t.id.1 as i64), &g, Some(&o), net::Opts { cut_grid: t.cut.0, cut_offset: t.cut.1, tunnels, ..Default::default() });
        std::fs::create_dir_all(&args[3]).ok();
        let name = format!("{}_{}", t.id.0, t.id.1);
        let bytes = out.to_bytes();
        std::fs::write(format!("{}/{}.json", args[3], name), &out.json).ok();
        std::fs::write(format!("{}/{}.cpu.json", args[3], name), &out.cpu_json).ok();
        std::fs::write(format!("{}/{}.sq", args[3], name), &bytes).ok();
        println!("{} {:016x} {:016x}", name, out.key, geom::fnv(&bytes));
        return;
    }
    if args.len() == 3 && args[1] == "handover" {
        // (M3 structures) a zone input file (BNGE_HANDOVER_DUMP's .rsho): its output's FNV-1a 64 and counts
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(2) });
        match roadsys::handover::run_input(&b) {
            Ok((drops, out, st)) => {
                let bytes = roadsys::handover::output_bytes(&drops, &out);
                println!("handover {:016x} {} {} {}", roadsys::geom::fnv(&bytes), out.t.len(), st.squares_h, st.squares_z);
                return;
            }
            Err(e) => { eprintln!("{}", e); std::process::exit(1) }
        }
    }
    if args.len() == 3 && args[1] == "ceil" {
        // (M5) an under-deck ceiling's input file (ROADSYS_CEIL_DUMP's .rsce): the FNV-1a 64 of its ceiling and drop on
        // a 0.5 m grid over its decks, the points and those with a ceiling
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(2) });
        match roadsys::ceil::run_input(&b) {
            Ok((bytes, n, hit)) => { println!("ceil {:016x} {} {}", roadsys::geom::fnv(&bytes), n, hit); return; }
            Err(e) => { eprintln!("{}", e); std::process::exit(1) }
        }
    }
    if args.len() == 3 && args[1] == "decks" {
        // (M4 surface) a decks input file (ROADSYS_DECK_DUMP's .rsdk): its output's FNV-1a 64 and counts
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(2) });
        match roadsys::deck::run_input(&b) {
            Ok((bytes, o)) => {
                println!("decks {:016x} {} {} {}", roadsys::geom::fnv(&bytes), o.t.len(), o.runs.len(), o.mt.len());
                // (tools: ROADSYS_DECKS_RUNS=FILE writes the parapet runs (tile frame) and the stats as JSON)
                if let Ok(path) = std::env::var("ROADSYS_DECKS_RUNS") {
                    let runs: Vec<String> = o.runs.iter().map(|r| format!("[{}]", r.p.iter().map(|q| format!("[{:.3},{:.3},{:.3}]", q[0], q[1], q[2])).collect::<Vec<_>>().join(","))).collect();
                    std::fs::write(&path, format!("{{\"stats\":{},\"runs\":[{}]}}", o.json, runs.join(","))).ok();
                }
                // (tools: ROADSYS_DECKS_FACES=FILE writes the faces as JSON {"faces": {v, t, k, f}}, decks_*.json's shape)
                if let Ok(path) = std::env::var("ROADSYS_DECKS_FACES") {
                    let v: Vec<String> = o.v.iter().map(|q| format!("[{:.3},{:.3},{:.3}]", q[0], q[1], q[2])).collect();
                    let t: Vec<String> = o.t.iter().map(|q| format!("[{},{},{}]", q[0], q[1], q[2])).collect();
                    let k: Vec<String> = o.kind.iter().map(|x| x.to_string()).collect();
                    let f: Vec<String> = o.feat.iter().map(|x| x.to_string()).collect();
                    std::fs::write(&path, format!("{{\"faces\":{{\"v\":[{}],\"t\":[{}],\"k\":[{}],\"f\":[{}]}}}}", v.join(","), t.join(","), k.join(","), f.join(","))).ok();
                }
                // (tools: ROADSYS_DECKS_AT=x,y,r lists the faces whose middle lies within r of (x, y): kind, feature, z)
                if let Ok(v) = std::env::var("ROADSYS_DECKS_AT") {
                    let f: Vec<f64> = v.split(',').filter_map(|x| x.parse().ok()).collect();
                    if f.len() == 3 {
                        for (i, t) in o.t.iter().enumerate() {
                            let c = [(o.v[t[0] as usize][0] + o.v[t[1] as usize][0] + o.v[t[2] as usize][0]) / 3.0,
                                     (o.v[t[0] as usize][1] + o.v[t[1] as usize][1] + o.v[t[2] as usize][1]) / 3.0];
                            let z = (o.v[t[0] as usize][2] + o.v[t[1] as usize][2] + o.v[t[2] as usize][2]) / 3.0;
                            if (c[0] - f[0]).hypot(c[1] - f[1]) <= f[2] {
                                let fe = if o.feat[i] >= 0 { o.feats[o.feat[i] as usize].as_str() } else { "-" };
                                println!("face {:.2} {:.2} kind {} feat {} z {:.3}", c[0], c[1], o.kind[i], fe, z);
                            }
                        }
                    }
                }
                return;
            }
            Err(e) => { eprintln!("{}", e); std::process::exit(1) }
        }
    }
    if args.len() == 3 && args[1] == "fit" {
        // (BNE_ROADSYS_AREAS) a tile input's (ROADSYS dump, RSTILE03 / 04) lane graph with its mapped areas' fit: FNV-1a
        // 64 of every stroke's area program and of its boundary offsets every 5 m, the fit's counts and the audit's hash
        // (the program's strip factor hashed only with the cross-section model on: RSTILE03 hashes as before)
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(1) });
        let t = io::read_tile(&b).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
        let order: Vec<usize> = (0..t.place.segs.len()).collect();
        let net = net::build_opts(&t.place, &order, net::Opts { cut_grid: t.cut.0, cut_offset: t.cut.1, areas: true, ..Default::default() });
        let mut bytes: Vec<u8> = Vec::new();
        let mut off = Vec::new();
        for st in &net.strokes {
            bytes.extend_from_slice(&st.uid.to_le_bytes());
            let nk = if t.place.xsec { 5 } else { 4 };
            for (s, k) in &st.area { bytes.extend_from_slice(&s.to_bits().to_le_bytes()); for v in &k[..nk] { bytes.extend_from_slice(&v.to_bits().to_le_bytes()); } }
            let n = (st.len() / 5.0).floor() as usize;
            for i in 0..=n { st.offsets((i as f64 * 5.0).min(st.len()), &mut off); for v in &off { bytes.extend_from_slice(&v.to_bits().to_le_bytes()); } }
        }
        let f = &net.area_fit;
        println!("fit {:016x} strokes {} fitted {} knots {} valid {}", geom::fnv(&bytes), f.strokes, f.fitted, f.knots, f.valid);
        return;
    }
    if args.len() == 4 && args[1] == "areas" {
        // the mapped road areas' audit of a surface (tools/widecorpus/checks.py, areas.rs run_input): IN.bin -> OUT.json
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(1) });
        match areas::run_input(&b) {
            Ok(j) => { std::fs::write(&args[3], j).unwrap_or_else(|e| { eprintln!("{}: {}", args[3], e); std::process::exit(1) }); }
            Err(e) => { eprintln!("{}", e); std::process::exit(1); }
        }
        return;
    }
    if args.len() == 3 && args[1] == "endaudit" {
        // (tools: the deck-end skew audit) a decks input file's (.rsdk) mapped bridge ends, one JSON line each: the
        // end (place frame), the deck's inward direction, its paved width, and the nearest road or railway crossing
        // under it within 80 m inward (its reach along the deck, the crossing's sine, the crossed paved width, class)
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(2) });
        let n = u64::from_le_bytes(b[8..16].try_into().unwrap()) as usize;
        let t = io::read_tile(&b[16..16 + n]).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
        endaudit(&t.place);
        return;
    }
    if args.len() == 4 && args[1] == "audit" {
        // the M0 audit (tools/widecorpus/checks.py): IN.bin -> OUT.json
        let b = std::fs::read(&args[2]).unwrap_or_else(|e| { eprintln!("{}: {}", args[2], e); std::process::exit(1) });
        match audit::run(&b) {
            Ok(j) => { std::fs::write(&args[3], j).unwrap_or_else(|e| { eprintln!("{}: {}", args[3], e); std::process::exit(1) }); }
            Err(e) => { eprintln!("{}", e); std::process::exit(1); }
        }
        return;
    }
    if args.len() < 3 { eprintln!("usage: roadsys PLACE.bin OUTDIR [--reverse] [--repeat N] [--cold] [--cold-repeat N] [--halo M] [--tag NAME]"); std::process::exit(2); }
    let mut place = io::read(&args[1]).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
    let outdir = &args[2];
    let flag = |f: &str| args.iter().any(|a| a == f);
    // (BNE_ROADSYS_XSEC) a format-4 place holds the cross-section tags and builds with the model; --no-xsec without
    if flag("--no-xsec") { place.xsec = false; for s in place.segs.iter_mut() { s.xsec.clear(); } }
    // (BNE_ROADSYS_AREAS) --areas FILE: mapped road areas in the place's frame (an RSTILE03 tile input's areas block
    // after "RSAREAS1": build, count, areas) and the fit on (the width checks; plan views)
    let areas_on = if let Some(i) = args.iter().position(|a| a == "--areas") {
        let b = std::fs::read(&args[i + 1]).unwrap_or_else(|e| { eprintln!("{}: {}", args[i + 1], e); std::process::exit(1) });
        let (build, areas) = io::read_areas(&b).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
        place.area_build = build;
        place.areas = areas;
        true
    } else { false };
    let val = |f: &str, d: f64| args.iter().position(|a| a == f).and_then(|i| args.get(i + 1)).and_then(|v| v.parse::<f64>().ok()).unwrap_or(d);
    let tag = args.iter().position(|a| a == "--tag").and_then(|i| args.get(i + 1)).cloned().unwrap_or_default();
    let reverse = flag("--reverse");
    let repeat = val("--repeat", 1.0) as usize;
    let cold = flag("--cold");
    let cold_repeat = val("--cold-repeat", 1.0) as usize;
    let halo = val("--halo", 400.0);
    std::fs::create_dir_all(outdir).ok();
    let mut order: Vec<usize> = (0..place.segs.len()).collect();
    if reverse { order.reverse(); }
    let core = place.core;
    let nx = (2.0 * core / TILE_M).round() as i32;
    let (x0, y0) = (-core, -core);
    if let Some(i) = args.iter().position(|a| a == "--debug-near") {
        let x: f64 = args[i + 1].parse().unwrap(); let y: f64 = args[i + 2].parse().unwrap(); let rr: f64 = args[i + 3].parse().unwrap();
        let mut net = net::build(&place, &order);
        let _lay = part::layout(&mut net);
        let mut off = Vec::new();
        for (si, st) in net.strokes.iter().enumerate() {
            let (s, d) = st.line.project([x, y], 0.0, f64::INFINITY);
            if d.abs() > rr { continue; }
            st.offsets(s, &mut off);
            let seg = &net.segs[net.edges[st.parts[0].edge].seg];
            println!("stroke {} len {:.0} s {:.1} d {:.1} oneway {} lht {} cls {} parts {} cut {:.1}/{:.1} snap0 {:?} snap1 {:?} fid0 {} lanes {} lfw {} lbw {} place '{}' turn '{}'",
                     si, st.len(), s, d, st.oneway, st.lht, st.cls, st.parts.len(), st.cut0, st.cut1, st.snap0, st.snap1, seg.fid, seg.lanes, seg.lfw, seg.lbw, seg.place, seg.turn);
            println!("   anchor_l {:?} right_windows {:?} lane_scale_n {}", st.anchor_l, st.right_windows, st.lane_scale.len());
            for (k, sl) in st.slots.iter().enumerate() { println!("   slot {} kind {} grp {} w {:.2} alive {:?} prog {:?} caps {}", k, sl.kind, sl.group, sl.w(s), sl.alive, sl.prog, sl.cap.len()); }
            println!("   offsets {:?}", off.iter().map(|v| (v * 100.0).round() / 100.0).collect::<Vec<_>>());
        }
        return;
    }
    // ---- place mode (canonical products for the whole place, then tiles)
    let mut best: Option<Run> = None;
    let mut tmin = [f64::INFINITY; 5];
    for _ in 0..repeat.max(1) {
        let r = run_all(&place, &order, None);
        let ts = [r.t_net, r.t_lay, r.t_prof, r.t_mesh, r.t_mark];
        for k in 0..5 { if ts[k] < tmin[k] { tmin[k] = ts[k]; } }
        if best.is_none() { best = Some(r); }
    }
    let r = best.unwrap();
    // tiles (in reverse order with --reverse)
    let tb = tile::buckets(&r.built.mesh.v, &r.built.mesh.t, x0, y0, TILE_M, nx, nx);
    let mb = tile::buckets(&r.marks.v, &r.marks.t, x0, y0, TILE_M, nx, nx);
    let mut ids: Vec<(i32, i32)> = (0..nx).flat_map(|j| (0..nx).map(move |i| (i, j))).collect();
    if reverse { ids.reverse(); }
    let mut tiles: Vec<tile::TileOut> = Vec::new();
    let mut derived: Vec<((i32, i32), derive::Derived)> = Vec::new();
    for &(i, j) in &ids {
        let k = (j * nx + i) as usize;
        let mut best_t: Option<tile::TileOut> = None;
        for _ in 0..repeat.max(1) {
            let t = tile::tile(&r.built.mesh, &tb[k], &r.marks, &mb[k], i, j, x0, y0, TILE_M);
            if best_t.as_ref().map(|b| t.cpu < b.cpu).unwrap_or(true) { best_t = Some(t); }
        }
        let mut t = best_t.unwrap();
        let bx = [geom::mm(x0 + TILE_M * i as f64), geom::mm(y0 + TILE_M * j as f64), geom::mm(x0 + TILE_M * (i + 1) as f64), geom::mm(y0 + TILE_M * (j + 1) as f64)];
        let mut best_d: Option<derive::Derived> = None;
        for _ in 0..repeat.max(1) {
            let dd = derive::derive(&r.built.mesh, &tb[k], bx);
            if best_d.as_ref().map(|b| dd.cpu < b.cpu).unwrap_or(true) { best_d = Some(dd); }
        }
        let dd = best_d.unwrap();
        t.hash ^= dd.hash.rotate_left(17);
        derived.push(((i, j), dd));
        tiles.push(t);
    }
    tiles.sort_by_key(|t| (t.j, t.i));
    derived.sort_by_key(|x| (x.0 .1, x.0 .0));
    // per-tile amortised attribution: each stroke's work by its length share per tile; zones by centre;
    // components by their strokes' length shares; G1 + layout by the strokes' length shares of the place totals
    let ns = r.net.strokes.len();
    let ntile = (nx * nx) as usize;
    let mut share: Vec<Vec<(usize, f64)>> = vec![Vec::new(); ns];
    let mut tot_len = 0.0;
    let mut tile_len = vec![0.0; ntile];
    for (si, st) in r.net.strokes.iter().enumerate() {
        let len = st.len();
        let n = ((len / 5.0).ceil() as usize).max(1);
        let mut cnt: std::collections::BTreeMap<usize, f64> = Default::default();
        for k in 0..n {
            let (p, _) = st.line.at((k as f64 + 0.5) * len / n as f64);
            let (i, j) = (((p[0] - x0) / TILE_M).floor() as i32, ((p[1] - y0) / TILE_M).floor() as i32);
            if i < 0 || j < 0 || i >= nx || j >= nx { continue; }
            *cnt.entry((j * nx + i) as usize).or_default() += 1.0 / n as f64;
        }
        for (&t, &f) in &cnt { share[si].push((t, f)); tile_len[t] += f * len; tot_len += f * len; }
    }
    // all strokes' length (incl. the halo) for the G1 / layout totals
    let all_len: f64 = r.net.strokes.iter().map(|s| s.len()).sum();
    let mut comp_len: std::collections::BTreeMap<usize, f64> = Default::default();
    for (si, st) in r.net.strokes.iter().enumerate() { *comp_len.entry(r.prof.comp_of[si]).or_default() += st.len(); }
    let mut t_g1 = vec![0.0; ntile];
    let mut t_g3 = vec![0.0; ntile];
    let mut t_g4 = vec![0.0; ntile];
    let mut t_mk = vec![0.0; ntile];
    let mut t_g6 = vec![0.0; ntile];
    for t in 0..ntile { t_g1[t] = (tmin[0] + tmin[1]) * tile_len[t] / all_len.max(1.0); }
    for (si, st) in r.net.strokes.iter().enumerate() {
        let cl = comp_len[&r.prof.comp_of[si]].max(1e-9);
        for &(t, f) in &share[si] {
            t_g3[t] += r.prof.comp_cpu[si] * f * st.len() / cl;
            t_g4[t] += r.built.stroke_cpu[si] * f;
            t_mk[t] += r.mark_cpu[si] * f;
        }
    }
    for (c, nodes) in r.lay.clusters.iter().enumerate() {
        let _ = nodes;
        let p = r.lay.centre[c];
        let (i, j) = (((p[0] - x0) / TILE_M).floor() as i32, ((p[1] - y0) / TILE_M).floor() as i32);
        if i < 0 || j < 0 || i >= nx || j >= nx { continue; }
        t_g4[(j * nx + i) as usize] += r.built.zone_cpu[c];
    }
    for t in &tiles { t_g6[(t.j * nx + t.i) as usize] = t.cpu; }
    let mut t_g5 = vec![0.0; ntile];
    for ((i, j), dd) in &derived { t_g5[(j * nx + i) as usize] = dd.cpu; }
    let t_tot: Vec<f64> = (0..ntile).map(|t| t_g1[t] + t_g3[t] + t_g4[t] + t_mk[t] + t_g5[t] + t_g6[t]).collect();
    // checks
    let chk = check::run(&r.net, &r.built.mesh, core, &tiles, nx);
    // ---- cold mode: each tile alone, from the segments within halo of it
    let mut cold_json = String::new();
    if cold {
        let mut c_g1 = Vec::new(); let mut c_g3 = Vec::new(); let mut c_g4 = Vec::new(); let mut c_mk = Vec::new(); let mut c_g6 = Vec::new(); let mut c_tot = Vec::new();
        let mut c_hash = String::new();
        let mut c_segs = Vec::new();
        for &(i, j) in &ids {
            let bx = [x0 + TILE_M * i as f64, y0 + TILE_M * j as f64, x0 + TILE_M * (i + 1) as f64, y0 + TILE_M * (j + 1) as f64];
            let hb = [bx[0] - halo, bx[1] - halo, bx[2] + halo, bx[3] + halo];
            let sub: Vec<usize> = order.iter().copied().filter(|&g| place.segs[g].pts.iter().any(|p| p[0] >= hb[0] && p[0] <= hb[2] && p[1] >= hb[1] && p[1] <= hb[3])).collect();
            c_segs.push(sub.len() as f64);
            let mut best = [f64::INFINITY; 6];
            let mut hash = 0u64;
            for _ in 0..cold_repeat.max(1) {
                let rr = run_all(&place, &sub, Some([bx[0] - 10.0, bx[1] - 10.0, bx[2] + 10.0, bx[3] + 10.0]));
                let all: Vec<u32> = (0..rr.built.mesh.t.len() as u32).collect();
                let allm: Vec<u32> = (0..rr.marks.t.len() as u32).collect();
                let t = tile::tile(&rr.built.mesh, &all, &rr.marks, &allm, i, j, x0, y0, TILE_M);
                let bxm = [geom::mm(bx[0]), geom::mm(bx[1]), geom::mm(bx[2]), geom::mm(bx[3])];
                let dd = derive::derive(&rr.built.mesh, &all, bxm);
                hash = t.hash ^ dd.hash.rotate_left(17);
                let v = [rr.t_net + rr.t_lay, rr.t_prof, rr.t_mesh, rr.t_mark + dd.cpu, t.cpu];
                let tot: f64 = v.iter().sum();
                if tot < best[5] { best = [v[0], v[1], v[2], v[3], v[4], tot]; }
            }
            c_g1.push(best[0]); c_g3.push(best[1]); c_g4.push(best[2]); c_mk.push(best[3]); c_g6.push(best[4]); c_tot.push(best[5]);
            let _ = write!(c_hash, "{}\"{}_{}\":\"{:016x}\"", if c_hash.is_empty() { "" } else { "," }, i, j, hash);
        }
        cold_json = format!(",\"cold\":{{\"halo_m\":{},{},{},{},{},{},{},\"segs_mean\":{:.0},\"hashes\":{{{}}}}}",
                            halo, stat3("g1_network", &c_g1), stat3("g3_profile", &c_g3), stat3("g4_surface", &c_g4),
                            stat3("markings_g5", &c_mk), stat3("g6_clip", &c_g6), stat3("total", &c_tot),
                            c_segs.iter().sum::<f64>() / c_segs.len().max(1) as f64, c_hash);
    }
    // ---- outputs
    let name = format!("{}{}", place.name, tag);
    let mut all_bytes = Vec::new();
    let mut hashes = String::new();
    for t in &tiles {
        all_bytes.extend_from_slice(&(t.bytes.len() as u64).to_le_bytes());
        all_bytes.extend_from_slice(&t.bytes);
        let _ = write!(hashes, "{}\"{}_{}\":\"{:016x}\"", if hashes.is_empty() { "" } else { "," }, t.i, t.j, t.hash);
    }
    let place_hash = geom::fnv(&all_bytes);
    std::fs::write(format!("{}/{}.tiles", outdir, name), &all_bytes).ok();
    // plan file: f32 x y, per tri kind/sup, marks
    let mut pl: Vec<u8> = Vec::new();
    let m = &r.built.mesh;
    pl.extend_from_slice(&(m.v.len() as u32).to_le_bytes());
    for v in &m.v { pl.extend_from_slice(&((v[0] as f64 / 1000.0) as f32).to_le_bytes()); pl.extend_from_slice(&((v[1] as f64 / 1000.0) as f32).to_le_bytes()); pl.extend_from_slice(&((v[2] as f64 / 1000.0) as f32).to_le_bytes()); }
    pl.extend_from_slice(&(m.t.len() as u32).to_le_bytes());
    for (t, g) in m.t.iter().zip(&m.tag) { for &v in t { pl.extend_from_slice(&v.to_le_bytes()); } pl.push(g.kind); pl.push(g.sup); pl.extend_from_slice(&g.piece.to_le_bytes()); pl.extend_from_slice(&g.slot.to_le_bytes()); }
    let mk = &r.marks;
    pl.extend_from_slice(&(mk.v.len() as u32).to_le_bytes());
    for v in &mk.v { pl.extend_from_slice(&((v[0] as f64 / 1000.0) as f32).to_le_bytes()); pl.extend_from_slice(&((v[1] as f64 / 1000.0) as f32).to_le_bytes()); }
    pl.extend_from_slice(&(mk.t.len() as u32).to_le_bytes());
    for (t, &c) in mk.t.iter().zip(&mk.colour) { for &v in t { pl.extend_from_slice(&v.to_le_bytes()); } pl.push(c); }
    // overlap examples
    pl.extend_from_slice(&(chk.overlap_examples.len() as u32).to_le_bytes());
    for (x, y, a, _c) in &chk.overlap_examples { pl.extend_from_slice(&(*x as f32).to_le_bytes()); pl.extend_from_slice(&(*y as f32).to_le_bytes()); pl.extend_from_slice(&(*a as f32).to_le_bytes()); }
    pl.extend_from_slice(&(chk.holes.len() as u32).to_le_bytes());
    for h in &chk.holes { pl.extend_from_slice(&(h[0] as f32).to_le_bytes()); pl.extend_from_slice(&(h[1] as f32).to_le_bytes()); }
    std::fs::write(format!("{}/{}.plan", outdir, name), &pl).ok();
    // (BNE_ROADSYS_XSEC) --widths: the strokes' cross-sections every 10 m (away from their ends), for the width checks
    if let Some(i) = args.iter().position(|a| a == "--widths") {
        let mut w = String::from("stroke,fid,s,x,y,nx,ny,left,right,lanes,strips,shoulders,cls,urban,xsec,tags\n");
        let mut off = Vec::new();
        for (si, st) in r.net.strokes.iter().enumerate() {
            if st.absorbed { continue; }
            let len = st.len();
            let mut s = 15.0;
            while s < len - 15.0 {
                let part = st.parts.iter().find(|p| s >= p.s0 && s <= p.s1).copied().unwrap_or(st.parts[0]);
                let seg = &r.net.segs[r.net.edges[part.edge].seg];
                st.offsets(s, &mut off);
                let (p, n, _k) = st.row_frame(s, (off[0] - off[off.len() - 1]) / 2.0);
                let (mut lanes, mut strips, mut sh) = (0.0, 0.0, 0.0);
                for (k, sl) in st.slots.iter().enumerate() {
                    let v = off[k] - off[k + 1];
                    match sl.kind { net::K_LANE => lanes += v, net::K_SHOULDER => sh += v, xsec::K_PARK | xsec::K_CYCLE | xsec::K_BUFFER => strips += v, _ => {} }
                }
                let g = r.net.edges[part.edge].seg;
                let _ = write!(w, "{},{},{:.1},{:.3},{:.3},{:.5},{:.5},{:.3},{:.3},{:.3},{:.3},{:.3},{},{},{},{}\n", si, seg.fid.split('|').next().unwrap_or(""), s, p[0], p[1], n[0], n[1],
                               off[0], off[off.len() - 1], lanes, strips, sh, seg.cls_s, seg.urban as u8, !seg.xsec.is_empty() as u8, r.net.xs_tags.get(g).copied().unwrap_or(0));
                s += 10.0;
            }
        }
        std::fs::write(&args[i + 1], w).ok();
    }
    let st = &r.net.stats;
    let ps = &r.prof.stats;
    let bs = &r.built.stats;
    let mut tri_tile: Vec<f64> = tiles.iter().map(|t| t.tris as f64).collect();
    let mut vert_tile: Vec<f64> = tiles.iter().map(|t| t.verts as f64).collect();
    let json = format!(
        "{{\"place\":\"{}\",\"tag\":\"{}\",\"reverse\":{},\"tiles\":{},\"tile_m\":{},\"place_hash\":\"{:016x}\",\
         \"place_cpu_s\":{{\"g1_network\":{:.4},\"layout\":{:.4},\"g3_profile\":{:.4},\"g4_surface\":{:.4},\"markings\":{:.4},\"g6_clip_all\":{:.4}}},\
         \"amortised\":{{{},{},{},{},{},{},{}}},\
         \"xsec\":{{\"on\":{},\"segs\":{},\"shaped\":{},\"width\":{},\"reject\":{},\"fork_skip\":{}}},\
         \"areas\":{{\"on\":{},\"strokes\":{},\"fitted\":{},\"knots\":{},\"valid\":{}}},\
         \"net\":{{\"segs\":{},\"nodes\":{},\"edges\":{},\"strokes\":{},\"forks\":{},\"forks_at_grade\":{},\"forks_no_separation\":{},\"adds\":{},\"drops\":{},\"structures\":{},\"tunnels\":{},\"crossings\":{},\"unbridged\":{},\"nose_beyond\":{},\"floors_along\":{},\"km\":{:.1},\"km_core\":{:.1}}},\
         \"profile\":{{\"components\":{},\"tier_a\":{},\"tier_b\":{},\"knots\":{},\"biggest_knots\":{},\"biggest_km\":{:.1},\"rounds_max\":{},\"factor_fail\":{},\"floors\":{},\"floors_met\":{},\"floor_short_max_m\":{:.3},\"grade_viol\":{},\"comfort_viol\":{},\"couple_max_mm\":{:.2}}},\
         \"surface\":{{\"zones\":{},\"clusters\":{},\"cluster_cap\":{},\"absorbed\":{},\"zone_fallback_straight\":{},\"zone_fallback_hull\":{},\"zone_fail\":{},\"snap_ok\":{},\"snap_mismatch\":{},\"twin_capped\":{},\"twin_lane_overlap\":{},\"rows\":{},\"tris\":{},\"verts\":{},\"tris_per_tile_p50\":{},\"tris_per_tile_max\":{},\"verts_per_tile_p50\":{},\"verts_per_tile_max\":{},\"mark_pieces\":{},\"mark_fragments\":{}}},\
         \"checks\":{{\"tris_core\":{},\"paved_m2\":{:.0},\"lane_m2\":{:.0},\"overlap_pairs\":{},\"overlap_m2\":{:.1},\"overlap_lane_m2\":{:.1},\"overlap_same_piece_m2\":{:.1},\"overlap_strip_strip_m2\":{:.1},\"overlap_zone_strip_m2\":{:.1},\"overlap_zone_zone_m2\":{:.1},\"stacked_pairs\":{},\"stacked_struct_pairs\":{},\"hole_samples\":{},\"hole_uncovered\":{},\"nonmanifold_edges\":{},\"flipped_edges\":{},\"boundary_edges\":{},\"tjunctions\":{},\"zero_area\":{},\"folded\":{},\"seam_pairs\":{},\"seam_mismatch\":{}}},\
         \"derived\":{{\"ground_cells\":{},\"ceil_cells\":{},\"slab_tris\":{},\"parapet_edges\":{},\"collider_tris\":{},\"collider_tris_tile_max\":{}}},\"tile_hashes\":{{{}}}{}}}",
        place.name, tag, reverse, tiles.len(), TILE_M, place_hash,
        tmin[0], tmin[1], tmin[2], tmin[3], tmin[4], tiles.iter().map(|t| t.cpu).sum::<f64>(),
        stat3("g1_network", &t_g1), stat3("g3_profile", &t_g3), stat3("g4_surface", &t_g4), stat3("markings", &t_mk), stat3("g5_derived", &t_g5), stat3("g6_clip", &t_g6), stat3("total", &t_tot),
        r.net.xsec_on, st.xsec_segs, st.xsec_shaped, st.xsec_width, st.xsec_reject, st.xsec_fork_skip,
        areas_on, r.net.area_fit.strokes, r.net.area_fit.fitted, r.net.area_fit.knots, r.net.area_fit.valid,
        st.segs, st.nodes, st.edges, st.strokes, st.forks, st.forks_at_grade, st.forks_no_sep, st.adds, st.drops, st.structures, st.tunnels, st.crossings, st.unbridged, st.nose_beyond, st.along,
        all_len / 1000.0, tot_len / 1000.0,
        ps.components, ps.tier_a, ps.tier_b, ps.knots, ps.biggest_component_knots, ps.biggest_component_km, ps.rounds_max, ps.factor_fail, ps.crossings, ps.floors_met, ps.floor_short_max_m, ps.grade_viol, ps.comfort_viol, ps.couple_max_mm,
        bs.zones, bs.clusters, bs.cluster_cap, bs.absorbed, bs.zone_fallback_straight, bs.zone_fallback_hull, bs.zone_fail, bs.snap_ok, bs.snap_mismatch, bs.twin_capped, bs.twin_lane_overlap, bs.rows, bs.tris, bs.verts,
        pct(&mut tri_tile, 0.5), tri_tile.last().copied().unwrap_or(0.0), pct(&mut vert_tile, 0.5), vert_tile.last().copied().unwrap_or(0.0), r.marks.pieces, r.marks.fragments,
        chk.tris_core, chk.paved_m2, chk.lane_m2, chk.overlap_pairs, chk.overlap_m2, chk.overlap_lane_m2, chk.overlap_fold_m2, chk.overlap_strip_strip_m2, chk.overlap_zone_strip_m2, chk.overlap_zone_zone_m2, chk.stacked_pairs, chk.stacked_struct_pairs, chk.hole_samples, chk.hole_uncovered, chk.nonmanifold_edges, chk.flipped_edges, chk.boundary_edges, chk.tjunctions, chk.zero_area, chk.folded, chk.seam_pairs, chk.seam_mismatch,
        derived.iter().map(|x| x.1.ground_cells).sum::<usize>(), derived.iter().map(|x| x.1.ceil_cells).sum::<usize>(), derived.iter().map(|x| x.1.slab_tris).sum::<usize>(), derived.iter().map(|x| x.1.parapet_edges).sum::<usize>(), derived.iter().map(|x| x.1.collider_tris).sum::<usize>(), derived.iter().map(|x| x.1.collider_tris).max().unwrap_or(0),
        hashes, cold_json);
    std::fs::write(format!("{}/{}.json", outdir, name), &json).ok();
    println!("{}", json);
}
