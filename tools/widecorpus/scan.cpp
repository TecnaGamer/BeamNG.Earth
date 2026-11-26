// Adapted from the codex-snag collision census. Input triangles have CCW upward
// winding restored after CDAE decoding.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <set>
#include <tuple>
#include <vector>
using namespace std;
struct V {
  double x, y, z;
  V operator-(V b) const { return {x - b.x, y - b.y, z - b.z}; }
  V operator+(V b) const { return {x + b.x, y + b.y, z + b.z}; }
  V operator*(double k) const { return {x * k, y * k, z * k}; }
};
double cross(V a, V b) { return a.x * b.y - a.y * b.x; }
double dot(V a, V b) { return a.x * b.x + a.y * b.y; }
double len(V a) { return hypot(a.x, a.y); }
struct T {
  V p[3], c;
  int label, idx;
  double area;
  bool top;
  double z(V a) const {
    V u = p[1] - p[0], v = p[2] - p[0], w = a - p[0];
    return p[0].z + (cross(w, v) * u.z + cross(u, w) * v.z) / area;
  }
};
using Poly = vector<V>;
template <class F> Poly clip(const Poly &p, F f) {
  Poly r;
  if (p.empty())
    return r;
  V a = p.back();
  double x = f(a);
  for (V b : p) {
    double y = f(b);
    if ((x > 0) != (y > 0))
      r.push_back(a + (b - a) * (x / (x - y)));
    if (y >= 0)
      r.push_back(b);
    a = b;
    x = y;
  }
  return r;
}
struct Lane {
  double ax, ay, bx, by, w;
};
vector<Lane> lanes;
double margin(V p) {
  double best = -999;
  for (auto l : lanes) {
    V a = {l.ax, l.ay, 0}, d = {l.bx - l.ax, l.by - l.ay, 0};
    double t = dot(d, p - a) / max(dot(d, d), 1e-20);
    if (t >= 0 && t <= 1)
      best = max(best, l.w * .5 - len(p - a - d * t));
  }
  return best;
}
struct Stat {
  int n = 0;
  double sum = 0, hi = -1;
  V where{};
  int i, j;
};
map<tuple<string, int, int>, Stat> stats;
void add(string why, int a, int b, double v, V p, int i, int j) {
  auto &s = stats[{why, min(a, b), max(a, b)}];
  s.n++;
  s.sum += v;
  if (v > s.hi) {
    s.hi = v;
    s.where = p;
    s.i = i;
    s.j = j;
  }
}
void measure(T &a, T &b, bool terrain) {
  Poly p(a.p, a.p + 3);
  for (int j = 0; j < 3 && !p.empty(); j++) {
    V q = b.p[j], d = b.p[(j + 1) % 3] - q;
    p = clip(p, [&](V v) { return cross(d, v - q); });
  }
  if (p.size() >= 3) {
    for (double sign : {-1., 1.})
      p = clip(p, [&](V v) { return .05 - sign * (a.z(v) - b.z(v)); });
    if (p.size() >= 3) {
      double area = 0;
      V c{};
      for (size_t k = 1; k + 1 < p.size(); k++) {
        double w = abs(cross(p[k] - p[0], p[k + 1] - p[0])) / 2;
        area += w;
        c = c + (p[0] + p[k] + p[k + 1]) * (w / 3);
      }
      if (area > 1e-6) {
        c = c * (1 / area);
        c.z = a.z(c);
        add("overlap", a.label, b.label, area, c, a.idx, b.idx);
        if (margin(c) >= .75)
          add("laneOverlap", a.label, b.label, area, c, a.idx, b.idx);
      }
    }
  }
  if (terrain)
    return;
  // Opposite-side triangles across a shared (possibly partial) edge. Exclude
  // same-side overlays.
  for (int i = 0; i < 3; i++)
    for (int j = 0; j < 3; j++) {
      V u = a.p[i], v = a.p[(i + 1) % 3], d = v - u, q = b.p[j],
        r = b.p[(j + 1) % 3], e = r - q;
      double L = len(d), E = len(e);
      if (min(L, E) < .01)
        continue;
      if (dot(d, e) >= 0 || abs(cross(d, e)) / (L * E) > 0.001)
        continue;
      double t0 = dot(q - u, d) / (L * L), t1 = dot(r - u, d) / (L * L),
             lo = max(0., t1), hi = min(1., t0);
      if ((hi - lo) * L < .01)
        continue;
      V s = u + d * lo, h = u + d * hi, m = (s + h) * .5;
      double lateral = cross(d, q - u) / L;
      auto dz = [&](V k) {
        return k.z - (q + e * (dot(k - q, e) / (E * E))).z;
      };
      double gap = max(abs(dz(s)), abs(dz(h)));
      if (max(abs(cross(e, s - q)), abs(cross(e, h - q))) / E > .0003) {
        if (lateral < -.001 && lateral >= -.05 && gap < .05)
          add("gapCandidate", a.label, b.label, -lateral, m, a.idx, b.idx);
        continue;
      }
      bool split = (lo * L > .001 || (1 - hi) * L > .001 ||
                    (t0 - hi) * L > .001 || (lo - t1) * L > .001);
      if (gap <= .001 && split)
        add("Tjoin", a.label, b.label, (hi - lo) * L, m, a.idx, b.idx);
      if (gap > .001) {
        string k = gap <= .005   ? "step1"
                   : gap <= .020 ? "step2"
                   : gap <= .05  ? "step3"
                                 : "step4";
        add(k, a.label, b.label, gap, m, a.idx, b.idx);
        if (gap > .02 && margin(m) >= .75)
          add("inLane", a.label, b.label, gap, m, a.idx, b.idx);
      }
    }
}
int main(int argc, char **argv) {
  if (argc != 3 && argc != 4)
    return 2;
  ifstream l(argv[2], ios::binary);
  if (!l)
    return 2;
  Lane lane;
  while (l.read((char *)&lane, sizeof(lane)))
    lanes.push_back(lane);
  vector<T> tris;
  map<pair<int, int>, vector<int>> grid;
  ifstream f(argv[1], ios::binary);
  if (!f)
    return 2;
  double row[11];
  while (f.read((char *)row, sizeof row)) {
    T t{};
    t.label = (int)row[0];
    t.idx = (int)row[1];
    for (int k = 0; k < 3; k++)
      t.p[k] = {row[2 + 3 * k], row[3 + 3 * k], row[4 + 3 * k]};
    t.c = (t.p[0] + t.p[1] + t.p[2]) * (1. / 3);
    V u = t.p[1] - t.p[0], v = t.p[2] - t.p[0];
    t.area = cross(u, v);
    double norm =
        hypot(t.area, hypot(u.y * v.z - u.z * v.y, u.z * v.x - u.x * v.z));
    t.top = t.area > .45 * norm && norm > 1e-10;
    double longest = 0;
    for (int i = 0; i < 3; i++) {
      V d = t.p[i] - t.p[(i + 1) % 3];
      longest = max(longest, hypot(len(d), d.z));
    }
    if (t.label && longest > .01 && norm / longest < .001)
      add("sliver", t.label, t.label, longest, t.c, t.idx, -1);
    if (!t.top)
      continue;
    double x0 = t.p[0].x, x1 = x0, y0 = t.p[0].y, y1 = y0;
    for (V a : t.p) {
      x0 = min(x0, a.x);
      x1 = max(x1, a.x);
      y0 = min(y0, a.y);
      y1 = max(y1, a.y);
    }
    set<int> seen;
    int id = tris.size();
    for (int x = floor((x0 - .05) / 4); x <= floor((x1 + .05) / 4); x++)
      for (int y = floor((y0 - .05) / 4); y <= floor((y1 + .05) / 4); y++) {
        auto &g = grid[{x, y}];
        seen.insert(g.begin(), g.end());
        if (t.label)
          g.push_back(id);
      }
    for (int a : seen) {
      T &b = tris[a];
      if (abs(t.c.x - b.c.x) > 1000)
        continue;
      measure(t, b, t.label == 0 || argc == 4);
    }
    if (t.label)
      tris.push_back(t);
  }
  cout << setprecision(12);
  for (auto &[k, s] : stats) {
    auto &[why, a, b] = k;
    cout << why << " " << a << " " << b << " " << s.n << " " << s.sum << " "
         << s.hi << " " << s.where.x << " " << s.where.y << " " << s.where.z
         << " " << s.i << " " << s.j << " " << margin(s.where) << "\n";
  }
}
