//! Unique players over days without keeping any install id (the user, 2026-11-11: "update the stats page to show the
//! total uniqe players without needing to update the clients"): one HyperLogLog sketch per UTC day (P bits of index:
//! 2^P one-byte registers, ~1.6 % standard error at P = 12), fed with each report's install id. A sketch holds only
//! the longest run of zero bits seen per register, so no id can be read back from it (the collector's privacy rule:
//! perf.rs keeps its per-day distinct counts through an in-memory keyed hash for the same reason). Any span of days is
//! the registers' maximum, so the last 24 h / 7 days / 30 days / all time come from the same files.
//! Files: <dir>/uniques/<unix day>.hll (2^P bytes), written when changed (tick, once a minute).
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::PathBuf;

pub const P: u32 = 12;
const M: usize = 1 << P;

/// 64 well-mixed bits of an id: FNV-1a then the splitmix64 finaliser (fixed: the same id gives the same bits after a
/// restart or a rebuild, which std's hasher does not promise).
pub fn hash(id: &str) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in id.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h ^= h >> 30;
    h = h.wrapping_mul(0xbf58476d1ce4e5b9);
    h ^= h >> 27;
    h = h.wrapping_mul(0x94d049bb133111eb);
    h ^ (h >> 31)
}

fn add(regs: &mut [u8], h: u64) -> bool {
    let idx = (h >> (64 - P)) as usize;
    let w = (h << P) | (1u64 << (P - 1)); // (a sentinel bit: rho at most 64 - P + 1)
    let rho = (w.leading_zeros() + 1) as u8;
    if rho > regs[idx] {
        regs[idx] = rho;
        true
    } else {
        false
    }
}

/// The distinct ids a sketch's registers hold, estimated (HyperLogLog with the small-range linear-counting correction).
pub fn estimate(regs: &[u8]) -> f64 {
    let m = regs.len() as f64;
    let alpha = 0.7213 / (1.0 + 1.079 / m);
    let mut sum = 0.0;
    let mut zeros = 0usize;
    for &r in regs {
        sum += 2f64.powi(-(r as i32));
        zeros += (r == 0) as usize;
    }
    let e = alpha * m * m / sum;
    if e <= 2.5 * m && zeros > 0 {
        m * (m / zeros as f64).ln()
    } else {
        e
    }
}

pub struct Uniques {
    days: BTreeMap<u64, Vec<u8>>,
    dirty: HashSet<u64>,
    dir: Option<PathBuf>,
}

impl Uniques {
    pub fn new(dir: Option<PathBuf>) -> Uniques {
        let mut u = Uniques { days: BTreeMap::new(), dirty: HashSet::new(), dir: dir.map(|d| d.join("uniques")) };
        if let Some(d) = &u.dir {
            if let Ok(rd) = fs::read_dir(d) {
                for e in rd.flatten() {
                    let name = e.file_name().to_string_lossy().to_string();
                    let Some(day) = name.strip_suffix(".hll").and_then(|s| s.parse::<u64>().ok()) else { continue };
                    if let Ok(b) = fs::read(e.path()) {
                        if b.len() == M {
                            u.days.insert(day, b);
                        }
                    }
                }
            }
        }
        u
    }

    /// An install heard from at unix second `at`.
    pub fn saw(&mut self, id: &str, at: u64) {
        let day = at / 86400;
        let regs = self.days.entry(day).or_insert_with(|| vec![0u8; M]);
        if add(regs, hash(id)) {
            self.dirty.insert(day);
        }
    }

    /// The changed days written (tick).
    pub fn save(&mut self) {
        let Some(d) = &self.dir else {
            self.dirty.clear();
            return;
        };
        if self.dirty.is_empty() {
            return;
        }
        let _ = fs::create_dir_all(d);
        for day in std::mem::take(&mut self.dirty) {
            if let Some(regs) = self.days.get(&day) {
                let tmp = d.join(format!("{day}.hll.tmp"));
                if fs::write(&tmp, regs).is_ok() {
                    let _ = fs::rename(&tmp, d.join(format!("{day}.hll")));
                }
            }
        }
    }

    /// Distinct installs on the days from `from_day` on (all: 0), estimated and rounded.
    pub fn since(&self, from_day: u64) -> u64 {
        let mut regs = vec![0u8; M];
        let mut any = false;
        for (_, r) in self.days.range(from_day..) {
            any = true;
            for (a, b) in regs.iter_mut().zip(r) {
                *a = (*a).max(*b);
            }
        }
        if any { estimate(&regs).round() as u64 } else { 0 }
    }

    /// The first day counted (unix day), if any.
    pub fn first_day(&self) -> Option<u64> {
        self.days.keys().next().copied()
    }

    /// {"day", "week", "month", "all", "since"} for the stats page: the last 1 / 7 / 30 UTC days (today included).
    pub fn view(&self, unix_now: u64) -> serde_json::Value {
        let today = unix_now / 86400;
        serde_json::json!({
            "day": self.since(today),
            "week": self.since(today.saturating_sub(6)),
            "month": self.since(today.saturating_sub(29)),
            "all": self.since(0),
            "since": self.first_day().map(|d| d * 86400),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(i: u32) -> String {
        format!("{:032x}", (i as u128).wrapping_mul(0x9e3779b97f4a7c15f39cc0605cedc835))
    }

    #[test]
    fn estimates_within_a_few_percent() {
        for &n in &[1u32, 10, 100, 1000, 5000, 20000, 100000] {
            let mut u = Uniques::new(None);
            for i in 0..n {
                u.saw(&id(i), 20000 * 86400);
                u.saw(&id(i), 20000 * 86400 + 600); // (the same install again: once)
            }
            let e = u.since(0) as f64;
            let err = (e - n as f64).abs() / n as f64;
            assert!(err < 0.05 || (e - n as f64).abs() <= 1.0, "n {n}: {e}");
        }
    }

    #[test]
    fn days_merge_and_survive_a_restart() {
        let dir = std::env::temp_dir().join(format!("uniques_test_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let mut u = Uniques::new(Some(dir.clone()));
        let d0 = 20000 * 86400;
        for i in 0..1000 {
            u.saw(&id(i), d0);
        }
        for i in 500..1500 {
            u.saw(&id(i), d0 + 86400 * 3); // (500 of them again three days later)
        }
        u.save();
        let v = Uniques::new(Some(dir.clone())).view(d0 + 86400 * 3);
        let all = v["all"].as_u64().unwrap() as f64;
        assert!((all - 1500.0).abs() / 1500.0 < 0.05, "{all}");
        let day = v["day"].as_u64().unwrap() as f64;
        assert!((day - 1000.0).abs() / 1000.0 < 0.05, "{day}");
        assert_eq!(v["since"].as_u64(), Some(d0));
        let old = Uniques::new(Some(dir.clone())).view(d0 + 86400 * 40);
        assert_eq!(old["month"].as_u64(), Some(0));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_hash_is_fixed() {
        assert_eq!(hash("0123456789abcdef0123456789abcdef"), hash("0123456789abcdef0123456789abcdef"));
        assert_ne!(hash("0123456789abcdef0123456789abcdef"), hash("0123456789abcdef0123456789abcdee"));
    }
}
