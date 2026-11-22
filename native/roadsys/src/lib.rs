//! roadsys: the road surface + bridge rewrite (bridgerewrite/DESIGN.md, BNE_ROADSYS). Kernels: G1 lane graph (net.rs),
//! G2 support + crossings (net.rs), G3 one profile per carriageway (prof.rs), G4 planar partition (part.rs), markings
//! (mark.rs), G5-lite derived products (derive.rs), G6 tiles (tile.rs), the sheet's own checks (check.rs), the M0
//! audit of any built road scene (audit.rs: today's output and the shadow product alike) and the shadow product
//! (product.rs: per-component builds with declared read lists) and M2's profile squares for today's deck builder and
//! grading (square.rs, BNE_ROADSYS=heights).
//!
//! Pure Rust, no dependencies: IEEE + - * / and sqrt only (no FMA, no libm), integer-mm topology, BTreeMap only.
pub mod audit;
pub mod check;
pub mod derive;
pub mod endmatch;
pub mod geom;
pub mod io;
pub mod mark;
pub mod net;
pub mod part;
pub mod prof;
pub mod product;
pub mod square;
pub mod tile;

/// Thread CPU seconds (timings only: nothing built reads them).
#[cfg(target_os = "linux")]
pub fn cpu_now() -> f64 {
    #[repr(C)]
    struct Ts { s: i64, ns: i64 }
    extern "C" { fn clock_gettime(clk: i32, tp: *mut Ts) -> i32; }
    let mut t = Ts { s: 0, ns: 0 };
    unsafe { clock_gettime(3, &mut t); }    // CLOCK_THREAD_CPUTIME_ID
    t.s as f64 + t.ns as f64 * 1e-9
}

#[cfg(not(target_os = "linux"))]
pub fn cpu_now() -> f64 {
    use std::sync::OnceLock;
    static T0: OnceLock<std::time::Instant> = OnceLock::new();
    T0.get_or_init(std::time::Instant::now).elapsed().as_secs_f64()
}
