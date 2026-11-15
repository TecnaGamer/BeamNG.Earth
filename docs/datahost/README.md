# Data-host integration handoff

The application repository does not run a separate national-data build workflow.
`build-nl-rvv-data.yml` is a template for the BeamNG.Earth-data maintainer to adapt
to that site's layout and deployment. It is deliberately outside `.github` and
does not publish anything from this application PR.

For a self-contained NDW build, retain these files at their relative paths:

- `tools/build_nl_rvv.py`: official CSV-to-shards producer, Python standard library.
- `server/nlrvvdata.py`: shared normalization and shard schema; its source export
  and licence links remain part of the produced manifest.

The workflow writes `public/nl-rvv`; the site's existing deployment should serve
that dataset using its ordinary manifest/shard mechanism. The application reads
only public tiles when opted in. It never downloads the national CSV during play.

The separate signal-context producer, inputs, source provenance and deployment
layout are described in `../NL_SIGNAL_CONTEXT_DATA.md`. It additionally requires
pyproj and shapely at build time, but neither dependency is needed by the runtime
reader. Do not describe either dataset as published until the data-host
maintainer has deployed it.
