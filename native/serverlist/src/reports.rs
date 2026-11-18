//! Player reports sent from the game's reporter (the user, 2026-11-12: "Could we update the in game reporter to send
//! reports directly to our mini pc server?"). Private: they are never listed publicly or published anywhere (the
//! user's call: "sent privately to the BeamNG.Earth developers; not published").
//!
//! POST /report   one report, a JSON body (Content-Type application/json):
//!                {"install": 32 hex (the app's random install id), "v": the app version,
//!                 "report": {...} (report.json: kind, title, note, view, performance), "hardware": {...},
//!                 "server": {...} (the world server's state), "screenshot": base64 JPEG, "logs": base64 gzip,
//!                 "sent": {"screenshot", "exactLocation", "logs"} (the player's choices in the send window)}
//!                Only "install", "v" and "report" are required. Answer {"ok": true, "id": "ABCD-EFGH"}: a short id the
//!                player can quote. Limits (Limits): the body, each part and the whole report decoded; JSON parts must
//!                be objects, the screenshot a JPEG (its magic bytes), the logs gzip (decompressed size bounded); no
//!                other fields (deny_unknown_fields) and nothing the client sends is used in a path.
//!                Rate limits per install id and per address (the address is kept in memory for that only, never
//!                written), a daily cap over everyone, a disk cap (refused with a log line above it), at most
//!                UPLOADS bodies read at once and each within UPLOAD_S.
//! GET  /reports/list?from=YYYY-MM-DD&to=YYYY-MM-DD&version=..&kind=..&after=<id>&limit=N   (token)
//!                newest first, paged: {"reports": [meta], "next": id or null, "total": n, "bytes": disk use}
//! GET  /reports/<id>/<file>   (token) one stored file: meta.json, report.json, hardware.json, server.json,
//!                screenshot.jpg, logs.gz
//!
//! Stored in <dir>/<YYYY-MM-DD>/<ID>/ (dir: BNGE_REPORTS_DIR, default "reports" next to the binary): the parts as sent
//! plus meta.json (the server-made id, the time received, the version, the install id, kind, title, file sizes).
//! Removed after KEEP_DAYS (the hourly sweep).

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::fs;
use std::io::Read;
use std::net::IpAddr;
use std::path::{Path, PathBuf};

#[derive(Clone)]
pub struct Limits {
    pub max_body: usize,       // the request body (base64 makes binary parts 4/3 as big)
    pub max_shot: usize,       // the screenshot, decoded
    pub max_json: usize,       // each JSON part, compact
    pub max_logs: usize,       // the logs part, compressed
    pub max_logs_raw: u64,     // ... and decompressed (a gzip bomb is refused)
    pub max_total: usize,      // every part together, decoded
    pub install_hour: usize,
    pub install_day: usize,
    pub ip_hour: usize,
    pub ip_day: usize,
    pub ip_attempts_hour: usize, // requests from one address an hour, accepted or not
    pub global_day: usize,
    pub disk_cap: u64,
    pub keep_days: u64,
}

impl Default for Limits {
    fn default() -> Limits {
        Limits {
            max_body: 2_900_000,
            max_shot: 1_500_000,
            max_json: 256 * 1024,
            max_logs: 256 * 1024,
            max_logs_raw: 8 << 20,
            max_total: 2_000_000,
            install_hour: 30,
            install_day: 100,
            ip_hour: 60,
            ip_day: 200,
            ip_attempts_hour: 300,
            global_day: 5000,
            disk_cap: 5 << 30,
            keep_days: 90,
        }
    }
}

pub const UPLOADS: usize = 4; // bodies read at once (the rest: 503, the client retries)
pub const UPLOAD_S: u64 = 60; // a body not read in this long: 408
pub const STUCK_MAX: usize = 32; // upload threads still blocked in a read past UPLOAD_S at most (then 503 for all)

pub const FILES: [&str; 6] = ["meta.json", "report.json", "hardware.json", "server.json", "screenshot.jpg", "logs.gz"];
const KINDS: [&str; 7] = ["map", "perf", "bug", "idea", "note", "bench", "profile"];
const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

#[derive(Deserialize, Default, Serialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Sent {
    #[serde(default)]
    pub screenshot: bool,
    #[serde(default, rename = "exactLocation")]
    pub exact_location: bool,
    #[serde(default)]
    pub logs: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Body {
    install: String,
    v: String,
    report: serde_json::Value,
    #[serde(default)]
    hardware: Option<serde_json::Value>,
    #[serde(default)]
    server: Option<serde_json::Value>,
    #[serde(default)]
    screenshot: Option<String>,
    #[serde(default)]
    logs: Option<String>,
    #[serde(default)]
    sent: Option<Sent>,
}

/// A checked report, ready to store.
#[derive(Debug)]
pub struct Parsed {
    pub install: String,
    pub v: String,
    pub kind: String,
    pub title: String,
    pub sent: Sent,
    pub files: Vec<(&'static str, Vec<u8>)>, // (fixed name, bytes)
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Meta {
    pub id: String,
    pub date: String,
    pub received: u64,
    #[serde(rename = "receivedAt")]
    pub received_at: String,
    pub v: String,
    pub install: String,
    pub kind: String,
    pub title: String,
    #[serde(default)]
    pub sent: Sent,
    /// The sender's country (ISO code from the connection's address, "" unknown; the address itself is not kept).
    #[serde(default)]
    pub country: String,
    pub files: BTreeMap<String, u64>,
}

type Err = (u16, &'static str);

/// Strict base64 (standard alphabet, padding optional, no whitespace).
pub fn b64_decode(s: &str, max_out: usize) -> Result<Vec<u8>, Err> {
    let s = s.as_bytes();
    let s = match s.iter().rposition(|&c| c != b'=') {
        Some(i) if s.len() - (i + 1) <= 2 => &s[..=i],
        None if s.is_empty() => s,
        _ => return Err((400, "bad base64")),
    };
    if s.len() / 4 * 3 > max_out + 3 {
        return Err((413, "part too big"));
    }
    if s.len() % 4 == 1 {
        return Err((400, "bad base64"));
    }
    let val = |c: u8| -> Result<u32, Err> {
        Ok(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return Err((400, "bad base64")),
        } as u32)
    };
    let mut out = Vec::with_capacity(s.len() / 4 * 3 + 3);
    for chunk in s.chunks(4) {
        let mut n = 0u32;
        for (i, &c) in chunk.iter().enumerate() {
            n |= val(c)? << (18 - 6 * i);
        }
        out.push((n >> 16) as u8);
        if chunk.len() > 2 {
            out.push((n >> 8) as u8);
        }
        if chunk.len() > 3 {
            out.push(n as u8);
        }
    }
    if out.len() > max_out {
        return Err((413, "part too big"));
    }
    Ok(out)
}

fn printable(s: &str, max: usize) -> String {
    s.chars().filter(|c| !c.is_control()).take(max).collect::<String>().trim().to_string()
}

fn json_part(v: serde_json::Value, max: usize) -> Result<Vec<u8>, Err> {
    if !v.is_object() {
        return Err((400, "a JSON part is not an object"));
    }
    if serde_json::to_vec(&v).map_err(|_| (400, "bad json"))?.len() > max {
        return Err((413, "a JSON part is too big"));
    }
    serde_json::to_vec_pretty(&v).map_err(|_| (400, "bad json"))
}

/// The body checked and decoded (pure: tests).
pub fn parse(body: &[u8], lim: &Limits) -> Result<Parsed, Err> {
    if body.len() > lim.max_body {
        return Err((413, "too big"));
    }
    let b: Body = serde_json::from_slice(body).map_err(|_| (400, "bad json"))?;
    let install = b.install.to_ascii_lowercase();
    if install.len() != 32 || !install.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err((400, "bad install id"));
    }
    let v = printable(&b.v, 40);
    if v.is_empty() || !v.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '+' | '_')) {
        return Err((400, "bad version"));
    }
    let kind = b.report.get("kind").and_then(|k| k.as_str()).unwrap_or("").to_string();
    if !KINDS.contains(&kind.as_str()) {
        return Err((400, "bad kind"));
    }
    let title = b.report.get("title").and_then(|t| t.as_str()).map(|t| printable(t, 200)).unwrap_or_default();
    let mut files: Vec<(&'static str, Vec<u8>)> = vec![("report.json", json_part(b.report, lim.max_json)?)];
    if let Some(h) = b.hardware {
        files.push(("hardware.json", json_part(h, lim.max_json)?));
    }
    if let Some(s) = b.server {
        files.push(("server.json", json_part(s, lim.max_json)?));
    }
    if let Some(s) = b.screenshot {
        let jpg = b64_decode(&s, lim.max_shot)?;
        if jpg.len() < 4 || jpg[0] != 0xFF || jpg[1] != 0xD8 || jpg[2] != 0xFF {
            return Err((415, "the screenshot is not a JPEG"));
        }
        files.push(("screenshot.jpg", jpg));
    }
    if let Some(s) = b.logs {
        let gz = b64_decode(&s, lim.max_logs)?;
        if gz.len() < 18 || gz[0] != 0x1F || gz[1] != 0x8B || gz[2] != 8 {
            return Err((415, "the logs are not gzip"));
        }
        let mut n = 0u64;
        let mut d = flate2::read::MultiGzDecoder::new(&gz[..]).take(lim.max_logs_raw + 1);
        let mut buf = [0u8; 16384];
        loop {
            match d.read(&mut buf) {
                Ok(0) => break,
                Ok(k) => n += k as u64,
                Err(_) => return Err((415, "the logs are not gzip")),
            }
        }
        if n > lim.max_logs_raw {
            return Err((413, "the logs are too big"));
        }
        files.push(("logs.gz", gz));
    }
    if files.iter().map(|f| f.1.len()).sum::<usize>() > lim.max_total {
        return Err((413, "too big"));
    }
    Ok(Parsed { install, v, kind, title, sent: b.sent.unwrap_or_default(), files })
}

/// A report id as typed by a player ("abcd-efgh", "ABCDEFGH") in its stored form, or None.
pub fn norm_id(s: &str) -> Option<String> {
    let t: String = s.chars().filter(|c| *c != '-').map(|c| c.to_ascii_uppercase()).collect();
    (t.len() == 8 && t.bytes().all(|c| CROCKFORD.contains(&c))).then_some(t)
}

pub fn show_id(id: &str) -> String {
    format!("{}-{}", &id[..4], &id[4..])
}

/// (year, month, day) of a unix day (Howard Hinnant's civil_from_days).
pub fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

/// The unix day of "YYYY-MM-DD", or None (days_from_civil).
pub fn day_of(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() != 10 || b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let (y, m, d): (i64, i64, i64) = (s[..4].parse().ok()?, s[5..7].parse().ok()?, s[8..].parse().ok()?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) || !s.bytes().enumerate().all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit()) {
        return None;
    }
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    (civil(days) == (y, m as u32, d as u32)).then_some(days)
}

pub fn date(unix: u64) -> String {
    let (y, m, d) = civil((unix / 86400) as i64);
    format!("{y:04}-{m:02}-{d:02}")
}

fn stamp(unix: u64) -> String {
    let s = unix % 86400;
    format!("{}T{:02}:{:02}:{:02}Z", date(unix), s / 3600, s / 60 % 60, s % 60)
}

fn random_id() -> String {
    let mut buf = [0u8; 8];
    let ok = fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut buf)).is_ok();
    if !ok {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        buf = (t as u64 ^ (std::process::id() as u64).rotate_left(32)).to_le_bytes();
    }
    let n = u64::from_le_bytes(buf);
    (0..8).map(|i| CROCKFORD[((n >> (5 * i)) & 31) as usize] as char).collect()
}

fn dir_bytes(p: &Path) -> u64 {
    let mut n = 0;
    if let Ok(rd) = fs::read_dir(p) {
        for e in rd.flatten() {
            match e.file_type() {
                Ok(t) if t.is_dir() => n += dir_bytes(&e.path()),
                Ok(t) if t.is_file() => n += e.metadata().map(|m| m.len()).unwrap_or(0),
                _ => {}
            }
        }
    }
    n
}

fn hits(q: &mut VecDeque<u64>, now: u64) -> (usize, usize) {
    while q.front().is_some_and(|t| now.saturating_sub(*t) >= 86400) {
        q.pop_front();
    }
    (q.iter().filter(|t| now.saturating_sub(**t) < 3600).count(), q.len())
}

pub struct Reports {
    dir: PathBuf,
    pub lim: Limits,
    index: BTreeMap<String, Meta>, // id -> meta
    used: u64,
    reserved: u64,
    by_install: HashMap<String, VecDeque<u64>>,
    by_ip: HashMap<IpAddr, VecDeque<u64>>,
    attempts: HashMap<IpAddr, VecDeque<u64>>,
    global: VecDeque<u64>,
    last_full_log: u64,
}

pub struct Ticket {
    pub id: String,
    date: String,
    bytes: u64,
}

impl Reports {
    pub fn new(dir: PathBuf, lim: Limits) -> Reports {
        let mut r = Reports { dir, lim, index: BTreeMap::new(), used: 0, reserved: 0, by_install: HashMap::new(),
                              by_ip: HashMap::new(), attempts: HashMap::new(), global: VecDeque::new(), last_full_log: 0 };
        r.load();
        r
    }

    fn load(&mut self) {
        self.index.clear();
        let _ = fs::create_dir_all(&self.dir);
        let Ok(days) = fs::read_dir(&self.dir) else { return };
        for d in days.flatten() {
            let name = d.file_name().to_string_lossy().into_owned();
            if day_of(&name).is_none() || !d.path().is_dir() {
                continue;
            }
            let Ok(ids) = fs::read_dir(d.path()) else { continue };
            for e in ids.flatten() {
                let id = e.file_name().to_string_lossy().into_owned();
                if norm_id(&id).as_deref() != Some(id.as_str()) {
                    continue; // (a .tmp folder of an interrupted write: the sweep removes it)
                }
                if let Ok(m) = fs::read(e.path().join("meta.json")).map_err(|_| ()).and_then(|b| {
                    serde_json::from_slice::<Meta>(&b).map_err(|_| ())
                }) {
                    if m.id == id && m.date == name {
                        self.index.insert(id, m);
                    }
                }
            }
        }
        self.used = dir_bytes(&self.dir);
    }

    pub fn used(&self) -> u64 {
        self.used
    }

    pub fn count(&self) -> usize {
        self.index.len()
    }

    /// A request from ip, counted before its body is checked (an address hammering bad requests).
    pub fn attempt(&mut self, ip: IpAddr, now: u64) -> Result<(), Err> {
        let q = self.attempts.entry(ip).or_default();
        let (hour, _) = hits(q, now);
        if hour >= self.lim.ip_attempts_hour {
            return Err((429, "too many requests"));
        }
        q.push_back(now);
        Ok(())
    }

    /// A checked report from ip: the rate limits and the disk cap, then its id and folder reserved.
    pub fn admit(&mut self, p: &Parsed, ip: IpAddr, now: u64) -> Result<Ticket, Err> {
        let (gh, gd) = hits(&mut self.global, now);
        let _ = gh;
        if gd >= self.lim.global_day {
            return Err((503, "too many reports today"));
        }
        let (ih, id_) = hits(self.by_install.entry(p.install.clone()).or_default(), now);
        if ih >= self.lim.install_hour || id_ >= self.lim.install_day {
            return Err((429, "too many reports from this install"));
        }
        let (ph, pd) = hits(self.by_ip.entry(ip).or_default(), now);
        if ph >= self.lim.ip_hour || pd >= self.lim.ip_day {
            return Err((429, "too many reports from this address"));
        }
        let bytes = p.files.iter().map(|f| f.1.len() as u64).sum::<u64>() + 1024;
        if self.used + self.reserved + bytes > self.lim.disk_cap {
            if now.saturating_sub(self.last_full_log) >= 600 {
                self.last_full_log = now;
                eprintln!("reports: refused, the store is full ({} MB of {} MB): remove old reports or raise \
                           BNGE_REPORTS_MAX_GB", self.used >> 20, self.lim.disk_cap >> 20);
            }
            return Err((507, "the report store is full"));
        }
        let mut id = random_id();
        while self.index.contains_key(&id) {
            id = random_id();
        }
        self.global.push_back(now);
        self.by_install.get_mut(&p.install).unwrap().push_back(now);
        self.by_ip.get_mut(&ip).unwrap().push_back(now);
        self.reserved += bytes;
        Ok(Ticket { id, date: date(now), bytes })
    }

    /// The folder a ticket's files go to (written by write(), outside the lock).
    pub fn path_of(&self, t: &Ticket) -> PathBuf {
        self.dir.join(&t.date).join(&t.id)
    }

    /// The files written into <dir>/<date>/<id>.tmp, then renamed (a crash leaves no half report listed).
    pub fn write(folder: &Path, p: &Parsed, meta: &Meta) -> std::io::Result<()> {
        let tmp = folder.with_extension("tmp");
        let _ = fs::remove_dir_all(&tmp);
        fs::create_dir_all(&tmp)?;
        let res = (|| {
            for (name, bytes) in &p.files {
                fs::write(tmp.join(name), bytes)?;
            }
            fs::write(tmp.join("meta.json"), serde_json::to_vec_pretty(meta).unwrap())?;
            fs::rename(&tmp, folder)
        })();
        if res.is_err() {
            let _ = fs::remove_dir_all(&tmp);
        }
        res
    }

    pub fn meta(p: &Parsed, t: &Ticket, now: u64, country: &str) -> Meta {
        let mut files: BTreeMap<String, u64> = p.files.iter().map(|(n, b)| (n.to_string(), b.len() as u64)).collect();
        files.insert("meta.json".into(), 0);
        Meta { id: t.id.clone(), date: t.date.clone(), received: now, received_at: stamp(now), v: p.v.clone(),
               install: p.install.clone(), kind: p.kind.clone(), title: p.title.clone(), sent: p.sent.clone(),
               country: country.to_string(), files }
    }

    /// The write's outcome: listed, or its reservation (and its rate-limit hits stay: a failed write is rare).
    pub fn finish(&mut self, t: Ticket, meta: Option<Meta>) {
        self.reserved = self.reserved.saturating_sub(t.bytes);
        if let Some(m) = meta {
            self.used += m.files.values().sum::<u64>() + 1024;
            self.index.insert(t.id, m);
        }
    }

    /// The private list: newest first, filtered, paged by the last id of the previous page.
    pub fn list(&self, q: &HashMap<String, String>) -> serde_json::Value {
        let from = q.get("from").or(q.get("date")).and_then(|d| day_of(d));
        let to = q.get("to").or(q.get("date")).and_then(|d| day_of(d));
        let limit = q.get("limit").and_then(|l| l.parse::<usize>().ok()).unwrap_or(50).clamp(1, 500);
        let mut all: Vec<&Meta> = self
            .index
            .values()
            .filter(|m| {
                let d = day_of(&m.date).unwrap_or(0);
                from.is_none_or(|f| d >= f)
                    && to.is_none_or(|t| d <= t)
                    && q.get("version").is_none_or(|v| m.v == *v || m.v.starts_with(&format!("{v}.")) || m.v.starts_with(&format!("{v}-")))
                    && q.get("kind").is_none_or(|k| m.kind == *k)
                    && q.get("install").is_none_or(|i| m.install == i.to_ascii_lowercase())
            })
            .collect();
        all.sort_by(|a, b| b.received.cmp(&a.received).then(b.id.cmp(&a.id)));
        let total = all.len();
        let start = match q.get("after").and_then(|a| norm_id(a)) {
            Some(a) => all.iter().position(|m| m.id == a).map(|i| i + 1).unwrap_or(all.len()),
            None => 0,
        };
        let page: Vec<&Meta> = all.iter().skip(start).take(limit).copied().collect();
        let next = (start + page.len() < total).then(|| page.last().map(|m| m.id.clone())).flatten();
        serde_json::json!({"reports": page, "next": next, "total": total, "bytes": self.used})
    }

    /// The stored file of a report: only the fixed names, the folder found through the index (no client text in a path).
    pub fn file(&self, id: &str, name: &str) -> Option<(PathBuf, &'static str)> {
        let id = norm_id(id)?;
        let m = self.index.get(&id)?;
        let fixed = *FILES.iter().find(|f| **f == name)?;
        let ctype = match fixed {
            "screenshot.jpg" => "image/jpeg",
            "logs.gz" => "application/gzip",
            _ => "application/json",
        };
        let p = self.dir.join(&m.date).join(&m.id).join(fixed);
        p.is_file().then_some((p, ctype))
    }

    /// The retention sweep: the days older than keep_days removed (and interrupted writes' .tmp folders).
    pub fn sweep(&mut self, now: u64) -> usize {
        let today = (now / 86400) as i64;
        let mut gone = 0;
        if let Ok(days) = fs::read_dir(&self.dir) {
            for d in days.flatten() {
                let name = d.file_name().to_string_lossy().into_owned();
                let Some(day) = day_of(&name) else { continue };
                if today - day >= self.lim.keep_days as i64 {
                    if fs::remove_dir_all(d.path()).is_ok() {
                        gone += 1;
                    }
                } else if let Ok(ids) = fs::read_dir(d.path()) {
                    for e in ids.flatten() {
                        let n = e.file_name().to_string_lossy().into_owned();
                        let old = e.metadata().and_then(|m| m.modified()).ok().and_then(|t| t.elapsed().ok())
                            .is_some_and(|a| a.as_secs() > 3600);
                        if n.ends_with(".tmp") && old {
                            let _ = fs::remove_dir_all(e.path());
                        }
                    }
                }
            }
        }
        // (the hour's rate-limit queues of addresses and installs not heard from in a day)
        self.by_ip.retain(|_, q| q.back().is_some_and(|t| now.saturating_sub(*t) < 86400));
        self.by_install.retain(|_, q| q.back().is_some_and(|t| now.saturating_sub(*t) < 86400));
        self.attempts.retain(|_, q| q.back().is_some_and(|t| now.saturating_sub(*t) < 3600));
        if gone > 0 {
            self.load();
        }
        gone
    }
}

/// Bearer token check in constant time over the token's length.
pub fn authorized(token: &Option<String>, header: Option<&str>) -> bool {
    let (Some(t), Some(h)) = (token, header) else { return false };
    let Some(given) = h.strip_prefix("Bearer ") else { return false };
    let (a, b) = (t.as_bytes(), given.trim().as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// "a=1&b=2" -> map (percent-decoding not needed: ids, dates and versions are plain).
pub fn query(url: &str) -> HashMap<String, String> {
    url.split_once('?')
        .map(|x| x.1)
        .unwrap_or("")
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .filter(|(k, v)| !k.is_empty() && v.len() <= 64)
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn b64(data: &[u8]) -> String {
        const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut s = String::new();
        for c in data.chunks(3) {
            let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
            for i in 0..4 {
                if i <= c.len() {
                    s.push(A[(n >> (18 - 6 * i) & 63) as usize] as char);
                } else {
                    s.push('=');
                }
            }
        }
        s
    }

    fn gz(data: &[u8]) -> Vec<u8> {
        let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        e.write_all(data).unwrap();
        e.finish().unwrap()
    }

    fn body(extra: serde_json::Value) -> Vec<u8> {
        let mut b = serde_json::json!({"install": "0123456789abcdef0123456789ABCDEF", "v": "1.9.1-beta.dev.300",
                                       "report": {"kind": "map", "title": "Floating\u{7} bridge", "note": "x"}});
        for (k, v) in extra.as_object().unwrap() {
            b[k] = v.clone();
        }
        serde_json::to_vec(&b).unwrap()
    }

    fn tmpdir(tag: &str) -> PathBuf {
        let base = std::env::var("CARGO_TARGET_TMPDIR").map(PathBuf::from).unwrap_or_else(|_| std::env::temp_dir());
        let d = base.join(format!("bnge-reports-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn base64_roundtrip_and_rejects() {
        for n in 0..20 {
            let data: Vec<u8> = (0..n).map(|i| (i * 37 + 11) as u8).collect();
            assert_eq!(b64_decode(&b64(&data), 100).unwrap(), data, "n {n}");
            assert_eq!(b64_decode(b64(&data).trim_end_matches('='), 100).unwrap(), data, "unpadded n {n}");
        }
        assert_eq!(b64_decode("ab$d", 10).unwrap_err().0, 400);
        assert_eq!(b64_decode("ab d", 10).unwrap_err().0, 400);
        assert_eq!(b64_decode("a===", 10).unwrap_err().0, 400);
        assert_eq!(b64_decode(&b64(&[0u8; 30]), 20).unwrap_err().0, 413);
    }

    #[test]
    fn a_full_report_parses() {
        let jpg = [&[0xFF, 0xD8, 0xFF, 0xE0][..], &[1u8; 1000][..], &[0xFF, 0xD9][..]].concat();
        let logs = gz(b"{\"server.log\": \"line\"}");
        let p = parse(&body(serde_json::json!({"hardware": {"cpu": "x"}, "server": {"v": 1},
            "screenshot": b64(&jpg), "logs": b64(&logs), "sent": {"screenshot": true, "exactLocation": false, "logs": true}})),
            &Limits::default()).unwrap();
        assert_eq!(p.install, "0123456789abcdef0123456789abcdef");
        assert_eq!((p.kind.as_str(), p.title.as_str()), ("map", "Floating bridge"));
        let names: Vec<&str> = p.files.iter().map(|f| f.0).collect();
        assert_eq!(names, ["report.json", "hardware.json", "server.json", "screenshot.jpg", "logs.gz"]);
        assert!(p.sent.screenshot && !p.sent.exact_location && p.sent.logs);
    }

    #[test]
    fn validation() {
        let lim = Limits::default();
        let bad = |extra: serde_json::Value| parse(&body(extra), &lim).unwrap_err();
        assert_eq!(parse(b"not json", &lim).unwrap_err().0, 400);
        assert_eq!(bad(serde_json::json!({"install": "../../etc"})).0, 400);
        assert_eq!(bad(serde_json::json!({"install": "0123456789abcdef0123456789abcdeg"})).0, 400);
        assert_eq!(bad(serde_json::json!({"v": "1.0/../../x"})).0, 400);
        assert_eq!(bad(serde_json::json!({"report": {"kind": "../x"}})).0, 400);
        assert_eq!(bad(serde_json::json!({"report": [1, 2]})).0, 400, "not an object");
        assert_eq!(bad(serde_json::json!({"hardware": "text"})).0, 400);
        assert_eq!(bad(serde_json::json!({"filename": "x.exe"})).0, 400, "unknown fields refused");
        assert_eq!(bad(serde_json::json!({"sent": {"evil": true}})).0, 400);
        // a PNG / an executable as the screenshot
        assert_eq!(bad(serde_json::json!({"screenshot": b64(b"\x89PNG\r\n\x1a\nxxxx")})).0, 415);
        assert_eq!(bad(serde_json::json!({"screenshot": b64(b"MZ\x90\x00xxxxxxxx")})).0, 415);
        assert_eq!(bad(serde_json::json!({"logs": b64(b"plain text, not gzip at all")})).0, 415);
        // sizes: the screenshot, a JSON part, the logs decompressed (a bomb), the whole body
        let big = [&[0xFF, 0xD8, 0xFF][..], &vec![0u8; 1_600_000][..]].concat();
        assert_eq!(bad(serde_json::json!({"screenshot": b64(&big)})).0, 413);
        assert_eq!(bad(serde_json::json!({"server": {"x": "y".repeat(300_000)}})).0, 413);
        let bomb = gz(&vec![b'a'; 9 << 20]);
        assert!(bomb.len() < lim.max_logs);
        assert_eq!(bad(serde_json::json!({"logs": b64(&bomb)})).0, 413);
        assert_eq!(parse(&vec![b' '; lim.max_body + 1], &lim).unwrap_err().0, 413);
        // every part under its own cap but together over max_total
        let shot = [&[0xFF, 0xD8, 0xFF][..], &vec![7u8; 1_499_000][..]].concat();
        let json = serde_json::json!({"x": "z".repeat(255_000)});
        assert_eq!(bad(serde_json::json!({"screenshot": b64(&shot), "hardware": json.clone(), "server": json})).0, 413);
    }

    #[test]
    fn ids_and_dates() {
        let id = random_id();
        assert_eq!(norm_id(&id).unwrap(), id);
        assert_eq!(norm_id(&show_id(&id).to_ascii_lowercase()).unwrap(), id);
        for bad in ["", "ABCDEFG", "ABCDEFGHI", "ABCD/EFG", "../ABCDE", "ABCDEFGU", "ABCDEFG\u{0}"] {
            assert!(norm_id(bad).is_none(), "{bad}");
        }
        assert_eq!(date(0), "1970-01-01");
        assert_eq!(date(1_762_905_600), "2025-11-12");
        assert_eq!(day_of("2025-11-12"), Some(1_762_905_600 / 86400));
        for bad in ["2025-13-01", "2025-02-30", "25-11-12", "2025/11/12", "2025-11-1x", "+025-11-12"] {
            assert!(day_of(bad).is_none(), "{bad}");
        }
        assert_eq!(stamp(1_762_905_600 + 3661), "2025-11-12T01:01:01Z");
    }

    #[test]
    fn auth_and_query() {
        let t = Some("0123456789abcdefXYZ".to_string());
        assert!(authorized(&t, Some("Bearer 0123456789abcdefXYZ")));
        assert!(!authorized(&t, Some("Bearer 0123456789abcdefXY")));
        assert!(!authorized(&t, Some("0123456789abcdefXYZ")));
        assert!(!authorized(&t, None));
        assert!(!authorized(&None, Some("Bearer x")));
        let q = query("/reports/list?from=2025-11-01&limit=5&x");
        assert_eq!((q["from"].as_str(), q["limit"].as_str(), q.len()), ("2025-11-01", "5", 2));
    }

    fn stored(r: &mut Reports, ip: &str, install: &str, now: u64) -> Result<String, Err> {
        let mut b: serde_json::Value = serde_json::from_slice(&body(serde_json::json!({}))).unwrap();
        b["install"] = install.into();
        let p = parse(&serde_json::to_vec(&b).unwrap(), &r.lim)?;
        let t = r.admit(&p, ip.parse().unwrap(), now)?;
        let m = Reports::meta(&p, &t, now, "DE");
        Reports::write(&r.path_of(&t), &p, &m).unwrap();
        let id = t.id.clone();
        r.finish(t, Some(m));
        Ok(id)
    }

    #[test]
    fn rate_limits() {
        let lim = Limits::default();
        let mut r = Reports::new(tmpdir("rate"), lim.clone());
        let now = 1_762_905_600;
        let a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        for i in 0..lim.install_hour as u64 {
            stored(&mut r, "203.0.113.1", a, now + i).unwrap();
        }
        assert_eq!(stored(&mut r, "203.0.113.1", a, now + 20).unwrap_err().0, 429, "one over the hour's cap from an install");
        assert!(stored(&mut r, "203.0.113.1", a, now + 3600).is_ok(), "the next hour");
        // the day's cap per install
        let mut t = now + 7200;
        let step = 3600 / (lim.install_hour as u64 - 1);
        while stored(&mut r, &format!("198.51.100.{}", t % 200), a, t).is_ok() {
            t += step;
        }
        assert_eq!(r.by_install[a].len(), lim.install_day);
        // per address: other installs from one address, ip_hour an hour
        let ip = "192.0.2.9";
        let mut ok = 0;
        for i in 0..lim.ip_hour as u64 + 5 {
            if stored(&mut r, ip, &format!("{i:032x}"), now + 100_000 + i).is_ok() {
                ok += 1;
            }
        }
        assert_eq!(ok, lim.ip_hour);
        // bad requests from an address: refused once over the hour's attempts
        let ip: IpAddr = "192.0.2.10".parse().unwrap();
        for _ in 0..lim.ip_attempts_hour {
            r.attempt(ip, now).unwrap();
        }
        assert_eq!(r.attempt(ip, now + 10).unwrap_err().0, 429);
        assert!(r.attempt(ip, now + 3600).is_ok());
    }

    #[test]
    fn disk_cap_listing_files_and_retention() {
        let dir = tmpdir("store");
        let lim = Limits { disk_cap: 12_000, ..Limits::default() };
        let mut r = Reports::new(dir.clone(), lim);
        let day0 = 1_762_905_600; // 2025-11-12
        let a = stored(&mut r, "203.0.113.1", &format!("{:032x}", 1), day0).unwrap();
        let b = stored(&mut r, "203.0.113.2", &format!("{:032x}", 2), day0 + 86400).unwrap();
        // full: refused (507) until something goes
        let mut n = 2;
        while stored(&mut r, "203.0.113.3", &format!("{:032x}", 100 + n), day0 + 86400 + n).is_ok() {
            n += 1;
            assert!(n < 50);
        }
        assert_eq!(stored(&mut r, "203.0.113.4", &format!("{:032x}", 999), day0 + 86500).unwrap_err().0, 507);
        assert!(r.used() <= 12_000 + 2048);
        // the private list and a file
        let l = r.list(&query("/reports/list?date=2025-11-12"));
        assert_eq!(l["total"], 1);
        assert_eq!(l["reports"][0]["id"], a.as_str());
        let l = r.list(&query("/reports/list?limit=1"));
        assert_eq!(l["reports"][0]["date"], "2025-11-13");
        let l2 = r.list(&query(&format!("/reports/list?limit=1&after={}", l["next"].as_str().unwrap())));
        assert_ne!(l2["reports"][0]["id"], l["reports"][0]["id"]);
        assert!(r.list(&query("/reports/list?version=1.9.1-beta"))["total"].as_u64().unwrap() >= 2);
        assert_eq!(r.list(&query("/reports/list?version=1.8"))["total"], 0);
        let (p, ct) = r.file(&show_id(&b).to_ascii_lowercase(), "report.json").unwrap();
        assert_eq!(ct, "application/json");
        assert!(p.starts_with(&dir) && fs::read_to_string(p).unwrap().contains("Floating"));
        assert!(r.file(&a, "../meta.json").is_none());
        assert!(r.file(&a, "screenshot.jpg").is_none(), "not sent");
        assert!(r.file("../../etc", "report.json").is_none());
        assert!(r.file(&a, "meta.json").is_some());
        // a restart reads the index back
        let r2 = Reports::new(dir.clone(), Limits { disk_cap: 12_000, ..Limits::default() });
        assert_eq!(r2.count(), r.count());
        // a stray folder and a stale .tmp
        fs::create_dir_all(dir.join("not-a-date")).unwrap();
        // retention: day0's folder goes 90 days later, day0 + 1 stays
        assert_eq!(r.sweep(day0 + 89 * 86400), 0);
        assert_eq!(r.sweep(day0 + 90 * 86400), 1);
        assert!(r.file(&a, "report.json").is_none());
        assert!(r.file(&b, "report.json").is_some());
        assert!(dir.join("not-a-date").is_dir(), "only date folders are removed");
        assert!(stored(&mut r, "203.0.113.4", &format!("{:032x}", 999), day0 + 90 * 86400).is_ok(), "room again");
        let _ = fs::remove_dir_all(&dir);
    }
}
