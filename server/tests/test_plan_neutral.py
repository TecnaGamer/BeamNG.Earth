"""Plan-neutral build switches (revisit.PLAN_NEUTRAL; the user, 2026-11-16, after a tester with 21 Experimental switches
on got no shared plans in New York: "decisions: none built for version ..."): the BNE_* switches that change no road
plan and no cell fact are left out of the plans' own switches key (revisit.plans_switches_key: the "plans" and
"cellfacts" format markers, decisions.vkey_doc, bakepack.switches, the BeamMP share code), every other key keeps them.

A switch is on the list only with both proofs:

  static (StaticGuardTest): the planning closure, every function the plan and cell-facts building can reach from its
  entry points (ROOTS: what tools/portcheck/build.py and tools/build_decisions.py run), never reads the switch: its
  variable's name, the module values made from it (roadsigns.ON, anything assigned from it, to a fixpoint) or a
  function that does. Reach over-approximates Python's calls: every function or class a function names (a class:
  all of its methods), what a module name it reads was assigned (a factory's function, a hook: `mod.X = f`), self /
  cls attributes onto the class's family (bases and subclasses), and any other object's attribute onto the
  same-named methods of every class reached (repeated until nothing is added). Two corpus runs traced with a profile
  hook (986 functions run while build.py planned Fells Point and Laramie, 2026-11-17) all lie inside it but for
  import-time code (module bodies and what they call) and the harness's own call (earthd.raise_open_files).
  Also: no switch has an import-time effect; the code each switch gates (its tests, what they guard, the locals made
  from it) writes only its own function's locals (its values flow out through returns to code the planning never
  runs) and passes nothing made from the switch to a planning function it calls (a consumer of the plans: catenary's
  masts on the graded ground); the planning reads the environment by name only (the keys' own functions aside); the
  native kernels read none of them. The guard is discriminating: every Experimental switch that does change plans or
  facts is seen (PLANS_READ), and a read planted in road_specs is found.

  Not covered by the static guard: state a gated tile function leaves in an object the planning later reads in the
  same process. The dynamic proof's sessions (tools/portcheck/session.py: road tiles, AI graph, blocks and plans in
  one server) found the run's plans identical with every listed switch on (the commit's report).

  dynamic (CorpusTest, opt-in: PLAN_NEUTRAL_CORPUS=1): tools/portcheck/build.py over the frozen corpus with each
  switch on and with none, compared with plancmp.py / factcmp.py: byte-identical plans and facts.

A change that makes the planning read a listed switch fails the static guard: take the switch off revisit.PLAN_NEUTRAL
in the same commit (its keys then hold it again, as before the list)."""
import ast
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))

import experimental  # noqa: E402
import revisit  # noqa: E402

# The planning's entry points (tools/portcheck/build.py, tools/build_decisions.py): a road process's state
# (blockworker.init: the map data, elevation, measured widths, surfaces, sea, airports), an anchor's grader and plan
# store, a grader's cell plans, a cell's roads layer, its level ranges and road specs (the cell facts)
ROOTS = (("blockworker", "init"), ("blockworker", "_State.anchor"), ("grading", "Grader.prepare"),
         ("mapdata", "MapData.cell"), ("roads", "level_ranges"), ("roads", "road_specs"))
# ... and what must be inside the closure (it is the planning, not a subset of it)
MUST_REACH = (("grading", "_Planner._plan_cell"), ("grading", "PlanStore.planner_of_cell"),
              ("plancache", "PlanCache.store"), ("plancache", "PlanCache._decided"), ("decisions", "plans"),
              ("roads", "_facts_store"), ("roads", "_facts_key"), ("roads", "TunnelProfiles.parts_todo"),
              ("railways", "grade_plans"), ("gradesep", "_underpasses"), ("decisions", "vkey_doc"))
# Tables of switch NAMES (keys, settings), not of their values: reading one is not reading a switch
NAME_TABLES = {("revisit", "PLAN_NEUTRAL"), ("revisit", "NOT_SWITCHES"), ("experimental", "FEATURES"),
               ("experimental", "IDS"), ("bakepack", "CELL_SCOPED")}
# The Experimental switches the planning does read (each changes plans or facts): the guard must see every one
PLANS_READ = ("BNE_BUMPS", "BNE_DIRT_BANK", "BNE_DIRT_BUMP", "BNE_ENTRANCE_FLARES", "BNE_EU_ROAD_DETAILS",
              "BNE_FR_ROADS", "BNE_GRADE_GUARD", "BNE_HRDEM_NEAR", "BNE_LIDARBC", "BNE_PHOTO_SURFACE",
              "BNE_RAIL_CURVES", "BNE_RAIL_TUNNELS", "BNE_RAIL_XING", "BNE_SUBSEA_RELAX", "BNE_TERRAIN_BUMPS",
              "BNE_TUNNEL_LIDS",
              # (the Revamped roads setting: tapers, deck K, level spacing, footbridges, rail lifecycle, raceways)
              "BNE_DECK_K_SPEED", "BNE_RACEWAY_DIG", "BNE_FAST_DECK_ENDS", "BNE_FAST_GAP", "BNE_FAST_DECK_GROUND", "BNE_RAIL_LIFECYCLE",
              "BNE_RAIL_SEAM_NATURAL",
              "BNE_SPEED_TAPERS")
# Experimental switches the planning does not read that are still NOT on the list, and why (each keeps its keys)
NOT_LISTED = {
    "BNE_GEN_GROUND": "its gated code (the base photo with other arguments: genground.render, blockworker.base_photo, "
                      "earthd.Server._base_in) stores into the server's own state and drives the photo pipeline whose "
                      "products feed planning inputs (roadsurf's verdicts measured from photos on the disk): not "
                      "provable by this guard",
    "BNE_FR_HOUSES": "comes only with BNE_FR_ROADS and BNE_EU_ROAD_DETAILS (the EU scenery setting), which the plans "
                     "read; its gated code also stores into its callers' objects (buildings.tile_buildings)",
    "BNE_STALE_REVISIT": "no build switch at all (revisit.NOT_SWITCHES)",
    "BNE_TUNNEL_ROAD_OVER": "road tiles' tunnel entrances only (portals.fit: the headwall's top under a road crossing "
                            "over the mouth); not yet proven plan-neutral both ways, so it stays keyed with the "
                            "Revamped roads setting",
    "BNE_FOOT_CLEAR": "railway tiles only (railways.FOOT_CLEAR: a footbridge that cannot clear drops its walls over the "
                      "road); not yet proven plan-neutral both ways, so it stays keyed with the Revamped roads setting",
    "BNE_RACEWAY_PROPS": "road tiles' props only (furniture.RACEWAY_PROPS: no street poles, wires or lamps along a "
                         "racetrack); comes with the Revamped roads setting, whose other switches the plans read, so "
                         "not proving it plan-neutral costs no shared plans; it stays keyed",
    "BNE_RAIL_ROAD_FIT": "railway tiles and station meshes only (railways.RAIL_ROAD_FIT: buffer stops, the 3D track's "
                         "ends at a level crossing; rails.py's crossing deck; railstations.ROAD_FIT: worked-out "
                         "platforms); not yet proven plan-neutral both ways, so it stays keyed with the Revamped "
                         "roads setting",
}
# Functions of the planning closure that read the environment by a computed name: the keys' own (they hash the
# switches' names and values), the proxy settings (a fixed list of proxy variables)
ENV_BY_COMPUTED_NAME = {("revisit", "_switch_keys"), ("revisit", "code_key"), ("bakepack", "switches"),
                        ("netfetch", "HttpsTransport._resolve_proxy")}


class Code:
    """server/*.py as units (top-level functions, methods, class bodies, module code) and what each names."""

    def __init__(self, server):
        self.mods = sorted(f[:-3] for f in os.listdir(server) if f.endswith(".py"))
        mods = set(self.mods)
        self.trees = {m: ast.parse((Path(server) / f"{m}.py").read_text(encoding="utf-8")) for m in self.mods}
        self.units, self.top, self.bases = {}, {}, {}
        self.classmeths = defaultdict(set)
        for m, t in self.trees.items():
            mod_body = []
            for n in t.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.top[(m, n.name)] = "f"
                    self.units[(m, n.name)] = [n]
                elif isinstance(n, ast.ClassDef):
                    self.top[(m, n.name)] = "c"
                    rest = []
                    for b in n.body:
                        if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            q = (m, f"{n.name}.{b.name}")
                            self.units[q] = [b]
                            self.classmeths[(m, n.name)].add(q)
                        else:
                            rest.append(b)
                    self.units[(m, n.name)] = rest + n.bases + n.decorator_list
                else:
                    mod_body.append(n)
                    for x in ast.walk(n):
                        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                            self.top.setdefault((m, x.id), "v")
            self.units[(m, "<module>")] = mod_body
        self.aliases = {}
        for m, t in self.trees.items():
            al = {}
            for n in ast.walk(t):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name.split(".")[0] in mods:
                            al[a.asname or a.name.split(".")[0]] = ("mod", a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0 and n.module.split(".")[0] in mods:
                    for a in n.names:
                        if a.name in mods and (n.module, a.name) not in self.top:
                            al[a.asname or a.name] = ("mod", a.name)
                        else:
                            al[a.asname or a.name] = ("from", n.module, a.name)
            self.aliases[m] = al
        for m, t in self.trees.items():
            for n in t.body:
                if isinstance(n, ast.ClassDef):
                    bs = []
                    for b in n.bases:
                        if isinstance(b, ast.Name):
                            a = self.aliases[m].get(b.id)
                            bs.append((a[1], a[2]) if a and a[0] == "from" else (m, b.id))
                        elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
                            a = self.aliases[m].get(b.value.id)
                            if a and a[0] == "mod":
                                bs.append((a[1], b.attr))
                    self.bases[(m, n.name)] = bs
        self.info = {q: self.refs(q[0], body) for q, body in self.units.items()}
        # what is assigned to a module name (anywhere: `X = f(...)`, `mod.X = obj.method`, under `global X`): calling
        # the name calls what the value names (a factory's function, a hook)
        self.var_info = defaultdict(lambda: (set(), set()))
        for m, t in self.trees.items():
            for x in ast.walk(t):
                if isinstance(x, (ast.Assign, ast.AnnAssign)) and x.value is not None:
                    for v in self._assigned_names(m, x):
                        refs, sa, oa, _s = self.refs(m, [x.value])
                        self.var_info[v][0].update(refs)
                        self.var_info[v][1].update(sa | oa)
        self._fam = None
        self._assigns = None

    def _assigned_names(self, m, x):
        """The module names (module, name) assignment x of module m stores into: its own module's names, and
        another module's attributes (mod.X = ...)."""
        out = set()
        for t_ in (x.targets if isinstance(x, ast.Assign) else [x.target]):
            for y in ast.walk(t_):
                if isinstance(y, ast.Name) and self.top.get((m, y.id)) == "v":
                    out.add((m, y.id))
                elif isinstance(y, ast.Attribute) and isinstance(y.value, ast.Name):
                    a = self.aliases[m].get(y.value.id)
                    if a and a[0] == "mod":
                        out.add((a[1], y.attr))
        return out

    def refs(self, m, body):
        """(units and module names {(module, name)}, self / cls attributes, other attributes, string constants)."""
        al = self.aliases[m]
        refs, sa, oa, strs = set(), set(), set(), set()
        for st in body:
            for n in ast.walk(st):
                if isinstance(n, ast.Name):
                    a = al.get(n.id)
                    if a and a[0] == "from":
                        refs.add((a[1], a[2]))
                    elif (m, n.id) in self.top:
                        refs.add((m, n.id))
                elif isinstance(n, ast.Attribute):
                    v = n.value
                    a = al.get(v.id) if isinstance(v, ast.Name) else None
                    if a and a[0] == "mod":
                        refs.add((a[1], n.attr))
                    elif isinstance(v, ast.Name) and v.id in ("self", "cls"):
                        sa.add(n.attr)
                    else:
                        oa.add(n.attr)
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and \
                        n.func.id in ("getattr", "hasattr", "setattr") and len(n.args) >= 2 and \
                        isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str):
                    v = n.args[0]
                    a = al.get(v.id) if isinstance(v, ast.Name) else None
                    if a and a[0] == "mod":
                        refs.add((a[1], n.args[1].value))
                    elif isinstance(v, ast.Name) and v.id in ("self", "cls"):
                        sa.add(n.args[1].value)
                    else:
                        oa.add(n.args[1].value)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                    strs.add(n.value)
        return refs, sa, oa, strs

    def family(self, c):
        """Class c with every class it shares a base or a subclass with (the classes self.x may dispatch to)."""
        if self._fam is None:
            par = {}

            def find(x):
                while par.get(x, x) != x:
                    x = par[x]
                return x
            for k, bs in self.bases.items():
                for b in bs:
                    ra, rb = find(k), find(b)
                    if ra != rb:
                        par[ra] = rb
            groups = defaultdict(set)
            for k in self.bases:
                groups[find(k)].add(k)
            self._fam = {k: groups[find(k)] for k in self.bases}
        return self._fam.get(c, {c})

    def closure(self, roots):
        """The units reachable from roots (the module docstring's rules)."""
        seen, classes, names_extra = set(), set(), set()
        stack = [r for r in roots if r in self.units and r[1] != "<module>"]
        while True:
            while stack:
                q = stack.pop()
                if q in seen:
                    continue
                seen.add(q)
                if q[0] == "=":         # (a module name's assigned values)
                    refs, attrs = self.var_info[q[1]]
                    names_extra.update(attrs)
                    sa = set()
                else:
                    refs, sa, _oa, _s = self.info[q]
                nxt = set()
                for r in refs:
                    if r in self.var_info and ("=", r) not in seen:
                        nxt.add(("=", r))
                    if self.top.get(r) == "c":
                        nxt.add(r)
                        nxt |= self.classmeths[r]
                        classes.add(r)
                    elif r in self.units and r[1] != "<module>":
                        nxt.add(r)
                if q[0] != "=" and "." in q[1]:
                    for f in self.family((q[0], q[1].split(".")[0])):
                        classes.add(f)
                        nxt |= {(f[0], f"{f[1]}.{a}") for a in sa if (f[0], f"{f[1]}.{a}") in self.units}
                stack.extend(x for x in nxt if x not in seen)
            names = set(names_extra)
            for q in seen:
                if q[0] != "=":
                    names |= self.info[q][2]
            add = {u for c in classes for u in self.classmeths[c] if u not in seen and u[1].split(".")[-1] in names}
            if not add:
                return {q for q in seen if q[0] != "="}
            stack = list(add)

    def flag_vars(self, env):
        """The module-level names holding env's value or anything made from it (assignments, to a fixpoint)."""
        if self._assigns is None:
            self._assigns = []
            for m, t in self.trees.items():
                for x in ast.walk(t):
                    if isinstance(x, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and x.value is not None:
                        refs, _sa, _oa, s = self.refs(m, [x.value])
                        tg = self._assigned_names(m, x) if not isinstance(x, ast.AugAssign) else {
                            (m, y.id) for y in ast.walk(x.target)
                            if isinstance(y, ast.Name) and self.top.get((m, y.id)) == "v"}
                        if tg:
                            self._assigns.append((refs, s, tg))
        tv = set()
        while True:
            n0 = len(tv)
            for refs, s, tg in self._assigns:
                if env in s or refs & tv:
                    tv |= tg - NAME_TABLES
            if len(tv) == n0:
                return tv

    def readers(self, env):
        """(flag names, the units reading env's name or one of them)."""
        tv = self.flag_vars(env)
        return tv, {q for q, (refs, _sa, _oa, s) in self.info.items()
                    if q[1] != "<module>" and (env in s or refs & tv)}

    def mentions(self, m, node, tv, env, local=frozenset()):
        refs, _sa, _oa, s = self.refs(m, [node])
        if refs & tv or env in s:
            return True
        return bool(local) and any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in local
                                   for n in ast.walk(node))

    def gated(self, q, tv, env, with_locals=False):
        """The code of unit q whose running or values depend on the switch: the local names made from it (to a
        fixpoint); every simple statement reading it or such a name (whole: its calls take the value); every if /
        while / for testing or iterating one (whole); the whole unit when such a test guards a return, raise,
        continue or break."""
        m, body = q[0], self.units[q]
        local = set()
        while True:
            n0 = len(local)
            for st in body:
                for n in ast.walk(st):
                    if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
                        tg, val = (n.targets if isinstance(n, ast.Assign) else [n.target]), n.value
                    elif isinstance(n, (ast.For, ast.comprehension)):
                        tg, val = [n.target], n.iter
                    elif isinstance(n, ast.withitem) and n.optional_vars is not None:
                        tg, val = [n.optional_vars], n.context_expr
                    else:
                        continue
                    if self.mentions(m, val, tv, env, local):
                        local |= {x.id for t in tg for x in ast.walk(t) if isinstance(x, ast.Name)}
            if len(local) == n0:
                break
        out, whole = [], []

        def visit(node):
            if isinstance(node, (ast.If, ast.While)):
                if self.mentions(m, node.test, tv, env, local):
                    out.append(node)
                    if _jumps(node):
                        whole.append(node)
                    return
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                if self.mentions(m, node.iter, tv, env, local):
                    out.append(node)
                    return
            elif isinstance(node, ast.stmt) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                       ast.ClassDef, ast.With, ast.AsyncWith, ast.Try)):
                if self.mentions(m, node, tv, env, local):
                    out.append(node)
                    return
            for ch in ast.iter_child_nodes(node):
                visit(ch)
        for st in body:
            visit(st)
        nodes = list(body) if whole else out
        return (nodes, local) if with_locals else nodes


def _jumps(node):
    """Does node hold a return / raise / continue / break of its own function (not of a nested one)?"""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
            return True
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return False


MUTATORS = {"append", "extend", "update", "setdefault", "add", "insert", "pop", "popitem", "clear", "remove",
            "discard", "move_to_end", "appendleft", "sort", "reverse", "fill", "put", "put_nowait", "write"}


def outside_writes(code, q, nodes):
    """What nodes (code of unit q) write besides the locals q makes itself: names under global / nonlocal, items or
    attributes of parameters, of self or of module names, and mutating calls on them."""
    params, assigned, glob = set(), set(), set()
    for st in code.units[q]:
        for n in ast.walk(st):
            if isinstance(n, ast.arg):
                params.add(n.arg)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                assigned.add(n.id)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                glob |= set(n.names)
    fresh = assigned - glob - params

    def base(e):
        while isinstance(e, (ast.Subscript, ast.Attribute, ast.Starred, ast.Call)):
            e = e.func if isinstance(e, ast.Call) else e.value
        return e

    def flat(t):
        if isinstance(t, (ast.Tuple, ast.List)):
            return [x for e in t.elts for x in flat(e)]
        return [t.value] if isinstance(t, ast.Starred) and isinstance(t.value, ast.Name) else [t]
    out = []
    for st in nodes:
        for n in ast.walk(st):
            if isinstance(n, ast.Assign):
                tg = n.targets
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                tg = [n.target]
            elif isinstance(n, ast.Delete):
                tg = n.targets
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS:
                b = base(n.func.value)
                if not (isinstance(b, ast.Name) and b.id in fresh):
                    out.append(f"{ast.unparse(n.func)} (line {n.lineno})")
                continue
            else:
                continue
            for e in (x for t in tg for x in flat(t)):
                if isinstance(e, ast.Name):
                    if e.id in glob:
                        out.append(f"global {e.id} (line {n.lineno})")
                elif not (isinstance(base(e), ast.Name) and base(e).id in fresh):
                    out.append(f"{ast.unparse(e)} (line {n.lineno})")
    return out


def _fresh_keys():
    with revisit._lock:
        revisit._switches.clear()
        revisit._code.clear()
    import bakepack
    bakepack._switches.clear()


def _clean_env():
    """os.environ without any keyed variable (patch.dict restores it)."""
    p = mock.patch.dict(os.environ, {})
    p.start()
    for k in [k for k in os.environ if revisit.keyed_env(k)]:
        os.environ.pop(k, None)
    return p


EXPERIMENTAL_VARS = sorted({k for _sid, env, _n in experimental.FEATURES for k in env})


def _switch_env(var):
    """The environment the settings page gives for the Experimental switch that sets var (with the switch it needs)."""
    for sid, env, needs in experimental.FEATURES:
        if var in env:
            return experimental.env_for({"version": 3, sid: True, **({needs: True} if needs else {})})
    return {var: "on"}


class StaticGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = Code(SERVER)
        cls.planning = cls.code.closure(ROOTS)

    def path_to(self, target):
        """A chain of units from ROOTS to target (the failure message: how the planning reaches it)."""
        code = self.code
        par, frontier = {}, [r for r in ROOTS if r in code.units]
        seen = set(frontier)
        while frontier and target not in seen:
            nxt = []
            for q in frontier:
                refs, sa, oa, _s = code.info[q]
                cand = {r for r in refs if r in code.units}
                for r in refs:
                    if code.top.get(r) == "c":
                        cand |= code.classmeths[r]
                cand |= {u for u in self.planning if "." in u[1] and u[1].split(".")[-1] in (sa | oa)}
                for u in cand & self.planning:
                    if u not in seen:
                        seen.add(u)
                        par[u] = q
                        nxt.append(u)
            frontier = nxt
        p = [target]
        while p[-1] in par:
            p.append(par[p[-1]])
        return " <- ".join(":".join(x) for x in p)

    def test_the_closure_is_the_planning(self):
        for q in ROOTS + MUST_REACH:
            self.assertIn(q, self.planning, q)
        self.assertLess(len(self.planning), len(self.code.units) * 0.6, "not everything")

    def test_no_planning_code_reads_a_plan_neutral_switch(self):
        for env in revisit.PLAN_NEUTRAL:
            with self.subTest(env=env):
                tv, readers = self.code.readers(env)
                self.assertTrue(readers, "a listed switch is read somewhere (else the list names a dead variable)")
                hit = sorted(readers & self.planning)
                self.assertEqual(hit, [], f"{env} is read by the planning: {[self.path_to(h) for h in hit][:3]} "
                                          "(take it off revisit.PLAN_NEUTRAL)")

    def test_the_guard_sees_the_switches_that_change_plans(self):
        for env in PLANS_READ:
            with self.subTest(env=env):
                self.assertIn(env, EXPERIMENTAL_VARS)
                _tv, readers = self.code.readers(env)
                self.assertTrue(readers & self.planning, f"{env}: the planning reads it, the guard must see it")

    def test_every_experimental_switch_is_accounted_for(self):
        listed = set(revisit.PLAN_NEUTRAL)
        self.assertTrue(listed <= set(EXPERIMENTAL_VARS), "only Experimental switches (each proven)")
        self.assertFalse(listed & set(revisit.NOT_SWITCHES))
        self.assertFalse(listed & set(PLANS_READ))
        self.assertFalse(listed & set(NOT_LISTED))
        self.assertEqual(set(EXPERIMENTAL_VARS), listed | set(PLANS_READ) | set(NOT_LISTED),
                         "a new Experimental switch: prove it plan-neutral (both ways) or add it to PLANS_READ / "
                         "NOT_LISTED")
        for env, why in revisit.PLAN_NEUTRAL.items():
            self.assertTrue(why.strip(), env)

    def test_no_import_time_effect(self):
        """A listed switch is read at import only to define module values (an assignment from the environment and
        plain conversions: no registration, no file, no other call)."""
        for env in revisit.PLAN_NEUTRAL:
            tv, _readers = self.code.readers(env)
            for m in self.code.mods:
                for st in self.code.units[(m, "<module>")]:
                    if not self.code.mentions(m, st, tv, env):
                        continue
                    with self.subTest(env=env, module=m, line=st.lineno):
                        self.assertIsInstance(st, (ast.Assign, ast.AnnAssign), ast.unparse(st)[:200])
                        calls = [ast.unparse(n.func) for n in ast.walk(st.value) if isinstance(n, ast.Call)]
                        self.assertEqual([c for c in calls if not _plain_call(c)], [], ast.unparse(st)[:200])

    def test_gated_code_writes_only_its_locals(self):
        """What a listed switch changes stays in its function's locals and return value (no store into a parameter,
        self, a module value or a global), so it reaches nothing but the callers, which the planning never runs."""
        for env in revisit.PLAN_NEUTRAL:
            tv, readers = self.code.readers(env)
            for q in sorted(readers):
                with self.subTest(env=env, unit=q):
                    self.assertEqual(outside_writes(self.code, q, self.code.gated(q, tv, env)), [])

    def test_gated_code_hands_the_planning_nothing_of_it(self):
        """A planning function the gated code calls (a consumer: catenary's masts on the graded ground, the AI graph's
        tunnel floors) gets no argument made from the switch: it plans what it would plan with the switch off."""
        planning_methods = {u[1].split(".")[-1] for u in self.planning if "." in u[1]}
        for env in revisit.PLAN_NEUTRAL:
            tv, readers = self.code.readers(env)
            for q in sorted(readers):
                nodes, local = self.code.gated(q, tv, env, with_locals=True)
                bad = []
                for st in nodes:
                    for n in ast.walk(st):
                        if not isinstance(n, ast.Call):
                            continue
                        f = n.func
                        if ast.unparse(f) == "os.environ.get":
                            continue            # (reading the switch itself)
                        refs = self.code.refs(q[0], [f])[0]
                        named = bool(refs & self.planning) or (
                            isinstance(f, ast.Attribute) and f.attr in planning_methods and not refs)
                        if named and any(self.code.mentions(q[0], a, tv, env, local)
                                         for a in list(n.args) + [k.value for k in n.keywords]):
                            bad.append(f"{ast.unparse(n)[:120]} (line {n.lineno})")
                with self.subTest(env=env, unit=q):
                    self.assertEqual(bad, [])

    def test_the_planning_reads_the_environment_by_name(self):
        """A computed name could be any switch's: only the keys' own functions (and the proxy list) do that."""
        found = set()
        for q in self.planning:
            for st in self.code.units[q]:
                for n in ast.walk(st):
                    if _environ_by_computed_name(n, self.code.aliases[q[0]], q[0], self.code):
                        found.add(q)
        self.assertEqual(found - ENV_BY_COMPUTED_NAME, set())

    def test_the_native_module_reads_none(self):
        """The planning's native kernels (earthfast) read no listed switch (their few environment reads are others)."""
        src = ROOT / "native" / "earthfast" / "src"
        if not src.is_dir():
            self.skipTest("no native sources")
        text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(src.rglob("*.rs")))
        for env in revisit.PLAN_NEUTRAL:
            self.assertNotIn(env, text)

    def test_the_guard_finds_a_planted_read(self):
        """A planted read of a listed switch in a planning function, directly and through a module value: seen."""
        code = Code(SERVER)
        env = sorted(revisit.PLAN_NEUTRAL)[0]
        tv, _r = code.readers(env)
        flag = sorted(tv)[0]
        q = ("roads", "road_specs")
        own = list(code.units[q])
        for planted in (f"os.environ.get({env!r})", f"{flag[0]}.{flag[1]}"):
            code.aliases["roads"].setdefault(flag[0], ("mod", flag[0]))
            code.units[q] = own + ast.parse(f"x = {planted}").body
            code.info[q] = code.refs("roads", code.units[q])
            _tv, readers = code.readers(env)
            self.assertIn(q, readers & code.closure(ROOTS), planted)


def _plain_call(func):
    """A call that only reads the environment or converts a value (no effect beyond its result)."""
    return func in ("os.environ.get", "bool", "int", "float", "str", "frozenset", "set", "tuple") or \
        func.endswith((".strip", ".lower", ".upper", ".split")) or func.startswith("os.environ.get(")


def _environ_by_computed_name(n, aliases, m, code):
    """A read of os.environ whose name is neither a string constant nor a module name holding one; or a loop over it."""
    def environ(e):
        return isinstance(e, ast.Attribute) and e.attr == "environ"

    def constant(e):
        if isinstance(e, ast.Constant):
            return True
        if isinstance(e, ast.Name) and code.top.get((m, e.id)) == "v":
            return True                 # (a module constant: the guard's string search covers its value)
        return False
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and environ(n.func.value):
        if n.func.attr in ("items", "keys", "values", "copy"):
            return True
        if n.func.attr in ("get", "pop", "setdefault") and n.args and not constant(n.args[0]):
            return True
    if isinstance(n, ast.Subscript) and environ(n.value) and not constant(n.slice):
        return True
    if isinstance(n, (ast.For, ast.comprehension)):
        it = n.iter
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "sorted" and it.args:
            it = it.args[0]
        if environ(it):
            return True
    return False


class KeysTest(unittest.TestCase):
    """The defaults keep today's keys; only plan-neutral switches on: the plans' keys are the defaults'; a switch off
    the list still keeps them apart; every other key holds every switch."""

    VERSIONS = {"overture": "2026-01-21.0", "source": "overture"}

    def setUp(self):
        self.addCleanup(_fresh_keys)
        self.addCleanup(_clean_env().stop)
        _fresh_keys()

    def keys(self, env):
        import bakepack
        import decisions
        with mock.patch.dict(os.environ, env):
            _fresh_keys()
            return {"plans_switches": revisit.plans_switches_key(),
                    "plans": revisit.format_key("plans", "9.21", "r10", "w1s1", self.VERSIONS, (14, 1, 2)),
                    "cellfacts": revisit.format_key("cellfacts", 21, "specs", ("14/1/2", 0, "{}"), (False,)),
                    "vkey": decisions.vkey(dict(self.VERSIONS))[0],
                    "bake": bakepack.switches(),
                    "switches": revisit.switches_key(), "code": revisit.code_key(),
                    "other_kind": revisit.format_marker("otherkind", 1)}

    def test_defaults_are_todays_keys(self):
        import bakepack
        import decisions
        import roads
        _fresh_keys()
        empty = hashlib.sha1().hexdigest()[:16]
        self.assertEqual(revisit.switches_key(), empty)
        self.assertEqual(revisit.plans_switches_key(), empty, "the same hash of the same (no) variables")
        for kind in ("plans", "cellfacts", "otherkind"):
            self.assertEqual(revisit.format_marker(kind, 7), f"fmt:{kind}:7:{empty}", "the marker as before")
        self.assertEqual(decisions.vkey_doc(dict(self.VERSIONS))["switches"], empty)
        old = hashlib.sha1(f"BNE_BRIDGE_PROFILE={roads.bridge_profile_mode()}".encode()).hexdigest()[:16]
        self.assertEqual(bakepack.switches(), old, "the packs' and BeamMP shares' switches as before")
        # (and with a switch off the list set, every key as before: the list takes nothing else out)
        with mock.patch.dict(os.environ, {"BNE_BUMPS": "1", "BNE_DEM_ZOOM": "13"}):
            _fresh_keys()
            h = hashlib.sha1(b"BNE_BUMPS=1BNE_DEM_ZOOM=13").hexdigest()[:16]
            self.assertEqual(revisit.switches_key(), h)
            self.assertEqual(revisit.plans_switches_key(), h)
            self.assertEqual(revisit.format_marker("plans", 7), f"fmt:plans:7:{h}")

    def test_only_neutral_switches_keep_the_plans_keys(self):
        base = self.keys({})
        plans_kinds = ("plans_switches", "plans", "cellfacts", "vkey", "bake")
        every = {}
        for env in sorted(revisit.PLAN_NEUTRAL):
            on = _switch_env(env)
            every.update(on)
            with self.subTest(env=env):
                got = self.keys(on)
                for k in plans_kinds:
                    self.assertEqual(got[k], base[k], k)
                for k in ("switches", "code", "other_kind"):
                    self.assertNotEqual(got[k], base[k], f"{k}: every other cache keeps it apart")
        got = self.keys(every)
        for k in plans_kinds:
            self.assertEqual(got[k], base[k], f"all of them together: {k}")

    def test_a_switch_off_the_list_still_counts(self):
        base = self.keys({})
        neutral = sorted(revisit.PLAN_NEUTRAL)[0]
        bumps = self.keys({"BNE_BUMPS": "1"})
        both = self.keys({"BNE_BUMPS": "1", neutral: "on"})
        for k in ("plans_switches", "plans", "cellfacts", "vkey", "bake"):
            self.assertNotEqual(bumps[k], base[k], k)
            self.assertEqual(both[k], bumps[k], f"{k}: the neutral one adds nothing to it")
        self.assertNotEqual(both["switches"], bumps["switches"])

    def test_the_settings_page_neutral_ones(self):
        """The settings page's switches whose variables are all listed keep the archives (the user's tester)."""
        base = self.keys({})
        seen = 0
        for sid, _env, needs in experimental.FEATURES:
            env = experimental.env_for({"version": 3, sid: True, **({needs: True} if needs else {})})
            if not set(env) <= set(revisit.PLAN_NEUTRAL):
                continue
            seen += 1
            with self.subTest(sid=sid):
                self.assertEqual(self.keys(env)["vkey"], base["vkey"])
        self.assertEqual(seen, len({sid for sid, env, _n in experimental.FEATURES
                                    if set(env) <= set(revisit.PLAN_NEUTRAL)}))
        self.assertGreater(seen, 0)


CORPUS = Path(os.environ.get("BNGE_PORTCHECK_CACHE") or os.path.expanduser("~/bnge-tests/portcheck/cache"))


@unittest.skipUnless(os.environ.get("PLAN_NEUTRAL_CORPUS") == "1",
                     "builds the corpus once per listed switch (minutes): set PLAN_NEUTRAL_CORPUS=1")
class CorpusTest(unittest.TestCase):
    """tools/portcheck/build.py with each listed switch on and with none, offline from the frozen corpus cache
    (PLAN_NEUTRAL_PLACES, default laramie: a small place with rails, crossings and gravel; the full corpus run is the
    commit's report): plancmp.py and factcmp.py find every plan and fact byte-identical."""

    def build(self, out, env):
        e = {k: v for k, v in os.environ.items() if not revisit.keyed_env(k)}
        e.update(env)
        r = subprocess.run(["nice", "-n", "15", sys.executable, str(ROOT / "tools" / "portcheck" / "build.py"),
                            "--cache", str(CORPUS), "--out", out, "--places", self.places], env=e,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def cmp(self, tool, a, b):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "portcheck" / tool), a, b, "--places", self.places],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip().splitlines()[-1:] if r.stdout.strip() else r.stderr

    def test_plans_and_facts_identical(self):
        if not CORPUS.is_dir():
            self.skipTest(f"no corpus cache at {CORPUS}")
        self.places = os.environ.get("PLAN_NEUTRAL_PLACES", "laramie")
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~/bnge-tests") if
                                         os.path.isdir(os.path.expanduser("~/bnge-tests")) else None) as tmp:
            base = os.path.join(tmp, "base")
            self.build(base, {})
            for env in sorted(revisit.PLAN_NEUTRAL):
                on = _switch_env(env)
                with self.subTest(env=env):
                    out = os.path.join(tmp, env)
                    self.build(out, on)
                    for tool in ("plancmp.py", "factcmp.py"):
                        code, last = self.cmp(tool, base, out)
                        self.assertEqual(code, 0, f"{env}: {tool}: {last}")


if __name__ == "__main__":
    unittest.main()
