use super::*;
use crate::dm::Dm;
use std::cell::RefCell;
use std::collections::HashMap;

type Quad = [[f64; 2]; 4];
#[derive(Default)]
struct Quads {
    anchor: (u64, u64),
    tiles: HashMap<(u32, Key), Quad>,
}
thread_local! {static QUADS:RefCell<Quads>=RefCell::default();}

/// All scenery trackers on a connection share an anchor. Grouped far buildings often
/// test the very same parent quad sixteen times in a drop pass. No Python references are
/// retained by this bounded cache, and its iteration order is immaterial.
pub(super) struct TileFrame {
    origin: (u64, u64),
    frame: Frame,
}
impl TileFrame {
    pub(super) fn new(fr: &Obj<'_>) -> PyResult<Self> {
        let lat: f64 = fr.getattr("lat0")?.extract()?;
        let lon: f64 = fr.getattr("lon0")?.extract()?;
        Ok(Self {
            origin: (lat.to_bits(), lon.to_bits()),
            frame: Frame::new(lat, lon),
        })
    }
    pub(super) fn quad(&self, z: u32, k: Key) -> Quad {
        QUADS.with_borrow_mut(|cache| {
            if cache.anchor != self.origin || cache.tiles.len() >= 20000 {
                cache.tiles.clear();
                cache.anchor = self.origin;
            }
            *cache
                .tiles
                .entry((z, k))
                .or_insert_with(|| quad(&self.frame, z, k.0, k.1))
        })
    }
}

#[pyfunction]
pub(super) fn detail_order(
    fr: &Obj<'_>,
    keys: Vec<Key>,
    z: u32,
    p: Point,
    tree: &Bound<'_, PyDict>,
    near: f64,
) -> PyResult<Vec<Key>> {
    let grid = TileFrame::new(fr)?;
    let mut ranked = Vec::with_capacity(keys.len());
    for k in keys {
        let d = quad_dist(&grid.quad(z, k), p.0, p.1);
        let rank = if d <= near {
            (0, d)
        } else if let Some(got) = tree.get_item(k)? {
            (1, got.get_item(0)?.extract()?)
        } else {
            (2, d)
        };
        ranked.push((rank, k));
    }
    ranked.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    Ok(ranked.into_iter().map(|(_, k)| k).collect())
}

#[pyfunction]
pub(super) fn road_eta_rank(
    eta: f64,
    p: f64,
    gap: f64,
    speed: f64,
    build_s: f64,
    params: (f64, f64, f64, f64, f64, f64),
) -> f64 {
    let (p_min, gamma, side, window, keep, late) = params;
    let p = p.max(p_min);
    let power = if gamma == 0.5 { p.sqrt() } else { p.dpowf(gamma) };
    let mut key = eta / power + gap / (speed / 4.).min(side);
    let win = window / speed.max(1.);
    let left = (eta + win - build_s.max(eta)) / win;
    if left < keep {
        key += late * if left <= 0. { 2. } else { 1. };
    }
    key
}

#[pyfunction]
pub(super) fn scenery_result(tr: &Obj<'_>, key: Key, hash: &Obj<'_>) -> PyResult<bool> {
    let redo = set(tr, "redo")?;
    let again = redo.contains(key)?;
    redo.discard(key)?;
    let sent = set(tr, "sent")?;
    if again || tr.getattr("digests")?.is_truthy()? {
        let hashes = dict(tr, "sent_h")?;
        if again && !hash.is_none() && sent.contains(key)? {
            if let Some(old) = hashes.get_item(key)? {
                if old.eq(hash)? {
                    return Ok(false);
                }
            }
        }
        hashes.set_item(key, hash)?;
    }
    let ever = tr.getattr("ever")?;
    if !ever.is_none() {
        let ever = ever.cast::<PySet>()?;
        if ever.contains(key)? {
            tr.setattr("rebuilt", tr.getattr("rebuilt")?.extract::<usize>()? + 1)?;
        }
        ever.add(key)?;
    }
    sent.add(key)?;
    Ok(true)
}
