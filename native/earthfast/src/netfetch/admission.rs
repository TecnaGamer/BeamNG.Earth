//! Host-local admission. Callers supply monotonic timestamps so a simulated clock drives the same transitions.
use super::{token_wait, TOKEN_EPS};
use pyo3::prelude::*;
use std::collections::VecDeque;

#[pyclass]
pub struct NetBucket {
    #[pyo3(get)]
    rate: f64,
    burst: f64,
    tokens: f64,
    at: f64,
    paused_until: Option<f64>,
}

impl NetBucket {
    fn refill(&mut self, now: f64) {
        if let Some(until) = self.paused_until {
            if now < until {
                return;
            }
            self.at = self.at.max(until);
            self.paused_until = None;
        }
        if now > self.at {
            self.tokens = self.burst.min(self.tokens + (now - self.at) * self.rate);
            self.at = now;
        }
    }
}

#[pymethods]
impl NetBucket {
    #[new]
    fn new(rate: f64, burst: f64, now: f64) -> Self {
        Self {
            rate,
            burst,
            tokens: burst,
            at: now,
            paused_until: None,
        }
    }

    fn level(&mut self, now: f64) -> f64 {
        self.refill(now);
        self.tokens
    }

    fn wait_time(&mut self, now: f64, floor: f64) -> PyResult<f64> {
        self.refill(now);
        let need = 1.0 + floor;
        if let Some(until) = self.paused_until {
            Ok(until - now + token_wait((need - self.tokens).max(0.0), self.rate)?)
        } else if self.tokens >= need - TOKEN_EPS {
            Ok(0.0)
        } else {
            token_wait(need - self.tokens, self.rate)
        }
    }

    /// None on a pause or a timeout, zero on admission, otherwise the wait until the caller can try again.
    fn take(&mut self, now: f64, deadline: Option<f64>, floor: f64) -> PyResult<Option<f64>> {
        let wait = self.wait_time(now, floor.min((self.burst - 1.0).max(0.0)))?;
        if self.paused_until.is_some() {
            return Ok(None);
        }
        if wait <= 0.0 {
            self.tokens -= 1.0;
            return Ok(Some(0.0));
        }
        if deadline.is_some_and(|d| now + wait > d) {
            Ok(None)
        } else {
            Ok(Some(wait))
        }
    }

    fn try_take(&mut self, now: f64, reserve: f64) -> bool {
        self.refill(now);
        if self.paused_until.is_none()
            && self.tokens >= 1.0 - TOKEN_EPS
            && self.tokens > reserve * self.burst
        {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    fn pause(&mut self, now: f64, until: f64) {
        self.refill(now);
        self.tokens = 0.0;
        self.paused_until = Some(until.max(self.paused_until.unwrap_or(until)));
    }

    fn set_rate(&mut self, now: f64, rate: f64) {
        self.refill(now);
        self.rate = rate;
    }
}

#[derive(PartialEq)]
enum State {
    Closed,
    Open,
    HalfOpen,
}

#[pyclass]
pub struct NetBreaker {
    threshold: i64,
    window: f64,
    base: f64,
    max_open: f64,
    #[pyo3(get)]
    open_s: f64,
    #[pyo3(get)]
    open_until: f64,
    #[pyo3(get)]
    trips: u64,
    probing: bool,
    failures: VecDeque<f64>,
    state: State,
}

#[pymethods]
impl NetBreaker {
    #[new]
    fn new(threshold: i64, window: f64, open_s: f64, max_open: f64) -> Self {
        Self {
            threshold,
            window,
            base: open_s,
            max_open,
            open_s,
            open_until: 0.0,
            trips: 0,
            probing: false,
            failures: VecDeque::new(),
            state: State::Closed,
        }
    }

    #[getter]
    fn state(&self) -> &'static str {
        match self.state {
            State::Closed => "closed",
            State::Open => "open",
            State::HalfOpen => "half-open",
        }
    }

    fn allow(&mut self, now: f64) -> (bool, bool, f64) {
        if self.state == State::Closed {
            return (true, false, 0.0);
        }
        if self.state == State::Open {
            if now < self.open_until {
                return (false, false, self.open_until - now);
            }
            self.state = State::HalfOpen;
        }
        if self.probing {
            return (false, false, 1.0);
        }
        self.probing = true;
        (true, true, 0.0)
    }

    fn may_try(&self, now: f64) -> bool {
        self.state == State::Closed
            || (!self.probing && (self.state != State::Open || now >= self.open_until))
    }

    fn is_open(&self) -> bool {
        self.state != State::Closed
    }

    fn release_probe(&mut self) {
        self.probing = false;
    }

    fn success(&mut self) -> bool {
        if self.state == State::Open {
            return false;
        }
        self.failures.clear();
        self.probing = false;
        if self.state == State::Closed {
            return false;
        }
        self.state = State::Closed;
        self.open_s = self.base;
        true
    }

    fn failure(&mut self, now: f64) -> bool {
        match self.state {
            State::Open => return false,
            State::HalfOpen => self.open_s = self.max_open.min(self.open_s * 2.0),
            State::Closed => {
                self.failures.push_back(now);
                while self
                    .failures
                    .front()
                    .is_some_and(|&at| now - at > self.window)
                {
                    self.failures.pop_front();
                }
                if (self.failures.len() as i64) < self.threshold {
                    return false;
                }
            }
        }
        self.trip(now, self.open_s);
        true
    }

    fn trip(&mut self, now: f64, seconds: f64) {
        let end = now + seconds;
        self.open_until = if self.state == State::Open {
            self.open_until.max(end)
        } else {
            end
        };
        self.state = State::Open;
        self.probing = false;
        self.failures.clear();
        self.trips += 1;
    }
}

/// Daily admission is based on requests already made. The persisted day counters also include worker-independent
/// session() requests. The fetcher owns those counters and resets this state when the UTC date rolls over.
#[pyclass]
pub struct NetBudget {
    soft: Option<i64>,
    hard: Option<i64>,
    predownload_share: f64,
    warning_share: f64,
    repeat_s: f64,
    warned: f64,
    warned_over: f64,
    half_rate: bool,
}

type BudgetDecision = (bool, Option<&'static str>, Option<(String, bool)>);

#[pymethods]
impl NetBudget {
    #[new]
    fn new(
        soft: Option<i64>,
        hard: Option<i64>,
        predownload_share: f64,
        warning_share: f64,
        repeat_s: f64,
    ) -> Self {
        Self {
            soft,
            hard,
            predownload_share,
            warning_share,
            repeat_s,
            warned: 0.0,
            warned_over: 0.0,
            half_rate: false,
        }
    }

    /// Returns a rate change, a warning to log, and a rejection with whether it increments the budget counter.
    fn check(&mut self, used: i64, kind: &str, now: f64) -> BudgetDecision {
        let ahead = kind == "predownload";
        let fraction = (100.0 * self.predownload_share).round_ties_even();
        if let Some(hard) = self.hard {
            if used >= hard {
                return (
                    false,
                    None,
                    Some((format!("daily limit of {hard} requests reached"), true)),
                );
            }
            if ahead && used as f64 >= self.predownload_share * hard as f64 {
                return (
                    false,
                    None,
                    Some((
                        format!("past {fraction} % of the daily limit (downloads ahead)"),
                        false,
                    )),
                );
            }
        }
        let Some(soft) = self.soft else {
            return (false, None, None);
        };
        let warn_at = used as f64 >= self.warning_share * soft as f64;
        let halve = used >= soft && !self.half_rate;
        let mut warning = None;
        if used >= soft {
            self.half_rate = true;
            if self.warned_over == 0.0 || now - self.warned_over >= self.repeat_s {
                self.warned = now;
                self.warned_over = now;
                warning = Some("over");
            }
        } else if warn_at && self.warned == 0.0 {
            self.warned = now;
            warning = Some("near");
        }
        let refusal = if ahead && used as f64 >= self.predownload_share * soft as f64 {
            Some((
                format!("past {fraction} % of the daily budget (downloads ahead)"),
                false,
            ))
        } else if warn_at && !matches!(kind, "demand" | "urgent") {
            let percent = (100.0 * self.warning_share).round_ties_even();
            Some((format!("past {percent} % of the daily budget"), true))
        } else {
            None
        };
        (halve, warning, refusal)
    }

    fn reset(&mut self) -> bool {
        self.warned = 0.0;
        self.warned_over = 0.0;
        std::mem::take(&mut self.half_rate)
    }
}
