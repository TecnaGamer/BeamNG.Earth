//! Immutable server-side surface index and numeric assembly planning API.
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::json;
use crate::nlassembly_core::{self,Kind,Request};
use crate::nlsurface_core::{Scene,Surface,Terrain,Rejection};

fn request(r:numpy::ndarray::ArrayView1<'_,f64>)->Request{
    Request{stop:[r[0],r[1],r[2]],travel:[r[3],r[4]],
        lanes:if r[5].is_finite()&&r[5].fract()==0.&&(1. ..=16.).contains(&r[5])
            &&(r[8]==0.||r[8]==1.){r[5] as usize}else{0},
        opposite:if r[6].is_finite()&&r[6].fract()==0.&&(0. ..=16.).contains(&r[6]){r[6] as usize}else{17},
        radius:r[7],overhead:r[8]==1.,before:r[9],after:r[10]}
}

#[pyclass(module="earthfast", name="NlSignalScene")]
pub struct SignalScene { scene: Scene }

#[pymethods]
impl SignalScene {
    #[new]
    #[pyo3(signature=(points,triangles,kinds,terrain,origin,step,coverage,holes=None))]
    fn new(py: Python<'_>, points: PyReadonlyArray2<'_,f64>, triangles: PyReadonlyArray2<'_,i64>,
           kinds: PyReadonlyArray1<'_,u8>, terrain: PyReadonlyArray2<'_,f64>, origin:[f64;2],
           step:f64, coverage:PyReadonlyArray2<'_,f64>, holes:Option<PyReadonlyArray2<'_,u8>>) -> PyResult<Self> {
        let (p,t,k,z,c)=(points.as_array(),triangles.as_array(),kinds.as_array(),terrain.as_array(),coverage.as_array());
        if p.ncols()!=3 || t.ncols()!=3 || t.nrows()!=k.len() || t.nrows()>1_000_000
            || p.nrows()>2_000_000 || c.ncols()!=4 || c.nrows()>4096 || z.ncols()>4096 || z.nrows()>4096 {
            return Err(PyValueError::new_err("signal surface array shapes"));
        }
        let mut surfaces=Vec::with_capacity(t.nrows());
        for (i,row) in t.rows().into_iter().enumerate() {
            if row.iter().any(|j|*j<0 || *j as usize>=p.nrows()) { return Err(PyValueError::new_err("signal surface triangle index")); }
            let mut tri=[[0.;3];3];
            for j in 0..3 { for axis in 0..3 { tri[j][axis]=p[[row[j] as usize,axis]]; } }
            surfaces.push(Surface{kind:k[i],triangle:tri});
        }
        let hole_flags=if let Some(mask)=holes { let h=mask.as_array();
            if h.shape()!=z.shape() { return Err(PyValueError::new_err("terrain hole mask shape")); }
            h.iter().copied().collect()
        } else { Vec::new() };
        let terrain=Terrain{origin,step,width:z.ncols(),height:z.nrows(),z:z.iter().copied().collect(),holes:hole_flags};
        let coverage:Vec<[f64;4]>=c.rows().into_iter().map(|r|[r[0],r[1],r[2],r[3]]).collect();
        let scene=py.detach(||Scene::new(surfaces,terrain,coverage)).map_err(PyValueError::new_err)?;
        Ok(Self{scene})
    }

    fn probe<'py>(&self,py:Python<'py>, queries:PyReadonlyArray2<'py,f64>) -> PyResult<Bound<'py,PyArray2<f64>>> {
        let q=queries.as_array();
        if q.ncols()!=3 || q.nrows()>100_000 { return Err(PyValueError::new_err("signal foot queries")); }
        let rows:Vec<[f64;3]>=q.rows().into_iter().map(|r|[r[0],r[1],r[2]]).collect();
        let n=rows.len();
        let out:Vec<f64>=py.detach(||rows.iter().flat_map(|r|match self.scene.foot([r[0],r[1]],r[2]) {
            Ok(f)=>[0.,f.z,f.kind as f64],
            Err(e)=>[match e{Rejection::MissingGround=>-1.,Rejection::WrongHeightLayer=>-2.,
                            Rejection::Carriageway=>-3.,Rejection::NearCarriageway=>-4.,Rejection::WalkEdge=>-5.},f64::NAN,0.],
        }).collect());
        Ok(numpy::ndarray::Array2::from_shape_vec((n,3),out).unwrap().into_pyarray(py))
    }

    fn plans(&self,py:Python<'_>, rows:PyReadonlyArray2<'_,f64>) -> PyResult<Vec<String>> {
        let a=rows.as_array();
        if a.ncols()!=11 || a.nrows()>10_000 { return Err(PyValueError::new_err("signal approach rows")); }
        let requests:Vec<Request>=a.rows().into_iter().map(request).collect();
        Ok(py.detach(||requests.iter().map(|r|match nlassembly_core::plan(&self.scene,r) {
            Err(reason)=>json!({"ready":false,"reason":reason,"retryable":matches!(reason,"surface_tiles_pending"|"ground_tiles_pending")}).to_string(),
            Ok(p)=>{
                let groups:Vec<_>=p.groups.iter().map(|g|json!({"foot":g.foot.xyz,"back_m":g.foot.back,
                    "lateral_m":g.foot.lateral,"mirrored":g.mirrored,"standing":g.standing,"lanes":g.lanes})).collect();
                json!({"ready":true,"kind":match p.kind{Kind::Standing=>"standing",Kind::Cantilever=>"cantilever",
                    Kind::Portal=>"portal",Kind::Split=>"split"},"lane_width_m":p.lane_width,"groups":groups}).to_string()
            }
        }).collect()))
    }

    fn assemblies(&self,py:Python<'_>,rows:PyReadonlyArray2<'_,f64>,catalog:&str,
                  turns:Vec<Vec<String>>,zones:Vec<Vec<i8>>,exits:Vec<bool>)->PyResult<Vec<String>>{
        let a=rows.as_array();let n=a.nrows();
        if a.ncols()!=11||n>256||turns.len()!=n||zones.len()!=n||exits.len()!=n||catalog.len()>1_000_000
            ||turns.iter().any(|r|r.len()>16||r.iter().any(|s|s.len()>128))
            ||zones.iter().any(|r|r.len()>128){return Err(PyValueError::new_err("signal assembly batch"));}
        let catalog:serde_json::Value=serde_json::from_str(catalog).map_err(|_|PyValueError::new_err("signal component catalog JSON"))?;
        let requests:Vec<Request>=a.rows().into_iter().map(request).collect();
        Ok(py.detach(||requests.iter().enumerate().map(|(i,r)|{
            let result=nlassembly_core::plan(&self.scene,r).and_then(|p|
                crate::nlparts::expand(r,&p,&catalog,&turns[i],&zones[i],exits[i]));
            match result{
                Ok(parts)=>json!({"ready":true,"assembly":parts}).to_string(),
                Err(reason)=>json!({"ready":false,"reason":reason,
                    "retryable":matches!(reason,"surface_tiles_pending"|"ground_tiles_pending")}).to_string(),
            }
        }).collect()))
    }
}

#[pyfunction]
pub fn nl_signal_lens_mm(zones:Vec<i8>,motorway_exit:bool)->PyResult<u16> {
    if zones.len()>128 || zones.iter().any(|v|!(-1..=1).contains(v)) { return Err(PyValueError::new_err("junction zones")); }
    Ok(nlassembly_core::lens_mm(&zones,motorway_exit))
}
