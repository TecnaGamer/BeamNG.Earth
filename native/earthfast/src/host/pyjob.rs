//! The PyJob executor (the Rust port's phase 2, plan section 2.2): Python functions run on threads Rust owns, while
//! Python remains. Rust code (phase 2b's far state machine) submits a call with a completion it handles in Rust
//! (`PyJobs::submit_rust`), instead of waiting on asyncio futures; Python submits as to a concurrent.futures executor
//! and gets a concurrent.futures.Future (`PyJobs.submit`), so the server's pools can move over one by one.
//!
//! Jobs run in submission order, one per thread at a time. A worker holds the GIL only while it runs a job; it waits
//! for work without it. Threads start as jobs come (concurrent.futures's rule: a new thread only when none is idle, up
//! to the pool's size), so they inherit the submitting thread's priority and CPU set as the executor's threads did.
//! Once the interpreter is finalizing a worker drops what it has without touching Python (Python::try_attach); the
//! workers end after the jobs left once the executor is shut down (`shutdown`) or collected.
use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

/// What happens with a job's result.
pub enum Done {
    /// a concurrent.futures.Future, completed by the worker under the GIL
    Future(Py<PyAny>),
    /// Rust's own completion, called by the worker under the GIL
    Rust(Box<dyn for<'py> FnOnce(Python<'py>, PyResult<Bound<'py, PyAny>>) + Send>),
}

struct Job {
    /// its place in the submissions (a failed spawn takes back its own job)
    id: u64,
    f: Py<PyAny>,
    args: Py<PyTuple>,
    done: Done,
}

#[derive(Default)]
struct State {
    jobs: VecDeque<Job>,
    idle: usize,
    next_id: u64,
    started: usize,
    stop: bool,
}

struct Core {
    st: Mutex<State>,
    cv: Condvar,
    size: usize,
    name: String,
    threads: Mutex<Vec<JoinHandle<()>>>,
}

impl Core {
    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.st.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn push(self: &Arc<Self>, mut job: Job) -> PyResult<()> {
        let id;
        let spawn = {
            let mut st = self.state();
            if st.stop {
                drop(st);
                // (the job's references dropped under the GIL: the caller holds it)
                drop(job);
                return Err(PyRuntimeError::new_err("cannot schedule new futures after shutdown"));
            }
            id = st.next_id;
            st.next_id += 1;
            job.id = id;
            st.jobs.push_back(job);
            let spawn = st.idle < st.jobs.len() && st.started < self.size;
            if spawn {
                st.started += 1;
            }
            spawn.then_some(st.started)
        };
        self.cv.notify_one();
        if let Some(n) = spawn {
            let core = self.clone();
            match std::thread::Builder::new().name(format!("{}_{}", self.name, n - 1)).spawn(move || core.work()) {
                Ok(h) => self.threads.lock().unwrap_or_else(|e| e.into_inner()).push(h),
                Err(e) => {
                    // (its place given back so that the next submission starts one; with another worker the job is
                    // accepted (that worker runs it), with none it is taken back out and refused: never both refused
                    // and run; Codex port2a r1, r2)
                    let mut st = self.state();
                    st.started -= 1;
                    if st.started > 0 {
                        return Ok(());
                    }
                    let mine = st.jobs.iter().position(|j| j.id == id).and_then(|i| st.jobs.remove(i));
                    drop(st);
                    drop(mine);
                    return Err(PyRuntimeError::new_err(format!("{}: {e}", self.name)));
                }
            }
        }
        Ok(())
    }

    fn work(&self) {
        loop {
            let job = {
                let mut st = self.state();
                loop {
                    if let Some(j) = st.jobs.pop_front() {
                        break j;
                    }
                    if st.stop {
                        return;
                    }
                    st.idle += 1;
                    st = self.cv.wait(st).unwrap_or_else(|e| e.into_inner());
                    st.idle -= 1;
                }
            };
            let mut slot = Some(job);
            let ran = Python::try_attach(|py| {
                if let Some(j) = slot.take() {
                    run(py, j);
                }
            });
            if ran.is_none() {
                // the interpreter is finalizing: nothing of Python touched again (its references leaked)
                std::mem::forget(slot.take());
                let mut st = self.state();
                st.stop = true;
                std::mem::forget(std::mem::take(&mut st.jobs));
                return;
            }
        }
    }
}

fn run(py: Python<'_>, j: Job) {
    let Job { f, args, done, .. } = j;
    match done {
        Done::Future(fut) => {
            let fut = fut.bind(py);
            // (a future cancelled while it waited: not run, as concurrent.futures's _WorkItem)
            match fut.call_method0("set_running_or_notify_cancel").and_then(|r| r.is_truthy()) {
                Ok(true) => {}
                Ok(false) => return,
                Err(e) => {
                    e.write_unraisable(py, Some(fut));
                    return;
                }
            }
            let r = match f.bind(py).call1(args.bind(py)) {
                Ok(v) => fut.call_method1("set_result", (v,)),
                Err(e) => fut.call_method1("set_exception", (e.value(py),)),
            };
            if let Err(e) = r {
                e.write_unraisable(py, Some(fut));
            }
        }
        Done::Rust(cb) => {
            let r = f.bind(py).call1(args.bind(py));
            cb(py, r);
        }
    }
}

/// PyJobs(size, name): an executor of `size` threads named name_N.
#[pyclass(module = "earthfast", frozen)]
pub struct PyJobs {
    core: Arc<Core>,
}

impl PyJobs {
    /// Rust's submission: f(*args) on a worker, then done(py, result) there under the GIL.
    #[allow(dead_code)]
    pub fn submit_rust(&self, f: Py<PyAny>, args: Py<PyTuple>,
                       done: Box<dyn for<'py> FnOnce(Python<'py>, PyResult<Bound<'py, PyAny>>) + Send>) -> PyResult<()> {
        self.core.push(Job { id: 0, f, args, done: Done::Rust(done) })
    }
}

#[pymethods]
impl PyJobs {
    #[new]
    #[pyo3(signature = (size, name))]
    fn new(size: usize, name: &str) -> PyResult<Self> {
        if size == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err("size must be greater than 0"));
        }
        Ok(PyJobs {
            core: Arc::new(Core { st: Mutex::new(State::default()), cv: Condvar::new(), size, name: name.to_string(),
                                  threads: Mutex::new(Vec::new()) }),
        })
    }

    /// fn(*args) on a worker: a concurrent.futures.Future of its result.
    #[pyo3(signature = (f, *args))]
    fn submit<'py>(&self, py: Python<'py>, f: Py<PyAny>, args: Py<PyTuple>) -> PyResult<Bound<'py, PyAny>> {
        let fut = py.import("concurrent.futures")?.getattr("Future")?.call0()?;
        self.core.push(Job { id: 0, f, args, done: Done::Future(fut.clone().unbind()) })?;
        Ok(fut)
    }

    /// Jobs waiting for a worker.
    fn pending(&self) -> usize {
        self.core.state().jobs.len()
    }

    /// Threads started so far.
    fn threads(&self) -> usize {
        self.core.state().started
    }

    /// No new jobs; cancel_futures: the waiting ones' futures cancelled; wait: the threads joined once the jobs under
    /// way and those still waiting are done (as ThreadPoolExecutor.shutdown).
    #[pyo3(signature = (wait = true, cancel_futures = false))]
    fn shutdown(&self, py: Python<'_>, wait: bool, cancel_futures: bool) -> PyResult<()> {
        let dropped = {
            let mut st = self.core.state();
            st.stop = true;
            if cancel_futures { std::mem::take(&mut st.jobs) } else { VecDeque::new() }
        };
        self.core.cv.notify_all();
        for j in dropped {
            if let Done::Future(fut) = &j.done {
                fut.bind(py).call_method0("cancel")?;
            }
        }
        if wait {
            let hs = std::mem::take(&mut *self.core.threads.lock().unwrap_or_else(|e| e.into_inner()));
            py.detach(|| {
                for h in hs {
                    let _ = h.join();
                }
            });
        }
        Ok(())
    }
}

impl Drop for PyJobs {
    /// The executor collected (a Server of the tests gone): its workers end once the jobs left are done, as
    /// ThreadPoolExecutor's do (not joined here: a worker may be the one dropping it).
    fn drop(&mut self) {
        self.core.state().stop = true;
        self.core.cv.notify_all();
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyJobs>()
}
