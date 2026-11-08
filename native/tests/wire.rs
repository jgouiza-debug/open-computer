use serde_json::{Value, json};
use std::io::Write;
use std::process::{Command, Stdio};

fn exchange(lines: &str) -> Vec<Value> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_opencomputer-native"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn owned reader");
    child
        .stdin
        .take()
        .expect("stdin")
        .write_all(lines.as_bytes())
        .expect("send");
    let output = child.wait_with_output().expect("join");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("UTF8")
        .lines()
        .map(|line| serde_json::from_str(line).expect("JSONL only"))
        .collect()
}
#[test]
fn readonly_handshake_and_rejected_input() {
    let output = exchange(&format!(
        "{}\n{}\n",
        json!({"v":1,"id":"a","method":"hello","params":{}}),
        json!({"v":1,"id":"b","method":"click","params":{}})
    ));
    assert_eq!(output.len(), 2);
    assert_eq!(output[0]["result"]["read_only"], true);
    assert_eq!(output[0]["result"]["supports_input"], false);
    assert_eq!(output[0]["result"]["supports_arm"], false);
    assert_eq!(output[1]["error"]["code"], "unknown_method");
    assert_eq!(output[1]["id"], "b");
}
#[test]
fn invalid_handles_and_duplicate_keys_fail_without_observing_desktop() {
    let output = exchange(
        "{\"v\":1,\"id\":\"a\",\"method\":\"capture_window\",\"params\":{\"window_id\":\"0\",\"expected_pid\":1}}\n{\"v\":1,\"id\":\"b\",\"method\":\"hello\",\"params\":{},\"params\":{}}\n",
    );
    assert_eq!(output[0]["error"]["code"], "invalid_params");
    assert_eq!(output[1]["error"]["code"], "invalid_request");
}
