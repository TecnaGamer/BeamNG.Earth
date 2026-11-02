//! `/game/<path>`: the game's own shape files for the props (trees.lua's CATALOG: stock lamp posts, hydrants,
//! signs, guard rails ... and the mod's own), found where the game loads them from (read only):
//!   1. `<user-dir>/temp/<path>`: the engine's compiled cache (the mod's .dae shapes as the game compiled them)
//!   2. the mod folder (`mod/beamng_earth/<path>`)
//!   3. the game's content zips (`content/art_shapes.zip`, `content/levels/<level>.zip`: each .dae ships with its
//!      compiled .cdae, stored)
//! Served unchanged; the response's `x-source` says which. Only shape files (.cdae), and the mod folder's
//! textures (.png / .dds: its materials' opacity maps, the highway signs' lettering).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use axum::body::Body;
use axum::extract::{Path as UrlPath, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::files::safe_join;
use crate::zipfs;
use crate::App;

pub struct ZipIndex {
    zips: Vec<PathBuf>,
    /// lowercased path -> (zip, entry)
    entries: HashMap<String, (usize, zipfs::Entry)>,
}

static INDEX: OnceLock<Mutex<Option<Arc<ZipIndex>>>> = OnceLock::new();

fn build_index(game: &Path) -> ZipIndex {
    let content = game.join("content");
    let mut zips: Vec<PathBuf> = vec![content.join("art_shapes.zip")];
    if let Ok(rd) = std::fs::read_dir(content.join("levels")) {
        let mut lv: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x.eq_ignore_ascii_case("zip")))
            .collect();
        lv.sort();
        zips.extend(lv);
    }
    let mut entries = HashMap::new();
    let mut kept = Vec::new();
    for z in zips {
        let Ok(mut f) = std::fs::File::open(&z) else { continue };
        match zipfs::index(&mut f) {
            Ok(list) => {
                let k = kept.len();
                for e in list {
                    let n = e.name.to_ascii_lowercase();
                    if n.ends_with(".cdae") {
                        entries.entry(n).or_insert((k, e));
                    }
                }
                kept.push(z);
            }
            Err(e) => eprintln!("game zip {}: {e}", z.display()),
        }
    }
    ZipIndex { zips: kept, entries }
}

fn index(game: &Path) -> Arc<ZipIndex> {
    let cell = INDEX.get_or_init(|| Mutex::new(None));
    let mut g = cell.lock().unwrap();
    if g.is_none() {
        let t = std::time::Instant::now();
        let ix = build_index(game);
        println!("game zips: {} shape files in {} zips, indexed in {:.1} s", ix.entries.len(), ix.zips.len(), t.elapsed().as_secs_f32());
        *g = Some(Arc::new(ix));
    }
    g.as_ref().unwrap().clone()
}

/// The file's bytes and where they came from.
fn find(app: &App, rel: &str) -> Result<(Vec<u8>, String), (StatusCode, String)> {
    let lower = rel.to_ascii_lowercase();
    let bad = || (StatusCode::BAD_REQUEST, "bad path".to_string());
    // the mod's own material textures (its *.materials.json name them): from the mod folder only
    if lower.ends_with(".png") || lower.ends_with(".dds") {
        let m = safe_join(&app.cfg.mod_dir, rel).ok_or_else(bad)?;
        return std::fs::read(&m)
            .map(|b| (b, "mod".to_string()))
            .map_err(|_| (StatusCode::NOT_FOUND, format!("{rel}: not in the mod folder")));
    }
    if !lower.ends_with(".cdae") {
        return Err((StatusCode::FORBIDDEN, "only .cdae shapes and the mod's textures".into()));
    }
    let cache = safe_join(&app.cfg.user_dir.join("temp"), rel).ok_or_else(bad)?;
    if let Ok(b) = std::fs::read(&cache) {
        return Ok((b, "cache".into()));
    }
    let m = safe_join(&app.cfg.mod_dir, rel).ok_or_else(bad)?;
    if let Ok(b) = std::fs::read(&m) {
        return Ok((b, "mod".into()));
    }
    let Some(game) = &app.cfg.game_dir else {
        return Err((StatusCode::NOT_FOUND, "no game folder (--game-dir)".into()));
    };
    let ix = index(game);
    let key = lower.trim_start_matches('/').to_string();
    let Some((z, e)) = ix.entries.get(&key) else {
        return Err((StatusCode::NOT_FOUND, format!("{rel}: not in the game's cache, the mod or its zips")));
    };
    let zp = &ix.zips[*z];
    let mut f = std::fs::File::open(zp).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let b = zipfs::read(&mut f, e).map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    Ok((b, format!("zip:{}", zp.file_name().unwrap_or_default().to_string_lossy())))
}

/// Every `*.materials.json` under the mod's `levels/` folder, merged into one {name: definition} map (later files
/// win, as the engine's later loads), for the page's materials (materials.js).
fn mod_materials(mod_dir: &Path) -> serde_json::Map<String, serde_json::Value> {
    let mut files = Vec::new();
    let mut stack = vec![mod_dir.join("levels")];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with(".materials.json")) {
                files.push(p);
            }
        }
    }
    files.sort();
    let mut out = serde_json::Map::new();
    for f in files {
        let Ok(t) = std::fs::read_to_string(&f) else { continue };
        match serde_json::from_str::<serde_json::Value>(&t) {
            Ok(serde_json::Value::Object(m)) => out.extend(m),
            Ok(_) => {}
            Err(e) => eprintln!("{}: {e}", f.display()),
        }
    }
    out
}

pub async fn materials(State(app): State<Arc<App>>) -> Response {
    let dir = app.cfg.mod_dir.clone();
    match tokio::task::spawn_blocking(move || mod_materials(&dir)).await {
        Ok(m) => ([(header::CONTENT_TYPE, "application/json")], serde_json::Value::Object(m).to_string()).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

pub async fn shape(State(app): State<Arc<App>>, UrlPath(rel): UrlPath<String>) -> Response {
    let res = tokio::task::spawn_blocking(move || find(&app, &rel)).await;
    match res {
        Ok(Ok((bytes, src))) => (
            [(header::CONTENT_TYPE, "application/octet-stream".to_string()),
             (header::CACHE_CONTROL, "no-cache".to_string()),
             (header::HeaderName::from_static("x-source"), src)],
            Body::from(bytes),
        )
            .into_response(),
        Ok(Err((code, msg))) => (code, msg).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}
