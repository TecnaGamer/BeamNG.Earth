//! `/tiles/...`: earthd's map tiles (server/maptiles.py: GET only, one request per connection,
//! bound to 127.0.0.1) passed through for the minimap, so the page needs no CORS and no other port.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::{header, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use crate::App;

const TIMEOUT: Duration = Duration::from_secs(60);

async fn fetch(port: u16, path_query: &str) -> std::io::Result<(u16, String, Vec<u8>)> {
    let mut s = TcpStream::connect(("127.0.0.1", port)).await?;
    let req = format!("GET {path_query} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    s.write_all(req.as_bytes()).await?;
    let mut buf = Vec::new();
    s.read_to_end(&mut buf).await?;
    let split = buf
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| std::io::Error::other("no header end"))?;
    let head = String::from_utf8_lossy(&buf[..split]).into_owned();
    let body = buf[split + 4..].to_vec();
    let mut lines = head.lines();
    let status = lines
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|c| c.parse().ok())
        .unwrap_or(502);
    let ctype = lines
        .filter_map(|l| l.split_once(':'))
        .find(|(k, _)| k.trim().eq_ignore_ascii_case("content-type"))
        .map(|(_, v)| v.trim().to_string())
        .unwrap_or_else(|| "application/octet-stream".into());
    Ok((status, ctype, body))
}

pub async fn proxy(State(app): State<Arc<App>>, uri: Uri) -> Response {
    let Some(port) = *app.map_port.lock().unwrap() else {
        return (StatusCode::SERVICE_UNAVAILABLE, "earthd sent no mapPort (map tiles off, or no earthd hello yet)")
            .into_response();
    };
    let pq = uri.path_and_query().map(|p| p.as_str()).unwrap_or("/");
    match tokio::time::timeout(TIMEOUT, fetch(port, pq)).await {
        Ok(Ok((status, ctype, body))) => (
            StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
            [(header::CONTENT_TYPE, ctype), (header::CACHE_CONTROL, "max-age=3600".to_string())],
            Body::from(body),
        )
            .into_response(),
        Ok(Err(e)) => (StatusCode::BAD_GATEWAY, e.to_string()).into_response(),
        Err(_) => (StatusCode::GATEWAY_TIMEOUT, "map tile timed out").into_response(),
    }
}
