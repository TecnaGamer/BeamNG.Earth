//! `POST /api/earthd/start`: the page's "Start the world server" button. Runs the repository's
//! `run_server.bat` (Windows: in a console window of its own, as a double-click would) or
//! `run_server.sh` (in a terminal when one is found, else in the background with its output in
//! `earthd-viewer.log`). The viewer does not own the server: closing its window stops it, and the
//! page connects by itself once earthd answers.
//!
//! Only for an earthd on this computer, only from this page (the Origin must be the viewer's own),
//! and at most once every 20 s.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::extract::State;
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::App;

const AGAIN_AFTER: Duration = Duration::from_secs(20);

pub fn script_name() -> &'static str {
    if cfg!(windows) { "run_server.bat" } else { "run_server.sh" }
}

/// The server's start script, if this viewer can start it (earthd on this computer, script present).
pub fn script(app: &App) -> Option<PathBuf> {
    if !app.cfg.earthd.ip().is_loopback() {
        return None;
    }
    let p = app.cfg.repo_dir.join(script_name());
    p.is_file().then_some(p)
}

fn reply(status: StatusCode, v: serde_json::Value) -> Response {
    (status, [(header::CONTENT_TYPE, "application/json")], v.to_string()).into_response()
}

/// A request from the viewer's own page (a browser always sends Origin on a POST).
fn same_origin(app: &App, headers: &HeaderMap) -> bool {
    let Some(origin) = headers.get(header::ORIGIN).and_then(|o| o.to_str().ok()) else { return false };
    let port = app.cfg.port;
    origin == format!("http://127.0.0.1:{port}") || origin == format!("http://localhost:{port}")
}

pub async fn start(State(app): State<Arc<App>>, headers: HeaderMap) -> Response {
    if !same_origin(&app, &headers) {
        return reply(StatusCode::FORBIDDEN, serde_json::json!({ "ok": false, "error": "Only the viewer's own page can start the server." }));
    }
    let Some(script) = script(&app) else {
        let error = if app.cfg.earthd.ip().is_loopback() {
            format!("{} is not in {}.", script_name(), app.cfg.repo_dir.display())
        } else {
            format!("The viewer is set to a server on another computer, {}.", app.cfg.earthd)
        };
        return reply(StatusCode::CONFLICT, serde_json::json!({ "ok": false, "error": error }));
    };
    {
        let mut last = app.launched.lock().unwrap();
        if let Some(t) = *last {
            if t.elapsed() < AGAIN_AFTER {
                // (a second click, or a second page: the first start is still under way)
                return reply(StatusCode::OK, serde_json::json!({ "ok": true, "how": "already starting" }));
            }
        }
        *last = Some(Instant::now());
    }
    match spawn(&app.cfg.repo_dir, &script) {
        Ok(how) => {
            println!("started the world server ({how}): {}", script.display());
            reply(StatusCode::OK, serde_json::json!({ "ok": true, "how": how }))
        }
        Err(e) => {
            *app.launched.lock().unwrap() = None;
            eprintln!("could not start the world server: {e}");
            reply(StatusCode::INTERNAL_SERVER_ERROR, serde_json::json!({ "ok": false, "error": e }))
        }
    }
}

#[cfg(windows)]
fn spawn(dir: &Path, script: &Path) -> Result<&'static str, String> {
    use std::os::windows::process::CommandExt;
    let name = script.file_name().and_then(|n| n.to_str()).unwrap_or("run_server.bat");
    // `start "title" /D dir script`: a new console window, as a double-click on the file opens
    Command::new("cmd")
        .raw_arg(format!("/C start \"BeamNG.Earth world server\" /D \"{}\" \"{name}\"", dir.display()))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| "a new window")
        .map_err(|e| format!("Windows could not open a window for the server: {e}."))
}

#[cfg(not(windows))]
fn spawn(dir: &Path, script: &Path) -> Result<&'static str, String> {
    let quiet = |c: &mut Command| { c.current_dir(dir).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()); };
    if cfg!(target_os = "macos") {
        let mut c = Command::new("open");
        c.args(["-a", "Terminal"]).arg(script);
        quiet(&mut c);
        return c.spawn().map(|_| "Terminal").map_err(|e| format!("open: {e}"));
    }
    if std::env::var_os("DISPLAY").is_some() || std::env::var_os("WAYLAND_DISPLAY").is_some() {
        let terms: [(&str, &[&str]); 4] =
            [("x-terminal-emulator", &["-e"]), ("gnome-terminal", &["--"]), ("konsole", &["-e"]), ("xterm", &["-e"])];
        for (t, args) in terms {
            let mut c = Command::new(t);
            c.args(args).arg("sh").arg(script);
            quiet(&mut c);
            if c.spawn().is_ok() {
                return Ok("a terminal");
            }
        }
    }
    let log = std::fs::File::create(dir.join("earthd-viewer.log")).map_err(|e| format!("earthd-viewer.log: {e}"))?;
    let err = log.try_clone().map_err(|e| e.to_string())?;
    Command::new("sh").arg(script).current_dir(dir).stdin(Stdio::null()).stdout(log).stderr(err).spawn()
        .map(|_| "the background (earthd-viewer.log)").map_err(|e| format!("sh: {e}"))
}
