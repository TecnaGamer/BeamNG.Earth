#!/bin/sh
# BeamNG.Earth viewer: a browser view of what the world server sends the game (docs/VIEWER.md).
# Start the world server first (run_server.sh), then this, then open http://127.0.0.1:47700/
cd "$(dirname "$0")/native/viewer" || exit 1
if ! command -v cargo >/dev/null 2>&1; then
  echo "The viewer needs Rust's cargo: https://rustup.rs"
  exit 1
fi
exec cargo run --release -- "$@"
