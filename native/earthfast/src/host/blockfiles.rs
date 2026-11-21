//! Ownership of immutable terrain revision paths. A ticket covers one delivery, including repeat
//! deliveries of the same revision. Eviction drops the cache's ownership; the last game to release
//! a retired path hands it back to the file remover. All mutations happen on the server loop.
use pyo3::prelude::*;
use std::collections::{BTreeMap, BTreeSet};

type Centre = (i64, i64, i64);
type Revisions = BTreeMap<Vec<String>, u64>;

#[pyclass(module = "earthfast")]
#[derive(Default)]
pub(super) struct BlockFiles {
    next: u64,
    legacy: BTreeMap<String, BTreeMap<Centre, Revisions>>,
    leases: BTreeMap<String, BTreeMap<u64, Vec<String>>>,
    paths: BTreeMap<String, (usize, bool)>, // outstanding deliveries, retired by server
}

#[pymethods]
impl BlockFiles {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn holds(&self, lease: &str) -> bool {
        self.leases.contains_key(lease)
    }

    fn hold(&mut self, lease: String, paths: Vec<String>) -> u64 {
        self.next += 1;
        for path in &paths {
            self.paths.entry(path.clone()).or_default().0 += 1;
        }
        self.leases
            .entry(lease)
            .or_default()
            .insert(self.next, paths);
        self.next
    }

    // Older games identify the centre they installed, but never confirm which repaint succeeded.
    // Replays of the same delivery add no ownership. Retain every candidate at that centre.
    fn hold_legacy(&mut self, lease: String, centre: Centre, paths: Vec<String>) -> u64 {
        if let Some(ticket) = self
            .legacy
            .get(&lease)
            .and_then(|c| c.get(&centre))
            .and_then(|r| r.get(&paths))
        {
            return *ticket;
        }
        let ticket = self.hold(lease.clone(), paths.clone());
        self.legacy
            .entry(lease)
            .or_default()
            .entry(centre)
            .or_default()
            .insert(paths, ticket);
        ticket
    }

    // The game has moved on. Its pending follow and the first block of a future anchor can
    // still be installed. During a staged swap the caller holds the rollback set until commit.
    fn leave_legacy(
        &mut self,
        lease: &str,
        current: Option<Centre>,
        pending: Option<Centre>,
    ) -> Vec<String> {
        let Some(centres) = self.legacy.get_mut(lease) else {
            return Vec::new();
        };
        let mut gone = Vec::new();
        centres.retain(|centre, revisions| {
            let keep =
                current.is_some_and(|k| *centre == k || centre.0 > k.0 || Some(*centre) == pending);
            if !keep {
                gone.extend(revisions.values().copied());
            }
            keep
        });
        if centres.is_empty() {
            self.legacy.remove(lease);
        }
        self.release(lease, gone)
    }

    fn retire(&mut self, paths: Vec<String>) -> Vec<String> {
        paths
            .into_iter()
            .filter(|path| {
                if let Some(state) = self.paths.get_mut(path) {
                    state.1 = true;
                    false
                } else {
                    true
                }
            })
            .collect()
    }

    fn release(&mut self, lease: &str, tickets: Vec<u64>) -> Vec<String> {
        let Some(deliveries) = self.leases.get_mut(lease) else {
            return Vec::new();
        };
        let mut ready = Vec::new();
        for ticket in tickets {
            if let Some(paths) = deliveries.remove(&ticket) {
                for path in paths {
                    let state = self.paths.get_mut(&path).unwrap();
                    state.0 -= 1;
                    if state.0 == 0 {
                        let retired = state.1;
                        self.paths.remove(&path);
                        if retired {
                            ready.push(path);
                        }
                    }
                }
            }
        }
        if deliveries.is_empty() {
            self.leases.remove(lease);
        }
        ready
    }

    // The old transport has closed. Only the deliveries in the game's snapshot survived it.
    fn resume(&mut self, lease: &str, held: Vec<u64>) -> Vec<String> {
        let mut keep: BTreeSet<_> = held.into_iter().collect();
        if let Some(centres) = self.legacy.get(lease) {
            for revisions in centres.values() {
                keep.extend(revisions.values().copied());
            }
        }
        let expired = self
            .leases
            .get(lease)
            .into_iter()
            .flat_map(|m| m.keys())
            .filter(|ticket| !keep.contains(ticket))
            .copied()
            .collect();
        self.release(lease, expired)
    }

    fn close(&mut self, lease: &str) -> Vec<String> {
        self.legacy.remove(lease);
        self.resume(lease, Vec::new())
    }
}
