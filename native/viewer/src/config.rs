//! Command line and the default folders (the same ones earthd and start.py use).

use std::net::SocketAddr;
use std::path::{Path, PathBuf};

pub struct Config {
    pub earthd: SocketAddr,
    pub port: u16,
    /// The BeamNG.drive user folder earthd writes into (`<user-dir>/temp/beamngEarth/<sessionId>/`).
    pub user_dir: PathBuf,
    /// earthd's download cache (the map-data mirror: `<cache-dir>/mapdata-mirror/`).
    pub cache_dir: PathBuf,
    pub web_dir: PathBuf,
}

const HELP: &str = "bnge-viewer [--earthd 127.0.0.1:47613] [--port 47700] [--user-dir DIR] [--cache-dir DIR] [--web DIR]

  --earthd     earthd's address (python3 server/earthd.py / start.py; default 127.0.0.1:47613)
  --port       the viewer's HTTP port on 127.0.0.1 (default 47700)
  --user-dir   the BeamNG.drive user folder earthd writes into (its --user-dir; default: start.py's
               .beamng_earth.json, else the usual place of the game's folder)
  --cache-dir  earthd's download cache (its --cache-dir; default: the launcher's choice, else ~/.cache/beamng-earth)
  --web        the web app's folder (default: native/viewer/web)";

fn home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// The repository root (native/viewer/../..), where start.py keeps `.beamng_earth.json`.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

fn json_str(path: &Path, key: &str) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get(key)?.as_str().filter(|s| !s.is_empty()).map(str::to_string)
}

/// start.py's choice (install.py writes it), else the first usual folder that exists.
fn default_user_dir() -> PathBuf {
    if let Some(p) = json_str(&repo_root().join(".beamng_earth.json"), "user_dir") {
        return PathBuf::from(p);
    }
    let mut cands = Vec::new();
    if let Some(la) = std::env::var_os("LOCALAPPDATA") {
        let la = PathBuf::from(la);
        cands.push(la.join("BeamNG").join("BeamNG.drive").join("current"));
        cands.push(la.join("BeamNG.drive").join("current"));
    }
    cands.push(home().join(".local/share/BeamNG/BeamNG.drive/current"));
    cands.push(home().join(".local/share/BeamNG.drive/current"));
    for c in &cands {
        if c.is_dir() {
            return c.clone();
        }
    }
    cands.into_iter().next().unwrap()
}

/// apppaths.py: the launcher's app.json (`cacheDir`) in its data folder, else ~/.cache/beamng-earth.
fn default_cache_dir() -> PathBuf {
    let data_dir = match std::env::var_os("LOCALAPPDATA") {
        Some(la) if cfg!(windows) => PathBuf::from(la).join("beamng-earth"),
        _ => home().join(".cache").join("beamng-earth"),
    };
    if let Some(p) = json_str(&data_dir.join("app.json"), "cacheDir") {
        return PathBuf::from(p);
    }
    home().join(".cache").join("beamng-earth")
}

fn default_web_dir() -> PathBuf {
    let built = Path::new(env!("CARGO_MANIFEST_DIR")).join("web");
    if built.is_dir() {
        return built;
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("web")))
        .unwrap_or(built)
}

impl Config {
    pub fn from_args(args: Vec<String>) -> Result<Config, String> {
        let mut earthd: SocketAddr = "127.0.0.1:47613".parse().unwrap();
        let mut port = 47700u16;
        let (mut user_dir, mut cache_dir, mut web_dir) = (None, None, None);
        let mut it = args.into_iter();
        while let Some(a) = it.next() {
            let mut val = |name: &str| it.next().ok_or_else(|| format!("{name} needs a value\n\n{HELP}"));
            match a.as_str() {
                "--earthd" => {
                    let v = val("--earthd")?;
                    let v = if v.contains(':') { v } else { format!("127.0.0.1:{v}") };
                    earthd = v.parse().map_err(|e| format!("--earthd {v}: {e}"))?;
                }
                "--port" => port = val("--port")?.parse().map_err(|e| format!("--port: {e}"))?,
                "--user-dir" => user_dir = Some(PathBuf::from(val("--user-dir")?)),
                "--cache-dir" => cache_dir = Some(PathBuf::from(val("--cache-dir")?)),
                "--web" => web_dir = Some(PathBuf::from(val("--web")?)),
                "-h" | "--help" => return Err(HELP.to_string()),
                other => return Err(format!("unknown argument {other}\n\n{HELP}")),
            }
        }
        Ok(Config {
            earthd,
            port,
            user_dir: user_dir.unwrap_or_else(default_user_dir),
            cache_dir: cache_dir.unwrap_or_else(default_cache_dir),
            web_dir: web_dir.unwrap_or_else(default_web_dir),
        })
    }
}
