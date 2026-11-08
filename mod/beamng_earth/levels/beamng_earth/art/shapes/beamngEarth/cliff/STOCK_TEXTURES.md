# Stock canyon detail textures

The active canyon detail uses BeamNG GmbH's native 1024-square t_dirt_rocky
terrain color, normal, AO and roughness from terrain.zip. Source files are
copied unchanged; the normal retains its mip chain. Neutral shader detail
maps are reproducibly derived by tools/bake_canyon_detail.py.

Exact original paths and SHA-256 hashes are in
ground_detail.sources.json. These stock assets and derivatives
are BeamNG-owned content and excluded from the project's MIT license.
Local copies prevent later game asset changes from breaking the material.

Superseded normal trials and pre-relief roughness maps have been removed;
their history remains in Git. Retained source maps feed the current detail
and roughness bakers. See SOURCES.md for the shared asset naming convention.
