# Stock rock detail textures

canyon_detail_italy_v1.data.dds and canyon_detail_italy_v1.normal.dds are
unmodified copies of BeamNG.drive Italy rock detail textures by BeamNG GmbH.
They are BeamNG-owned assets, not covered by this project's MIT license.
Their original paths and SHA-256 hashes are recorded in
canyon_detail_italy_v1.sources.json. Local copies keep the canyon material
independent of later changes to the game's stock asset paths and content.

West Coast 2K source color and normal are pinned from rocks.zip / assets/materials/rocks/t_wca_rock/. The source color is converted to neutral RGB detail data by tools/bake_canyon_detail.py. The stock originals and this derived detail map remain BeamNG-owned content, excluded from the project MIT license. See canyon_detail_wca_v1.sources.json for exact hashes and provenance.

The active canyon detail pair uses t_dirt_rocky from terrain.zip / assets/materials/terrain/rock/t_dirt_rocky/. The 1024-square source color and mipmapped normal are copied unchanged; canyon_detail_dirt_rocky_v1.data.png is neutral RGB luminance detail derived by tools/bake_canyon_detail.py. Exact source paths and hashes are recorded in canyon_detail_dirt_rocky_v1.sources.json. These BeamNG GmbH assets and derivatives are excluded from the project MIT license. Earlier Italy mesh-detail and West Coast mesh-surface pairs are no longer bound to the canyon materials.

Previous t_rock_b trial: canyon_detail_terrain_rock_source.color.png and canyon_detail_terrain_rock_v1.normal.png are unchanged 1024-square copies of levels/west_coast_usa/art/terrains/t_rock_b.png and t_rock_nm.png from west_coast_usa.zip. They are paired in the stock Rock_cliff TerrainMaterial. The derived neutral RGB detail and exact source hashes are recorded in canyon_detail_terrain_rock_v1.sources.json. BeamNG GmbH content and derivatives, excluded from the project MIT license. This pair is retained for comparison and is no longer bound to either near canyon material.

Active t_dirt_rocky now also includes unchanged native 1K t_dirt_rocky_ao.data.dds and t_dirt_rocky_r.data.dds sources from terrain.zip. The *_v1_ao.data.png and *_v1_roughness.data.png are neutral additive shader-detail encodings derived by tools/bake_canyon_detail.py; provenance and SHA-256 hashes are in canyon_detail_dirt_rocky_v1.sources.json. These sources and derivatives are BeamNG GmbH content, excluded from the project MIT license.
