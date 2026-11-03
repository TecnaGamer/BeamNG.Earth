"""Measure/render distant natural-cliff coverage without starting BeamNG.

Uses the normal cached DEM provider (fetches missing tiles). Run from repo root:
python tools/preview_far_cliffs.py --output build/grand-canyon-cliffs
"""
import argparse,json,sys,time
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import dem,farfield as ff,farcliffs as fc,geodesy


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='build/grand-canyon-cliffs')
    parser.add_argument('--cache',default=str(Path.home()/'.cache/beamng-earth'))
    parser.add_argument('--radius-tiles',type=int,default=4)
    args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    frame=geodesy.EnuFrame(36.14,-112.12)
    provider=dem.Dem(args.cache)
    layout=ff.Layout(64)
    records=[];meshes=[];count=0
    # L4 = 4096 m per tile, 64 m grid, covering ~33 km across with defaults.
    for j in range(-args.radius_tiles,args.radius_tiles):
        for i in range(-args.radius_tiles,args.radius_tiles):
            tile=ff.Tile(4,i,j)
            started=time.perf_counter()
            built=ff.build_tile(frame,provider,tile,12,ref_zoom=12,with_sink=False,layout=layout)
            t0=time.perf_counter()
            labels=fc.classify(built,frame)
            args_mesh=fc.geometry(built,frame,built.planar,built.detail_uvs,'terrain')
            cost=time.perf_counter()-t0
            p=built.positions+np.asarray(built.pos);t=built.tris
            v=p[t];n=np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0]);a=np.linalg.norm(n,axis=1)*.5
            record=dict(key=tile.key,triangles=len(t),cliff_triangles=int((labels>=0).sum()),
                cliff_area_km2=float(a[labels>=0].sum()/1e6),surface_area_km2=float(a[n[:,2]>0].sum()/1e6),
                added_triangles=0,added_vertices=0 if args_mesh is None else len(args_mesh['positions'])-len(p),
                material_seconds=cost,build_seconds=time.perf_counter()-started)
            records.append(record)
            meshes.append((p,t+count,labels));count+=len(p)
            print(tile.key,record['cliff_triangles'],'cliff triangles',round(cost*1000,1),'ms',flush=True)
    np.savez_compressed(out/'terrain.npz',positions=np.concatenate([m[0] for m in meshes]),
        tris=np.concatenate([m[1] for m in meshes]),labels=np.concatenate([m[2] for m in meshes]))
    report=dict(center=[36.14,-112.12],span_km=args.radius_tiles*2*4.096,grid_m=64,
        tiles=len(records),tiles_with_cliffs=sum(r['cliff_triangles']>0 for r in records),
        triangles=sum(r['triangles'] for r in records),cliff_triangles=sum(r['cliff_triangles'] for r in records),
        cliff_area_km2=sum(r['cliff_area_km2'] for r in records),added_triangles=0,
        material_ms_median=float(np.median([r['material_seconds']*1000 for r in records])),
        material_ms_p95=float(np.percentile([r['material_seconds']*1000 for r in records],95)),records=records)
    (out/'coverage.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='records'},indent=2),flush=True)
    provider.pool.shutdown();provider.urgent_pool.shutdown();provider.probe_pool.shutdown()

if __name__=='__main__': main()
