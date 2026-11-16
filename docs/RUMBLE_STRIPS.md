# Rumble strip audio

BeamNG.Earth plays a short, soft rumble when the player's car approaches an edge or centerline that is likely to have
longitudinal audio-tactile line marking (ATLM). The setting is **Rumble strip sound** under World settings and is on by
default.

## Where it plays

The current heuristic covers:

- **Australia, New Zealand and the United States**.
- Motorways: the two outside boundaries of each mapped carriageway.
- Rural trunk, primary and secondary roads: outside boundaries, plus the centerline when the carriageway is
  undivided.
- No same-direction lane separators, ramps/links, unpaved roads, or urban non-motorway roads.

The sound triggers as the vehicle approaches a candidate strip, with a short re-arm distance and cooldown so staying
near the line does not produce a continuous loop. This is an audible cue only; it does not apply forces to the vehicle
or change tire, suspension, road collision or handling physics.

## Data limits and rationale

OSM does not provide a reliable global inventory for longitudinal lane-departure rumble strips. The approved
[`traffic_calming=rumble_strip`](https://wiki.openstreetmap.org/wiki/Tag:traffic_calming%3Drumble_strip) describes
transverse strips that span a road, and its documentation says longitudinal lane-departure strips are generally not
mapped. It would be incorrect to treat that tag as a centerline or edge-line installation.

The heuristic is therefore deliberately limited and approximate. NSW guidance says ATLM may be placed on edge, lane and
center lines and targets rural roads based on run-off-road or cross-carriageway risk; Queensland guidance also describes
ATLM as a component of wide-centreline treatments. New Zealand guidance describes strips on state-highway edges and
centrelines. US Federal Highway Administration guidance describes centerline and shoulder/edge strips on rural roads
and notes that agencies decide placement based on road context and design.

References: [NSW Transport for NSW, ATLM installation direction](https://www.transport.nsw.gov.au/sites/default/files/media/documents/rww/projects/01documents/saving-lives-on-country-roads/tetd-2019-01-installation-of-audio-tactile-line-marking.pdf),
[Queensland TMR Technical Note 214](https://www.tmr.qld.gov.au/_/media/busind/techstdpubs/technical-notes/road-safety/tn214.pdf?extension=pdf&hash=5D910408C15A4F5D8DBB9337171ADB54&rev=623814c229dd4564b6b3eff66b2cff45&sc_lang=en&size=5043355),
[NZ Transport Agency, rumble strips](https://nzta.govt.nz/travelling-on-our-roads/safety-cameras/other-safety-infrastructure/rumble-strips),
[FHWA, rumble strip design and construction](https://highways.fhwa.dot.gov/safety/rwd/keep-vehicles-road/rumble-strips/design-construction).

This is a road-class and country heuristic, not a claim that every eligible road has strips or that every treated corridor
is detected. Exact placement would require corridor-level agency inventories or consistent mapped ATLM geometry, neither
of which the project currently has. The country list is kept to regions with the cited public-road guidance and can be
expanded if comparable sources or mapped data become available.
