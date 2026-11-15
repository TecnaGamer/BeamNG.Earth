//! Source-backed junction approaches. All lane/stop/heading decisions stay native.
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use crate::dm::Dm;

fn num(v:&Value)->Option<f64>{v.as_f64().filter(|n|n.is_finite()&&n.abs()<1e7)}
fn point(v:&Value)->Option<[f64;3]>{Some([num(&v[0])?,num(&v[1])?,num(&v[2])?])}
fn lanes(v:&Value)->Option<usize>{let n=v.as_u64()?;if n<=16{Some(n as usize)}else{None}}
#[derive(Clone)]
struct Arm {nb:String, travel:[f64;2], incoming:usize, opposite:usize, radius:f64, speed:f64, length:f64}

// Graph edges are split at ordinary shape nodes. A stop/source can be farther
// upstream than the final short edge without belonging to another junction.
fn path(j:&str,a:&Arm,nodes:&serde_json::Map<String,Value>,arms:&BTreeMap<String,Vec<Arm>>,
        neighbours:&BTreeMap<String,BTreeSet<String>>)->Vec<([f64;3],f64)> {
    let mut out=vec![(point(&nodes[j]).unwrap(),a.radius)];
    let mut current=a.clone();let mut seen=BTreeSet::from([j.to_owned()]);let mut length=0.;
    loop {
        if !seen.insert(current.nb.clone()){break;}
        let Some(p)=nodes.get(&current.nb).and_then(point)else{break};
        length+=current.length;out.push((p,current.radius));
        if length>=100.||neighbours.get(&current.nb).map_or(0,|v|v.len())!=2 {break;}
        let choices:Vec<_>=arms.get(&current.nb).into_iter().flatten().filter(|b|
            !seen.contains(&b.nb)&&b.incoming==a.incoming&&b.opposite==a.opposite
            &&b.travel[0]*current.travel[0]+b.travel[1]*current.travel[1]>0.5).collect();
        if choices.len()!=1 {break;}current=choices[0].clone();
    }
    out
}
fn projection(path:&[([f64;3],f64)],q:[f64;2])->Option<f64>{
    let mut station=0.;let mut best=None;
    for pair in path.windows(2){
        let (a,b)=(pair[0].0,pair[1].0);let dx=b[0]-a[0];let dy=b[1]-a[1];let len=dx.dhypot(dy);
        let t=((q[0]-a[0])*dx+(q[1]-a[1])*dy)/len;
        let side=((q[0]-a[0])*dy-(q[1]-a[1])*dx).abs()/len;
        if t>=-4.&&t<=len&&station+t<=80.&&side<=pair[1].1.min(7.){
            if best.map_or(true,|(d,_)|side<d){best=Some((side,(station+t).max(0.)));}
        }
        station+=len;
    }
    best.map(|(_,s)|s)
}
fn station(path:&[([f64;3],f64)],mut back:f64)->Option<([f64;3],[f64;2],f64)>{
    for pair in path.windows(2){
        let (a,b)=(pair[0].0,pair[1].0);let dx=b[0]-a[0];let dy=b[1]-a[1];let len=dx.dhypot(dy);
        if back<=len {let t=back/len;return Some(([a[0]+dx*t,a[1]+dy*t,a[2]+(b[2]-a[2])*t],[-dx/len,-dy/len],pair[1].1));}
        back-=len;
    }
    None
}

/// Public motor-signal points are already transformed to the graph's ENU frame.
/// Unknown/empty source data never manufactures a new Dutch-controlled junction.
pub fn requests(graph:&Value,signals:&Value)->Result<Value,&'static str>{
    let nodes=graph["nodes"].as_object().ok_or("signal_graph_nodes")?;
    let edges=graph["edges"].as_array().ok_or("signal_graph_edges")?;
    let source=signals.as_array().ok_or("signal_graph_sources")?;
    if nodes.len()>100_000||edges.len()>200_000||source.len()>100_000{return Err("signal_graph_bounds");}
    let mut arms:BTreeMap<String,Vec<Arm>>=BTreeMap::new();
    let mut neighbours:BTreeMap<String,BTreeSet<String>>=BTreeMap::new();
    for e in edges {
        let (Some(a),Some(b))=(e[0].as_str(),e[1].as_str())else{continue};
        let (Some(pa),Some(pb))=(nodes.get(a).and_then(point),nodes.get(b).and_then(point))else{continue};
        let (Some(lo),Some(li),Some(radius),Some(speed))=(lanes(&e[3]),lanes(&e[4]),num(&e[7]),num(&e[5]))else{continue};
        if lo+li==0||lo+li>16||radius<1.||radius>40.{continue;}
        let dx=pb[0]-pa[0];let dy=pb[1]-pa[1];let len=dx.dhypot(dy);if len<3.||len>250.{continue;}
        neighbours.entry(a.into()).or_default().insert(b.into());
        neighbours.entry(b.into()).or_default().insert(a.into());
        if lo>0 {arms.entry(b.into()).or_default().push(Arm{nb:a.into(),travel:[dx/len,dy/len],incoming:lo,
            opposite:li,radius,speed,length:len});}
        if e[2].as_u64()!=Some(1)&&li>0 {arms.entry(a.into()).or_default().push(Arm{nb:b.into(),travel:[-dx/len,-dy/len],
            incoming:li,opposite:lo,radius,speed,length:len});}
    }
    // Match each source point to one topological junction, never every nearby
    // corner. Reject an equally plausible neighbour rather than duplicate it.
    let mut cells:BTreeMap<(i64,i64),Vec<&String>>=BTreeMap::new();
    for j in arms.keys(){
        if neighbours.get(j).map_or(0,|v|v.len())<3{continue;}
        if let Some(p)=nodes.get(j).and_then(point){
            cells.entry(((p[0]/128.).floor() as i64,(p[1]/128.).floor() as i64)).or_default().push(j);
        }
    }
    let paths:BTreeMap<_,_>=arms.iter().flat_map(|(j,aa)|aa.iter().map(|a|
        ((j.clone(),a.nb.clone()),path(j,a,nodes,&arms,&neighbours)))).collect();
    let mut evidence:BTreeMap<String,Vec<[f64;2]>>=BTreeMap::new();
    for s in source {
        let (Some(x),Some(y))=(num(&s[0]),num(&s[1]))else{return Err("signal_graph_source_point")};
        let mut candidates=Vec::new();
        let cell=((x/128.).floor() as i64,(y/128.).floor() as i64);
        let mut nearby=Vec::new();
        for dx in -1..=1{for dy in -1..=1{
            if let Some(ids)=cells.get(&(cell.0+dx,cell.1+dy)){nearby.extend(ids.iter().copied());}
        }}
        for j in nearby {
            let aa=&arms[j];
            let Some(p)=nodes.get(j).and_then(point)else{continue};
            let d=(x-p[0]).dhypot(y-p[1]);if d>85.{continue;}
            let aligned=aa.iter().any(|a|projection(&paths[&(j.clone(),a.nb.clone())],[x,y]).is_some());
            if d<8.||aligned{candidates.push((d,j));}
        }
        candidates.sort_by(|a,b|a.0.total_cmp(&b.0).then(a.1.cmp(b.1)));
        if candidates.len()>1&&(candidates[1].0-candidates[0].0).abs()<2.{continue;}
        if let Some((_,j))=candidates.first(){evidence.entry((*j).clone()).or_default().push([x,y]);}
    }
    let old=graph["signals"].as_array().cloned().unwrap_or_default();
    let mut signal_rows=old.clone();
    let mut groups=Vec::new();
    for (j,points) in evidence {
        point(&nodes[&j]).ok_or("signal_graph_junction")?;
        let mut aa=arms.remove(&j).unwrap_or_default();aa.sort_by(|a,b|a.nb.cmp(&b.nb));
        let mut apps=Vec::new();let mut approaches=Vec::new();
        let axis=aa.first().map(|a|a.travel).unwrap_or([0.,1.]);
        for a in aa {
            let path=&paths[&(j.clone(),a.nb.clone())];
            let mut back=num(&nodes[&j][3]).unwrap_or(a.radius).max(a.radius)+3.;
            for q in &points {
                if let Some(b)=projection(path,*q){if b>back{back=b+1.;}}
            }
            let Some((stop,travel,radius))=station(path,back)else{continue};
            let phase=if (axis[0]*a.travel[0]+axis[1]*a.travel[1]).abs()>0.7{0}else{1};
            let index=apps.len()+1;let owner=format!("bne_{j}_{index}");
            // The original graph's prop remains a fallback until renderer acceptance.
            // It is not used as a physical mast position by the native planner.
            let fallback=old.iter().find(|v|v["j"]==j).and_then(|v|v["app"].as_array())
                .and_then(|rows|rows.iter().find(|v|v[0]==a.nb));
            let mut app=vec![json!(a.nb),json!(stop[0]),json!(stop[1]),json!(stop[2]),
                json!(travel[0]),json!(travel[1]),json!(phase)];
            for k in 7..11{app.push(fallback.map(|v|v[k].clone()).unwrap_or(Value::Null));}
            apps.push(Value::Array(app));
            approaches.push(json!({"owner":owner,"index":index,"phase":phase,"request":{
                "stop":stop,"travel":travel,"lanes":a.incoming,"opposite":a.opposite,"radius":radius,
                "overhead":a.incoming>1||a.speed>=19.44,"before":20.,"after":3.}}));
        }
        if !apps.is_empty(){signal_rows.retain(|row|row["j"]!=j);
            signal_rows.push(json!({"j":j,"kind":"signal","app":apps}));
            groups.push(json!({"junction":j,"approaches":approaches}));}
        if groups.len()>128{return Err("signal_graph_junction_limit");}
    }
    Ok(json!({"signals":signal_rows,"groups":groups}))
}

#[cfg(test)]mod tests{
    use super::*;
    fn graph()->Value{json!({"nodes":{"j":[0.,0.,0.,3.],"w":[-60.,0.,0.,3.],"e":[60.,0.,0.,3.],
        "n":[0.,60.,0.,3.]},"edges":[["w","j",1,2,0,22.,1.,3.3],["j","e",1,2,0,22.,1.,3.3],
        ["j","n",0,1,1,13.,1.,3.3]],"signals":[]})}
    #[test]fn absent_source_never_invents_signals(){assert_eq!(requests(&graph(),&json!([])).unwrap()["groups"],json!([]));}
    #[test]fn source_at_junction_covers_only_incoming_lanes(){let p=requests(&graph(),&json!([[0.,0.]])).unwrap();
        assert_eq!(p["groups"][0]["approaches"].as_array().unwrap().len(),2);
        assert_eq!(p["groups"][0]["approaches"][1]["request"]["lanes"],2);
        assert_eq!(p["groups"][0]["approaches"][1]["request"]["opposite"],0);}
    #[test]fn source_on_outgoing_far_road_is_not_a_signal_junction(){assert_eq!(requests(&graph(),&json!([[50.,0.]])).unwrap()["groups"],json!([]));}
    #[test]fn repeated_evidence_has_one_group(){let p=requests(&graph(),&json!([[0.,0.],[-10.,0.]])).unwrap();
        assert_eq!(p["groups"].as_array().unwrap().len(),1);}
    #[test]fn explicit_roadside_station_moves_stop_before_head(){let p=requests(&graph(),&json!([[-25.,0.]])).unwrap();
        let r=&p["groups"][0]["approaches"][1]["request"];assert!(r["stop"][0].as_f64().unwrap() < -25.);}
    #[test]fn malformed_source_rejected(){assert!(requests(&graph(),&json!([[null,2.]])).is_err());}
    fn split_graph()->Value{let mut g=graph();g["nodes"]["s"]=json!([-5.,0.,0.,3.]);
        g["edges"][0]=json!(["s","j",1,2,0,22.,1.,3.3]);
        g["edges"].as_array_mut().unwrap().push(json!(["w","s",1,2,0,22.,1.,3.3]));g}
    #[test]fn source_beyond_short_final_edge_still_controls_all_incoming_lanes(){
        let p=requests(&split_graph(),&json!([[-25.,0.]])).unwrap();
        let apps=p["groups"][0]["approaches"].as_array().unwrap();assert_eq!(apps.len(),2);
        let r=&apps.iter().find(|a|a["request"]["lanes"]==2).unwrap()["request"];
        assert!((r["stop"][0].as_f64().unwrap()+26.).abs()<1e-6);
    }
    #[test]fn curved_upstream_station_uses_local_heading_and_height(){
        let mut g=split_graph();g["nodes"]["w"]=json!([-45.,-30.,2.,3.]);
        let p=requests(&g,&json!([[-25.,-15.]])).unwrap();
        let r=&p["groups"][0]["approaches"].as_array().unwrap().iter()
            .find(|a|a["request"]["lanes"]==2).unwrap()["request"];
        let t=&r["travel"];assert!((t[0].as_f64().unwrap()-0.8).abs()<1e-6);
        assert!((t[1].as_f64().unwrap()-0.6).abs()<1e-6);assert!(r["stop"][2].as_f64().unwrap()>0.);
    }
    #[test]fn upstream_search_never_crosses_another_junction(){
        let mut g=split_graph();g["nodes"]["extra"]=json!([-5.,-50.,0.,3.]);
        g["edges"].as_array_mut().unwrap().push(json!(["extra","s",1,1,0,13.,1.,2.]));
        let nodes=g["nodes"].as_object().unwrap();
        let a=Arm{nb:"s".into(),travel:[1.,0.],incoming:2,opposite:0,radius:3.3,speed:22.,length:5.};
        let neighbours=BTreeMap::from([("s".into(),BTreeSet::from(["j".into(),"w".into(),"extra".into()]))]);
        assert!(station(&path("j",&a,nodes,&BTreeMap::new(),&neighbours),20.).is_none());
    }
}
