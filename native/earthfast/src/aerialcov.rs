//! The 1 m aerial packs' coverage (imagery source "bngaer", server/aerialpacks.py; built by tools/build_aerial_packs.py):
//! one JSON file per zoom-6 region says which zoom-11 cells hold which zoom-17 packs (8 x 8 zoom-17 tiles = one zoom-14
//! tile), at which build generation. A zoom-16 / 15 / 14 pack exists where any zoom-17 pack under it does.
//!
//!     {"format": 1, "region": [x, y], "cells": {"587,782": {"g": 1, "p": "ffffffffffffffff", ...}, ...}}
//!
//! "p": the cell's zoom-17 packs, a 64-bit mask in hex (bit (py % 8) * 8 + (px % 8), least significant first); "g": 0
//! with no "p": no open photos there (never asked to be built). A cell not listed: not built yet.
//!
//! aerial_cov_table parses a region file into a fixed table (32 x 32 cells, CELL_BYTES each: state u8, generation u16,
//! mask u64); aerial_pack_state reads a pack's state from it. Pure, no I/O.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

pub const CELL_Z: u32 = 11;
pub const REGION_Z: u32 = 6;
pub const TOP_Z: u32 = 17;
pub const LOW_Z: u32 = 14;
const SIDE: u32 = 1 << (CELL_Z - REGION_Z); // 32 cells a side
const CELL_BYTES: usize = 11;
const ST_UNBUILT: u8 = 0;
const ST_BUILT: u8 = 1;
const ST_NONE: u8 = 2; // no open photos there

/// The region file's table: (region x, region y, SIDE * SIDE * CELL_BYTES bytes). Cells outside the region or with
/// a bad entry are left unbuilt (never an error for one cell: a newer builder's fields are ignored).
pub fn parse(json: &[u8]) -> Result<(u32, u32, Vec<u8>), String> {
    let doc: serde_json::Value = serde_json::from_slice(json).map_err(|e| format!("coverage: {e}"))?;
    if doc.get("format").and_then(|v| v.as_u64()) != Some(1) {
        return Err("coverage: not format 1".into());
    }
    let reg = doc.get("region").and_then(|v| v.as_array()).ok_or("coverage: no region")?;
    let rx = reg.first().and_then(|v| v.as_u64()).ok_or("coverage: bad region")? as u32;
    let ry = reg.get(1).and_then(|v| v.as_u64()).ok_or("coverage: bad region")? as u32;
    if rx >= 1 << REGION_Z || ry >= 1 << REGION_Z {
        return Err("coverage: region outside the grid".into());
    }
    let mut t = vec![0u8; (SIDE * SIDE) as usize * CELL_BYTES];
    if let Some(cells) = doc.get("cells").and_then(|v| v.as_object()) {
        for (k, v) in cells {
            let mut it = k.split(',').map(|s| s.trim().parse::<u32>());
            let (cx, cy) = match (it.next(), it.next(), it.next()) {
                (Some(Ok(a)), Some(Ok(b)), None) => (a, b),
                _ => continue,
            };
            if cx >> (CELL_Z - REGION_Z) != rx || cy >> (CELL_Z - REGION_Z) != ry {
                continue;
            }
            let g = v.get("g").and_then(|x| x.as_u64()).unwrap_or(0);
            let p = v.get("p").and_then(|x| x.as_str()).and_then(|s| u64::from_str_radix(s, 16).ok()).unwrap_or(0);
            let (st, g) = if g > 0 && g <= u16::MAX as u64 && p != 0 {
                (ST_BUILT, g as u16)
            } else if g == 0 && p == 0 {
                (ST_NONE, 0)
            } else {
                continue;
            };
            let i = ((cy % SIDE) * SIDE + (cx % SIDE)) as usize * CELL_BYTES;
            t[i] = st;
            t[i + 1..i + 3].copy_from_slice(&g.to_le_bytes());
            t[i + 3..i + 11].copy_from_slice(&p.to_le_bytes());
        }
    }
    Ok((rx, ry, t))
}

/// The region (zoom 6) and cell (zoom 11) of pack (z, px, py) (px, py: its tile at zoom z - 3), and the mask of the
/// cell's zoom-17 packs under it; None outside zooms LOW_Z..=TOP_Z.
pub fn locate(z: u32, px: u32, py: u32) -> Option<((u32, u32), (u32, u32), u64)> {
    if !(LOW_Z..=TOP_Z).contains(&z) || px >= 1 << (z - 3) || py >= 1 << (z - 3) {
        return None;
    }
    let s = TOP_Z - z; // the zoom-14 tiles under it: 2^s a side
    let (x0, y0) = (px << s, py << s);
    let (cx, cy) = (x0 >> 3, y0 >> 3);
    let mut m = 0u64;
    for j in 0..(1u32 << s) {
        for i in 0..(1u32 << s) {
            m |= 1u64 << (((y0 + j) & 7) * 8 + ((x0 + i) & 7));
        }
    }
    Some(((cx >> (CELL_Z - REGION_Z), cy >> (CELL_Z - REGION_Z)), (cx, cy), m))
}

/// A pack's state in a region table: (state, generation): state 0 not built (yet), 1 built (generation >= 1),
/// 2 no open photos there, 3 another region's pack / not a pack zoom.
pub fn state(rx: u32, ry: u32, t: &[u8], z: u32, px: u32, py: u32) -> (u8, u16) {
    let Some(((qx, qy), (cx, cy), m)) = locate(z, px, py) else { return (3, 0) };
    if (qx, qy) != (rx, ry) || t.len() != (SIDE * SIDE) as usize * CELL_BYTES {
        return (3, 0);
    }
    let i = ((cy % SIDE) * SIDE + (cx % SIDE)) as usize * CELL_BYTES;
    match t[i] {
        ST_BUILT => {
            let g = u16::from_le_bytes([t[i + 1], t[i + 2]]);
            let p = u64::from_le_bytes(t[i + 3..i + 11].try_into().unwrap());
            if p & m != 0 { (ST_BUILT, g) } else { (ST_UNBUILT, 0) }
        }
        ST_NONE => (ST_NONE, 0),
        _ => (ST_UNBUILT, 0),
    }
}

/// aerial_cov_table(json bytes) -> (region x, region y, table bytes). ValueError on a file that is not one.
#[pyfunction]
pub fn aerial_cov_table<'py>(py: Python<'py>, data: &[u8]) -> PyResult<(u32, u32, Bound<'py, PyBytes>)> {
    let (rx, ry, t) = py.detach(|| parse(data)).map_err(PyValueError::new_err)?;
    Ok((rx, ry, PyBytes::new(py, &t)))
}

/// aerial_pack_where(z, px, py) -> ((region x, y), (cell x, y)) or None (not a pack zoom / outside the grid).
#[pyfunction]
pub fn aerial_pack_where(z: u32, px: u32, py: u32) -> Option<((u32, u32), (u32, u32))> {
    locate(z, px, py).map(|(r, c, _)| (r, c))
}

/// aerial_pack_state(region x, region y, table, z, px, py) -> (state, generation): 0 not built, 1 built, 2 no photos,
/// 3 not this region's.
#[pyfunction]
pub fn aerial_pack_state(rx: u32, ry: u32, table: &[u8], z: u32, px: u32, py: u32) -> (u8, u16) {
    state(rx, ry, table, z, px, py)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pack_states() {
        // cell 587,782 (region 18,24): packs (4696..4703, 6256..6263) at zoom 14; mask: only pack (4697, 6256) (bit 1)
        let j = br#"{"format":1,"region":[18,24],"cells":{"587,782":{"g":3,"p":"0000000000000002"},"588,782":{"g":0},
                   "9,9":{"g":1,"p":"ff"},"586,782":{"g":2}}}"#;
        let (rx, ry, t) = parse(j).unwrap();
        assert_eq!((rx, ry), (18, 24));
        assert_eq!(state(rx, ry, &t, 17, 4697, 6256), (1, 3));
        assert_eq!(state(rx, ry, &t, 17, 4696, 6256), (0, 0));
        assert_eq!(state(rx, ry, &t, 16, 4697 >> 1, 6256 >> 1), (1, 3)); // a zoom-16 pack over it
        assert_eq!(state(rx, ry, &t, 16, 4698 >> 1, 6256 >> 1), (0, 0));
        assert_eq!(state(rx, ry, &t, 14, 587, 782), (1, 3)); // the cell's own zoom-14 pack
        assert_eq!(state(rx, ry, &t, 17, 4704, 6256), (2, 0)); // 588,782: no photos
        assert_eq!(state(rx, ry, &t, 17, 4688, 6256), (0, 0)); // 586,782: g without p -> ignored
        assert_eq!(state(rx, ry, &t, 13, 293, 391), (3, 0)); // not a pack zoom
        assert_eq!(state(0, 0, &t, 17, 4697, 6256), (3, 0));
        assert_eq!(locate(17, 4697, 6256).unwrap().0, (18, 24));
        assert!(parse(br#"{"format":2,"region":[1,1]}"#).is_err());
        assert!(parse(br#"{"format":1,"region":[64,1]}"#).is_err());
    }
}
