import json
import sys

import numpy as np

CATS = ("pine", "fir_spruce", "birch", "oak", "beech", "mediterranean", "eucalyptus")
C = sys.argv[1]
ours = json.load(open(C + "/euf_ours.json"))
dev = {tuple(r["cell"]): r for r in json.load(open(C + "/euf_dev.json"))} if len(sys.argv) < 3 else {}


def shares(d, cats=CATS):
    v = np.array([d.get(c, 0) for c in cats], float)
    return v / v.sum() if v.sum() else v


def conifer(d):
    tot = sum(d.values())
    return (d.get("pine", 0) + d.get("fir_spruce", 0) + d.get("conifer?", 0)) / tot if tot else np.nan


rows = []
for r in ours:
    o, m = r["obs"], r["model"]
    if sum(m.get(c, 0) for c in CATS) < 50:
        continue
    so, sm = shares(o), shares(m)
    dv = dev.get(tuple(r["cell"]))
    rows.append((tuple(r["cell"]), r["plots"], so, sm, conifer(o), conifer(m),
                 conifer(dv["model"]) if dv else np.nan, r["elev"]))
print(f"cells: {len(rows)}")
co = np.array([x[4] for x in rows]); cm = np.array([x[5] for x in rows]); cd = np.array([x[6] for x in rows])
print("\n== udział iglastych (obserwacja vs model)")
for name, v in (("nasza", cm), ("dev", cd)):
    ok = ~np.isnan(v)
    print(f"  {name:6} MAE {np.mean(np.abs(v[ok] - co[ok])):.3f}   korelacja {np.corrcoef(v[ok], co[ok])[0, 1]:.2f}")
SO = np.array([x[2] for x in rows]); SM = np.array([x[3] for x in rows])
print("\n== gatunki (nasza wersja): średni udział obs/model, MAE, korelacja między komórkami")
for i, c in enumerate(CATS):
    if SO[:, i].std() > 0 and SM[:, i].std() > 0:
        r_ = np.corrcoef(SO[:, i], SM[:, i])[0, 1]
    else:
        r_ = float("nan")
    print(f"  {c:14} obs {SO[:, i].mean():.3f}  model {SM[:, i].mean():.3f}  MAE {np.mean(np.abs(SO[:, i] - SM[:, i])):.3f}"
          f"  r {r_:.2f}")
dom = np.mean(SO.argmax(1) == SM.argmax(1))
print(f"\n  dominujący gatunek zgodny w {dom:.0%} komórek")
err = np.abs(SO - SM).sum(1) / 2
print(f"  różnica składu (0 = identyczny, 1 = rozłączny): mediana {np.median(err):.2f}, 90. percentyl "
      f"{np.percentile(err, 90):.2f}")
print("\n== najgorsze komórki")
for k in np.argsort(-err)[:25]:
    cell, n, so, sm = rows[k][:4]
    f = lambda s: " ".join(f"{c[:4]}{int(round(100 * x)):3d}" for c, x in zip(CATS, s) if x >= .03)
    print(f"  {cell} n={n:4d} elev {rows[k][7]:5.0f}  obs [{f(so)}]  model [{f(sm)}]")
json.dump([{"cell": x[0], "obs": list(x[2]), "model": list(x[3])} for x in rows], open(C + "/euf_rows.json", "w"))
