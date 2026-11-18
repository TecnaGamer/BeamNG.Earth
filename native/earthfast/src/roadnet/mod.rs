//! The road network's spec inputs in Rust (the road revamp's R2a: a byte-identical port of the Python glue that reads a
//! cell's road layer into the spec kernels' inputs; .claude/work/road-revamp-plan-2026-11-16.md section R2). The plan
//! names it `earthcore::roadnet::specs`; earthfast is the crate today, so it lives here as `roadnet`.
//!
//! Each piece replaced a Python function bit for bit (no format bump): the old Python is kept only inside the parity
//! tests (server/tests/test_roadnet_*.py), never as a fallback (fast.require).

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
pub mod splitlayer;
pub mod surfaces;
pub mod verdicts;
