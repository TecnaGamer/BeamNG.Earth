# QLD aerial imagery: licence check (plan item 2.1), 2026-11-07

Verdict: NOT wired in. The licence statements conflict and part of the mosaic is third-party "all rights reserved".

## Service
- Public service: `https://spatial-img.information.qld.gov.au/arcgis/rest/services/Basemaps/LatestStateProgram_AllUsers/ImageServer`
  (the `spatial-gis...` host redirects to a login; the `_AllUsers` service is the public one). Also published as WMS and WMTS.
- Catalogue: https://www.data.qld.gov.au/dataset/queensland-imagery-latest-state-program-public-basemap-service
- Service metadata: `.../LatestStateProgram_AllUsers/ImageServer/info/iteminfo?f=pjson`
- Aerial imagery three years or older from the State Imagery Program (SISP); Planet satellite mosaic fills gaps. Tile cache to 1:1129.

## What the sources say
- data.qld.gov.au listing: licence "Creative Commons Attribution 4.0" (CC BY 4.0), security class PUBLIC. Mentions no limit on apps or bulk tiles.
- The service's own metadata (`licenseInfo`): imagery "publicly available under Creative Commons Attribution-Share-Alike licence (CC BY-SA)".
  Attribution text from the copyright/credits fields must go on every copy; a no-warranty acknowledgement is required on products.
- Its `accessInformation` also reads: "(c) Planet Labs Netherlands B.V. reproduced under licence from Planet and Geoplex, all rights reserved".
  That mosaic is outside the CC licence.
- Terms of use: the user accepts all risk; no warranties; the user indemnifies the State.
- OSM wiki (Australian Data Sources/Queensland): older imagery CC BY 4.0, newer SISP imagery under separate terms.

## Why it is not clearly open
1. CC BY 4.0 (catalogue) against CC BY-SA (service). BY-SA would pull share-alike duties onto every derived ground photo.
2. The Planet/Geoplex fallback is all rights reserved, and a tile request cannot tell which source it hit.
3. No statement either way on automated tile access by an app.

## Recommendation
- Do not ship until the Queensland Spatial Catalogue / DNRME confirms in writing: one licence for the tiles, whether the Planet fallback is
  excluded, and that app tile access is allowed. Contact: the data custodian listed on the catalogue record.
- If cleared: add `"au_qld"` beside `au_nsw` in `server/imagery_regional.py` (bounds about (-29.2, 138.0, -10.0, 153.6), xyz, attribution
  "(c) State of Queensland (Dept of Natural Resources and Mines) CC BY"), and credit it in the credits and README.
- Until then Brisbane and the rest of Queensland stay on Sentinel-2 (about 10 m).
