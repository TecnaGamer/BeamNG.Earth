//! Physical support feet against drawn road/walk triangles, in their actual height layer.
//! Terrain is a regular rendered height grid, not an arbitrary flat fallback.
use std::collections::HashMap;

const CELL: f64 = 32.;
pub const CLEARANCE: f64 = 0.6;
pub const LEVEL_TOLERANCE: f64 = 2.5;

#[derive(Clone)]
pub struct Surface {
    pub kind: u8, // 1 drawn carriageway/deck, 2 walk/island, 3 terrain-painted carriageway
    pub triangle: [[f64; 3]; 3],
}

pub struct Terrain {
    pub origin: [f64; 2],
    pub step: f64,
    pub width: usize,
    pub height: usize,
    pub z: Vec<f64>, // decoded quantised game grid, row-major y increasing; NaN means unknown
    pub holes: Vec<u8>, // empty means no holes; otherwise one flag per terrain sample
}

impl Terrain {
    fn sample(&self, p: [f64; 2]) -> Option<f64> {
        let u = (p[0] - self.origin[0]) / self.step;
        let v = (p[1] - self.origin[1]) / self.step;
        if !u.is_finite() || !v.is_finite() || u < 0. || v < 0.
            || u > (self.width - 1) as f64 || v > (self.height - 1) as f64 { return None; }
        // Match server/heightmap.py height_at, including the last collidable sample.
        let (x, y) = ((u.floor() as usize).min(self.width-2), (v.floor() as usize).min(self.height-2));
        if !self.holes.is_empty() && self.holes[y*self.width+x]!=0 { return None; }
        let (a, b) = (u - x as f64, v - y as f64);
        let q = [self.z[y*self.width+x], self.z[y*self.width+x+1],
                 self.z[(y+1)*self.width+x], self.z[(y+1)*self.width+x+1]];
        if !q.iter().all(|v| v.is_finite()) { return None; }
        Some((q[0]*(1.-a)+q[1]*a)*(1.-b)+(q[2]*(1.-a)+q[3]*a)*b)
    }
}

#[derive(Debug, PartialEq)]
pub struct Foot {
    pub z: f64,
    pub kind: u8, // 2 walk, 3 terrain/verge
}

#[derive(Debug, PartialEq)]
pub enum Rejection { MissingGround, WrongHeightLayer, Carriageway, NearCarriageway, WalkEdge }

/// Squared XY distance to a triangle, with height at its nearest point.
fn nearest(p: [f64; 2], t: &[[f64; 3]; 3]) -> (f64, f64) {
    let (a,b,c) = (t[0],t[1],t[2]);
    let den = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1]);
    if den.abs() > 1e-12 {
        let u = ((b[1]-c[1])*(p[0]-c[0])+(c[0]-b[0])*(p[1]-c[1]))/den;
        let v = ((c[1]-a[1])*(p[0]-c[0])+(a[0]-c[0])*(p[1]-c[1]))/den;
        let w = 1.-u-v;
        if u >= -1e-10 && v >= -1e-10 && w >= -1e-10 {
            return (0.,u*a[2]+v*b[2]+w*c[2]);
        }
    }
    let mut best = (f64::INFINITY, 0.);
    for i in 0..3 {
        let (a,b) = (t[i],t[(i+1)%3]);
        let d = [b[0]-a[0], b[1]-a[1]];
        let length = d[0]*d[0]+d[1]*d[1];
        let u = if length > 1e-12 { (((p[0]-a[0])*d[0]+(p[1]-a[1])*d[1])/length).clamp(0.,1.) } else { 0. };
        let e = [p[0]-a[0]-u*d[0],p[1]-a[1]-u*d[1]];
        let d2 = e[0]*e[0]+e[1]*e[1];
        if d2 < best.0 { best = (d2,a[2]+u*(b[2]-a[2])); }
    }
    best
}

pub struct Scene {
    surfaces: Vec<Surface>,
    cells: HashMap<(i32,i32),Vec<usize>>,
    terrain: Terrain,
    terrain_known: bool,
    coverage: Vec<[f64;4]>, // fully decoded surface tiles; pending neighbours are not empty verges
    quads: Vec<[[f64;2];4]>,
}

fn clip(poly:&[[f64;2]],a:[f64;2],b:[f64;2],inside:bool)->Vec<[f64;2]>{
    let side=|p:[f64;2]|(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);
    let mut out=Vec::new();
    if poly.is_empty(){return out;}
    let mut p=*poly.last().unwrap();let mut dp=side(p);
    for &q in poly{let dq=side(q);let ip=if inside{dp>=0.}else{dp<=0.};let iq=if inside{dq>=0.}else{dq<=0.};
        if ip!=iq&&(dp-dq).abs()>1e-14{let t=dp/(dp-dq);out.push([p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])]);}
        if iq{out.push(q);}p=q;dp=dq;
    }out
}
fn area(poly:&[[f64;2]])->f64{
    if poly.len()<3{return 0.;}
    let o=poly[0];poly[1..].windows(2).map(|w|
        (w[0][0]-o[0])*(w[1][1]-o[1])-(w[0][1]-o[1])*(w[1][0]-o[0])).sum::<f64>().abs()/2.
}

impl Scene {
    pub fn covered(&self, p:[f64;2], radius:f64) -> bool {
        if !p.iter().all(|v|v.is_finite()) || !radius.is_finite() || radius<=0. { return false; }
        if !self.quads.is_empty(){
            let mut remaining=vec![vec![[p[0]-radius,p[1]-radius],[p[0]+radius,p[1]-radius],
                [p[0]+radius,p[1]+radius],[p[0]-radius,p[1]+radius]]];
            for quad in &self.quads{
                let mut next=Vec::new();
                for poly in remaining{
                    let mut within=poly;
                    for k in 0..4{
                        let outside=clip(&within,quad[k],quad[(k+1)%4],false);
                        if area(&outside)>1e-10{next.push(outside);}
                        within=clip(&within,quad[k],quad[(k+1)%4],true);
                        if area(&within)<=1e-10{break;}
                    }
                }
                if next.is_empty(){return true;}
                if next.len()>4096{return false;}remaining=next;
            }return false;
        }
        let mut remaining=vec![[p[0]-radius,p[1]-radius,p[0]+radius,p[1]+radius]];
        // Exact rectangle-union coverage: two complete adjacent tiles cover a
        // foot on their seam; a missing neighbour must not count as empty verge.
        for c in &self.coverage {
            let mut next=Vec::new();
            for r in remaining {
                let a=[r[0].max(c[0]),r[1].max(c[1]),r[2].min(c[2]),r[3].min(c[3])];
                if a[0]>=a[2] || a[1]>=a[3] { next.push(r);continue; }
                if r[0]<a[0] { next.push([r[0],r[1],a[0],r[3]]); }
                if a[2]<r[2] { next.push([a[2],r[1],r[2],r[3]]); }
                if r[1]<a[1] { next.push([a[0],r[1],a[2],a[1]]); }
                if a[3]<r[3] { next.push([a[0],a[3],a[2],r[3]]); }
            }
            if next.is_empty() { return true; }
            if next.len()>256 { return false; }
            remaining=next;
        }
        false
    }

    pub fn new(surfaces: Vec<Surface>, terrain: Terrain, coverage: Vec<[f64;4]>) -> Result<Self, &'static str> {
        if surfaces.len() > 1_000_000 || terrain.width < 2 || terrain.height < 2
            || terrain.width > 4096 || terrain.height > 4096 || terrain.z.len() != terrain.width*terrain.height
            || !terrain.holes.is_empty() && terrain.holes.len()!=terrain.z.len()
            || terrain.holes.iter().any(|v|*v>1)
            || !terrain.step.is_finite() || terrain.step <= 0. || terrain.step > 100.
            || !terrain.origin.iter().all(|v| v.is_finite() && v.abs() < 1e9) {
            return Err("invalid support terrain");
        }
        if coverage.len()>4096 || coverage.iter().any(|b| !b.iter().all(|v|v.is_finite() && v.abs()<1e9)
            || b[0]>=b[2] || b[1]>=b[3]) { return Err("invalid surface coverage"); }
        let mut cells: HashMap<(i32,i32),Vec<usize>> = HashMap::new();
        let mut references = 0i64;
        for (i, surface) in surfaces.iter().enumerate() {
            if !(1..=3).contains(&surface.kind)
                || !surface.triangle.iter().flatten().all(|v| v.is_finite() && v.abs() < 1e9) {
                return Err("invalid support surface");
            }
            let mut lo = [f64::INFINITY;2];let mut hi = [f64::NEG_INFINITY;2];
            for p in surface.triangle { for k in 0..2 { lo[k]=lo[k].min(p[k]);hi[k]=hi[k].max(p[k]); } }
            let a = [((lo[0]-CLEARANCE)/CELL).floor() as i32,((lo[1]-CLEARANCE)/CELL).floor() as i32];
            let b = [((hi[0]+CLEARANCE)/CELL).floor() as i32,((hi[1]+CLEARANCE)/CELL).floor() as i32];
            let count = (b[0] as i64-a[0] as i64+1)*(b[1] as i64-a[1] as i64+1);
            references += count;
            if count > 16384 || references > 20_000_000 {
                return Err("support triangle spans too many cells");
            }
            for x in a[0]..=b[0] { for y in a[1]..=b[1] { cells.entry((x,y)).or_default().push(i); } }
        }
        let terrain_known=terrain.z.iter().all(|v|v.is_finite());
        Ok(Self { surfaces, cells, terrain, terrain_known, coverage, quads:Vec::new() })
    }

    pub fn with_quads(mut self,mut quads:Vec<[[f64;2];4]>)->Result<Self,&'static str>{
        if quads.len()+self.coverage.len()>4096{return Err("invalid surface quads");}
        for q in &quads{
            if !q.iter().flatten().all(|v|v.is_finite()&&v.abs()<1e9){return Err("invalid surface quads");}
            for k in 0..4{let a=q[k];let b=q[(k+1)%4];let c=q[(k+2)%4];
                if (b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])<=1e-10{return Err("nonconvex surface quad");}
            }
        }
        for r in &self.coverage{quads.push([[r[0],r[1]],[r[2],r[1]],[r[2],r[3]],[r[0],r[3]]]);}
        self.quads=quads;Ok(self)
    }

    pub fn ground_covered(&self,p:[f64;2],radius:f64)->bool {
        self.terrain_known && p[0]-radius>=self.terrain.origin[0] && p[1]-radius>=self.terrain.origin[1]
            && p[0]+radius<=self.terrain.origin[0]+(self.terrain.width-1) as f64*self.terrain.step
            && p[1]+radius<=self.terrain.origin[1]+(self.terrain.height-1) as f64*self.terrain.step
    }

    pub fn foot(&self, p: [f64; 2], reference_z: f64) -> Result<Foot, Rejection> {
        if !p.iter().all(|v| v.is_finite() && v.abs() < 1e9) || !reference_z.is_finite() {
            return Err(Rejection::MissingGround);
        }
        if !self.covered(p,CLEARANCE) { return Err(Rejection::MissingGround); }
        let cell = ((p[0]/CELL).floor() as i32,(p[1]/CELL).floor() as i32);
        let near = self.cells.get(&cell).map(Vec::as_slice).unwrap_or(&[]);
        let walk=self.walk_height(p,reference_z);
        let z = walk.or_else(|| self.terrain.sample(p)).ok_or(Rejection::MissingGround)?;
        if (z-reference_z).abs() > LEVEL_TOLERANCE { return Err(Rejection::WrongHeightLayer); }
        if walk.is_some() {
            // A known raised island overrides the road skin underneath it, but
            // only when the full physical foot fits on that walk surface.
            for d in [[0.16,0.],[-0.16,0.],[0.,0.16],[0.,-0.16],
                      [0.11313708498984761,0.11313708498984761],[-0.11313708498984761,0.11313708498984761],
                      [0.11313708498984761,-0.11313708498984761],[-0.11313708498984761,-0.11313708498984761]] {
                if self.walk_height([p[0]+d[0],p[1]+d[1]],z).map_or(true,|h|(h-z).abs()>0.12) {
                    return Err(Rejection::WalkEdge);
                }
            }
        } else {
            for d in [[0.16,0.],[-0.16,0.],[0.,0.16],[0.,-0.16]] {
                if self.terrain.sample([p[0]+d[0],p[1]+d[1]]).map_or(true,|h|(h-z).abs()>0.12) {
                    return Err(Rejection::MissingGround);
                }
            }
        }
        for &i in near {
            let s = &self.surfaces[i];
            if s.kind != 1 && s.kind != 3 { continue; }
            let (d,mesh_z) = nearest(p,&s.triangle);
            let road_z=if s.kind==3 {let Some(z)=self.terrain.sample(p)else{continue};z}else{mesh_z};
            if walk.is_some() && road_z<=z+0.025 { continue; }
            if (road_z-z).abs() < LEVEL_TOLERANCE && d <= CLEARANCE*CLEARANCE {
                return Err(if d <= 1e-12 { Rejection::Carriageway } else { Rejection::NearCarriageway });
            }
        }
        Ok(Foot { z, kind: if walk.is_some() { 2 } else { 3 } })
    }

    fn walk_height(&self,p:[f64;2],reference_z:f64)->Option<f64> {
        let cell=((p[0]/CELL).floor() as i32,(p[1]/CELL).floor() as i32);
        let mut best:Option<f64>=None;
        for &i in self.cells.get(&cell).map(Vec::as_slice).unwrap_or(&[]) {
            let s=&self.surfaces[i];if s.kind!=2 { continue; }
            let (d,z)=nearest(p,&s.triangle);
            if d<=1e-12 && (z-reference_z).abs()<=LEVEL_TOLERANCE
                && best.map_or(true,|old|(z-reference_z).abs()<(old-reference_z).abs()
                               || (z-reference_z).abs()==(old-reference_z).abs() && z<old) { best=Some(z); }
        }
        best
    }

    pub fn road_floor(&self, p: [f64;2], reference_z: f64) -> Option<f64> {
        let cell=((p[0]/CELL).floor() as i32,(p[1]/CELL).floor() as i32);
        let mut best:Option<f64>=None;
        for &i in self.cells.get(&cell).map(Vec::as_slice).unwrap_or(&[]) {
            let s=&self.surfaces[i];if s.kind!=1 && s.kind!=3 { continue; }
            let (d,mesh_z)=nearest(p,&s.triangle);
            let z=if s.kind==3 {let Some(z)=self.terrain.sample(p)else{continue};z}else{mesh_z};
            if self.walk_height(p,reference_z).is_some_and(|h|h>=z-0.025) { continue; }
            if d<=1e-12 && (z-reference_z).abs()<=LEVEL_TOLERANCE
                && best.map_or(true,|old|(z-reference_z).abs()<(old-reference_z).abs()) { best=Some(z); }
        }
        best
    }

    pub fn column_clear(&self, p: [f64;2], bottom: f64, top: f64, radius: f64) -> bool {
        if !p.iter().all(|v|v.is_finite()) || !bottom.is_finite() || !top.is_finite()
            || top <= bottom || !(0. ..=CLEARANCE).contains(&radius) { return false; }
        let cell=((p[0]/CELL).floor() as i32,(p[1]/CELL).floor() as i32);
        self.cells.get(&cell).map_or(true, |indices| indices.iter().all(|&i| {
            let s=&self.surfaces[i];let (d,mesh_z)=nearest(p,&s.triangle);
            let z=if s.kind==3 {let Some(z)=self.terrain.sample(p)else{return false};z}else{mesh_z};
            d > radius*radius || z <= bottom+0.25 || z >= top+0.15
        }))
    }

    pub fn overhead_clear(&self, a: [f64;2], b: [f64;2], bottom: f64, top: f64) -> bool {
        let length=((b[0]-a[0]).powi(2)+(b[1]-a[1]).powi(2)).sqrt();
        if !length.is_finite() || length > 40. || !bottom.is_finite() || !top.is_finite() || top <= bottom { return false; }
        let count=(length/0.4).ceil().max(1.) as usize;
        for k in 0..=count {
            let t=k as f64/count as f64;let p=[a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])];
            if !self.covered(p,0.16) { return false; }
            let cell=((p[0]/CELL).floor() as i32,(p[1]/CELL).floor() as i32);
            for &i in self.cells.get(&cell).map(Vec::as_slice).unwrap_or(&[]) {
                let s=&self.surfaces[i];let (d,mesh_z)=nearest(p,&s.triangle);
                let z=if s.kind==3 {let Some(z)=self.terrain.sample(p)else{return false};z}else{mesh_z};
                if d > 0.16*0.16 { continue; }
                if z > bottom && z < top+0.15 { return false; }
                if (s.kind == 1 || s.kind == 3) && z <= bottom && bottom-z < 4.5 { return false; }
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn terrain(z: f64) -> Terrain { Terrain { origin: [-20.,-20.],step:20.,width:3,height:3,z:vec![z;9],holes:vec![] } }
    fn triangle(kind: u8, z: f64) -> Surface { Surface {kind,triangle:[[-10.,-10.,z],[10.,-10.,z],[0.,10.,z]]} }
    #[test] fn actual_walk_height_prevents_floating() {
        let scene=Scene::new(vec![triangle(2,0.15)],terrain(0.),vec![[-100.,-100.,100.,100.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],0.),Ok(Foot{z:0.15,kind:2}));
    }
    #[test] fn carriageway_and_complete_foot_margin_are_rejected() {
        let scene=Scene::new(vec![triangle(1,0.)],terrain(0.),vec![[-100.,-100.,100.,100.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],0.),Err(Rejection::Carriageway));
        assert_eq!(scene.foot([0.,-10.4],0.),Err(Rejection::NearCarriageway));
        assert_eq!(scene.foot([0.,-10.7],0.),Ok(Foot{z:0.,kind:3}));
    }
    #[test] fn overhead_bridge_does_not_block_ground_level_foot() {
        let scene=Scene::new(vec![triangle(1,8.)],terrain(0.),vec![[-100.,-100.,100.,100.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],0.),Ok(Foot{z:0.,kind:3}));
    }
    #[test] fn bridge_approach_cannot_put_foot_on_ground_far_below() {
        let scene=Scene::new(vec![],terrain(0.),vec![[-100.,-100.,100.,100.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],8.),Err(Rejection::WrongHeightLayer));
    }
    #[test] fn sloping_walk_uses_barycentric_surface_height() {
        let mut walk=triangle(2,0.);walk.triangle[2][2]=2.;
        let scene=Scene::new(vec![walk],terrain(0.),vec![[-100.,-100.,100.,100.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],1.),Ok(Foot{z:1.,kind:2}));
    }
    #[test] fn outside_or_unknown_terrain_has_no_flat_fallback() {
        let scene=Scene::new(vec![],terrain(f64::NAN),vec![[-100.,-100.,100.,100.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],0.),Err(Rejection::MissingGround));
        assert_eq!(scene.foot([100.,100.],0.),Err(Rejection::MissingGround));
    }
    #[test] fn quantised_terrain_bilinear_and_last_grid_sample_match_game_contract() {
        let grid=Terrain{origin:[0.,0.],step:1.,width:2,height:2,z:vec![0.,2.,4.,8.],holes:vec![]};
        assert_eq!(grid.sample([0.5,0.5]),Some(3.5));
        assert_eq!(grid.sample([1.,1.]),Some(8.));
        assert_eq!(grid.sample([1.001,1.]),None);
    }
    #[test] fn invalid_surface_rejects_scene() {
        let mut road=triangle(1,0.);road.triangle[0][0]=f64::NAN;
        assert!(Scene::new(vec![road],terrain(0.),vec![]).is_err());
    }
    #[test] fn complete_adjacent_tiles_cover_their_shared_seam() {
        let scene=Scene::new(vec![],terrain(0.),vec![[-20.,-20.,0.,20.],[0.,-20.,20.,20.]]).unwrap();
        assert!(scene.covered([0.,0.],0.6));
        assert!(scene.foot([0.,0.],0.).is_ok());
        let partial=Scene::new(vec![],terrain(0.),vec![[-20.,-20.,0.,20.]]).unwrap();
        assert!(!partial.covered([0.,0.],0.6));
    }
    #[test]fn rotated_loaded_tiles_cover_a_seam_without_bounding_box_false_coverage(){
        let left=[[-20.,-22.],[0.,-20.],[0.,20.],[-20.,18.]];
        let right=[[0.,-20.],[20.,-18.],[20.,22.],[0.,20.]];
        let full=Scene::new(vec![],terrain(0.),vec![]).unwrap().with_quads(vec![left,right]).unwrap();
        assert!(full.covered([0.,0.],0.6));
        let half=Scene::new(vec![],terrain(0.),vec![]).unwrap().with_quads(vec![left]).unwrap();
        assert!(!half.covered([0.,0.],0.6));
        assert!(!full.covered([19.,-19.],0.1));
    }
    #[test] fn raised_island_overrides_underlying_road_skin_without_becoming_a_lane() {
        let scene=Scene::new(vec![triangle(1,0.),triangle(2,0.15)],terrain(0.),vec![[-20.,-20.,20.,20.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],0.),Ok(Foot{z:0.15,kind:2}));
        assert_eq!(scene.road_floor([0.,0.],0.),None);
        assert_eq!(scene.foot([0.,9.95],0.),Err(Rejection::WalkEdge));
    }
    #[test] fn terrain_holes_never_supply_invisible_ground() {
        let mut grid=terrain(0.);grid.holes=vec![1;9];
        let scene=Scene::new(vec![],grid,vec![[-20.,-20.,20.,20.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],0.),Err(Rejection::MissingGround));
    }
    #[test] fn painted_carriageway_uses_committed_terrain_not_placeholder_mesh_height() {
        let scene=Scene::new(vec![triangle(3,-99.)],terrain(2.),vec![[-20.,-20.,20.,20.]]).unwrap();
        assert_eq!(scene.road_floor([0.,0.],2.),Some(2.));
        assert_eq!(scene.road_floor([0.,0.],20.),None);
        assert_eq!(scene.foot([0.,0.],2.),Err(Rejection::Carriageway));
        assert!(!scene.overhead_clear([-1.,0.],[1.,0.],5.,6.));
    }
    #[test] fn raised_island_overrides_painted_carriageway() {
        let scene=Scene::new(vec![triangle(3,-99.),triangle(2,2.15)],terrain(2.),vec![[-20.,-20.,20.,20.]]).unwrap();
        assert_eq!(scene.foot([0.,0.],2.),Ok(Foot{z:2.15,kind:2}));
        assert_eq!(scene.road_floor([0.,0.],2.),None);
    }
    #[test] fn painted_road_does_not_fill_terrain_holes() {
        let mut grid=terrain(2.);grid.holes=vec![1;9];
        let scene=Scene::new(vec![triangle(3,2.)],grid,vec![[-20.,-20.,20.,20.]]).unwrap();
        assert_eq!(scene.road_floor([0.,0.],2.),None);
        assert_eq!(scene.foot([0.,0.],2.),Err(Rejection::MissingGround));
    }
}
