# Highway signs and mile markers for 1.6.0-beta

- Added auto generated highway exit signs using OpenStreetMap exit numbers and destinations.
- Added advance exit signs at 1 mile, 1/2 mile and 1/4 mile, aswell as blue rest area and welcome center signs.
- Added interstate mile markers with the direction, interstate shield, **MILE** label and mile number.
- Added separate **Highway exit signs** and **Interstate mile markers** toggles under **Mod options → World**.
- Added background sign data loading, automatic refresh when the data becomes ready, and packaged lettering/reference assets. sign data failures allow roads to continue generating

This ports the sign features from the modified 1.5.5 build onto 1.6.0's existing road geometry and streaming system. It does not replace the newer road, bridge, terrain, minimap or multiplayer implementations.

Mile marker coverage includes the bundled Michigan and Kentucky references and supported OSM milestones elsewhere. Missing mileage is only filled between compatible mapped references; numbering is not invented for unsupported roads. Exit sign coverage depends on available OSM destination data, which may require an initial background download.

Validation: automated placement, duplicate prevention, worker/server parity, settings, streaming and packaging checks. Ingame driving and multiplayer visual checks remain to be completed before merging. See `docs/HIGHWAY_SIGNS.md` for details.
