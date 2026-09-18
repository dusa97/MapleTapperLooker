# Domain context

## Discord detection alerts

Status: approved design. No production behavior is implemented here.
Parent specification: https://github.com/dusa97/MapleTapperLooker/issues/16.
Implementation ticket: https://github.com/dusa97/MapleTapperLooker/issues/17.

- **Confirmed hit:** a positive OCR result accepted by the existing confirmation loop. A hit pauses detection and automated input.
- **Detection region:** the user-selected screen area read by OCR. This is not the Discord image scope.
- **Watching activation:** one interval started by F9 or the Start countdown and ended by pause, selection, error, or a confirmed hit.
- **Target window:** the external foreground window bound when a watching activation starts. Never replace it after focus changes.
- **Game-only image:** a window-targeted capture of the target window. Never a cropped desktop screenshot.
- **Discord recipient:** the configured Discord user mentioned in a server-channel webhook message. This is not a direct message.
- **Discord alerts:** a remembered opt-in checkbox independent of detection audio mute. First-use default is off.

Accepted product decisions: private-channel mention; full target-window image; remembered checkbox; settings dialog; test alert; foreground target binding; text-only image fallback; window-only capture; Windows user-bound encryption; asynchronous delivery without automatic retry.

Notification sound and device banners depend on the recipient's Discord and operating-system settings. Channel access is required.
Game content, including character names and chat rendered within the target window, can appear in the image.
Existing OCR desktop capture and debug-image storage remain unchanged. The new privacy boundary applies to Discord image attachments.

See [the design specification](docs/design/discord-hit-alerts.md), [capture decision](docs/adr/0001-discord-window-capture.md), and [dated evidence](docs/research/2026-09-18-discord-window-capture.md).
