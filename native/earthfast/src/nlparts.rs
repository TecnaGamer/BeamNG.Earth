//! Expand grounded supports into authored sockets and independently aimed parts.
//! The client receives finished matrices; it does not infer lanes or read roads.
use serde_json::{json,Value};
use crate::nlassembly_core::{Kind,Plan,Request,lens_mm};
type Vec3=[f64;3];
type Basis=[f64;9]; // row-major, before conversion to the wire's basis columns
fn vec(v:&Value)->Result<Vec3,&'static str>{
    let a=v.as_array().ok_or("catalog_vector")?;
    if a.len()!=3{return Err("catalog_vector");}
    let mut out=[0.;3];for i in 0..3{out[i]=a[i].as_f64().filter(|v|v.is_finite()&&v.abs()<100.).ok_or("catalog_vector")?;}
    Ok(out)
}
fn basis(v:&Value)->Result<Basis,&'static str>{
    let a=v.as_array().ok_or("catalog_basis")?;
    if a.len()!=9{return Err("catalog_basis");}
    let mut out=[0.;9];for i in 0..9{out[i]=a[i].as_f64().filter(|v|v.is_finite()&&v.abs()<1.001).ok_or("catalog_basis")?;}
    for i in 0..3{for j in 0..3{
        let dot=(0..3).map(|k|out[k*3+i]*out[k*3+j]).sum::<f64>();
        if (dot-if i==j{1.}else{0.}).abs()>1e-4{return Err("catalog_basis");}
    }}
    let det=out[0]*(out[4]*out[8]-out[5]*out[7])-out[1]*(out[3]*out[8]-out[5]*out[6])+out[2]*(out[3]*out[7]-out[4]*out[6]);
    if det<0.9999{return Err("catalog_basis");}Ok(out)
}
fn shape(v:&Value)->Result<&str,&'static str>{
    let s=v.as_str().ok_or("catalog_shape")?;
    if !s.starts_with("/levels/beamng_earth/art/shapes/beamngEarth/groenpol/")||!s.ends_with(".dae")
        ||s.contains("..")||s.len()>240{return Err("catalog_shape");}Ok(s)
}
fn rotate(b:Basis,p:Vec3)->Vec3{
    [b[0]*p[0]+b[1]*p[1]+b[2]*p[2],b[3]*p[0]+b[4]*p[1]+b[5]*p[2],b[6]*p[0]+b[7]*p[1]+b[8]*p[2]]
}
fn mul(a:Basis,b:Basis)->Basis{
    let mut out=[0.;9];for i in 0..3{for j in 0..3{out[i*3+j]=(0..3).map(|k|a[i*3+k]*b[k*3+j]).sum();}}out
}
fn at(origin:Vec3,b:Basis,p:Vec3)->Vec3{let q=rotate(b,p);[origin[0]+q[0],origin[1]+q[1],origin[2]+q[2]]}
fn part(key:String,role:&str,model:&Value,p:Vec3,b:Basis,scale:Vec3)->Result<Value,&'static str>{
    let shape=shape(model)?;
    if !p.iter().chain(scale.iter()).all(|v|v.is_finite())||scale.iter().any(|v|*v<=0.||*v>40.){return Err("invalid_part_transform");}
    Ok(json!({"key":key,"role":role,"shape":shape,"pose":[p[0],p[1],p[2],
        b[0]*scale[0],b[3]*scale[0],b[6]*scale[0],b[1]*scale[1],b[4]*scale[1],b[7]*scale[1],b[2]*scale[2],b[5]*scale[2],b[8]*scale[2]]}))
}
fn variant(turn:&str,mm:u16)->String{
    let base=if mm==300{"vehicle_300"}else{"vehicle"};
    // Through/shared/unknown lanes retain a full lens, never an invented arrow.
    let suffix=match turn{"left"=>"_arrow_left","right"=>"_arrow_right",
        "slight_left"=>"_arrow_slight_left","slight_right"=>"_arrow_slight_right",_=>""};
    format!("{base}{suffix}")
}
fn head(c:&Value,variant:&str,key:String,lane:usize,repeat:bool,world_socket:Vec3,b:Basis)->Result<Value,&'static str>{
    let h=&c["heads"][variant];
    if h["participant"].as_str()!=Some("vehicle")||h["interface"].as_str()!=Some("pole_cap_145"){return Err("vehicle_head_unavailable");}
    let socket=vec(&h["socket"]["position"])?;
    let sb=basis(&h["socket"]["basis"])?;
    let inverse=[sb[0],sb[3],sb[6],sb[1],sb[4],sb[7],sb[2],sb[5],sb[8]];
    let rotation=mul(b,inverse);let offset=rotate(rotation,socket);
    let origin=[world_socket[0]-offset[0],world_socket[1]-offset[1],world_socket[2]-offset[2]];
    let mut out=part(key,"head",&h["shape"],origin,rotation,[1.;3])?;
    out["lane"]=json!(lane);out["repeat"]=json!(repeat);out["variant"]=json!(variant);
    if !h["forest"].is_null(){
        for role in ["body","red","amber","green"]{shape(&h["forest"][role])?;}
        out["forest"]=h["forest"].clone();
    }
    Ok(out)
}

/// Expands only a successful, complete physical plan. `zones` covers the whole
/// junction; a mixed boundary and every motorway exit always choose 300 mm.
pub fn expand(r:&Request,p:&Plan,c:&Value,turns:&[String],zones:&[i8],exit:bool)->Result<Value,&'static str>{
    if c["format"].as_u64()!=Some(5)||turns.len()!=r.lanes||zones.len()>128
        ||zones.iter().any(|v|!(-1..=1).contains(v)){return Err("assembly_context");}
    let norm=(r.travel[0]*r.travel[0]+r.travel[1]*r.travel[1]).sqrt();
    if !norm.is_finite()||norm<0.5{return Err("assembly_direction");}
    let travel=[r.travel[0]/norm,r.travel[1]/norm];let right=[travel[1],-travel[0]];
    let front=[-travel[0],-travel[1]];
    let root:Basis=[front[0],right[0],0.,front[1],right[1],0.,0.,0.,1.];
    let reverse=mul(root,[-1.,0.,0.,0.,-1.,0.,0.,0.,1.]);
    let mm=lens_mm(zones,exit);let mut parts=Vec::new();
    let supports=&c["supports"];let straight=&supports["straight"];
    let cap=vec(&straight["mounts"][0]["position"])?[2];
    let portal=p.kind==Kind::Portal;
    if portal&&p.groups.len()!=2{return Err("portal_feet");}
    let template=if portal{&supports["portal_5"]}else{&supports["cantilever_3"]};
    let construction=&template["construction"];
    let mounts=template["mounts"].as_array().ok_or("catalog_mounts")?;
    if mounts.is_empty(){return Err("catalog_mounts");}
    for (gi,g) in p.groups.iter().enumerate(){
        let stem=format!("support:{gi}");let steel=if g.mirrored{reverse}else{root};
        let mut scale=[1.;3];
        if portal&&gi==1{
            let h=vec(&construction["leg_bounds_m"][1])?[2];
            scale[2]=(p.groups[0].foot.xyz[2]+h-g.foot.xyz[2])/h;
        }
        parts.push(part(stem.clone(),"support",if g.standing{&straight["shape"]}else{&construction["leg_shape"]},g.foot.xyz,steel,scale)?);
        if !g.standing&&!g.lanes.is_empty(){
            let length=if portal{(p.groups[0].foot.lateral-p.groups[1].foot.lateral).abs()}else{
                g.lanes.iter().map(|i|(-r.radius+(r.opposite as f64+*i as f64+0.5)*p.lane_width-g.foot.lateral).abs()).fold(0.,f64::max)
            };
            if length<=0.||length>40.||!portal&&length>=12.{return Err("beam_reach");}
            parts.push(part(format!("beam:{gi}"),"beam",&construction["beam_shape"],g.foot.xyz,steel,[1.,length,1.])?);
        }
        for (slot,&lane) in g.lanes.iter().enumerate(){
            if lane>=turns.len(){return Err("assembly_lane");}
            let variant=variant(&turns[lane],mm);
            if g.standing{
                parts.push(head(c,&variant,format!("lane:{lane}"),lane,false,at(g.foot.xyz,root,[0.,0.,cap]),root)?);
            }else{
                let mount=&mounts[slot.min(mounts.len()-1)];
                let local=vec(&mount["position"])?;let tilt=basis(&mount["basis"])?;
                let lane_lateral=-r.radius+(r.opposite as f64+lane as f64+0.5)*p.lane_width;
                let socket=[r.stop[0]+right[0]*lane_lateral-travel[0]*g.foot.back+front[0]*local[0],
                            r.stop[1]+right[1]*lane_lateral-travel[1]*g.foot.back+front[1]*local[0],
                            g.foot.xyz[2]+local[2]];
                let world_basis=mul(root,tilt);
                let hardware=mount["hardware"].as_str().ok_or("mount_hardware")?;
                parts.push(part(format!("hanger:{lane}"),"hardware",&c["hardware"][hardware]["shape"],socket,world_basis,[1.;3])?);
                parts.push(head(c,&variant,format!("lane:{lane}"),lane,false,socket,world_basis)?);
            }
        }
        if !g.standing{
            let lane=if g.mirrored{0}else{r.lanes-1};let low=&c["low_repeat"];
            // The original loose foot projects along local X, perpendicular to
            // the bar's Y. On a mirrored post its quarter-turn is 180 degrees;
            // the post/bar stay mirrored while the head still faces the driver.
            parts.push(part(format!("low_mount:{gi}"),"hardware",&low["hardware_shape"],g.foot.xyz,root,[1.;3])?);
            parts.push(head(c,&variant(&turns[lane],mm),format!("low_head:{gi}"),lane,true,
                            at(g.foot.xyz,root,vec(&low["position"])?),root)?);
        }
    }
    Ok(json!({"format":1,"lens_mm":mm,"parts":parts,"lane_count":r.lanes,
        "complete_lane_coverage":true,"native_mounted_assembly":true}))
}

#[cfg(test)]
mod tests{
    use super::*;
    use crate::nlassembly_core::{Group,Site};
    fn catalog()->Value{serde_json::from_str(include_str!("../../../docs/assets/groenpol/components.json")).unwrap()}
    fn request(n:usize)->Request{Request{stop:[0.,0.,0.],travel:[0.,1.],lanes:n,opposite:0,
        radius:n as f64*1.65,overhead:true,before:20.,after:3.}}
    fn plan(n:usize,portal:bool,mirrored:bool)->Plan{
        let foot=Site{xyz:[if mirrored{-1.}else{1.}*(n as f64*1.65+0.6),0.,0.],
                      lateral:if mirrored{-1.}else{1.}*(n as f64*1.65+0.6),back:0.};
        let mut groups=vec![Group{foot,mirrored,standing:false,lanes:(0..n).collect()}];
        if portal{groups.push(Group{foot:Site{xyz:[-foot.xyz[0],0.,0.4],lateral:-foot.lateral,back:0.},
                                    mirrored:true,standing:false,lanes:vec![]});}
        Plan{kind:if portal{Kind::Portal}else{Kind::Cantilever},groups,lane_width:3.3}
    }
    #[test] fn original_sockets_make_one_primary_head_per_lane(){
        let out=expand(&request(3),&plan(3,false,false),&catalog(),&vec!["through".into();3],&[1],false).unwrap();
        let parts=out["parts"].as_array().unwrap();
        assert_eq!(parts.iter().filter(|p|p["role"]=="head"&&p["repeat"]==false).count(),3);
        assert_eq!(parts.iter().filter(|p|p["role"]=="hardware").count(),4);
        assert_eq!(out["lens_mm"],200);
        let mut keys=std::collections::HashSet::new();
        for p in parts{assert!(keys.insert(p["key"].as_str().unwrap()));
            assert_eq!(p["pose"].as_array().unwrap().len(),12);}
    }
    #[test] fn portal_has_eight_primary_heads_and_height_adjusted_left_foot(){
        let c=catalog();let out=expand(&request(8),&plan(8,true,false),&c,&vec!["through".into();8],&[0],false).unwrap();
        let rows=out["parts"].as_array().unwrap();
        assert_eq!(rows.iter().filter(|p|p["role"]=="head"&&p["repeat"]==false).count(),8);
        assert_eq!(rows.iter().filter(|p|p["role"]=="beam").count(),1);
        assert_eq!(rows.iter().filter(|p|p["role"]=="support").count(),2);
        let left=rows.iter().find(|p|p["key"]=="support:1").unwrap();
        let h=c["supports"]["portal_5"]["construction"]["leg_bounds_m"][1][2].as_f64().unwrap();
        assert!((left["pose"][2].as_f64().unwrap()+left["pose"][11].as_f64().unwrap()*h-h).abs()<1e-10);
    }
    #[test] fn mirrored_steel_never_reverses_driver_facing_heads(){
        let out=expand(&request(2),&plan(2,false,true),&catalog(),&vec!["left".into(),"through".into()],&[1],false).unwrap();
        let rows=out["parts"].as_array().unwrap();
        let steel=rows.iter().find(|p|p["role"]=="support").unwrap();
        assert!(steel["pose"][4].as_f64().unwrap()>0.99);
        for h in rows.iter().filter(|p|p["role"]=="head"){
            assert!(h["pose"][4].as_f64().unwrap() < -0.99);
        }
    }
    #[test] fn mixed_junction_and_exit_choose_larger_supplied_models(){
        for (zones,exit) in [(vec![1,0],false),(vec![1],true)]{
            let out=expand(&request(1),&plan(1,false,false),&catalog(),&vec!["right".into()],&zones,exit).unwrap();
            assert_eq!(out["lens_mm"],300);
            for h in out["parts"].as_array().unwrap().iter().filter(|p|p["role"]=="head"){
                assert_eq!(h["variant"],"vehicle_300_arrow_right");
            }
        }
    }
    #[test] fn straight_or_shared_lane_keeps_full_lens(){
        assert_eq!(variant("through",200),"vehicle");assert_eq!(variant("left;through",300),"vehicle_300");
        assert_eq!(variant("",200),"vehicle");
    }
    #[test] fn invalid_catalog_and_nonvehicle_head_fail_closed(){
        let mut c=catalog();c["heads"]["vehicle"]["participant"]=json!("pedestrian");
        assert_eq!(expand(&request(1),&plan(1,false,false),&c,&vec!["through".into()],&[1],false).unwrap_err(),"vehicle_head_unavailable");
        assert!(expand(&request(1),&plan(1,false,false),&catalog(),&[],&[1],false).is_err());
    }
}
