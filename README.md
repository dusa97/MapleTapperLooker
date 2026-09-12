# MapleTapperLooker

Detect `+<number>` values in a selected screen region with a compact dark Windows control window.

## Download the Windows executable

Download `MapleTapperLooker.exe` from [GitHub Releases](https://github.com/dusa97/MapleTapperLooker/releases).
Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) separately. Python is not required for the executable.

Each push to `main` runs tests, builds the executable, and publishes a new release.
You can also run **Windows release** manually from the **Actions** tab on `main`.
The workflow uses `MapleTapperLooker.spec` and publishes only after tests and the build succeed.

## Run on Windows

Install Tesseract OCR and Python dependencies:

```powershell
python -m pip install -r requirements.txt
pythonw main.py
```

Double-click **MapleTapperLooker.pyw** to open only the application UI and region overlay, without a terminal.
The packaged **MapleTapperLooker.exe** also runs without a terminal.
Use `python main.py` only if you want terminal diagnostics.

## Select the detection region

1. Press and release **F8** to hide the current box and start a new selection.
2. Click-drag over the area to detect. Release to save the region. Detection stays paused.
3. Drag the green corner handles to resize the box, or drag its border to move it.

Each F8 press clears the selection, including an unfinished drag. **Escape** cancels and restores the previous region.
Selections must be at least 20 × 20 pixels. Smaller drags let you try again.
The selector covers all monitors. Detection pauses during selection; Enter and click automation stops and stays off.
Press **F9** to restart automation after selection.
F8 works globally, including with `--no-enter-spam`, and does not reach other programs.
On first launch or with `--reselect`, click-drag to select the initial region. Escape exits this initial selector.

## Control window

The application opens a normal dark Windows window (not a terminal) with detection status, a safe **Start watching** countdown, region selection, audio controls, and the six most recent detections or errors.

- **Start watching** waits three seconds. Click your game target during the countdown to focus the game. Start cancels if the control window still has focus. Press and release **F9** to start/stop. Focus the game before starting.
- **Choose region** (or **F8**) opens the screen selector. Drag the green overlay border to move it and a corner to resize it.
- If Tesseract is missing or unavailable, its diagnostic appears in Recent activity instead of a hidden console.
- Close the control window to exit. Double-clicking the overlay does not close the application.

## Record a detection message

1. Press and release **F11** to start recording from the default microphone.
2. Speak after Recent activity shows `REC - F11: stop`.
3. Press and release **F11** again to save the message.
4. Detect a `+<number>` value to hear the message instead of the beep.
5. Repeat the recording steps to replace the saved message.

Press and release **F12** to delete the saved message and restore the original beep.
F12 also stops message playback and cancels an unfinished recording. No restart is needed.
If deletion fails, Recent activity shows an error; press F12 again after correcting the problem.

**F10** mutes both the message and the beep. F12 does not change this mute setting.
**F9** toggles OCR, detection audio, and Enter/click automation. All start paused.
A confirmed detection plays one alert and pauses detection until you press F9 again.
With `--no-enter-spam`, F9 controls detection without Enter/click automation.
Audio hotkeys also work with `--no-enter-spam`.
The application blocks F11 and F12 from reaching other programs while it runs.

The recording stays in `detection_message.wav` beside the script or executable, including after restart.
A failed recording keeps the previous message. Detection audio is silent while recording.
Before a message is saved, detection uses the original beep.
Closing the program during recording discards the unfinished replacement.

Recording uses Windows audio APIs. No additional Python dependency is required.
Enable microphone access for desktop applications in Windows Settings if recording fails.
The application folder must permit file writes.

## Repository layout

- `main.py`, `recorded_alert.py`, `MapleTapperLooker.pyw`: application and launcher.
- `assets/`: application icons.
- `tests/`: Python tests and release-note checks.
- `.github/workflows/`: release automation.
- `MapleTapperLooker.spec`, `requirements.txt`: build settings and dependencies.

## Check

Run from the repository root:

```powershell
python -m unittest discover -v
powershell -NoProfile -File tests/test_release_notes.ps1
```

Tests mock audio devices. Check microphone recording and playback manually with the F11 steps above.
