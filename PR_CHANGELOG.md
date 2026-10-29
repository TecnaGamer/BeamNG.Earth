# Fix highway lane counts, mile markers and exit signs

Restore the highway improvements from the modified 1.7.0 build in 1.7.1, including lane counts, mile markers, Kentucky barriers and exit sign layouts.

## Changes

* Default highways without mapped lane counts to **two lanes**: per carriageway on divided highways, or total on roads carrying both directions. Ramps carrying traffic in one direction default to one lane.
* Prioritize valid OSM lane counts and prevent nearby roads, ramps or opposing traffic from supplying incorrect counts. Keep roads, bridges, tunnels, markings and AI consistent.
* Restore the single lane ramp at I-65 southbound Exit 76 near Upton.
* Restore green mile markers in Michigan, Kentucky and southern Indiana with their previous shields and directions. Keep them available when OSM marker data is missing and remove duplicates.
* Restore Kentucky I-65 and I-75 Jersey barriers with blue markers every 0.2 miles on eligible sections. Restore the I-65 cable barrier section and continuation through Indiana Exit 9, preserving bridge exclusions and road openings.
* Prevent duplicate or overlapping exit signs. Place paired destinations on a shared overhead gantry with the correct shields, exit numbers and arrows.
* Restore the 1.7.0 sign sizes and lettering proportions. Size panels to their content, with two poles for one destination and three for multiple destinations or routes without destination names. Gore and rest area signs keep two poles.
* Include the marker data, barrier materials and source credits in the packaged application.

## Testing

The latest sign checks passed **122 of 123 tests**. Earlier checks passed **626 of 627 Python tests** and **126 Lua assertions**. Both Python runs had the same existing font regeneration failure, also reproduced on unchanged code.

Rendered previews of mile markers, roadside signs and overhead signs were checked. Testing in BeamNG is still needed.

## Scope

Based on 1.7.1 beta. The two lane default applies to motorway and trunk roads worldwide. Bundled mile marker data covers the restored Michigan, Kentucky and southern Indiana routes; other locations use OSM data. Other custom lighting, road width and curve changes are excluded.
