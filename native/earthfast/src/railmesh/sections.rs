//! Cross-sections of a contiguous visible run. Each band has its own rows and its own UV seam.
use super::mesh::{at, RailMesh};
use crate::{busstation::Point, dm::Dm};
const UP: [[f64; 3]; 1] = [[0., 0., 1.]];
const DOWN: [[f64; 3]; 1] = [[0., 0., -1.]];
pub(super) struct Run<'a> {
    pub p: &'a [Point],
    pub u: &'a [Point],
    pub top: &'a [f64],
    pub v: Vec<f64>,
}
impl Run<'_> {
    fn row(&self, mut f: impl FnMut(Point, Point, f64, usize) -> [f64; 3]) -> Vec<[f64; 3]> {
        (0..self.p.len())
            .map(|i| f(self.p[i], self.u[i], self.top[i], i))
            .collect()
    }
    fn offset(&self, x: f64, z: f64) -> Vec<[f64; 3]> {
        self.row(|p, u, t, _| at(p + u * x, t + z))
    }
    fn side(&self, sg: f64, z: f64) -> Vec<[f64; 3]> {
        self.u.iter().map(|u| at(*u * sg, z)).collect()
    }
    pub fn ballast(&self, mesh: &mut RailMesh, feet: [(&[f64], &[f64]); 2], mat: i64, tile: f64) {
        let v: Vec<_> = self.v.iter().map(|v| v * 3. / tile).collect();
        let left = self.offset(1.8, 0.);
        let right = self.offset(-1.8, 0.);
        mesh.band(&left, &right, 0., 2. * 1.8 / tile, &v, mat, &UP);
        for (k, sg) in [1., -1.].into_iter().enumerate() {
            let (foot, z) = feet[k];
            let bottom = self.row(|p, u, t, i| {
                at(
                    if k == 0 {
                        p + u * foot[i]
                    } else {
                        p - u * foot[i]
                    },
                    z[i].min(t) - 0.4,
                )
            });
            mesh.band(
                if k == 0 { &left } else { &right },
                &bottom,
                0.,
                0.8 * 3. / tile,
                &v,
                mat,
                &self.side(sg, 1.),
            );
        }
    }
    pub fn deck(&self, mesh: &mut RailMesh, tram: bool, ballast: i64, concrete: i64) {
        if tram {
            let top = |sg| self.row(|p, u, t, _| at(p + u * (sg * 1.3), t - 0.3 + 0.03));
            let a = top(1.);
            let b = top(-1.);
            let la = self.offset(1.3, -1.4);
            let lb = self.offset(-1.3, -1.4);
            mesh.band(&a, &b, 0., 0.9, &self.v, concrete, &UP);
            mesh.band(&a, &la, 0., 0.5, &self.v, concrete, &self.side(1., 0.));
            mesh.band(&b, &lb, 0., 0.5, &self.v, concrete, &self.side(-1., 0.));
            mesh.band(&la, &lb, 0., 0.9, &self.v, concrete, &DOWN);
            return;
        }
        let half = 2.4;
        let inside = half - 0.3;
        mesh.band(
            &self.offset(inside, 0.),
            &self.offset(-inside, 0.),
            0.,
            2. * half / 3.,
            &self.v,
            ballast,
            &UP,
        );
        let v: Vec<_> = self.v.iter().map(|v| v / 1.5).collect();
        for sg in [1., -1.] {
            let a = self.offset(sg * inside, 0.);
            let b = self.offset(sg * inside, 0.7);
            let c = self.offset(sg * half, 0.7);
            let d = self.offset(sg * half, -1.4);
            mesh.band(&a, &b, 0., 0.2, &v, concrete, &self.side(-sg, 0.));
            mesh.band(&b, &c, 0., 0.1, &v, concrete, &UP);
            mesh.band(&c, &d, 0., 0.6, &v, concrete, &self.side(sg, 0.));
        }
        mesh.band(
            &self.offset(half, -1.4),
            &self.offset(-half, -1.4),
            0.,
            1.,
            &v,
            concrete,
            &DOWN,
        );
    }
    pub fn tunnel(&self, mesh: &mut RailMesh, ballast: i64, concrete: i64, start: bool, end: bool) {
        let k = self.p.len();
        let flat = vec![3.9 - 0.3; k];
        let z: Vec<_> = self.top.iter().map(|t| t - 0.3 - 0.3).collect();
        self.ballast(mesh, [(&flat, &z), (&flat, &z)], ballast, 3.);
        let mut section = vec![Point(-3.9, -0.3)];
        let pi = std::f64::consts::PI;
        for i in 0..14 {
            let a = if i == 13 {
                0.
            } else {
                pi + (i as f64) * (-pi / 13.)
            };
            section.push(Point(3.9 * a.dcos(), 1.6 + 4.4 * a.dsin()));
        }
        section.push(Point(3.9, -0.3));
        for w in section.windows(2) {
            let middle = (w[0] + w[1]) / 2.;
            mesh.band(
                &self.offset(w[0].0, w[0].1),
                &self.offset(w[1].0, w[1].1),
                0.,
                0.3,
                &self.v,
                concrete,
                &self.side(-middle.0, 2. - middle.1),
            );
        }
        let v = vec![0.; section.len()];
        for (i, sg, on) in [(0, -1., start), (k - 1, 1., end)] {
            if !on {
                continue;
            }
            let d = if i == 0 {
                self.p[1] - self.p[0]
            } else {
                self.p[k - 1] - self.p[k - 2]
            };
            let dir = (d / d.norm().max(1e-9)) * sg;
            let (mut inner, mut outer) = (vec![], vec![]);
            for &Point(x, z) in &section {
                inner.push(at(self.p[i] + self.u[i] * x, self.top[i] + z));
                let x = x + 1.3f64.copysign(x) * f64::from(x.abs() > 0.1);
                outer.push(at(
                    self.p[i] + self.u[i] * x,
                    self.top[i] + z + if z > -0.3 + 0.01 { 1.3 } else { 0. },
                ));
            }
            mesh.band(&inner, &outer, 0., 0.3, &v, concrete, &[at(dir, 0.)]);
            mesh.band(&outer, &inner, 0., 0.3, &v, concrete, &[at(-dir, -0.)]);
        }
    }
    pub fn heads(&self, mesh: &mut RailMesh, half_g: f64, mat: i64, tram: bool, collider: bool) {
        for sg in [1., -1.] {
            let edge = |outer: bool, high: bool| {
                self.row(|p, u, t, _| {
                    let q = if collider {
                        let gin = half_g - 0.07 / 2.;
                        p + u * sg * (if outer { gin + 0.30 } else { gin })
                    } else {
                        let c = p + u * sg * half_g;
                        if outer {
                            c - u * 0.07 / 2.
                        } else {
                            c + u * 0.07 / 2.
                        }
                    };
                    let foot = if tram {
                        t - 0.3 + 0.03 - 0.16 + 0.005
                    } else {
                        t - 0.08 + 0.2
                    };
                    at(
                        q,
                        if high {
                            foot + 0.16
                        } else if collider {
                            t - 0.10
                        } else {
                            foot
                        },
                    )
                })
            };
            let a = edge(false, true);
            let b = edge(true, true);
            mesh.band(&a, &b, 0., 0.02, &self.v, mat, &UP);
            let dir = if collider { -sg } else { 1. };
            mesh.band(
                &edge(false, false),
                &a,
                0.,
                0.05,
                &self.v,
                mat,
                &self.side(dir, 0.),
            );
            mesh.band(
                &edge(true, false),
                &b,
                0.,
                0.05,
                &self.v,
                mat,
                &self.side(-dir, 0.),
            );
        }
    }
}
