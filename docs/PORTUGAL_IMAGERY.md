# Portugal orthophotos

`pt_dgt2021` and `pt_dgt2018` use DGT's 25 cm aerial orthophotos, in that
order, before the broad Spanish source. The newer flights cover the north;
white missing tiles fall through to 2018, then the normal global chain.
The services advertise a mainland bounding box, including blank surrounding
land and sea. It is a request filter, not a precise coverage polygon. The
2021 box stops at 39.50 N: its flights hold imagery only north of ~39.58 N
(tested from Portugal: Braga to Leiria have it; Santarém, Lisbon, Évora and
Faro come back ~97 % white), and each empty 1024 px export cost ~260 ms
before the 2018 fallback.

Both use the existing lazy WMS export path (one 1024 px image per z15
parent, cached as z17 children), the ordinary shared per-host limits, and
the map's ordinary imagery consumer. Source GSD is not the output texel size.
No new numerical or generation algorithm is introduced.

The WMS endpoints are `https://cartografia.dgterritorio.gov.pt/ortos2018/service`
and `https://cartografia.dgterritorio.gov.pt/ortos2021/service`. Their
`GetCapabilities` documents advertise `Ortos2018-RGB` / `Ortos2021-RGB`,
EPSG:3857 and EPSG:3763, and no fees or access constraints. Request PNG:
`image/jpeg` returns an XML `InvalidFormat` response on both services.
Use an opaque white background, matching the existing export nodata policy.

DGT identifies its aerial orthophoto collection as CC BY 4.0 and requires
attribution to Direção-Geral do Território:
[DGT's orthophoto catalogue announcement, 25 August 2026](https://www.dgterritorio.gov.pt/novo-catalogo-de-ortofotos-de-portugal-continental-no-centro-de-dados-da-dgt).
This integration uses the aerial 2018/2021 products, not OrtoSat satellite imagery.

## Validation

Run `python -m unittest discover -s server/tests -p test_portugal_imagery.py`.
The offline contract tests exercise actual WMS requests, disk caching, ground
rendering and map selection, including missing northern coverage, the Spanish
border and a point outside the Portuguese bounds. Existing imagery, map-tile,
network-policy and credit tests cover their unchanged shared consumers.

Live requests on 7 November 2026 returned nonblank PNG imagery at Porto
(2021) and Almada (2018), and an opaque white 2021 image at Almada. Inspecting
those images confirms the expected coverage and fallback inputs. This is
provider and server validation; an in-game visual check is still needed.

A live production-path check also warmed the DGT chain in a new isolated
cache and served an inspected Almada map tile from `pt_dgt2018`, after
the 2021 coverage miss.
