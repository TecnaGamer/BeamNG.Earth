//! Messages made in Rust (the Rust port's phase 2a, plan section 3 "Phase 2"): the far field's far_tile, far_swap,
//! far_remove and far_done, the status line and the loading line (load_progress). Each builder gives the message as a
//! `J` (fields in the order the mod's protocol table and the Python server always sent them; values rounded as
//! Python's round did, pyjson.rs), handed to Python's Connection.send as a dict now (phase 2a: its lease, closing and
//! buffer checks unchanged) and to the Wire as bytes once the far state machine is Rust's (phase 2b).
//!
//! The values that come from Python's objects (a tile's key and level, a frame's coordinates, a status text) pass
//! through as they are, so their JSON is json.dumps's own; the rounded floats are rounded here for a float and by
//! Python's round for anything else (an int stays an int, numpy's float64 keeps its own __round__).
use pyo3::prelude::*;

use super::pyjson::{round0_obj, round_obj, J};

fn rnd(v: &Bound<'_, PyAny>, n: usize) -> PyResult<J> {
    Ok(J::of(&round_obj(v, n)?))
}

fn rnd_list(v: &Bound<'_, PyAny>, n: usize) -> PyResult<J> {
    let mut a = Vec::new();
    for x in v.try_iter()? {
        a.push(rnd(&x?, n)?);
    }
    Ok(J::A(a))
}

fn list(v: &Bound<'_, PyAny>) -> PyResult<J> {
    let mut a = Vec::new();
    for x in v.try_iter()? {
        a.push(J::of(&x?));
    }
    Ok(J::A(a))
}

fn msg(kind: &'static str, mut fields: Vec<(&'static str, J)>) -> J {
    fields.insert(0, ("type", J::S(kind.to_string())));
    J::O(fields)
}

/// A built far tile (Connection._stream). pos: the tile's pivot (x, y, z); frame: a global tile's [lat, lon] (then pos
/// is {0, 0, z} in it and poly its quad); poly: a piece's footprint; slot_serial: a pooled shape's send serial;
/// material / texture / tex: protocol 4's imagery (tex: FORMAT 4's texture px, None before).
#[allow(clippy::too_many_arguments)]
pub fn far_tile(anchor_id: &Bound<'_, PyAny>, key: &Bound<'_, PyAny>, level: &Bound<'_, PyAny>, shape: &Bound<'_, PyAny>,
                pos: &Bound<'_, PyAny>, err: &Bound<'_, PyAny>, skirt: &Bound<'_, PyAny>, sink: Option<&Bound<'_, PyAny>>,
                frame: Option<&Bound<'_, PyAny>>, poly: Option<&Bound<'_, PyAny>>, slot_serial: Option<&Bound<'_, PyAny>>,
                material: Option<&Bound<'_, PyAny>>, texture: Option<&Bound<'_, PyAny>>, tex: Option<&Bound<'_, PyAny>>)
    -> PyResult<J> {
    let mut f = vec![
        ("anchorId", J::of(anchor_id)),
        ("key", J::of(key)),
        ("level", J::of(level)),
        ("shape", J::of(shape)),
        ("pos", J::Null),
        ("err", rnd(err, 3)?),
        ("skirt", rnd(skirt, 2)?),
        ("state", J::S("group".into())),
    ];
    if let Some(s) = sink {
        f.push(("sink", rnd(s, 2)?));
    }
    match (frame, poly) {
        (Some(fr), Some(q)) => {
            // geotiles phase 3b: a global tile, placed by the mod from its own frame
            f.push(("frame", J::A(vec![J::of(&fr.get_item(0)?), J::of(&fr.get_item(1)?)])));
            f[4].1 = J::A(vec![J::Int(0), J::Int(0), rnd(&pos.get_item(2)?, 3)?]);
            f.push(("poly", rnd_list(q, 2)?));
        }
        (None, Some(q)) => {
            f[4].1 = rnd_list(pos, 3)?;
            f.push(("poly", rnd_list(q, 2)?));
        }
        _ => f[4].1 = rnd_list(pos, 3)?,
    }
    if let Some(s) = slot_serial {
        f.push(("slotSerial", J::of(s)));
    }
    if let Some(m) = material {
        f.push(("material", J::of(m)));
        f.push(("texture", texture.map_or(J::Null, J::of)));
        f.push(("detail", J::Bool(true)));
        if let Some(t) = tex {
            f.push(("tex", J::of(t)));
        }
    }
    Ok(msg("far_tile", f))
}

/// A swap group whose adds are all sent (Connection.emit_groups): its deltas rounded to cm.
pub fn far_swap(anchor_id: &Bound<'_, PyAny>, gid: &Bound<'_, PyAny>, adds: &Bound<'_, PyAny>,
                removes: &Bound<'_, PyAny>, d_in: &Bound<'_, PyAny>, d_out: &Bound<'_, PyAny>) -> PyResult<J> {
    Ok(msg("far_swap", vec![("anchorId", J::of(anchor_id)), ("id", J::of(gid)), ("add", J::of(adds)),
                            ("remove", J::of(removes)), ("dIn", rnd(d_in, 2)?), ("dOut", rnd(d_out, 2)?)]))
}

/// The status line (Connection.status): the network note after the text (offline / degraded); radius: the far field's
/// (--horizon only), whole metres.
pub fn status(text: &Bound<'_, PyAny>, note: &Bound<'_, PyAny>, backlog: &Bound<'_, PyAny>,
              radius: Option<&Bound<'_, PyAny>>) -> PyResult<J> {
    let text = if note.is_truthy()? {
        if text.is_truthy()? {
            J::S(format!("{} | {}", text.str()?.to_str()?, note.str()?.to_str()?))
        } else {
            J::of(note)
        }
    } else {
        J::of(text)
    };
    let mut f = vec![("text", text), ("backlog", J::of(backlog))];
    if let Some(r) = radius {
        f.push(("radius", J::of(&round0_obj(r)?)));
    }
    Ok(msg("status", f))
}

/// The loading line's counts (Connection.send_progress): (done, wanted) pairs and the downloads in flight.
pub fn load_progress(anchor_id: &Bound<'_, PyAny>, ground: &Bound<'_, PyAny>, roads: &Bound<'_, PyAny>,
                     trees: &Bound<'_, PyAny>, far: &Bound<'_, PyAny>, net: &Bound<'_, PyAny>) -> PyResult<J> {
    Ok(msg("load_progress", vec![("anchorId", J::of(anchor_id)), ("ground", list(ground)?), ("roads", list(roads)?),
                                 ("trees", list(trees)?), ("far", list(far)?), ("net", list(net)?)]))
}

#[pyfunction]
#[pyo3(name = "msg_far_tile", signature = (anchor_id, key, level, shape, pos, err, skirt, sink = None, frame = None,
                                            poly = None, slot_serial = None, material = None, texture = None, tex = None))]
#[allow(clippy::too_many_arguments)]
fn py_far_tile<'py>(py: Python<'py>, anchor_id: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>, level: &Bound<'py, PyAny>,
                    shape: &Bound<'py, PyAny>, pos: &Bound<'py, PyAny>, err: &Bound<'py, PyAny>,
                    skirt: &Bound<'py, PyAny>, sink: Option<&Bound<'py, PyAny>>, frame: Option<&Bound<'py, PyAny>>,
                    poly: Option<&Bound<'py, PyAny>>, slot_serial: Option<&Bound<'py, PyAny>>,
                    material: Option<&Bound<'py, PyAny>>, texture: Option<&Bound<'py, PyAny>>,
                    tex: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    far_tile(anchor_id, key, level, shape, pos, err, skirt, sink, frame, poly, slot_serial, material, texture, tex)?
        .to_py(py)
}

#[pyfunction]
#[pyo3(name = "msg_far_swap")]
fn py_far_swap<'py>(py: Python<'py>, anchor_id: &Bound<'py, PyAny>, gid: &Bound<'py, PyAny>, adds: &Bound<'py, PyAny>,
                    removes: &Bound<'py, PyAny>, d_in: &Bound<'py, PyAny>, d_out: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    far_swap(anchor_id, gid, adds, removes, d_in, d_out)?.to_py(py)
}

/// far_remove: a shown tile goes (a previous anchor's, or a stale one).
#[pyfunction]
#[pyo3(name = "msg_far_remove")]
fn py_far_remove<'py>(py: Python<'py>, anchor_id: &Bound<'py, PyAny>, key: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    msg("far_remove", vec![("anchorId", J::of(anchor_id)), ("key", J::of(key))]).to_py(py)
}

/// far_done: an anchor's far field is whole (count: its tiles shown).
#[pyfunction]
#[pyo3(name = "msg_far_done")]
fn py_far_done<'py>(py: Python<'py>, anchor_id: &Bound<'py, PyAny>, count: &Bound<'py, PyAny>)
    -> PyResult<Bound<'py, PyAny>> {
    msg("far_done", vec![("anchorId", J::of(anchor_id)), ("count", J::of(count))]).to_py(py)
}

#[pyfunction]
#[pyo3(name = "msg_status", signature = (text, note, backlog, radius = None))]
fn py_status<'py>(py: Python<'py>, text: &Bound<'py, PyAny>, note: &Bound<'py, PyAny>, backlog: &Bound<'py, PyAny>,
                  radius: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    status(text, note, backlog, radius)?.to_py(py)
}

#[pyfunction]
#[pyo3(name = "msg_load_progress")]
fn py_load_progress<'py>(py: Python<'py>, anchor_id: &Bound<'py, PyAny>, ground: &Bound<'py, PyAny>,
                         roads: &Bound<'py, PyAny>, trees: &Bound<'py, PyAny>, far: &Bound<'py, PyAny>,
                         net: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    load_progress(anchor_id, ground, roads, trees, far, net)?.to_py(py)
}

/// The builders' field names in order, by message type (test_pyjson checks them against server/protocol_table.json).
#[pyfunction]
fn msg_fields() -> Vec<(String, Vec<String>)> {
    [
        ("far_tile", &["type", "anchorId", "key", "level", "shape", "pos", "err", "skirt", "state", "sink", "frame",
                       "poly", "slotSerial", "material", "texture", "detail", "tex"][..]),
        ("far_swap", &["type", "anchorId", "id", "add", "remove", "dIn", "dOut"][..]),
        ("far_remove", &["type", "anchorId", "key"][..]),
        ("far_done", &["type", "anchorId", "count"][..]),
        ("status", &["type", "text", "backlog", "radius"][..]),
        ("load_progress", &["type", "anchorId", "ground", "roads", "trees", "far", "net"][..]),
    ]
    .iter()
    .map(|(k, f)| (k.to_string(), f.iter().map(|s| s.to_string()).collect()))
    .collect()
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_far_tile, m)?)?;
    m.add_function(wrap_pyfunction!(py_far_swap, m)?)?;
    m.add_function(wrap_pyfunction!(py_far_remove, m)?)?;
    m.add_function(wrap_pyfunction!(py_far_done, m)?)?;
    m.add_function(wrap_pyfunction!(py_status, m)?)?;
    m.add_function(wrap_pyfunction!(py_load_progress, m)?)?;
    m.add_function(wrap_pyfunction!(msg_fields, m)?)?;
    Ok(())
}
