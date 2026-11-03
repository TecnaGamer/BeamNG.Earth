//! BeamNG.Earth viewer (docs/VIEWER.md): a browser view of exactly the files earthd sends the game.
//!
//! The program is one more client of earthd (protocol 4, 127.0.0.1:47613), never a second
//! generator: the page speaks the mod's protocol through `/ws` (bridged to earthd's framed TCP,
//! one earthd connection per page), earthd writes its files into the game's temp folder as for
//! the game, and `/data/...` serves those files byte for byte. Nothing of the server is changed.
//!
//! Endpoints:
//!   /                 the web app (`web/`)
//!   /ws               WebSocket <-> earthd (text frames = the protocol's JSON messages)
//!   /data/<sid>/<f>   <user-dir>/temp/beamngEarth/<sid>/<f>, unchanged
//!   /api/config       what the page needs to know about this program
//!   /api/osm?z&x&y    the OSM ways and Overture connectors of a map-data cell (osm.rs)
//!   /tiles/...        earthd's map tiles (maptiles.py, port from its hello.mapPort)
//!   /game/<path>      the game's own shapes for the props, the mod's textures (game.rs)
//!   /api/materials    the mod's material definitions, merged (game.rs)
//!   /api/earthd/start POST: start the world server (run_server.bat / .sh, launch.rs)

mod bridge;
mod config;
mod files;
mod game;
mod launch;
mod osm;
mod tiles;
mod zipfs;

use std::sync::{Arc, Mutex};

use axum::routing::{get, post};
use axum::Router;

pub struct App {
    pub cfg: config::Config,
    /// earthd's map tile port (hello.mapPort of the latest earthd hello).
    pub map_port: Mutex<Option<u16>>,
    /// When the page last started the world server (launch.rs).
    pub launched: Mutex<Option<std::time::Instant>>,
}

#[tokio::main]
async fn main() {
    let cfg = match config::Config::from_args(std::env::args().skip(1).collect()) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    let bind = format!("127.0.0.1:{}", cfg.port);
    println!("bnge-viewer {}", env!("CARGO_PKG_VERSION"));
    println!("  earthd      {}", cfg.earthd);
    println!("  user dir    {}", cfg.user_dir.display());
    println!("  cache dir   {}", cfg.cache_dir.display());
    println!("  web app     {}", cfg.web_dir.display());
    match &cfg.game_dir {
        Some(g) => println!("  game        {}", g.display()),
        None => println!("  game        not found: props drawn as markers (--game-dir)"),
    }
    if !cfg.user_dir.join("temp").is_dir() {
        eprintln!("warning: {} has no temp folder: is it the BeamNG.drive user folder earthd writes into? (--user-dir)",
                  cfg.user_dir.display());
    }
    let app = Arc::new(App { cfg, map_port: Mutex::new(None), launched: Mutex::new(None) });
    let router = Router::new()
        .route("/ws", get(bridge::ws))
        .route("/api/config", get(files::config))
        .route("/api/earthd/start", post(launch::start))
        .route("/api/osm", get(osm::handler))
        .route("/api/materials", get(game::materials))
        .route("/api/material", get(game::material))
        .route("/data/*path", get(files::data))
        .route("/game/*path", get(game::shape))
        .route("/tiles/*path", get(tiles::proxy))
        .fallback(get(files::web))
        .with_state(app);
    let listener = match tokio::net::TcpListener::bind(&bind).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot listen on {bind}: {e}");
            std::process::exit(1);
        }
    };
    println!("open http://{bind}/");
    if let Err(e) = axum::serve(listener, router).await {
        eprintln!("server: {e}");
    }
}
