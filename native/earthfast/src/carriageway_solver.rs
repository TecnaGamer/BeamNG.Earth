//! Coupled road profiles: the existing banded quadratic objective with hard linear road-design constraints.
//! An unconstrained banded solve handles already-feasible profiles; Clarabel's native sparse QP handles the rest.
//!
//! Soft laws: a chain may also have a comfortable grade and a comfortable vertical curve (Parameters.soft_grade,
//! soft_k), past which each segment's (window's) excess is a slack variable paid for quadratically (w_grade, w_curve).
//! Its `grade` and `k` stay the hard ceilings. A problem whose laws contradict is then still feasible within the
//! ceilings: the excess goes where it costs least, not to an infeasible certificate and a relaxation's solves again.
use clarabel::algebra::CscMatrix;
use clarabel::solver::{
    DefaultSettings, DefaultSolver, IPSolver, NonnegativeConeT, SolverStatus, ZeroConeT,
};
use std::time::Instant;

const W_MIN: f64 = 1.0e-3;
/// A soft law's ceiling this loose is none: no hard row (a grade of 100 %, a curve's K of 0.001).
pub const NO_GRADE_CEILING: f64 = 1.0;
pub const NO_CURVE_CEILING: f64 = 1.0e-3;

#[derive(Clone, Copy)]
pub struct Parameters {
    pub grade: f64,
    pub k: f64,
    pub lam: f64,
    pub step: f64,
    pub length: f64,
    /// The comfortable grade (rise / run); 0: none (the hard `grade` alone).
    pub soft_grade: f64,
    /// The cost of a segment's grade past soft_grade: w_grade (s / h)^2 h / 2 for an excess s (m of height) over a
    /// segment h m long, i.e. per metre of road, per (excess grade)^2.
    pub w_grade: f64,
    /// The comfortable vertical curve's K (m of curve per % of grade change); 0: none (the hard `k` alone).
    pub soft_k: f64,
    /// The cost of a window's curve past soft_k: w_curve (s / hm)^2 hm / 2 for an excess s of its curve row (m),
    /// hm the window's mean spacing.
    pub w_curve: f64,
}

impl Parameters {
    /// The five hard parameters alone (no soft law).
    pub fn hard(grade: f64, k: f64, lam: f64, step: f64, length: f64) -> Self {
        Self { grade, k, lam, step, length, soft_grade: 0., w_grade: 0., soft_k: 0., w_curve: 0. }
    }

    fn soft_grade_on(&self) -> bool {
        self.soft_grade > 0. && self.w_grade > 0. && self.soft_grade < self.grade
    }

    fn soft_curve_on(&self) -> bool {
        self.soft_k > self.k && self.w_curve > 0.
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Coupling {
    pub a: usize,
    pub b: usize,
    pub inequality: bool,
    pub value: f64,
    /// The coupled quantity is z[a] - (1 - alpha) z[b] - alpha z[c]: b's height moved alpha of the way to c (a road's
    /// height where another leaves it, between two of its samples or past one). c == b, alpha 0: z[a] - z[b].
    pub c: usize,
    pub alpha: f64,
    /// An inequality's side: true for <= value, false for >= value.
    pub upper: bool,
    /// An inequality's softness: 0, hard; > 0, its shortfall s (m) is a slack paid w s^2 / 2 (a clearance OSM's
    /// layers ask that the joins and the laws contradict: the least shortfall, where it costs least, not no answer).
    pub soft: f64,
}

impl Coupling {
    /// The coupled quantity at heights z.
    pub fn gap(&self, z: &[f64]) -> f64 {
        z[self.a] - (1. - self.alpha) * z[self.b] - self.alpha * z[self.c]
    }
}

pub struct Problem {
    pub target: Vec<f64>,
    pub weight: Vec<f64>,
    pub lower: Vec<f64>,
    pub upper: Vec<f64>,
    pub offsets: Vec<usize>,
    pub parameters: Vec<Parameters>,
    /// The distance from each sample to the next along its chain (the last of a chain unused): the grade and curve
    /// bounds of a segment are its own, not one chain-wide step's. A value <= 0 (or an empty vector) means the chain's
    /// `Parameters.step` for that segment.
    pub spacing: Vec<f64>,
    pub couplings: Vec<Coupling>,
}

impl Problem {
    /// The distance of chain `chain`'s segment i (sample i to i + 1).
    fn gap(&self, chain: usize, i: usize) -> f64 {
        let h = self.spacing.get(self.offsets[chain] + i).copied().unwrap_or(0.);
        if h > 0. { h } else { self.parameters[chain].step }
    }

    /// A window's (i, i + 1, i + 2) curve row and the vertical curve's bound on it. The change of grade across the
    /// window, (z2 - z1) / h1 - (z1 - z0) / h0, is at most hm / (100 K) with hm the mean of the two spacings; the row is
    /// that times hm, [hm / h0, -(hm / h0 + hm / h1), hm / h1], bound hm^2 / (100 K): with equal spacings exactly the
    /// second difference [1, -2, 1] of heights and step^2 / (100 K), as before (unequal ones carried a grade term
    /// (h1 - h0) s the bound never had room for).
    fn curve_row(&self, chain: usize, i: usize) -> ([f64; 3], f64, f64) {
        self.curve_row_k(chain, i, self.parameters[chain].k)
    }

    /// curve_row with the bound of a curve of K = k (the soft law's comfortable one).
    fn curve_row_k(&self, chain: usize, i: usize, k: f64) -> ([f64; 3], f64, f64) {
        let (h0, h1) = (self.gap(chain, i), self.gap(chain, i + 1));
        let hm = 0.5 * (h0 + h1);
        let (a, c) = (hm / h0, hm / h1);
        ([a, -(a + c), c], hm * hm / (k * 100.), hm)
    }
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
    /// The largest grade past a soft law's comfortable one (rise / run), and curve past its comfortable one (change of
    /// grade per metre, as curve_excess), over the chains that have one; and what the soft laws' excess costs in all.
    pub soft_grade_excess: f64,
    pub soft_curve_excess: f64,
    pub soft_cost: f64,
    /// The largest shortfall of a soft coupling (m).
    pub soft_clearance_shortfall: f64,
    pub slack_vars: usize,
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
    index: [usize; 4],
    coeff: [f64; 4],
    len: usize,
    rhs: f64,
}

impl Row {
    fn new(index: &[usize], coeff: &[f64]) -> Self {
        let mut row = Row { index: [0; 4], coeff: [0.; 4], len: index.len(), rhs: 0. };
        row.index[..index.len()].copy_from_slice(index);
        row.coeff[..coeff.len()].copy_from_slice(coeff);
        row
    }
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
        for (k, w) in q.windows(2).enumerate() {
            let h = problem.gap(i, k);
            let g = p.grade * h;
            let excess = ((w[1] - w[0]).abs() - g).max(0.);
            d.grade_excess = d.grade_excess.max(excess / h);
            d.feasible &= excess <= g * 0.02 + 1e-3;
        }
        for (k, w) in q.windows(3).enumerate() {
            let (row, curve, hm) = problem.curve_row(i, k);
            let excess = ((row[0] * w[0] + row[1] * w[1] + row[2] * w[2]).abs() - curve).max(0.);
            d.curve_excess = d.curve_excess.max(excess / (hm * hm));
            d.feasible &= excess <= curve * 0.05 + 1e-4;
        }
        if p.soft_grade_on() {
            for (k, w) in q.windows(2).enumerate() {
                let h = problem.gap(i, k);
                let s = ((w[1] - w[0]).abs() - p.soft_grade * h).max(0.);
                d.soft_grade_excess = d.soft_grade_excess.max(s / h);
                d.soft_cost += 0.5 * p.w_grade * s * s / h;
            }
        }
        if p.soft_curve_on() {
            for (k, w) in q.windows(3).enumerate() {
                let (row, curve, hm) = problem.curve_row_k(i, k, p.soft_k);
                let s = ((row[0] * w[0] + row[1] * w[1] + row[2] * w[2]).abs() - curve).max(0.);
                d.soft_curve_excess = d.soft_curve_excess.max(s / (hm * hm));
                d.soft_cost += 0.5 * p.w_curve * s * s / hm;
            }
        }
    }
    for (i, &v) in z.iter().enumerate() {
        d.lower_shortfall = d.lower_shortfall.max((problem.lower[i] - v).max(0.));
        d.upper_excess = d.upper_excess.max((v - problem.upper[i]).max(0.));
        d.feasible &= v.is_finite();
    }
    for c in &problem.couplings {
        let gap = c.gap(z);
        if c.inequality && c.soft > 0. {
            let short = (if c.upper { gap - c.value } else { c.value - gap }).max(0.);
            d.soft_clearance_shortfall = d.soft_clearance_shortfall.max(short);
            d.soft_cost += 0.5 * c.soft * short * short;
        } else if c.inequality {
            let short = if c.upper { gap - c.value } else { c.value - gap };
            d.clearance_shortfall = d.clearance_shortfall.max(short.max(0.));
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
    let mut nv = n;                     // (variables: the n heights, then the soft laws' slacks)
    let mut slack_p: Vec<f64> = Vec::new();
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
            if p.soft_grade_on() && p.grade >= NO_GRADE_CEILING {
                continue;       // (no ceiling: the soft law alone)
            }
            bounds(
                Row::new(&[a + i, a + i + 1], &[-1., 1.]),
                -p.grade * problem.gap(chain, i),
                p.grade * problem.gap(chain, i),
                &mut eq,
                &mut ineq,
            );
        }
        for i in 0..count.saturating_sub(2) {
            let (row, curve, _) = problem.curve_row(chain, i);
            a0[i] += p.lam;
            a0[i + 1] += 4. * p.lam;
            a0[i + 2] += p.lam;
            a1[i] -= 2. * p.lam;
            a1[i + 1] -= 2. * p.lam;
            a2[i] += p.lam;
            if p.soft_curve_on() && p.k <= NO_CURVE_CEILING {
                continue;
            }
            bounds(
                Row::new(&[a + i, a + i + 1, a + i + 2], &row),
                -curve,
                curve,
                &mut eq,
                &mut ineq,
            );
        }
        // (the soft laws: a slack per segment past the comfortable grade, per window past the comfortable curve, each
        // nonnegative and paid for quadratically; the hard rows above stay the ceilings)
        if p.soft_grade_on() {
            for i in 0..count.saturating_sub(1) {
                let h = problem.gap(chain, i);
                let s = nv;
                nv += 1;
                slack_p.push(p.w_grade / h);
                let rhs = p.soft_grade * h;
                for sign in [1., -1.] {
                    let mut row = Row::new(&[a + i, a + i + 1, s], &[-sign, sign, -1.]);
                    row.rhs = rhs;
                    ineq.push(row);
                }
            }
        }
        if p.soft_curve_on() {
            for i in 0..count.saturating_sub(2) {
                let (row, curve, hm) = problem.curve_row_k(chain, i, p.soft_k);
                let s = nv;
                nv += 1;
                slack_p.push(p.w_curve / hm);
                for sign in [1., -1.] {
                    let mut r = Row::new(&[a + i, a + i + 1, a + i + 2, s],
                                         &[sign * row[0], sign * row[1], sign * row[2], -1.]);
                    r.rhs = curve;
                    ineq.push(r);
                }
            }
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
                    Row::new(&[a + i], &[1.]),
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
        let row = if c.alpha == 0. || c.c == c.b {
            Row::new(&[c.a, c.b], &[1., -1.])
        } else {
            Row::new(&[c.a, c.b, c.c], &[1., -(1. - c.alpha), -c.alpha])
        };
        if c.inequality && c.soft > 0. {
            // (row <= value + s, or row >= value - s: the slack the shortfall, nonnegative, paid for)
            let s = nv;
            nv += 1;
            slack_p.push(c.soft);
            let sign = if c.upper { 1. } else { -1. };
            let mut r = Row::new(&[], &[]);
            for j in 0..row.len {
                r.index[j] = row.index[j];
                r.coeff[j] = sign * row.coeff[j];
            }
            r.index[row.len] = s;
            r.coeff[row.len] = -1.;
            r.len = row.len + 1;
            r.rhs = sign * c.value;
            ineq.push(r);
        } else if c.inequality && c.upper {
            bounds(row, f64::NEG_INFINITY, c.value, &mut eq, &mut ineq);
        } else if c.inequality {
            bounds(row, c.value, f64::INFINITY, &mut eq, &mut ineq);
        } else {
            bounds(row, 0., 0., &mut eq, &mut ineq);
        }
    }
    measure(problem, &z, &mut d);
    d.slack_vars = nv - n;
    let exact = d.soft_clearance_shortfall <= 1e-8
        && d.soft_grade_excess <= 1e-10
        && d.soft_curve_excess <= 1e-12
        && d.grade_excess <= 1e-10
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
    for (k, &w) in slack_p.iter().enumerate() {
        pr.push(n + k);
        pc.push(n + k);
        pv.push(w);
    }
    q.resize(nv, 0.);
    let pmat = CscMatrix::new_from_triplets(nv, nv, pr, pc, pv);
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
    let amat = CscMatrix::new_from_triplets(rhs.len(), nv, ar, ac, av);
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
        // (no refinement rounds after each step: half the time on a city's laws, the same answer to 2 cm)
        iterative_refinement_enable: false,
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
        z = solver.solution.x[..n].to_vec();
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
    d.soft_grade_excess = measured.soft_grade_excess;
    d.soft_curve_excess = measured.soft_curve_excess;
    d.soft_cost = measured.soft_cost;
    d.soft_clearance_shortfall = measured.soft_clearance_shortfall;
    d.converged = matches!(status, SolverStatus::Solved | SolverStatus::AlmostSolved) && d.feasible;
    d.elapsed_s = start.elapsed().as_secs_f64();
    Ok((z, d))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One chain of n samples 4 m apart drawn to a ground with a 6 m step at its middle, its ends held (a road meeting
    /// two levels), with the given laws.
    fn step_problem(n: usize, p: Parameters) -> Problem {
        let target: Vec<f64> = (0..n).map(|i| if i < n / 2 { 0. } else { 6. }).collect();
        let mut lower = vec![f64::NEG_INFINITY; n];
        let mut upper = vec![f64::INFINITY; n];
        lower[0] = 0.;
        upper[0] = 0.;
        lower[n - 1] = 6.;
        upper[n - 1] = 6.;
        Problem {
            target,
            weight: vec![1.; n],
            lower,
            upper,
            offsets: vec![0, n],
            parameters: vec![p],
            spacing: Vec::new(),
            couplings: Vec::new(),
        }
    }

    #[test]
    fn soft_laws_past_what_the_hard_ones_allow_still_solve_within_the_ceilings() {
        // (6 m over 40 m: 15 % needed; a hard 8 % law has no answer, a comfortable 8 % with a 20 % ceiling has one)
        let hard = step_problem(11, Parameters::hard(0.08, 2., 1., 4., 0.));
        let (_, d) = solve(&hard, 200, 10.).unwrap();
        assert!(!d.feasible || d.grade_excess > 1e-3, "{:?}", d);
        let mut p = Parameters::hard(0.20, 2., 1., 4., 0.);
        p.soft_grade = 0.08;
        p.w_grade = 1.0e4;
        let (z, d) = solve(&step_problem(11, p), 200, 10.).unwrap();
        assert!(d.converged && d.feasible, "{:?}", d);
        assert!(d.grade_excess < 1e-6, "{:?}", d);
        assert!(d.soft_grade_excess > 0.01, "the comfortable grade is exceeded: {:?}", d);
        assert!((z[0] - 0.).abs() < 1e-6 && (z[10] - 6.).abs() < 1e-6);
        assert!(d.slack_vars == 10);
    }

    #[test]
    fn a_costlier_grade_spreads_the_climb() {
        // (the dearer the excess, the more of the 6 m is taken by the samples away from the step)
        let run = |w: f64| {
            let mut p = Parameters::hard(0.5, 0.01, 1., 4., 0.);
            p.soft_grade = 0.02;
            p.w_grade = w;
            let (z, d) = solve(&step_problem(41, p), 300, 10.).unwrap();
            assert!(d.converged, "{:?}", d);
            z.windows(2).map(|w| (w[1] - w[0]).abs() / 4.).fold(0., f64::max)
        };
        let (cheap, dear) = (run(1.), run(1.0e5));
        assert!(dear < cheap, "steepest grade {dear} with a dear excess, {cheap} with a cheap one");
    }

    #[test]
    fn a_soft_clearance_that_contradicts_a_join_is_short_where_it_costs_least() {
        // (two flat chains joined at sample 0, the first asked 6 m over the second at sample 1 by a hard grade of 10 %
        // over 4 m: no answer with the clearance hard; soft, the shortfall is taken and both stay near their ground)
        let p = Parameters::hard(0.1, 2., 1., 4., 0.);
        let mk = |soft: f64| Problem {
            target: vec![0.; 6],
            weight: vec![1.; 6],
            lower: vec![f64::NEG_INFINITY; 6],
            upper: vec![f64::INFINITY; 6],
            offsets: vec![0, 3, 6],
            parameters: vec![p, p],
            spacing: Vec::new(),
            couplings: vec![
                Coupling { a: 0, b: 3, inequality: false, value: 0., c: 3, alpha: 0., upper: false, soft: 0. },
                Coupling { a: 1, b: 4, inequality: true, value: 6., c: 4, alpha: 0., upper: false, soft },
            ],
        };
        let (_, d) = solve(&mk(0.), 200, 10.).unwrap();
        assert!(!d.converged, "hard: {:?}", d);
        let (z, d) = solve(&mk(1.0e3), 200, 10.).unwrap();
        assert!(d.converged && d.feasible, "soft: {:?}", d);
        assert!(d.soft_clearance_shortfall > 5., "{:?}", d);       // (0.8 m of the 6 at most within the grades)
        assert!(z[1] - z[4] > 0.5 && z[1] - z[4] <= 0.8 + 1e-6, "{:?}", z);
    }

    #[test]
    fn no_soft_law_is_the_hard_solve() {
        let p = Parameters::hard(0.2, 2., 1., 4., 0.);
        let (a, _) = solve(&step_problem(11, p), 200, 10.).unwrap();
        let mut q = p;
        q.w_grade = 5.;         // (a cost with no comfortable grade: no soft law)
        let (b, d) = solve(&step_problem(11, q), 200, 10.).unwrap();
        assert_eq!(d.slack_vars, 0);
        assert_eq!(a, b);
    }

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
