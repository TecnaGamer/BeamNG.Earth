//! Files: earthd's output under `<user-dir>/temp/beamngEarth/` (`/data/...`, byte for byte: the
//! same bytes the game loads) and the web app.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Path as UrlPath, State};
use axum::http::{header, StatusCode, Uri};
use axum::response::{IntoResponse, Response};

use crate::App;

/// `rel` under `root`, or None when it could leave it (no `..`, no absolute parts, no drive letters).
pub fn safe_join(root: &Path, rel: &str) -> Option<PathBuf> {
    let mut p = root.to_path_buf();
    let mut any = false;
    for seg in rel.split('/') {
        if seg.is_empty() {
            continue;
        }
        if seg == "." || seg == ".." || seg.contains('\\') || seg.contains(':') || seg.contains('\0') {
            return None;
        }
        p.push(seg);
        any = true;
    }
    any.then_some(p)
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase().as_str() {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "svg" => "image/svg+xml",
        _ => "application/octet-stream",
    }
}

async fn send_file(path: PathBuf, cache: &'static str) -> Response {
    match tokio::fs::read(&path).await {
        Ok(bytes) => (
            [(header::CONTENT_TYPE, content_type(&path)), (header::CACHE_CONTROL, cache)],
            Body::from(bytes),
        )
            .into_response(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (StatusCode::NOT_FOUND, "not found").into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

/// `/data/<sid>/<file>`: earthd's files, unchanged. Never cached by the browser: pooled shape
/// files (`slot_<n>.cdae`) are replaced in place when their slot goes to another tile.
pub async fn data(State(app): State<Arc<App>>, UrlPath(rel): UrlPath<String>) -> Response {
    let root = app.cfg.user_dir.join("temp").join("beamngEarth");
    match safe_join(&root, &rel) {
        Some(p) => send_file(p, "no-store").await,
        None => (StatusCode::BAD_REQUEST, "bad path").into_response(),
    }
}

/// The web app (`/` = index.html).
pub async fn web(State(app): State<Arc<App>>, uri: Uri) -> Response {
    let rel = uri.path().trim_start_matches('/');
    let rel = if rel.is_empty() { "index.html" } else { rel };
    match safe_join(&app.cfg.web_dir, rel) {
        Some(p) => send_file(p, "no-cache").await,
        None => (StatusCode::BAD_REQUEST, "bad path").into_response(),
    }
}

/// `/api/config`: the folders in use (shown in the page's status panel).
pub async fn config(State(app): State<Arc<App>>) -> Response {
    let c = &app.cfg;
    let v = serde_json::json!({
        "version": env!("CARGO_PKG_VERSION"),
        "earthd": c.earthd.to_string(),
        "userDir": c.user_dir.display().to_string(),
        "userDirOk": c.user_dir.join("temp").is_dir(),
        "cacheDir": c.cache_dir.display().to_string(),
        "mapPort": *app.map_port.lock().unwrap(),
        "canStart": crate::launch::script(&app).is_some(),
        "serverScript": crate::launch::script_name(),
    });
    ([(header::CONTENT_TYPE, "application/json")], v.to_string()).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_join_stays_inside() {
        let r = Path::new("/root");
        assert_eq!(safe_join(r, "a/b.cdae"), Some(PathBuf::from("/root/a/b.cdae")));
        assert_eq!(safe_join(r, "/a//b"), Some(PathBuf::from("/root/a/b")));
        assert_eq!(safe_join(r, "a/../../etc"), None);
        assert_eq!(safe_join(r, "..\\x"), None);
        assert_eq!(safe_join(r, "C:/x"), None);
        assert_eq!(safe_join(r, ""), None);
    }
}
