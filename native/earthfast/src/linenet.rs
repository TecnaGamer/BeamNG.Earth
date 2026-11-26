//! The line network (641): every line the solve reads (roads on the ground, decks, tunnels, tracks: no difference
//! here) in one index, kept as lines are added. A grid over the lines' segments (not their boxes: a long road's box
//! holds a district), and each pair of lines that come near is found once, when the later of the two is added: what
//! is near a line is then a lookup, not a scan. The geometry of a near pair (where the two cross, where an end of one
//! lies on the other) is gradeshapes._pair_relations' own, to the bit: _seg_hits (numpy's elementwise arithmetic on
//! the segment pairs whose boxes overlap, in row-major order), proj (the first nearest segment, libm's hypot) and
//! arclen (numpy's cumsum of the segments' hypot). What a contact means (a joint or a crossing, which is over) is the
//! Python's, from these numbers.
use crate::dm::Dm;

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2};
use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;

type P = [f64; 2];

/// A segment's box covering more grid cells than this is kept in a list every query reads (a long straight line).
const LONG_CELLS: i64 = 256;

struct Line {
    p: Vec<P>,
    s: Vec<f64>,
}

/// A near pair's geometry: the crossings [(s on i, s on j, x, y)] away from both lines' ends, and the ends of either
/// line on the other [(the end is j's, s of the end, s on the other line, x, y)].
type Hits = Vec<(f64, f64, f64, f64)>;
type Ends = Vec<(bool, f64, f64, f64, f64)>;

#[pyclass(module = "earthfast")]
pub struct LineNet {
    side: f64,
    pad: f64,
    lines: Vec<Line>,
    grid: HashMap<(i64, i64), Vec<(u32, u32)>>,
    long: Vec<(u32, u32)>,
    near: Vec<Vec<u32>>,
    pairs: usize,
}

/// arclen: numpy's cumsum of the segments' lengths (libm's hypot of numpy's diff).
fn arclen(p: &[P]) -> Vec<f64> {
    let mut s = Vec::with_capacity(p.len().max(1));
    s.push(0.0);
    let mut acc = 0.0f64;
    for w in p.windows(2) {
        acc += (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1]);
        s.push(acc);
    }
    s
}

fn seg_box(a: P, b: P) -> [f64; 4] {
    [a[0].min(b[0]), a[1].min(b[1]), a[0].max(b[0]), a[1].max(b[1])]
}

/// gradeshapes.proj: (s along the line of its point nearest q, the distance), the first nearest segment's.
fn proj(p: &[P], s: &[f64], q: P) -> (f64, f64) {
    let mut best = (f64::INFINITY, 0usize, 0.0f64);
    for k in 0..p.len() - 1 {
        let (a, b) = (p[k], p[k + 1]);
        let (abx, aby) = (b[0] - a[0], b[1] - a[1]);
        let l2 = (abx * abx + aby * aby).max(1e-12);
        let t = (((q[0] - a[0]) * abx + (q[1] - a[1]) * aby) / l2).clamp(0.0, 1.0);
        let d = (a[0] + t * abx - q[0]).dhypot(a[1] + t * aby - q[1]);
        if k == 0 || d < best.0 {
            best = (d, k, t);
        }
    }
    let (d, k, t) = best;
    (s[k] + t * (s[k + 1] - s[k]), d)
}

/// gradeshapes._seg_hits with _pair_relations' arc lengths and its CROSS_END_M test: [(s on a, s on b, x, y)].
fn seg_hits(a: &Line, b: &Line, cross_end: f64) -> Hits {
    let mut out = Vec::new();
    let (la, lb) = (a.s[a.s.len() - 1], b.s[b.s.len() - 1]);
    for i in 0..a.p.len().saturating_sub(1) {
        let (p0, p1) = (a.p[i], a.p[i + 1]);
        let ba = seg_box(p0, p1);
        let (rx, ry) = (p1[0] - p0[0], p1[1] - p0[1]);
        for j in 0..b.p.len().saturating_sub(1) {
            let (q0, q1) = (b.p[j], b.p[j + 1]);
            let bb = seg_box(q0, q1);
            if !(ba[0] <= bb[2] && ba[1] <= bb[3] && ba[2] >= bb[0] && ba[3] >= bb[1]) {
                continue;
            }
            let (sx, sy) = (q1[0] - q0[0], q1[1] - q0[1]);
            let (dx, dy) = (q0[0] - p0[0], q0[1] - p0[1]);
            let den = rx * sy - ry * sx;
            if den.abs() <= 1e-12 || den.is_nan() {
                continue;
            }
            let ta = (dx * sy - dy * sx) / den;
            let tb = (dx * ry - dy * rx) / den;
            if !(ta >= 0.0 && ta <= 1.0 && tb >= 0.0 && tb <= 1.0) {
                continue;
            }
            let si = a.s[i] + ta * (a.s[i + 1] - a.s[i]);
            let sj = b.s[j] + tb * (b.s[j + 1] - b.s[j]);
            // (Python's min(si, L - si, sj, L - sj) >= CROSS_END_M)
            let m = si.min(la - si).min(sj).min(lb - sj);
            if m >= cross_end {
                out.push((si, sj, p0[0] + ta * rx, p0[1] + ta * ry));
            }
        }
    }
    out
}

/// _pair_relations' ends: each end of i on j's line, then each end of j on i's, within join_m.
fn ends(a: &Line, b: &Line, join: f64) -> Ends {
    let mut out = Vec::new();
    for (e, o, swap) in [(a, b, false), (b, a, true)] {
        let le = e.s[e.s.len() - 1];
        for se in [0.0, le] {
            let q = if se == 0.0 { e.p[0] } else { e.p[e.p.len() - 1] };
            let (so, d) = proj(&o.p, &o.s, q);
            if d <= join {
                out.push((swap, se, so, q[0], q[1]));
            }
        }
    }
    out
}

impl LineNet {
    fn cells(&self, b: [f64; 4], pad: f64) -> (i64, i64, i64, i64) {
        let f = |v: f64| (v / self.side).floor() as i64;
        (f(b[0] - pad), f(b[1] - pad), f(b[2] + pad), f(b[3] + pad))
    }

    fn line(&self, i: usize) -> PyResult<&Line> {
        self.lines.get(i).ok_or_else(|| PyIndexError::new_err(format!("LineNet: no line {i}")))
    }

    fn geometry(&self, i: usize, j: usize, cross_end: f64, join: f64) -> (Hits, Ends) {
        let (a, b) = (&self.lines[i], &self.lines[j]);
        if a.p.len() < 2 || b.p.len() < 2 {
            return (Vec::new(), Vec::new());
        }
        (seg_hits(a, b, cross_end), ends(a, b, join))
    }

    /// Lines new..: their arc lengths and segments into the grid, each against every line already there (and the new
    /// ones before it): a pair is near when a segment of each comes within pad of the other's, box to box.
    fn insert(&mut self, new: Vec<Vec<P>>) {
        let pad = self.pad;
        let n0 = self.lines.len();
        let mut stamp = vec![usize::MAX; n0 + new.len()];
        for (k, p) in new.into_iter().enumerate() {
            let i = n0 + k;
            let s = arclen(&p);
            self.lines.push(Line { p, s });
            self.near.push(Vec::new());
            let segs: Vec<[f64; 4]> = self.lines[i].p.windows(2).map(|w| seg_box(w[0], w[1])).collect();
            let mut found: Vec<u32> = Vec::new();
            for b in &segs {
                let (x0, y0, x1, y1) = self.cells(*b, pad);
                let mut look = |c: &(u32, u32)| {
                    let (j, sg) = (c.0 as usize, c.1 as usize);
                    if stamp[j] == i {
                        return;
                    }
                    let w = &self.lines[j].p;
                    let o = seg_box(w[sg], w[sg + 1]);
                    if o[0] <= b[2] + pad && o[2] >= b[0] - pad && o[1] <= b[3] + pad && o[3] >= b[1] - pad {
                        stamp[j] = i;
                        found.push(j as u32);
                    }
                };
                if (x1 - x0 + 1) * (y1 - y0 + 1) > LONG_CELLS * 4 {
                    for list in self.grid.values() {
                        list.iter().for_each(&mut look);
                    }
                } else {
                    for x in x0..=x1 {
                        for y in y0..=y1 {
                            if let Some(list) = self.grid.get(&(x, y)) {
                                list.iter().for_each(&mut look);
                            }
                        }
                    }
                }
                self.long.iter().for_each(&mut look);
            }
            for &j in &found {
                self.near[j as usize].push(i as u32);
            }
            self.pairs += found.len();
            found.sort_unstable();
            self.near[i] = found;
            for (sg, b) in segs.iter().enumerate() {
                let (x0, y0, x1, y1) = self.cells(*b, 0.0);
                let c = (i as u32, sg as u32);
                if (x1 - x0 + 1) * (y1 - y0 + 1) > LONG_CELLS {
                    self.long.push(c);
                    continue;
                }
                for x in x0..=x1 {
                    for y in y0..=y1 {
                        self.grid.entry((x, y)).or_default().push(c);
                    }
                }
            }
        }
        // (a line added later than one already near it was pushed at its end: those lists stay ascending)
    }
}

#[pymethods]
impl LineNet {
    /// pad: m within which two lines' segments make them a near pair (gradeshapes: JOIN_M + 0.5); side: the grid's
    /// cell, m.
    #[new]
    #[pyo3(signature = (pad, side = 32.0))]
    fn new(pad: f64, side: f64) -> PyResult<Self> {
        if !(side > 0.0 && pad >= 0.0) {
            return Err(PyValueError::new_err("LineNet: side > 0 and pad >= 0"));
        }
        Ok(LineNet { side, pad, lines: Vec::new(), grid: HashMap::new(), long: Vec::new(), near: Vec::new(), pairs: 0 })
    }

    /// Add lines [(k, 2) or wider: x, y used]; returns the first one's number (they are numbered in order).
    fn add(&mut self, py: Python<'_>, lines: Vec<PyReadonlyArray2<'_, f64>>) -> PyResult<usize> {
        let mut new = Vec::with_capacity(lines.len());
        for a in &lines {
            let a = a.as_array();
            if a.nrows() > 0 && a.ncols() < 2 {
                return Err(PyValueError::new_err("LineNet.add: a line is (k, 2)"));
            }
            new.push((0..a.nrows()).map(|r| [a[[r, 0]], a[[r, 1]]]).collect::<Vec<P>>());
        }
        let n0 = self.lines.len();
        py.detach(|| self.insert(new));
        Ok(n0)
    }

    fn __len__(&self) -> usize {
        self.lines.len()
    }

    /// The near pairs found so far (each once).
    #[getter]
    fn pairs(&self) -> usize {
        self.pairs
    }

    /// Line i's arc lengths (gradeshapes.arclen).
    fn arclen<'py>(&self, py: Python<'py>, i: usize) -> PyResult<Bound<'py, PyArray1<f64>>> {
        Ok(self.line(i)?.s.clone().into_pyarray(py))
    }

    /// The lines near line i, ascending.
    fn near(&self, i: usize) -> PyResult<Vec<u32>> {
        self.line(i)?;
        let mut v = self.near[i].clone();
        v.sort_unstable();
        Ok(v)
    }

    /// The geometry of lines i and j (gradeshapes._pair_geometry): (crossings [(s on i, s on j, x, y)], ends
    /// [(j's end, s of the end, s on the other line, x, y)]), cross_end: CROSS_END_M, join: JOIN_M.
    fn pair(&self, i: usize, j: usize, cross_end: f64, join: f64) -> PyResult<(Hits, Ends)> {
        self.line(i)?;
        self.line(j)?;
        Ok(self.geometry(i, j, cross_end, join))
    }

    /// The geometry of line i with each line near it that it touches: [(j, crossings, ends)], j ascending, the pairs
    /// with neither left out.
    fn contacts(&self, py: Python<'_>, i: usize, cross_end: f64, join: f64) -> PyResult<Vec<(u32, Hits, Ends)>> {
        self.line(i)?;
        let mut js = self.near[i].clone();
        js.sort_unstable();
        Ok(py.detach(|| {
            js.into_iter()
                .filter_map(|j| {
                    let (h, e) = self.geometry(i, j as usize, cross_end, join);
                    if h.is_empty() && e.is_empty() { None } else { Some((j, h, e)) }
                })
                .collect()
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn net(lines: Vec<Vec<P>>) -> LineNet {
        let mut n = LineNet { side: 32.0, pad: 3.0, lines: Vec::new(), grid: HashMap::new(), long: Vec::new(),
                              near: Vec::new(), pairs: 0 };
        n.insert(lines);
        n
    }

    #[test]
    fn a_crossing_and_a_joint_are_found_once_each() {
        let n = net(vec![
            vec![[-100.0, 0.0], [100.0, 0.0]],
            vec![[0.0, -50.0], [0.0, 50.0]],
            vec![[0.0, 50.0], [0.0, 200.0]],
            vec![[500.0, 500.0], [600.0, 500.0]],
        ]);
        assert_eq!(n.pairs, 2);
        assert_eq!(n.near[1], vec![0, 2]);
        assert!(n.near[3].is_empty());
        let (h, e) = n.geometry(0, 1, 2.0, 2.5);
        assert_eq!(h, vec![(100.0, 50.0, 0.0, 0.0)]);
        assert!(e.is_empty());
        let (h, e) = n.geometry(1, 2, 2.0, 2.5);
        assert!(h.is_empty());
        assert_eq!(e, vec![(false, 100.0, 0.0, 0.0, 50.0), (true, 0.0, 100.0, 0.0, 50.0)]);
    }

    #[test]
    fn a_long_diagonal_segment_is_near_what_it_passes() {
        let n = net(vec![vec![[0.0, 0.0], [20000.0, 20000.0]], vec![[10000.0, 10001.0], [10000.0, 10050.0]]]);
        assert_eq!(n.near[0], vec![1]);
    }
}
