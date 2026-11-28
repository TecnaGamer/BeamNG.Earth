//! GPU compute (the user, 2026-10-22: "the CPU has a lot of work, so moving any work to the gpu will help a lot"):
//! a headless wgpu device made on first use (Vulkan, DX12 or Metal: whatever the machine has; none -> the callers
//! keep their CPU path), and the kernels that run on it.
//!
//! seam_detect: colormatch.detect_seams' oriented step filter over all its angles (the 36 rotations, the box sums,
//! the segment medians, the agreement and the S2 gate, the rotation back and the best angle per cell), which was ~10
//! s of CPU per colour-match region (the rotations in PIL, earthfast.seam_pass per angle). The same geometry as the
//! CPU path (PIL's rotate with expand, bilinear, zero outside; the box and band sums; the lower medians), in float32:
//! close to the CPU path's results, not bit-identical (the CPU path's sums are float64), and the same on one machine
//! every time.
//!
//! The service (2026-11-27, ~/bnge-tests/reports/gpu_offload_research.md move 1):
//! - one device per process, and in the server only its main process makes one (BNGE_GPU_OWNER, set by earthd's
//!   main to its pid: the block and far-tile processes it starts keep the CPU paths; each device was ~0.1-0.4 s of
//!   start and memory of its own);
//! - every kernel's submissions short (encode_blocks' CHUNK_BLOCKS, seam_detect's SEAM_WG_PER_SUBMIT), each waited
//!   for before the next, so the game's frames get the GPU between ours: some adapters preempt compute only at a
//!   dispatch's or a DMA buffer's boundary (Windows: DXGI's ComputePreemptionGranularity, in describe());
//! - the block encoders' buffers kept between calls (EncBufs), let go after ENC_IDLE_S unused;
//! - per call its submissions, the time waited for them, the longest one, the bytes up and down (last(): the calling
//!   thread's last call; totals(): the process's), for the server's log;
//! - on a Steam Deck (deck(): a GPU-bound handheld, whose game pays for every dispatch in frames) the kernels run only
//!   while the car is parked or a teleport loads (allowed(); note_pose from the server's pose handler).
use crate::dm::Dm;

use std::cell::Cell;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use wgpu::util::DeviceExt;

pub struct Gpu {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pub name: String,
    pub info: wgpu::AdapterInfo,
    /// (graphics, compute) preemption granularity from DXGI (Windows; None elsewhere or unknown)
    pub preempt: Option<(&'static str, &'static str)>,
    seam: Mutex<Option<SeamPipes>>,
}

static GPU: OnceLock<Option<Gpu>> = OnceLock::new();

/// Is the GPU off for this process: BNGE_GPU=0, or BNGE_GPU_OWNER names another process (earthd's main sets it to its
/// own pid: the processes it starts inherit it and make no device).
pub fn switched_off() -> bool {
    if std::env::var("BNGE_GPU").map(|v| v == "0").unwrap_or(false) {
        return true;
    }
    match std::env::var("BNGE_GPU_OWNER") {
        Ok(v) if !v.trim().is_empty() => v.trim().parse::<u32>().map(|p| p != std::process::id()).unwrap_or(true),
        _ => false,
    }
}

/// The device (made on first use), or None where there is no usable GPU (BNGE_GPU=0 turns it off; switched_off).
pub fn get() -> Option<&'static Gpu> {
    GPU.get_or_init(|| {
        if switched_off() {
            return None;
        }
        // (a driver's panic must not take the server down: no GPU, the CPU paths)
        std::panic::catch_unwind(|| pollster::block_on(init())).ok().flatten()
    })
    .as_ref()
}

async fn init() -> Option<Gpu> {
    let instance = wgpu::Instance::new(&wgpu::InstanceDescriptor {
        backends: wgpu::Backends::VULKAN | wgpu::Backends::DX12 | wgpu::Backends::METAL,
        ..Default::default()
    });
    let adapter = instance
        .request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            force_fallback_adapter: false,
            compatible_surface: None,
        })
        .await
        .ok()?;
    let info = adapter.get_info();
    if info.device_type == wgpu::DeviceType::Cpu {
        return None; // (a software rasteriser: no faster than the CPU path)
    }
    let mut limits = wgpu::Limits::default();
    let al = adapter.limits();
    limits.max_storage_buffer_binding_size = al.max_storage_buffer_binding_size;
    limits.max_buffer_size = al.max_buffer_size;
    let (device, queue) = adapter
        .request_device(&wgpu::DeviceDescriptor {
            label: Some("earthfast"),
            required_features: wgpu::Features::empty(),
            required_limits: limits,
            memory_hints: wgpu::MemoryHints::Performance,
            trace: wgpu::Trace::Off,
        })
        .await
        .ok()?;
    let preempt = dxgi_preemption(info.vendor, info.device);
    Some(Gpu { device, queue, name: format!("{} ({:?})", info.name, info.backend), info, preempt,
               seam: Mutex::new(None) })
}

/// One line about the adapter for the server's log: its name, backend, kind, driver, PCI ids, on Windows how finely it
/// preempts graphics and compute work (DXGI; "DMA buffer" or "dispatch": a long dispatch of ours holds the GPU the
/// game draws with until it ends), and the Steam Deck's gate.
pub fn describe(g: &Gpu) -> String {
    let i = &g.info;
    let mut s = format!("{} ({:?}, {:?}), driver {}", i.name, i.backend, i.device_type,
                        if i.driver.is_empty() { "?" } else { i.driver.as_str() });
    if !i.driver_info.is_empty() {
        s += &format!(" {}", i.driver_info);
    }
    s += &format!(", PCI {:04x}:{:04x}", i.vendor, i.device);
    match g.preempt {
        Some((gr, cs)) => s += &format!(", preemption: graphics at {gr}, compute at {cs}"),
        None if cfg!(windows) => s += ", preemption: unknown (DXGI)",
        None => {}
    }
    if deck() {
        s += "; Steam Deck: kernels only while parked or loading";
    }
    s
}

/// DXGI's (graphics, compute) preemption granularity of the adapter with these PCI ids (Windows), whichever API wgpu
/// uses on it (WDDM preempts per adapter).
#[cfg(windows)]
fn dxgi_preemption(vendor: u32, device: u32) -> Option<(&'static str, &'static str)> {
    use windows::core::Interface;
    use windows::Win32::Graphics::Dxgi::{CreateDXGIFactory1, IDXGIAdapter2, IDXGIFactory1};
    // (DXGI_GRAPHICS_PREEMPTION_GRANULARITY: DMA buffer, primitive, triangle, pixel, instruction;
    // DXGI_COMPUTE_PREEMPTION_GRANULARITY: DMA buffer, dispatch, thread group, thread, instruction)
    let gname = |v: i32| ["DMA buffer", "primitive", "triangle", "pixel", "instruction"].get(v as usize).copied();
    let cname = |v: i32| ["DMA buffer", "dispatch", "thread group", "thread", "instruction"].get(v as usize).copied();
    // SAFETY: plain COM calls on the interfaces the factory hands out, each result checked
    unsafe {
        let f: IDXGIFactory1 = CreateDXGIFactory1().ok()?;
        for k in 0..16u32 {
            let Ok(a) = f.EnumAdapters1(k) else { break };
            let Ok(a2) = a.cast::<IDXGIAdapter2>() else { continue };
            let Ok(d) = a2.GetDesc2() else { continue };
            if d.VendorId == vendor && d.DeviceId == device {
                return Some((gname(d.GraphicsPreemptionGranularity.0).unwrap_or("unknown"),
                             cname(d.ComputePreemptionGranularity.0).unwrap_or("unknown")));
            }
        }
    }
    None
}

#[cfg(not(windows))]
fn dxgi_preemption(_vendor: u32, _device: u32) -> Option<(&'static str, &'static str)> {
    None
}

// --- the Steam Deck's gate ------------------------------------------------------------------------------------------
// Its game is GPU-bound (20-26 ms of GPU a frame on the Handheld preset): a dispatch of ours costs it frames while
// driving. There the kernels run only while the car is parked (under PARK_V for park_s()) or a teleport loads; the
// server's Python glue asks allowed() before each call and keeps the CPU path otherwise.

/// m/s: slower than this the car is standing
pub const PARK_V: f64 = 0.5;
/// s standing before the car counts as parked (BNGE_GPU_PARK_S overrides it: tests)
const PARK_S: f64 = 3.0;

static EPOCH: OnceLock<Instant> = OnceLock::new();
/// ms since EPOCH of the last pose that moved (u64::MAX: none yet)
static LAST_MOVING_MS: AtomicU64 = AtomicU64::new(u64::MAX);
/// the last pose was a teleport's, still loading
static LOADING: AtomicBool = AtomicBool::new(false);

fn now_ms() -> u64 {
    EPOCH.get_or_init(Instant::now).elapsed().as_millis() as u64
}

fn park_s() -> f64 {
    std::env::var("BNGE_GPU_PARK_S").ok().and_then(|v| v.trim().parse::<f64>().ok()).unwrap_or(PARK_S)
}

/// A pose from the game (host/pose.rs motion): the car's speed (m/s) and whether a teleport is loading. Two atomic
/// stores at every pose; no device is made.
pub fn note_pose(speed: f64, loading: bool) {
    LOADING.store(loading, Ordering::Relaxed);
    if !loading && !(speed < PARK_V) {      // (NaN counts as moving)
        LAST_MOVING_MS.store(now_ms(), Ordering::Relaxed);
    }
}

/// Seconds since the last moving pose (None: none yet).
pub fn standing_s() -> Option<f64> {
    let t = LAST_MOVING_MS.load(Ordering::Relaxed);
    (t != u64::MAX).then(|| now_ms().saturating_sub(t) as f64 / 1000.0)
}

/// Is this a GPU-bound handheld (a Steam Deck: its APU in the adapter's name, or SteamOS)? BNGE_GPU_DECK=1 / 0
/// overrides (tests, an A/B).
pub fn deck() -> bool {
    match std::env::var("BNGE_GPU_DECK").as_deref() {
        Ok("1") => return true,
        Ok("0") => return false,
        _ => {}
    }
    static D: OnceLock<bool> = OnceLock::new();
    *D.get_or_init(|| {
        let by_name = |n: &str| {
            let n = n.to_ascii_uppercase();
            // (RADV names the APU VANGOGH; the drivers' names "AMD Custom GPU 0405" (LCD) and "0932" (OLED))
            n.contains("VANGOGH") || n.contains("CUSTOM GPU 0405") || n.contains("CUSTOM GPU 0932")
        };
        if get().is_some_and(|g| by_name(&g.info.name) || by_name(&g.info.driver_info)) {
            return true;
        }
        std::fs::read_to_string("/etc/os-release").map(|s| s.to_ascii_lowercase().contains("steamos")).unwrap_or(false)
    })
}

/// May a kernel run on the GPU now? Off a Deck always; on one while a teleport loads, before the first moving pose,
/// or once the car has stood park_s(). BNGE_GPU_DRIVING=1: always (an A/B).
pub fn allowed() -> bool {
    if !deck() || std::env::var("BNGE_GPU_DRIVING").map(|v| v == "1").unwrap_or(false) {
        return true;
    }
    LOADING.load(Ordering::Relaxed) || standing_s().is_none_or(|s| s >= park_s())
}

// --- per-call accounting ------------------------------------------------------------------------------------------

/// One kernel call's GPU side: its submissions, the time waited for them (the GPU's work and its queue), the longest
/// one, the bytes uploaded and read back, the call's wall time, and whether it worked.
#[derive(Clone, Copy, Default, Debug)]
pub struct CallStat {
    pub kernel: &'static str,
    pub submits: u32,
    pub wait_s: f64,
    pub max_submit_s: f64,
    pub up_bytes: u64,
    pub down_bytes: u64,
    pub wall_s: f64,
    pub ok: bool,
}

thread_local! {
    static LAST: Cell<Option<CallStat>> = const { Cell::new(None) };
}

#[derive(Clone, Copy, Default, Debug)]
pub struct Totals {
    pub calls: u64,
    pub fails: u64,
    pub submits: u64,
    pub wait_s: f64,
    pub wall_s: f64,
    pub max_submit_s: f64,
    pub up_bytes: u64,
    pub down_bytes: u64,
}

static TOTALS: Mutex<Vec<(&'static str, Totals)>> = Mutex::new(Vec::new());

fn record(st: CallStat) {
    LAST.with(|l| l.set(Some(st)));
    let mut t = TOTALS.lock().unwrap_or_else(|e| e.into_inner());
    let i = match t.iter().position(|(k, _)| *k == st.kernel) {
        Some(i) => i,
        None => {
            t.push((st.kernel, Totals::default()));
            t.len() - 1
        }
    };
    let a = &mut t[i].1;
    a.calls += 1;
    a.fails += (!st.ok) as u64;
    a.submits += st.submits as u64;
    a.wait_s += st.wait_s;
    a.wall_s += st.wall_s;
    a.max_submit_s = a.max_submit_s.max(st.max_submit_s);
    a.up_bytes += st.up_bytes;
    a.down_bytes += st.down_bytes;
}

/// The calling thread's last kernel call (the Python glue reads it right after the call, on the same thread).
pub fn last() -> Option<CallStat> {
    LAST.with(|l| l.get())
}

/// The process's totals per kernel since its start (or the last reset).
pub fn totals(reset: bool) -> Vec<(&'static str, Totals)> {
    let mut t = TOTALS.lock().unwrap_or_else(|e| e.into_inner());
    let out = t.clone();
    if reset {
        t.clear();
    }
    out
}

/// A kernel call with its accounting: f counts its submissions and bytes into the CallStat (submit_wait, read_back),
/// this the wall time and the outcome.
pub fn accounted<T>(kernel: &'static str, f: impl FnOnce(&mut CallStat) -> Result<T, String>) -> Result<T, String> {
    let t0 = Instant::now();
    let mut st = CallStat { kernel, ..Default::default() };
    let r = f(&mut st);
    st.wall_s = t0.elapsed().as_secs_f64();
    st.ok = r.is_ok();
    record(st);
    r
}

/// Submits one command buffer and waits for it (each of ours short, the next only after it: see encode_blocks),
/// counted in st.
fn submit_wait(gpu: &Gpu, enc: wgpu::CommandEncoder, st: &mut CallStat) -> Result<(), String> {
    let t0 = Instant::now();
    let sub = gpu.queue.submit(Some(enc.finish()));
    gpu.device.poll(wgpu::PollType::WaitForSubmissionIndex(sub)).map_err(|e| format!("poll: {e:?}"))?;
    let dt = t0.elapsed().as_secs_f64();
    st.submits += 1;
    st.wait_s += dt;
    st.max_submit_s = st.max_submit_s.max(dt);
    Ok(())
}

/// The first n bytes of the mapped-readable buffer rb (filled by a submission already waited for) handed to take.
fn read_back<T>(gpu: &Gpu, rb: &wgpu::Buffer, n: u64, st: &mut CallStat, take: impl FnOnce(&[u8]) -> T)
                -> Result<T, String> {
    let slice = rb.slice(..n);
    let (tx, rx) = std::sync::mpsc::channel();
    slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
    gpu.device.poll(wgpu::PollType::Wait).map_err(|e| format!("poll: {e:?}"))?;
    rx.recv().map_err(|e| format!("map: {e}"))?.map_err(|e| format!("map: {e:?}"))?;
    let out = take(&slice.get_mapped_range());
    rb.unmap();
    st.down_bytes += n;
    Ok(out)
}

// --- seam_detect -----------------------------------------------------------------------------------------------

const SEAM_WGSL: &str = r#"
struct P {
    w: u32, h: u32, wr: u32, hr: u32,
    half: i32, depth: i32, nseg: u32, min_valid: u32,
    full: f32, gate: f32, agree: f32, ang: f32,
    // forward: rotated pixel -> source (orig) coordinates; back: orig pixel (+ crop) -> rotated coordinates
    fa: f32, fb: f32, fc: f32, fd: f32, fe: f32, ff: f32, cx: f32, cy: f32,
    ba: f32, bb: f32, bc: f32, bd: f32, be: f32, bf: f32, y0: u32, _pad: u32,
    offs: array<vec4<i32>, 2>,
};
@group(0) @binding(0) var<uniform> p: P;
@group(0) @binding(1) var<storage, read> src: array<f32>;          // 7 x h x w
@group(0) @binding(2) var<storage, read_write> rot: array<f32>;    // 7 x hr x wr
@group(0) @binding(3) var<storage, read_write> hs: array<f32>;     // 7 x hr x wr
@group(0) @binding(4) var<storage, read_write> dseg: array<f32>;   // 6 x hr x wr
@group(0) @binding(5) var<storage, read_write> okb: array<u32>;    // hr x wr
@group(0) @binding(6) var<storage, read_write> res: array<f32>;    // 4 x hr x wr: score, Dm r g b
@group(0) @binding(7) var<storage, read_write> outb: array<f32>;   // 5 x h x w: best, ang, D r g b

fn off(k: u32) -> i32 { return p.offs[k / 4u][k % 4u]; }

// PIL's bilinear filter for "F" images (libImaging Geometry.c): 0 outside, edges clamped, the next row only when in.
fn pil_bilinear(base: u32, W: i32, H: i32, xin: f32, yin: f32, fromsrc: bool) -> f32 {
    if (xin < 0.0 || xin >= f32(W) || yin < 0.0 || yin >= f32(H)) { return 0.0; }
    let x = xin - 0.5; let y = yin - 0.5;
    let xf = floor(x); let yf = floor(y);
    let dx = x - xf; let dy = y - yf;
    let xi = i32(xf); let yi = i32(yf);
    let x0 = clamp(xi, 0, W - 1); let x1 = clamp(xi + 1, 0, W - 1);
    let y0 = clamp(yi, 0, H - 1);
    var a0: f32; var a1: f32;
    if (fromsrc) { a0 = src[base + u32(y0 * W + x0)]; a1 = src[base + u32(y0 * W + x1)]; }
    else { a0 = res[base + u32(y0 * W + x0)]; a1 = res[base + u32(y0 * W + x1)]; }
    let v1 = a0 + (a1 - a0) * dx;
    var v2 = v1;
    if (yi + 1 >= 0 && yi + 1 < H) {
        var b0: f32; var b1: f32;
        if (fromsrc) { b0 = src[base + u32((yi + 1) * W + x0)]; b1 = src[base + u32((yi + 1) * W + x1)]; }
        else { b0 = res[base + u32((yi + 1) * W + x0)]; b1 = res[base + u32((yi + 1) * W + x1)]; }
        v2 = b0 + (b1 - b0) * dx;
    }
    return v1 + (v2 - v1) * dy;
}

@compute @workgroup_size(16, 16)
fn rotate(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = vec3<u32>(gid.x, gid.y + p.y0, gid.z);     // (this submission's rows: seam_detect's bands)
    if (g.x >= p.wr || g.y >= p.hr) { return; }
    let x = f32(g.x) + 0.5; let y = f32(g.y) + 0.5;
    let xs = p.fa * x + p.fb * y + p.fc;
    let ys = p.fd * x + p.fe * y + p.ff;
    let n = p.wr * p.hr; let ns = p.w * p.h;
    for (var c = 0u; c < 7u; c++) {
        rot[c * n + g.y * p.wr + g.x] = pil_bilinear(c * ns, i32(p.w), i32(p.h), xs, ys, true);
    }
}

@compute @workgroup_size(16, 16)
fn hsum(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = vec3<u32>(gid.x, gid.y + p.y0, gid.z);     // (this submission's rows: seam_detect's bands)
    if (g.x >= p.wr || g.y >= p.hr) { return; }
    let n = p.wr * p.hr;
    let x0 = max(i32(g.x) - p.half, 0); let x1 = min(i32(g.x) + p.half, i32(p.wr) - 1);
    for (var c = 0u; c < 7u; c++) {
        var s = 0.0;
        let row = c * n + g.y * p.wr;
        for (var x = x0; x <= x1; x++) { s += rot[row + u32(x)]; }
        hs[row + g.x] = s;
    }
}

fn band(c: u32, x: u32, y0: i32, y1: i32) -> f32 {   // rows [y0, y1) clipped, of hs channel c
    let n = p.wr * p.hr;
    var s = 0.0;
    for (var y = max(y0, 0); y < min(y1, i32(p.hr)); y++) { s += hs[c * n + u32(y) * p.wr + x]; }
    return s;
}

@compute @workgroup_size(16, 16)
fn bands(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = vec3<u32>(gid.x, gid.y + p.y0, gid.z);     // (this submission's rows: seam_detect's bands)
    if (g.x >= p.wr || g.y >= p.hr) { return; }
    let n = p.wr * p.hr; let i = g.y * p.wr + g.x; let j = i32(g.y);
    let wb = band(0u, g.x, j + 1, j + p.depth + 1);
    let wa = band(0u, g.x, j - p.depth, j);
    var ok = 0u;
    if (wb > p.full && wa > p.full) { ok = 1u; }
    okb[i] = ok;
    let ib = 1.0 / max(wb, 1e-3); let ia = 1.0 / max(wa, 1e-3);
    for (var c = 0u; c < 6u; c++) {
        dseg[c * n + i] = band(c + 1u, g.x, j + 1, j + p.depth + 1) * ib - band(c + 1u, g.x, j - p.depth, j) * ia;
    }
}

// the lower median of the ok values among v[0..k) (colormatch._vmedian; 0 when none)
fn lower_median(v: array<f32, 8>, ok: array<bool, 8>, k: u32, nvalid: u32) -> f32 {
    if (nvalid == 0u) { return 0.0; }
    var b: array<f32, 8>;
    var m = 0u;
    for (var i = 0u; i < k; i++) { if (ok[i]) { b[m] = v[i]; m++; } }
    for (var i = 1u; i < m; i++) {          // insertion sort (k <= 8)
        let x = b[i]; var j = i;
        while (j > 0u && b[j - 1u] > x) { b[j] = b[j - 1u]; j--; }
        b[j] = x;
    }
    return b[(nvalid - 1u) / 2u];
}

@compute @workgroup_size(16, 16)
fn segs(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = vec3<u32>(gid.x, gid.y + p.y0, gid.z);     // (this submission's rows: seam_detect's bands)
    if (g.x >= p.wr || g.y >= p.hr) { return; }
    let n = p.wr * p.hr; let i = g.y * p.wr + g.x;
    var oks: array<bool, 8>;
    var dl: array<vec3<f32>, 8>;
    var ds: array<vec3<f32>, 8>;
    var nvalid = 0u;
    for (var k = 0u; k < p.nseg; k++) {
        let xs = i32(g.x) + off(k);
        oks[k] = false; dl[k] = vec3<f32>(0.0); ds[k] = vec3<f32>(0.0);
        if (xs >= 0 && xs < i32(p.wr)) {
            let q = g.y * p.wr + u32(xs);
            oks[k] = okb[q] == 1u;
            let su = vec3<f32>(dseg[q], dseg[n + q], dseg[2u * n + q]);
            let s2 = vec3<f32>(dseg[3u * n + q], dseg[4u * n + q], dseg[5u * n + q]);
            dl[k] = su - s2; ds[k] = s2;
        }
        if (oks[k]) { nvalid++; }
    }
    var m = vec3<f32>(0.0); var msm = vec3<f32>(0.0);
    for (var c = 0u; c < 3u; c++) {
        var a: array<f32, 8>; var b: array<f32, 8>;
        for (var k = 0u; k < p.nseg; k++) { a[k] = dl[k][c]; b[k] = ds[k][c]; }
        m[c] = lower_median(a, oks, p.nseg, nvalid);
        msm[c] = lower_median(b, oks, p.nseg, nvalid);
    }
    let nm = length(m);
    var agree = 0u;
    for (var k = 0u; k < p.nseg; k++) {
        let nk = length(dl[k]);
        let cs = dot(dl[k], m) / max(nk * nm, 1e-6);
        if (cs > 0.7 && nk > 0.5 * nm && oks[k]) { agree++; }
    }
    let cons = nvalid >= p.min_valid && f32(agree) >= p.agree * f32(nvalid);
    let nsm = length(msm);
    let gt = clamp(1.0 - max(nsm - 0.03, 0.0) / (p.gate * nm + 1e-3), 0.0, 1.0);
    var sc = 0.0;
    if (cons) { sc = nm * gt; }
    res[i] = sc;
    res[n + i] = m.x; res[2u * n + i] = m.y; res[3u * n + i] = m.z;
}

@compute @workgroup_size(16, 16)
fn unrot(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = vec3<u32>(gid.x, gid.y + p.y0, gid.z);     // (this submission's rows: seam_detect's bands)
    if (g.x >= p.w || g.y >= p.h) { return; }
    let x = f32(g.x) + p.cx + 0.5; let y = f32(g.y) + p.cy + 0.5;    // (in the back-rotated image, cropped)
    let xs = p.ba * x + p.bb * y + p.bc;
    let ys = p.bd * x + p.be * y + p.bf;
    let n = p.wr * p.hr; let no = p.w * p.h; let i = g.y * p.w + g.x;
    let sc = pil_bilinear(0u, i32(p.wr), i32(p.hr), xs, ys, false);
    if (sc > outb[i]) {
        outb[i] = sc;
        outb[no + i] = p.ang;
        for (var c = 0u; c < 3u; c++) {
            outb[(2u + c) * no + i] = pil_bilinear((1u + c) * n, i32(p.wr), i32(p.hr), xs, ys, false);
        }
    }
}
"#;

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct SeamParams {
    w: u32, h: u32, wr: u32, hr: u32,
    half: i32, depth: i32, nseg: u32, min_valid: u32,
    full: f32, gate: f32, agree: f32, ang: f32,
    fa: f32, fb: f32, fc: f32, fd: f32, fe: f32, ff: f32, cx: f32, cy: f32,
    ba: f32, bb: f32, bc: f32, bd: f32, be: f32, bf: f32, y0: u32, _pad: u32,
    offs: [i32; 8],
}

struct SeamPipes {
    layout: wgpu::BindGroupLayout,
    pipes: [wgpu::ComputePipeline; 5],
}

fn seam_pipes(gpu: &Gpu) -> std::sync::MutexGuard<'_, Option<SeamPipes>> {
    let mut g = gpu.seam.lock().unwrap();
    if g.is_none() {
        let d = &gpu.device;
        let module = d.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("seam"),
            source: wgpu::ShaderSource::Wgsl(SEAM_WGSL.into()),
        });
        let entry = |b: u32, ty: wgpu::BufferBindingType| wgpu::BindGroupLayoutEntry {
            binding: b,
            visibility: wgpu::ShaderStages::COMPUTE,
            ty: wgpu::BindingType::Buffer { ty, has_dynamic_offset: false, min_binding_size: None },
            count: None,
        };
        let mut entries = vec![entry(0, wgpu::BufferBindingType::Uniform),
                               entry(1, wgpu::BufferBindingType::Storage { read_only: true })];
        for b in 2..8 {
            entries.push(entry(b, wgpu::BufferBindingType::Storage { read_only: false }));
        }
        let layout = d.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: &entries });
        let pl = d.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[],
        });
        let mk = |ep: &str| d.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some(ep), layout: Some(&pl), module: &module, entry_point: Some(ep),
            compilation_options: Default::default(), cache: None,
        });
        *g = Some(SeamPipes { layout, pipes: [mk("rotate"), mk("hsum"), mk("bands"), mk("segs"), mk("unrot")] });
    }
    g
}

/// PIL's Image.rotate(deg, expand=True) for a w x h image: (the affine matrix from output pixel coordinates to the
/// input's, the output size), as Image.rotate computes them (float64, its rounding of cos / sin to 15 places).
fn pil_rotate(deg: f64, w: f64, h: f64) -> ([f64; 6], usize, usize) {
    let angle = -(deg % 360.0).to_radians();
    let r15 = |v: f64| (v * 1e15).round() / 1e15;
    let mut m = [r15(angle.dcos()), r15(angle.dsin()), 0.0, r15(-angle.dsin()), r15(angle.dcos()), 0.0];
    let t = |m: &[f64; 6], x: f64, y: f64| (m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5]);
    let (cx, cy) = (w / 2.0, h / 2.0);
    let (a, b) = t(&m, -cx, -cy);
    m[2] = a + cx;
    m[5] = b + cy;
    let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for (x, y) in [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)] {
        let (u, v) = t(&m, x, y);
        x0 = x0.min(u); x1 = x1.max(u); y0 = y0.min(v); y1 = y1.max(v);
    }
    let nw = x1.ceil() - x0.floor();
    let nh = y1.ceil() - y0.floor();
    let (a, b) = t(&m, -(nw - w) / 2.0, -(nh - h) / 2.0);
    m[2] = a;
    m[5] = b;
    (m, nw as usize, nh as usize)
}

/// Workgroups (16 x 16 invocations) per seam_detect submission at most: one of its five passes over a band of rows
/// (a z12 region's 1086² rotated grid is ~4,600 workgroups a pass; all five passes of an angle were one submission,
/// tens of ms of a small GPU's time held at once). ~1 ms of a mid-range GPU's time.
const SEAM_WG_PER_SUBMIT: usize = 1024;

/// The (first row, rows) bands of a pass over gw x gh invocations of 16 x 16, SEAM_WG_PER_SUBMIT workgroups each
/// at most (whole workgroup rows; at least one).
fn seam_bands(gw: usize, gh: usize) -> Vec<(u32, u32)> {
    let (wx, wy) = (gw.div_ceil(16).max(1), gh.div_ceil(16));
    let per = (SEAM_WG_PER_SUBMIT / wx).max(1);
    (0..wy).step_by(per).map(|r| ((r * 16) as u32, (per.min(wy - r)) as u32)).collect()
}

/// colormatch.detect_seams' angle loop: src (7, h, w) float32 = [w, w*L_0 .. w*L_5]; returns (best, ang, D) as
/// flat float32 (h*w, h*w, 3*h*w), or an error string. Each angle's five passes run one after another, each in bands
/// of rows (seam_bands), one submission a band, each waited for (the same results as one submission: every pass
/// reads only what the passes before it wrote).
#[allow(clippy::too_many_arguments)]
pub fn seam_detect(gpu: &Gpu, src: &[f32], h: usize, w: usize, n_ang: usize, half: i32, depth: i32, offs: &[i32],
                   full: f32, gate: f32, agree: f32, min_valid: u32) -> Result<Vec<f32>, String> {
    if offs.len() > 8 || src.len() != 7 * h * w {
        return Err("seam_detect: bad arguments".into());
    }
    accounted("seam", |st| {
        let d = &gpu.device;
        // the largest rotated size over the angles (at 45 deg)
        let (mut wr_max, mut hr_max) = (0usize, 0usize);
        let geo: Vec<_> = (0..n_ang).map(|k| {
            let deg = 180.0 * k as f64 / n_ang as f64;
            let (fwd, wr, hr) = pil_rotate(deg, w as f64, h as f64);
            let (back, w2, h2) = pil_rotate(-deg, wr as f64, hr as f64);
            wr_max = wr_max.max(wr);
            hr_max = hr_max.max(hr);
            (deg, fwd, wr, hr, back, w2, h2)
        }).collect();
        let nr = wr_max * hr_max;
        let buf = |label: &str, n: usize, usage: wgpu::BufferUsages| d.create_buffer(&wgpu::BufferDescriptor {
            label: Some(label), size: (n * 4) as u64, usage, mapped_at_creation: false,
        });
        let stu = wgpu::BufferUsages::STORAGE;
        let srcb = d.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("src"), contents: bytemuck::cast_slice(src), usage: stu,
        });
        let rot = buf("rot", 7 * nr, stu);
        let hs = buf("hs", 7 * nr, stu);
        let dseg = buf("dseg", 6 * nr, stu);
        let okb = buf("ok", nr, stu);
        let res = buf("res", 4 * nr, stu);
        // (best starts at 0: a score must exceed it, the CPU path's `score_o > best` from zeros; wgpu zeroes a new
        // buffer)
        let outb = buf("out", 5 * h * w, stu | wgpu::BufferUsages::COPY_SRC);
        st.up_bytes += (src.len() * 4) as u64;
        let pbuf = buf("params", std::mem::size_of::<SeamParams>() / 4, wgpu::BufferUsages::UNIFORM
                       | wgpu::BufferUsages::COPY_DST);
        let guard = seam_pipes(gpu);
        let sp = guard.as_ref().unwrap();
        let bg = d.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None,
            layout: &sp.layout,
            entries: &[&pbuf, &srcb, &rot, &hs, &dseg, &okb, &res, &outb].iter().enumerate()
                .map(|(i, b)| wgpu::BindGroupEntry { binding: i as u32, resource: b.as_entire_binding() })
                .collect::<Vec<_>>(),
        });
        let mut o8 = [0i32; 8];
        o8[..offs.len()].copy_from_slice(offs);
        for (deg, fwd, wr, hr, back, w2, h2) in geo {
            let mut pr = SeamParams {
                w: w as u32, h: h as u32, wr: wr as u32, hr: hr as u32, half, depth, nseg: offs.len() as u32,
                min_valid, full, gate, agree, ang: deg.to_radians() as f32,
                fa: fwd[0] as f32, fb: fwd[1] as f32, fc: fwd[2] as f32, fd: fwd[3] as f32, fe: fwd[4] as f32,
                ff: fwd[5] as f32,
                cx: ((w2 - w) / 2) as f32, cy: ((h2 - h) / 2) as f32,
                ba: back[0] as f32, bb: back[1] as f32, bc: back[2] as f32, bd: back[3] as f32, be: back[4] as f32,
                bf: back[5] as f32, y0: 0, _pad: 0,
                offs: o8,
            };
            for (i, pipe) in sp.pipes.iter().enumerate() {
                let (gw, gh) = if i == 4 { (w, h) } else { (wr, hr) };
                for (y0, rows) in seam_bands(gw, gh) {
                    pr.y0 = y0;
                    // (queued before the submission below, which reads it; the last one was waited for)
                    gpu.queue.write_buffer(&pbuf, 0, bytemuck::bytes_of(&pr));
                    let mut enc = d.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
                    {
                        let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor {
                            label: None, timestamp_writes: None,
                        });
                        pass.set_bind_group(0, &bg, &[]);
                        pass.set_pipeline(pipe);
                        pass.dispatch_workgroups(gw.div_ceil(16) as u32, rows, 1);
                    }
                    submit_wait(gpu, enc, st)?;
                }
            }
        }
        // read back
        let n_out = (5 * h * w * 4) as u64;
        let rb = buf("readback", 5 * h * w, wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST);
        let mut enc = d.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        enc.copy_buffer_to_buffer(&outb, 0, &rb, 0, n_out);
        submit_wait(gpu, enc, st)?;
        read_back(gpu, &rb, n_out, st, |b| bytemuck::cast_slice::<u8, f32>(b).to_vec())
    })
}

// --- bc1 -------------------------------------------------------------------------------------------------------

// earthfast.bc1_encode's block encoder (lib.rs bc1_block) in float32, one invocation per 4 x 4 block of any of the
// levels packed one after another: endpoints on the block's principal axis (power iteration), one least-squares
// refinement on the chosen indices, the better of the two, each texel the nearest of the 4 colours.
const BC1_WGSL: &str = r#"
struct Level { w: u32, h: u32, px: u32, nbx: u32, blk: u32, nblk: u32, _a: u32, _b: u32 };
@group(0) @binding(0) var<storage, read> lv: array<Level>;
@group(0) @binding(1) var<storage, read> pix: array<u32>;      // RGBA8 texels of every level
@group(0) @binding(2) var<storage, read_write> outb: array<u32>; // 2 words per block
@group(0) @binding(3) var<uniform> nlev: vec4<u32>;

fn to565(c: vec3<f32>) -> u32 {
    let k = clamp(c, vec3<f32>(0.0), vec3<f32>(255.0));
    let r = u32(floor(k.x * 31.0 / 255.0 + 0.5));
    let g = u32(floor(k.y * 63.0 / 255.0 + 0.5));
    let b = u32(floor(k.z * 31.0 / 255.0 + 0.5));
    return (r << 11u) | (g << 5u) | b;
}
fn rgb565(x: u32) -> vec3<i32> {
    let r = i32((x >> 11u) & 31u); let g = i32((x >> 5u) & 63u); let b = i32(x & 31u);
    return vec3<i32>((r << 3u) | (r >> 2u), (g << 2u) | (g >> 4u), (b << 3u) | (b >> 2u));
}

@compute @workgroup_size(64)
fn bc1(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = gid.x + nlev.y;                 // (this submission's first block: encode_blocks' chunks)
    var L = 0u;
    var found = false;
    for (var i = 0u; i < nlev.x; i++) {
        if (g >= lv[i].blk && g < lv[i].blk + lv[i].nblk) { L = i; found = true; }
    }
    if (!found) { return; }
    let l = lv[L];
    let b = g - l.blk;
    let by = b / l.nbx; let bx = b % l.nbx;
    var t: array<vec3<i32>, 16>;
    for (var yy = 0u; yy < 4u; yy++) {
        let y = min(by * 4u + yy, l.h - 1u);
        for (var xx = 0u; xx < 4u; xx++) {
            let x = min(bx * 4u + xx, l.w - 1u);
            let v = pix[l.px + y * l.w + x];
            t[yy * 4u + xx] = vec3<i32>(i32(v & 255u), i32((v >> 8u) & 255u), i32((v >> 16u) & 255u));
        }
    }
    var mean = vec3<f32>(0.0);
    for (var k = 0u; k < 16u; k++) { mean += vec3<f32>(t[k]) / 16.0; }
    var cov = mat3x3<f32>(vec3<f32>(0.0), vec3<f32>(0.0), vec3<f32>(0.0));
    for (var k = 0u; k < 16u; k++) {
        let d = vec3<f32>(t[k]) - mean;
        cov[0] += d * d.x; cov[1] += d * d.y; cov[2] += d * d.z;
    }
    var v = vec3<f32>(0.577, 0.577, 0.577);
    for (var it = 0; it < 6; it++) {
        let w = cov * v;
        let n = length(w);
        if (n < 1e-9) { break; }
        v = w / n;
    }
    var pmin = 1e30; var pmax = -1e30;
    for (var k = 0u; k < 16u; k++) {
        let p = dot(vec3<f32>(t[k]) - mean, v);
        pmin = min(pmin, p); pmax = max(pmax, p);
    }
    var e0 = mean + v * pmax;
    var e1 = mean + v * pmin;
    var berr = 0xffffffffu; var bc0 = 0u; var bc1v = 0u; var bidx = 0u;
    for (var ps = 0; ps < 2; ps++) {
        var c0 = to565(e0); var c1 = to565(e1);
        if (c0 < c1) { let s = c0; c0 = c1; c1 = s; }
        let a = rgb565(c0); let bb = rgb565(c1);
        var pal: array<vec3<i32>, 4>;
        if (c0 == c1) { pal[0] = a; pal[1] = a; pal[2] = a; pal[3] = a; }
        else { pal[0] = a; pal[1] = bb; pal[2] = (2 * a + bb) / 3; pal[3] = (a + 2 * bb) / 3; }
        var idx = 0u; var err = 0u;
        var sel: array<u32, 16>;
        for (var k = 0u; k < 16u; k++) {
            var bi = 0u; var be = 0x7fffffff;
            for (var pi = 0u; pi < 4u; pi++) {
                let d = t[k] - pal[pi];
                let dd = d.x * d.x + d.y * d.y + d.z * d.z;
                if (dd < be) { be = dd; bi = pi; }
            }
            sel[k] = bi; err += u32(be); idx |= bi << (2u * k);
        }
        if (ps == 0 || err < berr) { berr = err; bc0 = c0; bc1v = c1; bidx = idx; }
        if (ps == 1 || c0 == c1) { break; }
        var W = array<f32, 4>(1.0, 0.0, 2.0 / 3.0, 1.0 / 3.0);
        var aa = 0.0; var bbs = 0.0; var ab = 0.0;
        var ax = vec3<f32>(0.0); var bxv = vec3<f32>(0.0);
        for (var k = 0u; k < 16u; k++) {
            let wa = W[sel[k]]; let wb = 1.0 - wa;
            aa += wa * wa; bbs += wb * wb; ab += wa * wb;
            ax += wa * vec3<f32>(t[k]); bxv += wb * vec3<f32>(t[k]);
        }
        let det = aa * bbs - ab * ab;
        if (abs(det) < 1e-9) { break; }
        e0 = (bbs * ax - ab * bxv) / det;
        e1 = (aa * bxv - ab * ax) / det;
    }
    outb[2u * g] = bc0 | (bc1v << 16u);
    outb[2u * g + 1u] = bidx;
}
"#;

/// A block encoder's pipeline and its buffers, kept between calls (EncBufs).
pub struct EncPipe {
    layout: wgpu::BindGroupLayout,
    pipe: wgpu::ComputePipeline,
    bufs: Mutex<Option<EncBufs>>,
}

/// A block encoder's buffers and their bind group: made again only when a call needs more than they hold (or under a
/// quarter of it), let go ENC_IDLE_S after their last call (trim_idle). A new buffer was ~a third of a 4096² photo's
/// GPU call in CPU (each one made, zeroed and bound again a call: research 2.2).
struct EncBufs {
    lv_cap: u64,
    pix_cap: u64,
    out_cap: u64,
    lvb: wgpu::Buffer,
    pixb: wgpu::Buffer,
    outb: wgpu::Buffer,
    nl: wgpu::Buffer,
    rb: wgpu::Buffer,
    bg: wgpu::BindGroup,
    last: Instant,
}

/// s an encoder's buffers are kept after their last call (a 4096² photo's ~110 MB of GPU memory and its 22 MB
/// readback; the photos come every few seconds while driving)
const ENC_IDLE_S: u64 = 120;

/// Capacities in steps of 1 MiB (a texture's few sizes reuse one set).
fn round_cap(n: u64) -> u64 {
    n.max(16).div_ceil(1 << 20) * (1 << 20)
}

impl EncBufs {
    fn fits(&self, lv: u64, pix: u64, out: u64) -> bool {
        lv <= self.lv_cap && pix <= self.pix_cap && out <= self.out_cap && self.pix_cap <= 4 * round_cap(pix)
            && self.out_cap <= 4 * round_cap(out)
    }

    fn new(d: &wgpu::Device, layout: &wgpu::BindGroupLayout, lv: u64, pix: u64, out: u64) -> EncBufs {
        let (lv_cap, pix_cap, out_cap) = (lv.max(256).next_power_of_two(), round_cap(pix), round_cap(out));
        let st = wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST;
        let mk = |label: &str, size: u64, usage: wgpu::BufferUsages| d.create_buffer(&wgpu::BufferDescriptor {
            label: Some(label), size, usage, mapped_at_creation: false,
        });
        let lvb = mk("levels", lv_cap, st);
        let pixb = mk("pix", pix_cap, st);
        let outb = mk("blocks", out_cap, wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC);
        let nl = mk("n", 16, wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST);
        let rb = mk("rb", out_cap, wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST);
        let bg = d.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None, layout,
            entries: &[wgpu::BindGroupEntry { binding: 0, resource: lvb.as_entire_binding() },
                       wgpu::BindGroupEntry { binding: 1, resource: pixb.as_entire_binding() },
                       wgpu::BindGroupEntry { binding: 2, resource: outb.as_entire_binding() },
                       wgpu::BindGroupEntry { binding: 3, resource: nl.as_entire_binding() }],
        });
        EncBufs { lv_cap, pix_cap, out_cap, lvb, pixb, outb, nl, rb, bg, last: Instant::now() }
    }
}

static BC1: OnceLock<EncPipe> = OnceLock::new();
static BC7: OnceLock<EncPipe> = OnceLock::new();

/// The block encoder of WGSL source src (entry point ep): its layout (levels, texels, blocks, uniform) and pipeline.
fn enc_pipe(gpu: &Gpu, ep: &str, src: &str) -> EncPipe {
    let d = &gpu.device;
    let module = d.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some(ep), source: wgpu::ShaderSource::Wgsl(src.into()),
    });
    let entry = |b: u32, ty: wgpu::BufferBindingType| wgpu::BindGroupLayoutEntry {
        binding: b, visibility: wgpu::ShaderStages::COMPUTE,
        ty: wgpu::BindingType::Buffer { ty, has_dynamic_offset: false, min_binding_size: None }, count: None,
    };
    let layout = d.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: None,
        entries: &[entry(0, wgpu::BufferBindingType::Storage { read_only: true }),
                   entry(1, wgpu::BufferBindingType::Storage { read_only: true }),
                   entry(2, wgpu::BufferBindingType::Storage { read_only: false }),
                   entry(3, wgpu::BufferBindingType::Uniform)],
    });
    let pl = d.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: None, bind_group_layouts: &[&layout], push_constant_ranges: &[],
    });
    let pipe = d.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
        label: Some(ep), layout: Some(&pl), module: &module, entry_point: Some(ep),
        compilation_options: Default::default(), cache: None,
    });
    start_trim();
    EncPipe { layout, pipe, bufs: Mutex::new(None) }
}

/// Lets go of the encoders' buffers unused for ENC_IDLE_S (a thread of its own, made with the first encoder: every
/// 30 s it takes each lock for a moment; no Python).
fn start_trim() {
    static STARTED: OnceLock<()> = OnceLock::new();
    STARTED.get_or_init(|| {
        let _ = std::thread::Builder::new().name("earthfast-gpu-trim".into()).spawn(|| loop {
            std::thread::sleep(Duration::from_secs(30));
            trim_idle(Duration::from_secs(ENC_IDLE_S));
        });
    });
}

/// Drops the encoders' buffers whose last call is older than idle; the bytes kept after (tests, the log).
pub fn trim_idle(idle: Duration) -> u64 {
    let mut kept = 0;
    for p in [&BC1, &BC7] {
        if let Some(p) = p.get() {
            let mut b = p.bufs.lock().unwrap_or_else(|e| e.into_inner());
            if b.as_ref().is_some_and(|b| b.last.elapsed() >= idle) {
                *b = None;
            }
            kept += b.as_ref().map_or(0, |b| b.lv_cap + b.pix_cap + 2 * b.out_cap);
        }
    }
    kept
}

/// BC1 blocks of several RGB images at once (each (w, h, rgb bytes)): their blocks' bytes, one Vec per image.
pub fn bc1_encode(gpu: &Gpu, imgs: &[(usize, usize, &[u8])]) -> Result<Vec<Vec<u8>>, String> {
    let mut texels: Vec<(usize, usize, Vec<u8>)> = Vec::with_capacity(imgs.len());
    for &(w, h, rgb) in imgs {
        if rgb.len() != w * h * 3 || w == 0 || h == 0 {
            return Err("bc1_encode: bad image".into());
        }
        texels.push((w, h, rgb.chunks_exact(3).flat_map(|c| [c[0], c[1], c[2], 0]).collect()));
    }
    let refs: Vec<(usize, usize, &[u8])> = texels.iter().map(|(w, h, b)| (*w, *h, b.as_slice())).collect();
    let p = BC1.get_or_init(|| enc_pipe(gpu, "bc1", BC1_WGSL));
    accounted("bc1", |st| encode_blocks(gpu, p, &refs, 2, st))
}

/// BC7 (mode 6) blocks of several RGBA images at once (each (w, h, rgba bytes)), one Vec per image: bc7_encode's
/// (lib.rs bc7_block) in float32 (a few blocks can differ from it).
pub fn bc7_encode(gpu: &Gpu, imgs: &[(usize, usize, &[u8])]) -> Result<Vec<Vec<u8>>, String> {
    for &(w, h, rgba) in imgs {
        if rgba.len() != w * h * 4 || w == 0 || h == 0 {
            return Err("bc7_encode: bad image".into());
        }
    }
    let p = BC7.get_or_init(|| enc_pipe(gpu, "bc7", BC7_WGSL));
    // (RGBA8 bytes are the shader's packed u32 texels)
    accounted("bc7", |st| encode_blocks(gpu, p, imgs, 4, st))
}

/// Blocks per GPU submission (encode_blocks): 512 workgroups of 64, a few ms of a mid-range GPU's time.
const CHUNK_BLOCKS: u32 = 32768;

/// A block encoder over the images' 4 x 4 blocks (texels packed RGBA8 in u32; wpb u32 words a block), in short
/// submissions, into the encoder's kept buffers (EncBufs: one call at a time an encoder).
fn encode_blocks(gpu: &Gpu, p: &EncPipe, imgs: &[(usize, usize, &[u8])], wpb: u64, st: &mut CallStat)
                 -> Result<Vec<Vec<u8>>, String> {
    let d = &gpu.device;
    let mut levels: Vec<[u32; 8]> = Vec::new();
    let (mut nblk, mut npix) = (0u32, 0u64);
    for (w, h, t) in imgs {          // (texels: 4 bytes each, RGBA8 = a little-endian u32)
        let (nbx, nby) = (w.div_ceil(4) as u32, h.div_ceil(4) as u32);
        levels.push([*w as u32, *h as u32, (npix / 4) as u32, nbx, nblk, nbx * nby, 0, 0]);
        npix += t.len() as u64;
        nblk += nbx * nby;
    }
    let lv_bytes = (levels.len() * 32) as u64;
    let nb = (nblk as u64) * wpb * 4;
    let mut guard = p.bufs.lock().unwrap_or_else(|e| e.into_inner());
    if !guard.as_ref().is_some_and(|b| b.fits(lv_bytes, npix, nb)) {
        *guard = None;          // (the old ones let go first)
        *guard = Some(EncBufs::new(d, &p.layout, lv_bytes, npix, nb));
    }
    let b = guard.as_mut().unwrap();
    b.last = Instant::now();
    let r = encode_into(gpu, p, b, imgs, &levels, npix, nblk, wpb, st);
    if r.is_err() {
        *guard = None;          // (a failed call's buffers may be left mapped or half written: made again)
    }
    r
}

#[allow(clippy::too_many_arguments)]
fn encode_into(gpu: &Gpu, p: &EncPipe, b: &EncBufs, imgs: &[(usize, usize, &[u8])], levels: &[[u32; 8]], npix: u64,
               nblk: u32, wpb: u64, st: &mut CallStat) -> Result<Vec<Vec<u8>>, String> {
    let d = &gpu.device;
    let lv_bytes = (levels.len() * 32) as u64;
    let nb = (nblk as u64) * wpb * 4;
    // the texels straight into the kept buffer, image after image (no packed copy of them all here first)
    gpu.queue.write_buffer(&b.lvb, 0, bytemuck::cast_slice(levels));
    let mut off = 0u64;
    for (_, _, t) in imgs {
        gpu.queue.write_buffer(&b.pixb, off, t);
        off += t.len() as u64;
    }
    st.up_bytes += npix + lv_bytes;
    // in short submissions, each waited for before the next: one dispatch of a 4096² photo's ~1.4M blocks held the GPU
    // the game draws with for tens of ms, which Windows often cannot preempt (the game froze, 1.6.0); between ours,
    // the game's frames get the GPU
    let mut base = 0u32;
    while base < nblk {
        let n = (nblk - base).min(CHUNK_BLOCKS);
        gpu.queue.write_buffer(&b.nl, 0, bytemuck::cast_slice(&[levels.len() as u32, base, 0, 0]));
        let mut enc = d.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        {
            let mut pass = enc.begin_compute_pass(&wgpu::ComputePassDescriptor { label: None, timestamp_writes: None });
            pass.set_pipeline(&p.pipe);
            pass.set_bind_group(0, &b.bg, &[]);
            pass.dispatch_workgroups(n.div_ceil(64), 1, 1);
        }
        submit_wait(gpu, enc, st)?;
        base += n;
    }
    let mut enc = d.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
    enc.copy_buffer_to_buffer(&b.outb, 0, &b.rb, 0, nb);
    submit_wait(gpu, enc, st)?;
    let bpb = (wpb * 4) as usize;
    read_back(gpu, &b.rb, nb, st, |all| {
        levels.iter().map(|l| all[(l[4] as usize) * bpb..((l[4] + l[5]) as usize) * bpb].to_vec()).collect()
    })
}

// --- bc7 -------------------------------------------------------------------------------------------------------

// earthfast.bc7_encode's block encoder (lib.rs bc7_block, mode 6) in float32, one invocation per 4 x 4 block of any of
// the levels packed one after another: the endpoints at the block's extent along its principal axis (power iteration
// from the widest channel), quantised to 7 bits + a p-bit, each texel the nearest of the 16 weights, index 0 < 8.
const BC7_WGSL: &str = r#"
struct Level { w: u32, h: u32, px: u32, nbx: u32, blk: u32, nblk: u32, _a: u32, _b: u32 };
@group(0) @binding(0) var<storage, read> lv: array<Level>;
@group(0) @binding(1) var<storage, read> pix: array<u32>;      // RGBA8 texels of every level
@group(0) @binding(2) var<storage, read_write> outb: array<u32>; // 4 words per block
@group(0) @binding(3) var<uniform> nlev: vec4<u32>;

struct Q { q: vec4<i32>, p: i32 };
var<private> bits: array<u32, 4>;

fn put(v: u32, n: u32, pos: u32) {
    let w = pos / 32u;
    let s = pos % 32u;
    bits[w] = bits[w] | (v << s);
    if (s + n > 32u) { bits[w + 1u] = bits[w + 1u] | (v >> (32u - s)); }
}

fn quant(e: vec4<f32>, opaque: bool) -> Q {
    var best = Q(vec4<i32>(0), 0);
    var berr = 1e30;
    for (var p = 0; p < 2; p++) {
        let q = vec4<i32>(clamp(round((e - f32(p)) / 2.0), vec4<f32>(0.0), vec4<f32>(127.0)));
        let r = vec4<f32>((q << vec4<u32>(1u)) | vec4<i32>(p));
        let err = dot(abs(r - e), vec4<f32>(1.0));
        if (err < berr) { berr = err; best = Q(q, p); }
    }
    if (opaque) {
        var q = vec4<i32>(clamp(round((e - 1.0) / 2.0), vec4<f32>(0.0), vec4<f32>(127.0)));
        q.w = 127;
        best = Q(q, 1);
    }
    return best;
}

@compute @workgroup_size(64)
fn bc7(@builtin(global_invocation_id) gid: vec3<u32>) {
    let g = gid.x + nlev.y;                 // (this submission's first block: encode_blocks' chunks)
    var L = 0u;
    var found = false;
    for (var i = 0u; i < nlev.x; i++) {
        if (g >= lv[i].blk && g < lv[i].blk + lv[i].nblk) { L = i; found = true; }
    }
    if (!found) { return; }
    let l = lv[L];
    let b = g - l.blk;
    let by = b / l.nbx; let bx = b % l.nbx;
    var t: array<vec4<f32>, 16>;
    var opaque = true;
    var mean = vec4<f32>(0.0);
    for (var yy = 0u; yy < 4u; yy++) {
        let y = min(by * 4u + yy, l.h - 1u);
        for (var xx = 0u; xx < 4u; xx++) {
            let x = min(bx * 4u + xx, l.w - 1u);
            let v = pix[l.px + y * l.w + x];
            let c = vec4<f32>(f32(v & 255u), f32((v >> 8u) & 255u), f32((v >> 16u) & 255u), f32(v >> 24u));
            t[yy * 4u + xx] = c;
            mean += c;
            if ((v >> 24u) != 255u) { opaque = false; }
        }
    }
    mean = mean / 16.0;
    var cov = mat4x4<f32>(vec4<f32>(0.0), vec4<f32>(0.0), vec4<f32>(0.0), vec4<f32>(0.0));
    for (var k = 0u; k < 16u; k++) {
        let d = t[k] - mean;
        cov[0] += d * d.x; cov[1] += d * d.y; cov[2] += d * d.z; cov[3] += d * d.w;
    }
    var m = 0u;
    for (var i = 1u; i < 4u; i++) { if (cov[i][i] > cov[m][m]) { m = i; } }
    var v = vec4<f32>(0.0);
    v[m] = 1.0;
    for (var it = 0; it < 4; it++) {
        let w = cov * v;
        v = w / max(length(w), 1e-9);
    }
    var pmin = 1e30; var pmax = -1e30;
    for (var k = 0u; k < 16u; k++) {
        let p = dot(t[k] - mean, v);
        pmin = min(pmin, p); pmax = max(pmax, p);
    }
    let lo = clamp(mean + v * pmin, vec4<f32>(0.0), vec4<f32>(255.0));
    let hi = clamp(mean + v * pmax, vec4<f32>(0.0), vec4<f32>(255.0));
    var a0 = quant(lo, opaque);
    var a1 = quant(hi, opaque);
    let e0 = vec4<f32>((a0.q << vec4<u32>(1u)) | vec4<i32>(a0.p));
    let e1 = vec4<f32>((a1.q << vec4<u32>(1u)) | vec4<i32>(a1.p));
    let axis = e1 - e0;
    let den = max(dot(axis, axis), 1e-9);
    let W = array<f32, 16>(0.0, 4.0, 9.0, 13.0, 17.0, 21.0, 26.0, 30.0, 34.0, 38.0, 43.0, 47.0, 51.0, 55.0, 60.0, 64.0);
    var idx: array<u32, 16>;
    for (var k = 0u; k < 16u; k++) {
        let f = dot(t[k] - e0, axis) / den * 64.0;
        var bi = 0u; var be = 1e30;
        for (var w = 0u; w < 16u; w++) {
            let e = abs(f - W[w]);
            if (e < be) { be = e; bi = w; }
        }
        idx[k] = bi;
    }
    if (idx[0] >= 8u) {
        let s = a0; a0 = a1; a1 = s;
        for (var k = 0u; k < 16u; k++) { idx[k] = 15u - idx[k]; }
    }
    bits = array<u32, 4>(1u << 6u, 0u, 0u, 0u);
    var pos = 7u;
    for (var c = 0u; c < 4u; c++) {
        put(u32(a0.q[c]), 7u, pos); pos += 7u;
        put(u32(a1.q[c]), 7u, pos); pos += 7u;
    }
    put(u32(a0.p), 1u, pos); pos += 1u;
    put(u32(a1.p), 1u, pos); pos += 1u;
    put(idx[0], 3u, pos); pos += 3u;
    for (var k = 1u; k < 16u; k++) { put(idx[k], 4u, pos); pos += 4u; }
    for (var w = 0u; w < 4u; w++) { outb[4u * g + w] = bits[w]; }
}
"#;


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seam_bands_cover_every_row_in_short_submissions() {
        for (gw, gh) in [(1086usize, 1086usize), (768, 768), (16, 16), (5000, 3), (1, 1)] {
            let b = seam_bands(gw, gh);
            let wx = gw.div_ceil(16);
            let mut next = 0u32;
            for &(y0, rows) in &b {
                assert_eq!(y0, next * 16, "contiguous whole workgroup rows");
                assert!(rows >= 1 && (wx * rows as usize <= SEAM_WG_PER_SUBMIT || rows == 1), "{gw}x{gh}");
                next += rows;
            }
            assert_eq!(next as usize, gh.div_ceil(16), "every row once");
        }
        assert!(seam_bands(1086, 1086).len() >= 5, "a region's pass is split");
    }

    #[test]
    fn the_decks_gate() {
        // (the one test that touches these switches and the gate's state)
        std::env::set_var("BNGE_GPU_DECK", "0");
        assert!(allowed(), "off a Deck: always");
        std::env::set_var("BNGE_GPU_DECK", "1");
        std::env::set_var("BNGE_GPU_PARK_S", "0.3");
        note_pose(20.0, false);
        assert!(!allowed(), "driving");
        note_pose(0.0, false);
        assert!(!allowed(), "just stopped: not parked yet");
        note_pose(20.0, true);
        assert!(allowed(), "a teleport loading");
        note_pose(0.1, false);
        assert!(!allowed(), "loaded, stood under park_s");
        std::thread::sleep(Duration::from_millis(350));
        note_pose(0.1, false);
        assert!(allowed(), "parked");
        note_pose(f64::NAN, false);
        assert!(!allowed(), "a NaN speed counts as moving");
        std::env::set_var("BNGE_GPU_DRIVING", "1");
        assert!(allowed(), "the A/B switch");
        std::env::remove_var("BNGE_GPU_DRIVING");
        std::env::remove_var("BNGE_GPU_PARK_S");
        std::env::remove_var("BNGE_GPU_DECK");
    }

    #[test]
    fn kept_buffer_sizes() {
        assert_eq!(round_cap(1), 1 << 20);
        assert_eq!(round_cap((1 << 20) + 1), 2 << 20);
    }

    /// On a machine with a GPU: the kept buffers give the same blocks call after call, across a size change and a
    /// trim (no GPU: nothing to check).
    #[test]
    fn bc7_on_the_gpu_with_kept_buffers() {
        let Some(g) = get() else { return };
        let img = |n: usize, seed: u32| -> Vec<u8> {
            (0..n * n * 4).map(|i| ((i as u32).wrapping_mul(2654435761).wrapping_add(seed) >> 24) as u8).collect()
        };
        let (a, b) = (img(256, 1), img(64, 2));
        let first = bc7_encode(g, &[(256, 256, &a), (64, 64, &b)]).unwrap();
        assert_eq!(first[0].len(), 64 * 64 * 16);
        assert_eq!(first[1].len(), 16 * 16 * 16);
        let st = last().unwrap();
        assert!(st.ok && st.submits >= 2 && st.up_bytes >= (a.len() + b.len()) as u64, "{st:?}");
        assert_eq!(st.down_bytes, ((64 * 64 + 16 * 16) * 16) as u64);
        let again = bc7_encode(g, &[(256, 256, &a), (64, 64, &b)]).unwrap();
        assert_eq!(first, again, "the kept buffers: the same blocks");
        let big = img(1024, 3);
        let _ = bc7_encode(g, &[(1024, 1024, &big)]).unwrap();
        trim_idle(Duration::ZERO);
        let after = bc7_encode(g, &[(256, 256, &a), (64, 64, &b)]).unwrap();
        assert_eq!(first, after, "after a larger call and a trim");
    }
}
