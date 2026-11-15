//! Reconcile complete junction snapshots, never mutate an already displayed neighbour.
//! Only verified coincident feet are shared here. Relocating nearby feet remains
//! a physical-planner operation; this pass does not guess a new road-safe position.
use serde_json::{json,Value};
use crate::dm::Dm;
use crate::nlparts::part;
type V=[f64;3];
type B=[f64;9];
const GAP:f64=0.002;
fn vector(v:&Value)->Result<V,&'static str>{
    let a=v.as_array().ok_or("bundle_vector")?;if a.len()!=3{return Err("bundle_vector");}
    let mut p=[0.;3];for i in 0..3{p[i]=a[i].as_f64().filter(|n|n.is_finite()&&n.abs()<1e8).ok_or("bundle_vector")?;}Ok(p)
}
fn pose(p:&Value)->Result<(V,B),&'static str>{
    let a=p["pose"].as_array().ok_or("bundle_pose")?;if a.len()!=12{return Err("bundle_pose");}
    let mut n=[0.;12];for i in 0..12{n[i]=a[i].as_f64().filter(|n|n.is_finite()&&n.abs()<1e8).ok_or("bundle_pose")?;}
    let b=[n[3],n[6],n[9],n[4],n[7],n[10],n[5],n[8],n[11]];
    for col in 0..3{let s=(0..3).map(|r|b[r*3+col].powi(2)).sum::<f64>();if !(0.0001..=1600.).contains(&s){return Err("bundle_pose");}}
    Ok(([n[0],n[1],n[2]],b))
}
fn rot(b:B,v:V)->V{[b[0]*v[0]+b[1]*v[1]+b[2]*v[2],b[3]*v[0]+b[4]*v[1]+b[5]*v[2],b[6]*v[0]+b[7]*v[1]+b[8]*v[2]]}
fn at(p:V,b:B,v:V)->V{let q=rot(b,v);[p[0]+q[0],p[1]+q[1],p[2]+q[2]]}
fn dot(a:V,b:V)->f64{a[0]*b[0]+a[1]*b[1]+a[2]*b[2]}
fn sub(a:V,b:V)->V{[a[0]-b[0],a[1]-b[1],a[2]-b[2]]}
fn cross(a:V,b:V)->V{[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]}
fn moved(p:&Value,xyz:V)->Value{let mut q=p.clone();for i in 0..3{q["pose"][i]=json!(xyz[i]);}q}
fn support_bounds(c:&Value,shape:&Value)->Result<[V;2],&'static str>{
    for s in c["supports"].as_object().ok_or("bundle_catalog")?.values(){
        let b=if &s["shape"]==shape{&s["bounds_m"]}
              else if &s["construction"]["leg_shape"]==shape{&s["construction"]["leg_bounds_m"]}else{continue};
        return Ok([vector(&b[0])?,vector(&b[1])?]);
    }Err("bundle_support_shape")
}
fn head_bounds(c:&Value,p:&Value,bare:bool)->Result<[V;2],&'static str>{
    let h=&c["heads"][p["variant"].as_str().ok_or("bundle_head_variant")?];
    let b=&h[if bare{"bare_bounds_m"}else{"bounds_m"}];Ok([vector(&b[0])?,vector(&b[1])?])
}
struct Box3{centre:V,axes:[V;3],half:V}
fn box3(p:&Value,bounds:[V;2])->Result<Box3,&'static str>{
    let (origin,b)=pose(p)?;let mut centre=[0.;3];let mut half=[0.;3];let mut axes=[[0.;3];3];
    for i in 0..3{
        if bounds[1][i]<bounds[0][i]{return Err("bundle_bounds");}
        centre[i]=(bounds[0][i]+bounds[1][i])/2.;
        let col=[b[i],b[i+3],b[i+6]];let length=dot(col,col).sqrt();
        axes[i]=[col[0]/length,col[1]/length,col[2]/length];half[i]=(bounds[1][i]-bounds[0][i])*length/2.;
    }
    Ok(Box3{centre:at(origin,b,centre),axes,half})
}
fn boxes_clear(a:&Box3,b:&Box3)->bool{
    // Most neighbouring assemblies are many metres apart. Reject those in
    // world axes before allocating the SAT cross-axis list.
    for k in 0..3{
        let ra=(0..3).map(|i|a.axes[i][k].abs()*a.half[i]).sum::<f64>();
        let rb=(0..3).map(|i|b.axes[i][k].abs()*b.half[i]).sum::<f64>();
        if (a.centre[k]-b.centre[k]).abs()>=ra+rb+GAP{return true;}
    }
    let mut axes=Vec::from(a.axes);axes.extend(b.axes);
    for x in &a.axes{for y in &b.axes{axes.push(cross(*x,*y));}}
    for axis in axes{
        let len=dot(axis,axis).sqrt();if len<1e-9{continue;}
        let axis=[axis[0]/len,axis[1]/len,axis[2]/len];
        let ra=(0..3).map(|i|dot(a.axes[i],axis).abs()*a.half[i]).sum::<f64>();
        let rb=(0..3).map(|i|dot(b.axes[i],axis).abs()*b.half[i]).sum::<f64>();
        if dot(sub(a.centre,b.centre),axis).abs()>=ra+rb+GAP{return true;}
    }false
}

fn physical_bounds(c:&Value,p:&Value)->Result<Option<[V;2]>,&'static str>{
    match p["role"].as_str(){
        Some("head")=>Ok(Some(head_bounds(c,p,p["bare"]==true)?)),
        Some("support")=>Ok(Some(support_bounds(c,&p["shape"])?)),
        Some("beam")=>{
            for s in c["supports"].as_object().ok_or("bundle_catalog")?.values(){
                if s["construction"]["beam_shape"]==p["shape"]{
                    let b=&s["construction"]["beam_bounds_m"];return Ok(Some([vector(&b[0])?,vector(&b[1])?]));
                }
            }Err("bundle_beam_shape")
        },_=>Ok(None)
    }
}
fn shared_joint(a:&Value,b:&Value,parts:&[Value],c:&Value)->bool{
    if a["role"]=="head"||b["role"]=="head"{return false;}
    if a["role"]=="beam"&&b["role"]=="beam"{
        let (Ok((_,aa)),Ok((_,bb)))=(pose(a),pose(b))else{return false};
        let x=[aa[1],aa[4],aa[7]];let y=[bb[1],bb[4],bb[7]];
        if dot(x,y)>0.5*(dot(x,x)*dot(y,y)).sqrt(){return false;}
    }
    for post in parts.iter().filter(|p|p["role"]=="support"){
        let Some(owners)=post["shared_owners"].as_array()else{continue};
        if !owners.contains(&a["approach"])||!owners.contains(&b["approach"]){continue;}
        let Ok((foot,_))=pose(post)else{continue};
        let near=|p:&Value|->bool{
            let Ok((origin,basis))=pose(p)else{return false};
            if p["role"]=="support"{return (origin[0]-foot[0]).dhypot(origin[1]-foot[1])<0.01;}
            let Ok(Some(bounds))=physical_bounds(c,p)else{return false};
            // The beam's original mast-side end is y=0, not its distant tip.
            let start=at(origin,basis,[(bounds[0][0]+bounds[1][0])/2.,bounds[1][1],0.]);
            (start[0]-foot[0]).dhypot(start[1]-foot[1])<0.35
        };
        if near(a)&&near(b){return true;}
    }false
}
pub fn clearance(parts:&[Value],c:&Value)->Result<(),&'static str>{
    if parts.len()>4096{return Err("bundle_parts");}
    let mut boxes=Vec::with_capacity(parts.len());
    for p in parts{boxes.push(physical_bounds(c,p)?.map(|b|box3(p,b)).transpose()?);}
    for i in 0..parts.len(){let Some(a)=&boxes[i]else{continue};
        for j in i+1..parts.len(){let Some(b)=&boxes[j]else{continue};
            if parts[i]["approach"]==parts[j]["approach"]{continue;}
            if !boxes_clear(a,b)&&!shared_joint(&parts[i],&parts[j],parts,c){return Err("assembly_clearance_conflict");}
        }
    }Ok(())
}
fn clears_post(p:&Value,bounds:[V;2],foot:V,radius:f64,top:f64)->Result<bool,&'static str>{
    let (pos,b)=pose(p)?;
    // Shared low mounts remain upright; do not silently flatten another tilt.
    if b[2].abs()>1e-5||b[5].abs()>1e-5||(b[8]-1.).abs()>1e-5{return Ok(false);}
    if pos[2]+bounds[0][2]>=top-GAP{return Ok(true);}
    let d=sub(foot,pos);let x=d[0]*b[0]+d[1]*b[3];let y=d[0]*b[1]+d[1]*b[4];
    let dx=if x<bounds[0][0]{bounds[0][0]-x}else{(x-bounds[1][0]).max(0.)};
    let dy=if y<bounds[0][1]{bounds[0][1]-y}else{(y-bounds[1][1]).max(0.)};
    Ok(dx*dx+dy*dy>=(radius+GAP).powi(2))
}
fn choice(c:&Value,a:&Value,b:&Value,pa:V,pb:V,foot:V,radius:f64,top:f64,bare_a:bool,bare_b:bool)->Result<Option<(Value,Value)>,&'static str>{
    let mut a=moved(a,pa);let mut b=moved(b,pb);
    let ba=head_bounds(c,&a,bare_a)?;let bb=head_bounds(c,&b,bare_b)?;
    if !boxes_clear(&box3(&a,ba)?,&box3(&b,bb)?)||!clears_post(&a,ba,foot,radius,top)?||!clears_post(&b,bb,foot,radius,top)?{return Ok(None);}
    for (p,bare) in [(&mut a,bare_a),(&mut b,bare_b)]{
        if bare{
            if !p["bare_shape"].is_string(){return Err("bundle_bare_head_unavailable");}
            p["shape"]=p["bare_shape"].clone();
            if !p["forest_bare"].is_null(){p["forest"]=p["forest_bare"].clone();}
        }
        p["bare"]=json!(bare);p["shared_mount"]=json!(true);
    }
    Ok(Some((a,b)))
}
fn pair(c:&Value,a:&Value,b:&Value,foot:V,radius:f64,top:f64,key:&str,mm:u64)->Result<Option<(Value,Value,Vec<Value>,&'static str,u16)>,&'static str>{
    let (_,basis)=pose(a)?;let (_,other)=pose(b)?;
    let low=&c["low_repeat"];let socket=vector(&low["position"])?;
    if dot([basis[0],basis[3],0.],[other[0],other[3],0.])>0.99999{
        let dual=&c["double_mount"];let bar=vector(&dual["bar_centre"])?;
        let origin=at(foot,basis,[socket[0]-bar[0],socket[1]-bar[1],0.]);
        let pa=at(origin,basis,vector(&dual["sockets"][0])?);let pb=at(origin,basis,vector(&dual["sockets"][1])?);
        if let Some((a,b))=choice(c,a,b,pa,pb,foot,radius,top,true,true)?{
            let drop=dual["stand_off_drop_m"].as_f64().ok_or("bundle_dual_mount")?;
            let mut hardware=vec![part(format!("{key}:stem"),"hardware",&low["hardware_shape"],
                [foot[0],foot[1],foot[2]-drop],basis,[1.;3])?,
                part(format!("{key}:bar"),"hardware",&dual["hardware_shape"],origin,basis,[1.;3])?];
            let shield=&c["double_shields"][mm.to_string()];
            let centre=[(pa[0]+pb[0])/2.,(pa[1]+pb[1])/2.,(pa[2]+pb[2])/2.];
            let mut style="bare";
            if shield["shape"].is_string(){
                let row=part(format!("{key}:shield"),"hardware",&shield["shape"],centre,basis,[1.;3])?;
                if clears_post(&row,[vector(&shield["bounds_m"][0])?,vector(&shield["bounds_m"][1])?],foot,radius,top)?{
                    hardware.push(row);style="double_shield";
                }
            }
            return Ok(Some((a,b,hardware,style,180)));
        }
    }
    // Two original loose feet at an exact quarter turn. Head bearings do not
    // rotate with the mechanical feet, and shields are preferred whenever clear.
    let pa=at(foot,basis,socket);
    for (bare_a,bare_b) in [(false,false),(false,true),(true,false),(true,true)]{
        for (degrees,quarter) in [(90,[0.,-1.,0.,1.,0.,0.,0.,0.,1.]),(180,[-1.,0.,0.,0.,-1.,0.,0.,0.,1.]),(270,[0.,1.,0.,-1.,0.,0.,0.,0.,1.])]{
            let mut rb=[0.;9];for i in 0..3{for j in 0..3{rb[i*3+j]=(0..3).map(|k|basis[i*3+k]*quarter[k*3+j]).sum();}}
            let pb=at(foot,rb,socket);
            if let Some((a,b))=choice(c,a,b,pa,pb,foot,radius,top,bare_a,bare_b)?{
                return Ok(Some((a,b,vec![part(format!("{key}:foot_a"),"hardware",&low["hardware_shape"],foot,basis,[1.;3])?,
                    part(format!("{key}:foot_b"),"hardware",&low["hardware_shape"],foot,rb,[1.;3])?],
                    if bare_a||bare_b{"partial_shields"}else{"individual_shields"},degrees)));
            }
        }
    }Ok(None)
}

pub fn bundle(assemblies:&[Value],owners:&[String],c:&Value)->Result<Value,&'static str>{
    if assemblies.is_empty()||assemblies.len()!=owners.len()||assemblies.len()>128{return Err("bundle_approaches");}
    let mut ordered:Vec<_>=(0..owners.len()).collect();ordered.sort_by_key(|i|&owners[*i]);
    let mut ids=std::collections::HashSet::new();let mut parts=Vec::new();let mut mm=0;
    for i in ordered{
        let owner=&owners[i];let a=&assemblies[i];
        if owner.is_empty()||owner.len()>48||!owner.bytes().all(|b|b.is_ascii_alphanumeric()||b"_-:".contains(&b))||!ids.insert(owner){return Err("bundle_owner");}
        let lens=a["lens_mm"].as_u64().ok_or("bundle_lens_size")?;
        if ![200,300].contains(&lens)||mm!=0&&mm!=lens{return Err("bundle_junction_lens_mismatch");}mm=lens;
        if a["format"]!=1||a["bundle"]==true||a["complete_lane_coverage"]!=true||a["native_mounted_assembly"]!=true{return Err("bundle_incomplete_approach");}
        let rows=a["parts"].as_array().ok_or("bundle_parts")?;
        if rows.len()>128{return Err("bundle_parts");}
        let mut keys=std::collections::HashSet::new();
        for p in rows{
            pose(p)?;
            let key=p["key"].as_str().filter(|s|!s.is_empty()&&s.len()<=64).ok_or("bundle_part_key")?;
            if !keys.insert(key){return Err("bundle_part_key");}
            let mut p=p.clone();p["key"]=json!(format!("{owner}:{key}"));p["approach"]=json!(owner);parts.push(p);
        }
    }
    if parts.len()>4096{return Err("bundle_parts");}
    let mut removed=vec![false;parts.len()];let mut additions=Vec::new();let mut shared=Vec::new();
    for i in 0..parts.len(){
        if removed[i]||parts[i]["role"]!="support"{continue;}
        for j in i+1..parts.len(){
            if removed[j]||parts[j]["role"]!="support"||parts[i]["approach"]==parts[j]["approach"]{continue;}
            let (foot,ai)=pose(&parts[i])?;let (other,bj)=pose(&parts[j])?;
            if dot(sub(foot,other),sub(foot,other))>1e-10{continue;}
            if parts[i]["shared_owners"].is_array(){return Err("bundle_shared_mount_capacity");}
            let ba=support_bounds(c,&parts[i]["shape"])?;let bb=support_bounds(c,&parts[j]["shape"])?;
            for axis in [ai,bj]{if axis[2].abs()>1e-8||axis[5].abs()>1e-8||axis[6].abs()>1e-8||axis[7].abs()>1e-8||axis[8]<=0.{return Err("bundle_support_tilt");}}
            let ra=ba[0][0].abs().max(ba[1][0].abs())*(ai[0]*ai[0]+ai[3]*ai[3]).sqrt();
            let rb=bb[0][0].abs().max(bb[1][0].abs())*(bj[0]*bj[0]+bj[3]*bj[3]).sqrt();
            let ta=foot[2]+ba[1][2]*ai[8];let tb=foot[2]+bb[1][2]*bj[8];
            let radius=ra.max(rb);let top=ta.max(tb);
            if !((ra+1e-8>=rb&&ta+1e-8>=tb)||(rb+1e-8>=ra&&tb+1e-8>=ta)){return Err("bundle_incompatible_posts");}
            let nearest=|owner:&Value|->Result<usize,&'static str>{
                let mut found=Vec::new();
                for (k,p) in parts.iter().enumerate(){if !removed[k]&&p["role"]=="head"&&&p["approach"]==owner{
                    let (pos,_)=pose(p)?;let d=dot(sub(pos,foot),sub(pos,foot));
                    if pos[2]-foot[2]<4.1&&d<20.{found.push(k);}
                }}
                if found.len()!=1{return Err("bundle_shared_mount_capacity");}Ok(found[0])
            };
            let ha=nearest(&parts[i]["approach"])?;let hb=nearest(&parts[j]["approach"])?;
            let key=format!("shared:{}:{}",parts[i]["approach"].as_str().unwrap(),parts[j]["approach"].as_str().unwrap());
            let (a,b,hardware,style,quarter)=pair(c,&parts[ha],&parts[hb],foot,radius,top,&key,mm)?.ok_or("bundle_shared_heads_do_not_fit")?;
            parts[ha]=a;parts[hb]=b;
            for k in 0..parts.len(){if parts[k]["role"]=="hardware"&&parts[k]["key"].as_str().unwrap_or("").contains(":low_mount:"){
                let (pos,_)=pose(&parts[k])?;if dot(sub(pos,foot),sub(pos,foot))<1e-10{removed[k]=true;}
            }}
            if tb>ta+1e-8||rb>ra+1e-8{parts[i]["shape"]=parts[j]["shape"].clone();parts[i]["pose"]=parts[j]["pose"].clone();}
            parts[i]["shared_owners"]=json!([parts[i]["approach"],parts[j]["approach"]]);removed[j]=true;
            additions.extend(hardware);shared.push(json!({"owners":parts[i]["shared_owners"],"style":style,"quarter_degrees":quarter}));
        }
    }
    let mut out:Vec<_>=parts.into_iter().enumerate().filter_map(|(i,p)|if removed[i]{None}else{Some(p)}).collect();out.extend(additions);
    out.sort_by(|a,b|a["key"].as_str().cmp(&b["key"].as_str()));
    let heads:Vec<_>=out.iter().filter(|p|p["role"]=="head").collect();
    for i in 0..heads.len(){for j in i+1..heads.len(){
        if heads[i]["approach"]!=heads[j]["approach"]&&!boxes_clear(
            &box3(heads[i],head_bounds(c,heads[i],heads[i]["bare"]==true)?)?,
            &box3(heads[j],head_bounds(c,heads[j],heads[j]["bare"]==true)?)?){return Err("bundle_head_collision");}
    }}
    Ok(json!({"format":1,"bundle":true,"parts":out,"lens_mm":mm,"shared_posts":shared,
              "complete_lane_coverage":true,"native_mounted_assembly":true}))
}

#[cfg(test)]
mod tests{
    use super::*;
    const I:B=[1.,0.,0.,0.,1.,0.,0.,0.,1.];
    fn catalog()->Value{
        let mut c:Value=serde_json::from_str(include_str!("../../../docs/assets/groenpol/components.json")).unwrap();
        c["low_repeat"]["position"][0]=json!(c["low_repeat"]["position"][0].as_f64().unwrap()+0.05);c
    }
    fn assembly(c:&Value,front:B,x:f64)->Value{
        let h=&c["heads"]["vehicle"];let socket=vector(&c["low_repeat"]["position"]).unwrap();
        let support=part("support:0".into(),"support",&c["supports"]["cantilever_3"]["construction"]["leg_shape"],[x,0.,0.],I,[1.;3]).unwrap();
        let mut head=part("low_head:0".into(),"head",&h["shape"],at([x,0.,0.],front,socket),front,[1.;3]).unwrap();
        head["variant"]=json!("vehicle");head["bare_shape"]=h["bare_shape"].clone();head["lane"]=json!(0);head["repeat"]=json!(true);
        json!({"format":1,"lens_mm":200,"complete_lane_coverage":true,"native_mounted_assembly":true,"parts":[support,head]})
    }
    #[test] fn co_facing_pair_uses_one_post_original_double_mount_and_double_shield(){
        let c=catalog();let a=assembly(&c,I,0.);let out=bundle(&[a.clone(),a],&["a".into(),"b".into()],&c).unwrap();
        let p=out["parts"].as_array().unwrap();
        assert_eq!(p.iter().filter(|p|p["role"]=="support").count(),1);
        assert_eq!(p.iter().filter(|p|p["role"]=="head"&&p["bare"]==true).count(),2);
        assert_eq!(out["shared_posts"][0]["style"],"double_shield");
        let owners:std::collections::HashSet<_>=p.iter().filter(|p|p["role"]=="head").map(|p|p["approach"].as_str().unwrap()).collect();
        assert_eq!(owners.len(),2);
    }
    #[test] fn opposite_heads_keep_individual_shields_and_road_bearings(){
        let c=catalog();let reverse=[-1.,0.,0.,0.,-1.,0.,0.,0.,1.];
        let out=bundle(&[assembly(&c,I,0.),assembly(&c,reverse,0.)],&["a".into(),"b".into()],&c).unwrap();
        assert_eq!(out["shared_posts"][0]["style"],"individual_shields");
        assert_eq!(out["shared_posts"][0]["quarter_degrees"],180);
        let heads:Vec<_>=out["parts"].as_array().unwrap().iter().filter(|p|p["role"]=="head").collect();
        assert_eq!(heads[0]["pose"][3],1.);assert_eq!(heads[1]["pose"][3],-1.);
    }
    #[test] fn owner_order_does_not_change_result(){
        let c=catalog();let a=assembly(&c,I,0.);
        let first=bundle(&[a.clone(),a.clone()],&["a".into(),"b".into()],&c).unwrap();
        assert_eq!(first,bundle(&[a.clone(),a],&["b".into(),"a".into()],&c).unwrap());
    }
    #[test] fn nearby_feet_are_not_silently_moved_or_shared(){
        let c=catalog();let out=bundle(&[assembly(&c,I,0.),assembly(&c,I,0.6)],&["a".into(),"b".into()],&c).unwrap();
        assert_eq!(out["shared_posts"].as_array().unwrap().len(),0);
        assert_eq!(out["parts"].as_array().unwrap().iter().filter(|p|p["role"]=="support").count(),2);
    }
    #[test] fn incomplete_context_and_duplicate_owners_fail_closed(){
        let c=catalog();let a=assembly(&c,I,0.);let mut b=a.clone();b["lens_mm"]=json!(300);
        assert_eq!(bundle(&[a.clone(),b],&["a".into(),"b".into()],&c).unwrap_err(),"bundle_junction_lens_mismatch");
        assert_eq!(bundle(&[a.clone(),a],&["a".into(),"a".into()],&c).unwrap_err(),"bundle_owner");
    }
    #[test] fn a_third_low_head_cannot_overload_the_original_double_mount(){
        let c=catalog();let a=assembly(&c,I,0.);
        assert_eq!(bundle(&[a.clone(),a.clone(),a],&["a".into(),"b".into(),"c".into()],&c).unwrap_err(),"bundle_shared_mount_capacity");
    }
    #[test] fn unmerged_head_collisions_and_nested_controller_bundles_are_refused(){
        let c=catalog();let a=assembly(&c,I,0.);
        assert_eq!(bundle(&[a.clone(),assembly(&c,I,0.35)],&["a".into(),"b".into()],&c).unwrap_err(),"bundle_head_collision");
        let combined=bundle(&[a.clone(),a],&["a".into(),"b".into()],&c).unwrap();
        assert_eq!(bundle(&[combined],&["new_owner".into()],&c).unwrap_err(),"bundle_incomplete_approach");
    }
}
