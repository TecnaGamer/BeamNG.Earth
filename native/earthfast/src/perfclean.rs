//! The anonymous performance data's checks (server/perfstats.py, TELEMETRY.md sections 2-5): what the mod sends
//! (perfsummary.lua's perf_setup / perf_summary) is rebuilt here from the allowed shapes only, so neither a mod bug nor
//! a changed mod can put a position, a path or free text into a report:
//!   numbers (finite, |v| < 1e12) and booleans; lists of numbers (at most MAX_LIST); tables under plain names
//!   (NAME: 1-32 of A-Z a-z 0-9 _ + . : -, at most MAX_ITEMS) of those, at most MAX_DEPTH deep (a speed band's frame histogram: 4); strings (printable,
//!   trimmed, at most MAX_STR characters) only where allowed (the setup's model names and settings values).
//! Also: rounding to steps, the server process tree's CPU seconds and resident memory (Linux /proc), and the error
//! fingerprints (a hash of the exception type and the logging call's module and line: never a message or a value).

use blake2::{Blake2s256, Digest};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

const MAX_DEPTH: usize = 4;
const MAX_ITEMS: usize = 48;
const MAX_LIST: usize = 16;
const MAX_STR: usize = 80;

fn plain_name(k: &str) -> bool {
    !k.is_empty()
        && k.len() <= 32
        && k.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'+' | b'.' | b':' | b'-'))
}

fn clean_str(s: &str) -> Option<String> {
    let t: String = s.chars().filter(|c| !c.is_control()).collect();
    let t: String = t.trim().chars().take(MAX_STR).collect();
    let t = t.trim().to_string();
    if t.is_empty() { None } else { Some(t) }
}

fn number(v: &Bound<'_, PyAny>) -> Option<f64> {
    if v.is_instance_of::<PyBool>() {
        return None;
    }
    if v.is_instance_of::<PyInt>() || v.is_instance_of::<PyFloat>() {
        let f: f64 = v.extract().ok()?;
        if f.is_finite() && f.abs() < 1e12 {
            return Some(f);
        }
    }
    None
}

fn clean<'py>(py: Python<'py>, v: &Bound<'py, PyAny>, depth: usize, strings: bool) -> PyResult<Option<PyObject>> {
    if v.is_instance_of::<PyBool>() {
        return Ok(Some(v.clone().unbind()));
    }
    if v.is_instance_of::<PyInt>() || v.is_instance_of::<PyFloat>() {
        return Ok(number(v).map(|_| v.clone().unbind()));
    }
    if let Ok(s) = v.downcast::<PyString>() {
        if !strings {
            return Ok(None);
        }
        let s: String = s.to_str()?.to_string();
        return Ok(clean_str(&s).map(|t| PyString::new(py, &t).into_any().unbind()));
    }
    if depth >= MAX_DEPTH {
        return Ok(None);
    }
    if let Ok(list) = v.downcast::<PyList>() {
        let out = PyList::empty(py);
        for x in list.iter().take(MAX_LIST) {
            if number(&x).is_some() {
                out.append(x)?;
            }
        }
        return Ok(Some(out.into_any().unbind()));
    }
    if let Ok(d) = v.downcast::<PyDict>() {
        let out = PyDict::new(py);
        for (k, x) in d.iter().take(MAX_ITEMS) {
            let Ok(ks) = k.downcast::<PyString>() else { continue };
            let key = ks.to_str()?;
            if !plain_name(key) {
                continue;
            }
            if let Some(c) = clean(py, &x, depth + 1, strings)? {
                out.set_item(key, c)?;
            }
        }
        return Ok(Some(out.into_any().unbind()));
    }
    Ok(None)
}

/// v as a report may carry it, or None (the module's rules); strings: the setup's model names and settings values.
/// allowed: {name: None | [name, ...]}: v's names must be among its keys (anything else is dropped), and where a key
/// maps to a list, the table under it may only have those names (the settings tables: fixed lists in the mod).
#[pyfunction]
#[pyo3(signature = (v, strings=false, allowed=None))]
pub fn perf_clean(py: Python<'_>, v: &Bound<'_, PyAny>, strings: bool, allowed: Option<&Bound<'_, PyDict>>)
    -> PyResult<Option<PyObject>> {
    let Some(allowed) = allowed else { return clean(py, v, 0, strings) };
    let Ok(d) = v.downcast::<PyDict>() else { return Ok(None) };
    let out = PyDict::new(py);
    for (k, x) in d.iter().take(MAX_ITEMS) {
        let Ok(ks) = k.downcast::<PyString>() else { continue };
        let key = ks.to_str()?;
        let Some(rule) = allowed.get_item(key)? else { continue };
        let x = if let Ok(names) = rule.downcast::<PyList>() {
            let Ok(sub) = x.downcast::<PyDict>() else { continue };
            let kept = PyDict::new(py);
            for n in names.iter() {
                if let Ok(ns) = n.downcast::<PyString>() {
                    if let Some(val) = sub.get_item(ns)? {
                        kept.set_item(ns, val)?;
                    }
                }
            }
            kept.into_any()
        } else {
            x
        };
        if let Some(c) = clean(py, &x, 1, strings)? {
            out.set_item(key, c)?;
        }
    }
    Ok(Some(out.into_any().unbind()))
}

/// The largest step not above v (steps ascending; the first when v is below them all).
#[pyfunction]
pub fn perf_step(v: f64, steps: Vec<f64>) -> f64 {
    let mut out = steps.first().copied().unwrap_or(0.0);
    for s in steps {
        if v >= s {
            out = s;
        }
    }
    out
}

/// An error's fingerprint: 8 hex characters of a hash of "kind:module:line".
#[pyfunction]
pub fn perf_fingerprint(kind: &str, module: &str, line: i64) -> String {
    let mut h = Blake2s256::new();
    h.update(format!("{kind}:{module}:{line}").as_bytes());
    h.finalize().iter().take(4).map(|b| format!("{b:02x}")).collect()
}

#[cfg(target_os = "linux")]
fn tree(root: u32) -> Option<(f64, f64)> {
    use std::collections::HashMap;
    let tick = unsafe { libc_sysconf(2) } as f64; // _SC_CLK_TCK
    let page = unsafe { libc_sysconf(30) } as f64; // _SC_PAGESIZE
    if tick <= 0.0 || page <= 0.0 {
        return None;
    }
    let mut kids: HashMap<u32, Vec<u32>> = HashMap::new();
    let mut stat: HashMap<u32, (f64, f64)> = HashMap::new();
    for e in std::fs::read_dir("/proc").ok()?.flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else { continue };
        let Ok(text) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else { continue };
        let Some(rest) = text.rsplit_once(')').map(|x| x.1) else { continue };
        let f: Vec<&str> = rest.split_whitespace().collect();
        if f.len() < 22 {
            continue;
        }
        let (Ok(ppid), Ok(ut), Ok(stm), Ok(rss)) =
            (f[1].parse::<u32>(), f[11].parse::<f64>(), f[12].parse::<f64>(), f[21].parse::<f64>())
        else {
            continue;
        };
        kids.entry(ppid).or_default().push(pid);
        stat.insert(pid, ((ut + stm) / tick, rss * page / 1e6));
    }
    let (mut cpu, mut mem) = (0.0, 0.0);
    let mut todo = vec![root];
    let mut seen = std::collections::HashSet::new();
    while let Some(p) = todo.pop() {
        if !seen.insert(p) {
            continue;
        }
        if let Some((c, m)) = stat.get(&p) {
            cpu += c;
            mem += m;
        }
        if let Some(k) = kids.get(&p) {
            todo.extend(k.iter().copied());
        }
    }
    Some((cpu, mem))
}

#[cfg(target_os = "linux")]
extern "C" {
    #[link_name = "sysconf"]
    fn libc_sysconf(name: i32) -> i64;
}

#[cfg(not(target_os = "linux"))]
fn tree(_root: u32) -> Option<(f64, f64)> {
    None
}

/// (CPU seconds, resident MB) of process root and every process under it, or None (not Linux, or /proc unreadable).
#[pyfunction]
pub fn proc_tree(py: Python<'_>, root: u32) -> Option<(f64, f64)> {
    py.allow_threads(|| tree(root))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_steps() {
        assert!(plain_name("s100") && plain_name("mod:sea+trees") && !plain_name("a b") && !plain_name(""));
        assert!(!plain_name(&"x".repeat(33)) && !plain_name("C:\\Users"));
        assert_eq!(perf_step(700.0, vec![0.0, 512.0, 1024.0]), 512.0);
        assert_eq!(perf_step(-1.0, vec![0.0, 512.0]), 0.0);
        assert_eq!(perf_fingerprint("KeyError", "earthd", 12).len(), 8);
        assert_eq!(clean_str("  AMD\u{0}  Ryzen ").as_deref(), Some("AMD  Ryzen"));
        assert_eq!(clean_str(" \n "), None);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn own_tree() {
        let (cpu, mem) = tree(std::process::id()).unwrap();
        assert!(cpu >= 0.0 && mem > 0.0);
    }
}
