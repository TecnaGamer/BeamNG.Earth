"""ixcheck: independent checks over the interchange pipeline's cached artifacts.

It reads what a run left in BNE_IX_CACHE (ix/artifacts.py's records, one .npz per artifact) and reports, per step,
named failures and notes. It never calls generation code: the geometry is recomputed here (geom.py), the constants it
judges by are its own. See SPEC.md."""
