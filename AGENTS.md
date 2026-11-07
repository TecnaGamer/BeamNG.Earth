# Regional asset placement and map debugging

When a user asks to add or change the regional placement of assets (cliffs,
vegetation, buildings, roadside props, or other location-dependent features):

- Use the production selector as the source of truth. Extend the shared rule
  registry where one exists; do not duplicate geographic boundaries in the UI.
- Add or update a provider in `server/debuglayers.py` and expose it under the
  in-game map's **Layers > Debug layers > Regional placement** controls. Reuse
  the existing provider protocol, canvas, legend and bounded request path.
- Show meaningful type names and colours. Clearly distinguish appearance-rule
  regions, sampled predictions, and recorded generated footprints. Explain
  slope/land-cover/seed/settings conditions and missing or limited coverage.
- Make overlapping rules deterministic with explicit precedence. For cliffs,
  layered canyon beats default rock; confirmed road cuts are a separate
  provenance context. Test overlap, fallback, boundaries and near/far agreement.
- Debug views must be opt-in, read-only, bounded and use local data. Never start
  generation, download world data, load full render meshes or add per-frame
  gameplay work just to draw an overlay. Reuse coarse detector metadata.
- **Tell the user clearly during the work that you are adding/updating a map
  debug layer for this regional placement. In the final response, explicitly
  direct them to open the map, choose Layers > Debug layers, select the named
  layer, and try it in the relevant regions.** Do this even if they did not
  explicitly ask for a debug layer in that particular request.
- Verify the provider and map UI, including empty/error/off states. Report
  verification limits honestly; a rule preview does not prove in-game spawning.

See `docs/REGIONAL_DEBUG_LAYERS.md` for provider and cliff extension guidance.
