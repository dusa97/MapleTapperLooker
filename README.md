<p align="center">
  <img src="assets/logo.png" alt="MapleTapperLooker logo" width="160">
</p>

<h1 align="center">MapleTapperLooker</h1>

<p align="center">
  A Windows screen watcher that detects <code>+&lt;number&gt;</code> values, plays an alert, and stops automated input.
</p>

<p align="center">
  <a href="https://github.com/dusa97/MapleTapperLooker/releases/latest">Download for Windows</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="https://github.com/dusa97/MapleTapperLooker/issues">Report an issue</a>
</p>

<p align="center">
  <a href="https://github.com/dusa97/MapleTapperLooker/actions/workflows/release.yml"><img src="https://github.com/dusa97/MapleTapperLooker/actions/workflows/release.yml/badge.svg" alt="Windows release workflow status"></a>
</p>

## Overview

Select a screen region, such as a game's Combat Power Change field. MapleTapperLooker uses Tesseract OCR to detect positive values such as `+538,683`.

- Move or resize the detection region across multiple monitors.
- Start and stop detection with global keyboard shortcuts.
- Automate Enter presses and left-clicks, or use detection alone.
- Hear a beep or your recorded message when a value is detected.
- Optionally send a private Discord server-channel mention with a game-window image.
- View status and the six most recent detections or errors in a compact dark window.

A confirmed detection plays one alert and pauses detection and automated input. Press **F9** to resume.

### Screenshot

<p align="center">
  <img src="assets/screenshot.png" alt="MapleTapperLooker paused control window with region selection, recording, mute, reset, and recent activity controls" width="540">
</p>

*The actual Windows control window, shown with detection paused.*

## Quick start

**Requirements:** Windows and [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki). The executable does not require Python.

1. Install Tesseract OCR. The application checks `PATH` and `C:\Program Files\Tesseract-OCR\tesseract.exe`.
2. Download `MapleTapperLooker.exe` from the [latest release](https://github.com/dusa97/MapleTapperLooker/releases/latest).
3. Put the executable in a folder that permits file writes. Open it; Windows can request administrator permission.
4. On first launch, click-drag over the number to watch. Release to save the region; detection stays paused.
5. Click **Start watching**, then click your game target during the three-second countdown. Alternatively, focus the game and press **F9**.

> [!WARNING]
> By default, starting detection also sends repeated Enter presses and left-clicks.
> Focus the intended target before starting. Press **F9** to stop, or close the control window to exit.
> Use automation only where the target application's rules permit it.

For detection without automated input, run this command from the executable's folder:

```powershell
.\MapleTapperLooker.exe --no-enter-spam
```

The control window cancels a start if it still has focus. Detection and automated input start paused on each launch.

## Controls

Press and release each hotkey.

| Key | Action |
| --- | --- |
| **F8** | Pause detection and select a new region. |
| **F9** | Start or stop detection and, unless disabled, automated input. |
| **F10** | Mute or unmute both the beep and recorded message. |
| **F11** | Start recording; press again to save the message. |
| **F12** | Delete the message and restore the beep. Cancel any unfinished recording and stop message playback. |

All five hotkeys work with `--no-enter-spam`. The application blocks F8, F11, and F12 from reaching other programs.

### Select or adjust a region

1. Click **Choose region** or press **F8** to hide the current box and open the selector.
2. Click-drag over the number. Select an area of at least 20 × 20 pixels; smaller drags allow another attempt.
3. Drag the violet border to move the saved box, or drag a corner handle to resize it.
4. Focus the target application and press **F9** to resume.

Each F8 press clears the current selection attempt, including an unfinished drag. **Escape** cancels selection and restores the previous region.
On first launch, or with `--reselect`, Escape exits the initial selector instead.

The selector covers all monitors. Selection pauses detection and stops automated input; neither restarts automatically.
The region is saved in `last_region.json` beside the script or executable.
Double-clicking the overlay does not close the application; close the control window instead.

### Record a detection message

1. Press **F11** to record from the default microphone.
2. Wait for `REC - F11: stop` in Recent activity, then speak.
3. Press **F11** again to save the message.
4. Start detection to hear the message on the next confirmed match.

Repeat to replace the message. Press **F12** or click **Reset** to restore the beep without restarting.
Reset does not change the mute setting.

The message stays in `detection_message.wav` beside the script or executable, including after restart.
A failed recording keeps the previous message. Closing the application during recording discards the unfinished replacement.
Detection audio is silent during recording. Recording uses Windows audio APIs and needs no additional Python dependency.

### Discord alerts

Discord alerts start off. Click **Discord settings** to save a Discord webhook URL and one Discord user ID, then enable **Discord alerts**. The test button sends one text-only test mention. Audio mute and Discord alerts are independent.

A confirmed hit pauses detection before Discord capture or delivery. The app binds the foreground game window when watching starts and uploads only that full window. If Windows capture is unavailable, the alert says `Game image unavailable`; it never uploads the desktop, OCR crop, another window, or an old image.

Discord posts to a server channel and mentions the configured user. It is not a direct message and does not guarantee a device notification. Game chat and overlays inside the game window can appear in the image. The app makes one asynchronous request with no automatic retry. Closing the app cancels pending work and stops the capture helper. An already submitted request cannot be recalled.

Settings are saved only under `%LOCALAPPDATA%\MapleTapperLooker\discord-settings.bin`, protected for the current Windows user with DPAPI. Do not share the webhook URL or user ID. Clear settings disables alerts and removes this local file.

Discord images require Windows 10 version 1903 or later, Windows x64 graphics capture, and a supported game display mode. Protected content, exclusive fullscreen, minimization, HDR, or graphics failures can produce text-only alerts.

#### First-time Discord setup

Use the Discord desktop app or website for these steps. You need two values:

- **Webhook URL:** a secret address that lets this tool post to one server channel.
- **Discord user ID:** the numeric account identifier used to mention you. It is not your username or display name.

Messages go to a server channel, not a direct message. Anyone who can view that channel can see its alerts and images.
Use a private channel that your account can access. No bot or paid Discord subscription is required.

**1. Get a webhook URL**

If you need a server, click **+** in Discord's left sidebar, then select **Create My Own**.
Use a server you own, or ask its administrator for the **Manage Webhooks** permission in the chosen channel.

1. Open your server's menu and select **Server Settings**.
2. Open **Integrations**, then **Webhooks**.
3. Click **Create Webhook** or **New Webhook**, depending on what Discord shows.
4. Name it **MapleTapperLooker**, select your alert text channel, and save the changes.
5. Click **Copy Webhook URL**. Keep the copied address private.

The address starts with `https://discord.com/api/webhooks/`. Copy the entire address, not a channel link or server invite.

> [!WARNING]
> Treat the webhook URL like a password. Anyone with it can post to its channel.
> Never put it in screenshots, bug reports, or public chats.
> If you share it accidentally, delete that webhook and create a replacement. Update the app with the new URL.

**2. Get your Discord user ID**

1. Click the gear beside your Discord username to open **User Settings**.
2. Open **Advanced** and enable **Developer Mode**.
3. Find yourself in your server's member list, or use one of your own messages.
4. Right-click your username and select **Copy User ID**.

Copy your account's numeric ID, not the server, channel, or message ID. Developer Mode does not require programming knowledge.

**3. Connect the app and check it**

1. Open **Discord settings** in MapleTapperLooker.
2. Paste the two values into **Webhook URL** and **Discord user ID**, then click **Save**.
3. Reopen **Discord settings** and click **Send test alert**. Check your chosen channel for a text-only test mention.
4. Enable **Discord alerts** in the main window. The test button works even when this checkbox is off.
5. Focus the game and press **F9**. A confirmed hit sends the mention and game image when capture is available.

The test button never sends an image. Only a confirmed detection requests a game image.
If the image is unavailable, read the reason in **Recent activity**. Discord acceptance does not guarantee a phone notification.

Official Discord guides: [Create a webhook](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks) · [Find your user ID](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID).

## Run from source

Use Windows, Tesseract OCR, and Python. The release workflow builds and tests with **Python 3.11**.

1. Clone the repository:

   ```powershell
   git clone https://github.com/dusa97/MapleTapperLooker.git
   cd MapleTapperLooker
   ```

2. Create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. Start the application without a terminal:

   ```powershell
   .\.venv\Scripts\pythonw.exe main.py
   ```

Use `.\.venv\Scripts\python.exe main.py` for terminal diagnostics.
You can also double-click `MapleTapperLooker.pyw` if its associated Python installation has the required dependencies.
The packaged executable runs without a terminal.

### Command-line options

These options also work with `MapleTapperLooker.exe`.

| Option | Purpose |
| --- | --- |
| `--no-enter-spam` | Disable automated input; F9 still controls detection. |
| `--reselect` | Select a new region instead of loading the saved one. |
| `--region L T W H` | Set the region's left, top, width, and height in pixels. |
| `--no-elevate` | Skip the automatic administrator permission request. Hotkeys can fail when an elevated game has focus. |
| `--help` | List all options, including input intervals and configurable hotkeys. |

Example: select a new region and use detection without automated input.

```powershell
.\.venv\Scripts\python.exe main.py --reselect --no-enter-spam
```

## Troubleshooting

| Problem | Check or action |
| --- | --- |
| Tesseract is missing or unavailable | Read the diagnostic in **Recent activity**. Install Tesseract at the standard path or add it to `PATH`, then restart the application. |
| Start is cancelled | Click the game target during the countdown, or focus the game before pressing F9. |
| F9 does not work over an elevated game | Allow the application's administrator permission request. Do not use `--no-elevate` for that session. |
| Recording fails | Enable microphone access for desktop applications in Windows Settings. Check the default microphone and application folder write permissions. |
| Reset cannot delete the message | Read the error in **Recent activity**, correct the file-access problem, and press F12 again. |
| Discord cannot be enabled | Save a valid canonical `https://discord.com/api/.../webhooks/.../...` URL and a Discord user ID in **Discord settings**. |
| Discord image is unavailable | Focus the game and press F9 for a new activation. Closed, minimized, or unsupported targets use text-only alerts. |
| Discord status is unknown after timeout | Do not automatically repeat the alert. Discord may already have received it. |

## Development

### Run checks

Run from the repository root after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
powershell -NoProfile -File tests/test_release_notes.ps1
```

Tests mock audio devices and Discord network boundaries. Check microphone recording and playback manually with the [recording steps](#record-a-detection-message). Test Discord only with private credentials; do not commit settings or game images.

Run the synthetic capture check on Windows:

```powershell
python tests/synthetic_discord_capture.py
```

This check captures a synthetic window behind a separate-process covering window. It checks timeout cleanup and minimized/closed fallback without network requests.
For a console-free one-file check, build and run the same script:

```powershell
python -m PyInstaller --clean --noconfirm --onefile --noconsole --collect-all windows_capture --paths . tests/synthetic_discord_capture.py
$p = Start-Process dist/synthetic_discord_capture.exe -ArgumentList '--result', 'synthetic-capture-result.txt' -Wait -PassThru
if ($p.ExitCode -ne 0) { throw 'Synthetic capture failed.' }
Get-Content synthetic-capture-result.txt
```

The result must be `PASS`. No screenshot is written. These checks do not verify an intended game or a live Discord channel.

Existing source environments must remove the old OpenCV provider before installing the updated requirements:

```powershell
python -m pip uninstall opencv-python-headless
python -m pip install -r requirements.txt
```

### Build and releases

Each push to `main` runs tests, builds the executable with `MapleTapperLooker.spec`, and publishes a release only after success.
You can also run **Windows release** manually from the repository's **Actions** tab on `main`.

To build locally after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.16.0
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm MapleTapperLooker.spec
```

The executable is written to `dist/MapleTapperLooker.exe`. Tesseract must still be installed separately.

### Repository layout

| Path | Contents |
| --- | --- |
| `main.py`, `recorded_alert.py`, `MapleTapperLooker.pyw` | Application, recorded audio support, and launcher. |
| `assets/` | Application logo, icon, and README screenshot. |
| `tests/` | Python tests and release-note checks. |
| `.github/workflows/` | Release automation. |
| `MapleTapperLooker.spec`, `requirements.txt` | Build settings and dependencies. |

## Support and contributions

[Open an issue](https://github.com/dusa97/MapleTapperLooker/issues) for a bug report or feature request.
Include your Windows version, application release, reproduction steps, and relevant Recent activity messages.
Remove private information from screenshots before posting.

For code changes, keep pull requests focused and run the checks above. Include a screenshot when a change affects the control window.

## License

This repository does not currently include a license file. No open-source license is granted here; ask the owner before reuse or redistribution.
