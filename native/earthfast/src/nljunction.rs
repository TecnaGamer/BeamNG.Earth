//! Complete junction solve: checked shared feet, then upstream stations before omission.
use serde_json::{json,Value};
use crate::dm::Dm;
use crate::{nlassembly_core::{self,Request},nlparts,nlshared,nlsurface_core::Scene};

pub fn solve(scene:&Scene,requests:&[Request],c:&Value,turns:&[Vec<String>],zones:&[Vec<i8>],
             exits:&[bool],owners:&[String],neighbours:&[Value])->Result<Value,&'static str>{
    let n=requests.len();
    if n==0||n>16||[turns.len(),zones.len(),exits.len(),owners.len()].iter().any(|x|*x!=n){return Err("junction_context");}
    let mut rows:Vec<Value>=Vec::new();let mut stops=Vec::new();let mut last="junction_no_safe_assembly";
    for i in 0..n {
        let mut original=requests[i].clone();
        let norm=original.travel[0].dhypot(original.travel[1]);
        if !norm.is_finite()||norm<0.5{return Err("invalid_approach_geometry");}
        original.travel[0]/=norm;original.travel[1]/=norm;
        let mut selected=None;
        for shift in [0.,1.,2.,3.,4.,6.,8.,10.,12.,16.,20.] {
            if shift>original.before{continue;}
            let mut r=original.clone();r.stop[0]-=r.travel[0]*shift;r.stop[1]-=r.travel[1]*shift;
            r.before-=shift;if shift>0.{r.after=0.;}
            let plan=match nlassembly_core::plan(scene,&r){Ok(p)=>p,Err(e)=>{
                if shift==0.&&matches!(e,"surface_tiles_pending"|"ground_tiles_pending"){return Err(e);}
                last=e;continue;
            }};
            let mut choices=Vec::new();
            if plan.groups.len()==1{
                let here=plan.groups[0].foot.xyz;
                for old in &rows{for p in old["parts"].as_array().ok_or("junction_parts")?{
                    if p["role"]!="support"{continue;}
                    let foot=[p["pose"][0].as_f64().ok_or("junction_pose")?,p["pose"][1].as_f64().ok_or("junction_pose")?,
                              p["pose"][2].as_f64().ok_or("junction_pose")?];
                    if (foot[0]-here[0]).dhypot(foot[1]-here[1])<=1.2{
                        if let Some(p)=nlassembly_core::share_foot(scene,&r,&plan,foot){choices.push(p);}
                    }
                }}
            }
            choices.push(plan);
            for p in choices {
                let a=match nlparts::expand(&r,&p,c,&turns[i],&zones[i],exits[i]){Ok(a)=>a,Err(e)=>{last=e;continue;}};
                let mut trial=rows.clone();trial.push(a.clone());
                let bundle=match nlshared::bundle(&trial,&owners[..=i],c){Ok(b)=>b,Err(e)=>{last=e;continue;}};
                let mut all=neighbours.to_vec();all.extend(bundle["parts"].as_array().ok_or("junction_parts")?.iter().cloned());
                if let Err(e)=nlshared::clearance(&all,c){last=e;continue;}
                // Controllers stop before the earliest primary head. This is an
                // inferred station, never represented as a surveyed stop line.
                let mut back=0_f64;
                for h in a["parts"].as_array().ok_or("junction_parts")?.iter().filter(|h|h["role"]=="head"&&h["repeat"]==false){
                    let hx=h["pose"][0].as_f64().ok_or("junction_pose")?;
                    let hy=h["pose"][1].as_f64().ok_or("junction_pose")?;
                    back=back.max((r.stop[0]-hx)*r.travel[0]+(r.stop[1]-hy)*r.travel[1]);
                }
                let lateral=-r.radius+(r.opposite as f64+r.lanes as f64/2.)*p.lane_width;
                let stop=[r.stop[0]-r.travel[0]*(back+2.)+r.travel[1]*lateral,
                          r.stop[1]-r.travel[1]*(back+2.)-r.travel[0]*lateral,r.stop[2]];
                selected=Some((a,stop));break;
            }
            if selected.is_some(){break;}
        }
        let Some((row,stop))=selected else{return Err(last)};
        rows.push(row);stops.push(stop);
    }
    let mut out=nlshared::bundle(&rows,owners,c)?;out["stops"]=json!(stops);out["stop_source"]=json!("inferred_native_station");Ok(out)
}

#[cfg(test)]mod tests{
    use super::*;
    use crate::nlsurface_core::{Surface,Terrain};
    fn scene(n:usize)->Scene{
        let w=n as f64*1.65;
        Scene::new(vec![Surface{kind:1,triangle:[[-w,-50.,0.],[w,-50.,0.],[w,50.,0.]]},
            Surface{kind:1,triangle:[[-w,-50.,0.],[w,50.,0.],[-w,50.,0.]]}],
            Terrain{origin:[-100.,-100.],step:100.,width:3,height:3,z:vec![0.;9],holes:vec![]},
            vec![[-100.,-100.,100.,100.]]).unwrap()
    }
    fn req(n:usize)->Request{Request{stop:[0.,0.,0.],travel:[0.,1.],lanes:n,opposite:0,
        radius:n as f64*1.65,overhead:n>1,before:20.,after:3.}}
    fn catalog()->Value{serde_json::from_str(include_str!("../../../docs/assets/groenpol/components.json")).unwrap()}
    #[test]fn nearby_safe_standing_feet_are_combined(){
        let a=req(1);let mut b=a.clone();b.stop[0]=0.8;
        let out=solve(&scene(1),&[a,b],&catalog(),&[vec!["left".into()],vec!["right".into()]],
            &[vec![0],vec![0]],&[false,false],&["a".into(),"b".into()],&[]).unwrap();
        assert_eq!(out["parts"].as_array().unwrap().iter().filter(|p|p["role"]=="support").count(),1);
        assert_eq!(out["shared_posts"].as_array().unwrap().len(),1);
    }
    #[test]fn intersecting_parallel_arms_move_station_instead_of_morphing(){
        let a=req(3);
        let out=solve(&scene(3),&[a.clone(),a],&catalog(),&[vec!["through".into();3],vec!["through".into();3]],
            &[vec![0],vec![0]],&[false,false],&["a".into(),"b".into()],&[]).unwrap();
        assert!(out["stops"][1][1].as_f64().unwrap()<out["stops"][0][1].as_f64().unwrap()-0.5);
        nlshared::clearance(out["parts"].as_array().unwrap(),&catalog()).unwrap();
    }
    #[test]fn external_neighbour_steel_is_checked(){
        let a=req(3);
        let out=solve(&scene(3),&[a.clone()],&catalog(),&[vec!["through".into();3]],&[vec![0]],
            &[false],&["a".into()],&[]).unwrap();
        let next=solve(&scene(3),&[a],&catalog(),&[vec!["through".into();3]],&[vec![0]],
            &[false],&["b".into()],out["parts"].as_array().unwrap()).unwrap();
        assert!(next["stops"][0][1].as_f64().unwrap()<out["stops"][0][1].as_f64().unwrap()-0.5);
    }
}
