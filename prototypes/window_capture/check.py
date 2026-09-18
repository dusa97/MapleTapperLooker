"""Check HWND capture beneath an opaque synthetic window. No images are saved."""
import ctypes
import json
import threading
import time
import tkinter as tk

from windows_capture import WindowsCapture


def main():
    root = tk.Tk()
    root.title("Synthetic capture target")
    root.geometry("320x240+100+100")
    root.configure(bg="#00ff00")
    root.update()
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetAncestor.restype = ctypes.c_void_p
    hwnd = user32.GetAncestor(root.winfo_id(), 2)
    cover = tk.Toplevel(root)
    cover.title("Synthetic occluder")
    cover.geometry("400x320+60+60")
    cover.configure(bg="#ff0000")
    cover.attributes("-topmost", True)
    root.update()
    result = {}
    finished = threading.Event()

    def capture():
        try:
            session = WindowsCapture(window_hwnd=hwnd, cursor_capture=False,
                                     secondary_window=False)

            @session.event
            def on_frame_arrived(frame, control):
                pixel = frame.frame_buffer[frame.height // 2, frame.width // 2, :3]
                result.update(width=frame.width, height=frame.height,
                              center_bgr=[int(x) for x in pixel])
                control.stop()

            @session.event
            def on_closed():
                pass

            session.start()
        except Exception as error:
            result["error_type"] = type(error).__name__
        finally:
            finished.set()

    # Give the compositor time to draw the opaque cover before capture starts.
    start = time.monotonic()
    while time.monotonic() - start < 0.5:
        root.update()
        time.sleep(0.01)
    threading.Thread(target=capture, daemon=True).start()
    deadline = time.monotonic() + 10
    while not finished.is_set() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    root.destroy()
    print(json.dumps(result), flush=True)
    assert finished.is_set(), "Capture exceeded ten seconds"
    assert result.get("center_bgr") == [0, 255, 0], "Target pixels were not captured"
    print("PASS: target captured; opaque red occluder excluded", flush=True)


if __name__ == "__main__":
    main()
