use crate::protocol::{Error, Result, Target};
use serde::Serialize;
use windows::Win32::Foundation::{HWND, LPARAM, POINT, RECT};
use windows::Win32::Graphics::Gdi::ClientToScreen;
use windows::Win32::System::Threading::{GetCurrentProcessId, GetCurrentThreadId};
use windows::Win32::UI::WindowsAndMessaging::*;
use windows::core::BOOL;

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Window {
    pub id: String,
    pub pid: u32,
    pub title: String,
    pub rect: Rect,
}
pub fn hwnd(target: &Target) -> Result<HWND> {
    Ok(HWND(target.handle()? as *mut _))
}

pub fn inspect(target: &Target) -> Result<Window> {
    let handle = hwnd(target)?;
    // SAFETY: HWND is untrusted but never dereferenced by Rust; Win32 validates it.
    unsafe {
        if !IsWindow(Some(handle)).as_bool()
            || !IsWindowVisible(handle).as_bool()
            || IsIconic(handle).as_bool()
        {
            return Err(Error::new(
                "not_found",
                "Window is absent, hidden, or minimized",
            ));
        }
        let mut pid = 0;
        let tid = GetWindowThreadProcessId(handle, Some(&mut pid));
        if pid != target.expected_pid {
            return Err(Error::new("pid_mismatch", "Target PID changed"));
        }
        let mut r = RECT::default();
        GetClientRect(handle, &mut r)
            .map_err(|_| Error::new("not_found", "Cannot inspect client area"))?;
        let mut p = POINT::default();
        if !ClientToScreen(handle, &mut p).as_bool() || r.right <= 0 || r.bottom <= 0 {
            return Err(Error::new(
                "not_found",
                "Window has no available client area",
            ));
        }
        let mut text = [0u16; 2049];
        // Same-process cross-thread GetWindowText sends WM_GETTEXT synchronously;
        // never create the GUI-lock inversion that the Python controller guards.
        let title = if pid == GetCurrentProcessId() && tid != GetCurrentThreadId() {
            "Owned window".into()
        } else {
            let n = GetWindowTextW(handle, &mut text).max(0) as usize;
            String::from_utf16_lossy(&text[..n])
                .chars()
                .take(2048)
                .collect()
        };
        let mut final_pid = 0;
        GetWindowThreadProcessId(handle, Some(&mut final_pid));
        if final_pid != pid {
            return Err(Error::new(
                "window_changed",
                "Window identity changed during inspection",
            ));
        }
        Ok(Window {
            id: target.window_id.clone(),
            pid,
            title,
            rect: Rect {
                x: p.x,
                y: p.y,
                width: r.right,
                height: r.bottom,
            },
        })
    }
}
pub fn same_target(before: &Window, after: &Window) -> bool {
    before.id == after.id && before.pid == after.pid && before.rect == after.rect
}
pub fn list() -> Result<serde_json::Value> {
    struct Enumeration {
        windows: Vec<Window>,
        truncated: bool,
    }
    unsafe extern "system" fn visit(handle: HWND, parameter: LPARAM) -> BOOL {
        // SAFETY: EnumWindows invokes this synchronously while Enumeration lives.
        let out = unsafe { &mut *(parameter.0 as *mut Enumeration) };
        let mut pid = 0;
        unsafe {
            GetWindowThreadProcessId(handle, Some(&mut pid));
        }
        let target = Target {
            window_id: (handle.0 as usize).to_string(),
            expected_pid: pid,
        };
        if let Ok(w) = inspect(&target) {
            if !w.title.is_empty() {
                if out.windows.len() == 512 {
                    out.truncated = true;
                    return BOOL(0);
                }
                out.windows.push(w);
            }
        }
        BOOL(1)
    }
    let mut out = Enumeration {
        windows: Vec::new(),
        truncated: false,
    };
    // SAFETY: callback and context are valid until synchronous enumeration returns.
    let result = unsafe { EnumWindows(Some(visit), LPARAM(&mut out as *mut _ as isize)) };
    if result.is_err() && !out.truncated {
        return Err(Error::new("internal_error", "Window enumeration failed"));
    }
    Ok(serde_json::json!({"windows":out.windows,"truncated":out.truncated}))
}
