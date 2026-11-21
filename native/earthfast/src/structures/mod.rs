//! Road revamp track RS, "one structure per level" (.claude/work/road-revamp-structures-design-2026-11-18.md). A bridge
//! chain's decks are drawn today part by part and patched pairwise; RS makes one record per structure inside the chain
//! publish (roads.BridgeProfiles._chain_solve) and the tiles only clip it.
//!
//! Step S1 (earthfast 480): `structure_groups`, which decks of one chain form one structure (groups.rs). Nothing is
//! built from it yet: roads writes it as a shadow JSON per chain (BNE_DECK_STRUCT=shadow).
//!
//! Step S2b part 1 (earthfast 488): `structure_heights`, ONE joint height solve per structure (heights.rs), shadow only.
//!
//! Step S2b part 2a (earthfast 493): `structure_abutments`, one abutment height per structure end (abutments.rs), which
//! grading's plans give every approach road ending there (BNE_DECK_STRUCT=abut).
pub mod abutments;
pub mod groups;
pub mod heights;
