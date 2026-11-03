//! `/ws`: the page's WebSocket bridged to one earthd connection (protocol 4 frames: a 4-byte
//! little-endian length + UTF-8 JSON). Every message passes through unchanged; the page plays the
//! mod's part (hello, anchors, poses), so earthd sees an ordinary client and builds what it builds
//! for the game. The connection lives as long as the page: a reloaded page is a new client, and
//! earthd's per-connection state (what the client shows) starts fresh with it.
//!
//! Like the mod's net.lua, the page gets `{"type":"_connected"}` once earthd answers and
//! `{"type":"_disconnected","error":...}` when it goes away.

use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::Response;
use futures_util::{SinkExt, StreamExt};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc;

use crate::App;

/// Largest frame accepted from earthd (road tiles inline their JSON: a few MB at most).
const MAX_FRAME: usize = 512 << 20;

pub async fn ws(ws: WebSocketUpgrade, State(app): State<Arc<App>>) -> Response {
    ws.max_message_size(64 << 20)
        .max_frame_size(64 << 20)
        .on_upgrade(move |s| run(s, app))
}

fn note(kind: &str, error: Option<String>) -> Message {
    let mut v = serde_json::json!({ "type": kind });
    if let Some(e) = error {
        v["error"] = serde_json::Value::String(e);
    }
    Message::Text(v.to_string())
}

/// The map tile port of an earthd hello (`"mapPort"`), read without parsing every big message.
fn sniff_hello(app: &App, text: &str) {
    if text.len() > 1 << 16 || !text.contains("\"hello\"") {
        return;
    }
    if let Ok(v) = serde_json::from_str::<serde_json::Value>(text) {
        if v.get("type").and_then(|t| t.as_str()) == Some("hello") {
            let port = v.get("mapPort").and_then(|p| p.as_u64()).and_then(|p| u16::try_from(p).ok());
            *app.map_port.lock().unwrap() = port;
        }
    }
}

async fn run(ws: WebSocket, app: Arc<App>) {
    let (mut sink, mut stream) = ws.split();
    let (tx, mut rx) = mpsc::unbounded_channel::<Message>();
    // the page's writer: earthd's messages and the bridge's own notes, in order
    let writer = tokio::spawn(async move {
        while let Some(m) = rx.recv().await {
            let close = matches!(m, Message::Close(_));
            if sink.send(m).await.is_err() || close {
                break;
            }
        }
    });

    let tcp = match TcpStream::connect(app.cfg.earthd).await {
        Ok(t) => t,
        Err(e) => {
            let _ = tx.send(note("_disconnected", Some(format!("earthd at {}: {e}", app.cfg.earthd))));
            let _ = tx.send(Message::Close(None));
            let _ = writer.await;
            return;
        }
    };
    let _ = tcp.set_nodelay(true);
    let (mut rd, mut wr) = tcp.into_split();
    let _ = tx.send(note("_connected", None));

    // earthd -> page
    let tx2 = tx.clone();
    let app2 = app.clone();
    let reader = tokio::spawn(async move {
        let mut len = [0u8; 4];
        let err = loop {
            if let Err(e) = rd.read_exact(&mut len).await {
                break e.to_string();
            }
            let n = u32::from_le_bytes(len) as usize;
            if n > MAX_FRAME {
                break format!("frame of {n} bytes");
            }
            let mut buf = vec![0u8; n];
            if let Err(e) = rd.read_exact(&mut buf).await {
                break e.to_string();
            }
            let text = match String::from_utf8(buf) {
                Ok(t) => t,
                Err(e) => String::from_utf8_lossy(e.as_bytes()).into_owned(),
            };
            sniff_hello(&app2, &text);
            if tx2.send(Message::Text(text)).is_err() {
                return;
            }
        };
        let _ = tx2.send(note("_disconnected", Some(err)));
        let _ = tx2.send(Message::Close(None));
    });

    // page -> earthd
    while let Some(Ok(m)) = stream.next().await {
        let bytes = match m {
            Message::Text(t) => t.into_bytes(),
            Message::Binary(b) => b,
            Message::Close(_) => break,
            _ => continue,
        };
        let mut frame = Vec::with_capacity(bytes.len() + 4);
        frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
        frame.extend_from_slice(&bytes);
        if wr.write_all(&frame).await.is_err() {
            break;
        }
    }
    let _ = wr.shutdown().await;
    reader.abort();
    drop(tx);
    writer.abort();
}
