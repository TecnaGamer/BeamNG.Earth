//! On-demand builds of the 1 m aerial packs (the user, 2026-11-13: prebaked for metro areas and road corridors, the rest
//! built the first time a player drives there; the game's server/aerialpacks.py asks, tools/build_aerial_packs.py
//! builds). This is only the front door: it checks, dedupes and rate-limits the requests and leaves one file per cell in
//! a spool folder; the builder (a separate service on the same machine, holding the R2 credentials, never this one)
//! takes them oldest first, builds the cell, uploads its packs and coverage file and writes done/<x>_<y> (or
//! none/<x>_<y>: no open photos there).
//!
//! POST /aerial/request {"v": 1, "cell": [x, y]}  (x, y: a zoom-11 tile)
//!   -> {"state": "queued", "pos": n} | {"state": "known"} (queued, built or known empty before) |
//!      {"state": "refused"} (outside the areas with open photos, a bad body) | {"state": "limited"} (rate limits)
//! GET  /aerial/status?cell=x,y -> {"state": "queued" | "building" | "done" | "none" | "unknown"}
//!
//! Limits: PER_IP_HOUR / PER_IP_DAY new cells per address, DAY_MAX new cells a day in all, QUEUE_MAX waiting. Nothing
//! about the asker is stored (the address only in memory, for the limits). Env: BNGE_AERIAL_SPOOL (the folder; unset:
//! the endpoint answers "refused").

use std::collections::{HashMap, HashSet};
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const PER_IP_HOUR: usize = 12;
pub const PER_IP_DAY: usize = 40;
pub const DAY_MAX: usize = 600;
pub const QUEUE_MAX: usize = 3000;
const CELL_Z: u32 = 11;

/// (south, west, north, east) degrees with open aerial photos the builder can read (NAIP: the lower 48; the open
/// European agencies). A cell outside every box is refused before it is queued.
pub const AREAS: &[(f64, f64, f64, f64)] = &[
    (24.3, -125.0, 49.5, -66.8),  // US lower 48 (NAIP)
    (41.3, -5.2, 51.1, 9.6),      // France (IGN BD ORTHO)
    (35.9, -9.4, 43.8, 4.4),      // Spain (PNOA)
    (36.9, -9.6, 42.2, -6.2),     // Portugal (DGT)
    (45.8, 5.9, 47.9, 10.5),      // Switzerland (swisstopo)
    (46.3, 9.5, 49.1, 17.2),      // Austria (basemap.at)
    (47.2, 5.8, 55.1, 15.1),      // Germany (the states' open DOPs)
    (50.7, 3.3, 53.6, 7.3),       // Netherlands (PDOK)
    (49.4, 2.5, 51.6, 6.5),       // Belgium, Luxembourg
    (48.5, 12.0, 51.1, 18.9),     // Czechia (ČÚZK)
    (49.0, 14.1, 54.9, 24.2),     // Poland (GUGiK)
    (57.5, 21.7, 59.7, 28.3),     // Estonia (Maa-amet)
];

fn cell_ll(x: u32, y: u32) -> (f64, f64, f64, f64) {
    let n = (1u32 << CELL_Z) as f64;
    let lon = |v: f64| v / n * 360.0 - 180.0;
    let lat = |v: f64| (std::f64::consts::PI * (1.0 - 2.0 * v / n)).sinh().atan().to_degrees();
    (lat(y as f64 + 1.0), lon(x as f64), lat(y as f64), lon(x as f64 + 1.0))
}

pub fn in_areas(x: u32, y: u32) -> bool {
    let (s, w, n, e) = cell_ll(x, y);
    AREAS.iter().any(|&(as_, aw, an, ae)| e > aw && w < ae && n > as_ && s < an)
}

pub struct Aerial {
    spool: Option<PathBuf>,
    asked: HashMap<IpAddr, Vec<Instant>>,
    day: Vec<Instant>,
    known: HashSet<(u32, u32)>,
}

fn name(c: (u32, u32)) -> String {
    format!("{}_{}", c.0, c.1)
}

impl Aerial {
    pub fn new(spool: Option<PathBuf>) -> Aerial {
        if let Some(d) = &spool {
            for sub in ["queue", "building", "done", "none"] {
                let _ = std::fs::create_dir_all(d.join(sub));
            }
        }
        Aerial { spool, asked: HashMap::new(), day: Vec::new(), known: HashSet::new() }
    }

    fn state_of(&self, d: &Path, c: (u32, u32)) -> &'static str {
        for (sub, st) in [("done", "done"), ("none", "none"), ("building", "building"), ("queue", "queued")] {
            if d.join(sub).join(name(c)).exists() {
                return st;
            }
        }
        "unknown"
    }

    /// The answer to a request body from `ip` (JSON text).
    pub fn request(&mut self, ip: IpAddr, body: &[u8], now: Instant) -> (u16, String) {
        let Some(d) = self.spool.clone() else { return (503, r#"{"state":"refused"}"#.into()) };
        let cell = serde_json::from_slice::<serde_json::Value>(body).ok().and_then(|v| {
            if v.get("v")?.as_u64()? != 1 {
                return None;
            }
            let a = v.get("cell")?.as_array()?;
            let (x, y) = (a.first()?.as_u64()?, a.get(1)?.as_u64()?);
            (a.len() == 2 && x < 1 << CELL_Z && y < 1 << CELL_Z).then_some((x as u32, y as u32))
        });
        let Some(c) = cell else { return (400, r#"{"state":"refused"}"#.into()) };
        if !in_areas(c.0, c.1) {
            return (200, r#"{"state":"refused"}"#.into());
        }
        if self.known.contains(&c) || self.state_of(&d, c) != "unknown" {
            self.known.insert(c);
            return (200, r#"{"state":"known"}"#.into());
        }
        let hour = Duration::from_secs(3600);
        let dayd = Duration::from_secs(86400);
        self.day.retain(|t| now.duration_since(*t) < dayd);
        let mine = self.asked.entry(ip).or_default();
        mine.retain(|t| now.duration_since(*t) < dayd);
        let last_hour = mine.iter().filter(|t| now.duration_since(**t) < hour).count();
        if last_hour >= PER_IP_HOUR || mine.len() >= PER_IP_DAY || self.day.len() >= DAY_MAX {
            return (429, r#"{"state":"limited"}"#.into());
        }
        let queued = std::fs::read_dir(d.join("queue")).map(|r| r.count()).unwrap_or(0);
        if queued >= QUEUE_MAX {
            return (429, r#"{"state":"limited"}"#.into());
        }
        let p = d.join("queue").join(name(c));
        let tmp = d.join("queue").join(format!(".{}.tmp", name(c)));
        if std::fs::write(&tmp, b"").and_then(|_| std::fs::rename(&tmp, &p)).is_err() {
            return (500, r#"{"state":"error"}"#.into());
        }
        mine.push(now);
        self.day.push(now);
        self.known.insert(c);
        if self.asked.len() > 100_000 {
            self.asked.retain(|_, v| v.iter().any(|t| now.duration_since(*t) < dayd));
        }
        (200, format!(r#"{{"state":"queued","pos":{}}}"#, queued + 1))
    }

    /// GET /aerial/status?cell=x,y
    pub fn status(&self, query: &str) -> (u16, String) {
        let Some(d) = &self.spool else { return (503, r#"{"state":"unknown"}"#.into()) };
        let c = query.split('&').find_map(|kv| kv.strip_prefix("cell=")).and_then(|v| {
            let v = v.replace("%2C", ",").replace("%2c", ",");
            let (a, b) = v.split_once(',')?;
            Some((a.parse::<u32>().ok()?, b.parse::<u32>().ok()?))
        });
        match c {
            Some(c) if c.0 < 1 << CELL_Z && c.1 < 1 << CELL_Z => {
                (200, format!(r#"{{"state":"{}"}}"#, self.state_of(d, c)))
            }
            _ => (400, r#"{"state":"unknown"}"#.into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests() {
        let d = std::env::temp_dir().join(format!("bnge-aerial-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        let mut a = Aerial::new(Some(d.clone()));
        let ip: IpAddr = "192.0.2.1".parse().unwrap();
        let t = Instant::now();
        assert!(in_areas(587, 782)); // Baltimore
        assert!(!in_areas(1024, 1024)); // the Gulf of Guinea
        assert_eq!(a.request(ip, br#"{"v":1,"cell":[587,782]}"#, t).1, r#"{"state":"queued","pos":1}"#);
        assert_eq!(a.request(ip, br#"{"v":1,"cell":[587,782]}"#, t).1, r#"{"state":"known"}"#);
        assert_eq!(a.status("cell=587,782").1, r#"{"state":"queued"}"#);
        assert_eq!(a.request(ip, br#"{"v":1,"cell":[1024,1024]}"#, t).1, r#"{"state":"refused"}"#);
        assert_eq!(a.request(ip, br#"{"v":2,"cell":[587,782]}"#, t).0, 400);
        assert_eq!(a.request(ip, br#"{"v":1,"cell":[5870,782]}"#, t).0, 400);
        for i in 1..PER_IP_HOUR as u32 {
            assert_eq!(a.request(ip, format!(r#"{{"v":1,"cell":[{},782]}}"#, 587 + i).as_bytes(), t).0, 200);
        }
        assert_eq!(a.request(ip, br#"{"v":1,"cell":[500,782]}"#, t).1, r#"{"state":"limited"}"#);
        let other: IpAddr = "192.0.2.2".parse().unwrap();
        assert_eq!(a.request(other, br#"{"v":1,"cell":[500,782]}"#, t).0, 200);
        std::fs::rename(d.join("queue").join("500_782"), d.join("done").join("500_782")).unwrap();
        assert_eq!(a.status("cell=500%2C782").1, r#"{"state":"done"}"#);
        let _ = std::fs::remove_dir_all(&d);
    }
}
