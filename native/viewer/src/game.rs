//! The game's own files for the page's shapes and materials, found where the game loads them from (read only):
//!
//! `/game/<path>`: a shape (.cdae) or a texture (.dds / .png / .jpg), served unchanged; `x-source` says from where.
//!   - shapes: `<user-dir>/temp/<path>` (the engine's compiled cache: the mod's .dae shapes as the game compiled
//!     them), then the mod folder (`mod/beamng_earth/<path>`), then the game's content zips (each .dae there ships
//!     with its compiled .cdae).
//!   - textures: the mod folder, then the content zips, as the engine resolves a material's texture path: the path
//!     itself, its `.link` (a JSON {path} redirect: the level zips' links into `content/assets`), and the same with
//!     the extension `.dds` (the materials name `.png` files the game ships compressed as `.dds`).
//!
//! The content zips: `content/art_shapes.zip`, `content/levels/<level>.zip`, `content/assets/materials/*.zip`,
//! `content/assets/meshes.zip`, `content/assets/textures.zip`.
//!
//! `/api/materials`: the mod's material definitions (every `*.materials.json` under its `levels/` folder, merged).
//! `/api/material?name=<n>&near=<shape path>`: the definition the engine would give a shape's material name, from
//!   every `*.materials.json` in the mod and the content zips (`material_for`), with `__dir` (its file's folder, for
//!   relative texture paths) and `__file`.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use axum::body::Body;
use axum::extract::{Path as UrlPath, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::Value;

use crate::files::safe_join;
use crate::zipfs;
use crate::App;

pub struct ZipIndex {
    zips: Vec<PathBuf>,
    /// lowercased path (no leading slash) -> (zip, entry)
    entries: HashMap<String, (usize, zipfs::Entry)>,
}

/// A material definition and where it was read from.
struct MatDef {
    /// the file's folder in the game's file system, lowercased, with a leading slash ("/levels/x/art/shapes")
    dir: String,
    file: String,
    from_mod: bool,
    def: Value,
}

struct Materials {
    /// lowercased mapTo / name -> its definitions
    by_name: HashMap<String, Vec<MatDef>>,
    files: usize,
}

static INDEX: OnceLock<Mutex<Option<Arc<ZipIndex>>>> = OnceLock::new();
static MATERIALS: OnceLock<Mutex<Option<Arc<Materials>>>> = OnceLock::new();

fn content_zips(game: &Path) -> Vec<PathBuf> {
    let content = game.join("content");
    let mut zips: Vec<PathBuf> = vec![content.join("art_shapes.zip")];
    let list = |d: PathBuf| -> Vec<PathBuf> {
        let mut v: Vec<PathBuf> = std::fs::read_dir(d)
            .map(|rd| {
                rd.flatten()
                    .map(|e| e.path())
                    .filter(|p| p.extension().is_some_and(|x| x.eq_ignore_ascii_case("zip")))
                    .collect()
            })
            .unwrap_or_default();
        v.sort();
        v
    };
    zips.extend(list(content.join("levels")));
    zips.extend(list(content.join("assets").join("materials")));
    zips.push(content.join("assets").join("meshes.zip"));
    zips.push(content.join("assets").join("textures.zip"));
    zips
}

fn build_index(game: &Path) -> ZipIndex {
    let mut entries = HashMap::new();
    let mut kept = Vec::new();
    for z in content_zips(game) {
        let Ok(mut f) = std::fs::File::open(&z) else { continue };
        match zipfs::index(&mut f) {
            Ok(list) => {
                let k = kept.len();
                for e in list {
                    if e.name.ends_with('/') {
                        continue;
                    }
                    let n = e.name.to_ascii_lowercase();
                    entries.entry(n).or_insert((k, e));
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
        println!("game zips: {} files in {} zips, indexed in {:.1} s", ix.entries.len(), ix.zips.len(), t.elapsed().as_secs_f32());
        *g = Some(Arc::new(ix));
    }
    g.as_ref().unwrap().clone()
}

fn read_entry(ix: &ZipIndex, key: &str) -> Option<(Vec<u8>, String)> {
    let (z, e) = ix.entries.get(key)?;
    let zp = &ix.zips[*z];
    let mut f = std::fs::File::open(zp).ok()?;
    let b = zipfs::read(&mut f, e).ok()?;
    Some((b, format!("zip:{}", zp.file_name().unwrap_or_default().to_string_lossy())))
}

const TEXTURE_EXTS: [&str; 5] = [".dds", ".png", ".jpg", ".jpeg", ".tga"];

fn is_texture(lower: &str) -> bool {
    TEXTURE_EXTS.iter().any(|x| lower.ends_with(x))
}

/// The path with its texture extension swapped for `.dds` (as the engine finds a material's `.png` shipped as `.dds`).
fn as_dds(lower: &str) -> Option<String> {
    for x in &TEXTURE_EXTS[1..] {
        if let Some(stem) = lower.strip_suffix(x) {
            return Some(format!("{stem}.dds"));
        }
    }
    None
}

/// A texture from the content zips: the path, its .link, then both with .dds (links followed twice at most).
fn zip_texture(ix: &ZipIndex, lower: &str, depth: u32) -> Option<(Vec<u8>, String, String)> {
    let key = lower.trim_start_matches('/');
    let mut cands = vec![key.to_string()];
    if let Some(d) = as_dds(key) {
        cands.push(d);
    }
    for c in &cands {
        if let Some((b, src)) = read_entry(ix, c) {
            return Some((b, src, c.clone()));
        }
        if depth < 2 {
            if let Some((b, _)) = read_entry(ix, &format!("{c}.link")) {
                let to = serde_json::from_slice::<Value>(&b).ok()
                    .and_then(|v| v.get("path").and_then(|p| p.as_str()).map(|s| s.to_ascii_lowercase()));
                if let Some(to) = to {
                    if let Some(r) = zip_texture(ix, &to, depth + 1) {
                        return Some(r);
                    }
                }
            }
        }
    }
    None
}

fn content_type(path: &str) -> &'static str {
    if path.ends_with(".png") {
        "image/png"
    } else if path.ends_with(".jpg") || path.ends_with(".jpeg") {
        "image/jpeg"
    } else {
        "application/octet-stream"
    }
}

/// The file's bytes, where they came from and its content type.
fn find(app: &App, rel: &str) -> Result<(Vec<u8>, String, &'static str), (StatusCode, String)> {
    let lower = rel.to_ascii_lowercase();
    let bad = || (StatusCode::BAD_REQUEST, "bad path".to_string());
    if is_texture(&lower) {
        // the mod's own textures (as named, else as .dds)
        for c in std::iter::once(rel.to_string()).chain(as_dds(rel).into_iter()) {
            let m = safe_join(&app.cfg.mod_dir, &c).ok_or_else(bad)?;
            if let Ok(b) = std::fs::read(&m) {
                return Ok((b, "mod".into(), content_type(&c.to_ascii_lowercase())));
            }
        }
        let Some(game) = &app.cfg.game_dir else {
            return Err((StatusCode::NOT_FOUND, "no game folder (--game-dir)".into()));
        };
        let ix = index(game);
        return match zip_texture(&ix, &lower, 0) {
            Some((b, src, path)) => Ok((b, src, content_type(&path))),
            None => Err((StatusCode::NOT_FOUND, format!("{rel}: not in the mod or the game's zips"))),
        };
    }
    if !lower.ends_with(".cdae") {
        return Err((StatusCode::FORBIDDEN, "only .cdae shapes and textures".into()));
    }
    let cache = safe_join(&app.cfg.user_dir.join("temp"), rel).ok_or_else(bad)?;
    if let Ok(b) = std::fs::read(&cache) {
        return Ok((b, "cache".into(), "application/octet-stream"));
    }
    let m = safe_join(&app.cfg.mod_dir, rel).ok_or_else(bad)?;
    if let Ok(b) = std::fs::read(&m) {
        return Ok((b, "mod".into(), "application/octet-stream"));
    }
    let Some(game) = &app.cfg.game_dir else {
        return Err((StatusCode::NOT_FOUND, "no game folder (--game-dir)".into()));
    };
    let ix = index(game);
    match read_entry(&ix, lower.trim_start_matches('/')) {
        Some((b, src)) => Ok((b, src, "application/octet-stream")),
        None => Err((StatusCode::NOT_FOUND, format!("{rel}: not in the game's cache, the mod or its zips"))),
    }
}

/// JSON as the engine reads its material files: a byte order mark and trailing commas (`, }` / `, ]`) allowed.
fn lenient_json(b: &[u8]) -> Option<Value> {
    let t = std::str::from_utf8(b).ok()?.trim_start_matches('\u{feff}');
    if let Ok(v) = serde_json::from_str(t) {
        return Some(v);
    }
    let mut out = String::with_capacity(t.len());
    let (mut in_str, mut esc) = (false, false);
    let chars: Vec<char> = t.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        if in_str {
            out.push(c);
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = false;
            }
            continue;
        }
        if c == '"' {
            in_str = true;
        } else if c == ',' {
            let next = chars[i + 1..].iter().find(|c| !c.is_whitespace());
            if matches!(next, Some('}') | Some(']')) {
                continue;
            }
        }
        out.push(c);
    }
    serde_json::from_str(&out).ok()
}

fn add_defs(by: &mut HashMap<String, Vec<MatDef>>, file: &str, from_mod: bool, v: Value) {
    let Value::Object(m) = v else { return };
    let dir = file.rsplit_once('/').map(|(d, _)| d.to_string()).unwrap_or_default();
    for (k, def) in m {
        if !def.is_object() || def.get("class").and_then(|c| c.as_str()).is_some_and(|c| !c.eq_ignore_ascii_case("material")) {
            continue;
        }
        let mut names = vec![k.to_ascii_lowercase()];
        for f in ["mapTo", "name"] {
            if let Some(n) = def.get(f).and_then(|n| n.as_str()) {
                let n = n.to_ascii_lowercase();
                if !n.is_empty() && n != "unmapped_mat" && !names.contains(&n) {
                    names.push(n);
                }
            }
        }
        for n in names {
            by.entry(n).or_default().push(MatDef { dir: dir.clone(), file: file.to_string(), from_mod, def: def.clone() });
        }
    }
}

fn mod_material_files(mod_dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut stack = vec![mod_dir.join("levels")];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with("materials.json")) {
                files.push(p);
            }
        }
    }
    files.sort();
    files
}

fn build_materials(app: &App) -> Materials {
    let mut by = HashMap::new();
    let mut files = 0;
    for p in mod_material_files(&app.cfg.mod_dir) {
        let rel = p.strip_prefix(&app.cfg.mod_dir).unwrap_or(&p).to_string_lossy().replace('\\', "/").to_ascii_lowercase();
        if let Some(v) = std::fs::read(&p).ok().and_then(|b| lenient_json(&b)) {
            add_defs(&mut by, &format!("/{}", rel.trim_start_matches('/')), true, v);
            files += 1;
        }
    }
    if let Some(game) = &app.cfg.game_dir {
        let ix = index(game);
        let mut keys: Vec<&String> = ix.entries.keys().filter(|k| k.ends_with("materials.json")).collect();
        keys.sort();
        for k in keys {
            match read_entry(&ix, k).and_then(|(b, _)| lenient_json(&b)) {
                Some(v) => {
                    add_defs(&mut by, &format!("/{k}"), false, v);
                    files += 1;
                }
                None => eprintln!("material file {k}: not readable"),
            }
        }
    }
    Materials { by_name: by, files }
}

fn materials_index(app: &App) -> Arc<Materials> {
    let cell = MATERIALS.get_or_init(|| Mutex::new(None));
    let mut g = cell.lock().unwrap();
    if g.is_none() {
        let t = std::time::Instant::now();
        let m = build_materials(app);
        println!("materials: {} names from {} files, read in {:.1} s", m.by_name.len(), m.files, t.elapsed().as_secs_f32());
        *g = Some(Arc::new(m));
    }
    g.as_ref().unwrap().clone()
}

/// The definition the engine gives a material name for a shape at `near`: the mod's (its level's, loaded first),
/// else the one in the shape's own folder or the nearest folder above it, else the common ones (`/art/`,
/// `/assets/`), else the first.
fn material_for<'a>(m: &'a Materials, name: &str, near: &str) -> Option<&'a MatDef> {
    let list = m.by_name.get(&name.to_ascii_lowercase())?;
    if let Some(d) = list.iter().find(|d| d.from_mod) {
        return Some(d);
    }
    let near = near.to_ascii_lowercase();
    let shared = |d: &MatDef| {
        let dir = format!("{}/", d.dir);
        if near.starts_with(&dir) { dir.len() } else { 0 }
    };
    if let Some(d) = list.iter().filter(|d| shared(d) > 0).max_by_key(|d| shared(d)) {
        return Some(d);
    }
    list.iter()
        .find(|d| d.dir.starts_with("/art/") || d.dir.starts_with("/assets/"))
        .or_else(|| list.first())
}

#[derive(Deserialize)]
pub struct MaterialQuery {
    name: String,
    #[serde(default)]
    near: String,
}

pub async fn material(State(app): State<Arc<App>>, Query(q): Query<MaterialQuery>) -> Response {
    let res = tokio::task::spawn_blocking(move || {
        let m = materials_index(&app);
        material_for(&m, &q.name, &q.near).map(|d| {
            let mut v = d.def.clone();
            if let Value::Object(o) = &mut v {
                o.insert("__dir".into(), Value::String(d.dir.clone()));
                o.insert("__file".into(), Value::String(d.file.clone()));
            }
            v.to_string()
        })
    })
    .await;
    match res {
        Ok(Some(j)) => ([(header::CONTENT_TYPE, "application/json")], j).into_response(),
        Ok(None) => (StatusCode::NOT_FOUND, "no such material").into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

/// Every `*.materials.json` under the mod's `levels/` folder, merged into one {name: definition} map (later files
/// win, as the engine's later loads).
fn mod_materials(mod_dir: &Path) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    for f in mod_material_files(mod_dir) {
        match std::fs::read(&f).ok().and_then(|b| lenient_json(&b)) {
            Some(Value::Object(m)) => out.extend(m),
            Some(_) => {}
            None => eprintln!("{}: not readable", f.display()),
        }
    }
    out
}

pub async fn materials(State(app): State<Arc<App>>) -> Response {
    let dir = app.cfg.mod_dir.clone();
    match tokio::task::spawn_blocking(move || mod_materials(&dir)).await {
        Ok(m) => ([(header::CONTENT_TYPE, "application/json")], Value::Object(m).to_string()).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

pub async fn shape(State(app): State<Arc<App>>, UrlPath(rel): UrlPath<String>) -> Response {
    let res = tokio::task::spawn_blocking(move || find(&app, &rel)).await;
    match res {
        Ok(Ok((bytes, src, ctype))) => (
            [(header::CONTENT_TYPE, ctype.to_string()),
             (header::CACHE_CONTROL, "no-cache".to_string()),
             (header::HeaderName::from_static("x-source"), src)],
            Body::from(bytes),
        )
            .into_response(),
        Ok(Err((code, msg))) => (code, msg).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lenient() {
        let v = lenient_json(b"\xef\xbb\xbf{\"a\": {\"b\": [1, 2,], \"s\": \"x, }\",},\n}").unwrap();
        assert_eq!(v["a"]["b"][1], 2);
        assert_eq!(v["a"]["s"], "x, }");
    }

    #[test]
    fn dds_swap() {
        assert_eq!(as_dds("/a/t_b.color.png").as_deref(), Some("/a/t_b.color.dds"));
        assert_eq!(as_dds("/a/t.dds"), None);
    }
}
