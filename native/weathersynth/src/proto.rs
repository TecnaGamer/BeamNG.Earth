//! The live-serve line protocol: one JSON object per stdin line, the mod's 'weather_audio' as server/liveaudio.py
//! passes it on (server/protocol_table.json). A small JSON reader (no serde: the crate stays libm + cpal), strict
//! enough for our own lines, with limits so no input can make it recurse deeply or allocate without bound.

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

const MAX_DEPTH: usize = 8;
pub const MAX_LINE: usize = 64 * 1024;

impl Json {
    pub fn get(&self, k: &str) -> Option<&Json> {
        match self {
            Json::Obj(v) => v.iter().find(|(n, _)| n == k).map(|(_, j)| j),
            _ => None,
        }
    }
    pub fn num(&self, k: &str) -> Option<f64> {
        match self.get(k) {
            Some(Json::Num(n)) if n.is_finite() => Some(*n),
            _ => None,
        }
    }
    pub fn bool(&self, k: &str) -> Option<bool> {
        match self.get(k) {
            Some(Json::Bool(b)) => Some(*b),
            _ => None,
        }
    }
    pub fn str(&self, k: &str) -> Option<&str> {
        match self.get(k) {
            Some(Json::Str(s)) => Some(s),
            _ => None,
        }
    }
}

pub fn parse(s: &str) -> Result<Json, String> {
    parse_limit(s, MAX_LINE)
}

/// A whole JSON document of at most `max` bytes (the grain bank's manifest; the same depth limit).
pub fn parse_limit(s: &str, max: usize) -> Result<Json, String> {
    if s.len() > max {
        return Err("line too long".into());
    }
    let b = s.as_bytes();
    let mut i = 0;
    let v = value(b, &mut i, 0)?;
    ws(b, &mut i);
    if i != b.len() {
        return Err(format!("trailing data at {i}"));
    }
    Ok(v)
}

fn ws(b: &[u8], i: &mut usize) {
    while *i < b.len() && matches!(b[*i], b' ' | b'\t' | b'\r' | b'\n') {
        *i += 1;
    }
}

fn lit(b: &[u8], i: &mut usize, word: &str, v: Json) -> Result<Json, String> {
    if b[*i..].starts_with(word.as_bytes()) {
        *i += word.len();
        Ok(v)
    } else {
        Err(format!("bad literal at {i}"))
    }
}

fn value(b: &[u8], i: &mut usize, depth: usize) -> Result<Json, String> {
    if depth > MAX_DEPTH {
        return Err("nested too deep".into());
    }
    ws(b, i);
    match b.get(*i) {
        None => Err("unexpected end".into()),
        Some(b'n') => lit(b, i, "null", Json::Null),
        Some(b't') => lit(b, i, "true", Json::Bool(true)),
        Some(b'f') => lit(b, i, "false", Json::Bool(false)),
        Some(b'"') => Ok(Json::Str(string(b, i)?)),
        Some(b'[') => {
            *i += 1;
            let mut v = Vec::new();
            ws(b, i);
            if b.get(*i) == Some(&b']') {
                *i += 1;
                return Ok(Json::Arr(v));
            }
            loop {
                v.push(value(b, i, depth + 1)?);
                ws(b, i);
                match b.get(*i) {
                    Some(b',') => *i += 1,
                    Some(b']') => {
                        *i += 1;
                        return Ok(Json::Arr(v));
                    }
                    _ => return Err(format!("expected , or ] at {i}")),
                }
            }
        }
        Some(b'{') => {
            *i += 1;
            let mut v = Vec::new();
            ws(b, i);
            if b.get(*i) == Some(&b'}') {
                *i += 1;
                return Ok(Json::Obj(v));
            }
            loop {
                ws(b, i);
                if b.get(*i) != Some(&b'"') {
                    return Err(format!("expected a key at {i}"));
                }
                let k = string(b, i)?;
                ws(b, i);
                if b.get(*i) != Some(&b':') {
                    return Err(format!("expected : at {i}"));
                }
                *i += 1;
                let val = value(b, i, depth + 1)?;
                v.push((k, val));
                ws(b, i);
                match b.get(*i) {
                    Some(b',') => *i += 1,
                    Some(b'}') => {
                        *i += 1;
                        return Ok(Json::Obj(v));
                    }
                    _ => return Err(format!("expected , or }} at {i}")),
                }
            }
        }
        Some(_) => number(b, i),
    }
}

fn string(b: &[u8], i: &mut usize) -> Result<String, String> {
    *i += 1; // the opening quote
    let mut out: Vec<u8> = Vec::new();
    while let Some(&c) = b.get(*i) {
        *i += 1;
        match c {
            b'"' => return String::from_utf8(out).map_err(|_| "bad utf-8".into()),
            b'\\' => {
                let e = *b.get(*i).ok_or("bad escape")?;
                *i += 1;
                match e {
                    b'"' => out.push(b'"'),
                    b'\\' => out.push(b'\\'),
                    b'/' => out.push(b'/'),
                    b'b' => out.push(8),
                    b'f' => out.push(12),
                    b'n' => out.push(b'\n'),
                    b'r' => out.push(b'\r'),
                    b't' => out.push(b'\t'),
                    b'u' => {
                        let h = b.get(*i..*i + 4).ok_or("bad \\u")?;
                        let cp = u32::from_str_radix(std::str::from_utf8(h).map_err(|_| "bad \\u")?, 16)
                            .map_err(|_| "bad \\u")?;
                        *i += 4;
                        // (surrogate pairs are not needed by our lines: a lone one becomes U+FFFD)
                        let ch = char::from_u32(cp).unwrap_or('\u{FFFD}');
                        let mut buf = [0u8; 4];
                        out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                    }
                    _ => return Err("bad escape".into()),
                }
            }
            c if c < 0x20 => return Err("control character in a string".into()),
            c => out.push(c),
        }
    }
    Err("unterminated string".into())
}

fn number(b: &[u8], i: &mut usize) -> Result<Json, String> {
    let s = *i;
    while *i < b.len() && matches!(b[*i], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9') {
        *i += 1;
    }
    let t = std::str::from_utf8(&b[s..*i]).map_err(|_| "bad number")?;
    t.parse::<f64>().map(Json::Num).map_err(|_| format!("bad number {t:?} at {s}"))
}

/// The thunder kinds (storm.lua thunderFor), the renderer's distance class.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Strike,
    Close,
    Mid,
    Far,
}

impl Kind {
    pub fn from_str(s: &str) -> Option<Kind> {
        match s {
            "strike" => Some(Kind::Strike),
            "close" => Some(Kind::Close),
            "mid" => Some(Kind::Mid),
            "far" => Some(Kind::Far),
            _ => None,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Kind::Strike => "strike",
            Kind::Close => "close",
            Kind::Mid => "mid",
            Kind::Far => "far",
        }
    }
}

/// One thunder of a message (liveaudio.lua M.thunder).
#[derive(Clone, Debug, PartialEq)]
pub struct Thunder {
    pub id: String,
    /// UTC s the sound reaches the listener
    pub at: f64,
    pub d: f64,
    pub kind: Kind,
    pub gain: f64,
    pub seed: u32,
    /// false: a look-ahead hint (render it early); true: it struck (play it)
    pub fin: bool,
    /// the message's thunderSet is 'recorded' (the grain bank); else 'generated'
    pub rec: bool,
    /// (a final) the synth plays it: true when the mod saw it ready (readyThunders) or for a set the synth plays
    /// whole; false: the game plays its file, the synth only ducks its rain at `at` (the file's crack). Absent: true.
    pub play: bool,
}

/// A 'weather_audio' line, made safe (clamped; unknown fields ignored; a bad thunder skipped).
#[derive(Clone, Debug, PartialEq)]
pub struct Msg {
    pub live: bool,
    pub rain_mm_h: f64,
    pub wind_ms: f64,
    pub shelter: f64,
    pub tunnel: bool,
    pub cabin: bool,
    pub volume: f64,
    pub thunder_volume: f64,
    pub muted: bool,
    pub duck: f64,
    pub t: f64,
    pub thunders: Vec<Thunder>,
}

pub const MAX_THUNDERS: usize = 32;

pub fn msg(line: &str) -> Result<Msg, String> {
    let j = parse(line)?;
    if !matches!(j, Json::Obj(_)) {
        return Err("not an object".into());
    }
    let c01 = |k: &str, d: f64| j.num(k).unwrap_or(d).clamp(0.0, 1.0);
    let rec = j.str("thunderSet") == Some("recorded");
    let mut thunders = Vec::new();
    if let Some(Json::Arr(a)) = j.get("thunders") {
        for t in a.iter().take(MAX_THUNDERS) {
            let (Some(id), Some(at), Some(kind)) = (t.str("id"), t.num("at"), t.str("kind").and_then(Kind::from_str))
            else {
                continue;
            };
            if id.len() > 64 {
                continue;
            }
            thunders.push(Thunder {
                id: id.to_string(),
                at,
                d: t.num("d").unwrap_or(0.0).clamp(0.0, 1e6),
                kind,
                gain: t.num("gain").unwrap_or(1.0).clamp(0.0, 1.0),
                seed: t.num("seed").unwrap_or(0.0).clamp(0.0, u32::MAX as f64) as u32,
                fin: t.bool("final").unwrap_or(false),
                rec,
                play: t.bool("play").unwrap_or(true),
            });
        }
    }
    Ok(Msg {
        live: j.str("mode") == Some("live"),
        rain_mm_h: j.num("rainMmH").unwrap_or(0.0).clamp(0.0, 500.0),
        wind_ms: j.num("windMs").unwrap_or(0.0).clamp(0.0, 100.0),
        shelter: c01("shelter", 1.0),
        tunnel: j.bool("tunnel").unwrap_or(false),
        cabin: j.bool("cabin").unwrap_or(false),
        volume: c01("volume", 1.0),
        thunder_volume: c01("thunderVolume", 1.0),
        muted: j.bool("muted").unwrap_or(false),
        duck: c01("duck", 1.0),
        t: j.num("t").unwrap_or(0.0),
        thunders,
    })
}

/// A JSON string literal (for the state lines).
pub fn quote(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_mods_line() {
        let l = r#"{"mode":"live","rainMmH":4.5,"windMs":3,"shelter":0.25,"tunnel":false,"cabin":true,"volume":0.6,"thunderVolume":2,"muted":false,"duck":0.35,"t":1764000000.25,"thunders":[{"id":"7@1.000","at":6.5,"d":1715,"kind":"close","gain":0.6,"seed":7,"final":true},{"id":"bad","kind":"nope","at":1},{"id":"8@2.000","at":3,"kind":"far"}],"extra":{"a":[1,2,{"b":null}]}}"#;
        let m = msg(l).unwrap();
        assert!(m.live && m.cabin && !m.tunnel && !m.muted);
        assert_eq!((m.rain_mm_h, m.shelter, m.volume, m.thunder_volume, m.duck), (4.5, 0.25, 0.6, 1.0, 0.35));
        assert_eq!(m.t, 1764000000.25);
        assert_eq!(m.thunders.len(), 2, "the bad kind skipped");
        assert_eq!(m.thunders[0], Thunder { id: "7@1.000".into(), at: 6.5, d: 1715.0, kind: Kind::Close, gain: 0.6, seed: 7, fin: true, rec: false, play: true });
        assert!(!msg(r#"{"mode":"live","thunders":[{"id":"a","at":1,"kind":"strike","final":true,"play":false}]}"#).unwrap().thunders[0].play,
            "play = false: the game plays its file");
        assert!(msg(r#"{"mode":"live","thunderSet":"recorded","thunders":[{"id":"a","at":1,"kind":"far"}]}"#).unwrap().thunders[0].rec);
        assert!(!m.thunders[1].fin && m.thunders[1].kind == Kind::Far && m.thunders[1].gain == 1.0);
    }

    #[test]
    fn prerecorded_and_defaults() {
        let m = msg(r#"{"mode":"prerecorded"}"#).unwrap();
        assert!(!m.live && m.thunders.is_empty() && m.shelter == 1.0 && m.volume == 1.0);
    }

    #[test]
    fn rejects_junk() {
        for bad in ["", "[1,2]", "{", "{\"a\":}", "{\"a\":1}x", "{\"a\":\"\\q\"}", "nul", "{\"a\":1,}", "\"abc"] {
            assert!(msg(bad).is_err(), "{bad:?}");
        }
        let deep = "[".repeat(100) + &"]".repeat(100);
        assert!(parse(&deep).is_err(), "depth limit");
        assert!(parse(&" ".repeat(MAX_LINE + 1)).is_err(), "length limit");
    }

    #[test]
    fn strings_round_trip() {
        let s = "a\"b\\c\nd\u{1}é";
        assert_eq!(parse(&quote(s)).unwrap(), Json::Str(s.into()));
        assert_eq!(parse(r#""\u00e9\/""#).unwrap(), Json::Str("é/".into()));
    }
}
