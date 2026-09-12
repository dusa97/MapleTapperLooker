"""Record a Windows microphone message for detection alerts."""
import ctypes
from pathlib import Path
import platform
import threading
import wave


class RecordedAlert:
    def __init__(self, path):
        self.path = Path(path)
        self.recording = False
        self.status = "F11: record message"
        self._lock = threading.Lock()
        self._closed = False

    def _command(self, command):
        winmm = ctypes.windll.winmm
        error = winmm.mciSendStringW(command, None, 0, None)
        if error:
            text = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(error, text, len(text))
            raise RuntimeError(text.value or f"Audio error {error}")

    def toggle(self):
        with self._lock:
            if self._closed:
                return
            if platform.system() != "Windows":
                self.status = "Recording needs Windows"
                print(f"[audio] {self.status}", flush=True)
                return
            temporary = self.path.with_suffix(".tmp.wav")
            try:
                if not self.recording:
                    import winsound
                    winsound.PlaySound(None, 0)
                    self._command("open new type waveaudio alias maple_message")
                    self._command("record maple_message")
                    self.recording = True
                    self.status = "REC - F11: stop"
                else:
                    self._command("stop maple_message")
                    self._command(f'save maple_message "{temporary}"')
                    self._command("close maple_message")
                    self.recording = False
                    with wave.open(str(temporary), "rb") as audio:
                        if audio.getnframes() == 0:
                            raise RuntimeError("Recording is empty; record a longer message")
                    temporary.replace(self.path)
                    self.status = "Saved - F11: replace"
            except Exception as error:
                self.recording = False
                try:
                    self._command("close maple_message")
                except Exception:
                    pass
                self.status = "Audio error - retry F11"
                print(f"[audio] {error}. Previous message kept.", flush=True)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    print(f"[audio] Could not remove temporary recording: {error}", flush=True)
            print(f"[audio] {self.status}", flush=True)

    def play(self, fallback):
        with self._lock:
            if self._closed or self.recording:
                return
            if platform.system() == "Windows" and self.path.exists():
                try:
                    import winsound
                    winsound.PlaySound(str(self.path), winsound.SND_FILENAME |
                                       winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                    return
                except Exception as error:
                    print(f"[audio] Playback failed: {error}. Using beep.", flush=True)
            fallback()

    def close(self):
        with self._lock:
            self._closed = True
            if platform.system() == "Windows":
                import winsound
                try:
                    winsound.PlaySound(None, 0)
                except RuntimeError as error:
                    print(f"[audio] Could not stop playback: {error}", flush=True)
                if self.recording:
                    try:
                        self._command("close maple_message")
                    except Exception as error:
                        print(f"[audio] Could not close microphone: {error}", flush=True)
            self.recording = False
