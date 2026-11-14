"""The German tests' borders: upstream's regional data fixture (server/tests/fixtures/regional: PR #505's slice of the
published build; Germany at about 47-49.5 N and 50.5-51 N, Schrobenhausen, Munich, Kehl, Konstanz, Buesingen and
Freilassing among it), read through BNE_REGIONAL_DIR as test_countrybounds reads it. A module that checks German output
sets the variable at its top, before the server's imports (each module runs in a process of its own):

    os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                           "regional"))

and calls require() in its setUpModule: without border data every German lookup is False (germany.py: today's
output), so its tests would pass on today's output instead of Germany's."""
import os

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional")
SCHROBENHAUSEN = (48.561, 11.262)       # (lat, lon): the German tests' place (test_designs' JunctionGlueTest.FRAME)


def require():
    """Fail unless the border index is there and calls Schrobenhausen Germany."""
    import countrybounds
    import germany
    idx = countrybounds.bundled()
    if isinstance(idx, countrybounds.Absent) or not germany.contains(SCHROBENHAUSEN[1], SCHROBENHAUSEN[0])[0]:
        raise AssertionError(
            f"no border data at Schrobenhausen ({type(idx).__name__}, BNE_REGIONAL_DIR="
            f"{os.environ.get('BNE_REGIONAL_DIR')!r}): the German tests would pass on today's output")
