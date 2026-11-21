//! Surface profiles use linear constraints for the road geometry. Only the
//! clearance observations have slack variables. A pin is a two-sided penalty.
use super::{lin_val, Lin, Member, SolveOut, S_SOLVED, S_SOLVER};
use clarabel::algebra::CscMatrix;
use clarabel::solver::{
    DefaultSettings, DefaultSolver, IPSolver, NonnegativeConeT, SolverStatus, ZeroConeT,
};
use std::collections::BTreeMap;

// Relative to the integrated unit weight on today's elevations. A clearance
// shortfall costs both its magnitude and its square. The linear term lets a
// compatible observation be satisfied exactly.
const FLOOR_LINEAR: f64 = 4_000.;
const FLOOR_SQUARE: f64 = 400.;
const PIN_WEIGHT: f64 = 30_000.;
const SHEET_WEIGHT: f64 = 200_000.;
// 0.25 g at 70 mph, with room for interpolation onto emitted deck vertices.
const MAX_CURVATURE: f64 = 0.0025;

#[derive(Default)]
struct Triplets {
    row: Vec<usize>,
    col: Vec<usize>,
    value: Vec<f64>,
}

impl Triplets {
    fn push(&mut self, i: usize, j: usize, v: f64) {
        self.row.push(i);
        self.col.push(j);
        self.value.push(v);
    }

    fn matrix(self, m: usize, n: usize) -> CscMatrix<f64> {
        CscMatrix::new_from_triplets(m, n, self.row, self.col, self.value)
    }
}

fn objective(p: &mut Triplets, q: &mut [f64], l: &Lin, weight: f64) {
    for &(i, a) in &l.terms {
        q[i] -= weight * a * l.r;
        for &(j, b) in &l.terms {
            if i <= j {
                p.push(i, j, weight * a * b);
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn solve(
    ms: &[Member],
    base: &[usize],
    rows: Vec<BTreeMap<usize, f64>>,
    rhs: Vec<f64>,
    eqs: &[Lin],
    group: &[i64],
    joins: &[([usize; 3], f64, f64)],
    pins: &[Lin],
    floors: &[Lin],
    kinds: &[u8],
    traffic_order: &[Lin],
    traffic_gap: f64,
    grade: f64,
    grade_add: f64,
    mut report: Vec<f64>,
) -> SolveOut {
    let n = rhs.len();
    let vars = n + floors.len();
    let mut p = Triplets::default();
    let mut q: Vec<_> = rhs.into_iter().map(|v| -v).collect();
    q.resize(vars, 0.);
    for (i, row) in rows.into_iter().enumerate() {
        for (j, v) in row {
            p.push(j, i, v);
        }
    }
    let mut a = Triplets::default();
    let mut b = Vec::new();
    fn row(a: &mut Triplets, b: &mut Vec<f64>, terms: &[(usize, f64)], sign: f64, bound: f64) {
        for &(j, coeff) in terms {
            a.push(b.len(), j, coeff * sign);
        }
        b.push(bound);
    }
    for (l, &g) in eqs.iter().zip(group) {
        if g < 0 {
            row(&mut a, &mut b, &l.terms, 1., l.r);
        } else {
            objective(&mut p, &mut q, l, SHEET_WEIGHT);
        }
    }
    for &([left, joint, right], dl, dr) in joins {
        if dl > 1e-6 && dr > 1e-6 {
            row(
                &mut a,
                &mut b,
                &[
                    (left, 1. / dl),
                    (joint, -1. / dl - 1. / dr),
                    (right, 1. / dr),
                ],
                1.,
                0.,
            );
        }
    }
    let neq = b.len();
    for l in pins {
        objective(&mut p, &mut q, l, PIN_WEIGHT);
    }
    for (m, &start) in ms.iter().zip(base) {
        let mut limit = grade;
        for (s, z) in m.s.windows(2).zip(m.z.windows(2)) {
            let ds = s[1] - s[0];
            if ds >= 0.5 {
                limit = limit.max((z[1] - z[0]).abs() / ds);
            }
        }
        for (k, s) in m.s.windows(2).enumerate() {
            let i = start + k;
            let ds = s[1] - s[0];
            let terms = [(i, -1.), (i + 1, 1.)];
            for sign in [-1., 1.] {
                row(&mut a, &mut b, &terms, sign, ds * (limit + grade_add));
            }
            if k > 0 && ds > 1e-6 {
                let dl = s[0] - m.s[k - 1];
                if dl <= 1e-6 {
                    continue;
                }
                let bend = [(i - 1, 1. / dl), (i, -1. / dl - 1. / ds), (i + 1, 1. / ds)];
                for sign in [-1., 1.] {
                    row(&mut a, &mut b, &bend, sign, MAX_CURVATURE * (ds + dl) * 0.5);
                }
            }
        }
    }
    for l in traffic_order {
        row(&mut a, &mut b, &l.terms, -1., -l.r);
    }
    let mut order_rows = Vec::new();
    for (l, &kind) in floors.iter().zip(kinds) {
        if kind == 2 || kind == 5 {
            order_rows.push(b.len());
            row(&mut a, &mut b, &l.terms, -1., -traffic_gap.min(l.r));
        }
    }
    let hard_end = b.len();
    for (k, (l, &kind)) in floors.iter().zip(kinds).enumerate() {
        let weight = if kind == 1 { 0.1 } else { 1. };
        q[n + k] = FLOOR_LINEAR * weight;
        p.push(n + k, n + k, FLOOR_SQUARE * weight);
        a.push(b.len(), n + k, -1.);
        row(&mut a, &mut b, &l.terms, -1., -l.r);
        row(&mut a, &mut b, &[(n + k, -1.)], 1., 0.);
    }
    let p = p.matrix(vars, vars);
    let a = a.matrix(b.len(), vars);
    let cones = [ZeroConeT(neq), NonnegativeConeT(b.len() - neq)];
    let settings = DefaultSettings {
        verbose: false,
        max_threads: 1,
        direct_solve_method: "qdldl".into(),
        tol_feas: 1e-9,
        tol_gap_rel: 1e-10,
        max_iter: 150,
        ..Default::default()
    };
    let fail = |mut rep: Vec<f64>| {
        rep[0] = S_SOLVER as f64;
        SolveOut {
            z: ms.iter().flat_map(|m| m.z.iter().copied()).collect(),
            tried: vec![],
            status: S_SOLVER,
            report: rep,
        }
    };
    // Keep a vehicle-height reserve where feasible. Where it conflicts with
    // continuity or the grade bound, retain the mapped order and charge the
    // missing clearance to the same observation.
    let attempt = |bounds: &[f64]| {
        let mut solver = DefaultSolver::new(&p, &q, &a, bounds, &cones, settings.clone()).ok()?;
        solver.solve();
        if !matches!(
            solver.solution.status,
            SolverStatus::Solved | SolverStatus::AlmostSolved
        ) {
            return None;
        }
        let z = solver.solution.x[..n].to_vec();
        if !z.iter().all(|v| v.is_finite()) {
            return None;
        }
        let mut ax = vec![0.; hard_end];
        for (j, &v) in z.iter().enumerate() {
            for k in a.colptr[j]..a.colptr[j + 1] {
                let i = a.rowval[k];
                if i < hard_end {
                    ax[i] += a.nzval[k] * v;
                }
            }
        }
        let residual = (0..neq)
            .map(|i| (ax[i] - bounds[i]).abs())
            .fold(0., f64::max);
        if residual > 1e-5 || (neq..hard_end).any(|i| ax[i] > bounds[i] + 1e-5) {
            return None;
        }
        Some((z, solver.solution.iterations, residual))
    };
    let result = attempt(&b).or_else(|| {
        if order_rows.is_empty() {
            return None;
        }
        for i in order_rows {
            b[i] = b[i].max(-0.02);
        }
        attempt(&b)
    });
    let Some((z, iterations, residual)) = result else {
        return fail(report);
    };
    report[2] = iterations as f64;
    report[6] = residual;
    // Per-observation accounting: kind, source row, member, arc, request,
    // achieved value, absolute violation and the cost paid in the objective.
    let mut record = |kind: u8, k: usize, l: &Lin, actual: f64, error: f64, cost: f64| {
        if error <= 1e-6 {
            return;
        }
        let i = l.terms[0].0;
        let m = base.partition_point(|&v| v <= i) - 1;
        let station = |m: usize, sign: f64| {
            l.terms
                .iter()
                .filter(|&&(j, c)| c * sign > 0. && j >= base[m] && j < base[m] + ms[m].s.len())
                .map(|&(j, c)| sign * c * ms[m].s[j - base[m]])
                .sum::<f64>()
        };
        let other = l
            .terms
            .iter()
            .find(|&&(_, c)| c < 0.)
            .map(|&(j, _)| base.partition_point(|&v| v <= j) - 1);
        report.extend([
            kind as f64,
            k as f64,
            m as f64,
            station(m, 1.),
            l.r,
            actual,
            error,
            cost,
            other.map_or(-1., |m| m as f64),
            other.map_or(0., |m| station(m, -1.)),
        ]);
    };
    for (k, (l, &kind)) in floors.iter().zip(kinds).enumerate() {
        let diff = lin_val(&z, l);
        let err = (-diff).max(0.);
        let w = if kind == 1 { 0.1 } else { 1. };
        record(
            kind,
            k,
            l,
            l.r + diff,
            err,
            w * (FLOOR_LINEAR * err + 0.5 * FLOOR_SQUARE * err * err),
        );
    }
    for (k, l) in pins.iter().enumerate() {
        let diff = lin_val(&z, l);
        record(
            3,
            k,
            l,
            l.r + diff,
            diff.abs(),
            0.5 * PIN_WEIGHT * diff * diff,
        );
    }
    for (k, (l, &g)) in eqs.iter().zip(group).enumerate() {
        if g < 0 {
            continue;
        }
        let diff = lin_val(&z, l);
        record(
            4,
            k,
            l,
            l.r + diff,
            diff.abs(),
            0.5 * SHEET_WEIGHT * diff * diff,
        );
    }
    report[7] = floors.iter().filter(|l| lin_val(&z, l) < -0.02).count() as f64;
    SolveOut {
        z,
        tried: vec![],
        status: S_SOLVED,
        report,
    }
}
