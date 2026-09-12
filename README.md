# MapleTapperLooker

Detect `+<number>` values in a selected screen region.

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
python main.py
```

## Select the detection region

1. Press and release **F8** to hide the current box and start a new selection.
2. Click-drag over the area to detect. Release to save the region and resume detection.
3. Drag the green corner handles to resize the box, or drag its border to move it.

Each F8 press clears the selection, including an unfinished drag. **Escape** cancels and restores the previous region.
Selections must be at least 20 × 20 pixels. Smaller drags let you try again.
The selector covers all monitors. Detection pauses during selection; Enter and click automation stops and stays off.
Press **F9** to restart automation after selection.
F8 works globally, including with `--no-enter-spam`, and does not reach other programs.
On first launch or with `--reselect`, click-drag to select the initial region. Escape exits this initial selector.

## Record a detection message

1. Press and release **F11** to start recording from the default microphone.
2. Speak after the console shows `REC - F11: stop`.
3. Press and release **F11** again to save the message.
4. Detect a `+<number>` value to hear the message instead of the beep.
5. Repeat the recording steps to replace the saved message.

Press and release **F12** to delete the saved message and restore the original beep.
F12 also stops message playback and cancels an unfinished recording. No restart is needed.
If deletion fails, the console shows an error; press F12 again after correcting the problem.

**F10** mutes both the message and the beep. F12 does not change this mute setting.
**F9** toggles Enter and click automation.
Audio hotkeys also work with `--no-enter-spam`.
The application blocks F11 and F12 from reaching other programs while it runs.

The recording stays in `detection_message.wav` beside the script or executable, including after restart.
A failed recording keeps the previous message. Detection audio is silent while recording.
Before a message is saved, detection uses the original beep.
Closing the program during recording discards the unfinished replacement.

Recording uses Windows audio APIs. No additional Python dependency is required.
Enable microphone access for desktop applications in Windows Settings if recording fails.
The application folder must permit file writes.

## Check

```powershell
python -m unittest -v
```

Tests mock audio devices. Check microphone recording and playback manually with the F11 steps above.
