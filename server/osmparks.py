"""Whole named protected areas from OSM US Layercake, complementing Overture parks.

Only agency/national/state-park identities are read, with unclipped polygon rings.
Cell snapshots and source range blocks are cached; stale snapshots work offline.
"""
import json
import os
from pathlib import Path
import tempfile
import time
from concurrent.futures import CancelledError

import numpy as np
import mapgeom
import osmtags
import recreationrules

URL = 'https://data.openstreetmap.us/layercake/parks.parquet'
PATH = 'osm/parks.parquet'
WEEK = 7*86400


class Parks(osmtags.OsmTags):
    DIR = 'osmparks'

    def __init__(self, md):
        super().__init__(md.raw_dir, md.fetcher, URL)
        self.directory=Path(md.cache_dir)/'protected-areas-v1'
        self.failed={}

    def cell(self, key):
        target=self.directory/f'{key.z}_{key.x}_{key.y}.npz'
        def load(fresh):
            try:
                if fresh and int(target.stat().st_mtime)//WEEK != int(time.time())//WEEK:return None
                with np.load(target,allow_pickle=False) as f:return mapgeom.Features.from_arrays('protected_areas',f,'')
            except (OSError,ValueError,KeyError):return None
        cached=load(True)
        if cached is not None:return cached
        if time.monotonic()-self.failed.get(key,-float('inf'))<60.:
            stale=load(False)
            if stale is not None:return stale
            raise OSError('Protected-area source awaiting retry')
        try:
            for attempt in (0,1):
                _,remote=self.version('demand')
                try:
                    features=self.read(remote,key.bounds())
                    break
                except osmtags.rangeread.Changed:
                    if attempt:raise
                    self._forget()
            target.parent.mkdir(parents=True,exist_ok=True)
            fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.tmp')
            try:
                with os.fdopen(fd,'wb') as stream:np.savez_compressed(stream,**features.to_arrays(''))
                os.replace(tmp,target)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            return features
        except CancelledError:raise
        except Exception:
            self.failed[key]=time.monotonic()
            stale=load(False)
            if stale is not None:return stale
            raise

    def read(self, remote, bounds):
        rd,files=self._reader,{PATH:remote}
        con,proto,errors=rd._connect(files,'demand')
        try:
            uri=f"'{proto}://{PATH}'";groups=self._groups(con,uri,remote,'demand')
            candidates=self._candidates(groups,bounds)
            columns=('bbox','name','operator','protection_title','access','boundary','leisure')
            if not candidates:return self.features([])
            rd._fetch(files,{PATH:[r for g in candidates for c in columns for r in groups[g]['cols'].get(c,())]},'demand')
            only=' OR '.join(f"file_row_number BETWEEN {groups[g]['first']} AND {groups[g]['first']+groups[g]['n']-1}" for g in candidates)
            x0,y0,x1,y1=map(float,bounds)
            rows=con.execute(f'SELECT file_row_number,name,operator,protection_title,access,boundary,leisure '
                             f'FROM read_parquet({uri},file_row_number=true) WHERE ({only}) AND '
                             f'bbox.xmin<={x1} AND bbox.xmax>={x0} AND bbox.ymin<={y1} AND bbox.ymax>={y0}').fetchall()
            selected={}
            for number,name,operator,title,access,boundary,leisure in rows:
                if isinstance(name,(tuple,list)):name=next((n for n in name if n),'')
                tags=dict(operator=operator or '',designation=title or '',access=access or '',boundary=boundary or '',leisure=leisure or '')
                if name and access not in ('private','no') and recreationrules.entrance(name,tags,'US'):
                    selected[int(number)]=(str(name),tags)
            if not selected:return self.features([])
            hit=[g for g in candidates if any(groups[g]['first']<=r<groups[g]['first']+groups[g]['n'] for r in selected)]
            rd._fetch(files,{PATH:[r for g in hit for c in ('id','type','geometry') for r in groups[g]['cols'].get(c,())]},'demand')
            try:con.execute('SET enable_geoparquet_conversion=false')
            except Exception:pass
            result=[]
            for number,ident,typ,wkb in con.execute(f'SELECT file_row_number,id,type,geometry FROM read_parquet({uri},file_row_number=true) '
                                                   f"WHERE file_row_number IN ({','.join(map(str,selected))})").fetchall():
                if wkb is None:continue
                geom=mapgeom.parse_wkb(bytes(wkb))
                if geom[0]==mapgeom.POLYGON:result.append((f'osm:{typ}:{ident}',*selected[int(number)],geom))
            return self.features(result)
        except Exception:
            for error in errors:
                if isinstance(error,osmtags.rangeread.Changed):raise error
            raise
        finally:con.close()

    @staticmethod
    def features(rows):
        f=mapgeom.build('protected_areas',np.array([r[0] for r in rows],dtype='U80'),[r[3] for r in rows])
        f.attrs.update(name=np.array([r[1] for r in rows],dtype=str),tags=np.array([json.dumps(r[2]) for r in rows],dtype=str),
                       **{'class':np.full(len(rows),'protected_area')})
        return f
