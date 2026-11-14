//! Numeric support selection for the server migration, before asset/socket expansion.
//! Inputs are source road geometry, not angles guessed from visible arrows.
use crate::nlsurface_core::Scene;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Kind { Standing, Cantilever, Portal, Split }

#[derive(Clone, Debug)]
pub struct Request {
    pub stop: [f64;3], // carriageway centre at the inferred/mapped stop station
    pub travel: [f64;2],
    pub lanes: usize,
    pub opposite: usize,
    pub radius: f64,
    pub overhead: bool,
    pub before: f64,
    pub after: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Site { pub xyz: [f64;3], pub lateral: f64, pub back: f64 }

#[derive(Debug)]
pub struct Group {
    pub foot: Site,
    pub mirrored: bool,
    pub standing: bool,
    pub lanes: Vec<usize>,
}

#[derive(Debug)]
pub struct Plan {
    pub kind: Kind,
    pub groups: Vec<Group>,
    pub lane_width: f64,
}

pub fn lens_mm(zones: &[i8], motorway_exit: bool) -> u16 {
    if motorway_exit || zones.is_empty() || zones.iter().any(|z| *z != 1) { 300 } else { 200 }
}

fn lane_offset(r: &Request, width: f64, i: usize) -> f64 {
    -r.radius + (r.opposite as f64+i as f64+0.5)*width
}

fn point(r: &Request, right: [f64;2], lateral: f64, back: f64) -> [f64;2] {
    [r.stop[0]+right[0]*lateral-r.travel[0]*back,
     r.stop[1]+right[1]*lateral-r.travel[1]*back]
}

fn sites(scene: &Scene, r: &Request, right: [f64;2], left: bool, width: f64) -> Vec<Site> {
    let edge=if left { -r.radius+r.opposite as f64*width-0.6 } else { r.radius+0.6 };
    let mut out=Vec::new();
    for back in [0.,1.,-1.,2.,-2.,3.,-3.,4.,-4.,6.,-6.,8.,10.,12.,16.,20.] {
        if back < -r.after || back > r.before { continue; }
        for step in 0..=16 {
            let lateral=edge+step as f64*0.5*if left { -1. } else { 1. };
            let xy=point(r,right,lateral,back);
            if let Ok(floor)=scene.foot(xy,r.stop[2]) {
                out.push(Site{xyz:[xy[0],xy[1],floor.z],lateral,back});
            }
        }
    }
    out
}

fn group(scene: &Scene, r: &Request, right: [f64;2], width: f64, site: Site,
         first: usize, last: usize, mirrored: bool, standing: bool) -> Option<Group> {
    if last <= first || !standing && last-first > 3 || standing && last-first != 1 { return None; }
    let reach=(lane_offset(r,width,if mirrored { last-1 } else { first })-site.lateral).abs();
    if standing {
        if reach > width+0.6 || !scene.column_clear([site.xyz[0],site.xyz[1]],site.xyz[2],site.xyz[2]+4.1,0.16) { return None; }
    } else {
        if reach >= 12. || !scene.column_clear([site.xyz[0],site.xyz[1]],site.xyz[2],site.xyz[2]+6.,0.16) { return None; }
        let end=point(r,right,lane_offset(r,width,if mirrored { last-1 } else { first }),site.back);
        if !scene.overhead_clear([site.xyz[0],site.xyz[1]],end,site.xyz[2]+5.1,site.xyz[2]+6.9) { return None; }
    }
    Some(Group{foot:site,mirrored,standing,lanes:(first..last).collect()})
}

pub fn plan(scene: &Scene, request: &Request) -> Result<Plan,&'static str> {
    let mut r=request.clone();
    let norm=(r.travel[0]*r.travel[0]+r.travel[1]*r.travel[1]).sqrt();
    if !r.stop.iter().chain(r.travel.iter()).all(|v|v.is_finite() && v.abs()<1e9)
        || !norm.is_finite() || norm<0.5 || r.lanes==0 || r.lanes>16 || r.opposite>16
        || !r.radius.is_finite() || r.radius<=0. || r.radius>40.
        || !r.before.is_finite() || !r.after.is_finite() || !(0. ..=20.).contains(&r.before)
        || !(0. ..=3.).contains(&r.after) { return Err("invalid_approach_geometry"); }
    r.travel=[r.travel[0]/norm,r.travel[1]/norm];let right=[r.travel[1],-r.travel[0]];
    let width=2.*r.radius/(r.lanes+r.opposite) as f64;
    if !(1.5..=6.).contains(&width) { return Err("unverified_lane_width"); }
    // Plan once against a complete local snapshot. Choosing a later candidate
    // while an earlier one is still unknown would move the mast when its tile arrives.
    let mut lo=[f64::INFINITY;2];let mut hi=[f64::NEG_INFINITY;2];
    for lateral in [-r.radius+r.opposite as f64*width-8.6,r.radius+8.6] {
        for back in [-r.after,r.before] {
            let xy=point(&r,right,lateral,back);
            for k in 0..2 {lo[k]=lo[k].min(xy[k]);hi[k]=hi[k].max(xy[k]);}
        }
    }
    let centre=[(lo[0]+hi[0])/2.,(lo[1]+hi[1])/2.];
    let reach=((hi[0]-lo[0]).max(hi[1]-lo[1]))/2.+0.6;
    if !scene.covered(centre,reach) { return Err("surface_tiles_pending"); }
    if !scene.ground_covered(centre,reach) { return Err("ground_tiles_pending"); }
    for i in 0..r.lanes {
        let xy=point(&r,right,lane_offset(&r,width,i),0.);
        if !scene.covered(xy,0.6) { return Err("surface_tiles_pending"); }
        if scene.road_floor(xy,r.stop[2]).is_none() {
            return Err("lane_surface_unavailable");
        }
    }
    let rs=sites(scene,&r,right,false,width);let ls=sites(scene,&r,right,true,width);
    for &site in &rs {
        let standing=r.lanes==1 && !r.overhead;
        if let Some(g)=group(scene,&r,right,width,site,0,r.lanes,false,standing) {
            return Ok(Plan{kind:if standing{Kind::Standing}else{Kind::Cantilever},groups:vec![g],lane_width:width});
        }
        // A portal needs two genuinely clear feet on one straight stop station.
        for &left in ls.iter().filter(|p|(p.back-site.back).abs()<0.05) {
            let span=site.lateral-left.lateral;
            if span<=0. || span>40. || !scene.column_clear([site.xyz[0],site.xyz[1]],site.xyz[2],site.xyz[2]+6.,0.16)
                || !scene.column_clear([left.xyz[0],left.xyz[1]],left.xyz[2],site.xyz[2]+6.,0.16)
                || !scene.overhead_clear([site.xyz[0],site.xyz[1]],[left.xyz[0],left.xyz[1]],site.xyz[2]+5.1,site.xyz[2]+6.9) { continue; }
            if lane_offset(&r,width,0)<=left.lateral+0.3 || lane_offset(&r,width,r.lanes-1)>=site.lateral-0.3 { continue; }
            return Ok(Plan{kind:Kind::Portal,groups:vec![
                Group{foot:site,mirrored:false,standing:false,lanes:(0..r.lanes).collect()},
                Group{foot:left,mirrored:true,standing:false,lanes:vec![]}],lane_width:width});
        }
        // Two independently grounded arms; four lanes may use a 3+1 arrangement.
        if r.lanes>=2 && r.lanes<=6 {
            for &left in &ls {
                for split in 1..r.lanes {
                    let standing=r.lanes==4 && split==1;
                    if let (Some(l),Some(g))=(group(scene,&r,right,width,left,0,split,true,standing),
                                               group(scene,&r,right,width,site,split,r.lanes,false,false)) {
                        return Ok(Plan{kind:Kind::Split,groups:vec![l,g],lane_width:width});
                    }
                }
            }
        }
    }
    // A left-side mirrored arm can serve an approach whose right verge has no safe foot.
    if r.lanes<=3 {
        for &left in &ls {
            let standing=r.lanes==1 && !r.overhead;
            if let Some(g)=group(scene,&r,right,width,left,0,r.lanes,true,standing) {
                return Ok(Plan{kind:if standing{Kind::Standing}else{Kind::Cantilever},groups:vec![g],lane_width:width});
            }
        }
    }
    // Hierarchy permits an overhead head; it must not prohibit a safe standing
    // alternative under a low bridge. Two outside lanes can each have a pole.
    if r.lanes==1 {
        for (&site,mirrored) in rs.iter().map(|s|(s,false)).chain(ls.iter().map(|s|(s,true))) {
            if let Some(g)=group(scene,&r,right,width,site,0,1,mirrored,true) {
                return Ok(Plan{kind:Kind::Standing,groups:vec![g],lane_width:width});
            }
        }
    } else if r.lanes==2 {
        for &site in &rs { for &left in &ls {
            if let (Some(l),Some(g))=(group(scene,&r,right,width,left,0,1,true,true),
                                      group(scene,&r,right,width,site,1,2,false,true)) {
                return Ok(Plan{kind:Kind::Split,groups:vec![l,g],lane_width:width});
            }
        } }
    }
    Err("no_safe_complete_lane_coverage")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nlsurface_core::{Surface,Terrain};
    fn scene(mut extra:Vec<Surface>, n:usize)->Scene {
        let w=n as f64*1.65;
        extra.push(Surface{kind:1,triangle:[[-w,-50.,0.],[w,-50.,0.],[w,50.,0.]]});
        extra.push(Surface{kind:1,triangle:[[-w,-50.,0.],[w,50.,0.],[-w,50.,0.]]});
        Scene::new(extra,Terrain{origin:[-100.,-100.],step:100.,width:3,height:3,z:vec![0.;9],holes:vec![]},vec![[-100.,-100.,100.,100.]]).unwrap()
    }
    fn req(n:usize)->Request { Request{stop:[0.,0.,0.],travel:[0.,1.],lanes:n,opposite:0,radius:n as f64*1.65,overhead:true,before:20.,after:3.} }
    #[test] fn three_heads_use_one_arm_and_four_or_more_use_portal() {
        let s=scene(vec![],3);
        assert_eq!(plan(&s,&req(3)).unwrap().kind,Kind::Cantilever);
        for n in [4,6,8] { let p=plan(&scene(vec![],n),&req(n)).unwrap();assert_eq!(p.kind,Kind::Portal);
            assert_eq!(p.groups.iter().map(|g|g.lanes.len()).sum::<usize>(),n); }
    }
    #[test] fn one_lane_hierarchy_can_request_overhead() {
        let s=scene(vec![],1);let mut r=req(1);r.overhead=false;
        assert_eq!(plan(&s,&r).unwrap().kind,Kind::Standing);
        r.overhead=true;assert_eq!(plan(&s,&r).unwrap().kind,Kind::Cantilever);
    }
    #[test] fn single_mast_can_be_mirrored_to_safe_left_verge() {
        let road=Surface{kind:1,triangle:[[1.,-100.,0.],[100.,-100.,0.],[1.,100.,0.]]};
        let p=plan(&scene(vec![road],2),&req(2)).unwrap();
        assert!(p.groups[0].mirrored);assert!(p.groups[0].foot.xyz[0]<0.);
    }
    #[test] fn no_ground_does_not_make_floating_supports() {
        let s=Scene::new(vec![],Terrain{origin:[-100.,-100.],step:100.,width:3,height:3,z:vec![f64::NAN;9],holes:vec![]},vec![[-100.,-100.,100.,100.]]).unwrap();
        assert!(plan(&s,&req(3)).is_err());
    }
    #[test] fn mixed_unknown_and_motorway_exit_all_select_300() {
        assert_eq!(lens_mm(&[1,1],false),200);assert_eq!(lens_mm(&[1,0],false),300);
        assert_eq!(lens_mm(&[1,-1],false),300);assert_eq!(lens_mm(&[1],true),300);
    }
    fn blocked_left_stop_station(n:usize)->Vec<Surface> {
        let w=n as f64*1.65;
        vec![Surface{kind:1,triangle:[[-40.,-0.2,0.],[-w,-0.2,0.],[-w,0.2,0.]]},
             Surface{kind:1,triangle:[[-40.,-0.2,0.],[-w,0.2,0.],[-40.,0.2,0.]]}]
    }
    #[test] fn failed_portal_can_use_three_plus_one_on_separate_grounded_feet() {
        let p=plan(&scene(blocked_left_stop_station(4),4),&req(4)).unwrap();
        assert_eq!(p.kind,Kind::Split);
        assert_eq!(p.groups.iter().map(|g|g.lanes.len()).collect::<Vec<_>>(),vec![1,3]);
        assert!(p.groups[0].standing);assert!(!p.groups[1].standing);
        assert!(p.groups[0].foot.back>0.);assert_eq!(p.groups[1].foot.back,0.);
    }
    #[test] fn six_lane_fallback_uses_two_mirrored_three_head_arms() {
        let p=plan(&scene(blocked_left_stop_station(6),6),&req(6)).unwrap();
        assert_eq!(p.kind,Kind::Split);assert!(p.groups[0].mirrored);assert!(!p.groups[1].mirrored);
        assert!(p.groups.iter().all(|g|!g.standing && g.lanes.len()==3));
        let mut lanes:Vec<_>=p.groups.iter().flat_map(|g|g.lanes.clone()).collect();lanes.sort();
        assert_eq!(lanes,(0..6).collect::<Vec<_>>());
    }
    #[test] fn low_bridge_keeps_safe_standing_heads_instead_of_omitting_them() {
        let bridge=Surface{kind:1,triangle:[[-100.,-100.,5.8],[100.,-100.,5.8],[0.,100.,5.8]]};
        let p=plan(&scene(vec![bridge.clone()],1),&req(1)).unwrap();assert_eq!(p.kind,Kind::Standing);
        let p=plan(&scene(vec![bridge],2),&req(2)).unwrap();
        assert_eq!(p.kind,Kind::Split);assert!(p.groups.iter().all(|g|g.standing));
    }
    #[test] fn incomplete_search_snapshot_waits_instead_of_moving_later() {
        let s=Scene::new(vec![],Terrain{origin:[-100.,-100.],step:100.,width:3,height:3,z:vec![0.;9],holes:vec![]},
                         vec![[-5.,-5.,5.,5.]]).unwrap();
        assert_eq!(plan(&s,&req(1)).unwrap_err(),"surface_tiles_pending");
    }
}
