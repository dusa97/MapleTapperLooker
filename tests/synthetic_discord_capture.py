"""Capture a synthetic target through the same spawned helper used by alerts."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

import ctypes
import os
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).parent.parent))
from PIL import Image
# Include the native package when this synthetic helper check is frozen.
from windows_capture import WindowsCapture  # noqa: F401
from discord_alerts import WindowTarget, _process_creation_time, capture_window, WindowWatch


from tests.synthetic_window import cover_window


def main():
    target = tk.Tk()
    target.geometry("240x180+100+100")
    target.configure(bg="#00ff00")
    tk.Label(target, text="synthetic target", bg="#00ff00").pack(expand=True)
    target.update()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    cover = context.Process(target=cover_window, args=(child,))
    cover.start()
    child.close()
    if not parent.poll(10):
        exitcode = cover.exitcode
        cover.terminate()
        cover.join()
        raise RuntimeError(f"covering window did not start; child exit={exitcode}")
    ready = parent.recv()
    if ready is not True:
        cover.join(5)
        raise RuntimeError("covering window setup failed: " + str(ready))
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    identity = WindowTarget(user32.GetAncestor(target.winfo_id(), 2), os.getpid(),
                            _process_creation_time(os.getpid()))
    try:
        image = capture_window(identity)
        if image is None:
            raise RuntimeError("capture helper returned no image")
        decoded = Image.open(__import__("io").BytesIO(image)).convert("RGB")
        red, green, blue = decoded.getpixel((decoded.width // 2, decoded.height // 2))
        if green <= red or green <= blue:
            raise RuntimeError("capture helper included the covering window")
        before = {child.pid for child in multiprocessing.active_children()}
        if capture_window(identity, timeout=0.001) is not None:
            raise RuntimeError("expired capture returned an image")
        if {child.pid for child in multiprocessing.active_children()} != before:
            raise RuntimeError("capture timeout left a helper")
        watch = WindowWatch(identity.hwnd)
        target.iconify()
        target.update()
        if capture_window(identity) is not None:
            raise RuntimeError("minimized target returned an image")
        target.destroy()
        try:
            if watch.current() or capture_window(identity) is not None:
                raise RuntimeError("destroyed target returned an image")
        finally:
            watch.close()
        if sys.stdout is not None:
            print("PASS: target-only image, separate-process occluder, timeout cleanup, minimized and closed fallback")
    finally:
        parent.send(False)
        cover.join(5)
        if cover.is_alive():
            cover.terminate()
            cover.join()
        parent.close()
        try:
            target.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    try:
        main()
    except Exception as error:
        if args.result:
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            args.result.write_text("FAIL: " + detail, encoding="utf-8")
        raise SystemExit(1)
    if args.result:
        args.result.write_text("PASS", encoding="utf-8")
