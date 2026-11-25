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
    pub depth: f64,          // (M3) m of slab and girders the deck builder draws under its structure (0: unknown, DECK_FULL_M)
    pub xsec: String,        // (BNE_ROADSYS_XSEC) its cross-section tags, xsec.rs's text ("": none)
    pub maxh: Vec<(f64, f64, f64)>,  // (M5) its height limits (maxheight signs): (start, end) fractions, m
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
    pub xsec: bool,          // (BNE_ROADSYS_XSEC) the cross-section model on: every stroke gets its strip slots
    pub areas: Vec<crate::areas::Area>,     // OSM's mapped road areas round the read (areas.rs: the audit, the fit)
    pub area_build: u64,                    // their dataset build's identity (in the keys where the fit is on)
    pub rails: Vec<(Vec<P2>, f64)>,         // (M5) railway lines at ground level round the read: (points, clearance m)
}

impl Place {
    /// Everything but the segments (a place read from a subset of them).
    pub fn clone_meta(&self) -> Place {
        Place { name: self.name.clone(), lat: self.lat, lon: self.lon, core: self.core, halo: self.halo,
                country: self.country.clone(), style: self.style.clone(), segs: Vec::new(), dem: Dem::default(), xsec: self.xsec,
                areas: self.areas.clone(), area_build: self.area_build, rails: self.rails.clone() }
    }
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
    if ver != 3 && ver != 4 { return Err(format!("format {} (want 3 or 4)", ver)); }
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
        if ver >= 4 { s.xsec = r.s(); }     // (format 4: the cross-section tags, the model on)
        p.segs.push(s);
    }
    p.xsec = ver >= 4;
    let n = r.u32() as usize;
    let step = r.f64();
    let x0 = r.f64();
    let mut z = Vec::with_capacity(n * n);
    for _ in 0..n * n { z.push(r.f32()); }
    p.dem = Dem { n, step, x0, z };
    Ok(p)
}

// ------------------------------------------------------------------- a tile build's inputs (replay across platforms)

struct Wr(Vec<u8>);
impl Wr {
    fn u8(&mut self, v: u8) { self.0.push(v); }
    fn u32(&mut self, v: u32) { self.0.extend_from_slice(&v.to_le_bytes()); }
    fn i64(&mut self, v: i64) { self.0.extend_from_slice(&v.to_le_bytes()); }
    fn f64(&mut self, v: f64) { self.0.extend_from_slice(&v.to_le_bytes()); }
    fn s(&mut self, v: &str) { self.u32(v.len() as u32); self.0.extend_from_slice(v.as_bytes()); }
}

/// A shadow tile build's whole input (product::build): the segments, the style, the quad, the label, the frame, the
/// cut grid and the ground heights the build asked for (in its order): "RSTILE01" + fields, little-endian. The CLI
/// (`roadsys tile`) builds it again on any platform: the same bytes are the determinism test.
/// RSTILE03 adds the mapped road areas, their build and the fit's switch (areas_on); RSTILE04 (BNE_ROADSYS_XSEC) is
/// RSTILE03 with every segment's cross-section tags after its depth (the model on).
pub struct TileInput { pub place: Place, pub quad: [[f64; 2]; 4], pub id: (i32, i32), pub frame: u64, pub cut: (f64, f64), pub ground: Vec<f64>, pub areas_on: bool }

pub fn write_tile(t: &TileInput) -> Vec<u8> {
    let mut w = Wr(Vec::new());
    // (RSTILE04: RSTILE03 with the cross-section model on, every segment's tags after its depth; RSTILE03 otherwise,
    // byte for byte as before)
    // (RSTILE05, M5: a segment has height limits: the cross-section switch as a byte after the magic, every segment's
    // limits after its depth and tags)
    // (RSTILE06, M5: railway lines round the read: RSTILE05 with the rails after the areas)
    let v6 = !t.place.rails.is_empty();
    let v5 = v6 || t.place.segs.iter().any(|s| !s.maxh.is_empty());
    w.0.extend_from_slice(if v6 { b"RSTILE06" } else if v5 { b"RSTILE05" } else if t.place.xsec { b"RSTILE04" } else { b"RSTILE03" });
    if v5 { w.u8(t.place.xsec as u8); }
    let st = &t.place.style;
    w.s(&st.centre); w.s(&st.left_edge);
    for v in [st.w, st.w_motorway, st.edge, st.edge_motorway, st.centre_urban.0, st.centre_urban.1, st.lane_urban.0,
              st.lane_urban.1, st.lane_motorway.0, st.lane_motorway.1] { w.f64(v); }
    w.u8(st.double_centre as u8);
    w.s(&t.place.country);
    w.u32(t.place.segs.len() as u32);
    for s in &t.place.segs {
        for v in [&s.fid, &s.code, &s.cls_s, &s.turn, &s.turn_bw, &s.place] { w.s(v); }
        for v in [s.pave, s.drive, s.kmh, s.lane_w] { w.f64(v); }
        for v in [s.inferred, s.urban as i64, s.cls, s.link as i64, s.dir, s.lanes, s.lfw, s.lbw, s.region] { w.i64(v); }
        w.u32(s.pts.len() as u32);
        for p in &s.pts { w.f64(p[0]); w.f64(p[1]); }
        w.u32(s.cons.len() as u32);
        for (c, v) in &s.cons { w.s(c); w.u32(*v as u32); }
        w.u32(s.bridge.len() as u32);
        for &(a, b) in &s.bridge { w.f64(a); w.f64(b); }
        w.u32(s.tunnel.len() as u32);
        for &(a, b) in &s.tunnel { w.f64(a); w.f64(b); }
        w.u32(s.level.len() as u32);
        for &(a, b, v) in &s.level { w.f64(a); w.f64(b); w.f64(v); }
        w.f64(s.depth);
        if t.place.xsec { w.s(&s.xsec); }
        if v5 {
            w.u32(s.maxh.len() as u32);
            for &(a, b, v) in &s.maxh { w.f64(a); w.f64(b); w.f64(v); }
        }
    }
    for q in &t.quad { w.f64(q[0]); w.f64(q[1]); }
    w.i64(t.id.0 as i64); w.i64(t.id.1 as i64);
    w.0.extend_from_slice(&t.frame.to_le_bytes());
    w.f64(t.cut.0); w.f64(t.cut.1);
    w.u32(t.ground.len() as u32);
    for &v in &t.ground { w.f64(v); }
    w.u8(t.areas_on as u8);
    w.0.extend_from_slice(&t.place.area_build.to_le_bytes());
    w.u32(t.place.areas.len() as u32);
    for a in &t.place.areas {
        w.0.extend_from_slice(&a.key.to_le_bytes());
        w.s(&a.value);
        w.i64(a.layer);
        w.u8(a.flags);
        w.u32(a.links.len() as u32);
        for l in &a.links { w.0.extend_from_slice(&l.to_le_bytes()); }
        w.u32(a.rings.len() as u32);
        for (role, r) in &a.rings {
            w.u8(*role);
            w.u32(r.len() as u32);
            for p in r { w.f64(p[0]); w.f64(p[1]); }
        }
    }
    if v6 {
        w.u32(t.place.rails.len() as u32);
        for (p, c) in &t.place.rails {
            w.f64(*c);
            w.u32(p.len() as u32);
            for q in p { w.f64(q[0]); w.f64(q[1]); }
        }
    }
    w.0
}

pub fn read_tile(b: &[u8]) -> Result<TileInput, String> {
    let mut r = R { b, i: 0 };
    if b.len() < 8 { return Err("not a roadsys tile input".into()); }
    let magic = r.take(8).to_vec();
    let v6 = magic == b"RSTILE06";
    let v5 = v6 || magic == b"RSTILE05";
    let xs5 = if v5 { r.u8() != 0 } else { false };
    let v4 = magic == b"RSTILE04" || (v5 && xs5);
    let v3 = v4 || v5 || magic == b"RSTILE03";
    let v2 = v3 || magic == b"RSTILE02";
    if !v2 && magic != b"RSTILE01" { return Err("not a roadsys tile input".into()); }
    let mut st = Style { centre: r.s(), left_edge: r.s(), ..Default::default() };
    st.w = r.f64(); st.w_motorway = r.f64(); st.edge = r.f64(); st.edge_motorway = r.f64();
    st.centre_urban = (r.f64(), r.f64()); st.lane_urban = (r.f64(), r.f64()); st.lane_motorway = (r.f64(), r.f64());
    st.double_centre = r.u8() != 0;
    let country = r.s();
    let n = r.u32() as usize;
    let mut segs = Vec::with_capacity(n);
    for _ in 0..n {
        let mut s = Seg { fid: r.s(), code: r.s(), cls_s: r.s(), turn: r.s(), turn_bw: r.s(), place: r.s(), ..Default::default() };
        s.pave = r.f64(); s.drive = r.f64(); s.kmh = r.f64(); s.lane_w = r.f64();
        s.inferred = r.i64(); s.urban = r.i64() != 0; s.cls = r.i64(); s.link = r.i64() != 0; s.dir = r.i64();
        s.lanes = r.i64(); s.lfw = r.i64(); s.lbw = r.i64(); s.region = r.i64();
        let np = r.u32() as usize;
        for _ in 0..np { let x = r.f64(); let y = r.f64(); s.pts.push([x, y]); }
        let nc = r.u32() as usize;
        for _ in 0..nc { let c = r.s(); let v = r.u32() as usize; s.cons.push((c, v)); }
        let nb = r.u32() as usize;
        for _ in 0..nb { s.bridge.push((r.f64(), r.f64())); }
        let nt = r.u32() as usize;
        for _ in 0..nt { s.tunnel.push((r.f64(), r.f64())); }
        let nl = r.u32() as usize;
        for _ in 0..nl { s.level.push((r.f64(), r.f64(), r.f64())); }
        if v2 { s.depth = r.f64(); }
        if v4 { s.xsec = r.s(); }
        if v5 { let nm = r.u32() as usize; for _ in 0..nm { s.maxh.push((r.f64(), r.f64(), r.f64())); } }
        segs.push(s);
    }
    let mut quad = [[0.0; 2]; 4];
    for q in quad.iter_mut() { q[0] = r.f64(); q[1] = r.f64(); }
    let id = (r.i64() as i32, r.i64() as i32);
    let frame = u64::from_le_bytes(r.take(8).try_into().unwrap());
    let cut = (r.f64(), r.f64());
    let ng = r.u32() as usize;
    let ground = (0..ng).map(|_| r.f64()).collect();
    let mut place = Place { country, style: st, segs, xsec: v4, ..Default::default() };
    let mut areas_on = false;
    if v3 {
        areas_on = r.u8() != 0;
        let (build, areas) = areas_block(&mut r);
        place.area_build = build;
        place.areas = areas;
    }
    if v6 {
        let nr = r.u32() as usize;
        for _ in 0..nr {
            let c = r.f64();
            let np = r.u32() as usize;
            let p: Vec<P2> = (0..np).map(|_| { let x = r.f64(); let y = r.f64(); [x, y] }).collect();
            place.rails.push((p, c));
        }
    }
    Ok(TileInput { place, quad, id, frame, cut, ground, areas_on })
}

/// A tile input's areas block: the build, the count, the areas.
fn areas_block(r: &mut R<'_>) -> (u64, Vec<crate::areas::Area>) {
    let build = u64::from_le_bytes(r.take(8).try_into().unwrap());
    let na = r.u32() as usize;
    let mut areas = Vec::with_capacity(na);
    for _ in 0..na {
        let key = u64::from_le_bytes(r.take(8).try_into().unwrap());
        let value = r.s();
        let layer = r.i64();
        let flags = r.u8();
        let nl = r.u32() as usize;
        let links = (0..nl).map(|_| u64::from_le_bytes(r.take(8).try_into().unwrap())).collect();
        let nr = r.u32() as usize;
        let mut rings = Vec::with_capacity(nr);
        for _ in 0..nr {
            let role = r.u8();
            let np = r.u32() as usize;
            rings.push((role, (0..np).map(|_| { let x = r.f64(); [x, r.f64()] }).collect()));
        }
        areas.push(crate::areas::Area { key, value, layer, flags, links, rings });
    }
    (build, areas)
}

/// (the CLI's --areas) "RSAREAS1" + a tile input's areas block: mapped road areas in a place's frame.
pub fn read_areas(b: &[u8]) -> Result<(u64, Vec<crate::areas::Area>), String> {
    if b.len() < 20 || &b[..8] != b"RSAREAS1" { return Err("not a roadsys areas file".into()); }
    let mut r = R { b, i: 8 };
    Ok(areas_block(&mut r))
}
