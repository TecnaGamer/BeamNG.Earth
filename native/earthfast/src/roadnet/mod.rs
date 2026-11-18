//! The road network's spec inputs in Rust (the road revamp's R2a: a byte-identical port of the Python glue that reads a
//! cell's road layer into the spec kernels' inputs; .claude/work/road-revamp-plan-2026-11-16.md section R2). The plan
//! names it `earthcore::roadnet::specs`; earthfast is the crate today, so it lives here as `roadnet`.
//!
//! Each piece replaced a Python function bit for bit (no format bump): the old Python is kept only inside the parity
//! tests (server/tests/test_roadnet_*.py), never as a fallback (fast.require).
//!
//! Input contract (from piece 7 on, roadnet::widthlanes; the coordinator's call after Codex round 3 on bbfe23a2): a
//! kernel matches the Python for the inputs the pipeline makes, and refuses the rest with a TypeError before it reads
//! a row (pyobj::Contract) instead of following every object protocol a Python value could override. Columns: None,
//! exact lists / tuples, exact 1-D numpy arrays of a number / text dtype or of objects; items: None, exact bool /
//! int / float / str, exact numpy scalars; dicts exact with str keys. The facade's docstring states its own; the
//! parity tests assert the TypeError for subclasses and proxies, and the real-data checks hold the rest.

pub mod assembly;
pub mod columns;
pub mod copies;
pub mod flowhome;
pub mod countries;
pub mod eustreets;
pub mod laneflow;
pub mod levelkinds;
pub mod lanesplit;
pub mod pyfloat;
pub mod pyobj;
pub mod regions;
pub mod rings;
pub mod specjson;
pub mod splitlayer;
pub mod surfaces;
pub mod verdicts;
pub mod widthlanes;
