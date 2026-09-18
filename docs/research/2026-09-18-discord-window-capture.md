# Discord window-capture evidence

Date: 2026-09-18. Scope: select a safe image path for the approved Discord feature.
No credentials, personal images, or session transcripts are included.

## Blocking questions and verdict

1. Can a webhook attach an image and mention a user without a bot? Yes, in a server channel, not a DM.
2. Can the current desktop capture exclude overlapping apps? No. Use a window-specific API instead.
3. Can Python 3.11 and a one-file Windows executable use window-only capture? The synthetic prototype passed both checks.

## Primary sources

- [Discord webhook API](https://discord.com/developers/docs/resources/webhook): execute a webhook with multipart files and `allowed_mentions`. Use `wait=true` for server confirmation. Device notifications are not guaranteed.
- [Microsoft CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow): creates a capture item for one HWND; requires Windows 10 version 1903.
- [Microsoft screen capture](https://learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture): runtime support checks, frame lifetime, content size, resize handling, and HDR considerations.
- [Microsoft PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow): synchronous operation whose target app renders the result.
- [Microsoft CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata): default protection normally binds to the same user credentials and computer. This is not protection from malware running as that user.
- [PyPI release metadata](https://pypi.org/pypi/windows-capture/2.0.1/json): Python >=3.9; NumPy and opencv-python dependencies; Windows amd64 ABI3 wheel.
- [Package source inspected](https://github.com/NiiightmareXD/windows-capture/blob/c7d106448eb9d9b251345c39047711e1cd408ae2/windows-capture-python/src/lib.rs): explicit HWND path, substring title path, and default-to-monitor behavior. This upstream source is not claimed to be the release's exact build revision.

The installed 2.0.1 Python API was also inspected locally. It exposes `window_hwnd`, `secondary_window`, and `cursor_capture`.
Wheel: `windows_capture-2.0.1-cp39-abi3-win_amd64.whl`.
SHA-256: `209c2318e2168802b9afd1005e99499639e8835e71a1e188a3ddc77c7ccc3f04`.

## One throwaway prototype

Question: does explicit-HWND capture return the target's pixels when an opaque window covers it, from source and a packaged executable?

Branch: `prototype/discord-window-capture`.
Source: `prototypes/window_capture/check.py` on that branch.
Immutable commit: [57bb21ebdc480e18b90f8813df2fc5cc0a43e65b](https://github.com/dusa97/MapleTapperLooker/commit/57bb21ebdc480e18b90f8813df2fc5cc0a43e65b).
The prototype is evidence only. Do not merge it or copy it directly into production.

Environment: Windows 11 build 26200, x64; Python 3.11; windows-capture 2.0.1; PyInstaller 6.16.0.
An isolated temporary virtual environment was used. The main checkout's dependencies were not changed.
The prototype creates a solid green target and a larger opaque red covering window.
It captures only the synthetic target HWND, checks the center pixel, and closes both windows.
It never saves image pixels or accesses Discord.

Reproduction, on the prototype branch with an isolated environment:

```powershell
py -3.11 -m venv .prototype-venv
.\.prototype-venv\Scripts\python.exe -m pip install windows-capture==2.0.1 pyinstaller==6.16.0 pillow
.\.prototype-venv\Scripts\python.exe prototypes/window_capture/check.py
.\.prototype-venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --onefile --name window-capture-check prototypes/window_capture/check.py
.\dist\window-capture-check.exe
```

Both runs returned a 322 x 272 frame with center BGR `[0, 255, 0]`.
Both printed `PASS: target captured; opaque red occluder excluded`.
No image, executable, virtual environment, or build log will be published.

## Limits and required follow-up

The prototype used two synthetic Tk windows, not a real game, a separate-process occluder, an elevated target, or a protected surface.
It did not test API timeout cleanup, HDR, fullscreen, minimization, or handle reuse.
Implementation must check a separate application covering the target and the intended game in both source and packaged modes.
Do not claim an image is correct merely because the API returned a buffer. Blank and unavailable output needs text fallback.
No live webhook request or real-device notification was tested. The implementation smoke test must use local credentials without publishing them.
