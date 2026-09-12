# MapleTapperLooker

Detect `+<number>` values in a selected screen region.

## Run on Windows

Install Tesseract OCR and Python dependencies:

```powershell
python -m pip install -r requirements.txt
python main.py
```

## Record a detection message

1. Press and release **F11** to start recording from the default microphone.
2. Speak after the console shows `REC - F11: stop`.
3. Press and release **F11** again to save the message.
4. Detect a `+<number>` value to hear the message instead of the beep.
5. Repeat the recording steps to replace the saved message.

**F10** mutes both the message and the beep. **F9** toggles Enter and click automation.
Audio hotkeys also work with `--no-enter-spam`.

The recording stays in `detection_message.wav` beside the script or executable, including after restart.
A failed recording keeps the previous message. Detection audio is silent while recording.
Before a message is saved, detection uses the original beep.
Closing the program during recording discards the unfinished replacement.

Recording uses Windows audio APIs. No additional Python dependency is required.
Enable microphone access for desktop applications in Windows Settings if recording fails.
The application folder must permit file writes.

## Check

```powershell
python -m unittest -v test_beep test_recorded_alert
```

Tests mock audio devices. Check microphone recording and playback manually with the F11 steps above.
