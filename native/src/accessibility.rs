use crate::protocol::{Error, Result, Target};
use crate::window::{self, Rect, Window};
use serde::Serialize;
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};
use windows::Win32::Foundation::RECT;
use windows::Win32::System::Com::*;
use windows::Win32::System::Variant::{VARIANT, VT_BOOL, VariantClear};
use windows::Win32::UI::Accessibility::*;
use windows::core::Interface;

struct Apartment;
impl Apartment {
    fn new() -> Result<Self> {
        // SAFETY: this reader owns its thread and matches COM cleanup with RAII.
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
            .ok()
            .map_err(|error| provider_error("com.initialize", error))?;
        Ok(Self)
    }
}
impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe {
            CoUninitialize();
        }
    }
}
struct Variant(VARIANT);
impl Drop for Variant {
    fn drop(&mut self) {
        unsafe {
            let _ = VariantClear(&mut self.0);
        }
    }
}
fn unavailable(stage: &'static str, reason: &'static str) -> Error {
    Error::new(
        "accessibility_unavailable",
        format!("Accessibility provider is unavailable; stage={stage}; reason={reason}"),
    )
}
fn provider_error(stage: &'static str, error: windows::core::Error) -> Error {
    // Never format the OS/provider message: it can contain untrusted UI text.
    Error::new(
        "accessibility_unavailable",
        format!(
            "Accessibility provider is unavailable; stage={stage}; hresult=0x{:08X}",
            error.code().0 as u32
        ),
    )
}

#[derive(Serialize)]
struct Node {
    id: String,
    parent: Option<String>,
    role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    value: Option<String>,
    bounds: Rect,
    enabled: bool,
    focusable: bool,
    password: Option<bool>,
}
fn clip(r: RECT, window: &Rect) -> Option<Rect> {
    let left = (r.left as i64).max(window.x as i64);
    let top = (r.top as i64).max(window.y as i64);
    let right = (r.right as i64).min(window.x as i64 + window.width as i64);
    let bottom = (r.bottom as i64).min(window.y as i64 + window.height as i64);
    (right > left && bottom > top).then(|| Rect {
        x: (left - window.x as i64) as i32,
        y: (top - window.y as i64) as i32,
        width: (right - left) as i32,
        height: (bottom - top) as i32,
    })
}
fn role(id: i32) -> String {
    match id {
        50000 => "button",
        50001 => "calendar",
        50002 => "checkbox",
        50003 => "combobox",
        50004 => "edit",
        50005 => "hyperlink",
        50006 => "image",
        50007 => "listitem",
        50008 => "list",
        50009 => "menu",
        50010 => "menubar",
        50011 => "menuitem",
        50012 => "progressbar",
        50013 => "radiobutton",
        50014 => "scrollbar",
        50015 => "slider",
        50016 => "spinner",
        50017 => "statusbar",
        50018 => "tab",
        50019 => "tabitem",
        50020 => "text",
        50021 => "toolbar",
        50022 => "tooltip",
        50023 => "tree",
        50024 => "treeitem",
        50025 => "custom",
        50026 => "group",
        50027 => "thumb",
        50028 => "datagrid",
        50029 => "dataitem",
        50030 => "document",
        50031 => "splitbutton",
        50032 => "window",
        50033 => "pane",
        50034 => "header",
        50035 => "headeritem",
        50036 => "table",
        50037 => "titlebar",
        50038 => "separator",
        _ => "unknown",
    }
    .into()
}
fn decode_bool(value: &VARIANT) -> (Option<bool>, u16) {
    // SAFETY: inspect the discriminator before accessing the VARIANT_BOOL arm.
    unsafe {
        let data = &value.Anonymous.Anonymous;
        let result = (data.vt == VT_BOOL).then(|| data.Anonymous.boolVal.0 != 0);
        (result, data.vt.0)
    }
}
fn ignore_default_value(property: UIA_PROPERTY_ID) -> bool {
    // UIA supplies documented framework defaults for these nonprivacy properties.
    // https://learn.microsoft.com/windows/win32/winauto/uiauto-automation-element-propids
    // Password and every unreviewed property always require an explicit value.
    ![
        UIA_IsOffscreenPropertyId,
        UIA_IsEnabledPropertyId,
        UIA_IsKeyboardFocusablePropertyId,
    ]
    .contains(&property)
}
fn cached_bool_detail(
    element: &IUIAutomationElement,
    property: UIA_PROPERTY_ID,
) -> windows::core::Result<Option<bool>> {
    // The password path suppresses defaults: unknown never authorizes text reads.
    unsafe {
        let value = Variant(element.GetCachedPropertyValueEx(property, true)?);
        Ok(decode_bool(&value.0).0)
    }
}
fn cached_bool(element: &IUIAutomationElement, property: UIA_PROPERTY_ID) -> Option<bool> {
    cached_bool_detail(element, property).ok().flatten()
}
fn required_bool_result(
    result: windows::core::Result<VARIANT>,
    stage: &'static str,
    depth: usize,
) -> Result<bool> {
    let value = Variant(result.map_err(|error| {
        let mut error = provider_error(stage, error);
        error.message.push_str(&format!("; depth={depth}"));
        error
    })?);
    let (value, variant_type) = decode_bool(&value.0);
    required_bool(value, stage).map_err(|mut error| {
        // Only numeric VARIANT type/depth, never provider values or text.
        error
            .message
            .push_str(&format!("; vt={variant_type}; depth={depth}"));
        error
    })
}
fn required_property_bool(
    element: &IUIAutomationElement,
    property: UIA_PROPERTY_ID,
    stage: &'static str,
    depth: usize,
) -> Result<bool> {
    // FALSE asks UIA for its own default, not an invented client-side false.
    // A failed HRESULT or non-VT_BOOL still rejects the observation.
    unsafe {
        required_bool_result(
            element.GetCachedPropertyValueEx(property, ignore_default_value(property)),
            stage,
            depth,
        )
    }
}
fn text_allowed(password: Option<bool>) -> bool {
    password == Some(false)
}
fn required_bool(value: Option<bool>, stage: &'static str) -> Result<bool> {
    value.ok_or_else(|| unavailable(stage, "unsupported_bool"))
}
fn confirmed_password(flags: [Option<bool>; 3]) -> Option<bool> {
    if flags.contains(&Some(true)) {
        Some(true)
    } else if flags.iter().all(|&flag| flag == Some(false)) {
        Some(false)
    } else {
        None
    }
}
fn truncate_utf16(value: &[u16], allowance: usize) -> (String, bool) {
    let mut chars =
        char::decode_utf16(value.iter().copied()).map(|r| r.unwrap_or(char::REPLACEMENT_CHARACTER));
    let text = chars.by_ref().take(allowance).collect();
    (text, chars.next().is_some())
}
fn walk_result<T>(status: windows::core::HRESULT, element: Option<T>) -> Result<Option<T>> {
    status
        .ok()
        .map_err(|error| provider_error("walk.result", error))?;
    Ok(element)
}
fn walk(
    walker: &IUIAutomationTreeWalker,
    element: &IUIAutomationElement,
    cache: &IUIAutomationCacheRequest,
    first: bool,
) -> Result<Option<IUIAutomationElement>> {
    // Win32 documents S_OK + NULL as ordinary end of traversal. The generated
    // nonnullable wrapper turns NULL into E_POINTER, losing that distinction.
    // Read the raw out-pointer separately so actual HRESULT errors fail closed.
    unsafe {
        let mut pointer = std::ptr::null_mut();
        let call = if first {
            walker.vtable().GetFirstChildElementBuildCache
        } else {
            walker.vtable().GetNextSiblingElementBuildCache
        };
        let status = call(
            walker.as_raw(),
            element.as_raw(),
            cache.as_raw(),
            &mut pointer,
        );
        // Only a successful COM return transfers an owned reference. Do not
        // construct or release an out-pointer from a failed provider call.
        status.ok().map_err(|error| {
            provider_error(
                if first {
                    "walk.first_child"
                } else {
                    "walk.next_sibling"
                },
                error,
            )
        })?;
        let found = if pointer.is_null() {
            None
        } else {
            Some(IUIAutomationElement::from_raw(pointer))
        };
        walk_result(status, found)
    }
}
fn node_budget(visited: usize) -> bool {
    visited < 256
}
#[derive(Debug, PartialEq, Eq)]
struct WalkPolicy {
    emit: bool,
    descend: bool,
}
fn walk_policy(offscreen: bool, has_bounds: bool, password: Option<bool>) -> WalkPolicy {
    WalkPolicy {
        emit: !offscreen && has_bounds,
        descend: text_allowed(password),
    }
}
struct Traversal {
    window: Window,
    walker: IUIAutomationTreeWalker,
    structural: IUIAutomationCacheRequest,
    text: IUIAutomationCacheRequest,
    nodes: Vec<Node>,
    reasons: BTreeSet<&'static str>,
    text_chars: usize,
    visited: usize,
    started: Instant,
}
impl Traversal {
    fn within_budget(&mut self) -> bool {
        if !node_budget(self.visited) {
            self.reasons.insert("node_limit");
            return false;
        }
        if self.started.elapsed() >= Duration::from_millis(1500) {
            self.reasons.insert("time_limit");
            return false;
        }
        true
    }
    fn bounded_text(&mut self, value: &[u16], cap: usize) -> String {
        let allowance = cap.min(8192usize.saturating_sub(self.text_chars));
        let (text, truncated) = truncate_utf16(value, allowance);
        self.text_chars += text.chars().count();
        if truncated {
            self.reasons.insert("text_limit");
        }
        text
    }
    fn populate_text(&mut self, element: &IUIAutomationElement, node: &mut Node) -> Result<()> {
        if !text_allowed(node.password) {
            return Ok(());
        }
        if self.started.elapsed() >= Duration::from_millis(1500) {
            self.reasons.insert("time_limit");
            return Ok(());
        }
        unsafe {
            let text = element
                .BuildUpdatedCache(&self.text)
                .map_err(|error| provider_error("text.refresh", error))?;
            let text_password = cached_bool(&text, UIA_IsPasswordPropertyId);
            let post_password = if text_allowed(text_password)
                && self.started.elapsed() < Duration::from_millis(1500)
            {
                let post = element
                    .BuildUpdatedCache(&self.structural)
                    .map_err(|error| provider_error("text.password_refresh", error))?;
                cached_bool(&post, UIA_IsPasswordPropertyId)
            } else {
                if self.started.elapsed() >= Duration::from_millis(1500) {
                    self.reasons.insert("time_limit");
                }
                None
            };
            node.password = confirmed_password([node.password, text_password, post_password]);
            if text_allowed(node.password) {
                if let Ok(name) = text.CachedName() {
                    node.name = Some(self.bounded_text(&name, 160));
                }
                if let Ok(pattern) =
                    text.GetCachedPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId)
                {
                    if let Ok(value) = pattern.CachedValue() {
                        node.value = Some(self.bounded_text(&value, 256));
                    }
                }
            }
        }
        Ok(())
    }
    fn visit(
        &mut self,
        element: IUIAutomationElement,
        parent: Option<String>,
        depth: usize,
    ) -> Result<()> {
        if !self.within_budget() {
            return Ok(());
        }
        self.visited += 1;
        unsafe {
            let offscreen = required_property_bool(
                &element,
                UIA_IsOffscreenPropertyId,
                if depth == 0 {
                    "root.offscreen"
                } else {
                    "node.offscreen"
                },
                depth,
            )?;
            let bounds = element
                .CachedBoundingRectangle()
                .map_err(|error| provider_error("node.bounds", error))?;
            let bounds = clip(bounds, &self.window.rect);
            let mut password = cached_bool(&element, UIA_IsPasswordPropertyId);
            let policy = walk_policy(offscreen, bounds.is_some(), password);
            // A missing/hidden root cannot produce the canonical single-root
            // snapshot; return unavailable so the caller can use image fallback.
            if depth == 0 && !policy.emit {
                return Err(unavailable("root.visibility", "root_not_visible"));
            }
            let mut nearest_parent = parent.clone();
            if policy.emit {
                let id = format!("e{}", self.nodes.len());
                let mut node = Node {
                    id: id.clone(),
                    parent,
                    role: role(
                        element
                            .CachedControlType()
                            .map_err(|error| provider_error("node.control_type", error))?
                            .0,
                    ),
                    name: None,
                    value: None,
                    bounds: bounds.ok_or_else(|| unavailable("node.bounds", "missing_bounds"))?,
                    enabled: required_property_bool(
                        &element,
                        UIA_IsEnabledPropertyId,
                        "node.enabled",
                        depth,
                    )?,
                    focusable: required_property_bool(
                        &element,
                        UIA_IsKeyboardFocusablePropertyId,
                        "node.focusable",
                        depth,
                    )?,
                    password,
                };
                self.populate_text(&element, &mut node)?;
                password = node.password;
                self.nodes.push(node);
                nearest_parent = Some(id);
            } else if policy.descend && self.within_budget() {
                // Nonemitted containers may still contain visible controls.
                // Recheck only structural properties before descending; never
                // fetch their Name/Value or any protected subtree.
                let post = element
                    .BuildUpdatedCache(&self.structural)
                    .map_err(|error| provider_error("container.password_refresh", error))?;
                password = confirmed_password([
                    password,
                    password,
                    cached_bool(&post, UIA_IsPasswordPropertyId),
                ]);
            }
            // Providers may expose text descendants below protected controls.
            if !text_allowed(password) {
                return Ok(());
            }
            if depth >= 12 {
                self.reasons.insert("depth_limit");
                return Ok(());
            }
            if !self.within_budget() {
                return Ok(());
            }
            let mut child = walk(&self.walker, &element, &self.structural, true)?;
            while let Some(current) = child {
                if !self.within_budget() {
                    break;
                }
                self.visit(current.clone(), nearest_parent.clone(), depth + 1)?;
                if !self.within_budget() {
                    break;
                }
                child = walk(&self.walker, &current, &self.structural, false)?;
            }
        }
        Ok(())
    }
}
pub fn inspect(target: &Target) -> Result<serde_json::Value> {
    let started = Instant::now();
    let window = window::inspect(target)?;
    let _apartment = Apartment::new()?;
    // SAFETY: interfaces are scoped inside and released before the MTA guard.
    unsafe {
        let automation: IUIAutomation =
            CoCreateInstance(&CUIAutomation8, None, CLSCTX_INPROC_SERVER)
                .map_err(|error| provider_error("automation.create", error))?;
        if let Ok(v2) = automation.cast::<IUIAutomation2>() {
            v2.SetConnectionTimeout(500)
                .map_err(|error| provider_error("automation.connection_timeout", error))?;
            v2.SetTransactionTimeout(500)
                .map_err(|error| provider_error("automation.transaction_timeout", error))?;
        }
        let structural = automation
            .CreateCacheRequest()
            .map_err(|error| provider_error("cache.structural.create", error))?;
        structural
            .SetTreeScope(TreeScope_Element)
            .map_err(|error| provider_error("cache.structural.scope", error))?;
        for property in [
            UIA_IsPasswordPropertyId,
            UIA_ControlTypePropertyId,
            UIA_BoundingRectanglePropertyId,
            UIA_IsEnabledPropertyId,
            UIA_IsKeyboardFocusablePropertyId,
            UIA_IsOffscreenPropertyId,
        ] {
            structural
                .AddProperty(property)
                .map_err(|error| provider_error("cache.structural.property", error))?;
        }
        let text = automation
            .CreateCacheRequest()
            .map_err(|error| provider_error("cache.text.create", error))?;
        text.SetTreeScope(TreeScope_Element)
            .map_err(|error| provider_error("cache.text.scope", error))?;
        text.AddProperty(UIA_NamePropertyId)
            .map_err(|error| provider_error("cache.text.name", error))?;
        text.AddProperty(UIA_IsPasswordPropertyId)
            .map_err(|error| provider_error("cache.text.password", error))?;
        text.AddProperty(UIA_ValueValuePropertyId)
            .map_err(|error| provider_error("cache.text.value", error))?;
        text.AddPattern(UIA_ValuePatternId)
            .map_err(|error| provider_error("cache.text.pattern", error))?;
        let root = automation
            .ElementFromHandleBuildCache(window::hwnd(target)?, &structural)
            .map_err(|error| provider_error("root.element", error))?;
        let walker = automation
            .ControlViewWalker()
            .map_err(|error| provider_error("walker.control_view", error))?;
        let mut traversal = Traversal {
            window: window.clone(),
            walker,
            structural,
            text,
            nodes: Vec::new(),
            reasons: BTreeSet::new(),
            text_chars: 0,
            visited: 0,
            started,
        };
        traversal.visit(root, None, 0)?;
        if traversal.nodes.is_empty() {
            return Err(unavailable("tree.result", "empty_tree"));
        }
        let after = window::inspect(target)?;
        if !window::same_target(&window, &after) {
            return Err(Error::new(
                "window_changed",
                "Target changed during accessibility inspection",
            ));
        }
        static SERIAL: AtomicU64 = AtomicU64::new(1);
        let snapshot = format!(
            "{}-{}",
            std::process::id(),
            SERIAL.fetch_add(1, Ordering::Relaxed)
        );
        Ok(
            serde_json::json!({"snapshot_id":snapshot,"window":after,"nodes":traversal.nodes,
            "truncated":!traversal.reasons.is_empty(),"truncation_reasons":traversal.reasons,
            "metrics":{"inspect_ms":started.elapsed().as_secs_f64()*1000.0}}),
        )
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_reviewed_nonprivacy_bools_accept_framework_defaults() {
        for property in [
            UIA_IsOffscreenPropertyId,
            UIA_IsEnabledPropertyId,
            UIA_IsKeyboardFocusablePropertyId,
        ] {
            assert!(!ignore_default_value(property));
        }
        assert!(ignore_default_value(UIA_IsPasswordPropertyId));
        assert!(ignore_default_value(UIA_HasKeyboardFocusPropertyId));
    }

    #[test]
    fn required_bool_decoding_preserves_values_and_rejects_types_and_hresult() {
        assert!(required_bool_result(Ok(VARIANT::from(true)), "node.offscreen", 2).unwrap());
        assert!(!required_bool_result(Ok(VARIANT::from(false)), "node.offscreen", 2).unwrap());
        let invalid = required_bool_result(Ok(VARIANT::from(0i32)), "node.offscreen", 2)
            .expect_err("integer zero is not a boolean default");
        assert_eq!(invalid.code, "accessibility_unavailable");
        assert!(
            invalid
                .message
                .ends_with("reason=unsupported_bool; vt=3; depth=2")
        );
        let empty = required_bool_result(Ok(VARIANT::default()), "node.offscreen", 3)
            .expect_err("empty is not false");
        assert!(
            empty
                .message
                .ends_with("reason=unsupported_bool; vt=0; depth=3")
        );
        let failed = required_bool_result(
            Err(windows::core::Error::new(
                windows::core::HRESULT(0x80004005u32 as i32),
                "PRIVATE_PROVIDER_CONTENT",
            )),
            "node.offscreen",
            4,
        )
        .expect_err("failed call cannot supply a default");
        assert!(failed.message.ends_with("hresult=0x80004005; depth=4"));
        assert!(!failed.message.contains("PRIVATE_PROVIDER_CONTENT"));
    }

    #[test]
    fn diagnostics_include_only_fixed_stage_and_numeric_hresult() {
        let error = windows::core::Error::new(
            windows::core::HRESULT(0x80004005u32 as i32),
            "PRIVATE_UI_VALUE_DO_NOT_LOG",
        );
        let result = provider_error("root.element", error);
        assert_eq!(result.code, "accessibility_unavailable");
        assert!(!result.retryable);
        assert_eq!(
            result.message,
            "Accessibility provider is unavailable; stage=root.element; hresult=0x80004005"
        );
        assert!(!result.message.contains("PRIVATE_UI"));
    }

    #[test]
    fn unsupported_bool_and_root_policy_are_distinct_fixed_reasons() {
        let unsupported = required_bool(None, "node.focusable").expect_err("unsupported");
        let root = unavailable("root.visibility", "root_not_visible");
        assert_eq!(unsupported.code, root.code);
        assert_eq!(
            unsupported.message,
            "Accessibility provider is unavailable; stage=node.focusable; reason=unsupported_bool"
        );
        assert_eq!(
            root.message,
            "Accessibility provider is unavailable; stage=root.visibility; reason=root_not_visible"
        );
    }
    #[test]
    fn password_and_unknown_never_allow_text() {
        assert!(!text_allowed(Some(true)));
        assert!(!text_allowed(None));
        assert!(text_allowed(Some(false)));
    }
    #[test]
    fn traversal_end_is_distinct_from_provider_error() {
        use windows::core::HRESULT;
        assert_eq!(
            walk_result::<u8>(HRESULT(0), None).expect("normal end"),
            None
        );
        assert_eq!(walk_result(HRESULT(0), Some(7)).expect("element"), Some(7));
        for code in [0x80004003u32, 0x80004005u32] {
            assert_eq!(
                walk_result::<u8>(HRESULT(code as i32), None)
                    .expect_err("provider error")
                    .code,
                "accessibility_unavailable"
            );
        }
    }
    #[test]
    fn excluded_nodes_still_consume_visit_budget() {
        assert!(node_budget(0));
        assert!(node_budget(255));
        assert!(!node_budget(256));
        assert!(!node_budget(1000));
    }
    #[test]
    fn invisible_containers_allow_visible_children_but_protected_containers_do_not() {
        for (offscreen, bounds) in [(true, true), (false, false), (true, false)] {
            assert_eq!(
                walk_policy(offscreen, bounds, Some(false)),
                WalkPolicy {
                    emit: false,
                    descend: true
                }
            );
            assert!(walk_policy(false, true, Some(false)).emit);
            for password in [Some(true), None] {
                assert!(!walk_policy(offscreen, bounds, password).descend);
            }
        }
        assert_eq!(
            walk_policy(false, true, Some(true)),
            WalkPolicy {
                emit: true,
                descend: false
            }
        );
    }
    #[test]
    fn required_structure_errors_are_not_policy_omissions() {
        assert!(required_bool(None, "test.bool").is_err());
        assert!(!required_bool(Some(false), "test.bool").expect("known"));
    }
    #[test]
    fn changed_or_unknown_password_discards_text_authority() {
        assert_eq!(
            confirmed_password([Some(false), Some(false), Some(false)]),
            Some(false)
        );
        for flags in [
            [Some(false), Some(true), None],
            [Some(false), Some(false), Some(true)],
            [Some(false), None, Some(false)],
        ] {
            assert!(!text_allowed(confirmed_password(flags)));
        }
    }
    #[test]
    fn utf16_decoding_caps_before_allocating_full_string() {
        assert_eq!(
            truncate_utf16(&[65, 0xd83d, 0xde00, 66], 2),
            ("A😀".into(), true)
        );
        assert_eq!(truncate_utf16(&[0xd800, 65], 2), ("�A".into(), false));
        assert_eq!(truncate_utf16(&[65; 100_000], 160).0.len(), 160);
    }
    #[test]
    fn clip_is_client_relative_and_excludes_outside() {
        let w = Rect {
            x: 100,
            y: 200,
            width: 500,
            height: 300,
        };
        assert_eq!(
            clip(
                RECT {
                    left: 80,
                    top: 190,
                    right: 130,
                    bottom: 240
                },
                &w
            ),
            Some(Rect {
                x: 0,
                y: 0,
                width: 30,
                height: 40
            })
        );
        assert!(
            clip(
                RECT {
                    left: 0,
                    top: 0,
                    right: 50,
                    bottom: 50
                },
                &w
            )
            .is_none()
        );
    }
}
