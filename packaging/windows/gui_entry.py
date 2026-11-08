"""Windowed entry point. A startup error must remain visible without stdio."""
import ctypes
from ctypes import wintypes


def main() -> int:
    try:
        from opencomputer.app import main as app_main

        result = app_main()
        return 0 if result is None else result
    except Exception as exc:
        message = ("Open Computer could not start.\n\n" + type(exc).__name__ + ": " + str(exc)[:350] +
                   "\n\nUse OpenComputerCLI.exe setup doctor --offline for diagnostics.")
        show = ctypes.windll.user32.MessageBoxW
        show.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        show.restype = ctypes.c_int
        show(None, message, "Open Computer startup error", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
