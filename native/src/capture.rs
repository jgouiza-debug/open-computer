use crate::protocol::{Error, MAX_CACHE, MAX_PIXELS, Result, Target};
use crate::window::{self, Window};
use base64::{Engine, engine::general_purpose::STANDARD};
use std::io::{self, Write};
use std::time::Instant;
use windows::Win32::Foundation::HWND;
use windows::Win32::Graphics::Gdi::*;

struct WindowDc {
    hwnd: HWND,
    dc: HDC,
}
impl Drop for WindowDc {
    fn drop(&mut self) {
        unsafe {
            ReleaseDC(Some(self.hwnd), self.dc);
        }
    }
}
struct MemoryDc(HDC);
impl Drop for MemoryDc {
    fn drop(&mut self) {
        unsafe {
            let _ = DeleteDC(self.0);
        }
    }
}
struct Bitmap(HBITMAP);
impl Drop for Bitmap {
    fn drop(&mut self) {
        unsafe {
            let _ = DeleteObject(HGDIOBJ(self.0.0));
        }
    }
}
struct Selected<'a> {
    dc: &'a MemoryDc,
    _bitmap: &'a Bitmap,
    old: HGDIOBJ,
}
impl Drop for Selected<'_> {
    fn drop(&mut self) {
        unsafe {
            SelectObject(self.dc.0, self.old);
        }
    }
}

struct Cache {
    window: Window,
    raw: Vec<u8>,
    encoded: String,
}
#[derive(Default)]
pub struct Capturer {
    cache: Option<Cache>,
}

struct BoundedBytes(Vec<u8>);
impl Write for BoundedBytes {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > 24_000_000 {
            return Err(io::Error::other("Encoded frame exceeds reply budget"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
pub fn encode_rgb(raw: &[u8], width: u32, height: u32) -> Result<String> {
    let mut output = BoundedBytes(Vec::new());
    {
        let mut encoder = png::Encoder::new(&mut output, width, height);
        encoder.set_color(png::ColorType::Rgb);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.set_compression(png::Compression::Balanced);
        let mut writer = encoder
            .write_header()
            .map_err(|_| Error::new("capture_failed", "PNG initialization failed"))?;
        writer.write_image_data(raw).map_err(|_| {
            Error::new(
                "resource_limit",
                "PNG exceeds encoding budget or cannot be encoded",
            )
        })?;
        writer
            .finish()
            .map_err(|_| Error::new("capture_failed", "PNG finalization failed"))?;
    }
    Ok(STANDARD.encode(output.0))
}

fn grab(window: &Window, target: &Target) -> Result<Vec<u8>> {
    let width = window.rect.width as usize;
    let height = window.rect.height as usize;
    let pixels = width
        .checked_mul(height)
        .filter(|&n| n <= MAX_PIXELS)
        .ok_or_else(|| Error::new("resource_limit", "Client area exceeds pixel limit"))?;
    let handle = window::hwnd(target)?;
    // SAFETY: GDI resources are checked and owned by RAII guards. DIB has exactly
    // width*height*4 bytes; top-down32bpp rows have no padding beyond width*4.
    unsafe {
        let source = WindowDc {
            hwnd: handle,
            dc: GetDC(Some(handle)),
        };
        if source.dc.0.is_null() {
            return Err(Error::new("capture_failed", "Cannot acquire client DC"));
        }
        let memory = MemoryDc(CreateCompatibleDC(Some(source.dc)));
        if memory.0.0.is_null() {
            return Err(Error::new("capture_failed", "Cannot create capture DC"));
        }
        let info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: window.rect.width,
                biHeight: -window.rect.height,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut pointer = std::ptr::null_mut();
        let bitmap = Bitmap(
            CreateDIBSection(
                Some(source.dc),
                &info,
                DIB_RGB_COLORS,
                &mut pointer,
                None,
                0,
            )
            .map_err(|_| Error::new("capture_failed", "Cannot allocate capture bitmap"))?,
        );
        if pointer.is_null() {
            return Err(Error::new("capture_failed", "Capture bitmap has no pixels"));
        }
        let old = SelectObject(memory.0, HGDIOBJ(bitmap.0.0));
        if old.0.is_null() || old.0 as isize == -1 {
            return Err(Error::new("capture_failed", "Cannot select capture bitmap"));
        }
        let _selection = Selected {
            dc: &memory,
            _bitmap: &bitmap,
            old,
        };
        BitBlt(
            memory.0,
            0,
            0,
            window.rect.width,
            window.rect.height,
            Some(source.dc),
            0,
            0,
            SRCCOPY | CAPTUREBLT,
        )
        .map_err(|_| Error::new("capture_failed", "Client capture failed"))?;
        if !GdiFlush().as_bool() {
            return Err(Error::new("capture_failed", "GDI flush failed"));
        }
        let bgra = std::slice::from_raw_parts(pointer as *const u8, pixels * 4);
        let mut rgb = Vec::with_capacity(pixels * 3);
        for pixel in bgra.chunks_exact(4) {
            rgb.extend_from_slice(&[pixel[2], pixel[1], pixel[0]]);
        }
        Ok(rgb)
    }
}
impl Capturer {
    fn encoded_frame(&mut self, window: &Window, raw: Vec<u8>) -> Result<(String, bool)> {
        let hit = self
            .cache
            .as_ref()
            .is_some_and(|cache| window::same_target(&cache.window, window) && cache.raw == raw);
        if hit {
            return self
                .cache
                .as_ref()
                .map(|c| (c.encoded.clone(), true))
                .ok_or_else(|| Error::new("internal_error", "Capture cache missing"));
        }
        let encoded = encode_rgb(&raw, window.rect.width as u32, window.rect.height as u32)?;
        self.cache = if raw.len().saturating_add(encoded.len()) <= MAX_CACHE {
            Some(Cache {
                window: window.clone(),
                raw,
                encoded: encoded.clone(),
            })
        } else {
            None
        };
        Ok((encoded, false))
    }
    pub fn capture(&mut self, target: &Target) -> Result<serde_json::Value> {
        let result = self.capture_inner(target);
        if result.is_err() {
            self.cache = None;
        }
        result
    }
    fn capture_inner(&mut self, target: &Target) -> Result<serde_json::Value> {
        let started = Instant::now();
        let before = window::inspect(target)?;
        let raw = grab(&before, target)?;
        let after = window::inspect(target)?;
        if !window::same_target(&before, &after) {
            return Err(Error::new(
                "window_changed",
                "Client geometry changed during capture",
            ));
        }
        let capture_ms = started.elapsed().as_secs_f64() * 1000.0;
        let encode_start = Instant::now();
        let (encoded, hit) = self.encoded_frame(&after, raw)?;
        let encode_ms = encode_start.elapsed().as_secs_f64() * 1000.0;
        Ok(
            serde_json::json!({"window":after,"image_base64":encoded,"mime_type":"image/png",
            "image_width":before.rect.width,"image_height":before.rect.height,
            "metrics":{"capture_ms":capture_ms,"encode_ms":encode_ms,"cache_hit":hit}}),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn png_roundtrip_preserves_pixels() {
        let pixels = [255, 0, 17, 0, 255, 88];
        let encoded = encode_rgb(&pixels, 2, 1).expect("encode");
        let png = STANDARD.decode(encoded).expect("base64");
        let mut reader = png::Decoder::new(std::io::Cursor::new(png))
            .read_info()
            .expect("header");
        let mut out = vec![0; reader.output_buffer_size().expect("size")];
        let frame = reader.next_frame(&mut out).expect("frame");
        assert_eq!(&out[..frame.buffer_size()], &pixels);
    }
    #[test]
    fn writer_bounds_large_payload() {
        let mut w = BoundedBytes(Vec::new());
        assert!(w.write(&vec![0; 24_000_001]).is_err());
        assert!(w.0.is_empty());
    }
    #[test]
    fn cache_requires_exact_pixels_identity_and_geometry() {
        let mut cache = Capturer::default();
        let mut w = Window {
            id: "123".into(),
            pid: 42,
            title: "Test".into(),
            rect: window::Rect {
                x: 10,
                y: 20,
                width: 2,
                height: 1,
            },
        };
        let raw = vec![1, 2, 3, 4, 5, 6];
        let (first, hit) = cache.encoded_frame(&w, raw.clone()).expect("first");
        assert!(!hit);
        let (same, hit) = cache.encoded_frame(&w, raw.clone()).expect("same");
        assert!(hit);
        assert_eq!(first, same);
        let mut different = raw.clone();
        different[0] = 9;
        assert!(!cache.encoded_frame(&w, different).expect("pixels").1);
        w.pid += 1;
        assert!(!cache.encoded_frame(&w, raw.clone()).expect("pid").1);
        w.id = "124".into();
        assert!(!cache.encoded_frame(&w, raw.clone()).expect("handle").1);
        w.rect.x += 1;
        assert!(!cache.encoded_frame(&w, raw.clone()).expect("position").1);
        w.rect.width = 1;
        w.rect.height = 2;
        assert!(!cache.encoded_frame(&w, raw).expect("size").1);
    }
}
