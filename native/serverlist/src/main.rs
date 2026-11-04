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
//! In memory only (servers announce again within a minute of a restart). Env: BNGE_LIST_ADDR (default 0.0.0.0:47640),
//! BNGE_LIST_GEOIP (the table's path; default dbip-country-lite.csv.gz next to the binary).

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
    let server = Server::http(&addr).expect("listen");
    eprintln!("bnge-serverlist on {addr}");
    let mut list = List { entries: HashMap::new() };
    let geo = Arc::new(Mutex::new(Arc::new(Geo::default())));
    let keep = geo.clone();
    std::thread::spawn(move || keep_geo(keep));
    for mut req in server.incoming_requests() {
        let now = Instant::now();
        let path = req.url().split('?').next().unwrap_or("").to_string();
        let resp = match (req.method(), path.as_str()) {
            (Method::Get, "/servers") => {
                let g = geo.lock().unwrap().clone();
                json(200, serde_json::to_string(&list.servers(now, &g)).unwrap())
            }
            (Method::Post, "/announce") => {
                let mut body = Vec::new();
                let read = req.as_reader().take(MAX_BODY as u64 + 1).read_to_end(&mut body);
                match (read, client_ip(&req)) {
                    (Ok(n), _) if n > MAX_BODY => json(413, "{\"error\":\"too big\"}".into()),
                    (Ok(_), Some(ip)) => match serde_json::from_slice::<Announce>(&body) {
                        Ok(a) => match list.announce(ip, a, now) {
                            Ok(()) => json(200, "{\"ok\":true}".into()),
                            Err((code, why)) => json(code, format!("{{\"error\":\"{why}\"}}")),
                        },
                        Err(_) => json(400, "{\"error\":\"bad json\"}".into()),
                    },
                    _ => json(400, "{\"error\":\"bad request\"}".into()),
                }
            }
            _ => json(404, "{\"error\":\"not found\"}".into()),
        };
        let _ = req.respond(resp);
    }
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
