# A11 bridge ranges

Implement the shared bridge-range correction approved in the conversation for issue #909's A11 follow-up at 53.29391, 14.11574. Source: upstream dev; keep the existing checkout untouched.

Resolve skew extensions from a deterministic, bounded neighborhood of each bridge, rather than the first point of its potentially kilometer-long road feature. Both deck and ground consumers must use the same resolved off-ground ranges. Preserve source feature/ring/interval identity when collecting underpass candidates, so overlapping copies cannot lower their own road stretch; genuine self-crossings at distinct source intervals remain eligible. Cache missing context only temporarily and invalidate persisted facts/plans after the change.

The alternative considered was a grading-only filter. It is smaller but leaves conflicting deck and ground extents in other consumers; the user selected the shared correction with a narrow defensive check.

Constraints: bound context reads to the bridge neighborhood; preserve legitimate underpass grading, local bridge levels, and #920 railway grading. Reuse existing native kernels and the requested assets once; no image rendering or mesh exports are needed. Validate behavior with synthetic offline map cells and the saved real A11 data.
