//! Live air traffic (server/airtraffic.py; the mod's beamngEarth/airtraffic.lua): an ADS-B network's answer, the
//! ADSBexchange v2 JSON that adsb.lol, airplanes.live and adsb.fi all serve, as the compact rows the mod reads.
//! Based on devo343's Live Air Traffic add-on (its companion's `compact`), with permission.
//!
//!     {"ac": [{"hex": "a1b2c3", "flight": "DAL1234 ", "t": "A321", "r": "N123DN", "category": "A3",
//!              "lat": 33.6, "lon": -84.4, "alt_baro": 3400 | "ground", "alt_geom": 3550, "gs": 180.2,
//!              "track": 270.1, "track_rate": -0.5, "baro_rate": -704, "geom_rate": -690,
//!              "true_heading": 268.0, "mag_heading": 273.4, "nav_altitude_mcp": 3000, "seen_pos": 1.2, ...}],
//!      "now": 1764200000123, ...}
//!
//! A row: {h, f, t, r, c, lat, lon, g, ts, ab?, ag?, gs?, trk?, tr?, vs?, vsg?, hdg?, mhdg?, sel?}; ts is the
//! position's own time (UTC ms: the answer's "now" less seen_pos), so every game that reads the same answer predicts
//! the same aircraft to the same place whatever the delay (BeamMP players side by side). Positions older than
//! MAX_POS_AGE_S, outside max_km of the player or without a valid hex / lat / lon are left out; at most max_n rows,
//! the nearest first (ties by hex: the same answer gives the same rows). Text fields are cut to a short printable set
//! (they are drawn in the world and on the map). No I/O; the caller caps the body (netfetch max_bytes).
//!
//! adsbdb_route: one api.adsbdb.com /v0/callsign answer as (origin code, destination code, destination town) or None.

use crate::dm::Dm;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use serde_json::Value;

pub const MAX_POS_AGE_S: f64 = 60.0; // ADSBexchange: a position older than this is no longer valid
pub const MAX_BODY: usize = 8 << 20; // a second guard after netfetch's max_bytes
const R_EARTH_KM: f64 = 6371.0088;

#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub h: String,
    pub f: String,
    pub t: String,
    pub r: String,
    pub c: String,
    pub lat: f64,
    pub lon: f64,
    pub g: bool,
    pub ts: i64,
    pub km: f64,
    /// (key, value) of the optional numbers present, in a fixed order
    pub nums: Vec<(&'static str, f64)>,
}

fn num(v: Option<&Value>) -> Option<f64> {
    let x = match v? {
        Value::Number(n) => n.as_f64()?,
        Value::String(s) => s.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    if x.is_finite() {
        Some(x)
    } else {
        None
    }
}

/// A text field cut to what is safe and useful to draw: ASCII letters, digits, '-' and '.', at most max chars
/// (callsigns are padded with spaces: trimmed; letters upper-cased where asked).
fn clean(v: Option<&Value>, max: usize, upper: bool) -> String {
    let s = match v {
        Some(Value::String(s)) => s.as_str(),
        _ => return String::new(),
    };
    let mut out = String::new();
    for ch in s.trim().chars() {
        if out.len() >= max {
            break;
        }
        if ch.is_ascii_alphanumeric() || ch == '-' || ch == '.' {
            out.push(if upper { ch.to_ascii_uppercase() } else { ch });
        }
    }
    out
}

fn hex_of(v: Option<&Value>) -> String {
    let s = match v {
        Some(Value::String(s)) => s.trim(),
        _ => return String::new(),
    };
    // ICAO 24-bit addresses: 6 hex digits; '~' marks a non-ICAO (TIS-B) address
    if s.is_empty() || s.len() > 8 || !s.chars().all(|c| c.is_ascii_hexdigit() || c == '~') {
        return String::new();
    }
    s.to_ascii_lowercase()
}

/// Great-circle distance in km (haversine, spherical Earth: plenty for "within range" and "nearest first").
pub fn dist_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (p1, p2) = (lat1.to_radians(), lat2.to_radians());
    let dp = p2 - p1;
    let dl = (lon2 - lon1).to_radians();
    let (s1, s2) = ((dp / 2.0).dsin(), (dl / 2.0).dsin());
    let a = s1 * s1 + p1.dcos() * p2.dcos() * s2 * s2;
    2.0 * R_EARTH_KM * a.sqrt().min(1.0).dasin()
}

fn round_to(x: f64, nd: i32) -> f64 {
    let k = 10f64.powi(nd);
    (x * k).round() / k
}

/// The answer's own clock (UTC ms): "now" in ms (adsb.lol, airplanes.live) or s (some mirrors), else fallback.
fn answer_now_ms(doc: &Value, fallback_ms: f64) -> f64 {
    match num(doc.get("now")) {
        Some(n) if n > 1e11 => n,
        Some(n) if n > 1e8 => n * 1000.0,
        _ => fallback_ms,
    }
}

/// The rows of one answer (see the module doc): (the answer's clock in UTC ms, rows nearest first, aircraft read).
pub fn compact(
    body: &[u8],
    lat: f64,
    lon: f64,
    max_km: f64,
    max_n: usize,
    fallback_now_ms: f64,
) -> Result<(f64, Vec<Row>, usize), String> {
    if body.len() > MAX_BODY {
        return Err(format!("answer too large ({} bytes)", body.len()));
    }
    if !(lat.is_finite() && lon.is_finite() && (-90.0..=90.0).contains(&lat) && (-180.0..=180.0).contains(&lon)) {
        return Err("bad player position".into());
    }
    let doc: Value = serde_json::from_slice(body).map_err(|e| format!("not JSON: {e}"))?;
    let list = doc
        .get("ac")
        .or_else(|| doc.get("aircraft"))
        .and_then(|v| v.as_array());
    let list = match list {
        Some(l) => l,
        None => {
            // adsb.lol answers {"ac": null} or leaves it out when nothing is in range
            if doc.is_object() && doc.get("ac").map_or(true, |v| v.is_null()) {
                return Ok((answer_now_ms(&doc, fallback_now_ms), Vec::new(), 0));
            }
            return Err("no aircraft list in the answer".into());
        }
    };
    let now = answer_now_ms(&doc, fallback_now_ms);
    let mut rows = Vec::new();
    for a in list {
        let a = match a.as_object() {
            Some(o) => o,
            None => continue,
        };
        let (la, lo) = match (num(a.get("lat")), num(a.get("lon"))) {
            (Some(la), Some(lo)) if (-90.0..=90.0).contains(&la) && (-180.0..=180.0).contains(&lo) => (la, lo),
            _ => continue,
        };
        let age = num(a.get("seen_pos")).or_else(|| num(a.get("seen"))).unwrap_or(0.0).max(0.0);
        if age > MAX_POS_AGE_S {
            continue;
        }
        let h = hex_of(a.get("hex"));
        if h.is_empty() {
            continue;
        }
        let km = dist_km(lat, lon, la, lo);
        if !(km <= max_km) {
            continue;
        }
        let ground = matches!(a.get("alt_baro"), Some(Value::String(s)) if s == "ground")
            || matches!(a.get("ground"), Some(Value::Bool(true)));
        let mut nums = Vec::new();
        if let Some(v) = num(a.get("alt_baro")) {
            nums.push(("ab", v.round()));
        }
        if let Some(v) = num(a.get("alt_geom")) {
            nums.push(("ag", v.round()));
        }
        for (src, key, nd) in [
            ("gs", "gs", 1),
            ("track", "trk", 1),
            ("track_rate", "tr", 2),
            ("baro_rate", "vs", 0),
            ("geom_rate", "vsg", 0),
            ("true_heading", "hdg", 1),
            ("mag_heading", "mhdg", 1),
            ("nav_altitude_mcp", "sel", 0),
        ] {
            if let Some(v) = num(a.get(src)) {
                nums.push((key, round_to(v, nd)));
            }
        }
        rows.push(Row {
            h,
            f: clean(a.get("flight"), 10, true),
            t: clean(a.get("t"), 6, true),
            r: clean(a.get("r"), 12, true),
            c: clean(a.get("category"), 2, true),
            lat: round_to(la, 6),
            lon: round_to(lo, 6),
            g: ground,
            ts: (now - age * 1000.0).round() as i64,
            km,
            nums,
        });
    }
    let read = list.len();
    rows.sort_by(|a, b| a.km.total_cmp(&b.km).then_with(|| a.h.cmp(&b.h)));
    rows.truncate(max_n);
    Ok((now, rows, read))
}

fn code(v: Option<&Value>) -> String {
    clean(v, 4, true)
}

/// One adsbdb callsign answer: (origin IATA or ICAO code, destination code, destination town or airport name).
pub fn route(body: &[u8]) -> Option<(String, String, String)> {
    if body.len() > MAX_BODY {
        return None;
    }
    let doc: Value = serde_json::from_slice(body).ok()?;
    let fr = doc.get("response")?.get("flightroute")?;
    let (o, d) = (fr.get("origin")?, fr.get("destination")?);
    let pick = |ap: &Value| {
        let c = code(ap.get("iata_code"));
        if c.is_empty() {
            code(ap.get("icao_code"))
        } else {
            c
        }
    };
    let (oc, dc) = (pick(o), pick(d));
    if oc.is_empty() || dc.is_empty() {
        return None;
    }
    // the town: letters, spaces and a few marks only, 40 chars (shown in the identify line)
    let town = d
        .get("municipality")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .or_else(|| d.get("name").and_then(|v| v.as_str()))
        .unwrap_or("");
    let mut t = String::new();
    for ch in town.trim().chars() {
        if t.chars().count() >= 40 {
            break;
        }
        if ch.is_alphanumeric() || " -'.,()/".contains(ch) {
            t.push(ch);
        }
    }
    Some((oc, dc, t))
}

#[pyfunction]
pub fn adsb_compact<'py>(
    py: Python<'py>,
    body: &[u8],
    lat: f64,
    lon: f64,
    max_km: f64,
    max_n: usize,
    now_ms: f64,
) -> PyResult<(f64, Bound<'py, PyList>, usize)> {
    let (now, rows, read) =
        py.detach(|| compact(body, lat, lon, max_km, max_n, now_ms)).map_err(PyValueError::new_err)?;
    let out = PyList::empty(py);
    for r in rows {
        let d = PyDict::new(py);
        d.set_item("h", r.h)?;
        d.set_item("f", r.f)?;
        d.set_item("t", r.t)?;
        d.set_item("r", r.r)?;
        d.set_item("c", r.c)?;
        d.set_item("lat", r.lat)?;
        d.set_item("lon", r.lon)?;
        d.set_item("g", r.g)?;
        d.set_item("ts", r.ts)?;
        for (k, v) in r.nums {
            if v.fract() == 0.0 && v.abs() < 1e15 {
                d.set_item(k, v as i64)?;
            } else {
                d.set_item(k, v)?;
            }
        }
        out.append(d)?;
    }
    Ok((now, out, read))
}

#[pyfunction]
pub fn adsbdb_route(body: &[u8]) -> Option<(String, String, String)> {
    route(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: f64 = 1_764_200_000_000.0;

    fn body(ac: &str) -> Vec<u8> {
        format!("{{\"ac\":[{ac}],\"now\":{NOW},\"msg\":\"No error\"}}").into_bytes()
    }

    #[test]
    fn a_row_has_the_fields_and_an_absolute_time() {
        let b = body(
            r#"{"hex":"A1B2C3","flight":"DAL1234 ","t":"a321","r":"N123DN","category":"A3","lat":33.64,"lon":-84.43,
                "alt_baro":3400,"alt_geom":3550,"gs":180.25,"track":270.1,"track_rate":-0.5,"baro_rate":-704,
                "geom_rate":-690,"nav_altitude_mcp":3000,"seen_pos":1.5,"squawk":"1234"}"#,
        );
        let (now, rows, read) = compact(&b, 33.6, -84.4, 200.0, 400, 0.0).unwrap();
        assert_eq!((now, read, rows.len()), (NOW, 1, 1));
        let r = &rows[0];
        assert_eq!((r.h.as_str(), r.f.as_str(), r.t.as_str(), r.r.as_str(), r.c.as_str()),
                   ("a1b2c3", "DAL1234", "A321", "N123DN", "A3"));
        assert_eq!(r.ts, (NOW - 1500.0) as i64);
        assert!(!r.g);
        let keys: Vec<&str> = r.nums.iter().map(|x| x.0).collect();
        assert_eq!(keys, ["ab", "ag", "gs", "trk", "tr", "vs", "vsg", "sel"]);
        assert_eq!(r.nums[2].1, 180.3); // gs to 0.1 (180.25 rounds away from zero)
    }

    #[test]
    fn stale_far_and_bad_rows_are_left_out() {
        let b = body(
            r#"{"hex":"aaaaaa","lat":33.6,"lon":-84.4,"seen_pos":61},
               {"hex":"bbbbbb","lat":40.0,"lon":-74.0,"seen_pos":1},
               {"hex":"","lat":33.6,"lon":-84.4},
               {"hex":"cc cc","lat":33.6,"lon":-84.4},
               {"hex":"dddddd","lat":95,"lon":-84.4},
               {"hex":"eeeeee","lon":-84.4},
               "junk",
               {"hex":"ffffff","lat":33.61,"lon":-84.41,"alt_baro":"ground","seen":3}"#,
        );
        let (_, rows, read) = compact(&b, 33.6, -84.4, 200.0, 400, 0.0).unwrap();
        assert_eq!(read, 8);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].h, "ffffff");
        assert!(rows[0].g);
        assert_eq!(rows[0].ts, (NOW - 3000.0) as i64);
    }

    #[test]
    fn nearest_first_capped_ties_by_hex() {
        let b = body(
            r#"{"hex":"000003","lat":33.9,"lon":-84.4},{"hex":"000002","lat":33.7,"lon":-84.4},
               {"hex":"000001","lat":33.7,"lon":-84.4},{"hex":"000004","lat":33.61,"lon":-84.4}"#,
        );
        let (_, rows, _) = compact(&b, 33.6, -84.4, 200.0, 3, 0.0).unwrap();
        let hs: Vec<&str> = rows.iter().map(|r| r.h.as_str()).collect();
        assert_eq!(hs, ["000004", "000001", "000002"]);
    }

    #[test]
    fn text_is_cut_to_a_safe_set() {
        let b = body(r#"{"hex":"abcdef","flight":"<b>X\"Y</b>1234567890","r":"N1 2;3","lat":0,"lon":0}"#);
        let (_, rows, _) = compact(&b, 0.0, 0.0, 10.0, 10, 0.0).unwrap();
        assert_eq!(rows[0].f, "BXYB123456");
        assert_eq!(rows[0].r, "N123");
    }

    #[test]
    fn empty_answers_and_clocks() {
        let (now, rows, read) = compact(br#"{"ac":null,"now":1764200000}"#, 0.0, 0.0, 10.0, 10, 5.0).unwrap();
        assert_eq!((now, rows.len(), read), (1_764_200_000_000.0, 0, 0));
        let (now, _, _) = compact(br#"{"ac":[]}"#, 0.0, 0.0, 10.0, 10, 5.0).unwrap();
        assert_eq!(now, 5.0);
        assert!(compact(b"<html>", 0.0, 0.0, 10.0, 10, 5.0).is_err());
        assert!(compact(br#"{"ac":[]}"#, f64::NAN, 0.0, 10.0, 10, 5.0).is_err());
        assert!(compact(b"[1,2]", 0.0, 0.0, 10.0, 10, 5.0).is_err());
    }

    #[test]
    fn routes() {
        let b = br#"{"response":{"flightroute":{"callsign":"DAL1234",
            "origin":{"iata_code":"ATL","icao_code":"KATL","municipality":"Atlanta"},
            "destination":{"iata_code":"","icao_code":"KJFK","municipality":"New York <script>","name":"JFK"}}}}"#;
        assert_eq!(route(b), Some(("ATL".into(), "KJFK".into(), "New York script".into())));
        assert_eq!(route(br#"{"response":"unknown callsign"}"#), None);
        assert_eq!(route(b"nope"), None);
    }

    #[test]
    fn distance() {
        let d = dist_km(51.4700, -0.4543, 40.6413, -73.7781); // LHR - JFK
        assert!((d - 5540.0).abs() < 15.0, "{d}");
    }
}
