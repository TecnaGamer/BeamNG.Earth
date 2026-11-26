"""tools/ixreplay.py's own parts: the step lines it reads, the tiers of a log, the scaling fit."""
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools"))
import ixreplay  # noqa: E402

LINE = "WARNING earthd.grading: ix step: %s key abcdef123456 pieces %d crossings %d %.2f s %s @ 14/1/2 system %s"


def test_the_step_lines_are_read():
    h = ixreplay.StepLog()
    rec = logging.LogRecord("earthd.grading", logging.WARNING, "", 0,
                            "ix step: heights key abcdef123456 pieces 12 crossings 30 1.25 s ok (2 eased: grade x2) @ 14/1/2 "
                            "system deadbeef", (), None)
    h.emit(rec)
    assert h.steps == [dict(step="heights", key="abcdef123456", pieces=12, crossings=30, seconds=1.25,
                            status="ok (2 eased: grade x2)", region="14/1/2", system="deadbeef")]
    h.emit(logging.LogRecord("earthd.grading", logging.WARNING, "", 0, "ix region: 14/1/2 systems 3 4.50 s, load 1.00 s, "
                             "scan 0.50 s, steps paths 1.00 s", (), None))
    assert h.regions[0]["systems"] == 3 and h.regions[0]["load"] == 1.0


def test_the_ladder_sorts_interchanges_into_tiers(tmp_path, capsys):
    d = tmp_path / "junc" / "after"
    d.mkdir(parents=True)
    lines = []
    for sy, p, c in (("aa", 5, 3), ("bb", 30, 20), ("cc", 40, 21), ("dd", 90, 500), ("ee", 100, 501), ("ff", 900, 2001)):
        lines.append(LINE % ("paths", p, c, 0.5, "ok", sy))
        lines.append(LINE % ("heights", p, c, 1.5, "ok", sy))
    (d / "view.log").write_text("\n".join(lines), encoding="utf-8")
    rows = ixreplay.ladder(str(tmp_path))
    out = capsys.readouterr().out
    tier = {sy: next(n for lim, n in ixreplay.TIERS if r["crossings"] <= lim) for (j, sy), r in rows.items()}
    assert tier == {"aa": "tiny", "bb": "tiny", "cc": "small", "dd": "medium", "ee": "large", "ff": "huge"}
    assert "== large: 1 interchanges" in out


def test_the_fit_finds_the_exponent_of_a_cost():
    pts = [(n, 0.002 * n ** 1.5) for n in (5, 10, 20, 40, 80, 160)]
    b, a = ixreplay.fit(pts)
    assert abs(b - 1.5) < 1e-6 and abs(a - 0.002) < 1e-6
    # (steps under the clock's step say nothing of a size's cost; fewer than three points or one size is no fit)
    assert ixreplay.fit([(n, 0.0) for n in range(1, 9)]) is None
    assert ixreplay.fit([(10, 1.0), (10, 2.0), (10, 3.0)]) is None
