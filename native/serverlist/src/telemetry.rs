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
//! GET  /stats/advanced  public since 2026-11-06 (the user: "i dont see much of an issue with the private page being
//!                   public ... i think its fine to keep exact country counts"; main.rs caches it a minute): versions,
//!                   systems, every country, BeamMP sessions, and per data source the requests, MB, errors and 429s per
//!                   hour over the last 7 days and in the last 24 h. (/stats/private: the same with the token.)
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
const SAMPLE_KEEP: u64 = 90 * 86400; // 5 min samples
const MINUTE_KEEP: u64 = 7 * 86400; // 1 min samples (the last hours' graphs)
const HOUR_KEEP: u64 = 20 * 365 * 86400; // the hours' player peaks: kept (the graphs over years)
const SOURCE_KEEP: u64 = 400 * 86400; // ... their data-source sums this long
const MAX_POINTS: u64 = 1000; // a range's points at most (buckets of the finest series that covers it)
const MAX_SPAN: u64 = 20 * 366 * 86400;
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
    // the performance section (perf.rs), only from players who chose "Performance data"
    #[serde(default)]
    pub perf: Option<crate::perf::PerfIn>,
}

impl Report {
    pub fn id(&self) -> String {
        self.id.to_ascii_lowercase()
    }
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

#[derive(Serialize, Deserialize)]
struct Saved {
    id: String,
    at: u64, // unix s it was last heard from
    v: String,
    os: String,
    cc: String,
    game: bool,
    mp: bool,
}

struct Install {
    seen: Instant,
    seen_unix: u64,
    v: String,
    os: String,
    cc: String,
    game: bool,
    mp: bool,
}

pub struct Telemetry {
    installs: HashMap<String, Install>,
    per_ip: HashMap<IpAddr, (Instant, Vec<String>)>,
    samples: VecDeque<(u64, u32, u32)>, // (unix s, online, in game), every SAMPLE_S
    minutes: VecDeque<(u64, u32, u32)>, // ... every minute
    last_minute: u64,
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
                                hours: BTreeMap::new(), dir, last_sample: 0, written: HashSet::new(),
                                minutes: VecDeque::new(), last_minute: 0 };
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
        // the installs heard from lately (a restart kept counting them: online did not drop to 0 until their next report)
        if let Ok(text) = fs::read_to_string(dir.join("installs.json")) {
            if let Ok(list) = serde_json::from_str::<Vec<Saved>>(&text) {
                let inst_now = Instant::now();
                for x in list {
                    let ago = Duration::from_secs(now.saturating_sub(x.at));
                    if ago < ONLINE_TTL {
                        if let Some(seen) = inst_now.checked_sub(ago) {
                            self.installs.insert(x.id, Install { seen, seen_unix: x.at, v: x.v, os: x.os, cc: x.cc,
                                                                 game: x.game, mp: x.mp });
                        }
                    }
                }
            }
        }
        if let Ok(text) = fs::read_to_string(dir.join("minutes.jsonl")) {
            for line in text.lines() {
                if let Ok((t, o, p)) = serde_json::from_str::<(u64, u32, u32)>(line) {
                    if now.saturating_sub(t) <= MINUTE_KEEP {
                        self.minutes.push_back((t, o, p));
                    }
                }
            }
        }
        if let Ok(text) = fs::read_to_string(dir.join("hours.jsonl")) {
            for line in text.lines() {
                if let Ok(mut h) = serde_json::from_str::<Hour>(line) {
                    if now.saturating_sub(h.h * 3600) <= HOUR_KEEP {
                        if now.saturating_sub(h.h * 3600) > SOURCE_KEEP {
                            h.sources.clear(); // (the peaks stay, the data-source sums go)
                        }
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
        let m: String = self.minutes.iter().map(|x| serde_json::to_string(x).unwrap() + "\n").collect();
        let _ = fs::write(dir.join("minutes.jsonl"), m);
        let h: String = self.hours.values().map(|x| serde_json::to_string(x).unwrap() + "\n").collect();
        let _ = fs::write(dir.join("hours.jsonl"), h);
    }

    fn append(&self, file: &str, line: String) {
        let Some(dir) = &self.dir else { return };
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(dir.join(file)) {
            let _ = f.write_all((line + "\n").as_bytes());
        }
    }

    fn save_installs(&self, now: Instant) {
        let Some(dir) = &self.dir else { return };
        let list: Vec<Saved> = self.installs.iter().filter(|(_, i)| now.duration_since(i.seen) < ONLINE_TTL)
            .map(|(id, i)| Saved { id: id.clone(), at: i.seen_unix, v: i.v.clone(), os: i.os.clone(), cc: i.cc.clone(),
                                   game: i.game, mp: i.mp }).collect();
        let tmp = dir.join("installs.json.tmp");
        if fs::write(&tmp, serde_json::to_string(&list).unwrap()).is_ok() {
            let _ = fs::rename(&tmp, dir.join("installs.json"));
        }
    }

    /// An install's (version, OS) as kept from its last report.
    pub fn info(&self, id: &str) -> Option<(String, String)> {
        self.installs.get(id).map(|i| (i.v.clone(), i.os.clone()))
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
        self.installs.insert(id, Install { seen: now, seen_unix: unix_now, v: clean(r.v, 24), os: clean(r.os, 12), cc,
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
        let minute = unix_now / 60 * 60;
        if minute > self.last_minute {
            self.last_minute = minute;
            self.save_installs(now);
            let (o, p) = self.online(now);
            self.minutes.push_back((minute, o, p));
            self.append("minutes.jsonl", serde_json::to_string(&(minute, o, p)).unwrap());
            while self.minutes.front().is_some_and(|s| unix_now.saturating_sub(s.0) > MINUTE_KEEP) {
                self.minutes.pop_front();
            }
        }
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
        let src_cut = unix_now.saturating_sub(SOURCE_KEEP) / 3600;
        for (_, h) in self.hours.range_mut(..src_cut) {
            h.sources.clear();
        }
        self.written.retain(|h| *h >= cut);
    }


    pub fn public(&self, now: Instant, unix_now: u64) -> serde_json::Value {
        let (o, p) = self.online(now);
        let day_ago = unix_now.saturating_sub(86400);
        let peak24 = self.samples.iter().filter(|s| s.0 >= day_ago).map(|s| s.1).max().unwrap_or(0).max(o);
        let (mut peak_all, mut peak_at) = (o, unix_now);
        let all = self.hours.values().map(|h| (h.h * 3600, h.online))
            .chain(self.samples.iter().map(|s| (s.0, s.1))).chain(self.minutes.iter().map(|s| (s.0, s.1)));
        for (t, n) in all {
            if n > peak_all {
                peak_all = n;
                peak_at = t;
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

    /// The live numbers (the page asks every few seconds): online, in game, the peaks, the public countries.
    pub fn now(&self, now: Instant, unix_now: u64) -> serde_json::Value {
        let p = self.public(now, unix_now);
        serde_json::json!({"t": unix_now, "online": p["online"], "playing": p["playing"], "peak24h": p["peak24h"],
                           "peakAll": p["peakAll"], "peakAllAt": p["peakAllAt"], "countries": p["countries"]})
    }

    /// Players over [from, to] (unix s): the finest series whose retention reaches `from` (1 min, 5 min, hourly
    /// peaks), in buckets of whole steps so at most MAX_POINTS come back, each bucket's highest online and in-game
    /// counts. The current moment's count closes a range that reaches now.
    pub fn range(&self, now: Instant, unix_now: u64, from: u64, to: u64) -> Result<serde_json::Value, (u16, &'static str)> {
        let to = to.min(unix_now);
        if from >= to || to - from < 60 || to - from > MAX_SPAN {
            return Err((400, "bad range"));
        }
        let span = to - from;
        // every series over the range, each where no finer one has data (a series starts later than the coarser ones:
        // the minutes at the collector's update, the 5 min samples at its start): bucket maxima mix them correctly
        let fine = self.minutes.front().map(|m| m.0).unwrap_or(u64::MAX);
        let mid = self.samples.front().map(|m| m.0).unwrap_or(u64::MAX).min(fine);
        let mut series: Vec<(u64, u32, u32)> = self.hours.range(from / 3600..=to / 3600)
            .filter(|(h, _)| (**h + 1) * 3600 <= mid).map(|(h, x)| (h * 3600, x.online, x.playing)).collect();
        series.extend(self.samples.iter().filter(|s| s.0 >= from && s.0 <= to && s.0 < fine));
        series.extend(self.minutes.iter().filter(|s| s.0 >= from && s.0 <= to));
        let mut step = 60;
        while span / step > MAX_POINTS {
            step = match step {
                60 => 300,
                300 => 900,
                900 => 3600,
                3600 => 3 * 3600,
                10800 => 6 * 3600,
                21600 => 86400,
                86400 => 7 * 86400,
                s => s * 2,
            };
        }
        let mut buckets: BTreeMap<u64, (u32, u32)> = BTreeMap::new();
        for (t, o, p) in series {
            let b = buckets.entry(t / step * step).or_default();
            b.0 = b.0.max(o);
            b.1 = b.1.max(p);
        }
        if to + 90 >= unix_now {
            let (o, p) = self.online(now);
            let b = buckets.entry(unix_now / step * step).or_default();
            b.0 = b.0.max(o);
            b.1 = b.1.max(p);
        }
        let points: Vec<(u64, u32, u32)> = buckets.into_iter().map(|(t, (o, p))| (t, o, p)).collect();
        let first = self.hours.keys().next().map(|h| h * 3600).into_iter()
            .chain(self.samples.front().map(|s| s.0)).chain(self.minutes.front().map(|s| s.0)).min();
        Ok(serde_json::json!({"from": from, "to": to, "step": step, "points": points, "dataSince": first}))
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
        Report { id: id.into(), v: Some("1.8.2".into()), os: Some("linux".into()), game: Some(game), mp: Some(false), sources: Some(s),
                 perf: None }
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
    fn ranges_pick_a_resolution_and_stay_bounded() {
        let mut t = Telemetry::new(None);
        let now = Instant::now();
        let u0 = 1_800_000_000 - 1_800_000_000 % 86400;
        let ip: IpAddr = "192.0.2.1".parse().unwrap();
        t.report(ip, "US".into(), rep(&format!("{:032x}", 3), true, 1), now, u0).unwrap();
        for k in 0..(3 * 86400 / 60) {
            t.tick(now, u0 + k * 60);           // three days of minutes
        }
        let u = u0 + 3 * 86400;
        let hour = t.range(now, u, u - 3600, u).unwrap();
        assert_eq!(hour["step"], 60);
        assert!(hour["points"].as_array().unwrap().len() >= 59);
        let three = t.range(now, u, u - 3 * 86400, u).unwrap();
        assert!(three["points"].as_array().unwrap().len() as u64 <= MAX_POINTS + 1);
        assert!(three["step"].as_u64().unwrap() >= 300);
        let decade = t.range(now, u, u - 10 * 365 * 86400, u).unwrap();
        assert!(decade["step"].as_u64().unwrap() >= 86400);
        assert!(decade["points"].as_array().unwrap().len() as u64 <= MAX_POINTS + 1);
        assert_eq!(t.range(now, u, u, u - 10).unwrap_err().0, 400);
    }

    #[test]
    fn a_restart_keeps_who_is_online_and_ranges_fall_back_to_older_series() {
        let dir = std::env::temp_dir().join(format!("bnge_tel_r_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let _ = fs::create_dir_all(&dir);
        let u = unix();
        let ip: IpAddr = "192.0.2.9".parse().unwrap();
        {
            let mut t = Telemetry::new(Some(dir.clone()));
            let now = Instant::now();
            // 5 min samples over the last 6 hours (as written before an update), no minutes
            for k in 0..72u64 {
                let x = (u - 6 * 3600 + k * 300, 4u32, 2u32);
                t.samples.push_back(x);
                t.append("samples.jsonl", serde_json::to_string(&x).unwrap());
            }
            t.report(ip, "US".into(), rep(&format!("{:032x}", 11), true, 1), now, u).unwrap();
            t.tick(now, u);             // (writes installs.json)
        }
        let t = Telemetry::new(Some(dir.clone()));
        let now = Instant::now();
        assert_eq!(t.online(now), (1, 1));                       // still online after the restart
        let r = t.range(now, u, u - 6 * 3600, u).unwrap();
        assert!(r["points"].as_array().unwrap().len() > 50);    // the samples, not just the new minutes
        assert_eq!(t.public(now, u)["peakAll"], 4);              // the peak from the samples
        let _ = fs::remove_dir_all(&dir);
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
