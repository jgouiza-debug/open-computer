mod accessibility;
mod capture;
mod protocol;
mod window;
use protocol::{Error, MAX_REQUEST, MAX_RESPONSE, Result};
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};

fn dispatch(request: protocol::Request, capturer: &mut capture::Capturer) -> Result<Value> {
    match request.method.as_str() {
        "hello" => {
            protocol::empty_params(&request.params)?;
            Ok(
                json!({"engine":"opencomputer-native","version":env!("CARGO_PKG_VERSION"),"platform":"windows",
                "read_only":true,"supports_input":false,"supports_arm":false,
                "methods":["hello","list_windows","inspect_window","capture_window","inspect_accessibility"],
                "limits":{"max_request_bytes":MAX_REQUEST,"max_response_bytes":MAX_RESPONSE,
                    "max_capture_pixels":protocol::MAX_PIXELS,"max_windows":512,"max_title_chars":2048,
                    "max_accessibility_nodes":256,"max_accessibility_depth":12,"max_accessibility_text_chars":8192}}),
            )
        }
        "list_windows" => {
            protocol::empty_params(&request.params)?;
            window::list()
        }
        "inspect_window" => {
            serde_json::to_value(window::inspect(&protocol::target(request.params)?)?)
                .map_err(|_| Error::new("internal_error", "Cannot serialize window"))
        }
        "capture_window" => capturer.capture(&protocol::target(request.params)?),
        "inspect_accessibility" => accessibility::inspect(&protocol::target(request.params)?),
        _ => Err(Error::new("unknown_method", "Unknown read-only method")),
    }
}
// Drain overflow without allocating it, preserving the next request boundary.
fn read_line(reader: &mut impl BufRead) -> io::Result<Option<std::result::Result<Vec<u8>, Error>>> {
    let mut bytes = Vec::new();
    let mut oversized = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if bytes.is_empty() && !oversized {
                Ok(None)
            } else {
                Ok(Some(Err(Error::new(
                    "invalid_request",
                    "Request must end in newline",
                ))))
            };
        }
        let newline = available.iter().position(|&b| b == b'\n');
        let count = newline.map_or(available.len(), |i| i + 1);
        if !oversized {
            if bytes.len().saturating_add(count) > MAX_REQUEST {
                oversized = true;
                bytes.clear();
            } else {
                bytes.extend_from_slice(&available[..count]);
            }
        }
        reader.consume(count);
        if newline.is_some() {
            if oversized {
                return Ok(Some(Err(Error::new(
                    "resource_limit",
                    "Request exceeds byte limit",
                ))));
            }
            bytes.pop();
            if bytes.last() == Some(&b'\r') {
                bytes.pop();
            }
            return Ok(Some(Ok(bytes)));
        }
    }
}
fn response_bytes(response: &Value) -> io::Result<Vec<u8>> {
    let mut bytes = serde_json::to_vec(response)?;
    if bytes.len().saturating_add(1) > MAX_RESPONSE {
        bytes = serde_json::to_vec(&protocol::response(
            response.get("id").and_then(Value::as_str),
            Err(Error::new("resource_limit", "Response exceeds byte limit")),
        ))?;
    }
    bytes.push(b'\n');
    Ok(bytes)
}
fn run() -> io::Result<()> {
    unsafe {
        windows::Win32::UI::HiDpi::SetProcessDpiAwarenessContext(
            windows::Win32::UI::HiDpi::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        )
        .map_err(|_| io::Error::other("Cannot establish physical-pixel DPI context"))?;
    }
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    let mut capturer = capture::Capturer::default();
    while let Some(line) = read_line(&mut input)? {
        let parsed = line.and_then(|bytes| protocol::parse(&bytes));
        let response = match parsed {
            Ok(request) => {
                let id = request.id.clone();
                protocol::response(Some(&id), dispatch(request, &mut capturer))
            }
            Err(error) => protocol::response(None, Err(error)),
        };
        let bytes = response_bytes(&response)?;
        output.write_all(&bytes)?;
        output.flush()?;
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("Native reader stopped: {error}");
        std::process::exit(1);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn oversize_is_drained_before_next_request() {
        let mut data = vec![b'x'; MAX_REQUEST + 10];
        data.extend_from_slice(b"\n{}\n");
        let mut reader = io::Cursor::new(data);
        assert!(read_line(&mut reader).expect("io").expect("line").is_err());
        assert_eq!(
            read_line(&mut reader)
                .expect("io")
                .expect("line")
                .expect("valid"),
            b"{}"
        );
    }
    #[test]
    fn eof_without_newline_is_rejected() {
        assert!(
            read_line(&mut io::Cursor::new(b"{}"))
                .expect("io")
                .expect("line")
                .is_err()
        );
    }
    #[test]
    fn request_wire_limit_includes_lf_and_crlf() {
        for ending in [b"\n".as_slice(), b"\r\n".as_slice()] {
            let mut exact = vec![b' '; MAX_REQUEST - ending.len()];
            exact.extend_from_slice(ending);
            assert!(
                read_line(&mut io::Cursor::new(&exact))
                    .expect("io")
                    .expect("line")
                    .is_ok()
            );
            exact.insert(0, b' ');
            assert_eq!(
                read_line(&mut io::Cursor::new(exact))
                    .expect("io")
                    .expect("line")
                    .expect_err("over limit")
                    .code,
                "resource_limit"
            );
        }
    }
    #[test]
    fn reply_wire_limit_includes_lf() {
        let exact = Value::String("x".repeat(MAX_RESPONSE - 3));
        assert_eq!(response_bytes(&exact).expect("exact").len(), MAX_RESPONSE);
        let oversized = Value::String("x".repeat(MAX_RESPONSE - 2));
        let reply = response_bytes(&oversized).expect("limit error");
        assert!(reply.len() < MAX_RESPONSE);
        assert_eq!(
            serde_json::from_slice::<Value>(&reply).expect("JSON")["error"]["code"],
            "resource_limit"
        );
    }
}
