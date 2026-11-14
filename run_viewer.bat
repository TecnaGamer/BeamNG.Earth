@echo off
rem BeamNG.Earth viewer: a browser view of what the world server sends the game (docs/VIEWER.md).
rem Start the world server first (run_server.bat), then this, then open http://127.0.0.1:47700/
cd /d "%~dp0native\viewer"
where cargo >nul 2>nul
if errorlevel 1 (
  echo The viewer needs Rust's cargo: https://rustup.rs
  pause
  exit /b 1
)
cargo run --release -- %*
pause
