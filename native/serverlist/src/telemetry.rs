//! BeamNG.Earth's usage telemetry (the user, 2026-11-06: "create a telemetry system. Have it track current players and
//! a way to monitor the stress we're putting on the data sources ... I want the statistics to be public, but in a safe
//! way. Like how steam tracks player count. More details info could be private").
//!
//! POST /telemetry   {"id": 32 hex (a random install id), "v", "os", "game", "mp", "interval",
//!                    "sources": {host: {"req", "mb", "ok", "e4xx", "e404", "e429", "e5xx", "err", "retry"}}}
//!                   from each player's companion server (server/telemetry.py) every 5 minutes. The sender's address
//!                   gives its country (Geo) and is then dropped: no address, name or position is kept.
//! GET  /stats       public: players online now (an install heard from in ONLINE_TTL) and in game, the peaks, the
//!                   last 48 h (5 min samples), daily peaks, countries with at least K_ANON players (the rest "other").
//! GET  /stats/private  with "Authorization: Bearer <BNGE_TELEMETRY_TOKEN>" (404 without the variable): versions,
//!                   systems, every country, BeamMP sessions, and per data source the requests, MB, errors and 429s per
//!                   hour over the last 7 days and in the last 24 h.
//!
//! Kept in BNGE_TELEMETRY_DIR (default: next to the binary): samples.jsonl (5 min samples, SAMPLE_KEEP) and hours.jsonl
//! (an hour's peaks and data-source sums, HOUR_KEEP), appended and compacted at start.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::IpAddr;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const ONLINE_TTL: Duration = Duration::from_secs(11 * 60); // two reports and a minute
const MIN_GAP: Duration = Duration::from_secs(60); // reports from one install closer than this are refused
const SAMPLE_S: u64 = 300;
const SAMPLE_KEEP: u64 = 30 * 86400;
const HOUR_KEEP: u64 = 400 * 86400;
const MAX_INSTALLS: usize = 200_000;
const MAX_IDS_PER_IP: usize = 32; // distinct installs one address may report for in an hour
const MAX_SOURCES: usize = 64;
pub const K_ANON: u32 = 5; // a country shown publicly from this many players online

#[derive(Deserialize, Default)]
pub struct SourceIn {
    req: Option<u64>,
    mb: Option<f64>,
    ok: Option<u64>,
    e4xx: Option<u64>,
    e404: Option<u64>,
    e429: Option<u64>,
    e5xx: Option<u64>,
    err: Option<u64>,
    retry: Option<u64>,
}

#[derive(Deserialize)]
pub struct Report {
    id: String,
    v: Option<String>,
    os: Option<String>,
    game: Option<bool>,
    mp: Option<bool>,
    sources: Option<HashMap<String, SourceIn>>,
}

#[derive(Serialize, Deserialize, Default, Clone, Debug, PartialEq)]
pub struct Source {
    pub req: u64,
    pub mb: f64,
    pub ok: u64,
    pub e4xx: u64,
    pub e404: u64,
    pub e429: u64,
    pub e5xx: u64,
    pub err: u64,
    pub retry: u64,
}

impl Source {
    fn add(&mut self, o: &Source) {
        self.req += o.req;
        self.mb += o.mb;
        self.ok += o.ok;
        self.e4xx += o.e4xx;
        self.e404 += o.e404;
        self.e429 += o.e429;
        self.e5xx += o.e5xx;
        self.err += o.err;
        self.retry += o.retry;
    }
}

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct Hour {
    pub h: u64, // unix hour
    pub online: u32,
    pub playing: u32,
    pub sources: BTreeMap<String, Source>,
}

struct Install {
    seen: Instant,
    v: String,
    os: String,
    cc: String,
    game: bool,
    mp: bool,
}

pub struct Telemetry {
    installs: HashMap<String, Install>,
    per_ip: HashMap<IpAddr, (Instant, Vec<String>)>,
    samples: VecDeque<(u64, u32, u32)>, // (unix s, online, in game)
    hours: BTreeMap<u64, Hour>,
    dir: Option<PathBuf>,
    last_sample: u64,
    written: HashSet<u64>, // hours already in hours.jsonl
}

fn unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn clean(s: Option<String>, max: usize) -> String {
    s.unwrap_or_default().chars().filter(|c| c.is_ascii_graphic() || *c == ' ').take(max).collect::<String>().trim().to_string()
}

fn host_ok(h: &str) -> bool {
    !h.is_empty() && h.len() <= 64 && h.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | ':'))
}

impl Telemetry {
    pub fn new(dir: Option<PathBuf>) -> Telemetry {
        let mut t = Telemetry { installs: HashMap::new(), per_ip: HashMap::new(), samples: VecDeque::new(),
                                hours: BTreeMap::new(), dir, last_sample: 0, written: HashSet::new() };
        t.load();
        t
    }

    fn load(&mut self) {
        let Some(dir) = self.dir.clone() else { return };
        let now = unix();
        if let Ok(text) = fs::read_to_string(dir.join("samples.jsonl")) {
            for line in text.lines() {
                if let Ok((t, o, p)) = serde_json::from_str::<(u64, u32, u32)>(line) {
                    if now.saturating_sub(t) <= SAMPLE_KEEP {
                        self.samples.push_back((t, o, p));
                    }
                }
            }
        }
        if let Ok(text) = fs::read_to_string(dir.join("hours.jsonl")) {
            for line in text.lines() {
                if let Ok(h) = serde_json::from_str::<Hour>(line) {
                    if now.saturating_sub(h.h * 3600) <= HOUR_KEEP {
                        self.written.insert(h.h);
                        self.hours.insert(h.h, h);
                    }
                }
            }
        }
        // (compacted: only what is kept)
        let _ = fs::create_dir_all(&dir);
        let s: String = self.samples.iter().map(|x| serde_json::to_string(x).unwrap() + "\n").collect();
        let _ = fs::write(dir.join("samples.jsonl"), s);
        let h: String = self.hours.values().map(|x| serde_json::to_string(x).unwrap() + "\n").collect();
        let _ = fs::write(dir.join("hours.jsonl"), h);
    }

    fn append(&self, file: &str, line: String) {
        let Some(dir) = &self.dir else { return };
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(dir.join(file)) {
            let _ = f.write_all((line + "\n").as_bytes());
        }
    }

    /// (online, in game) now.
    pub fn online(&self, now: Instant) -> (u32, u32) {
        let mut o = 0;
        let mut p = 0;
        for i in self.installs.values() {
            if now.duration_since(i.seen) < ONLINE_TTL {
                o += 1;
                p += i.game as u32;
            }
        }
        (o, p)
    }

    pub fn report(&mut self, ip: IpAddr, cc: String, r: Report, now: Instant, unix_now: u64) -> Result<(), (u16, &'static str)> {
        let id = r.id.to_ascii_lowercase();
        if id.len() != 32 || !id.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err((400, "bad id"));
        }
        if let Some(i) = self.installs.get(&id) {
            if now.duration_since(i.seen) < MIN_GAP {
                return Err((429, "too soon"));
            }
        } else if self.installs.len() >= MAX_INSTALLS {
            self.tick(now, unix_now);
            if self.installs.len() >= MAX_INSTALLS {
                return Err((503, "full"));
            }
        }
        let slot = self.per_ip.entry(ip).or_insert((now, Vec::new()));
        if now.duration_since(slot.0) > Duration::from_secs(3600) {
            *slot = (now, Vec::new());
        }
        if !slot.1.contains(&id) {
            if slot.1.len() >= MAX_IDS_PER_IP {
                return Err((429, "too many installs from this address"));
            }
            slot.1.push(id.clone());
        }
        let hour = unix_now / 3600;
        let entry = self.hours.entry(hour).or_insert_with(|| Hour { h: hour, ..Default::default() });
        if let Some(src) = r.sources {
            for (host, s) in src.into_iter().take(MAX_SOURCES) {
                if !host_ok(&host) || entry.sources.len() >= 512 && !entry.sources.contains_key(&host) {
                    continue;
                }
                let c = |v: Option<u64>| v.unwrap_or(0).min(10_000_000);
                let add = Source { req: c(s.req), mb: s.mb.unwrap_or(0.0).clamp(0.0, 1.0e6), ok: c(s.ok), e4xx: c(s.e4xx),
                                   e404: c(s.e404), e429: c(s.e429), e5xx: c(s.e5xx), err: c(s.err), retry: c(s.retry) };
                entry.sources.entry(host.to_ascii_lowercase()).or_default().add(&add);
            }
        }
        self.installs.insert(id, Install { seen: now, v: clean(r.v, 24), os: clean(r.os, 12), cc,
                                           game: r.game.unwrap_or(false), mp: r.mp.unwrap_or(false) });
        let (o, p) = self.online(now);
        let e = self.hours.get_mut(&hour).unwrap();
        e.online = e.online.max(o);
        e.playing = e.playing.max(p);
        Ok(())
    }

    /// Every few seconds (main): forget long-gone installs, take the 5 min sample, close finished hours.
    pub fn tick(&mut self, now: Instant, unix_now: u64) {
        self.installs.retain(|_, i| now.duration_since(i.seen) < Duration::from_secs(86400));
        self.per_ip.retain(|_, s| now.duration_since(s.0) < Duration::from_secs(3600));
        let slot = unix_now / SAMPLE_S * SAMPLE_S;
        if slot > self.last_sample {
            self.last_sample = slot;
            let (o, p) = self.online(now);
            self.samples.push_back((slot, o, p));
            self.append("samples.jsonl", serde_json::to_string(&(slot, o, p)).unwrap());
            while self.samples.front().is_some_and(|s| unix_now.saturating_sub(s.0) > SAMPLE_KEEP) {
                self.samples.pop_front();
            }
            let hour = unix_now / 3600;
            let e = self.hours.entry(hour).or_insert_with(|| Hour { h: hour, ..Default::default() });
            e.online = e.online.max(o);
            e.playing = e.playing.max(p);
        }
        // finished hours to disk once (the current one stays in memory until it ends)
        let done: Vec<u64> = self.hours.keys().copied().filter(|h| *h < unix_now / 3600).collect();
        for h in done {
            if let Some(x) = self.hours.get(&h) {
                if (!x.sources.is_empty() || x.online > 0) && !self.written.contains(&h) {
                    self.append("hours.jsonl", serde_json::to_string(x).unwrap());
                    self.written.insert(h);
                }
            }
        }
        let cut = unix_now.saturating_sub(HOUR_KEEP) / 3600;
        self.hours.retain(|h, _| *h >= cut);
        self.written.retain(|h| *h >= cut);
    }


    pub fn public(&self, now: Instant, unix_now: u64) -> serde_json::Value {
        let (o, p) = self.online(now);
        let day_ago = unix_now.saturating_sub(86400);
        let peak24 = self.samples.iter().filter(|s| s.0 >= day_ago).map(|s| s.1).max().unwrap_or(0).max(o);
        let (mut peak_all, mut peak_at) = (o, unix_now);
        for h in self.hours.values() {
            if h.online > peak_all {
                peak_all = h.online;
                peak_at = h.h * 3600;
            }
        }
        let hist: Vec<(u64, u32, u32)> =
            self.samples.iter().filter(|s| s.0 + 48 * 3600 >= unix_now).copied().collect();
        let mut daily: BTreeMap<u64, (u32, u32)> = BTreeMap::new();
        for h in self.hours.values() {
            let d = daily.entry(h.h / 24).or_default();
            d.0 = d.0.max(h.online);
            d.1 = d.1.max(h.playing);
        }
        let daily: Vec<(u64, u32, u32)> = daily.into_iter().map(|(d, (a, b))| (d * 86400, a, b)).collect();
        let mut cc: HashMap<&str, u32> = HashMap::new();
        for i in self.installs.values() {
            if now.duration_since(i.seen) < ONLINE_TTL {
                *cc.entry(if i.cc.is_empty() { "??" } else { i.cc.as_str() }).or_default() += 1;
            }
        }
        let mut countries: Vec<(String, u32)> = Vec::new();
        let mut other = 0;
        for (c, n) in cc {
            if n >= K_ANON && c != "??" {
                countries.push((c.to_string(), n));
            } else {
                other += n;
            }
        }
        countries.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        if other > 0 {
            countries.push(("other".into(), other));
        }
        serde_json::json!({"online": o, "playing": p, "peak24h": peak24, "peakAll": peak_all, "peakAllAt": peak_at,
                           "history": hist, "daily": daily, "countries": countries, "sampleSeconds": SAMPLE_S,
                           "onlineMeans": "a BeamNG.Earth app running now; playing: with the game connected"})
    }

    pub fn private(&self, now: Instant, unix_now: u64) -> serde_json::Value {
        let mut versions: BTreeMap<String, u32> = BTreeMap::new();
        let mut systems: BTreeMap<String, u32> = BTreeMap::new();
        let mut countries: BTreeMap<String, u32> = BTreeMap::new();
        let mut mp = 0;
        for i in self.installs.values() {
            if now.duration_since(i.seen) < ONLINE_TTL {
                *versions.entry(i.v.clone()).or_default() += 1;
                *systems.entry(i.os.clone()).or_default() += 1;
                *countries.entry(if i.cc.is_empty() { "??".into() } else { i.cc.clone() }).or_default() += 1;
                mp += i.mp as u32;
            }
        }
        let week = unix_now.saturating_sub(7 * 86400) / 3600;
        let day = unix_now.saturating_sub(86400) / 3600;
        let mut last24: BTreeMap<String, Source> = BTreeMap::new();
        let mut hourly: Vec<serde_json::Value> = Vec::new();
        for h in self.hours.range(week..) {
            let src: BTreeMap<&String, &Source> = h.1.sources.iter().collect();
            if *h.0 >= day {
                for (k, s) in &src {
                    last24.entry((*k).clone()).or_default().add(s);
                }
            }
            hourly.push(serde_json::json!({"t": h.0 * 3600, "online": h.1.online, "playing": h.1.playing, "sources": src}));
        }
        serde_json::json!({"versions": versions, "systems": systems, "countries": countries, "beammp": mp,
                           "sources24h": last24, "hourly": hourly})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rep(id: &str, game: bool, req: u64) -> Report {
        let mut s = HashMap::new();
        s.insert("overturemaps-us-west-2.s3.amazonaws.com".to_string(), SourceIn { req: Some(req), mb: Some(1.5), e429: Some(1), ..Default::default() });
        Report { id: id.into(), v: Some("1.8.2".into()), os: Some("linux".into()), game: Some(game), mp: Some(false), sources: Some(s) }
    }

    #[test]
    fn counts_rate_limits_and_k_anonymity() {
        let mut t = Telemetry::new(None);
        let now = Instant::now();
        let u = 1_800_000_000;
        let ip: IpAddr = "203.0.113.5".parse().unwrap();
        for k in 0..6 {
            let id = format!("{:032x}", k + 1);
            t.report(ip, if k < 5 { "DE".into() } else { "FR".into() }, rep(&id, k % 2 == 0, 10), now, u).unwrap();
        }
        assert_eq!(t.online(now), (6, 3));
        // the same install again within a minute: refused
        assert_eq!(t.report(ip, "DE".into(), rep(&format!("{:032x}", 1), true, 1), now, u).unwrap_err().0, 429);
        assert_eq!(t.report(ip, "DE".into(), rep("not-hex", true, 1), now, u).unwrap_err().0, 400);
        let p = t.public(now, u);
        assert_eq!(p["online"], 6);
        // Germany (5) shown, France (1) only as "other"
        assert_eq!(p["countries"], serde_json::json!([["DE", 5], ["other", 1]]));
        let q = t.private(now, u);
        assert_eq!(q["countries"]["FR"], 1);
        assert_eq!(q["sources24h"]["overturemaps-us-west-2.s3.amazonaws.com"]["req"], 60);
        assert_eq!(q["sources24h"]["overturemaps-us-west-2.s3.amazonaws.com"]["e429"], 6);
        // gone after the online window
        let later = now + ONLINE_TTL + Duration::from_secs(1);
        assert_eq!(t.online(later), (0, 0));
    }

    #[test]
    fn samples_and_hours_survive_a_restart() {
        let dir = std::env::temp_dir().join(format!("bnge_tel_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let now = Instant::now();
        let u = 1_800_000_000 - 1_800_000_000 % 3600 + 10;
        let ip: IpAddr = "198.51.100.7".parse().unwrap();
        {
            let mut t = Telemetry::new(Some(dir.clone()));
            t.report(ip, "US".into(), rep(&format!("{:032x}", 9), true, 7), now, u).unwrap();
            t.tick(now, u);
            t.tick(now, u + 3600);        // (the hour is over: written)
        }
        let t = Telemetry::new(Some(dir.clone()));
        assert_eq!(t.samples.len(), 2);     // (both ticks: one sample each)
        let h = t.hours.get(&(u / 3600)).unwrap();
        assert_eq!(h.online, 1);
        assert_eq!(h.sources.values().next().unwrap().req, 7);
        let _ = fs::remove_dir_all(&dir);
    }
}
