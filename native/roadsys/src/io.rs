//! The exporter's place file (~/bnge-tests/pt3/tools/export_place.py, format 3): segments as the server's v3 lane
//! graph reads them, their bridge / tunnel / level ranges, connectors, the marking style and a DEM grid.

pub type P2 = [f64; 2];

#[derive(Clone, Debug, Default)]
pub struct Seg {
    pub fid: String,
    pub code: String,
    pub cls_s: String,
    pub pave: f64,
    pub drive: f64,
    pub inferred: i64,
    pub urban: bool,
    pub cls: i64,
    pub link: bool,
    pub dir: i64,
    pub lanes: i64,
    pub lfw: i64,
    pub lbw: i64,
    pub kmh: f64,
    pub lane_w: f64,
    pub region: i64,
    pub turn: String,
    pub turn_bw: String,
    pub place: String,
    pub pts: Vec<P2>,
    pub cons: Vec<(String, usize)>,
    pub bridge: Vec<(f64, f64)>,
    pub tunnel: Vec<(f64, f64)>,
    pub level: Vec<(f64, f64, f64)>,
}

#[derive(Clone, Debug, Default)]
pub struct Style {
    pub centre: String,
    pub left_edge: String,
    pub w: f64,
    pub w_motorway: f64,
    pub edge: f64,
    pub edge_motorway: f64,
    pub centre_urban: (f64, f64),
    pub lane_urban: (f64, f64),
    pub lane_motorway: (f64, f64),
    pub double_centre: bool,
}

#[derive(Clone, Debug, Default)]
pub struct Dem {
    pub n: usize,
    pub step: f64,
    pub x0: f64,
    pub z: Vec<f32>,
}

impl Dem {
    /// Bilinear height at (x, y) in the place frame (clamped to the grid).
    pub fn at(&self, p: P2) -> f64 {
        if self.n < 2 { return 0.0; }
        let fx = (p[0] - self.x0) / self.step;
        let fy = (p[1] - self.x0) / self.step;
        let mx = (self.n - 2) as f64;
        let fx = if fx < 0.0 { 0.0 } else if fx > mx { mx } else { fx };
        let fy = if fy < 0.0 { 0.0 } else if fy > mx { mx } else { fy };
        let (ix, iy) = (fx as usize, fy as usize);
        let (u, v) = (fx - ix as f64, fy - iy as f64);
        let g = |x: usize, y: usize| self.z[y * self.n + x] as f64;
        let a = g(ix, iy) * (1.0 - u) + g(ix + 1, iy) * u;
        let b = g(ix, iy + 1) * (1.0 - u) + g(ix + 1, iy + 1) * u;
        a * (1.0 - v) + b * v
    }
}

#[derive(Clone, Debug, Default)]
pub struct Place {
    pub name: String,
    pub lat: f64,
    pub lon: f64,
    pub core: f64,
    pub halo: f64,
    pub country: String,
    pub style: Style,
    pub segs: Vec<Seg>,
    pub dem: Dem,
}

struct R<'a> { b: &'a [u8], i: usize }

impl R<'_> {
    fn take(&mut self, n: usize) -> &[u8] {
        let s = &self.b[self.i..self.i + n];
        self.i += n;
        s
    }
    fn u8(&mut self) -> u8 { self.take(1)[0] }
    fn u32(&mut self) -> u32 { u32::from_le_bytes(self.take(4).try_into().unwrap()) }
    fn i64(&mut self) -> i64 { i64::from_le_bytes(self.take(8).try_into().unwrap()) }
    fn f64(&mut self) -> f64 { f64::from_le_bytes(self.take(8).try_into().unwrap()) }
    fn f32(&mut self) -> f32 { f32::from_le_bytes(self.take(4).try_into().unwrap()) }
    fn s(&mut self) -> String {
        let n = self.u32() as usize;
        String::from_utf8_lossy(self.take(n)).into_owned()
    }
}

pub fn read(path: &str) -> Result<Place, String> {
    let b = std::fs::read(path).map_err(|e| format!("{}: {}", path, e))?;
    let mut r = R { b: &b, i: 0 };
    if r.take(8) != b"PT3PLACE" { return Err("not a PT3 place file".into()); }
    let ver = r.u32();
    if ver != 3 { return Err(format!("format {} (want 3)", ver)); }
    let mut p = Place { name: r.s(), ..Default::default() };
    p.lat = r.f64(); p.lon = r.f64(); p.core = r.f64(); p.halo = r.f64();
    p.country = r.s();
    let mut st = Style { centre: r.s(), left_edge: r.s(), ..Default::default() };
    st.w = r.f64(); st.w_motorway = r.f64(); st.edge = r.f64(); st.edge_motorway = r.f64();
    st.centre_urban = (r.f64(), r.f64());
    st.lane_urban = (r.f64(), r.f64());
    st.lane_motorway = (r.f64(), r.f64());
    st.double_centre = r.u8() != 0;
    p.style = st;
    let ns = r.u32() as usize;
    for _ in 0..ns {
        let mut s = Seg { fid: r.s(), code: r.s(), cls_s: r.s(), ..Default::default() };
        s.pave = r.f64(); s.drive = r.f64(); s.inferred = r.i64(); s.urban = r.u8() != 0;
        s.cls = r.i64(); s.link = r.u8() != 0; s.dir = r.i64(); s.lanes = r.i64(); s.lfw = r.i64(); s.lbw = r.i64();
        s.kmh = r.f64(); s.lane_w = r.f64(); s.region = r.i64();
        s.turn = r.s(); s.turn_bw = r.s(); s.place = r.s();
        let np = r.u32() as usize;
        for _ in 0..np { let x = r.f64(); let y = r.f64(); s.pts.push([x, y]); }
        let nc = r.u32() as usize;
        for _ in 0..nc { let id = r.s(); let v = r.u32() as usize; s.cons.push((id, v)); }
        let nb = r.u32() as usize;
        for _ in 0..nb { s.bridge.push((r.f64(), r.f64())); }
        let nt = r.u32() as usize;
        for _ in 0..nt { s.tunnel.push((r.f64(), r.f64())); }
        let nl = r.u32() as usize;
        for _ in 0..nl { s.level.push((r.f64(), r.f64(), r.f64())); }
        p.segs.push(s);
    }
    let n = r.u32() as usize;
    let step = r.f64();
    let x0 = r.f64();
    let mut z = Vec::with_capacity(n * n);
    for _ in 0..n * n { z.push(r.f32()); }
    p.dem = Dem { n, step, x0, z };
    Ok(p)
}
