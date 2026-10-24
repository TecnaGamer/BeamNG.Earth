//! BeamNG.Earth's list of BeamMP servers (the user, 2026-10-24: "show a list of all the .earth servers ... make it so
//! players without .earth cant see .earth server in the server list"). A BeamNG.Earth server is Private in BeamMP (off
//! BeamMP's public list, which every BeamMP player sees); its plugin announces it here every minute instead, and the
//! game's BeamNG.Earth tab reads the list.
//!
//! POST /announce  {"port": 30814, "name": "...", "players": 2, "maxPlayers": 8, "version": "1.6.2", "place": "..."}
//!                 The address is the one the request comes from (behind a local reverse proxy: its X-Forwarded-For),
//!                 never the body's: a server can only list itself. Entries go TTL_S after their last announce.
//! GET  /servers   [{"ip", "port", "name", "players", "maxPlayers", "version", "place", "age"}], newest first.
//!
//! In memory only (servers announce again within a minute of a restart). Env: BNGE_LIST_ADDR (default 0.0.0.0:47640).

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::Read;
use std::net::IpAddr;
use std::time::{Duration, Instant};
use tiny_http::{Header, Method, Request, Response, Server};

const TTL: Duration = Duration::from_secs(180);
const MIN_GAP: Duration = Duration::from_secs(20); // announces from one server closer than this are refused
const MAX_SERVERS: usize = 1000;
const MAX_PER_IP: usize = 8; // servers one address may list (one per port)
const MAX_BODY: usize = 4096;

#[derive(Deserialize)]
struct Announce {
    port: u16,
    name: Option<String>,
    players: Option<u32>,
    #[serde(rename = "maxPlayers")]
    max_players: Option<u32>,
    version: Option<String>,
    place: Option<String>,
}

#[derive(Serialize, Clone)]
struct Listed {
    ip: String,
    port: u16,
    name: String,
    players: u32,
    #[serde(rename = "maxPlayers")]
    max_players: u32,
    version: String,
    place: String,
    age: u64,
}

struct Entry {
    listed: Listed,
    seen: Instant,
}

/// Printable text only, at most `max` characters (names show in the game's UI).
fn clean(s: Option<String>, max: usize) -> String {
    s.unwrap_or_default().chars().filter(|c| !c.is_control()).take(max).collect::<String>().trim().to_string()
}

/// The announcing server's address: the peer, or the first X-Forwarded-For hop when the peer is a local proxy.
fn client_ip(req: &Request) -> Option<IpAddr> {
    let peer = req.remote_addr()?.ip();
    if peer.is_loopback() {
        for h in req.headers() {
            if h.field.equiv("X-Forwarded-For") {
                return h.value.as_str().split(',').next()?.trim().parse().ok();
            }
        }
    }
    Some(peer)
}

fn json(code: u16, body: String) -> Response<std::io::Cursor<Vec<u8>>> {
    Response::from_string(body)
        .with_status_code(code)
        .with_header(Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap())
        .with_header(Header::from_bytes(&b"Access-Control-Allow-Origin"[..], &b"*"[..]).unwrap())
}

struct List {
    entries: HashMap<(IpAddr, u16), Entry>,
}

impl List {
    fn expire(&mut self, now: Instant) {
        self.entries.retain(|_, e| now.duration_since(e.seen) < TTL);
    }

    fn announce(&mut self, ip: IpAddr, a: Announce, now: Instant) -> Result<(), (u16, &'static str)> {
        if a.port == 0 {
            return Err((400, "bad port"));
        }
        self.expire(now);
        let key = (ip, a.port);
        if let Some(e) = self.entries.get(&key) {
            if now.duration_since(e.seen) < MIN_GAP {
                return Err((429, "too soon"));
            }
        } else {
            if self.entries.len() >= MAX_SERVERS {
                return Err((503, "list full"));
            }
            if self.entries.keys().filter(|(i, _)| *i == ip).count() >= MAX_PER_IP {
                return Err((429, "too many servers from this address"));
            }
        }
        let listed = Listed {
            ip: ip.to_string(),
            port: a.port,
            name: clean(a.name, 48),
            players: a.players.unwrap_or(0).min(999),
            max_players: a.max_players.unwrap_or(0).min(999),
            version: clean(a.version, 24),
            place: clean(a.place, 64),
            age: 0,
        };
        self.entries.insert(key, Entry { listed, seen: now });
        Ok(())
    }

    fn servers(&mut self, now: Instant) -> Vec<Listed> {
        self.expire(now);
        let mut out: Vec<Listed> = self
            .entries
            .values()
            .map(|e| Listed { age: now.duration_since(e.seen).as_secs(), ..e.listed.clone() })
            .collect();
        out.sort_by(|a, b| a.age.cmp(&b.age).then(a.name.cmp(&b.name)));
        out
    }
}

fn main() {
    let addr = std::env::var("BNGE_LIST_ADDR").unwrap_or_else(|_| "0.0.0.0:47640".to_string());
    let server = Server::http(&addr).expect("listen");
    eprintln!("bnge-serverlist on {addr}");
    let mut list = List { entries: HashMap::new() };
    for mut req in server.incoming_requests() {
        let now = Instant::now();
        let path = req.url().split('?').next().unwrap_or("").to_string();
        let resp = match (req.method(), path.as_str()) {
            (Method::Get, "/servers") => json(200, serde_json::to_string(&list.servers(now)).unwrap()),
            (Method::Post, "/announce") => {
                let mut body = Vec::new();
                let read = req.as_reader().take(MAX_BODY as u64 + 1).read_to_end(&mut body);
                match (read, client_ip(&req)) {
                    (Ok(n), _) if n > MAX_BODY => json(413, "{\"error\":\"too big\"}".into()),
                    (Ok(_), Some(ip)) => match serde_json::from_slice::<Announce>(&body) {
                        Ok(a) => match list.announce(ip, a, now) {
                            Ok(()) => json(200, "{\"ok\":true}".into()),
                            Err((code, why)) => json(code, format!("{{\"error\":\"{why}\"}}")),
                        },
                        Err(_) => json(400, "{\"error\":\"bad json\"}".into()),
                    },
                    _ => json(400, "{\"error\":\"bad request\"}".into()),
                }
            }
            _ => json(404, "{\"error\":\"not found\"}".into()),
        };
        let _ = req.respond(resp);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ann(port: u16, name: &str) -> Announce {
        Announce { port, name: Some(name.into()), players: Some(2), max_players: Some(8), version: Some("1.6.2".into()),
            place: None }
    }

    #[test]
    fn listed_then_expired() {
        let mut l = List { entries: HashMap::new() };
        let ip: IpAddr = "203.0.113.5".parse().unwrap();
        let t0 = Instant::now();
        l.announce(ip, ann(30814, "A\u{7}b"), t0).unwrap();
        let s = l.servers(t0);
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].name, "Ab");
        assert_eq!(s[0].ip, "203.0.113.5");
        assert!(l.servers(t0 + TTL + Duration::from_secs(1)).is_empty());
    }

    #[test]
    fn too_soon_and_per_ip_cap() {
        let mut l = List { entries: HashMap::new() };
        let ip: IpAddr = "203.0.113.6".parse().unwrap();
        let t0 = Instant::now();
        l.announce(ip, ann(1, "a"), t0).unwrap();
        assert_eq!(l.announce(ip, ann(1, "a"), t0 + Duration::from_secs(5)).unwrap_err().0, 429);
        assert!(l.announce(ip, ann(1, "a"), t0 + MIN_GAP).is_ok());
        for p in 2..=MAX_PER_IP as u16 {
            l.announce(ip, ann(p, "x"), t0).unwrap();
        }
        assert_eq!(l.announce(ip, ann(999, "x"), t0).unwrap_err().0, 429);
        assert_eq!(l.announce(ip, ann(0, "x"), t0 + TTL * 2).unwrap_err().0, 400);
    }
}
