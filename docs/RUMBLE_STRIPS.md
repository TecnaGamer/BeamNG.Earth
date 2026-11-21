# Rumble-strip audio

BeamNG.Earth plays a recorded tyre rumble while a player's tyre touches a likely longitudinal audio-tactile line marking (ATLM). The existing **Rumble strip sound** setting under World settings enables/disables the effect.

## Playback and alignment

- The field recording is louder than the previous generated cue, positioned in 3D at each tyre that reaches a candidate line, and looped for as long as the tyre remains there.
- Volume fades in on contact and fades out after leaving. A narrow contact band plus a wider release band prevents flicker at line edges.
- The game-side graph check uses wheel contact positions sampled by a small player-vehicle Lua extension; no AI or other vehicles are scanned. The contact point is estimated from each wheel node and radius.
- The trigger does not modify tyres, suspension, road collision or vehicle forces. It is an audio cue only.
- Controller vibration is not included. BeamNG exposes player-configured gamepad vibration/force feedback, but no supported Lua call was found for injecting a brief mod-triggered haptic pulse. The setting therefore does not claim to provide controller vibration.

## Where it plays

The current heuristic covers:

- **Australia, New Zealand and the United States**.
- Motorways: the two outside boundaries of each mapped carriageway.
- Rural trunk, primary and secondary roads: outside boundaries, plus the centerline when the carriageway is undivided.
- No same-direction lane separators, ramps/links, unpaved roads, or urban non-motorway roads.

OSM does not provide a reliable global inventory for longitudinal lane-departure rumble strips. The approved [`traffic_calming=rumble_strip`](https://wiki.openstreetmap.org/wiki/Tag:traffic_calming%3Drumble_strip) tag describes transverse strips and is not used to infer centerline or edge installations. The country/road-class rules remain an approximation based on public agency guidance; actual placement varies by corridor.

References: [NSW Transport for NSW, ATLM installation direction](https://www.transport.nsw.gov.au/sites/default/files/media/documents/rww/projects/01documents/saving-lives-on-country-roads/tetd-2019-01-installation-of-audio-tactile-line-marking.pdf), [Queensland TMR Technical Note 214](https://www.tmr.qld.gov.au/_/media/busind/techstdpubs/technical-notes/road-safety/tn214.pdf?extension=pdf&hash=5D910408C15A4F5D8DBB9337171ADB54&rev=623814c229dd4564b6b3eff66b2cff45&sc_lang=en&size=5043355), [NZ Transport Agency, rumble strips](https://nzta.govt.nz/travelling-on-our-roads/safety-cameras/other-safety-infrastructure/rumble-strips), and [FHWA, rumble strip design and construction](https://highways.dot.gov/safety/rwd/keep-vehicles-road/rumble-strips/design-construction).

## Audio asset and limitations

`mod/beamng_earth/levels/beamng_earth/art/sound/rumble_strip.ogg` is a cross-faded, mono 44.1 kHz OGG conversion of the rumble-strip recording supplied by ProxyFirewall202. Playback gently fades when tyres enter and leave the marking; the loop seam is cross-faded to avoid an obvious restart or click. Provenance is recorded in [`CREDITS.md`](../CREDITS.md).

This is a road-side field recording rather than a recording made inside the cabin of the player's simulated car. Placing a separate 3D emitter at each touching tyre makes the cue local to the tyre(s) at the line and audible from the cabin listener position, but the sound will still vary with the vehicle's camera/listener placement and BeamNG's audio settings. In-game validation is needed to tune the perceived level and spatial balance across vehicles.
