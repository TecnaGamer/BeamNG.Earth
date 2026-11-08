"""Selected routing dependency regressions; no live game or server control."""
import json,sys,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'server/tests'),str(ROOT/'server')]
MODULES='''test_authoritative_perimeters test_fence_overhaul test_blockworker test_package_stamps test_vegetation test_furniture test_furniture_clearance test_i80_banking test_rural_lighting test_bridgeprofile test_bridge_clearance test_bridge_junction_surface test_bridge_scenarios test_terrainpatch test_facility_perimeters test_fences test_fence_buildings test_fence_regression test_fence_routing test_perimeters test_perimeters_client test_perimeter_continuity test_geodesy test_mapgeom test_revisit test_revisit_side test_buildings test_roadcorners test_roadgeo test_roadlanes test_roadmesh test_roads test_roads_refresh test_roadwidth test_roadz test_roadz_inputs test_road_classification test_road_seams test_smooth_road_collision test_sweden_roads'''.split()
if __name__=='__main__':
 start=time.perf_counter()
 import test_roadgeo
 test_roadgeo.RealDataTest.__unittest_skip__=True
 test_roadgeo.RealDataTest.__unittest_skip_why__='excluded from isolated suite: requires mutable external map cache and network'
 suite=unittest.defaultTestLoader.loadTestsFromNames(MODULES)
 result=unittest.TextTestRunner(verbosity=1).run(suite)
 report=dict(modules=MODULES,tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),seconds=time.perf_counter()-start,success=result.wasSuccessful(),failure_ids=[t.id() for t,_ in result.failures],error_ids=[t.id() for t,_ in result.errors],skip_reasons=[dict(test=t.id(),reason=reason) for t,reason in result.skipped])
 (ROOT/'probes/warren_authoritative_review/test_results.json').write_text(json.dumps(report,indent=2)+'\n')
 sys.exit(not result.wasSuccessful())
