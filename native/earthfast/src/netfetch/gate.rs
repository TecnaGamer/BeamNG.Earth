//! SharedGate's disk protocol. Python workers from older releases can hold slots and update the same bucket.
use super::{token_wait, TOKEN_EPS};
use pyo3::prelude::*;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

fn open(path: &Path) -> io::Result<File> {
    let mut opts = OpenOptions::new();
    opts.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o644);
    }
    opts.open(path)
}

#[cfg(unix)]
fn lock(file: &File, blocking: bool) -> io::Result<bool> {
    if blocking {
        file.lock()?;
        return Ok(true);
    }
    match file.try_lock() {
        Ok(()) => Ok(true),
        Err(std::fs::TryLockError::WouldBlock) => Ok(false),
        Err(std::fs::TryLockError::Error(e)) => Err(e),
    }
}

#[cfg(windows)]
fn lock(file: &File, blocking: bool) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{
        Foundation::ERROR_LOCK_VIOLATION,
        Storage::FileSystem::{LockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY},
        System::IO::OVERLAPPED,
    };
    let flags = if blocking {
        LOCKFILE_EXCLUSIVE_LOCK
    } else {
        LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY
    };
    // msvcrt.locking(fd, LK_NBLCK, 1) at offset zero. Closing the synchronous file handle releases the lock.
    let mut at: OVERLAPPED = unsafe { std::mem::zeroed() };
    if unsafe { LockFileEx(file.as_raw_handle() as _, flags, 0, 1, 0, &mut at) } != 0 {
        return Ok(true);
    }
    let err = io::Error::last_os_error();
    if !blocking && err.raw_os_error() == Some(ERROR_LOCK_VIOLATION as i32) {
        Ok(false)
    } else {
        Err(err)
    }
}

#[pyclass(frozen)]
pub struct NetSlot {
    file: Mutex<Option<File>>,
}

#[pymethods]
impl NetSlot {
    fn release(&self) {
        self.file.lock().unwrap().take();
    }
}

#[pyclass(frozen)]
pub struct NetGate {
    path: PathBuf,
    slots: Vec<PathBuf>,
    rate: f64,
    burst: f64,
}

#[derive(Clone, Copy)]
enum Operation {
    Blocked,
    Block(f64),
    Take(Option<f64>),
}

impl NetGate {
    fn update(
        &self,
        py: Python<'_>,
        clock: &Bound<'_, PyAny>,
        op: Operation,
    ) -> PyResult<(&'static str, f64)> {
        let (mut file, raw) = py.detach(|| -> io::Result<_> {
            let mut file = open(&self.path)?;
            lock(&file, true)?;
            let mut raw = [0; 24];
            let state = match file.read_exact(&mut raw) {
                Ok(()) => Some(raw),
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => None,
                Err(e) => return Err(e),
            };
            Ok((file, state))
        })?;
        // time.monotonic has a machine-wide epoch on both supported platforms. Take it under the file lock.
        let now: f64 = clock.call0()?.extract()?;
        py.detach(|| {
            let (mut tokens, t, mut blocked) = raw
                .map(|b| {
                    let d = |i| f64::from_le_bytes(b[i..i + 8].try_into().unwrap());
                    (d(0), d(8), d(16))
                })
                .filter(|(_, t, _)| !(*t > now + 1.0))
                .unwrap_or((self.burst, now, 0.0));
            tokens = self.burst.min(tokens + (now - t).max(0.0) * self.rate);
            let result = match op {
                Operation::Blocked => ("blocked", (blocked - now).max(0.0)),
                Operation::Block(seconds) => {
                    if now + seconds > blocked {
                        blocked = now + seconds;
                    }
                    ("blocked", 0.0)
                }
                Operation::Take(_) if blocked > now => ("blocked", blocked - now),
                Operation::Take(_) if tokens >= 1.0 - TOKEN_EPS => {
                    tokens -= 1.0;
                    ("go", 0.0)
                }
                Operation::Take(max_wait) => {
                    let need = token_wait(1.0 - tokens, self.rate)?;
                    if max_wait.is_none_or(|w| need > w) {
                        ("full", need)
                    } else {
                        tokens -= 1.0;
                        ("go", need)
                    }
                }
            };
            let mut buf = [0; 24];
            let next_t = if now > t { now } else { t };
            for (dst, value) in buf.chunks_exact_mut(8).zip([tokens, next_t, blocked]) {
                dst.copy_from_slice(&value.to_le_bytes());
            }
            file.rewind()?;
            file.write_all(&buf)?;
            Ok(result)
        })
    }
}

#[pymethods]
impl NetGate {
    #[new]
    fn new(path: PathBuf, slots: Vec<PathBuf>, rate: f64, burst: f64) -> Self {
        Self {
            path,
            slots,
            rate,
            burst,
        }
    }

    fn slot(&self, py: Python<'_>) -> PyResult<Option<NetSlot>> {
        py.detach(|| {
            for path in &self.slots {
                let file = open(path)?;
                if lock(&file, false)? {
                    return Ok(Some(NetSlot {
                        file: Mutex::new(Some(file)),
                    }));
                }
            }
            Ok(None)
        })
    }

    fn blocked(&self, py: Python<'_>, clock: &Bound<'_, PyAny>) -> PyResult<f64> {
        Ok(self.update(py, clock, Operation::Blocked)?.1)
    }

    fn block(&self, py: Python<'_>, clock: &Bound<'_, PyAny>, seconds: f64) -> PyResult<()> {
        self.update(py, clock, Operation::Block(seconds))?;
        Ok(())
    }

    fn take(
        &self,
        py: Python<'_>,
        clock: &Bound<'_, PyAny>,
        max_wait: Option<f64>,
    ) -> PyResult<(&'static str, f64)> {
        self.update(py, clock, Operation::Take(max_wait))
    }
}
