"""Report actual welcome-art coverage; never count registry slots as completed signs."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import travelboards


def audit():
    rows=[]
    for code, info in sorted(travelboards.registry()['states'].items()):
        rows.append(dict(state=code,name=info['name'],enabled=bool(travelboards.current(code)),
                         status=info['status'],source_date=info.get('source_date',''),
                         source=info.get('source_page',info.get('current_source',info.get('reference','')))))
    states=[row for row in rows if row['state']!='DC']
    return dict(enabled_states=sum(row['enabled'] for row in states),
                verified_current_states=sum(row['enabled'] and row['status']=='current_verified' for row in states),
                newest_available_states=sum(row['enabled'] and row['status']=='newest_available' for row in states),
                not_applicable_states=sum(row['status']=='not_applicable_no_land_border' for row in states),
                total_states=len(states),states=rows)


if __name__=='__main__':
    print(json.dumps(audit(),indent=2))
