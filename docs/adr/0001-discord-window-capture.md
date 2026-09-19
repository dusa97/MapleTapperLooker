# ADR 0001: Use window-only capture for Discord images

Status: accepted.
Date: 2026-09-18.

## Context

The user requires a full game-window image without overlapping applications. Existing mss desktop capture cannot satisfy that boundary.
The target must be the external window focused when watching starts, not a title search or the window focused at delivery time.

## Decision

Use Windows.Graphics.Capture through `windows-capture==2.0.1`, targeting an explicit validated HWND.
Never use the library's default monitor target or substring window-name selection.
Exclude secondary windows and the cursor. Retain the system capture border.
Use a short-lived capture helper with a hard process timeout so native capture cannot block the app or survive shutdown.
A missing or invalid target, unsupported capture, timeout, or unavailable image produces a text-only alert.
Never fall back to desktop capture, the OCR crop, PrintWindow, or another window.

The Python package requires `opencv-python`. Replace `opencv-python-headless`; never install both distributions into the same environment.
Keep NumPy and Pillow. Use the Python standard library for HTTP and Windows DPAPI through ctypes for protected settings.
Limit the supported new image path to Windows x64 with the required capture APIs; source and packaged checks use Python 3.11.
Window-handle capture requires Windows 10 version 1903 or later. Runtime capability failures still use text-only alerts.

## Consequences

This adds one native capture dependency and changes the OpenCV distribution. Recheck OCR, auto-location, and executable packaging.
A synthetic occlusion check passed from Python and a one-file executable. This does not prove compatibility with a protected game or every graphics driver.
The target can include in-game chat or overlays rendered by the game itself. External occluder exclusion is not content redaction.
Protected content, minimization, HDR, and exclusive fullscreen can make images unavailable or unsuitable. Text fallback remains part of the feature.

## Alternatives

- Desktop crop: rejected by the user because other applications can appear.
- PrintWindow: target-rendered and synchronous; not a reliable universal game capture path.
- Custom Direct3D/WinRT bindings: more code than the tested maintained package.
- Window picker: not selected; foreground binding preserves the approved start workflow.

## Evidence

See [dated research and prototype results](../research/2026-09-18-discord-window-capture.md).
