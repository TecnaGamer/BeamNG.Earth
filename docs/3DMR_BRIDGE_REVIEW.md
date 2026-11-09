# 3DMR bridge visual checks

The images compare the released BeamNG.Earth v1.9.0-beta with a backport of this candidate to that release. Both runs used the same published 3DMR model revisions, fixed daylight and matching camera positions. The right-hand image is a release-based test of the candidate, rather than the development branch itself.

## Elizabeth Quay Bridge

The candidate removes the duplicate generated footbridge along the imported walkway and corrects the timber deck's visible face.

![Elizabeth Quay comparison](images/3dmr-bridges/elizabeth-quay-19-comparison.jpg)

## Mount Henry Bridge

The modelled bridge replaces the generated bridge, but a solid modelled end blocks part of the road approach and the railway does not join its track.

![Mount Henry comparison](images/3dmr-bridges/mount-henry-19-comparison.jpg)

## Narrows Bridge

The model replaces the generated bridge, but its end structure still obstructs the lower road and the approach remains rough.

![Narrows comparison](images/3dmr-bridges/narrows-19-comparison.jpg)

Dragon Bridge in Da Nang was also checked as a control. These locations do not establish worldwide correctness. Visual checks do not establish continuous vehicle handling, collision coverage, surveyed elevation or clearance across the full width of an underpass. The crossing-clearance check samples across the mapped road width, is conservative and can leave an approach unchanged; it cannot guarantee clearance between samples. The Mount Henry road and railway obstructions, Narrows obstruction, further in-game driving and collision checks, and CI remain outstanding. Keep the PR in draft.
