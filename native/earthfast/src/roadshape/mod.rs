//! Road revamp v2, "road shape first" (.claude/work/road-revamp-v2-road-shape-first-2026-11-19.md), switch
//! BNE_ROADSHAPE (server/roadshape.py).
//!
//! Phase P1b (earthfast 498, BNE_ROADSHAPE=decks): curved, skewed decks with buried ends for the SIMPLE structures (a
//! structure group of one or two members with no END_ON or OVERLAP edge: plain overpasses, twins, mountain bridges),
//! decks.rs.
//!
//! Phase P2a (earthfast 510, BNE_ROADSHAPE=sheet, shadow only): the Fork records (an exit / entrance / ramp split from
//! the map alone: noses, shared length, quintic widths), forks.rs.
pub mod decks;
pub mod forks;
