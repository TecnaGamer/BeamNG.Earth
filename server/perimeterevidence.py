"""Perimeter evidence cache identity, without importing the geometry runtime.

Keep the same ordered path and byte inputs for selection and revisit keys.
Warren's authoritative snapshot retains its separate geographic tile signature.
"""
import hashlib
import os
from pathlib import Path

EVIDENCE = Path(__file__).with_name("perimeter_evidence.geojson")
CONSTRAINTS = Path(__file__).with_name("perimeter_constraints.geojson")


def paths(evidence=EVIDENCE, constraints=CONSTRAINTS):
    result = [evidence]
    if constraints.exists():
        result.append(constraints)
    if os.environ.get("BNE_PERIMETER_OVERRIDES"):
        result.append(Path(os.environ["BNE_PERIMETER_OVERRIDES"]))
    return result


def signature(evidence=EVIDENCE, constraints=CONSTRAINTS):
    h = hashlib.sha256()
    for path in paths(evidence, constraints):
        h.update(str(path).encode())
        try:
            h.update(path.read_bytes())
        except FileNotFoundError:
            if path != evidence:
                raise
    return h.hexdigest()
