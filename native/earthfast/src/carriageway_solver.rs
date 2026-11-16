//! Coupled road profiles: the existing banded quadratic objective with hard linear road-design constraints.
//! An unconstrained banded solve handles already-feasible profiles; Clarabel's native sparse QP handles the rest.
use clarabel::algebra::CscMatrix;
use clarabel::solver::{
    DefaultSettings, DefaultSolver, IPSolver, NonnegativeConeT, SolverStatus, ZeroConeT,
};
use std::time::Instant;

const W_MIN: f64 = 1.0e-3;

#[derive(Clone, Copy)]
pub struct Parameters {
    pub grade: f64,
    pub k: f64,
    pub lam: f64,
    pub step: f64,
    pub length: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Coupling {
    pub a: usize,
    pub b: usize,
    pub inequality: bool,
    pub value: f64,
}

pub struct Problem {
    pub target: Vec<f64>,
    pub weight: Vec<f64>,
    pub lower: Vec<f64>,
    pub upper: Vec<f64>,
    pub offsets: Vec<usize>,
    pub parameters: Vec<Parameters>,
    pub couplings: Vec<Coupling>,
}

#[derive(Default, Debug)]
pub struct Diagnostics {
    pub status: String,
    pub backend: &'static str,
    pub rounds: usize,
    pub timed_out: bool,
    pub converged: bool,
    pub infeasible: bool,
    pub feasible: bool,
    pub grade_excess: f64,
    pub curve_excess: f64,
    pub lower_shortfall: f64,
    pub upper_excess: f64,
    pub junction_gap: f64,
    pub clearance_shortfall: f64,
    pub elapsed_s: f64,
    pub objective_nnz: usize,
    pub constraint_nnz: usize,
    pub constraint_rows: usize,
    pub factor_nnz: usize,
}

struct Factor {
    l0: Vec<f64>,
    l1: Vec<f64>,
    l2: Vec<f64>,
}

impl Factor {
    fn new(a0: &[f64], a1: &[f64], a2: &[f64]) -> Result<Self, String> {
        let n = a0.len();
        let mut out = Self {
            l0: vec![0.; n],
            l1: vec![0.; n],
            l2: vec![0.; n],
        };
        for i in 0..n {
            if i >= 2 {
                out.l2[i] = a2[i - 2] / out.l0[i - 2];
            }
            if i >= 1 {
                out.l1[i] = (a1[i - 1]
                    - if i >= 2 {
                        out.l2[i] * out.l1[i - 1]
                    } else {
                        0.
                    })
                    / out.l0[i - 1];
            }
            let pivot = a0[i] - out.l1[i] * out.l1[i] - out.l2[i] * out.l2[i];
            if !pivot.is_finite() || pivot <= 0. {
                return Err("carriageway objective lost positive definiteness".into());
            }
            out.l0[i] = pivot.sqrt();
        }
        Ok(out)
    }

    fn solve_into(&self, rhs: &[f64], out: &mut [f64]) {
        for i in 0..rhs.len() {
            let mut v = rhs[i];
            if i >= 1 {
                v -= self.l1[i] * out[i - 1];
            }
            if i >= 2 {
                v -= self.l2[i] * out[i - 2];
            }
            out[i] = v / self.l0[i];
        }
        for i in (0..rhs.len()).rev() {
            let mut v = out[i];
            if i + 1 < rhs.len() {
                v -= self.l1[i + 1] * out[i + 1];
            }
            if i + 2 < rhs.len() {
                v -= self.l2[i + 2] * out[i + 2];
            }
            out[i] = v / self.l0[i];
        }
    }
}

#[derive(Clone, Copy)]
struct Row {
    index: [usize; 3],
    coeff: [f64; 3],
    len: usize,
    rhs: f64,
}

fn bounds(mut row: Row, lo: f64, hi: f64, equalities: &mut Vec<Row>, inequalities: &mut Vec<Row>) {
    if lo == hi {
        row.rhs = lo;
        equalities.push(row);
        return;
    }
    if hi.is_finite() {
        row.rhs = hi;
        inequalities.push(row);
    }
    if lo.is_finite() {
        row.rhs = -lo;
        for c in &mut row.coeff {
            *c = -*c;
        }
        inequalities.push(row);
    }
}

pub fn measure(problem: &Problem, z: &[f64], d: &mut Diagnostics) {
    d.feasible = true;
    for (i, p) in problem.parameters.iter().enumerate() {
        let q = &z[problem.offsets[i]..problem.offsets[i + 1]];
        let g = p.grade * p.step;
        let curve = p.step * p.step / (p.k * 100.);
        for w in q.windows(2) {
            let excess = ((w[1] - w[0]).abs() - g).max(0.);
            d.grade_excess = d.grade_excess.max(excess / p.step);
            d.feasible &= excess <= g * 0.02 + 1e-3;
        }
        for w in q.windows(3) {
            let excess = ((w[0] - 2. * w[1] + w[2]).abs() - curve).max(0.);
            d.curve_excess = d.curve_excess.max(excess / (p.step * p.step));
            d.feasible &= excess <= curve * 0.05 + 1e-4;
        }
    }
    for (i, &v) in z.iter().enumerate() {
        d.lower_shortfall = d.lower_shortfall.max((problem.lower[i] - v).max(0.));
        d.upper_excess = d.upper_excess.max((v - problem.upper[i]).max(0.));
        d.feasible &= v.is_finite();
    }
    for c in &problem.couplings {
        let gap = z[c.a] - z[c.b];
        if c.inequality {
            d.clearance_shortfall = d.clearance_shortfall.max((c.value - gap).max(0.));
        } else {
            d.junction_gap = d.junction_gap.max(gap.abs());
        }
    }
    d.feasible &= d.lower_shortfall <= 0.02
        && d.upper_excess <= 0.02
        && d.junction_gap <= 0.02
        && d.clearance_shortfall <= 0.02;
}

pub fn solve(
    problem: &Problem,
    rounds: usize,
    budget: f64,
) -> Result<(Vec<f64>, Diagnostics), String> {
    let start = Instant::now();
    let n = problem.target.len();
    let mut d = Diagnostics {
        backend: "banded",
        ..Default::default()
    };
    let (mut pr, mut pc, mut pv) = (
        Vec::with_capacity(3 * n),
        Vec::with_capacity(3 * n),
        Vec::with_capacity(3 * n),
    );
    let mut q = vec![0.; n];
    let mut z = vec![0.; n];
    let (mut eq, mut ineq) = (Vec::new(), Vec::new());
    for (chain, p) in problem.parameters.iter().enumerate() {
        let (a, b) = (problem.offsets[chain], problem.offsets[chain + 1]);
        let count = b - a;
        let mut a0: Vec<_> = problem.weight[a..b].iter().map(|w| w.max(W_MIN)).collect();
        let mut a1 = vec![0.; count.saturating_sub(1)];
        let mut a2 = vec![0.; count.saturating_sub(2)];
        for i in 0..count {
            q[a + i] = -a0[i] * problem.target[a + i];
        }
        for i in 0..count.saturating_sub(1) {
            a0[i] += p.length;
            a0[i + 1] += p.length;
            a1[i] -= p.length;
            bounds(
                Row {
                    index: [a + i, a + i + 1, 0],
                    coeff: [-1., 1., 0.],
                    len: 2,
                    rhs: 0.,
                },
                -p.grade * p.step,
                p.grade * p.step,
                &mut eq,
                &mut ineq,
            );
        }
        let curve = p.step * p.step / (p.k * 100.);
        for i in 0..count.saturating_sub(2) {
            a0[i] += p.lam;
            a0[i + 1] += 4. * p.lam;
            a0[i + 2] += p.lam;
            a1[i] -= 2. * p.lam;
            a1[i + 1] -= 2. * p.lam;
            a2[i] += p.lam;
            bounds(
                Row {
                    index: [a + i, a + i + 1, a + i + 2],
                    coeff: [1., -2., 1.],
                    len: 3,
                    rhs: 0.,
                },
                -curve,
                curve,
                &mut eq,
                &mut ineq,
            );
        }
        for i in 0..count {
            pr.push(a + i);
            pc.push(a + i);
            pv.push(a0[i]);
            if i > 0 {
                pr.push(a + i - 1);
                pc.push(a + i);
                pv.push(a1[i - 1]);
            }
            if i > 1 {
                pr.push(a + i - 2);
                pc.push(a + i);
                pv.push(a2[i - 2]);
            }
            if problem.lower[a + i] > problem.upper[a + i] {
                d.infeasible = true;
            } else {
                bounds(
                    Row {
                        index: [a + i, 0, 0],
                        coeff: [1., 0., 0.],
                        len: 1,
                        rhs: 0.,
                    },
                    problem.lower[a + i],
                    problem.upper[a + i],
                    &mut eq,
                    &mut ineq,
                );
            }
        }
        let factor = Factor::new(&a0, &a1, &a2)?;
        let rhs: Vec<_> = q[a..b].iter().map(|v| -v).collect();
        factor.solve_into(&rhs, &mut z[a..b]);
    }
    for c in &problem.couplings {
        let row = Row {
            index: [c.a, c.b, 0],
            coeff: [1., -1., 0.],
            len: 2,
            rhs: 0.,
        };
        if c.inequality {
            bounds(row, c.value, f64::INFINITY, &mut eq, &mut ineq);
        } else {
            bounds(row, 0., 0., &mut eq, &mut ineq);
        }
    }
    measure(problem, &z, &mut d);
    let exact = d.grade_excess <= 1e-10
        && d.curve_excess <= 1e-12
        && d.lower_shortfall <= 1e-8
        && d.upper_excess <= 1e-8
        && d.junction_gap <= 1e-8
        && d.clearance_shortfall <= 1e-8;
    if n == 0 || (exact && !d.infeasible) {
        d.status = "Unconstrained".into();
        d.converged = true;
        d.elapsed_s = start.elapsed().as_secs_f64();
        return Ok((z, d));
    }
    if d.infeasible || start.elapsed().as_secs_f64() >= budget {
        d.status = if d.infeasible {
            "InconsistentBounds"
        } else {
            "MaxTime"
        }
        .into();
        d.timed_out = !d.infeasible;
        d.elapsed_s = start.elapsed().as_secs_f64();
        return Ok((z, d));
    }
    let pmat = CscMatrix::new_from_triplets(n, n, pr, pc, pv);
    let neq = eq.len();
    let nineq = ineq.len();
    let (mut ar, mut ac, mut av) = (Vec::new(), Vec::new(), Vec::new());
    let mut rhs = Vec::with_capacity(neq + nineq);
    for (i, row) in eq.iter().chain(&ineq).enumerate() {
        rhs.push(row.rhs);
        for j in 0..row.len {
            ar.push(i);
            ac.push(row.index[j]);
            av.push(row.coeff[j]);
        }
    }
    let amat = CscMatrix::new_from_triplets(rhs.len(), n, ar, ac, av);
    d.backend = "sparse_qp";
    d.objective_nnz = pmat.nnz();
    d.constraint_nnz = amat.nnz();
    d.constraint_rows = amat.m;
    let settings = DefaultSettings {
        verbose: false,
        max_iter: rounds.min(u32::MAX as usize) as u32,
        time_limit: (budget - start.elapsed().as_secs_f64()).max(0.),
        max_threads: 1,
        tol_feas: 1e-9,
        tol_gap_abs: 1e-8,
        tol_gap_rel: 1e-9,
        direct_solve_method: "qdldl".into(),
        ..Default::default()
    };
    let cones = [ZeroConeT(neq), NonnegativeConeT(nineq)];
    let mut solver =
        DefaultSolver::new(&pmat, &q, &amat, &rhs, &cones, settings).map_err(|e| e.to_string())?;
    solver.solve();
    let status = solver.solution.status;
    d.status = format!("{status:?}");
    d.rounds = solver.solution.iterations as usize;
    d.infeasible = matches!(
        status,
        SolverStatus::PrimalInfeasible
            | SolverStatus::AlmostPrimalInfeasible
            | SolverStatus::DualInfeasible
            | SolverStatus::AlmostDualInfeasible
    );
    d.timed_out = status == SolverStatus::MaxTime;
    d.factor_nnz = solver.info.linsolver.nnzL;
    // Infeasible statuses return a certificate, not road heights. Never interpret that vector as a profile.
    if !d.infeasible && solver.solution.x.iter().all(|v| v.is_finite()) {
        z = std::mem::take(&mut solver.solution.x);
    }
    let mut measured = Diagnostics::default();
    measure(problem, &z, &mut measured);
    d.feasible = measured.feasible;
    d.grade_excess = measured.grade_excess;
    d.curve_excess = measured.curve_excess;
    d.lower_shortfall = measured.lower_shortfall;
    d.upper_excess = measured.upper_excess;
    d.junction_gap = measured.junction_gap;
    d.clearance_shortfall = measured.clearance_shortfall;
    d.converged = matches!(status, SolverStatus::Solved | SolverStatus::AlmostSolved) && d.feasible;
    d.elapsed_s = start.elapsed().as_secs_f64();
    Ok((z, d))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn banded_factor_solves_known_system() {
        let factor = Factor::new(&[5., 6., 7., 8.], &[-1., 0.5, -2.], &[0.2, 0.3]).unwrap();
        let expected = [1., 2., 3., 4.];
        let rhs = [
            5. - 2. + 0.6,
            -1. + 12. + 1.5 + 1.2,
            0.2 + 1. + 21. - 8.,
            0.6 - 6. + 32.,
        ];
        let mut z = [0.; 4];
        factor.solve_into(&rhs, &mut z);
        for (got, want) in z.iter().zip(expected) {
            assert!((got - want).abs() < 1e-12);
        }
    }
}
