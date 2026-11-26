"""Regression cases for the scanner, pairing and corpus gate. Run with unittest in this directory."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

import numpy as np

from checks import Terrain, boundary_rings, fragments, lane_metrics, outline_metrics
from common import HERE, REVAMPED, digest, places, read, save
from compare import historical, isolate, pair, ranking, store_definition, worst_places, TILE_FIELDS, LANE_FIELDS, UNITS


class MeshChecks(unittest.TestCase):
    def test_collision_buckets_and_lane_interior(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            subprocess.run(["c++","-std=c++17","-O2","-ffp-contract=off",HERE/"scan.cpp","-o",work/"scan"],
                           check=True)
            np.array([[0.,0.,40.,0.,4.]]).tofile(work/"lanes")
            rows = []
            for i, height in enumerate([.003,.012,.035,.080]):
                x = float(i*10)
                rows.extend([[1.,2*i,x,0,0,x+1,0,0,x,1,0],
                             [1.,2*i+1,x+1,0,height,x,0,height,x,-1,height]])
            np.array(rows).tofile(work/"triangles")
            text = subprocess.check_output([work/"scan",work/"triangles",work/"lanes"],text=True)
            counts = {r[0]:int(r[3]) for r in (line.split() for line in text.splitlines())}
            self.assertEqual([counts[k] for k in ("step1","step2","step3","step4","inLane")],[1,1,1,1,2])

            base=np.array([0,0,0, 2,0,0, 0,2,0],dtype=float)
            for dz,expected in ((.04,2.),(.06,0.)):
                overlay=base.copy()
                overlay[2::3]+=dz
                np.array([[1,0,*base],[1,1,*overlay]]).tofile(work/"triangles")
                text=subprocess.check_output([work/"scan",work/"triangles",work/"lanes","area"],text=True)
                area=sum(float(x.split()[4]) for x in text.splitlines() if x.startswith("overlap "))
                self.assertAlmostEqual(area,expected)

    def test_m0_audit_reads_the_scene(self):
        """The M0 audit (native/roadsys audit.rs) through checks.audit_input: a pad 3 cm under the ground road is a
        double owner with the terrain, a barrier across the lane a parapet in the carriageway."""
        import os
        import shutil
        from checks import audit
        from common import ROOT, TREE
        if not shutil.which("cargo"):
            self.skipTest("cargo needed for the audit binary")
        target = ROOT / "bin/roadsys-target"
        subprocess.run(["cargo", "build", "--release", "--quiet", "-j", "2", "--bin", "roadsys"],
                       cwd=TREE / "native/roadsys", check=True, env={**os.environ, "CARGO_TARGET_DIR": str(target)})
        terrain = Terrain.__new__(Terrain)
        terrain.origin = np.array([-40, -40])
        terrain.z = np.full((81, 81), 10.)
        terrain.mat = np.zeros((81, 81), int)
        quad = [[-30, -30], [30, -30], [30, 30], [-30, 30]]
        pad = [[[-10, -4, 9.97], [10, -4, 9.97], [10, 4, 9.97]], [[-10, -4, 9.97], [10, 4, 9.97], [-10, 4, 9.97]]]
        wall = [[[20.1, -5, 9.9], [20.1, 5, 9.9], [20.1, 5, 11.]], [[20.1, -5, 9.9], [20.1, 5, 11.], [20.1, -5, 11.]]]
        entries = [(3, True, [(0, np.array(pad))]), (5, True, [(0, np.array(wall))])]
        lines = [dict(level="ground", width=8., xy=[[-40., 0.], [40., 0.]])]
        with tempfile.TemporaryDirectory() as temp:
            got = audit(Path(temp), quad, terrain, entries, lines, target / "release/roadsys")
        self.assertTrue(100 < got["double_owner_terrain_m2"] < 200, got)
        self.assertTrue(3 <= got["parapet_in_cw_m"] <= 8, got)
        self.assertNotIn("handover_lip_over_5mm", {k for k, v in got.items() if v})

    def test_terrain_parity_and_holes(self):
        terrain = Terrain.__new__(Terrain)
        terrain.origin = np.zeros(2)
        terrain.z = np.array([[0,0,0],[0,1,0],[0,0,0]])
        terrain.mat = np.array([[0,0,0],[0,255,0],[0,0,0]])
        values, _ = terrain.sample(np.array([[.5,.5],[1.5,.5],[1.,1.],[-1.,0.]]))
        np.testing.assert_allclose(values,[.5,.5,np.nan,np.nan],equal_nan=True)
        # The exact same lattice pattern shifted by one world metre must switch triangles.
        terrain.origin=np.array([1,0])
        shifted,_=terrain.sample(np.array([[1.5,.5],[2.5,.5]]))
        np.testing.assert_allclose(shifted,[0,0])

    def test_bow_tie_and_mark_fragments(self):
        quad = np.array([[-5,-5],[5,-5],[5,5],[-5,5]])
        self.assertEqual(outline_metrics([[[0,0],[1,1],[0,1],[1,0]]],quad)["outline_crossings"],1)
        tri = np.array([[[0,0,0],[1,0,0],[0,1,0]],[[2,2,0],[2.1,2,0],[2,2.1,0]]])
        self.assertEqual(fragments(tri),1)

    def test_lane_profile_speed_and_pavement(self):
        from checks import surface
        terrain=Terrain.__new__(Terrain)
        terrain.origin=np.zeros(2)
        terrain.z=np.zeros((18,18))
        terrain.z[:,8]=.06
        terrain.mat=np.full((18,18),surface.ASPHALT)
        terrain.mat[4,8]=surface.DIRT
        quad=np.array([[1,2],[15,2],[15,6],[1,6]])
        lines=[dict(id="ridge",level="ground",width=3,lanes=1,paved=True,xy=[[2,4],[14,4]])]
        result=lane_metrics(lines,quad,terrain,[],[],[])
        self.assertEqual(result["missing_pavement_samples"],1)
        self.assertEqual(result["lane_samples"],6)
        for mph in (30,50,70):
            expected=.12/4*(mph*.44704)**2/9.80665
            self.assertAlmostEqual(result[f"ride_{mph}mph_max_g"],expected)

    def test_exterior_ring_omits_diagonals_and_degenerate_edges(self):
        vertex=np.array([[0,0],[2,0],[2,2],[0,2],[3,3]])
        rings=boundary_rings(vertex,[[0,1,2],[0,2,3],[4,4,4]])
        self.assertEqual(rings,[vertex[:4].tolist()])


class CorpusChecks(unittest.TestCase):
    def test_public_ids_and_strata(self):
        public = places(public_only=True)
        self.assertTrue(150 <= len(public) <= 250)
        self.assertTrue(all(p["region"] and p["why"] and p["tags"] and p["name"] for p in public))
        # (the sparse and extreme stratum, adaptive plan decision 2: ~40 places in many regions, each with its data note)
        sparse = [p for p in public if "sparse" in p["tags"]]
        self.assertGreaterEqual(len(sparse), 35)
        self.assertGreaterEqual(len({p["region"] for p in sparse}), 12)
        self.assertTrue(all(p.get("data") for p in sparse))
        tags = {tag for p in public for tag in p["tags"]}
        self.assertFalse({"stack","cloverleaf","diamond","slip","gravel","tram","rail-over-road",
                          "road-over-rail","racetrack","switchback","tunnel-portal","bridge-exit"}-tags)

    def test_no_cancellation_across_tiles(self):
        def scene(a,b):
            return dict(snapshot="same",code="same",tiles={k:dict(metrics={"step4":v,"lane_samples":1},seconds=1)
                                                          for k,v in zip(("A","B"),(a,b))})
        p={"id":"one"}
        result=pair(p,scene(10,0),scene(0,10))
        self.assertEqual(result["delta"]["step4"],0)
        self.assertEqual(result["regression"],10/UNITS["step4"])
        self.assertIn(["B","step4"],result["alerts"])
        self.assertEqual(result, json.loads(json.dumps(result)))
        bad=scene(10,0)
        bad["snapshot"]="different"
        with self.assertRaises(ValueError):
            pair(p,bad,scene(0,10))
        del bad["tiles"]["B"]
        with self.assertRaises(ValueError):
            pair(p,scene(10,0),{**bad,"snapshot":"same"})

    def test_two_audit_versions_are_not_paired(self):
        def scene(version):
            m = {"step4": 1, "lane_samples": 1, **({} if version is None else {"audit_version": version})}
            return dict(snapshot="same", code="same", tiles={"A": dict(metrics=m, seconds=1)})
        p = {"id": "one"}
        self.assertNotIn("audit_version", pair(p, scene(2.0), scene(2.0))["delta"])
        with self.assertRaises(ValueError):
            pair(p, scene(None), scene(2.0))   # (results from before the version: 1)
        with self.assertRaises(ValueError):
            pair(p, scene(2.0), scene(3.0))

    def test_missing_pair_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            result=ranking(Path(d),[{"id":"example"}],{"default":{},"revamped":{}},"code",30)
            self.assertEqual(result["complete"],0)
            self.assertIn("example",result["missing"])

    def test_nightly_population_detects_a_subset_and_duplicates(self):
        from compare import check_population
        ids=[p["id"] for p in places(public_only=True)]
        check_population({"places":ids},"public")
        for invalid in (ids[:-1],ids[:-1]+ids[:1]):
            with self.assertRaisesRegex(ValueError,"catalogue differs"):
                check_population({"places":invalid},"public")

    def test_confirmed_production_failure_requires_frozen_replays(self):
        p={"id":"failed"}
        configs={"default":{}, "revamped":REVAMPED}
        key=sorted(REVAMPED)[-1]   # (any of the set: read from server/experimental.py, which changes)
        error=dict(type="IndexError",file="server/roadmesh.py",function="example",message="out of range")
        identity=digest([p,30,"code"])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            folder=root/"failures"/p["id"]
            for env in ({}, REVAMPED, {key:REVAMPED[key]}, {k:v for k,v in REVAMPED.items() if k!=key}):
                row=dict(env=env,identity=identity,snapshot="fixed",code="code",error=error if key in env else None,
                         finished=[] if key in env else ["tile"])
                if not env:
                    row["tiles"]={"tile":{"metrics":{**dict.fromkeys(TILE_FIELDS|LANE_FIELDS,0),"lane_samples":10},"seconds":1}}
                save(folder/"trials"/(digest(env)[:16]+".json"),row)
            evidence=dict(id=p["id"],status="confirmed",identity=identity,snapshot="fixed",code="code",
                          sufficient_set=[key],candidate_error=error,initial_error=error,
                          without_set_error=None,failed_tile="tile",default_finished=["tile"],candidate_finished=[])
            save(folder/"attribution.json",evidence)
            result=ranking(root,[p],configs,"code",30)
            self.assertFalse(result["missing"])
            self.assertEqual(result["complete"],0)
            self.assertEqual(worst_places(result,3),["failed"])
            bad=folder/"trials"/(digest(REVAMPED)[:16]+".json")
            save(bad,{**read(bad),"snapshot":"changed"})
            result=ranking(root,[p],configs,"code",30)
            self.assertIn("incompatible failure trial",result["missing"]["failed"])

    def test_historical_same_mode_change_and_measurement_definition(self):
        p=places(public_only=True)[0]
        conf={"default":{}, "revamped":REVAMPED}
        with tempfile.TemporaryDirectory() as d:
            old,new=Path(d)/"accepted",Path(d)/"candidate"
            for root,code,n in ((old,"old",1),(new,"new",3)):
                manifest=dict(code=code,places=[p["id"]],radius=30,configs=conf)
                save(root/"manifest.json",manifest)
                store_definition(root,code)
                for label,env in conf.items():
                    metrics=dict.fromkeys(TILE_FIELDS | LANE_FIELDS,0)
                    tile=dict(metrics={**metrics,"step4":n,"lane_samples":5},seconds=1.)
                    save(root/p["id"]/(label+".json"),dict(tiles={"tile":tile},code=code,snapshot="equal",env=env,
                         id=p["id"],identity=digest([digest([p,30,code]),env])))
            results=historical(new,old,manifest)
            self.assertIn(["tile","step4"],results["default"]["places"][0]["alerts"])
            path=new/p["id"]/"default.json"
            damaged=read(path)
            del damaged["tiles"]["tile"]["metrics"]["missing_pavement_samples"]
            save(path,damaged)
            self.assertIn("missing measurements",historical(new,old,manifest)["default"]["missing"][p["id"]])
            save(old/"measurement.json",{"code":"old","definition":"changed"})
            with self.assertRaisesRegex(ValueError,"definitions differ"):
                historical(new,old,manifest)

    def test_bisect_requires_the_interacting_switches(self):
        needed=set(sorted(REVAMPED)[1:3])   # (two of the set that only matter together)
        p={"id":"fixture"}
        configs={"default":{}, "revamped":REVAMPED}
        def build(place,radius,configs,run,code):
            for label,env in configs.items():
                delta=10 if needed.issubset(env) else 0
                metrics=dict.fromkeys(TILE_FIELDS | LANE_FIELDS,0)
                tile={"metrics":{**metrics,"step4":delta,"lane_samples":10},"seconds":1}
                save(run/place["id"]/(label+".json"),dict(id=place["id"],env=env,tiles={"tile":tile},
                     code=code,snapshot="frozen",identity=digest([digest([place,radius,code]),env])))
            return True
        with tempfile.TemporaryDirectory() as d:
            run=Path(d)
            build(p,30,configs,run,"code")
            row=ranking(run,[p],configs,"code",30)["places"][0]
            out=isolate(p,row,run,30,"code",build)
            self.assertEqual(out["status"],"measured")
            self.assertEqual(set(out["sufficient_set"]),needed)
            self.assertEqual(out["delta_without_set"],0)

    def test_fresh_broker_requests_only_set_requested_experiments(self):
        from visuals import prepare, sheets, submit
        with tempfile.TemporaryDirectory() as d:
            run=Path(d)
            plan=run/"visuals"/"controls"
            pid=places(public_only=True)[0]["id"]
            refs=run/"references"/pid
            save(refs/"photos.json",{"photos":[]})
            (refs/"aerial.jpg").touch()
            save(run/"manifest.json",{"code":"fixture"})
            configs={"default":{}, "revamped":REVAMPED,
                     "without-v3":{k:v for k,v in REVAMPED.items() if k!="BNE_ROADSURF_V3"}}
            prepare(run,[pid],configs,plan)
            index=read(plan/"index.json")
            requests=[read(plan/(index["requests"][f"0/{label}"]+".json")) for label in configs]
            self.assertEqual([r["env"] for r in requests],list(configs.values()))
            self.assertTrue(all(r["device"]=="pc" for r in requests))
            self.assertTrue(all(r["shots"]==requests[0]["shots"] for r in requests))
            broker=run/"broker"
            submit(plan,broker)
            failed=index["requests"]["0/default"]
            (broker/(failed+".err")).write_text("game timed out")
            submit(plan,broker,retry_failed=True)
            after=read(plan/"index.json")
            self.assertEqual(after["requests"]["0/default"],failed+"-retry")
            self.assertEqual(read(broker/(failed+".json")),read(broker/(failed+"-retry.json")))
            self.assertEqual(index["requests"]["0/revamped"],after["requests"]["0/revamped"])
            prepare(run,[pid],configs,plan)
            self.assertEqual(after["requests"],read(plan/"index.json")["requests"])
            overhead=run/"visuals"/"overhead"
            prepare(run,[pid],configs,overhead,overhead_only=True)
            extra=read(overhead/"index.json")
            self.assertNotEqual(index["requests"],extra["requests"])
            shots=extra["entries"][0]["shots"]
            self.assertEqual(len(shots),1)
            self.assertEqual(shots[0]["cam"][:3],[0,0,145])
            del index["protocol"]
            save(plan/"index.json",index)
            with self.assertRaisesRegex(ValueError,"fresh captures"):
                sheets(run,plan,run/"broker")

    def test_sheet_uses_both_captures_and_checks_cameras(self):
        from PIL import Image
        from visuals import BROKER_PROTOCOL, sheets
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            plan=root/"visuals"/"worst"
            broker=root/"broker"
            broker.mkdir()
            entry={"id":"example","refs":[],"shots":[{"name":"overview"}]}
            save(root/"manifest.json",dict(code="fixture",configs={"default":{},"revamped":REVAMPED}))
            save(plan/"index.json",dict(entries=[entry],protocol=BROKER_PROTOCOL,code="fixture",configs=["default","revamped"],
                                        requests={"0/default":"first","0/revamped":"second"}))
            for name,rgb in (("first",(255,0,0)),("second",(0,0,255))):
                request={"env":{} if name=="first" else REVAMPED,"shots":[{"name":"p0_overview"}]}
                save(plan/(name+".json"),request)
                save(broker/(name+".json"),request)
                folder=broker/name
                folder.mkdir()
                file=folder/"p0_overview.png"
                Image.new("RGB",(120,80),rgb).save(file)
                if name=="second":
                    file=file.rename(folder/("pc-"+file.name))
                    save(broker/(name+".done"),{"device":"pc","files":[str(file)]})
                else:
                    save(broker/(name+".done"),{"files":[str(file)]})
            sheets(root,plan,broker)
            self.assertTrue((plan/"sheets.md").is_file())
            with Image.open(plan/"example.jpg") as sheet:
                self.assertGreater(sheet.getpixel((910,45))[0],240)
                self.assertGreater(sheet.getpixel((1360,45))[2],240)
            bad={"env":REVAMPED,"shots":[{"name":"p0_overview","cam":[0,0,50,0,0,-1]}]}
            for folder in (plan,broker):
                save(folder/"second.json",bad)
            with self.assertRaisesRegex(ValueError,"cameras"):
                sheets(root,plan,broker)
            for folder in (plan,broker):
                save(folder/"second.json",{**request,"env":{}})
            with self.assertRaisesRegex(ValueError,"switch environment"):
                sheets(root,plan,broker)
            for folder in (plan,broker):
                save(folder/"second.json",request)
            save(broker/"second.done",{"files":[]})
            with self.assertRaises(ValueError):
                sheets(root,plan,broker)


if __name__=="__main__":
    unittest.main()
