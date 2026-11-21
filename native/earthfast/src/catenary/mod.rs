//! Overhead line supports in track order. Each part keeps its own arclength origin.
//! Ground is sampled at individual support points, including portals rejected by the merge test.
mod input;
mod wires;
use crate::busstation::{array, points, Point};
use crate::dm::Dm;
use blake2::{digest::consts::U4, Blake2s, Digest};
use numpy::{IntoPyArray, PyReadonlyArray2};
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyDict, PyList},
};

const SHIFTS: [f64; 11] = [0., 3., -3., 6., -6., 9., -9., 12., -12., 15., -15.];
type Line = Vec<Point>;
type XY<'py> = PyReadonlyArray2<'py, f64>;

struct Track {
    id: String,
    xy: Line,
    arc: Vec<f64>,
}

fn phase(id: &str, salt: &str) -> f64 {
    let bytes = Blake2s::<U4>::digest(format!("{id}/{salt}"));
    u32::from_le_bytes(bytes.into()) as f64 / 4294967296.
}

#[derive(Clone, Copy)]
struct Site {
    s: f64,
    p: Point,
    u: Point,
}

impl Track {
    fn new(id: String, xy: Line) -> PyResult<Self> {
        if xy.len() < 2 {
            return Err(PyValueError::new_err("a catenary track needs two points"));
        }
        let mut arc = vec![0.];
        for w in xy.windows(2) {
            arc.push(arc.last().unwrap() + (w[1] - w[0]).norm());
        }
        Ok(Self { id, xy, arc })
    }
    fn length(&self) -> f64 {
        *self.arc.last().unwrap()
    }
    fn frame(&self, s: f64) -> Site {
        let s = s.clamp(0., self.length());
        let j = self
            .arc
            .partition_point(|&v| v < s)
            .saturating_sub(1)
            .min(self.xy.len() - 2);
        let d = self.xy[j + 1] - self.xy[j];
        let n = d.norm().max(1e-9);
        Site {
            s,
            p: self.xy[j] + d * ((s - self.arc[j]) / n),
            u: d / n,
        }
    }
    fn stations(&self) -> Vec<f64> {
        let mut out = Vec::new();
        let mut s = phase(&self.id, "cat") * 60.;
        while s < self.length() {
            out.push(s);
            s += self.span(s);
        }
        out
    }
    fn span(&self, s: f64) -> f64 {
        let a = self.frame(s - 20.).u;
        let b = self.frame(s + 20.).u;
        let ang = (a.0 * b.1 - a.1 * b.0).datan2(a.dot(b)).abs();
        if ang < 1e-4 {
            62.
        } else {
            (8. * (40. / ang) * 0.4).sqrt().clamp(30., 60.)
        }
    }
    fn nearest(&self, p: Point) -> (f64, Site) {
        let mut best = f64::INFINITY;
        let mut site = self.frame(0.);
        for (i, w) in self.xy.windows(2).enumerate() {
            let d = w[1] - w[0];
            let sq = d.dot(d).max(1e-12);
            let t = ((p - w[0]).dot(d) / sq).clamp(0., 1.);
            let q = w[0] + d * t;
            let dist = (q - p).norm();
            if dist < best {
                let len = sq.sqrt();
                best = dist;
                site = Site {
                    s: self.arc[i] + t * len,
                    p: q,
                    u: d / len,
                };
            }
        }
        (best, site)
    }
    fn supports(&self, mapped: &[Point]) -> Vec<(f64, f64, f64)> {
        let mut hits = Vec::new();
        for &p in mapped {
            let (dist, site) = self.nearest(p);
            if dist <= 8. {
                let v = p - site.p;
                let side = if site.u.0 * v.1 - site.u.1 * v.0 >= 0. {
                    1.
                } else {
                    -1.
                };
                hits.push((site.s, side, dist.clamp(2.5, 6.)));
            }
        }
        hits.sort_by(|a, b| a.0.total_cmp(&b.0));
        if hits.len() >= 3 && hits.windows(2).all(|w| w[1].0 - w[0].0 <= 100.) {
            let mut last = f64::NEG_INFINITY;
            hits.retain(|h| {
                if h.0 - last > 15. {
                    last = h.0;
                    true
                } else {
                    false
                }
            });
            return hits;
        }
        let side = if phase(&self.id, "side") < 0.5 {
            1.
        } else {
            -1.
        };
        self.stations()
            .into_iter()
            .map(|s| (s, side, 3.45))
            .collect()
    }
}

fn corridor(tracks: &[Track], i: usize, site: Site) -> Vec<(usize, Site)> {
    let mut members = vec![];
    for (j, track) in tracks.iter().enumerate() {
        if i == j {
            continue;
        }
        let (d, other) = track.nearest(site.p);
        if d <= 24. && site.u.dot(other.u).abs() >= 0.9 && other.s > 0. && other.s < track.length()
        {
            members.push((j, other));
        }
    }
    members
}
fn given_track(xy: XY, arc: Vec<f64>) -> PyResult<Track> {
    let xy = points(xy)?;
    if xy.len() < 2 || xy.len() != arc.len() {
        return Err(PyValueError::new_err("track and arclength samples differ"));
    }
    Ok(Track {
        id: String::new(),
        xy,
        arc,
    })
}
#[pyfunction]
fn catenary_span(xy: XY, arc: Vec<f64>, s: f64) -> PyResult<f64> {
    Ok(given_track(xy, arc)?.span(s))
}
#[pyfunction]
fn catenary_corridor<'p>(
    py: Python<'p>,
    parts: &Bound<'p, PyAny>,
    arcs: Vec<Vec<f64>>,
    i: usize,
    p: [f64; 2],
    u: [f64; 2],
) -> PyResult<Bound<'p, PyList>> {
    let tracks = arcs
        .into_iter()
        .enumerate()
        .map(|(j, a)| given_track(parts.get_item(j)?.get_item(1)?.extract()?, a))
        .collect::<PyResult<Vec<_>>>()?;
    let site = Site {
        s: 0.,
        p: Point(p[0], p[1]),
        u: Point(u[0], u[1]),
    };
    PyList::new(
        py,
        corridor(&tracks, i, site)
            .into_iter()
            .map(|(j, s)| (j, s.s, s.p.arr(py), s.u.arr(py))),
    )
}
fn distance(p: Point, a: Point, b: Point) -> f64 {
    let d = b - a;
    let t = ((p - a).dot(d) / d.dot(d).max(1e-12)).clamp(0., 1.);
    (a + d * t - p).norm()
}

fn near_ring(p: Point, ring: &[Point], margin: f64) -> bool {
    let mut inside = false;
    for (&a, &b) in ring.iter().zip(ring.iter().cycle().skip(1)) {
        if distance(p, a, b) <= margin {
            return true;
        }
        if (a.1 > p.1) != (b.1 > p.1) && p.0 < (b.0 - a.0) * (p.1 - a.1) / (b.1 - a.1) + a.0 {
            inside = !inside;
        }
    }
    inside
}

struct Layout<'py> {
    tracks: Vec<Track>,
    roads: Vec<(Line, f64)>,
    rings: Vec<Line>,
    ground: Bound<'py, PyAny>,
    result: Bound<'py, PyList>,
    supports: Vec<Vec<(f64, Bound<'py, PyDict>)>>,
    portals: Vec<Point>,
}

impl<'py> Layout<'py> {
    fn clear(&self, p: Point) -> bool {
        self.roads
            .iter()
            .all(|(xy, hw)| xy.windows(2).all(|w| distance(p, w[0], w[1]) > *hw + 2.))
            && !self.rings.iter().any(|r| near_ring(p, r, 1.5))
    }
    fn clear_tracks(&self, p: Point, skip: Option<usize>, margin: f64) -> bool {
        self.tracks.iter().enumerate().all(|(j, t)| {
            Some(j) == skip || t.xy.windows(2).all(|w| distance(p, w[0], w[1]) >= margin)
        })
    }
    fn z(&self, p: Point) -> PyResult<f64> {
        let py = self.ground.py();
        let answer = self.ground.call1((array(py, &[p]),))?;
        py.import("numpy")?
            .call_method1("asarray", (answer, "float64"))?
            .call_method0("ravel")?
            .get_item(0)?
            .extract()
    }
    fn mast(
        &self,
        i: usize,
        site: Site,
        q: Point,
        z: f64,
        zax: f64,
        dir: Point,
    ) -> PyResult<Bound<'py, PyDict>> {
        let py = self.result.py();
        let m = PyDict::new(py);
        m.set_item("fid", &self.tracks[i].id)?;
        m.set_item("part", i)?;
        m.set_item("s", site.s)?;
        m.set_item("q", q.arr(py))?;
        m.set_item("axis", site.p.arr(py))?;
        m.set_item("z", z)?;
        m.set_item("zax", zax)?;
        m.set_item("dir", dir.arr(py))?;
        m.set_item("u", site.u.arr(py))?;
        Ok(m)
    }
    fn place(&mut self, i: usize, s: f64, side: f64, off: f64) -> PyResult<()> {
        let site = self.tracks[i].frame(s);
        let mut members = vec![(i, site)];
        members.extend(corridor(&self.tracks, i, site));
        if members.len() >= 3 {
            if members.iter().skip(1).all(|(j, _)| i < *j) {
                self.portal(site, &members)?;
            }
            return Ok(());
        }
        for shift in &SHIFTS[..7] {
            let s = s + shift;
            if !(0. ..=self.tracks[i].length()).contains(&s) {
                continue;
            }
            let site = self.tracks[i].frame(s);
            for sg in [side, -side] {
                let q = site.p + site.u.left() * sg * off;
                if !self.clear_tracks(q, Some(i), 3.) || !self.clear(q) {
                    continue;
                }
                let zax = self.z(site.p)?;
                let z = self.z(q)?.clamp(zax - 0.5, zax + 0.2);
                let d = site.p - q;
                let m = self.mast(i, site, q, z, zax, d / d.norm().max(1e-9))?;
                m.set_item("pos", self.result.len())?;
                self.result.append(&m)?;
                self.supports[i].push((s, m));
                return Ok(());
            }
        }
        Ok(())
    }
    fn portal(&mut self, site: Site, members: &[(usize, Site)]) -> PyResult<()> {
        let n = site.u.left();
        let (mut lo, mut hi) = (0.0f64, 0.0f64);
        for (_, m) in members {
            let lat = (m.p - site.p).dot(n);
            lo = lo.min(lat);
            hi = hi.max(lat);
        }
        lo -= 3.4;
        hi += 3.4;
        if hi - lo > 60. + 2. * 3.4 {
            return Ok(());
        }
        let mut pair = None;
        for shift in SHIFTS {
            let c = site.p + site.u * shift;
            let (a, b) = (c + n * lo, c + n * hi);
            if self.clear(a) && self.clear(b) {
                pair.get_or_insert((a, b));
                if self.clear_tracks(a, None, 2.3) && self.clear_tracks(b, None, 2.3) {
                    pair = Some((a, b));
                    break;
                }
            }
        }
        let Some((a, b)) = pair else { return Ok(()) };
        let zs = members
            .iter()
            .map(|(_, s)| self.z(s.p))
            .collect::<PyResult<Vec<_>>>()?;
        let zm = crate::levels::np_sum(&zs) / zs.len() as f64;
        let za = self.z(a)?.clamp(zm - 0.5, zm + 0.2);
        let zb = self.z(b)?.clamp(zm - 0.5, zm + 0.2);
        let mid = (a + b) / 2.;
        if self.portals.iter().any(|p| (mid - *p).norm() < 14.) {
            return Ok(());
        }
        self.portals.push(mid);
        let py = self.result.py();
        let portal = PyDict::new(py);
        portal.set_item("portal", true)?;
        portal.set_item("fid", "portal")?;
        portal.set_item("s", site.s)?;
        let post = a.arr(py);
        portal.set_item("q", &post)?;
        portal.set_item("axis", &post)?;
        portal.set_item("posts", vec![(post, za), (b.arr(py), zb)])?;
        portal.set_item("z", zm)?;
        portal.set_item(
            "tracks",
            members
                .iter()
                .zip(&zs)
                .map(|((_, m), z)| (m.p.arr(py), *z))
                .collect::<Vec<_>>(),
        )?;
        self.result.append(portal)?;
        for (&(j, m), &z) in members.iter().zip(&zs) {
            let d = self.mast(j, m, m.p, z, z, Point::default())?;
            d.set_item("virtual", true)?;
            self.result.append(&d)?;
            self.supports[j].push((m.s, d));
        }
        Ok(())
    }
    fn link(&self, a: &Bound<'py, PyDict>, b: &Bound<'py, PyDict>) -> PyResult<()> {
        let d = PyDict::new(self.result.py());
        d.set_item("link", (a, b))?;
        self.result.append(d)
    }
    fn joints(&mut self) -> PyResult<()> {
        let mut ends = vec![];
        for (i, ms) in self.supports.iter_mut().enumerate() {
            if ms.is_empty() {
                continue;
            }
            ms.sort_by(|a, b| a.0.total_cmp(&b.0));
            let tr = &self.tracks[i];
            ends.push((i, tr.xy[0], 0., ms[0].1.clone()));
            ends.push((
                i,
                *tr.xy.last().unwrap(),
                tr.length(),
                ms.last().unwrap().1.clone(),
            ));
        }
        let mut used = vec![false; ends.len()];
        for (i, (a, p, _, ma)) in ends.iter().enumerate() {
            for (j, (b, q, _, mb)) in ends.iter().enumerate().skip(i + 1) {
                if a != b && (*p - *q).norm() <= 1.5 {
                    self.link(ma, mb)?;
                    used[i] = true;
                    used[j] = true;
                }
            }
        }
        for ((i, p, s, m), done) in ends.into_iter().zip(used) {
            if done {
                continue;
            }
            for (j, tr) in self.tracks.iter().enumerate() {
                if i == j {
                    continue;
                }
                let (d, site) = tr.nearest(p);
                if d <= 3. && site.s > 1. && site.s < tr.length() - 1. {
                    let z = self.z(site.p)?;
                    let end = self.mast(i, Site { s, ..site }, site.p, z, z, Point::default())?;
                    end.set_item("virtual", true)?;
                    end.set_item("end", true)?;
                    self.link(&m, &end)?;
                    break;
                }
            }
        }
        Ok(())
    }
}

#[pyfunction]
fn catenary_masts<'py>(
    py: Python<'py>,
    parts: Vec<(String, XY<'py>)>,
    roads: Vec<(XY<'py>, f64)>,
    rings: Vec<XY<'py>>,
    mapped: XY<'py>,
    ground: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let tracks = parts
        .into_iter()
        .map(|(id, xy)| Track::new(id, points(xy)?))
        .collect::<PyResult<Vec<_>>>()?;
    let roads = roads
        .into_iter()
        .map(|(xy, w)| Ok((points(xy)?, w)))
        .collect::<PyResult<_>>()?;
    let mut layout = Layout {
        supports: vec![vec![]; tracks.len()],
        tracks,
        roads,
        rings: rings.into_iter().map(points).collect::<PyResult<_>>()?,
        ground,
        result: PyList::empty(py),
        portals: vec![],
    };
    let mapped = points(mapped)?;
    for i in 0..layout.tracks.len() {
        for (s, side, off) in layout.tracks[i].supports(&mapped) {
            layout.place(i, s, side, off)?;
        }
    }
    layout.joints()?;
    Ok(layout.result)
}

#[pyfunction]
fn catenary_stations<'py>(
    py: Python<'py>,
    xy: XY<'py>,
    key: String,
) -> PyResult<(Vec<f64>, Bound<'py, numpy::PyArray1<f64>>)> {
    let p = points(xy)?;
    if p.len() < 2 {
        return Ok((vec![], vec![0.].into_pyarray(py)));
    }
    let t = Track::new(key, p)?;
    Ok((t.stations(), t.arc.into_pyarray(py)))
}

#[pyfunction]
fn catenary_near_ring(p: [f64; 2], rings: Vec<XY>, margin: f64) -> PyResult<bool> {
    for r in rings {
        if near_ring(Point(p[0], p[1]), &points(r)?, margin) {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(catenary_masts, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_stations, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_near_ring, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_span, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_corridor, m)?)?;
    input::register(m)?;
    wires::register(m)
}
