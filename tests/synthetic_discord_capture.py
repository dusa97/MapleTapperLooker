"""Capture a synthetic target through the same spawned helper used by alerts."""
import os
from pathlib import Path
import sys
import tkinter as tk

sys.path.insert(0, str(Path(__file__).parent.parent))
from PIL import Image
# Include the native package when this synthetic helper check is frozen.
from windows_capture import WindowsCapture  # noqa: F401
from discord_alerts import WindowTarget, _process_creation_time, capture_window


def main():
    target = tk.Tk()
    target.geometry("240x180+100+100")
    target.configure(bg="#00ff00")
    tk.Label(target, text="synthetic target", bg="#00ff00").pack(expand=True)
    target.update()
    cover = tk.Toplevel(target)
    cover.geometry("280x220+80+80")
    cover.configure(bg="#ff0000")
    cover.update()
    identity = WindowTarget(target.winfo_id(), os.getpid(), _process_creation_time(os.getpid()))
    try:
        image = capture_window(identity)
        if image is None:
            raise RuntimeError("capture helper returned no image")
        decoded = Image.open(__import__("io").BytesIO(image)).convert("RGB")
        red, green, blue = decoded.getpixel((decoded.width // 2, decoded.height // 2))
        if green <= red or green <= blue:
            raise RuntimeError("capture helper included the covering window")
        print("PASS: source/frozen helper captured the explicit synthetic target")
    finally:
        cover.destroy()
        target.destroy()


if __name__ == "__main__":
    main()
