"""The state welcome boards' coverage: photographed faces (with their licences), lettered boards, none."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
import travelboards


def audit():
    rows=[]
    for code, info in sorted(travelboards.registry()['states'].items()):
        rows.append(dict(state=code,name=info['name'],board=info['board'],license=info.get('license',''),
                         source=info.get('source_page',''),why_generic=info.get('why_generic','')))
    states=[row for row in rows if row['state']!='DC']
    return dict(photo_faces=sum(row['board']=='photo' for row in states),
                lettered_boards=sum(row['board']=='generic' for row in states),
                no_board=sum(row['board']=='none' for row in states),total_states=len(states),states=rows)


if __name__=='__main__':
    print(json.dumps(audit(),indent=2))
