---
name: port-review
description: Review an earthfast Python-to-Rust port for merge using its clean-base replay and release evidence.
---

Apply the project port-module checklist at `.agents/skills/port-module/SKILL.md`. Trace the production caller into the new binding, inspect changed ownership, operation order, errors and side effects, and confirm the old Python algorithm and fallback are gone.

Demand the pinned base and data manifest, case counts with a nonempty witness, callback trace where applicable, deliberate one-value failure, exact replay, and the original behaviour tests. Inspect the first mismatch and its input for any withheld piece. Reject weakened tests, regenerated expectations or unexplained differences.

On the integrated revision check every registration against VERSION/MIN_VERSION, rebuild both release binaries, verify their digests and glibc ceiling, and demand the applicable corpus and runtime results. Record skips, cross-build-only platforms, visual coverage, dependency versions/licences, production Python additions/deletions and the merge counter.
