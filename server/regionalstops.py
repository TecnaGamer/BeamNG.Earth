"""Appearance-only replacements for stock STOP props. No placement/AI changes."""
from functools import lru_cache
import hashlib
from pathlib import Path

DATA_PATH=Path(__file__).with_name('data')/'quebec_bounds.zip'
DATA_SHA256='becfdfb3fc08c1adb450e2f941aef41a83512f8cf84800f76002f60a30fe38ae'

@lru_cache(maxsize=1)
def quebec():
    import countrybounds
    if hashlib.sha256(DATA_PATH.read_bytes()).hexdigest()!=DATA_SHA256:
        raise ValueError('Quebec boundary checksum mismatch')
    return countrybounds.WorldIndex(DATA_PATH)

def style(country,lat,lon):
    if country=='TR':return 'stop_dur'
    if country=='CA' and quebec().at(lat,lon)=='QC':return 'stop_arret'
    return 'stop'

def localize(rows,profile,frame,country_at):
    # fr-varied and all older/off modes keep the exact stock prop.
    if not isinstance(profile,(list,tuple)) or len(profile)!=3 or profile[1]!='europe':return rows
    result=[]
    for row in rows:
        if row[0]=='stop':
            country=country_at(*row[1:4])
            if country in ('TR','CA'):
                lat,lon,_=frame.enu_to_geodetic(*row[1:4])
                row=[style(country,float(lat),float(lon)),*row[1:]]
        result.append(row)
    return result
