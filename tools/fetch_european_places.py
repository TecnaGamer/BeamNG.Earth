"""Pinned Overture localities for the European town-name scenery fallback.

Mapped French boards retain priority. The other countries' boards are estimated
at sustained built-up transitions, not administrative boundaries. This fetcher
is developer-only: players use the compact offline snapshot. Restart to reuse
the reader's cached byte ranges. Requires server requirements (DuckDB/fsspec).
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from fetch_country_bounds import Fetcher,RELEASE
import overture
import europeinfra


def fetch(cache,output,parquet=None):
    reader=overture.Reader(str(cache),Fetcher(),release=RELEASE)
    item_url=f'https://stac.overturemaps.org/{RELEASE}/divisions/division/00000/00000.json'
    try:
        if parquet:
            import duckdb
            con=duckdb.connect();errors=[]
            src="read_parquet('"+str(Path(parquet).resolve()).replace("'","''")+"')"
        else:
            item=json.load(urllib.request.urlopen(item_url,timeout=60))
            a=item['assets'];urls=[a['aws']['href'],a['azure']['href']];size=a['aws']['file:size'];name=urls[0].rsplit('/',1)[-1]
            remote=reader._remote('divisions','division',name,urls,size)
            con,proto,errors=reader._connect({name:remote},'demand')
            src=f"read_parquet('{proto}://{name}')"
        countries=','.join("'"+c+"'" for c in sorted(europeinfra.COUNTRIES-{'FR'}))
        sql=f'''SELECT id,country,class,names."primary",bbox.xmin,bbox.ymin,population
                 FROM {src} WHERE country IN ({countries})
                 AND (class IN ('city','town','village') OR (country='VA' AND subtype='country'))
                 AND names."primary" IS NOT NULL'''
        try:rows=con.execute(sql).fetchall()
        except Exception:
            if errors:print(errors[-1],file=sys.stderr)
            raise
        finally:con.close()
        places=[{'id':r[0],'country':r[1],'place':r[2] or 'city','name':r[3],'lon':r[4],'lat':r[5]}
                for r in rows if r[3].strip()]
        places.sort(key=lambda p:(p['country'],p['id']))
        metadata={'source':'Overture Maps divisions/division','release':RELEASE,'stac_item':item_url,
                  'license':'ODbL-1.0; see europe_places.NOTICE.md','selection':'class city/town/village, Vatican city-state point; France uses its OSM board snapshot',
                  'placement':'estimated built-up transition, not surveyed sign positions',
                  'counts':dict(sorted(Counter(p['country'] for p in places).items()))}
        raw=gzip.compress(json.dumps(dict(metadata,signs=[],places=places),ensure_ascii=False,separators=(',',':')).encode(),mtime=0)
        output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(raw)
        print(len(places),'places;',len(raw),'bytes; SHA256',hashlib.sha256(raw).hexdigest(),flush=True)
        print(metadata['counts'],flush=True)
    finally:reader.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--parquet',type=Path,help='Optional complete pinned Parquet download, avoids many small range reads')
    a=p.parse_args();fetch(a.cache,a.output,a.parquet)
