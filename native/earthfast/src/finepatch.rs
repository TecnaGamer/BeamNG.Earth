//! Fine ground (server finepatch.py): where a terrain block's graded ground is sharp (a cutting's wall, a mountain
//! road's steep side), its 2 m lattice draws a staircase of slanted cells and tilts the lanes beside them. Those cells
//! are holes in the block and a finer mesh of the same graded ground fills them. These are the cells, the holes and the
//! mesh; the heights come from the block's own heights function (Python: the grader is already Rust).
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

/// The cells (between samples [j..j+2, i..i+2]) of heights u (ny, nx) to draw finely: their corners `sharp_dz` or more
/// apart where the graded ground u lies `graded_dz` or more off the natural ground raw at one of them (natural cliffs
/// stay the terrain's), grown by `grow` cells (the slope's foot and top, the lanes beside it). (ny - 1, nx - 1) u8.
#[pyfunction]
pub fn fine_cells<'py>(py: Python<'py>, u: PyReadonlyArray2<'py, f64>, raw: PyReadonlyArray2<'py, f64>, sharp_dz: f64,
                       graded_dz: f64, grow: i64) -> Bound<'py, PyArray2<u8>> {
    let (u, raw) = (u.as_array(), raw.as_array());
    let (ny, nx) = (u.nrows(), u.ncols());
    let (cy, cx) = (ny.saturating_sub(1), nx.saturating_sub(1));
    let mut sharp = vec![false; cy * cx];
    for j in 0..cy {
        for i in 0..cx {
            let c = [u[[j, i]], u[[j, i + 1]], u[[j + 1, i]], u[[j + 1, i + 1]]];
            let hi = c.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let lo = c.iter().cloned().fold(f64::INFINITY, f64::min);
            let moved = [(j, i), (j, i + 1), (j + 1, i), (j + 1, i + 1)].iter()
                .map(|&(a, b)| (u[[a, b]] - raw[[a, b]]).abs()).fold(0.0, f64::max);
            sharp[j * cx + i] = hi - lo >= sharp_dz && moved >= graded_dz;
        }
    }
    let g = grow.max(0) as usize;
    let dilate = |m: &Vec<bool>, r: usize| -> Vec<bool> {
        let mut o = vec![false; cy * cx];
        for j in 0..cy {
            for i in 0..cx {
                if !m[j * cx + i] {
                    continue;
                }
                for a in j.saturating_sub(r)..(j + r + 1).min(cy) {
                    for b in i.saturating_sub(r)..(i + r + 1).min(cx) {
                        o[a * cx + b] = true;
                    }
                }
            }
        }
        o
    };
    let grown = dilate(&sharp, g);
    // closed (dilated by one, then eroded by one): no lone terrain cell inside a patch, which the engine draws with a
    // hole for a corner (pale: its layer index there is the hole's)
    let big = dilate(&grown, 1);
    let mut out = vec![0u8; cy * cx];
    for j in 0..cy {
        for i in 0..cx {
            let mut all = true;
            'n: for a in j.saturating_sub(1)..(j + 2).min(cy) {
                for b in i.saturating_sub(1)..(i + 2).min(cx) {
                    if !big[a * cx + b] {
                        all = false;
                        break 'n;
                    }
                }
            }
            out[j * cx + i] = (grown[j * cx + i] || all) as u8;
        }
    }
    // (and every island of terrain inside a patch filled: the closing above fills gaps of one cell only, and an island
    // of a few cells between a hairpin's legs, its corners all holes, drew pale whole; the user's report 2026-11-01. An
    // island: terrain cells not connected (4-way) to the block's edge through terrain cells)
    let mut outside = vec![false; cy * cx];
    let mut stack: Vec<usize> = Vec::new();
    for j in 0..cy {
        for i in 0..cx {
            if (j == 0 || i == 0 || j + 1 == cy || i + 1 == cx) && out[j * cx + i] == 0 {
                outside[j * cx + i] = true;
                stack.push(j * cx + i);
            }
        }
    }
    while let Some(k) = stack.pop() {
        let (j, i) = (k / cx, k % cx);
        let mut push = |a: usize, b: usize| {
            let q = a * cx + b;
            if !outside[q] && out[q] == 0 {
                outside[q] = true;
                stack.push(q);
            }
        };
        if j > 0 { push(j - 1, i); }
        if j + 1 < cy { push(j + 1, i); }
        if i > 0 { push(j, i - 1); }
        if i + 1 < cx { push(j, i + 1); }
    }
    for k in 0..cy * cx {
        if !outside[k] {
            out[k] = 1;
        }
    }
    numpy::ndarray::Array2::from_shape_vec((cy, cx), out).unwrap().into_pyarray(py)
}

/// The samples (ny, nx) of a block that are holes for the fine cells (ny - 1, nx - 1): each fine cell's south-west
/// sample (the engine skips a square by its south-west sample's hole flag and draws a kept one with all four corners:
/// tunnel-facts, earthfast tunnel_holes; the 2026-10-25 prototype's "all four cells round a sample" left the fine
/// cells round the patch's rim drawn by the terrain too, pale where a corner was a hole). The block's outermost samples
/// are left alone.
#[pyfunction]
pub fn fine_holes<'py>(py: Python<'py>, cells: PyReadonlyArray2<'py, u8>) -> Bound<'py, PyArray2<u8>> {
    let c = cells.as_array();
    let (cy, cx) = (c.nrows(), c.ncols());
    let (ny, nx) = (cy + 1, cx + 1);
    let mut out = vec![0u8; ny * nx];
    for j in 1..cy {
        for i in 1..cx {
            if c[[j, i]] != 0 {
                out[j * nx + i] = 1;
            }
        }
    }
    numpy::ndarray::Array2::from_shape_vec((ny, nx), out).unwrap().into_pyarray(py)
}

/// The rim of the fine cells (ny - 1, nx - 1): the cells the terrain still draws that can read a hole sample (fine_holes:
/// each fine cell's south-west one) in their layer blend, pale there: a hole corner of their own, or one more cell out,
/// as the terrain shader warps its layer lookups by up to half a sample (calcLayerWarp: pale tips past the first ring,
/// the user, 2026-11-01). The mesh covers them too, a little over the terrain.
#[pyfunction]
pub fn fine_rim<'py>(py: Python<'py>, cells: PyReadonlyArray2<'py, u8>) -> Bound<'py, PyArray2<u8>> {
    let c = cells.as_array();
    let (cy, cx) = (c.nrows(), c.ncols());
    let f = |j: usize, i: usize| j < cy && i < cx && c[[j, i]] != 0;
    let mut out = vec![0u8; cy * cx];
    for j in 0..cy {
        for i in 0..cx {
            if c[[j, i]] != 0 {
                continue;
            }
            // (its blend reads samples j - 1 ..= j + 2, i - 1 ..= i + 2 with the warp; a fine cell's hole is its own
            // south-west sample)
            let hit = (j.saturating_sub(1)..=j + 2).any(|a| (i.saturating_sub(1)..=i + 2).any(|b| f(a, b)));
            if hit {
                out[j * cx + i] = 1;
            }
        }
    }
    numpy::ndarray::Array2::from_shape_vec((cy, cx), out).unwrap().into_pyarray(py)
}

/// The fine mesh's points (x, y) of cells (n, 2) [j, i] on the lattice whose sample [0, 0] is at (x0, y0), `square`
/// apart, each cell k x k quads: (n * (k + 1)², 2), per cell row a (y) then column b (x).
#[pyfunction]
pub fn fine_points<'py>(py: Python<'py>, x0: f64, y0: f64, square: f64, k: i64, cells: PyReadonlyArray2<'py, i64>)
                        -> Bound<'py, PyArray2<f64>> {
    let c = cells.as_array();
    let k = k.max(1) as usize;
    let m = (k + 1) * (k + 1);
    let mut out = Vec::with_capacity(c.nrows() * m * 2);
    for r in 0..c.nrows() {
        let (j, i) = (c[[r, 0]] as f64, c[[r, 1]] as f64);
        for a in 0..=k {
            for b in 0..=k {
                out.push(x0 + (i + b as f64 / k as f64) * square);
                out.push(y0 + (j + a as f64 / k as f64) * square);
            }
        }
    }
    numpy::ndarray::Array2::from_shape_vec((c.nrows() * m, 2), out).unwrap().into_pyarray(py)
}

/// fine_tin's insertion order: the largest error first (total order over f64).
#[derive(PartialEq)]
struct Pending(f64, usize, u32);
impl Eq for Pending {}
impl PartialOrd for Pending {
    fn partial_cmp(&self, o: &Self) -> Option<std::cmp::Ordering> { Some(self.cmp(o)) }
}
impl Ord for Pending {
    fn cmp(&self, o: &Self) -> std::cmp::Ordering { self.0.total_cmp(&o.0).then(self.1.cmp(&o.1)) }
}

/// One piece's triangulation (fine_tin): CCW triangles, each edge k (v[k] -> v[k+1]) with its neighbour across (-1:
/// none, the piece's edge: never split but by the points given for it, never flipped), the candidate points inside each.
struct Tin {
    p: Vec<[f64; 3]>,
    v: Vec<[usize; 3]>,
    n: Vec<[i64; 3]>,
    cand: Vec<Vec<u32>>,
    ver: Vec<u32>,
}

fn orient(a: [f64; 3], b: [f64; 3], c: [f64; 3]) -> f64 {
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
}

fn incircle(a: [f64; 3], b: [f64; 3], c: [f64; 3], d: [f64; 3]) -> f64 {
    let (adx, ady, bdx, bdy, cdx, cdy) = (a[0] - d[0], a[1] - d[1], b[0] - d[0], b[1] - d[1], c[0] - d[0], c[1] - d[1]);
    let (ad, bd, cd) = (adx * adx + ady * ady, bdx * bdx + bdy * bdy, cdx * cdx + cdy * cdy);
    adx * (bdy * cd - bd * cdy) - ady * (bdx * cd - bd * cdx) + ad * (bdx * cdy - bdy * cdx)
}

const TIN_EPS: f64 = 1e-9;

impl Tin {
    fn plane(&self, t: usize, x: f64, y: f64) -> f64 {
        let [a, b, c] = [self.p[self.v[t][0]], self.p[self.v[t][1]], self.p[self.v[t][2]]];
        let d = orient(a, b, c);
        if d.abs() < 1e-12 {
            return (a[2] + b[2] + c[2]) / 3.0;
        }
        let q = [x, y, 0.0];
        let (wa, wb) = (orient(q, b, c) / d, orient(a, q, c) / d);
        wa * a[2] + wb * b[2] + (1.0 - wa - wb) * c[2]
    }

    fn inside(&self, t: usize, q: [f64; 3]) -> bool {
        let [a, b, c] = [self.p[self.v[t][0]], self.p[self.v[t][1]], self.p[self.v[t][2]]];
        orient(a, b, q) >= -TIN_EPS && orient(b, c, q) >= -TIN_EPS && orient(c, a, q) >= -TIN_EPS
    }

    /// Triangle t's worst candidate (error, index into cand[t]) or None.
    fn worst(&self, t: usize, cz: &[[f64; 3]]) -> Option<(f64, usize)> {
        let mut best: Option<(f64, usize)> = None;
        for (k, &c) in self.cand[t].iter().enumerate() {
            let q = cz[c as usize];
            let e = (q[2] - self.plane(t, q[0], q[1])).abs();
            if best.is_none_or(|b| e > b.0) {
                best = Some((e, k));
            }
        }
        best
    }

    fn new_tri(&mut self, v: [usize; 3], n: [i64; 3]) -> usize {
        self.v.push(v);
        self.n.push(n);
        self.cand.push(Vec::new());
        self.ver.push(0);
        self.v.len() - 1
    }

    /// In triangle nb, the neighbour across its edge (b, a) set to t.
    fn relink(&mut self, nb: i64, a: usize, b: usize, t: usize) {
        if nb < 0 {
            return;
        }
        let nb = nb as usize;
        for k in 0..3 {
            if self.v[nb][k] == b && self.v[nb][(k + 1) % 3] == a {
                self.n[nb][k] = t as i64;
                return;
            }
        }
    }

    /// The candidates of the triangles `from` (taken) shared out to the triangles `to` (the first holding each).
    fn share(&mut self, from: &[usize], to: &[usize], cz: &[[f64; 3]]) {
        let mut all: Vec<u32> = Vec::new();
        for &t in from {
            all.append(&mut self.cand[t]);
        }
        for c in all {
            let q = cz[c as usize];
            let t = to.iter().copied().find(|&t| self.inside(t, q)).unwrap_or(to[0]);
            self.cand[t].push(c);
        }
    }

    /// Point i into triangle t (inside it or on one of its edges); returns the triangles changed.
    fn insert(&mut self, t: usize, i: usize, cz: &[[f64; 3]]) -> Vec<usize> {
        let q = self.p[i];
        let [a, b, c] = self.v[t];
        let lens = |u: usize, w: usize| { let (pu, pw) = (self.p[u], self.p[w]); (pw[0] - pu[0]).hypot(pw[1] - pu[1]) };
        // (on an edge: within a hair of its line)
        let mut on = None;
        for k in 0..3 {
            let (u, w) = (self.v[t][k], self.v[t][(k + 1) % 3]);
            if orient(self.p[u], self.p[w], q).abs() <= 1e-7 * lens(u, w).max(1e-9) {
                on = Some(k);
            }
        }
        let mut changed;
        match on {
            None => {
                let [nab, nbc, nca] = self.n[t];
                let t1 = self.new_tri([b, c, i], [nbc, -1, t as i64]);
                let t2 = self.new_tri([c, a, i], [nca, t as i64, t1 as i64]);
                self.v[t] = [a, b, i];
                self.n[t] = [nab, t1 as i64, t2 as i64];
                self.n[t1][1] = t2 as i64;
                self.relink(nbc, b, c, t1);
                self.relink(nca, c, a, t2);
                changed = vec![t, t1, t2];
                self.share(&[t], &changed.clone(), cz);
                for (tt, ea) in [(t, 0usize), (t1, 0), (t2, 0)] {
                    self.legalize(tt, ea, i, cz, &mut changed);
                }
            }
            Some(k) => {
                let (pa, pb, pc) = (self.v[t][k], self.v[t][(k + 1) % 3], self.v[t][(k + 2) % 3]);
                let (u, nbc, nca) = (self.n[t][k], self.n[t][(k + 1) % 3], self.n[t][(k + 2) % 3]);
                // t -> (pa, i, pc) and tb = (i, pb, pc)
                let tb = self.new_tri([i, pb, pc], [-1, nbc, t as i64]);
                self.v[t] = [pa, i, pc];
                self.n[t] = [-1, tb as i64, nca];
                self.relink(nbc, pb, pc, tb);
                changed = vec![t, tb];
                if u >= 0 {
                    let u = u as usize;
                    let ku = (0..3).find(|&m| self.v[u][m] == pb && self.v[u][(m + 1) % 3] == pa).unwrap();
                    let pd = self.v[u][(ku + 2) % 3];
                    let (nad, ndb) = (self.n[u][(ku + 1) % 3], self.n[u][(ku + 2) % 3]);
                    // u = (pb, pa, pd) -> (i, pa, pd) [ub] and ua = (pb, i, pd)
                    let ua = self.new_tri([pb, i, pd], [tb as i64, u as i64, ndb]);
                    self.v[u] = [i, pa, pd];
                    self.n[u] = [t as i64, nad, ua as i64];
                    self.relink(ndb, pd, pb, ua);
                    self.n[t][0] = u as i64;
                    self.n[tb][0] = ua as i64;
                    let from = [t, tb, u, ua];
                    self.share(&[u], &from, cz);
                    changed.extend([u, ua]);
                    self.share(&[t], &[t, tb], cz);
                    self.legalize(u, 1, i, cz, &mut changed);
                    self.legalize(ua, 2, i, cz, &mut changed);
                } else {
                    self.share(&[t], &[t, tb], cz);
                }
                self.legalize(t, 2, i, cz, &mut changed);
                self.legalize(tb, 1, i, cz, &mut changed);
            }
        }
        changed
    }

    /// Lawson's flips from triangle t's edge k (opposite the new point i) on.
    fn legalize(&mut self, t: usize, k: usize, i: usize, cz: &[[f64; 3]], changed: &mut Vec<usize>) {
        let mut stack = vec![(t, k)];
        while let Some((t, k)) = stack.pop() {
            let (a, b) = (self.v[t][k], self.v[t][(k + 1) % 3]);
            if self.v[t][(k + 2) % 3] != i {
                continue;
            }
            let u = self.n[t][k];
            if u < 0 {
                continue;
            }
            let u = u as usize;
            let Some(ku) = (0..3).find(|&m| self.v[u][m] == b && self.v[u][(m + 1) % 3] == a) else { continue };
            let d = self.v[u][(ku + 2) % 3];
            let (pp, pa, pb, pd) = (self.p[i], self.p[a], self.p[b], self.p[d]);
            if incircle(pa, pb, pp, pd) <= 1e-9 || orient(pp, pa, pd) <= TIN_EPS || orient(pp, pd, pb) <= TIN_EPS {
                continue;
            }
            let (nbp, npa) = (self.n[t][(k + 1) % 3], self.n[t][(k + 2) % 3]);
            let (nad, ndb) = (self.n[u][(ku + 1) % 3], self.n[u][(ku + 2) % 3]);
            self.v[t] = [i, a, d];
            self.n[t] = [npa, nad, u as i64];
            self.v[u] = [i, d, b];
            self.n[u] = [t as i64, ndb, nbp];
            self.relink(nad, a, d, t);
            self.relink(nbp, b, i, u);
            self.share(&[t, u], &[t, u], cz);
            changed.extend([t, u]);
            stack.push((t, 1));
            stack.push((u, 1));
        }
    }
}

/// The fine cells (n, 2) [j, i] (fine_cells) as an adaptive triangulation instead of a grid (the user, 2026-11-01:
/// "the finer mesh should be adaptive. increase resolution when needed or not be force to allign to a grid": the 1 m
/// grid drew a road's edge and a hairpin's seam across it as a staircase). z (n, (k + 1)²): each cell's heights every 1 / k
/// (fine_points with k = 4); u (ny, nx): the lattice (the terrain's samples); mask (ny - 1, nx - 1): the fine cells.
/// Per piece (`per` cells square, from the block's corner): the cells' lattice triangles, then the point of most error
/// put in, Delaunay again (Lawson's flips), until every point is within tol or the piece holds max_tris. A side the
/// terrain or a rim draws stays its straight edge; a side shared with another piece's cell is split where its own
/// heights need it (alike from both sides: no crack). Returns (positions (m, 3), triangles (t, 3), each triangle's
/// cell (row of cells) and piece (pj * 100000 + pi)).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn fine_tin<'py>(py: Python<'py>, x0: f64, y0: f64, square: f64, cells: PyReadonlyArray2<'py, i64>,
                     z: PyReadonlyArray2<'py, f64>, u: PyReadonlyArray2<'py, f64>, mask: PyReadonlyArray2<'py, u8>,
                     per: i64, tol: f64, max_tris: i64)
                     -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, numpy::PyArray1<i64>>,
                         Bound<'py, numpy::PyArray1<i64>>) {
    use std::collections::{BinaryHeap, HashMap};
    let (c, z, u, mask) = (cells.as_array(), z.as_array(), u.as_array(), mask.as_array());
    let w_ = (z.ncols() as f64).sqrt().round() as usize;       // (each cell's (k + 1)² heights)
    let (k_, wv) = (w_.max(2) - 1, w_.max(2));
    #[allow(non_snake_case)]
    let (K, W) = (k_, wv);
    let per = per.max(1);
    let (cy, cx) = (mask.nrows() as i64, mask.ncols() as i64);
    let fine = |j: i64, i: i64| j >= 0 && j < cy && i >= 0 && i < cx && mask[[j as usize, i as usize]] != 0;
    let piece_of = |j: i64, i: i64| (j.div_euclid(per), i.div_euclid(per));
    let mut row_of: HashMap<(i64, i64), usize> = HashMap::new();
    let mut by_piece: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for r in 0..c.nrows() {
        let (j, i) = (c[[r, 0]], c[[r, 1]]);
        row_of.insert((j, i), r);
        by_piece.entry(piece_of(j, i)).or_default().push(r);
    }
    let mut keys: Vec<(i64, i64)> = by_piece.keys().copied().collect();
    keys.sort();
    let pt = |j: i64, i: i64, a: usize, b: usize, h: f64| -> [f64; 3] {
        [x0 + (i as f64 + b as f64 / K as f64) * square, y0 + (j as f64 + a as f64 / K as f64) * square, h]
    };
    let (mut pos, mut tri, mut tcell, mut tpiece) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for key in keys {
        let rows = &by_piece[&key];
        let mut tin = Tin { p: Vec::new(), v: Vec::new(), n: Vec::new(), cand: Vec::new(), ver: Vec::new() };
        // vertices on the sub-lattice (0.5 m: 4 per cell) by key; the cells' lattice triangles
        let mut vid: HashMap<(i64, i64), usize> = HashMap::new();
        let mut vert = |tin: &mut Tin, j: i64, i: i64, a: usize, b: usize, h: f64| -> usize {
            let kk = (j * K as i64 + a as i64, i * K as i64 + b as i64);
            *vid.entry(kk).or_insert_with(|| { tin.p.push(pt(j, i, a, b, h)); tin.p.len() - 1 })
        };
        let mut edge: HashMap<(usize, usize), (usize, usize)> = HashMap::new();
        for &r in rows {
            let (j, i) = (c[[r, 0]], c[[r, 1]]);
            let (ju, iu) = (j as usize, i as usize);
            // (a corner on the outline (a cell round it not fine) the terrain's sample, to meet its edges; one inside
            // the patch the cells' own (filtered) height: the samples there kept a sliver's height as cones 2 m apart,
            // 2026-11-01)
            let inner = |a: i64, b: i64| fine(a - 1, b - 1) && fine(a - 1, b) && fine(a, b - 1) && fine(a, b);
            let corner = |a: usize, b: usize, uu: f64| if inner(j + a as i64 / K as i64, i + b as i64 / K as i64) {
                z[[r, a * W + b]] } else { uu };
            let sw = vert(&mut tin, j, i, 0, 0, corner(0, 0, u[[ju, iu]]));
            let se = vert(&mut tin, j, i, 0, K, corner(0, K, u[[ju, iu + 1]]));
            let ne = vert(&mut tin, j, i, K, K, corner(K, K, u[[ju + 1, iu + 1]]));
            let nw = vert(&mut tin, j, i, K, 0, corner(K, 0, u[[ju + 1, iu]]));
            for v in [[sw, se, ne], [sw, ne, nw]] {
                let t = tin.new_tri(v, [-1, -1, -1]);
                for k in 0..3 {
                    let (a, b) = (v[k], v[(k + 1) % 3]);
                    if let Some(&(t2, k2)) = edge.get(&(b, a)) {
                        tin.n[t][k] = t2 as i64;
                        tin.n[t2][k2] = t as i64;
                    } else {
                        edge.insert((a, b), (t, k));
                    }
                }
            }
        }
        // a side shared with another piece's fine cell: split where its own heights leave its straight line by tol
        // (the most first, halves on: alike from both sides)
        let zc = |r: usize, a: usize, b: usize| z[[r, a * W + b]];
        for &r in rows {
            let (j, i) = (c[[r, 0]], c[[r, 1]]);
            // (sides: south (a = 0), east (b = K), north (a = K), west (b = 0); their neighbour cells)
            let sides: [((i64, i64), Vec<(usize, usize)>); 4] = [
                ((j - 1, i), (0..W).map(|s| (0, s)).collect()),
                ((j, i + 1), (0..W).map(|s| (s, K)).collect()),
                ((j + 1, i), (0..W).map(|s| (K, K - s)).collect()),
                ((j, i - 1), (0..W).map(|s| (K - s, 0)).collect()),
            ];
            for (nb, pts_) in sides {
                if !fine(nb.0, nb.1) || piece_of(nb.0, nb.1) == key || !row_of.contains_key(&nb) {
                    continue;
                }
                // (the side's sub-points in the order of the side's lattice-line coordinate: alike from both cells)
                let mut sp: Vec<[f64; 3]> = pts_.iter().map(|&(a, b)| pt(j, i, a, b, zc(r, a, b))).collect();
                sp.sort_by(|p, q| (p[0] + p[1]).total_cmp(&(q[0] + q[1])));
                let mut keep = vec![false; W];
                keep[0] = true;
                keep[W - 1] = true;
                let mut segs = vec![(0usize, W - 1)];
                while let Some((s0, s1)) = segs.pop() {
                    let mut best = (tol, None);
                    for m in s0 + 1..s1 {
                        let f = (m - s0) as f64 / (s1 - s0) as f64;
                        let e = (sp[m][2] - (sp[s0][2] + (sp[s1][2] - sp[s0][2]) * f)).abs();
                        if e > best.0 {
                            best = (e, Some(m));
                        }
                    }
                    if let Some(m) = best.1 {
                        keep[m] = true;
                        segs.push((s0, m));
                        segs.push((m, s1));
                    }
                }
                for m in 1..W - 1 {
                    if !keep[m] {
                        continue;
                    }
                    let q = sp[m];
                    let a = ((q[1] - y0) / square * K as f64).round() as i64 - j * K as i64;
                    let b = ((q[0] - x0) / square * K as f64).round() as i64 - i * K as i64;
                    let id = vert(&mut tin, j, i, a as usize, b as usize, q[2]);
                    if let Some(t) = (0..tin.v.len()).find(|&t| tin.n[t].contains(&-1) && tin.inside(t, q)) {
                        tin.insert(t, id, &[]);
                    }
                }
            }
        }
        // the candidates: every cell's sub-points not on a side the terrain, a rim or another piece draws
        let mut cz: Vec<[f64; 3]> = Vec::new();
        let mut seen: HashMap<(i64, i64), ()> = HashMap::new();
        for &r in rows {
            let (j, i) = (c[[r, 0]], c[[r, 1]]);
            for a in 0..W {
                for b in 0..W {
                    if (a == 0 || a == K) && (b == 0 || b == K) {
                        continue;       // (the lattice's own samples)
                    }
                    let side_nb = if a == 0 { Some((j - 1, i)) } else if a == K { Some((j + 1, i)) }
                                  else if b == 0 { Some((j, i - 1)) } else if b == K { Some((j, i + 1)) } else { None };
                    if let Some(nb) = side_nb {
                        if !fine(nb.0, nb.1) || piece_of(nb.0, nb.1) != key || !row_of.contains_key(&nb) {
                            continue;   // (a side drawn straight, or split above)
                        }
                    }
                    let kk = (j * K as i64 + a as i64, i * K as i64 + b as i64);
                    if vid.contains_key(&kk) || seen.insert(kk, ()).is_some() {
                        continue;
                    }
                    cz.push(pt(j, i, a, b, zc(r, a, b)));
                }
            }
        }
        // share them out to the triangles they lie in (a cell's two, by the diagonal)
        let ntri0 = tin.v.len();
        let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
        for t in 0..ntri0 {
            let [a, b, cc] = tin.v[t];
            let mx = (tin.p[a][0] + tin.p[b][0] + tin.p[cc][0]) / 3.0;
            let my = (tin.p[a][1] + tin.p[b][1] + tin.p[cc][1]) / 3.0;
            grid.entry((((my - y0) / square).floor() as i64, ((mx - x0) / square).floor() as i64)).or_default().push(t);
        }
        for (k, q) in cz.iter().enumerate() {
            let (gj, gi) = (((q[1] - y0) / square).floor() as i64, ((q[0] - x0) / square).floor() as i64);
            let mut placed = false;
            'f: for dj in -1..=1 {
                for di in -1..=1 {
                    if let Some(ts) = grid.get(&(gj + dj, gi + di)) {
                        for &t in ts {
                            if tin.inside(t, *q) {
                                tin.cand[t].push(k as u32);
                                placed = true;
                                break 'f;
                            }
                        }
                    }
                }
            }
            let _ = placed;
        }
        // the point of most error first, until within tol
        let mut heap = BinaryHeap::new();
        for t in 0..tin.v.len() {
            if let Some((e, _)) = tin.worst(t, &cz) {
                heap.push(Pending(e, t, tin.ver[t]));
            }
        }
        let budget = max_tris.max(1) as usize;
        while let Some(Pending(e, t, ver)) = heap.pop() {
            if e < tol || tin.v.len() + 2 > budget {
                break;
            }
            if ver != tin.ver[t] {
                continue;
            }
            let Some((_, k)) = tin.worst(t, &cz) else { continue };
            let ci = tin.cand[t].swap_remove(k) as usize;
            tin.p.push(cz[ci]);
            let id = tin.p.len() - 1;
            let mut changed = tin.insert(t, id, &cz);
            changed.sort();
            changed.dedup();
            for tt in changed {
                tin.ver[tt] = tin.ver[tt].wrapping_add(1);
                if let Some((e2, _)) = tin.worst(tt, &cz) {
                    heap.push(Pending(e2, tt, tin.ver[tt]));
                }
            }
        }
        let base = (pos.len() / 3) as i64;
        for q in &tin.p {
            pos.extend_from_slice(q);
        }
        for t in 0..tin.v.len() {
            let [a, b, cc] = tin.v[t];
            tri.extend([base + a as i64, base + b as i64, base + cc as i64]);
            let mx = (tin.p[a][0] + tin.p[b][0] + tin.p[cc][0]) / 3.0;
            let my = (tin.p[a][1] + tin.p[b][1] + tin.p[cc][1]) / 3.0;
            let cell = (((my - y0) / square).floor() as i64, ((mx - x0) / square).floor() as i64);
            tcell.push(row_of.get(&cell).map(|&r| r as i64).unwrap_or(rows[0] as i64));
            tpiece.push(key.0 * 100000 + key.1);
        }
    }
    let (nv, nt) = (pos.len() / 3, tri.len() / 3);
    (numpy::ndarray::Array2::from_shape_vec((nv, 3), pos).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((nt, 3), tri).unwrap().into_pyarray(py),
     tcell.into_pyarray(py), tpiece.into_pyarray(py))
}

/// The rim cells' covers (rim cells (n, 2) [j, i], fine_rim; u (ny, nx) the terrain's stored heights): each cell four
/// triangles round its centre, its corners the samples and its centre the higher of its diagonals' middles, all lift up.
/// Each triangle lies within one triangle of the cell however the terrain splits it (south-west to north-east or
/// north-west to south-east) and the cover is the corners' heights' upper hull: over the terrain drawn either way (a
/// cover on the one split stood under the other: white half squares, the user's report 2026-11-01). Its sides are the
/// terrain's straight edges, lift up, as the fine mesh's outline. Returns (positions (5n, 3), triangles (4n, 3)).
#[pyfunction]
pub fn fine_rim_mesh<'py>(py: Python<'py>, x0: f64, y0: f64, square: f64, cells: PyReadonlyArray2<'py, i64>,
                          u: PyReadonlyArray2<'py, f64>, lift: f64, centre: i64)
                          -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>) {
    let (c, u) = (cells.as_array(), u.as_array());
    let n = c.nrows();
    let mut pos = Vec::with_capacity(n * 15);
    let mut tri = Vec::with_capacity(n * 12);
    for r in 0..n {
        let (j, i) = (c[[r, 0]], c[[r, 1]]);
        let (ju, iu) = (j as usize, i as usize);
        let (a, b, cc, d) = (u[[ju, iu]], u[[ju, iu + 1]], u[[ju + 1, iu]], u[[ju + 1, iu + 1]]);   // sw se nw ne
        let (x, y) = (x0 + i as f64 * square, y0 + j as f64 * square);
        // (centre: 0 the higher diagonal's middle, 1 the south-west to north-east one's, 2 the other's, 3 the mean)
        let mid = match centre {
            1 => (a + d) * 0.5,
            2 => (b + cc) * 0.5,
            3 => (a + b + cc + d) * 0.25,
            _ => ((a + d) * 0.5).max((b + cc) * 0.5),
        };
        for (px, py, h) in [(x, y, a), (x + square, y, b), (x + square, y + square, d), (x, y + square, cc),
                            (x + 0.5 * square, y + 0.5 * square, mid)] {
            pos.extend_from_slice(&[px, py, h + lift]);
        }
        let s = (r * 5) as i64;
        // (sw se ne nw: 0 1 2 3, centre 4; counter-clockwise from above)
        tri.extend_from_slice(&[s, s + 1, s + 4, s + 1, s + 2, s + 4, s + 2, s + 3, s + 4, s + 3, s, s + 4]);
    }
    (numpy::ndarray::Array2::from_shape_vec((n * 5, 3), pos).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((n * 4, 3), tri).unwrap().into_pyarray(py))
}

/// One flat min (or max) pass of a square window 2r + 1 over a raster (w x h, NaN: no ground), rows then columns.
fn morph(a: &[f32], w: usize, h: usize, r: usize, max: bool) -> Vec<f32> {
    let pick = |x: f32, y: f32| -> f32 {
        if x.is_nan() { y } else if y.is_nan() { x } else if max { x.max(y) } else { x.min(y) }
    };
    let mut t = vec![f32::NAN; w * h];
    for j in 0..h {
        for i in 0..w {
            let mut v = f32::NAN;
            for b in i.saturating_sub(r)..(i + r + 1).min(w) {
                v = pick(v, a[j * w + b]);
            }
            t[j * w + i] = v;
        }
    }
    let mut o = vec![f32::NAN; w * h];
    for j in 0..h {
        for i in 0..w {
            if a[j * w + i].is_nan() {
                continue;
            }
            let mut v = f32::NAN;
            for c in j.saturating_sub(r)..(j + r + 1).min(h) {
                v = pick(v, t[c * w + i]);
            }
            o[j * w + i] = v;
        }
    }
    o
}

/// The fine cells' heights (z (n, (k + 1)²), fine_points' layout) without ridges and notches narrower than a window
/// of 2 radius + 1 samples: an opening (min, then max) then a closing (max, then min) with a flat square window over
/// the cells' points. A flat road, a plane, a crest or a wall's step wider than the window comes through as it is;
/// a narrower one is levelled: grading's slivers between two cores (the natural ground standing up as a 1.4 m fin
/// between a hairpin's legs) and the zigzag of a step's line, which the terrain's 2 m lattice never drew and the fine
/// mesh did, as spikes (the user, 2026-11-01). Beside the cells the windows see the terrain's own ground (u, the
/// block's samples, bilinear); a point whose windows reach past the block is left as it is.
#[pyfunction]
pub fn fine_filter<'py>(py: Python<'py>, cells: PyReadonlyArray2<'py, i64>, z: PyReadonlyArray2<'py, f64>,
                        radius: i64, u: PyReadonlyArray2<'py, f64>) -> Bound<'py, PyArray2<f64>> {
    let (c, z, u) = (cells.as_array(), z.as_array(), u.as_array());
    let (uy, ux) = (u.nrows() as i64, u.ncols() as i64);
    let n = c.nrows();
    let wv = ((z.ncols() as f64).sqrt().round() as usize).max(2);
    let k = wv - 1;
    let r = radius.max(0) as usize;
    let mut out = z.to_owned();
    if n == 0 || r == 0 {
        return out.into_pyarray(py);
    }
    let (mut j0, mut i0, mut j1, mut i1) = (i64::MAX, i64::MAX, i64::MIN, i64::MIN);
    for row in 0..n {
        j0 = j0.min(c[[row, 0]]); j1 = j1.max(c[[row, 0]]);
        i0 = i0.min(c[[row, 1]]); i1 = i1.max(c[[row, 1]]);
    }
    // (in tiles of TILE cells, each with the four passes' reach of margin)
    const TILE: i64 = 32;
    let m = (4 * r).div_ceil(k) as i64 + 1;
    let mut by_tile: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
    for row in 0..n {
        by_tile.entry(((c[[row, 0]] - j0).div_euclid(TILE), (c[[row, 1]] - i0).div_euclid(TILE))).or_default().push(row);
    }
    let mut index: std::collections::HashMap<(i64, i64), usize> = std::collections::HashMap::new();
    for row in 0..n {
        index.insert((c[[row, 0]], c[[row, 1]]), row);
    }
    for (&(tj, ti), rows) in by_tile.iter() {
        let (cj0, ci0) = (j0 + tj * TILE - m, i0 + ti * TILE - m);
        let cells_side = (TILE + 2 * m) as usize;
        let w = cells_side * k + 1;
        let mut a = vec![f32::NAN; w * w];
        for cj in 0..cells_side as i64 {
            for ci in 0..cells_side as i64 {
                let (gj, gi) = (cj0 + cj, ci0 + ci);
                if !index.contains_key(&(gj, gi)) && gj >= 0 && gi >= 0 && gj + 1 < uy && gi + 1 < ux {
                    // (beside the cells: the terrain's own ground, its samples bilinear, so a window by the patch's
                    // edge sees ground all round)
                    let (gju, giu) = (gj as usize, gi as usize);
                    let (u00, u10, u01, u11) = (u[[gju, giu]], u[[gju, giu + 1]], u[[gju + 1, giu]], u[[gju + 1, giu + 1]]);
                    for aa in 0..wv {
                        for bb in 0..wv {
                            let (fy, fx) = (aa as f64 / k as f64, bb as f64 / k as f64);
                            let v = (u00 * (1.0 - fx) + u10 * fx) * (1.0 - fy) + (u01 * (1.0 - fx) + u11 * fx) * fy;
                            let q = (cj as usize * k + aa) * w + ci as usize * k + bb;
                            if a[q].is_nan() {
                                a[q] = v as f32;
                            }
                        }
                    }
                }
                if let Some(&row) = index.get(&(gj, gi)) {
                    for aa in 0..wv {
                        for bb in 0..wv {
                            a[(cj as usize * k + aa) * w + ci as usize * k + bb] = z[[row, aa * wv + bb]] as f32;
                        }
                    }
                }
            }
        }
        let opened = morph(&morph(&a, w, w, r, false), w, w, r, true);
        let closed = morph(&morph(&opened, w, w, r, true), w, w, r, false);
        // (only where the four passes saw ground all round: by the block's edge a window cut short raised or lowered
        // a slope by up to its grade x the window: steps, 2026-11-01)
        let have: Vec<f32> = a.iter().map(|v| if v.is_nan() { 0.0 } else { 1.0 }).collect();
        let mut full = have.clone();
        for _ in 0..4 {
            full = morph(&full, w, w, r, false);
        }
        for &row in rows {
            let (cj, ci) = ((c[[row, 0]] - cj0) as usize, (c[[row, 1]] - ci0) as usize);
            for aa in 0..wv {
                for bb in 0..wv {
                    let q = (cj * k + aa) * w + ci * k + bb;
                    let v = closed[q];
                    let z0 = z[[row, aa * wv + bb]];
                    if full[q] > 0.5 && !v.is_nan() && (v - z0 as f32).abs() > 1e-3 {     // (the rest at full precision)
                        out[[row, aa * wv + bb]] = v as f64;
                    }
                }
            }
        }
    }
    out.into_pyarray(py)
}
