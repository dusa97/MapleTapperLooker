# Add private Discord hit alerts with game-only images

Status: approved implementation ticket.
Issue: https://github.com/dusa97/MapleTapperLooker/issues/17.
Design-artifact PR: https://github.com/dusa97/MapleTapperLooker/pull/18.

## Parent and blockers

Parent specification: https://github.com/dusa97/MapleTapperLooker/issues/16.
Blocking tickets: none. Use native GitHub sub-issue linkage to the parent.
Size: one medium-sized vertical feature, delivered in one implementation PR.
Do not publish an intermediate text-only feature or split work into UI, HTTP, and capture layer tickets.

## Implementation scope

Implement the complete behavior below. Keep the parent specification as the authoritative approved contract.
Carry the accepted design files into the implementation PR if they are not on main. Do not merge the design PR as part of this ticket.
Do not copy prototype code directly into production. Keep its branch separate as evidence.
Start with a failing behavior check at the agreed seams, then implement and run the full listed checks.
Update README setup, privacy, limitations, troubleshooting, and source dependency instructions.

## Behavior and acceptance contract


Original goal: "i want to add a checkbox like the mute button that its purpose will be to send discord image to the one using the tool and alert him via discord that he hit something"

Send one private-server-channel message that mentions the configured user after a confirmed hit.
Attach a full game-only window image when available. Otherwise send the detected value with an image-unavailable notice.
Remember the opt-in checkbox independently of audio mute. Provide settings and a test alert.
Bind the external foreground window when watching starts. Do not follow later focus changes.
Use Windows user-bound encryption, asynchronous delivery, and no automatic retries.

## Classification and scope

Medium implementation complexity; high privacy impact because image upload cannot be recalled by the app.
Affected systems: Tk controls, confirmation flow, activation identity, Windows capture, local settings, HTTP, shutdown, dependencies, and PyInstaller packaging.
UI and local settings are reversible. Uploaded images and mentions are externally visible side effects.
No Wayfinder map is needed. One capture ADR records the surprising dependency and privacy trade-off.
One prototype answers capture feasibility; a single vertical implementation ticket delivers the feature without a partial text-only release.

## User interface and settings

- Add a labeled, keyboard-operable `Discord alerts` checkbox styled like the mute switch, in a distinct notification row near the audio controls.
- First use is off. Save the enabled state across restarts. F10 changes only audio; Reset changes only recorded audio.
- Add `Discord settings` with labeled Webhook URL and Discord user ID fields, Save, Cancel, Clear settings, and `Send test alert`.
- Mask the webhook field. Show validation errors beside the field and delivery state in Recent activity. Do not display secrets in messages.
- Explain that this posts to a server channel, mentions one user, and can include game chat. It is not a DM or guaranteed device notification.
- The test button sends a labeled text-only test mention using validated saved settings. It never captures the settings window or starts detection.
- Save settings under `%LOCALAPPDATA%/MapleTapperLooker/discord-settings.bin` as a user-scoped DPAPI-protected JSON payload.
- Encrypt the JSON in memory before any disk write. Write only ciphertext to the temporary file, then atomically replace the saved file.
- Remove ciphertext temporary files on failure. Never stage plaintext JSON on disk. Retain previous settings and show a sanitized error on failure.
- Missing, corrupt, or undecryptable settings leave Discord off.
- Enabling requires valid settings. Clear settings disables Discord and removes the saved file. Cancel leaves settings unchanged.
- Configuration stays local. Never bundle it into an executable, commit it, or include it in diagnostic output.

## Confirmed-hit behavior and target identity

- Use the existing confirmation branch in `OverlayApp._ocr_loop`, not the first OCR candidate, image-save callback, or audio callback.
- Keep detection and automation pause independent of notification success. Pause before capture or network work. Never resume due to notification failure.
- Capture an external foreground top-level HWND, PID, and process creation time when an activation actually begins, including after the Start countdown.
- Track the target window's destruction during the activation. Invalidate it permanently if destroyed; HWND reuse must not restore validity.
- Reject the tool's own windows, desktop/shell windows, invalid handles, and targets that do not contain the watched region.
- A missing capture target does not disable detection or text notifications. Inform the user that images are unavailable for this activation.
- Never persist the HWND or recover a target by title. On each new activation, bind a new target. Later focus changes do not retarget.
- Snapshot the confirmed value, activation, target identity, and settings revision. Revalidate identity in both parent and helper before and after capture.
- Inside the helper, verify HWND, PID, and process creation time immediately before creating the capture item and after acquiring its frame.
- If identity cannot be checked, changes, or was invalidated by window destruction, discard the image and use text fallback.
- A confirmed hit while Discord is off produces no new Discord capture or network traffic. This does not change existing OCR/debug capture.
- Muting audio does not suppress Discord. Recording audio does not suppress Discord. Rejected, stale, cancelled, or unconfirmed OCR reads never send.

## Image contract

- Use only Windows.Graphics.Capture for the explicit validated target HWND. Do not supply a monitor target or a title substring.
- Use `windows-capture==2.0.1`, with cursor and secondary-window capture disabled and the default system capture border retained.
- Capture after confirmation; the image is not the exact earlier OCR frame. Do not use cached frames from an earlier activation.
- Allow up to five seconds for a fresh capture, including helper startup. Copy valid frame content before the native frame is released.
- Run capture in a short-lived helper process with a hard timeout. Use a module-level capture entry function and a one-way multiprocessing Pipe.
- Dispatch `multiprocessing.freeze_support()` at the start of the executable entry point, before normal application imports, argument parsing, UAC, or Tk startup.
- Source execution uses the spawn context and the same import-safe capture function. The child must not construct OverlayApp, register hotkeys, access settings, or relaunch itself.
- Test actual spawned helpers in source and one-file modes, not only direct function calls. The frozen console-free build must transfer its result through the Pipe, not stdout.
- On timeout, terminate and reap the helper. Close also cancels/reaps it before destroying Tk. No persistent screenshot file is permitted.
- The helper receives only capture identity, never webhook credentials. Validate its input and cap returned dimensions and encoded bytes before accepting the result.
- Resize proportionally to a maximum 1920-pixel longest edge; encode as JPEG. Cap the attachment at 8 MiB; otherwise use text-only fallback.
- Use text-only fallback for missing, closed, minimized, unsupported, timed-out, zero-sized, all-black, or otherwise failed capture.
- A blank-frame check is a limited heuristic, not proof of correct game rendering. Verify intended-game output manually.
- Never substitute a desktop image, detection crop, another window, or an old image. Do not write the Discord image into debug captures.
- Game chat and overlays rendered inside the target can remain. External-window exclusion is not semantic redaction of game content.
- The existing OCR still reads visible desktop pixels. This feature does not make OCR work through covering windows.

## Delivery, limits, and cancellation

- Send the mention, detected value, and optional image in one webhook request. When absent, say `Game image unavailable`.
- Use `allowed_mentions` with only the configured user ID and no automatic mention parsing. Never allow role or everyone mentions.
- Accept only canonical HTTPS `discord.com/api/webhooks/<numeric-id>/<token>` URLs, optionally with `/v10` after `/api`.
- Reject userinfo, alternate hosts, ports, query strings, fragments, malformed paths, and invalid IDs before any network operation.
- Validate user IDs as positive decimal Discord snowflakes within unsigned 64-bit range. Require a nonempty token containing only URL-safe token characters.
- Use standard-library HTTPS with normal certificate verification. Disable redirects. Append `wait=true` internally.
- Use a ten-second HTTP timeout and one attempt only. A timeout can mean Discord received the message; do not automatically resend.
- HTTP acceptance means Discord confirmed a channel message, not that the user saw a device notification.
- Report pending, accepted, failed, unknown-after-timeout, or skipped states in Recent activity. Log no URL, token, recipient ID, response body, or raw exception.
- Keep at most one notification job active, including the test alert. If another hit occurs while busy, log that its Discord alert was skipped.
- Do not prevent F9 or normal detection while a notification runs. This bounded best-effort behavior avoids unbounded threads or queues.
- Use one lock for settings revision/enabled/closing changes and the final transition from pending to submitted.
- At that submission gate, verify the app is open, the job revision is current, and the job is still authorized. A disabled normal alert cannot pass.
- The explicit test button authorizes one test even if automatic alerts are off. Later disable, settings changes, or close still invalidate its pending job.
- If cancellation wins the gate, make no HTTP request. If submission wins, the request is in flight and cannot be recalled. Do not hold the lock during network I/O.
- Closing first invalidates pending work and cancels/reaps capture, then destroys Tk. No worker can update destroyed widgets or start another helper.
- All Tk updates remain on the UI thread through the existing activity queue. Capture and HTTP must not block Tk or the OCR loop.
- Do not add a Discord bot, hosted service, auto-retry queue, notification history, new hotkey, or acknowledgement workflow.

## Dependencies and compatibility

Add `windows-capture==2.0.1` for Windows x64. Replace `opencv-python-headless` with `opencv-python` to satisfy its declared dependency without duplicate cv2 providers.
Reuse Pillow, NumPy, existing UI colors, activity queue, and current test framework. Do not add a requests client or general notification abstraction.
Window capture requires Windows 10 version 1903 or later and working graphics support. Unsupported capture uses text-only delivery.
Non-Windows source runs must not import the Windows capture package eagerly; disable Discord configuration where DPAPI is unavailable.
Verify PyInstaller native-library collection and the existing OpenCV binary exclusion. No release workflow expansion is required.

## Public test seams and acceptance

Use three behavior seams, with standard-library unittest and mocks at external boundaries:

1. **Settings/UI:** checkbox starts off and persists; mute remains independent; validation precedes I/O; save failure preserves settings; corrupt state disables Discord. Exercise keyboard access, masked input, clear/cancel, and text-only test alert.
2. **Confirmed-hit notification:** one eligible hit schedules one job after pause; no send for rejected/stale/cancelled reads or disabled state. Test fresh activation binding, focus changes, no automatic resume, and busy skip. Exercise both orderings of cancellation versus the submission gate, settings changes, and shutdown.
3. **Capture/delivery boundary:** explicit HWND only; no desktop fallback; fresh-frame limits; helper-side PID/creation-time mismatch and window destruction/reuse; source/frozen dispatch and timeout cleanup; ciphertext-only writes; text fallback; allowed mention; URL/redirect rejection; sanitized success, 429, server error, and ambiguous timeout without retry.

Run `python -m unittest discover -v` and `powershell -NoProfile -File tests/test_release_notes.ps1`.
Build with Python 3.11 and `python -m PyInstaller --clean --noconfirm MapleTapperLooker.spec`.

Manual Windows acceptance, in source and packaged mode:

- Use synthetic windows and a separate application covering the target. Confirm the attachment contains only the target; verify focus changes do not retarget.
- Test the intended game in its supported display mode, including elevated operation. Confirm a full-window image, or clearly report capture unavailable.
- Test target closure/minimization, network loss, app close during capture/send, and a restart with saved settings. Confirm no orphan helper.
- With privately configured credentials, send one labeled test and one controlled confirmed-hit alert. Verify the channel mention and attachment. Never publish credentials or game screenshots.
- Verify OCR, auto-location, recorded alerts, and normal controls after the OpenCV distribution change. Attach only synthetic UI evidence to the implementation PR.

No real Discord request, actual-game capture, or full-app packaging check has been completed during design.
The prototype proves only Python/native packaging feasibility and synthetic occluder exclusion.
If actual-game images fail, do not silently weaken privacy. Keep the text fallback and document the unsupported mode.

## Design provenance


Parent specification: https://github.com/dusa97/MapleTapperLooker/issues/16.
Goal: the original user goal quoted above.
Accepted rounds: 1A/2B/3B; 1A/2A/3A; B plus the encryption, async/no-retry, and test defaults.
Final approval includes this specification, the capture ADR/dependency choice, test seams, one-ticket size, absence of blocking edges, and the full publication manifest.
Non-goals: production implementation during design; bots/DMs; desktop uploads; OCR changes; retries; guaranteed notification sound; game-independent capture guarantees.
Seams: settings/UI, confirmed-hit notification, capture/delivery, plus manual Windows acceptance.
Repository baseline: `6981e05d059d546e17bcf0d710ca6397e9ec3b69`.
Source pointers: `main.py` OverlayApp._build_window, _set_enter_on, _ocr_loop, _quit; `tests/test_beep.py`; `tests/test_control_window.py`; `requirements.txt`; `MapleTapperLooker.spec`; `.github/workflows/release.yml`.
Guide: `C:/Users/zvika/.pi/agent/docs/workflows.md` at configuration commit `8349bd19c5d4972847647eafef05009feff5d345`.
Upstream guide reference: [mattpocock/skills at 3cca18b368ae95cdbdebbff572ccafa662551015](https://github.com/mattpocock/skills/tree/3cca18b368ae95cdbdebbff572ccafa662551015).
Research date: 2026-09-18; [primary sources and prototype results](../research/2026-09-18-discord-window-capture.md).
Prototype immutable commit: [57bb21ebdc480e18b90f8813df2fc5cc0a43e65b](https://github.com/dusa97/MapleTapperLooker/commit/57bb21ebdc480e18b90f8813df2fc5cc0a43e65b).
Fresh read-only review identified helper identity checks, frozen entry dispatch, an atomic submission gate, and encrypt-before-write storage. These requirements are included above.
No full transcript, secret, personal image, or unredacted trace is a publication artifact.
