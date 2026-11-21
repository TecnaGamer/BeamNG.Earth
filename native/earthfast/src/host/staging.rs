//! Temporary files stay on the game's filesystem, outside its watched user directory.
use std::collections::BTreeSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, SystemTime};

use pyo3::exceptions::{PyFileNotFoundError, PyPermissionError};
use pyo3::prelude::*;
use pyo3::types::PyString;

static MADE: LazyLock<Mutex<BTreeSet<PathBuf>>> = LazyLock::new(Default::default);
static REMADE: LazyLock<Mutex<BTreeSet<PathBuf>>> = LazyLock::new(Default::default);
static SERIAL: AtomicU64 = AtomicU64::new(0);

fn directory(path: &Path) -> Option<PathBuf> {
    // Python used the last /temp/ component and a case-sensitive comparison on Windows.
    let temp = path
        .ancestors()
        .skip(1)
        .find(|p| p.file_name().is_some_and(|n| n == "temp"))?;
    Some(temp.parent()?.parent()?.join("beamngEarth_staging"))
}

fn tmp_for(path: &Path, absolute: &Path, tid: u64) -> PathBuf {
    if let Some(dir) = directory(absolute) {
        let mut made = MADE.lock().unwrap();
        if made.contains(&dir) || (fs::create_dir_all(&dir).is_ok() && sweep(&dir).is_ok()) {
            made.insert(dir.clone());
            let mut name = std::ffi::OsString::from(format!(
                "{}_{tid}_{}_",
                std::process::id(),
                SERIAL.fetch_add(1, Ordering::Relaxed)
            ));
            name.push(path.file_name().unwrap_or_default());
            name.push(".tmp");
            return dir.join(name);
        }
    }
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    PathBuf::from(tmp)
}

fn sweep(dir: &Path) -> io::Result<()> {
    let now = SystemTime::now();
    for entry in dir.read_dir()?.flatten() {
        let path = entry.path();
        if path.extension().is_some_and(|ext| ext == "tmp")
            && path
                .metadata()
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| now.duration_since(t).ok())
                .is_some_and(|age| age > Duration::from_secs(3600))
        {
            let _ = fs::remove_file(path);
        }
    }
    Ok(())
}

#[pyfunction]
fn staging_dir<'py>(py: Python<'py>, absolute: PathBuf) -> PyResult<Option<Bound<'py, PyString>>> {
    Ok(directory(&absolute)
        .map(|p| p.as_os_str().into_pyobject(py))
        .transpose()?)
}

#[pyfunction]
fn staging_tmp<'py>(
    py: Python<'py>,
    path: PathBuf,
    absolute: PathBuf,
    tid: u64,
) -> PyResult<Bound<'py, PyString>> {
    Ok(py
        .detach(|| tmp_for(&path, &absolute, tid))
        .as_os_str()
        .into_pyobject(py)?)
}

#[pyfunction]
fn staging_warm(py: Python<'_>, path: PathBuf) {
    py.detach(|| {
        if let Ok(mut f) = fs::File::open(path) {
            let _ = io::copy(&mut f, &mut io::sink());
        }
    });
}

fn rename_once(
    py: Python<'_>,
    tmp: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
    rename: &Bound<'_, PyAny>,
    log: &Bound<'_, PyAny>,
) -> PyResult<()> {
    match rename.call1((tmp, path)) {
        Err(e) if e.is_instance_of::<PyFileNotFoundError>(py) => {
            let src: PathBuf = tmp.extract()?;
            let dst: PathBuf = path.extract()?;
            if !src.exists() {
                return Err(e);
            }
            let parent = dst.parent().unwrap_or(Path::new(""));
            fs::create_dir_all(parent)?;
            if REMADE.lock().unwrap().insert(parent.to_path_buf()) {
                log.call_method1(
                    "warning",
                    (
                        "folder %s was deleted while in use (cleared temp folder?): made again",
                        parent.as_os_str(),
                    ),
                )?;
            }
            rename.call1((tmp, path))?;
        }
        r => {
            r?;
        }
    }
    Ok(())
}

/// os.replace carries the platform's errno, filename and winerror; warm/log are the server's hooks.
/// Workers may retry a locked file. A synchronous loop caller keeps its original single attempt.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn staging_replace(
    py: Python<'_>,
    tmp: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
    waits: Vec<f64>,
    cached: bool,
    rename: &Bound<'_, PyAny>,
    warm: Option<&Bound<'_, PyAny>>,
    log: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    for attempt in 0..=waits.len() {
        match rename_once(py, tmp, path, rename, log) {
            Ok(()) => {
                if let Some(f) = warm {
                    f.call1((path,))?;
                }
                if attempt > 0 && !cached {
                    let p: PathBuf = path.extract()?;
                    log.call_method1("info",("%s was held (Windows: the game or a virus scanner): replaced at try %d",
                                             p.file_name().unwrap_or_default(),attempt+1))?;
                }
                return Ok(true);
            }
            Err(e) if e.is_instance_of::<PyPermissionError>(py) => {
                if let Some(&delay) = waits.get(attempt) {
                    py.detach(|| std::thread::sleep(Duration::from_secs_f64(delay)));
                } else if cached && path.extract::<PathBuf>()?.exists() {
                    let _ = fs::remove_file(tmp.extract::<PathBuf>()?);
                    return Ok(false);
                } else {
                    return Err(e);
                }
            }
            Err(e) => return Err(e),
        }
    }
    unreachable!()
}

#[pyfunction]
fn staging_link(py: Python<'_>, src: PathBuf, tmp: PathBuf) -> PyResult<()> {
    py.detach(|| -> io::Result<()> {
        match fs::remove_file(&tmp) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e),
        }
        if fs::hard_link(&src, &tmp).is_err() {
            fs::copy(src, tmp)?;
        }
        Ok(())
    })?;
    Ok(())
}

#[pyfunction]
fn staging_read_held<'py>(
    py: Python<'py>,
    read: &Bound<'py, PyAny>,
    waits: Vec<f64>,
) -> PyResult<Bound<'py, PyAny>> {
    for delay in waits {
        match read.call0() {
            Err(e) if e.is_instance_of::<PyPermissionError>(py) => {
                py.detach(|| std::thread::sleep(Duration::from_secs_f64(delay)))
            }
            r => return r,
        }
    }
    read.call0()
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(staging_dir, m)?)?;
    m.add_function(wrap_pyfunction!(staging_tmp, m)?)?;
    m.add_function(wrap_pyfunction!(staging_warm, m)?)?;
    m.add_function(wrap_pyfunction!(staging_replace, m)?)?;
    m.add_function(wrap_pyfunction!(staging_link, m)?)?;
    m.add_function(wrap_pyfunction!(staging_read_held, m)?)
}
