//! Plan views (north up, top down) of the built scene round a point, for the report's thumbnails beside the aerial:
//! the physics terrain shaded by height (holes red, paved paint darker), the drawn roads grey, sidewalks light, the
//! markings white, deck sides brown and deck tops orange (higher over lower), rails purple, barriers red-brown, the
//! tunnel tubes translucent blue, the mapped centre lines (ground cyan, bridge yellow, tunnel magenta) and the issue
//! marked by a magenta ring. Written as a binary PPM (P6).
use crate::scene::{self, Packs};
use roadsys::audit::{FLAG_DECK_SIDE, FLAG_DECK_TOP, FLAG_MARK, KIND_BARRIER, KIND_DECK, KIND_RAIL, KIND_ROAD, KIND_TUNNEL, KIND_WALK, DOWN};

fn layer(kind: u8, flags: u8, cls: u8) -> Option<(u8, [u8; 3], f64)> {
    Some(match kind {
        KIND_ROAD if flags & FLAG_MARK != 0 => (3, [240, 240, 235], 1.0),
        KIND_ROAD => (1, [118, 118, 122], 1.0),
        KIND_WALK => (2, [190, 188, 180], 1.0),
        KIND_RAIL => (4, [140, 70, 170], 1.0),
        KIND_BARRIER => (5, [160, 70, 60], 1.0),
        KIND_DECK if flags & FLAG_DECK_TOP != 0 => (7, [225, 145, 60], 1.0),
        KIND_DECK if flags & FLAG_DECK_SIDE != 0 => (6, [150, 95, 45], 1.0),
        KIND_DECK => (6, [170, 110, 60], 1.0),
        KIND_TUNNEL if cls == DOWN => (8, [70, 110, 215], 0.35),
        KIND_TUNNEL => (8, [70, 110, 215], 0.2),
        _ => return None,       // (collider-only meshes: walkcol, buscol, railcol, others)
    })
}

pub fn render(packs: &mut Packs, lat: f64, lon: f64, half: f64, px: usize, marks: &[(f64, f64)]) -> Result<Vec<u8>, String> {
    let ix = packs.ix;
    // the zoom-17 tile under the point (geotiles: the slippy-map numbering), else the one whose frame is nearest
    let n = (1u64 << 17) as f64;
    let gx = ((lon + 180.0) / 360.0 * n).floor() as i64;
    let gy = ((1.0 - lat.to_radians().tan().asinh() / std::f64::consts::PI) / 2.0 * n).floor() as i64;
    let ti = match ix.by_key.get(&format!("g17_{}_{}", gx, gy)) {
        Some(&i) => i,
        None => (0..ix.tiles.len()).min_by(|&a, &b| {
            let d = |i: usize| crate::rank::dist_m((ix.tiles[i].aff[0], ix.tiles[i].aff[3]), (lat, lon));
            d(a).total_cmp(&d(b)).then(a.cmp(&b))
        }).ok_or("no tiles")?,
    };
    let b = scene::build(packs, ti, half.clamp(8.0, 150.0))?;
    let a = b.aff;
    // (lat, lon) -> the tile's x, y: the affine's inverse
    let det = a[1] * a[5] - a[2] * a[4];
    let to_xy = |la: f64, lo: f64| {
        let (u, v) = (la - a[0], lo - a[3]);
        [(a[5] * u - a[2] * v) / det, (-a[4] * u + a[1] * v) / det]
    };
    let c = to_xy(lat, lon);
    let s = 2.0 * half / px as f64;
    let mut img = vec![0u8; px * px * 3];
    let ter = &b.scene.terrain;
    let mut zs: Vec<f64> = Vec::new();
    for j in 0..px { for i in 0..px {
        let (x, y) = (c[0] - half + (i as f64 + 0.5) * s, c[1] + half - (j as f64 + 0.5) * s);
        zs.push(ter.at(x, y));
    } }
    let fin: Vec<f64> = zs.iter().copied().filter(|z| z.is_finite()).collect();
    let (zlo, zhi) = fin.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |m, &z| (m.0.min(z), m.1.max(z)));
    for j in 0..px { for i in 0..px {
        let z = zs[j * px + i];
        let k = (j * px + i) * 3;
        if !z.is_finite() {
            let (x, y) = (c[0] - half + (i as f64 + 0.5) * s, c[1] + half - (j as f64 + 0.5) * s);
            let (fx, fy) = ((x - ter.ox as f64).floor(), (y - ter.oy as f64).floor());
            let inside = fx >= 0.0 && fy >= 0.0 && fx < (ter.n - 1) as f64 && fy < (ter.n - 1) as f64
                && ter.z[fy as usize * ter.n + fx as usize].is_finite();
            img[k..k + 3].copy_from_slice(if inside { &[200, 40, 40] } else { &[20, 20, 20] });
            continue;
        }
        // hillshade from the north-west plus the height band
        let zx = if i + 1 < px { zs[j * px + i + 1] } else { z };
        let zy = if j + 1 < px { zs[(j + 1) * px + i] } else { z };
        let (gx, gy) = (if zx.is_finite() { (zx - z) / s } else { 0.0 }, if zy.is_finite() { (z - zy) / s } else { 0.0 });
        let shade = (0.75 - 0.6 * (gx * 0.7 - gy * 0.7)).clamp(0.35, 1.0);
        let t = if zhi > zlo { (z - zlo) / (zhi - zlo) } else { 0.5 };
        let base = [70.0 + 60.0 * t, 105.0 + 40.0 * t, 60.0 + 30.0 * t];
        let (x, y) = (c[0] - half + (i as f64 + 0.5) * s, c[1] + half - (j as f64 + 0.5) * s);
        let paved = b.ter.paved_at(x, y) == Some(true);
        for q in 0..3 {
            let v = if paved { 95.0 } else { base[q] } * shade;
            img[k + q] = v.clamp(0.0, 255.0) as u8;
        }
    } }
    // the meshes, layer by layer, lower faces first
    let mut order: Vec<(u8, f64, usize)> = Vec::new();
    for (k, t) in b.scene.tris.iter().enumerate() {
        if let Some((l, _, _)) = layer(t.kind, t.flags, t.cls) {
            let zmax = t.p.iter().map(|v| v[2]).fold(f64::NEG_INFINITY, f64::max);
            order.push((l, zmax, k));
        }
    }
    order.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.total_cmp(&b.1)).then(a.2.cmp(&b.2)));
    let to_px = |p: [f64; 3]| [(p[0] - (c[0] - half)) / s, ((c[1] + half) - p[1]) / s];
    for &(_, _, k) in &order {
        let t = &b.scene.tris[k];
        let (_, col, alpha) = layer(t.kind, t.flags, t.cls).unwrap();
        let q = [to_px(t.p[0]), to_px(t.p[1]), to_px(t.p[2])];
        let (x0, x1) = (q.iter().map(|p| p[0]).fold(f64::INFINITY, f64::min), q.iter().map(|p| p[0]).fold(f64::NEG_INFINITY, f64::max));
        let (y0, y1) = (q.iter().map(|p| p[1]).fold(f64::INFINITY, f64::min), q.iter().map(|p| p[1]).fold(f64::NEG_INFINITY, f64::max));
        if x1 < 0.0 || y1 < 0.0 || x0 >= px as f64 || y0 >= px as f64 { continue; }
        let area = (q[1][0] - q[0][0]) * (q[2][1] - q[0][1]) - (q[2][0] - q[0][0]) * (q[1][1] - q[0][1]);
        if area.abs() < 1e-9 { continue; }
        for j in (y0.floor().max(0.0) as usize)..=(y1.ceil().min(px as f64 - 1.0) as usize) {
            for i in (x0.floor().max(0.0) as usize)..=(x1.ceil().min(px as f64 - 1.0) as usize) {
                let p = [i as f64 + 0.5, j as f64 + 0.5];
                let e = |a: [f64; 2], b: [f64; 2]| (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
                let (w0, w1, w2) = (e(q[0], q[1]), e(q[1], q[2]), e(q[2], q[0]));
                let inside = (w0 >= 0.0 && w1 >= 0.0 && w2 >= 0.0) || (w0 <= 0.0 && w1 <= 0.0 && w2 <= 0.0);
                if !inside { continue; }
                let k = (j * px + i) * 3;
                for c3 in 0..3 {
                    img[k + c3] = (img[k + c3] as f64 * (1.0 - alpha) + col[c3] as f64 * alpha).round() as u8;
                }
            }
        }
    }
    // mapped centre lines
    let dot = |x: f64, y: f64, col: [u8; 3], img: &mut Vec<u8>| {
        let (i, j) = (((x - (c[0] - half)) / s).floor(), (((c[1] + half) - y) / s).floor());
        if i >= 0.0 && j >= 0.0 && (i as usize) < px && (j as usize) < px {
            let k = (j as usize * px + i as usize) * 3;
            img[k..k + 3].copy_from_slice(&col);
        }
    };
    for l in &b.lines {
        let col = match l.level { 0 => [60, 220, 230], 1 => [250, 230, 60], _ => [235, 60, 220] };
        for w in l.xy.windows(2) {
            let d = ((w[1][0] - w[0][0]).powi(2) + (w[1][1] - w[0][1]).powi(2)).sqrt();
            let n = (d / (s * 0.7)).ceil().max(1.0) as usize;
            for k in 0..=n {
                if l.level == 2 && (k / 4) % 2 == 1 { continue; }
                let u = k as f64 / n as f64;
                dot(w[0][0] + (w[1][0] - w[0][0]) * u, w[0][1] + (w[1][1] - w[0][1]) * u, col, &mut img);
            }
        }
    }
    for &(mla, mlo) in marks {
        let m = to_xy(mla, mlo);
        let r = (6.0 * s).max(2.5);
        for k in 0..96 {
            let a = k as f64 / 96.0 * std::f64::consts::TAU;
            for dr in [0.0, s] {
                dot(m[0] + (r + dr) * a.cos(), m[1] + (r + dr) * a.sin(), [255, 0, 255], &mut img);
            }
        }
    }
    let mut out = format!("P6\n{} {}\n255\n", px, px).into_bytes();
    out.extend_from_slice(&img);
    Ok(out)
}
