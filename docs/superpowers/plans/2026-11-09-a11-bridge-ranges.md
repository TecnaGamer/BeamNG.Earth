# A11 Bridge Ranges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent A11 approaches from being lowered beneath conflicting copies of their own bridge.

**Architecture:** Attach a bounded road-cell lookup to decoded road layers. Resolve each bridge's crossing context consistently in roads.py before deriving deck/ground pieces. Carry source intervals through grading's span collection and chain merging to reject self-overlap requirements.

**Tech Stack:** Python, NumPy, existing earthfast kernels, unittest.

**Spec:** docs/superpowers/specs/2026-11-09-a11-bridge-ranges-design.md

## Global Constraints

- Preserve legitimate underpasses and same-feature crossings at distinct source intervals.
- Bound context reads to the bridge neighborhood; do not walk the road to its first-point cell.
- No new dependency, native build, image render, or mesh export.
- Missing context must not persist as complete facts or plans.

## Review Focus

- Long features with several bridges must resolve each bridge independently.
- Cell-boundary crossings and load order must produce identical intervals.
- Missing context must fault the build and recover on later requests.
- Genuine self-crossings and chained spans must retain underpass requirements.
- Cached facts/plans must be invalidated by the changed range behavior.

### Task 1: Shared bridge context

**Files:** server/mapdata.py, server/roads.py, server/tests/test_bridge_ranges.py.

**Interfaces:** road layers provide `_bnge_road_cells(keys)`; roads resolves bridge crossing features from bounded cells, deduplicated by feature ID.

- [x] Add offline long-feature/cell-boundary regression tests; run and confirm failure.
- [x] Implement bounded context selection, preserving standalone feature fixtures and faulting unavailable context.
- [x] Verify consistent deck/ground ranges, several spans, load order and context recovery.

### Task 2: Source identity in underpass grading

**Files:** server/grading.py, server/tests/test_bridge_ranges.py, server/tests/test_underpass_profiles.py.

**Interfaces:** ENU cell parts carry `(fid, ring, start, end)` provenance; span-chain merging preserves provenance when requested.

- [x] Add failing self-overlap regression and genuine self-crossing/chain controls.
- [x] Retain provenance through collection and reject candidates inside their own bridge's source interval.
- [x] Run grading, bridge ranges, deck joins, local levels, and underpass regressions.

### Task 3: Cache formats, real-data validation and review

**Files:** server/roads.py, server/plancache.py, server/tests/golden/bake_formats.json.

- [x] Bump affected cache formats and update format fixture through the repository's existing workflow.
- [x] Probe the saved A11 data: no artificial 7.5 m dip; approaches continuous; lower road retains clearance; compare neighboring cells.
- [x] Run the server suite and required repository checks, recording any baseline/environment failures.
- [x] Obtain one independent review, address material findings with regression tests, and leave the completed branch in its worktree.

Validation results and baseline failures: [A11 bridge range report](../../research/2026-11-09-a11-bridge-ranges.md).
