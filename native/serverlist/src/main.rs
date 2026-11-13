//! BeamNG.Earth's list of BeamMP servers (the user, 2026-10-24: "show a list of all the .earth servers ... make it so
//! players without .earth cant see .earth server in the server list"). A BeamNG.Earth server is Private in BeamMP (off
//! BeamMP's public list, which every BeamMP player sees); its plugin announces it here every minute instead, and the
//! game's BeamNG.Earth tab reads the list.
//!
//! POST /announce  {"port": 30814, "name": "...", "players": 2, "maxPlayers": 8, "version": "3.9.0", "place": "...",
//!                 "desc", "tags", "map", "modlist", "modstotalsize", "modstotal", "playerslist", "pass"}: what BeamMP's own
//!                 heartbeat sends its list (BeamMP-Server THeartbeatThread), but never its key, and the address is not taken from it.
//!                 The address is the one the request comes from (behind a local reverse proxy: its X-Forwarded-For),
//!                 never the body's: a server can only list itself. Entries go TTL_S after their last announce.
//! GET  /servers   [{"ip", "port", "name", "players", "maxPlayers", "version", "place", "desc", "tags", "map", "modlist", "modstotalsize",
//!                 "modstotal", "playerslist", "pass", "location", "age"}], newest first.
//!
//! Each server's "location" is the country its address is in (as BeamMP's own list has it, for the flag): DB-IP's
//! IP to Country Lite table (CC BY 4.0, https://db-ip.com), downloaded next to the binary and fetched again weekly.
//!
//! Player reports from the game's reporter: reports.rs (POST /report; private GET /reports/list and
//! /reports/<id>/<file> with the token).
//!
//! In memory only (servers announce again within a minute of a restart). Env: BNGE_LIST_ADDR (default 0.0.0.0:47640),
//! BNGE_LIST_GEOIP (the table's path; default dbip-country-lite.csv.gz next to the binary).

mod perf;
mod reports;
mod telemetry;
mod uniques;

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::Read;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};
use tiny_http::{Header, Method, Request, Response, Server};

const TTL: Duration = Duration::from_secs(180);
const MIN_GAP: Duration = Duration::from_secs(20); // announces from one server closer than this are refused
const MAX_SERVERS: usize = 1000;
const MAX_PER_IP: usize = 8; // servers one address may list (one per port)
const MAX_BODY: usize = 16384; // (a long mod list)
const MAX_DESC: usize = 256; // a description's characters kept
const GEOIP_URL: &str = "https://download.db-ip.com/free/dbip-country-lite-{ym}.csv.gz";
const GEOIP_MAX_AGE: Duration = Duration::from_secs(7 * 86400); // the table is fetched again after this
const GEOIP_CHECK: Duration = Duration::from_secs(6 * 3600);

#[derive(Deserialize)]
struct Announce {
    port: u16,
    name: Option<String>,
    players: Option<u32>,
    #[serde(rename = "maxPlayers")]
    max_players: Option<u32>,
    version: Option<String>,
    place: Option<String>,
    desc: Option<String>, // ServerConfig.toml's Description (BeamMP's list shows it under the name)
    tags: Option<String>,
    map: Option<String>,
    modlist: Option<String>, // "/name;" per client mod, as BeamMP's TResourceManager lists them
    modstotalsize: Option<u64>,
    modstotal: Option<u32>,
    playerslist: Option<String>, // "name;" per connected player
    pass: Option<bool>,      // has a password (never the password)
}

#[derive(Serialize, Clone)]
struct Listed {
    ip: String,
    port: u16,
    name: String,
    players: u32,
    #[serde(rename = "maxPlayers")]
    max_players: u32,
    version: String,
    place: String,
    desc: String,
    tags: String,
    map: String,
    modlist: String,
    modstotalsize: u64,
    modstotal: u32,
    playerslist: String,
    pass: bool,
    location: String, // ISO country code of the server's address ("" unknown)
    age: u64,
}

struct Entry {
    listed: Listed,
    addr: IpAddr,
    seen: Instant,
}

/// Address ranges -> country (DB-IP's CSV: start,end,CC per line, sorted, IPv4 then IPv6).
#[derive(Default)]
struct Geo {
    v4: Vec<(u32, u32, [u8; 2])>,
    v6: Vec<(u128, u128, [u8; 2])>,
}

impl Geo {
    fn parse(text: &str) -> Geo {
        let mut g = Geo::default();
        for line in text.lines() {
            let mut f = line.split(',');
            let (Some(a), Some(b), Some(cc)) = (f.next(), f.next(), f.next()) else { continue };
            let cc = cc.trim().trim_matches('"').as_bytes();
            if cc.len() != 2 || !cc.iter().all(u8::is_ascii_alphabetic) || cc.eq_ignore_ascii_case(b"ZZ") {
                continue;
            }
            let cc = [cc[0].to_ascii_uppercase(), cc[1].to_ascii_uppercase()];
            match (a.trim_matches('"').parse::<IpAddr>(), b.trim_matches('"').parse::<IpAddr>()) {
                (Ok(IpAddr::V4(a)), Ok(IpAddr::V4(b))) => g.v4.push((a.into(), b.into(), cc)),
                (Ok(IpAddr::V6(a)), Ok(IpAddr::V6(b))) => g.v6.push((a.into(), b.into(), cc)),
                _ => {}
            }
        }
        g.v4.sort_unstable_by_key(|r| r.0);
        g.v6.sort_unstable_by_key(|r| r.0);
        g
    }

    fn find<T: Ord + Copy>(ranges: &[(T, T, [u8; 2])], x: T) -> Option<[u8; 2]> {
        let i = ranges.partition_point(|r| r.0 <= x);
        let r = ranges.get(i.checked_sub(1)?)?;
        (x <= r.1).then_some(r.2)
    }

    /// The country code of ip ("" unknown).
    fn country(&self, ip: IpAddr) -> String {
        let ip = match ip {
            IpAddr::V6(v6) => v6.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(ip),
            v4 => v4,
        };
        let cc = match ip {
            IpAddr::V4(a) => Geo::find(&self.v4, u32::from(a)),
            IpAddr::V6(a) => Geo::find(&self.v6, u128::from(a)),
        };
        cc.map(|c| String::from_utf8_lossy(&c).into_owned()).unwrap_or_default()
    }
}

fn geo_path() -> PathBuf {
    if let Ok(p) = std::env::var("BNGE_LIST_GEOIP") {
        return PathBuf::from(p);
    }
    let dir = std::env::current_exe().ok().and_then(|e| e.parent().map(Path::to_path_buf)).unwrap_or_default();
    dir.join("dbip-country-lite.csv.gz")
}

fn load_geo(path: &Path) -> std::io::Result<Geo> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(path)?).read_to_string(&mut text)?;
    Ok(Geo::parse(&text))
}

/// "YYYY-MM" of now minus `back` months (UTC; the table is named by month).
fn year_month(back: i64) -> String {
    let days = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs() / 86400).unwrap_or(0) as i64;
    // civil from days (Howard Hinnant's algorithm)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    let idx = y * 12 + (m - 1) - back;
    format!("{:04}-{:02}", idx.div_euclid(12), idx.rem_euclid(12) + 1)
}

/// This month's table (last month's while this one isn't out yet) to path, through a temporary file.
fn download_geo(path: &Path) -> Result<(), String> {
    let tmp = path.with_extension("part");
    let mut last = String::new();
    for back in 0..2 {
        let url = GEOIP_URL.replace("{ym}", &year_month(back));
        match ureq::get(&url).timeout(Duration::from_secs(120)).call() {
            Ok(resp) => {
                let mut body = Vec::new();
                resp.into_reader().take(64 << 20).read_to_end(&mut body).map_err(|e| e.to_string())?;
                std::fs::write(&tmp, &body).map_err(|e| e.to_string())?;
                load_geo(&tmp).map_err(|e| format!("{url}: {e}"))?;
                return std::fs::rename(&tmp, path).map_err(|e| e.to_string());
            }
            Err(e) => last = format!("{url}: {e}"),
        }
    }
    Err(last)
}

/// The table kept current in the background: fetched when missing or older than GEOIP_MAX_AGE, loaded when changed.
fn keep_geo(shared: Arc<Mutex<Arc<Geo>>>) {
    let path = geo_path();
    let mut loaded: Option<SystemTime> = None;
    loop {
        let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
        let stale = mtime.map_or(true, |t| t.elapsed().map_or(false, |a| a > GEOIP_MAX_AGE));
        if stale {
            match download_geo(&path) {
                Ok(()) => eprintln!("geoip: fetched {}", path.display()),
                Err(e) => eprintln!("geoip: download failed: {e}"),
            }
        }
        let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
        if mtime.is_some() && mtime != loaded {
            match load_geo(&path) {
                Ok(g) => {
                    eprintln!("geoip: {} IPv4 and {} IPv6 ranges", g.v4.len(), g.v6.len());
                    *shared.lock().unwrap() = Arc::new(g);
                    loaded = mtime;
                }
                Err(e) => eprintln!("geoip: {}: {e}", path.display()),
            }
        }
        std::thread::sleep(GEOIP_CHECK);
    }
}

/// Printable text only, at most `max` characters (names show in the game's UI).
fn clean(s: Option<String>, max: usize) -> String {
    s.unwrap_or_default().chars().filter(|c| !c.is_control()).take(max).collect::<String>().trim().to_string()
}

/// The announcing server's address: the peer, or the first X-Forwarded-For hop when the peer is a local proxy.
fn client_ip(req: &Request) -> Option<IpAddr> {
    let peer = req.remote_addr()?.ip();
    if peer.is_loopback() {
        for h in req.headers() {
            if h.field.equiv("X-Forwarded-For") {
                return h.value.as_str().split(',').next()?.trim().parse().ok();
            }
        }
    }
    Some(peer)
}

fn json(code: u16, body: String) -> Response<std::io::Cursor<Vec<u8>>> {
    Response::from_string(body)
        .with_status_code(code)
        .with_header(Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap())
        .with_header(Header::from_bytes(&b"Access-Control-Allow-Origin"[..], &b"*"[..]).unwrap())
}

struct List {
    entries: HashMap<(IpAddr, u16), Entry>,
}

impl List {
    fn expire(&mut self, now: Instant) {
        self.entries.retain(|_, e| now.duration_since(e.seen) < TTL);
    }

    fn announce(&mut self, ip: IpAddr, a: Announce, now: Instant) -> Result<(), (u16, &'static str)> {
        if a.port == 0 {
            return Err((400, "bad port"));
        }
        self.expire(now);
        let key = (ip, a.port);
        if let Some(e) = self.entries.get(&key) {
            if now.duration_since(e.seen) < MIN_GAP {
                return Err((429, "too soon"));
            }
        } else {
            if self.entries.len() >= MAX_SERVERS {
                return Err((503, "list full"));
            }
            if self.entries.keys().filter(|(i, _)| *i == ip).count() >= MAX_PER_IP {
                return Err((429, "too many servers from this address"));
            }
        }
        let listed = Listed {
            ip: ip.to_string(),
            port: a.port,
            name: clean(a.name, 48),
            players: a.players.unwrap_or(0).min(999),
            max_players: a.max_players.unwrap_or(0).min(999),
            version: clean(a.version, 24),
            place: clean(a.place, 64),
            desc: clean(a.desc, MAX_DESC),
            tags: clean(a.tags, 256),
            map: clean(a.map, 128),
            modlist: clean(a.modlist, 8192),
            modstotalsize: a.modstotalsize.unwrap_or(0),
            modstotal: a.modstotal.unwrap_or(0).min(100_000),
            playerslist: clean(a.playerslist, 2048),
            pass: a.pass.unwrap_or(false),
            location: String::new(),
            age: 0,
        };
        self.entries.insert(key, Entry { listed, addr: ip, seen: now });
        Ok(())
    }

    fn servers(&mut self, now: Instant, geo: &Geo) -> Vec<Listed> {
        self.expire(now);
        let mut out: Vec<Listed> = self
            .entries
            .values()
            .map(|e| Listed { age: now.duration_since(e.seen).as_secs(), location: geo.country(e.addr), ..e.listed.clone() })
            .collect();
        out.sort_by(|a, b| a.age.cmp(&b.age).then(a.name.cmp(&b.name)));
        out
    }
}

fn main() {
    let addr = std::env::var("BNGE_LIST_ADDR").unwrap_or_else(|_| "0.0.0.0:47640".to_string());
    // any panic ends the process (systemd starts it again): tiny_http's accept thread died on EMFILE and left the
    // process running but answering nothing for two hours, 2026-11-11
    std::panic::set_hook(Box::new(|info| {
        eprintln!("panic: {info}");
        std::process::abort();
    }));
    let server = Arc::new(Server::http(&addr).expect("listen"));
    eprintln!("bnge-serverlist on {addr}");
    let geo = Arc::new(Mutex::new(Arc::new(Geo::default())));
    let keep = geo.clone();
    std::thread::spawn(move || keep_geo(keep));
    // usage telemetry (telemetry.rs): its files next to the binary unless BNGE_TELEMETRY_DIR
    let tel_dir = std::env::var("BNGE_TELEMETRY_DIR").map(PathBuf::from).unwrap_or_else(|_| {
        std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.join("telemetry"))).unwrap_or_else(|| PathBuf::from("telemetry"))
    });
    let token = std::env::var("BNGE_TELEMETRY_TOKEN").ok().filter(|t| t.len() >= 16);
    // the performance totals (perf.rs); the key of the installs' daily keyed hashes lives in memory only, new each start
    let salt = {
        let mut buf = [0u8; 8];
        let _ = std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut buf));
        u64::from_le_bytes(buf) ^ SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0)
    };
    let perf = perf::Perf::new(Some(tel_dir.join("perf")), salt);
    let tel = telemetry::Telemetry::new(Some(tel_dir));
    // the advanced statistics (public since 2026-11-06, the user: exact country counts are fine): built at most once a
    // minute, a shared link must not make the collector rebuild 7 days of data-source hours per visitor
    let state = Arc::new(Mutex::new(State { list: List { entries: HashMap::new() }, tel, perf, advanced: None,
                                            perf_cache: HashMap::new() }));
    {
        // (the 5 s tick: forget old installs, samples, today's files)
        let st = state.clone();
        std::thread::spawn(move || loop {
            let unix_now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
            {
                let mut s = st.lock().unwrap_or_else(|e| e.into_inner());
                s.tel.tick(Instant::now(), unix_now);
                s.perf.tick(Instant::now(), unix_now);
            }
            std::thread::sleep(Duration::from_secs(5));
        });
    }
    // player reports (reports.rs): their files next to the binary unless BNGE_REPORTS_DIR; the disk cap in GB
    let rep_dir = std::env::var("BNGE_REPORTS_DIR").map(PathBuf::from).unwrap_or_else(|_| {
        std::env::current_exe().ok().and_then(|e| e.parent().map(|p| p.join("reports"))).unwrap_or_else(|| PathBuf::from("reports"))
    });
    let mut lim = reports::Limits::default();
    if let Some(gb) = std::env::var("BNGE_REPORTS_MAX_GB").ok().and_then(|v| v.parse::<f64>().ok()).filter(|g| *g > 0.0) {
        lim.disk_cap = (gb * (1u64 << 30) as f64) as u64;
    }
    let reps = Arc::new(Mutex::new(reports::Reports::new(rep_dir.clone(), lim)));
    {
        let r = reps.lock().unwrap();
        eprintln!("reports: {} kept in {} ({} MB)", r.count(), rep_dir.display(), r.used() >> 20);
    }
    {
        // (the hourly retention sweep)
        let r = reps.clone();
        std::thread::spawn(move || loop {
            let unix_now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
            let gone = r.lock().unwrap_or_else(|e| e.into_inner()).sweep(unix_now);
            if gone > 0 {
                eprintln!("reports: {gone} day(s) past the retention removed");
            }
            std::thread::sleep(Duration::from_secs(3600));
        });
    }
    let slots = Arc::new(Slots::default());
    let mut workers = Vec::new();
    for _ in 0..WORKERS {
        let (server, st, geo, token) = (server.clone(), state.clone(), geo.clone(), token.clone());
        let (reps, slots) = (reps.clone(), slots.clone());
        workers.push(std::thread::spawn(move || loop {
            let mut req = match server.recv() {
                Ok(r) => r,
                Err(_) => continue,
            };
            let path = req.url().split('?').next().unwrap_or("").to_string();
            if *req.method() == Method::Post && path == "/report" {
                // (its own thread: a big body read slowly must not hold a request thread)
                upload(req, reps.clone(), slots.clone(), geo.clone());
                continue;
            }
            if path == "/reports/list" || path.starts_with("/reports/") {
                let resp = private_reports(&req, &path, &reps, &token);
                let _ = req.respond(resp);
                continue;
            }
            let mut body = Vec::new();
            let read = if *req.method() == Method::Post {
                req.as_reader().take(MAX_BODY as u64 + 1).read_to_end(&mut body)
            } else {
                Ok(0)
            };
            let now = Instant::now();
            let unix_now = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
            let resp = {
                let mut s = st.lock().unwrap_or_else(|e| e.into_inner());
                handle(&mut s, &req, read, &body, &geo, &token, now, unix_now)
            };
            let _ = req.respond(resp);
        }));
    }
    for w in workers {
        let _ = w.join();
    }
}

/// The upload threads reading report bodies: at most reports::UPLOADS within their first UPLOAD_S, at most
/// STUCK_MAX in all (a read blocked by a client that stopped sending cannot be cut off: tiny_http gives no socket
/// timeout; such a thread stops counting against the fresh slots after UPLOAD_S).
#[derive(Default)]
struct Slots {
    live: Mutex<HashMap<u64, Instant>>,
    next: std::sync::atomic::AtomicU64,
}

struct Slot(Arc<Slots>, u64);

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.live.lock().unwrap_or_else(|e| e.into_inner()).remove(&self.1);
    }
}

fn take_slot(slots: &Arc<Slots>) -> Option<Slot> {
    let mut live = slots.live.lock().unwrap_or_else(|e| e.into_inner());
    let fresh = live.values().filter(|t| t.elapsed() < Duration::from_secs(reports::UPLOAD_S)).count();
    if fresh >= reports::UPLOADS || live.len() >= reports::STUCK_MAX {
        return None;
    }
    let id = slots.next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    live.insert(id, Instant::now());
    Some(Slot(slots.clone(), id))
}

/// POST /report in a thread of its own: the body read within UPLOAD_S, checked (reports::parse), admitted (rate limits,
/// disk), written outside the lock.
fn upload(req: Request, reps: Arc<Mutex<reports::Reports>>, slots: Arc<Slots>, geo: Arc<Mutex<Arc<Geo>>>) {
    // (the slot taken before the thread: a flood of uploads is refused here, not one thread each)
    let Some(slot) = take_slot(&slots) else {
        let _ = req.respond(json(503, "{\"error\":\"busy, try again later\"}".into()));
        return;
    };
    std::thread::spawn(move || {
        let mut req = req;
        let unix_now = || SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        let err = |code: u16, why: &str| json(code, format!("{{\"error\":\"{why}\"}}"));
        let Some(ip) = client_ip(&req) else {
            let _ = req.respond(err(400, "bad request"));
            return;
        };
        let max_body = reps.lock().unwrap_or_else(|e| e.into_inner()).lim.max_body;
        if let Err((code, why)) = reps.lock().unwrap_or_else(|e| e.into_inner()).attempt(ip, unix_now()) {
            let _ = req.respond(err(code, why));
            return;
        }
        if req.body_length().is_some_and(|n| n > max_body) {
            let _ = req.respond(err(413, "too big"));
            return;
        }
        let t0 = Instant::now();
        let mut body = Vec::new();
        let mut buf = vec![0u8; 65536];
        let mut failed = None;
        {
            let mut r = req.as_reader().take(max_body as u64 + 1);
            loop {
                if t0.elapsed() > Duration::from_secs(reports::UPLOAD_S) {
                    failed = Some((408, "too slow"));
                    break;
                }
                match r.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => body.extend_from_slice(&buf[..n]),
                    Err(_) => {
                        failed = Some((400, "bad request"));
                        break;
                    }
                }
            }
        }
        drop(slot);
        if failed.is_none() && body.len() > max_body {
            failed = Some((413, "too big"));
        }
        if let Some((code, why)) = failed {
            let _ = req.respond(err(code, why));
            return;
        }
        let parsed = {
            let r = reps.lock().unwrap_or_else(|e| e.into_inner());
            reports::parse(&body, &r.lim)
        };
        drop(body);
        let p = match parsed {
            Ok(p) => p,
            Err((code, why)) => {
                let _ = req.respond(err(code, why));
                return;
            }
        };
        let now = unix_now();
        let admitted = {
            let mut r = reps.lock().unwrap_or_else(|e| e.into_inner());
            r.admit(&p, ip, now).map(|t| {
                let f = r.path_of(&t);
                (t, f)
            })
        };
        let (ticket, folder) = match admitted {
            Ok(x) => x,
            Err((code, why)) => {
                let _ = req.respond(err(code, why));
                return;
            }
        };
        let table = geo.lock().unwrap_or_else(|e| e.into_inner()).clone();
        let meta = reports::Reports::meta(&p, &ticket, now, &table.country(ip)); // (the address itself is not kept)
        let wrote = reports::Reports::write(&folder, &p, &meta);
        let id = reports::show_id(&ticket.id);
        let ok = wrote.is_ok();
        if let Err(e) = &wrote {
            eprintln!("reports: writing {}: {e}", folder.display());
        }
        reps.lock().unwrap_or_else(|e| e.into_inner()).finish(ticket, ok.then_some(meta));
        let _ = req.respond(if ok {
            eprintln!("reports: {id} received ({} {}, {} bytes)", p.kind, p.v, p.files.iter().map(|f| f.1.len()).sum::<usize>());
            json(200, format!("{{\"ok\":true,\"id\":\"{id}\"}}"))
        } else {
            err(500, "could not store the report")
        });
    });
}

/// GET /reports/list and /reports/<id>/<file>: the token only (404 without one configured, 401 with a wrong one).
fn private_reports(req: &Request, path: &str, reps: &Arc<Mutex<reports::Reports>>, token: &Option<String>)
                   -> Response<std::io::Cursor<Vec<u8>>> {
    let nf = || json(404, "{\"error\":\"not found\"}".into());
    if token.is_none() {
        return nf();
    }
    let auth = req.headers().iter().find(|h| h.field.equiv("Authorization")).map(|h| h.value.as_str().to_string());
    if !reports::authorized(token, auth.as_deref()) {
        return json(401, "{\"error\":\"token\"}".into());
    }
    if *req.method() != Method::Get {
        return json(405, "{\"error\":\"method\"}".into());
    }
    let r = reps.lock().unwrap_or_else(|e| e.into_inner());
    if path == "/reports/list" {
        return json(200, r.list(&reports::query(req.url())).to_string());
    }
    let mut parts = path.trim_start_matches("/reports/").splitn(2, '/');
    let (Some(id), Some(name)) = (parts.next(), parts.next()) else { return nf() };
    let Some((p, ctype)) = r.file(id, name) else { return nf() };
    drop(r);
    match std::fs::read(&p) {
        Ok(bytes) => Response::from_data(bytes)
            .with_header(Header::from_bytes(&b"Content-Type"[..], ctype.as_bytes()).unwrap())
            .with_header(Header::from_bytes(&b"Cache-Control"[..], &b"no-store"[..]).unwrap()),
        Err(_) => nf(),
    }
}

/// The collector's state, shared by the request threads (main): each reads its request's body and writes its
/// response outside the lock (a slow client held the one loop, and the accepted connections piled up until the
/// process ran out of file descriptors: "No file descriptors available", 502 for two hours, 2026-11-11).
struct State {
    list: List,
    tel: telemetry::Telemetry,
    perf: perf::Perf,
    advanced: Option<(Instant, String)>,
    perf_cache: HashMap<u64, (Instant, String)>,
}

const WORKERS: usize = 16; // request threads

#[allow(clippy::too_many_arguments)]
fn handle(s: &mut State, req: &Request, read: std::io::Result<usize>, body: &[u8], geo: &Arc<Mutex<Arc<Geo>>>,
          token: &Option<String>, now: Instant, unix_now: u64) -> Response<std::io::Cursor<Vec<u8>>> {
    let path = req.url().split('?').next().unwrap_or("").to_string();
    match (req.method(), path.as_str()) {
        (Method::Get, "/servers") => {
            let g = geo.lock().unwrap().clone();
            json(200, serde_json::to_string(&s.list.servers(now, &g)).unwrap())
        }
        (Method::Post, "/announce") => {
            match (read, client_ip(req)) {
                (Ok(n), _) if n > MAX_BODY => json(413, "{\"error\":\"too big\"}".into()),
                (Ok(_), Some(ip)) => match serde_json::from_slice::<Announce>(body) {
                    Ok(a) => match s.list.announce(ip, a, now) {
                        Ok(()) => json(200, "{\"ok\":true}".into()),
                        Err((code, why)) => json(code, format!("{{\"error\":\"{why}\"}}")),
                    },
                    Err(_) => json(400, "{\"error\":\"bad json\"}".into()),
                },
                _ => json(400, "{\"error\":\"bad request\"}".into()),
            }
        }
        (Method::Post, "/telemetry") => {
            match (read, client_ip(req)) {
                (Ok(n), _) if n > MAX_BODY => json(413, "{\"error\":\"too big\"}".into()),
                (Ok(_), Some(ip)) => match serde_json::from_slice::<telemetry::Report>(body) {
                    Ok(mut r) => {
                        let cc = geo.lock().unwrap().country(ip);     // (the address itself is not kept)
                        let (id, p) = (r.id(), r.perf.take());
                        match s.tel.report(ip, cc, r, now, unix_now) {
                            Ok(()) => {
                                if let (Some(p), Some((v, os))) = (p, s.tel.info(&id)) {
                                    s.perf.add(&id, &v, &os, p, now, unix_now);   // (folded into totals, then dropped)
                                }
                                json(200, "{\"ok\":true}".into())
                            }
                            Err((code, why)) => json(code, format!("{{\"error\":\"{why}\"}}")),
                        }
                    }
                    Err(_) => json(400, "{\"error\":\"bad json\"}".into()),
                },
                _ => json(400, "{\"error\":\"bad request\"}".into()),
            }
        }
        (Method::Get, "/stats") => json(200, s.tel.public(now, unix_now).to_string()),
        (Method::Get, "/stats/now") => json(200, s.tel.now(now, unix_now).to_string()),
        (Method::Get, "/stats/range") => {
            // ?from=<unix s>&to=<unix s> (to defaults to now)
            let q = req.url().split_once('?').map(|x| x.1).unwrap_or("");
            let arg = |k: &str| q.split('&').find_map(|kv| kv.strip_prefix(k).and_then(|v| v.strip_prefix('=')))
                .and_then(|v| v.parse::<u64>().ok());
            match arg("from") {
                Some(from) => match s.tel.range(now, unix_now, from, arg("to").unwrap_or(unix_now)) {
                    Ok(v) => json(200, v.to_string()),
                    Err((code, why)) => json(code, format!("{{\"error\":\"{why}\"}}")),
                },
                None => json(400, "{\"error\":\"from\"}".into()),
            }
        }
        (Method::Get, "/stats/private") => {
            let auth = req.headers().iter().find(|h| h.field.equiv("Authorization")).map(|h| h.value.as_str().to_string());
            match (token, auth) {
                (Some(t), Some(a)) if a == format!("Bearer {t}") => json(200, s.tel.private(now, unix_now).to_string()),
                (Some(_), _) => json(401, "{\"error\":\"token\"}".into()),
                (None, _) => json(404, "{\"error\":\"not found\"}".into()),
            }
        }
        (Method::Get, "/stats/performance") => {
            // ?days=N (1-400, default 7; 0: everything kept); built at most once a minute per range
            let q = req.url().split_once('?').map(|x| x.1).unwrap_or("");
            let days = q.split('&').find_map(|kv| kv.strip_prefix("days=")).and_then(|v| v.parse::<u64>().ok())
                .unwrap_or(7).min(400);
            let fresh = s.perf_cache.get(&days).is_some_and(|(at, _)| now.duration_since(*at) < Duration::from_secs(60));
            if !fresh {
                s.perf_cache.insert(days, (now, s.perf.public(unix_now, days).to_string()));
            }
            json(200, s.perf_cache[&days].1.clone())
        }
        (Method::Get, "/performance") | (Method::Get, "/performance/") => html(include_str!("performance.html")),
        (Method::Get, "/stats/advanced") => {
            if s.advanced.as_ref().is_none_or(|(at, _)| now.duration_since(*at) > Duration::from_secs(60)) {
                let mut v = s.tel.private(now, unix_now);
                v["errors7d"] = s.perf.errors(unix_now);
                s.advanced = Some((now, v.to_string()));
            }
            json(200, s.advanced.as_ref().unwrap().1.clone())
        }
        (Method::Get, "/players") | (Method::Get, "/players/") => html(include_str!("players.html")),
        (Method::Get, "/players/advanced") => html(include_str!("advanced.html")),
        (Method::Get, "/players/private") => Response::from_string("")
            .with_status_code(301)
            .with_header(Header::from_bytes(&b"Location"[..], &b"/players/advanced"[..]).unwrap()),
        _ => json(404, "{\"error\":\"not found\"}".into()),
    }
}

fn html(body: &str) -> Response<std::io::Cursor<Vec<u8>>> {
    Response::from_string(body.to_string())
        .with_header(Header::from_bytes(&b"Content-Type"[..], &b"text/html; charset=utf-8"[..]).unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ann(port: u16, name: &str) -> Announce {
        Announce { port, name: Some(name.into()), players: Some(2), max_players: Some(8), version: Some("1.6.2".into()),
            place: None, desc: Some("Drive\u{7} anywhere".into()), tags: Some("Freeroam".into()), map: None,
            modlist: Some("/a;/b;".into()), modstotalsize: Some(1024), modstotal: Some(2), playerslist: Some("x;".into()),
            pass: None }
    }

    #[test]
    fn listed_then_expired() {
        let mut l = List { entries: HashMap::new() };
        let ip: IpAddr = "203.0.113.5".parse().unwrap();
        let t0 = Instant::now();
        l.announce(ip, ann(30814, "A\u{7}b"), t0).unwrap();
        let s = l.servers(t0, &Geo::default());
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].name, "Ab");
        assert_eq!(s[0].ip, "203.0.113.5");
        assert_eq!(s[0].desc, "Drive anywhere");
        assert_eq!((s[0].modlist.as_str(), s[0].modstotal, s[0].modstotalsize, s[0].pass), ("/a;/b;", 2, 1024, false));
        assert!(l.servers(t0 + TTL + Duration::from_secs(1), &Geo::default()).is_empty());
    }

    #[test]
    fn country_of_an_address() {
        let g = Geo::parse("0.0.0.0,0.255.255.255,ZZ\n5.161.0.0,5.161.255.255,DE\n5.162.0.0,5.162.0.255,us\n\
                            2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,FI\nbad line\n");
        assert_eq!(g.country("5.161.253.210".parse().unwrap()), "DE");
        assert_eq!(g.country("5.162.0.7".parse().unwrap()), "US");
        assert_eq!(g.country("5.162.1.0".parse().unwrap()), "");
        assert_eq!(g.country("0.1.2.3".parse().unwrap()), "", "ZZ: unknown");
        assert_eq!(g.country("2001:db8::5".parse().unwrap()), "FI");
        assert_eq!(g.country("::ffff:5.161.0.1".parse().unwrap()), "DE", "an IPv4-mapped address");
        let mut l = List { entries: HashMap::new() };
        l.announce("5.161.1.1".parse().unwrap(), ann(30814, "a"), Instant::now()).unwrap();
        assert_eq!(l.servers(Instant::now(), &g)[0].location, "DE");
    }

    #[test]
    fn month_names() {
        let ym = year_month(0);
        assert_eq!(ym.len(), 7);
        let (y, m): (i32, u32) = (ym[..4].parse().unwrap(), ym[5..].parse().unwrap());
        let (py, pm) = if m == 1 { (y - 1, 12) } else { (y, m - 1) };
        assert_eq!(year_month(1), format!("{py:04}-{pm:02}"));
    }

    #[test]
    fn too_soon_and_per_ip_cap() {
        let mut l = List { entries: HashMap::new() };
        let ip: IpAddr = "203.0.113.6".parse().unwrap();
        let t0 = Instant::now();
        l.announce(ip, ann(1, "a"), t0).unwrap();
        assert_eq!(l.announce(ip, ann(1, "a"), t0 + Duration::from_secs(5)).unwrap_err().0, 429);
        assert!(l.announce(ip, ann(1, "a"), t0 + MIN_GAP).is_ok());
        for p in 2..=MAX_PER_IP as u16 {
            l.announce(ip, ann(p, "x"), t0).unwrap();
        }
        assert_eq!(l.announce(ip, ann(999, "x"), t0).unwrap_err().0, 429);
        assert_eq!(l.announce(ip, ann(0, "x"), t0 + TTL * 2).unwrap_err().0, 400);
    }
}
