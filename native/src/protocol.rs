use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::fmt;

pub const MAX_REQUEST: usize = 16_384;
pub const MAX_RESPONSE: usize = 33_554_432;
pub const MAX_PIXELS: usize = 8_388_608;
pub const MAX_CACHE: usize = 33_554_432;

#[derive(Debug, Serialize)]
pub struct Error {
    pub code: &'static str,
    pub message: String,
    pub retryable: bool,
}
impl Error {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            retryable: false,
        }
    }
}
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub v: u32,
    pub id: String,
    pub method: String,
    pub params: Value,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub window_id: String,
    pub expected_pid: u32,
}
impl Target {
    pub fn handle(&self) -> Result<usize> {
        if self.expected_pid == 0
            || self.window_id.is_empty()
            || !self.window_id.bytes().all(|b| b.is_ascii_digit())
            || self.window_id.starts_with('0')
        {
            return Err(Error::new(
                "invalid_params",
                "Expected nonzero decimal window_id and PID",
            ));
        }
        self.window_id
            .parse()
            .map_err(|_| Error::new("invalid_params", "Invalid window_id"))
    }
}

// serde_json::Value normally silently accepts duplicate object keys. Reject them at
// every nesting level before typed dispatch so the wire has one interpretation.
struct Unique;
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("unique-key JSON")
            }
            fn visit_map<M: serde::de::MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Unique, M::Error> {
                let mut seen = HashSet::new();
                while let Some(key) = map.next_key::<String>()? {
                    if !seen.insert(key) {
                        return Err(serde::de::Error::custom("Duplicate key"));
                    }
                    map.next_value::<Unique>()?;
                }
                Ok(Unique)
            }
            fn visit_seq<S: serde::de::SeqAccess<'de>>(
                self,
                mut seq: S,
            ) -> std::result::Result<Unique, S::Error> {
                while seq.next_element::<Unique>()?.is_some() {}
                Ok(Unique)
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
            fn visit_string<E: serde::de::Error>(
                self,
                _: String,
            ) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
            fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<Unique, E> {
                Ok(Unique)
            }
        }
        deserializer.deserialize_any(Visitor)
    }
}

pub fn parse(line: &[u8]) -> Result<Request> {
    if line.len() >= MAX_REQUEST {
        return Err(Error::new("resource_limit", "Request exceeds byte limit"));
    }
    serde_json::from_slice::<Unique>(line)
        .map_err(|_| Error::new("invalid_request", "Malformed JSON or duplicate key"))?;
    let req: Request = serde_json::from_slice(line)
        .map_err(|_| Error::new("invalid_request", "Invalid request envelope"))?;
    if req.v != 1
        || req.id.is_empty()
        || req.id.len() > 64
        || !req.id.bytes().all(|b| (32..=126).contains(&b))
    {
        return Err(Error::new(
            "invalid_request",
            "Invalid version or request ID",
        ));
    }
    Ok(req)
}
pub fn empty_params(value: &Value) -> Result<()> {
    if value.as_object().is_some_and(|m| m.is_empty()) {
        Ok(())
    } else {
        Err(Error::new(
            "invalid_params",
            "This method accepts only empty params",
        ))
    }
}
pub fn target(value: Value) -> Result<Target> {
    let t: Target = serde_json::from_value(value)
        .map_err(|_| Error::new("invalid_params", "Expected window_id and expected_pid only"))?;
    t.handle()?;
    Ok(t)
}
pub fn response(id: Option<&str>, result: Result<Value>) -> Value {
    match result {
        Ok(value) => json!({"v":1,"id":id,"ok":true,"result":value}),
        Err(error) => json!({"v":1,"id":id,"ok":false,"error":error}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_duplicate_nested_keys() {
        assert!(parse(br#"{"v":1,"id":"a","method":"hello","params":{"x":1,"x":2}}"#).is_err());
    }
    #[test]
    fn rejects_unknown_envelope_fields() {
        assert!(parse(br#"{"v":1,"id":"a","method":"hello","params":{},"other":1}"#).is_err());
    }
    #[test]
    fn validates_id_and_target() {
        for id in ["", "é", "a\n"] {
            assert!(
                parse(
                    json!({"v":1,"id":id,"method":"hello","params":{}})
                        .to_string()
                        .as_bytes()
                )
                .is_err()
            );
        }
        for value in [
            json!({"window_id":"01","expected_pid":1}),
            json!({"window_id":"1","expected_pid":0}),
            json!({"window_id":"-1","expected_pid":1}),
            json!({"window_id":"1","expected_pid":1,"x":2}),
        ] {
            assert!(target(value).is_err());
        }
        assert!(target(json!({"window_id":"123","expected_pid":1})).is_ok());
    }
}
