//! The mod's strike lattice (weather phase B, storm.lua M.hash / M.share / M.strikeAt / M.strikesNear), bit for bit:
//! each (0.1-degree cell, 10 s UTC slot) holds one strike or none, decided by a 32-bit hash of (cell, slot) alone, so the
//! world server knows every strike ahead of time and can render its thunder before the flash.
//! Checked against storm.lua itself (weathersynth storm-dump vs tools/storm_dump.lua under LuaJIT): identical output.

pub const SEED: u32 = 0x5354_524b; // 'STRK'
pub const CELL_DEG: f64 = 0.1;
pub const SLOT_S: f64 = 10.0;
pub const RATE: [f64; 3] = [0.25, 0.8, 2.0];
pub const CG_SHARE: f64 = 0.3;
pub const VARIANTS: u32 = 4;
const TWO32: f64 = 4294967296.0;

/// MurmurHash3's mixing over integer arguments, as storm.lua M.hash: each argument floored and taken mod 2^32 (Lua's
/// % is non-negative for a positive divisor).
pub fn hash(args: &[f64]) -> u32 {
    let mut h = SEED;
    for &a in args {
        let k0 = a.floor();
        let k0 = k0 - (k0 / TWO32).floor() * TWO32; // Lua: x % 2^32
        let mut k = k0 as u64 as u32;
        k = k.wrapping_mul(0xcc9e_2d51);
        k = k.rotate_left(15);
        k = k.wrapping_mul(0x1b87_3593);
        h ^= k;
        h = h.rotate_left(13);
        h = h.wrapping_mul(5).wrapping_add(0xe654_6b64);
    }
    h ^= h >> 16;
    h = h.wrapping_mul(0x85eb_ca6b);
    h ^= h >> 13;
    h = h.wrapping_mul(0xc2b2_ae35);
    h ^= h >> 16;
    h
}

/// The share of (cell, slot)s holding a strike in thunder class c.
pub fn share(c: i32) -> f64 {
    if c < 1 {
        return 0.0;
    }
    let r = RATE[(c as usize).min(RATE.len()) - 1];
    (r * SLOT_S / 60.0).min(1.0)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Strike {
    /// UTC epoch seconds of the flash
    pub t: f64,
    pub lat: f64,
    pub lon: f64,
    /// cloud-to-ground (a bolt) or inside the cloud
    pub cg: bool,
    pub pulses: u32,
    pub variant: u32,
    pub seed: u32,
}

/// The strike of cell (i, j) in UTC slot `slot` for class c, or None (storm.lua M.strikeAt).
pub fn strike_at(i: f64, j: f64, slot: f64, c: i32) -> Option<Strike> {
    let p = share(c);
    if p <= 0.0 || hash(&[i, j, slot, 1.0]) as f64 / TWO32 >= p {
        return None;
    }
    let h2 = hash(&[i, j, slot, 2.0]) as f64;
    let h3 = hash(&[i, j, slot, 3.0]) as f64;
    let h4 = hash(&[i, j, slot, 4.0]);
    let lo = h3 % 65536.0;
    let h4f = h4 as f64;
    Some(Strike {
        t: (slot + h2 / TWO32) * SLOT_S,
        lat: (i + lo / 65536.0) * CELL_DEG,
        lon: (j + (h3 - lo) / 65536.0 / 65536.0) * CELL_DEG,
        cg: (h4f % 1000.0) / 1000.0 < CG_SHARE,
        pulses: 1 + ((h4f / 1000.0).floor() % 3.0) as u32,
        variant: 1 + ((h4f / 3000.0).floor() % VARIANTS as f64) as u32,
        seed: h4,
    })
}

/// The strikes round (lat, lon) (its cell and the 8 next to it) in the slots from t0 to t1, sorted by time
/// (storm.lua M.strikesNear; equal times keep the walk's order, as table.sort may not: none occur in practice).
pub fn strikes_near(lat: f64, lon: f64, t0: f64, t1: f64, c: i32) -> Vec<Strike> {
    let mut out = Vec::new();
    if share(c) <= 0.0 {
        return out;
    }
    let i0 = (lat / CELL_DEG).floor();
    let j0 = (lon / CELL_DEG).floor();
    let mut slot = (t0 / SLOT_S).floor();
    let last = (t1 / SLOT_S).floor();
    while slot <= last {
        for di in -1..=1 {
            for dj in -1..=1 {
                if let Some(s) = strike_at(i0 + di as f64, j0 + dj as f64, slot, c) {
                    if s.t >= t0 && s.t < t1 {
                        out.push(s);
                    }
                }
            }
        }
        slot += 1.0;
    }
    out.sort_by(|a, b| a.t.partial_cmp(&b.t).unwrap());
    out
}

/// The dump compared with storm.lua (tools/storm_dump.lua): hashes, strikes over a grid of cells and slots, and two
/// strikesNear windows; floats printed with 17 significant digits.
pub fn dump() -> String {
    let mut s = String::new();
    for a in [-3.0, -1.0, 0.0, 1.0, 7.0, 123456.0, 4294967295.0, 4294967296.0, -4294967297.0, 2.5, -2.5] {
        s.push_str(&format!("hash {} {}\n", lua_num(a), hash(&[a])));
        s.push_str(&format!("hash2 {} {}\n", lua_num(a), hash(&[a, 17.0, -5.0])));
    }
    for c in 0..=3 {
        for i in [-900i64, -1, 0, 1, 371, 899] {
            for j in [-1800i64, -2, 0, 3, 1799] {
                for slot in [0i64, 1, 176_400_000, 176_400_001, 176_412_345] {
                    if let Some(st) = strike_at(i as f64, j as f64, slot as f64, c) {
                        s.push_str(&format!(
                            "strike {c} {i} {j} {slot} {} {} {} {} {} {} {}\n",
                            g17(st.t),
                            g17(st.lat),
                            g17(st.lon),
                            st.cg,
                            st.pulses,
                            st.variant,
                            st.seed
                        ));
                    }
                }
            }
        }
    }
    for (lat, lon) in [(52.52, 13.405), (-33.8688, 151.2093)] {
        for st in strikes_near(lat, lon, 1_764_000_000.0, 1_764_000_600.0, 3) {
            s.push_str(&format!("near {} {} {} {} {}\n", g17(st.t), g17(st.lat), g17(st.lon), st.cg, st.seed));
        }
    }
    s
}

fn lua_num(a: f64) -> String {
    if a.fract() == 0.0 && a.abs() < 1e15 {
        format!("{}", a as i64)
    } else {
        g17(a)
    }
}

/// %.17g as C's printf (what string.format('%.17g') gives in Lua).
pub fn g17(x: f64) -> String {
    if x == 0.0 {
        return "0".into();
    }
    let e = x.abs().log10().floor() as i32;
    if (-5..17).contains(&e) {
        let decimals = (16 - e).max(0) as usize;
        let s = format!("{:.*}", decimals, x);
        let s = if s.contains('.') { s.trim_end_matches('0').trim_end_matches('.').to_string() } else { s };
        s
    } else {
        let s = format!("{:.16e}", x);
        // Rust: 1.2340000000000000e5 -> C: 1.234e+05
        let (mant, exp) = s.split_once('e').unwrap();
        let mant = if mant.contains('.') { mant.trim_end_matches('0').trim_end_matches('.') } else { mant };
        let ev: i32 = exp.parse().unwrap();
        format!("{}e{}{:02}", mant, if ev < 0 { '-' } else { '+' }, ev.abs())
    }
}
