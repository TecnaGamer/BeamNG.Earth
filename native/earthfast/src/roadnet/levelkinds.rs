//! _bridge_specs' "has a range of that kind" mask (roads._has_kind) in Rust, byte for byte the Python loop it replaced.
//! The level lists themselves (_level_lists' home copies and neighbour reads, _raw_all's memo: the mapdata hook's
//! orchestration) stay Python and are handed in.

use numpy::{IntoPyArray, PyArray1};
use pyo3::basic::CompareOp;
use pyo3::prelude::*;

use super::countries::np_index;
use super::pyobj::{in_generator, unpack};

/// The mask _bridge_specs read (n bools): for each road i of elig, in order, has[i] = any(k == want for _, _, k in
/// lv[i]): lv[i] and its iterator asked first, outside the generator (a dict's KeyError, a list's IndexError or
/// negative index); in the generator each range unpacked (exactly three values) and its kind compared (`==`), a
/// StopIteration from either raised as the generator's RuntimeError; the comparison's truth taken by any(), outside
/// it, up to the first match; then has[i] stored (numpy's index rules: a negative i from the end, IndexError past
/// either end).
#[pyfunction]
pub fn level_kind_mask<'py>(py: Python<'py>, lv: Bound<'py, PyAny>, elig: Vec<i64>, want: Bound<'py, PyAny>,
                            n: usize) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let mut has = vec![false; n];
    for &i in &elig {
        let mut hit = false;
        for r in lv.get_item(i)?.try_iter()? {
            let r = r?;
            let eq = in_generator(py, unpack(&r, 3).and_then(|t| t[2].rich_compare(&want, CompareOp::Eq)))?;
            if eq.is_truthy()? {
                hit = true;
                break;
            }
        }
        has[np_index(n, i)?] = hit;
    }
    Ok(has.into_pyarray(py))
}
