//! 1b: the pose merge. While the Python lane is busy, the game's poses (2 Hz, more in a hurry) pile up in the batch
//! waiting for it, and Python would replay each in turn. Two poses of one connection with no other frame between
//! them and the same anchorId become one: the later pose as it came (its fields in its order, each value's text as
//! it was), with `slotsGone` the two lists joined in order. slotsGone is an event list (the pooled far-tile slots let
//! go since the last pose: core.lua slotsGoneNow()); every other field is a snapshot.
//!
//! Only poses are parsed (serde_json, borrowing each value's raw text). A frame that is not a JSON object, a pose
//! without an anchorId, or a slotsGone that is neither a list nor an object is never merged. An empty Lua table may
//! encode as {}; an object's values are its items in order (earthd's slots_gone reads it the same way).
use serde::de::{Deserializer, MapAccess, Visitor};
use serde_json::value::RawValue;
use std::fmt;

/// a frame's top-level members in their order, each value as its raw JSON text
struct Members<'a>(Vec<(String, &'a RawValue)>);

impl<'de> serde::Deserialize<'de> for Members<'de> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Members<'de>;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut m: A) -> Result<Self::Value, A::Error> {
                let mut out = Vec::new();
                while let Some((k, v)) = m.next_entry::<String, &'de RawValue>()? {
                    out.push((k, v));
                }
                Ok(Members(out))
            }
        }
        d.deserialize_map(V)
    }
}

/// the items of a raw slotsGone value: a list's items, an object's values (in order); None for anything else
fn items(v: &RawValue) -> Option<Vec<&RawValue>> {
    let t = v.get().trim_start();
    if t.starts_with('[') {
        serde_json::from_str::<Vec<&RawValue>>(v.get()).ok()
    } else if t.starts_with('{') {
        serde_json::from_str::<Members>(v.get()).ok().map(|m| m.0.into_iter().map(|(_, v)| v).collect())
    } else {
        None
    }
}

/// a quick test before parsing: could this frame be a pose? (every pose names its type)
pub fn maybe_pose(data: &[u8]) -> bool {
    data.windows(6).any(|w| w == b"\"pose\"")
}

/// A pose's parts: its members, and its anchorId's raw text (None: not a pose, or not mergeable).
fn pose(data: &[u8]) -> Option<(Members<'_>, String)> {
    let m: Members = serde_json::from_slice(data).ok()?;
    // (Python's json.loads keeps the last of a repeated key: so does this)
    let last = |key: &str| m.0.iter().rev().find(|(k, _)| k == key).map(|(_, v)| *v);
    if serde_json::from_str::<&str>(last("type")?.get()).ok()? != "pose" {
        return None;
    }
    let aid = last("anchorId")?.get().to_string();
    if let Some(g) = last("slotsGone") {
        items(g)?;
    }
    Some((m, aid))
}

/// The pose that replaces `a` then `b` (both a connection's poses, nothing between them), or None when they may not
/// merge: either is not a pose, their anchorIds differ (a rebase or teleport between them: each frame's position is
/// in its own anchor's frame), or a slotsGone is malformed.
pub fn merge(a: &[u8], b: &[u8]) -> Option<Vec<u8>> {
    let (ma, aid_a) = pose(a)?;
    let (mb, aid_b) = pose(b)?;
    // (the anchorIds compared as JSON values: 1 and 1.0 stay apart, the conservative side; the mod sends integers)
    let va: serde_json::Value = serde_json::from_str(&aid_a).ok()?;
    let vb: serde_json::Value = serde_json::from_str(&aid_b).ok()?;
    if va != vb || !(va.is_number() || va.is_string()) {
        return None;
    }
    let gone = |m: &Members| -> Vec<String> {
        m.0.iter()
            .rev()
            .find(|(k, _)| k == "slotsGone")
            .and_then(|(_, v)| items(v))
            .map(|v| v.into_iter().map(|r| r.get().to_string()).collect())
            .unwrap_or_default()
    };
    let (ga, gb) = (gone(&ma), gone(&mb));
    let joined = format!("[{}]", ga.into_iter().chain(gb).collect::<Vec<_>>().join(","));
    let has = mb.0.iter().any(|(k, _)| k == "slotsGone");
    let mut out = Vec::with_capacity(b.len() + joined.len());
    out.push(b'{');
    let mut first = true;
    let n = mb.0.len();
    for (i, (k, v)) in mb.0.iter().enumerate() {
        let is_last_gone = k == "slotsGone" && !mb.0[i + 1..n].iter().any(|(k2, _)| k2 == "slotsGone");
        if !first {
            out.push(b',');
        }
        first = false;
        out.extend_from_slice(serde_json::to_string(k).ok()?.as_bytes());
        out.push(b':');
        if is_last_gone {
            out.extend_from_slice(joined.as_bytes());
        } else {
            out.extend_from_slice(v.get().as_bytes());
        }
    }
    if !has && joined != "[]" {
        // (the later pose named none; the earlier one's go with it)
        if !first {
            out.push(b',');
        }
        out.extend_from_slice(b"\"slotsGone\":");
        out.extend_from_slice(joined.as_bytes());
    }
    out.push(b'}');
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    fn v(b: &[u8]) -> Value {
        serde_json::from_slice(b).unwrap()
    }

    #[test]
    fn later_pose_with_slots_joined() {
        let a = br#"{"type":"pose","anchorId":3,"x":1.5,"slotsGone":[[4,10],[5,11]],"cam":{"x":1}}"#;
        let b = br#"{"type":"pose","anchorId":3,"x":2.25,"slotsGone":[[6,12]],"cam":{"x":2},"follow":true}"#;
        let m = merge(a, b).unwrap();
        assert_eq!(
            std::str::from_utf8(&m).unwrap(),
            r#"{"type":"pose","anchorId":3,"x":2.25,"slotsGone":[[4,10],[5,11],[6,12]],"cam":{"x":2},"follow":true}"#
        );
    }

    #[test]
    fn values_keep_their_text_and_order() {
        // (a number's text, a string's escapes, whitespace inside values: as they came)
        let a = br#"{"type":"pose","anchorId":1,"slotsGone":{}}"#;
        let b = "{\"z\": 1.0e2, \"type\":\"pose\",\"anchorId\":1,\"name\":\"C\\u00f4te\",\"slotsGone\":{},\"held\": [ 1 , 2 ]}";
        let m = merge(a, b.as_bytes()).unwrap();
        assert_eq!(
            std::str::from_utf8(&m).unwrap(),
            r#"{"z":1.0e2,"type":"pose","anchorId":1,"name":"C\u00f4te","slotsGone":[],"held":[ 1 , 2 ]}"#
        );
        assert_eq!(v(&m)["name"], json!("C\u{f4}te"));
    }

    #[test]
    fn object_slots_are_their_values_in_order() {
        let a = br#"{"type":"pose","anchorId":2,"slotsGone":{"1":[7,1],"2":[8,2]}}"#;
        let b = br#"{"type":"pose","anchorId":2,"slotsGone":{"1":{"1":9,"2":3}}}"#;
        assert_eq!(v(&merge(a, b).unwrap())["slotsGone"], json!([[7, 1], [8, 2], {"1": 9, "2": 3}]));
    }

    #[test]
    fn earlier_slots_kept_when_the_later_pose_names_none() {
        let a = br#"{"type":"pose","anchorId":2,"slotsGone":[[1,2]]}"#;
        let b = br#"{"type":"pose","anchorId":2,"x":0}"#;
        assert_eq!(v(&merge(a, b).unwrap()), json!({"type": "pose", "anchorId": 2, "x": 0, "slotsGone": [[1, 2]]}));
        let c = br#"{"type":"pose","anchorId":2}"#;
        assert_eq!(merge(c, b).unwrap(), b.to_vec(), "nothing to join: the later pose as it is");
    }

    #[test]
    fn never_merged() {
        let p = |aid: &str| format!(r#"{{"type":"pose","anchorId":{aid},"x":1}}"#).into_bytes();
        assert!(merge(&p("1"), &p("2")).is_none(), "another anchor");
        assert!(merge(&p("1"), &p("1.0")).is_none(), "1 and 1.0: left apart (the conservative side)");
        assert!(merge(&p("null"), &p("null")).is_none(), "no anchor");
        assert!(merge(br#"{"type":"pose","x":1}"#, br#"{"type":"pose","x":2}"#).is_none(), "no anchorId");
        assert!(merge(br#"{"type":"tile_request","anchorId":1}"#, &p("1")).is_none(), "not a pose");
        assert!(merge(&p("1"), br#"{"type":"pose","anchorId":1,"slotsGone":5}"#).is_none(), "bad slotsGone");
        assert!(merge(b"[1,2]", &p("1")).is_none());
        assert!(merge(b"{not json", &p("1")).is_none());
    }

    #[test]
    fn a_repeated_key_counts_its_last_copy() {
        let a = br#"{"type":"tile_request","type":"pose","anchorId":4}"#;
        let b = br#"{"type":"pose","anchorId":9,"anchorId":4,"slotsGone":[[1,1]],"slotsGone":[[2,2]]}"#;
        let m = merge(a, b).unwrap();
        assert_eq!(v(&m)["slotsGone"], json!([[2, 2]]));
        assert_eq!(v(&m)["anchorId"], json!(4));
    }

    #[test]
    fn quick_test() {
        assert!(maybe_pose(br#"{"type":"pose"}"#));
        assert!(!maybe_pose(br#"{"type":"tile_request"}"#));
    }
}
