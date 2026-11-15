"""Build a self-contained attribute inspection map, without remote map tiles."""
import argparse
import html
import json
from pathlib import Path


def build(source, output, title):
    doc=json.loads(Path(source).read_text(encoding='utf-8'))
    rows=[]
    for f in doc['features']:
        p=f['properties'];lon,lat=f['geometry']['coordinates']
        rows.append(dict(id=p['id'],lon=lon,lat=lat,type=p.get('type_raw') or '?',
                         traffic=p.get('classification',{}).get('traffic','unknown'),
                         surface=p.get('bgt_anchor_observation','not_checked'),
                         goals=p.get('goals_raw',[])))
    payload=json.dumps(rows,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    page=TEMPLATE.replace('__TITLE__',html.escape(title)).replace('__ROWS__',payload)
    Path(output).write_text(page,encoding='utf-8')


TEMPLATE='''<!doctype html><html lang="nl"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>__TITLE__</title><style>
body{font:15px system-ui;margin:24px;color:#152436;background:#f4f6f9}h1{font-size:24px;margin-bottom:8px}
main{display:grid;grid-template-columns:minmax(450px,2fr) minmax(300px,1fr);gap:20px}canvas{width:100%;height:650px;background:white;border:1px solid #cad1da}
select,input{padding:9px;border:1px solid #bac3cf;border-radius:5px;margin:4px 5px 10px 0}#detail{background:white;padding:18px;white-space:pre-line;min-height:180px}
button{display:block;width:100%;text-align:left;padding:8px;border:0;border-bottom:1px solid #ddd;background:white;cursor:pointer}#list{max-height:400px;overflow:auto}
.muted{color:#4b5c70}#legend{font-size:13px;margin:8px 0} @media(max-width:800px){main{display:block}canvas{height:450px}}
</style><h1>__TITLE__</h1><p class="muted">Broncontrole, geen bewezen spelopstelling. Punten zijn NBd-ankers, geen gemeten mastvoeten. Richting, montagehoogte en actualiteit zijn nog niet bevestigd. Coördinatenplot zonder achtergrondkaart.</p>
<input id="search" aria-label="Zoek ID of bestemming" placeholder="Zoek ID of bestemming">
<select id="traffic" aria-label="Objectgroep"><option value="">Alle objectgroepen</option><option value="road_guide">Wegbewegwijzering</option><option value="bicycle">Fietsbewegwijzering</option><option value="unknown">Onbekende groep</option></select>
<select id="surface" aria-label="BGT-observatie"><option value="">Alle BGT-observaties</option></select><span id="count"></span>
<div id="legend">Blauw: vrij van verkeersvlak · rood: anker op verkeersvlak · oranje: nabij verkeersvlak · grijs: onbekende dekking of niet gecontroleerd</div>
<main><canvas id="map" width="1200" height="900" aria-label="Bronpuntenkaart"></canvas><section><div id="detail">Selecteer een punt of een object in de lijst.</div><h2>Objecten</h2><div id="list"></div></section></main>
<script>
const rows=__ROWS__,canvas=document.getElementById('map'),ctx=canvas.getContext('2d');
const $=id=>document.getElementById(id), names={anchor_clear_of_traffic_surface:'Vrij van verkeersvlak',anchor_on_traffic_surface:'Anker op verkeersvlak',anchor_near_traffic_surface:'Nabij verkeersvlak',outside_classified_bgt:'Buiten geclassificeerde BGT',not_checked:'BGT niet gecontroleerd'};
for(const value of [...new Set(rows.map(r=>r.surface))].sort()){const o=document.createElement('option');o.value=value;o.textContent=names[value]||value;$('surface').append(o)}
let shown=[],points=[],selected=null;
function select(row){selected=row; $('detail').textContent=`Object ${row.id}\nType: ${row.type} · ${row.traffic}\n${row.lat.toFixed(6)} N, ${row.lon.toFixed(6)} E\nBGT: ${names[row.surface]||row.surface}\n\nRuwe doelen: ${row.goals.join(' / ')||'ontbreken'}\n\nDit is geen gecontroleerde paneelindeling. Een portaalanker op de rijbaan is op zichzelf geen fout.`;draw()}
function draw(){ctx.clearRect(0,0,1200,900);points=[];if(!shown.length)return;
const xs=rows.map(r=>r.lon),ys=rows.map(r=>r.lat),west=Math.min(...xs),east=Math.max(...xs),south=Math.min(...ys),north=Math.max(...ys),aspect=Math.cos((north+south)/2*Math.PI/180);
const scale=Math.min(1100/Math.max((east-west)*aspect,.00001),800/Math.max(north-south,.00001));
for(const r of shown){const x=600+(r.lon-(east+west)/2)*aspect*scale,y=450-(r.lat-(north+south)/2)*scale;points.push({x,y,row:r});ctx.beginPath();ctx.arc(x,y,r===selected?8:4,0,Math.PI*2);ctx.fillStyle=({anchor_clear_of_traffic_surface:'#1565c0',anchor_on_traffic_surface:'#ba2533',anchor_near_traffic_surface:'#d97706'})[r.surface]||'#6b7280';ctx.fill();if(r===selected){ctx.strokeStyle='#111';ctx.lineWidth=2;ctx.stroke()}}
ctx.fillStyle='#334155';ctx.font='18px system-ui';ctx.fillText('N ↑',20,30)}
function update(){const text=$('search').value.toLocaleLowerCase();shown=rows.filter(r=>(!$('traffic').value||r.traffic===$('traffic').value)&&(!$('surface').value||r.surface===$('surface').value)&&(!text||(r.id+' '+r.goals.join(' ')).toLocaleLowerCase().includes(text)));$('count').textContent=shown.length+' / '+rows.length+' objecten';$('list').replaceChildren();for(const r of shown){const b=document.createElement('button');b.textContent=r.id+' · '+r.type+' · '+(r.goals[0]||'geen doelen');b.onclick=()=>select(r);$('list').append(b)}draw()}
for(const id of ['search','traffic','surface'])$(id).addEventListener('input',update);
canvas.addEventListener('click',e=>{const b=canvas.getBoundingClientRect(),x=(e.clientX-b.left)*1200/b.width,y=(e.clientY-b.top)*900/b.height;let best=null,d=144;for(const p of points){const v=(p.x-x)*(p.x-x)+(p.y-y)*(p.y-y);if(v<d){d=v;best=p.row}}if(best)select(best)});update();
</script></html>'''


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source');parser.add_argument('output');parser.add_argument('--title',default='NBd broncontrole')
    args=parser.parse_args();build(args.source,args.output,args.title)
