# Live acceptance checklist (real Telegram account, Windows)

Everything in `python -m pytest -q` runs offline. This checklist covers what
**cannot** be automated in CI: a live session, real Telegram limits, DPAPI on
Windows, and the installed build. Walk it once on a Windows 10/11 x64 machine
with the intended Telegram account before calling a build shippable.

Use a Telegram account you control. Keep a throwaway storage group for the
destructive steps (they are marked). Never paste login codes or the 2FA
password into logs, chat, or issue trackers.

## 0. Setup

| Step | Expected |
| --- | --- |
| `TELOUDE_API_ID` / `TELOUDE_API_HASH` set to your own my.telegram.org values | App starts; no credential block on the dashboard |
| `pip install -e ".[dev]"`, then `python -m teloude.main` | First-run wizard appears |
| Run the app twice | Second run starts straight into the dashboard |

## 1. Sign-in and session protection

| # | Action | Expected |
| --- | --- | --- |
| 1.1 | Enter the phone number in international format | "Code sent" appears; the code arrives in Telegram (not SMS) |
| 1.2 | Enter a wrong code | Inline error "The code is incorrect…"; no traceback, no code in the log |
| 1.3 | Enter the correct code (account with 2FA) | 2FA prompt appears; the app is *not* signed in yet |
| 1.4 | Enter the 2FA password | Dashboard appears; tray icon turns active |
| 1.5 | Exit the app, check `%APPDATA%\Teloude\sessions` | `.session` file present; opening it in a text editor shows no readable phone number or auth key fragment (DPAPI) |
| 1.6 | Start the app again | Unlocks silently; no code or password prompt |
| 1.7 | Settings → lock session, restart | Session unlocks; Telegram shows one active session, not a new one per launch |
| 1.8 | Settings → sign out | Dashboard returns to the sign-in state; the session file is gone; Telegram's active-sessions list no longer shows Teloude |

## 2. Storage lifecycle

| # | Action | Expected |
| --- | --- | --- |
| 2.1 | Create a storage named `Soak Test` | A private forum supergroup "Teloude Backup — Soak Test" appears in Telegram, topics enabled |
| 2.2 | In Telegram, add a topic manually, then press Refresh in Teloude | The manually created topic appears as a folder (adoption), files are not duplicated |
| 2.3 | Rename that topic in Telegram, refresh | The local folder name follows Telegram (Telegram is the source of truth for topic titles) |
| 2.4 | `Adopt existing` with the same group name on a second PC (or after deleting `%APPDATA%\Teloude`) | The storage links up and the file index is rebuilt from Telegram messages |

## 3. Backup engine (soak)

Create a few thousand small files plus one file >2 GiB and one ~4 GiB file (the
free tier limit) in a folder you can lose.

| # | Action | Expected |
| --- | --- | --- |
| 3.1 | Start the backup; watch progress | Byte/speed counters advance; the current file name is shown; UI stays responsive |
| 3.2 | Leave it running through a network drop (disable Wi-Fi for ~30 s) | Transfers show "waiting for network", then continue; no file is reported as failed because of a pause |
| 3.3 | Pause mid-file, wait, Resume | Progress continues from the same point; the file *is not* re-uploaded from the beginning (verify: the transfer's byte counter does not jump back to 0) |
| 3.4 | Cancel a run, then start it again | Cancel keeps already-finished files marked as backed up; the rest upload normally |
| 3.5 | Duplicate case: back up a folder twice with `Ask` | Skip / Upload again / Cancel dialog appears; "apply to all" is honored for the rest of the run |
| 3.6 | Compare one uploaded file in Telegram (`soak` folder → message) with the local original | Same name, same size, downloadable |
| 3.7 | Files above the account limit | Rejected up front with an actionable message, not a mid-upload failure |
| 3.8 | Interrupt mid-upload (kill the process from Task Manager), restart the app | Startup recovery marks stale transfers and requeues intact files; a subsequent run finishes them |
| 3.9 | Lock one file (open it in Word/Excel) and back up its folder | That file is listed as failed with a readable reason; every other file uploads normally |
| 3.10 | Back up a folder you do not have read permission for | Skipped; the run continues and the skipped item is reported |
| 3.11 | Edit a file while its backup is running | The newer content is what ends up in Telegram (the file is re-hashed, never uploaded stale) |

## 4. Restore

| # | Action | Expected |
| --- | --- | --- |
| 4.1 | Restore one file, two files, and a whole folder subtree into an empty folder | Byte-identical results (compare with `certutil -hashfile` or `fc /b`) |
| 4.2 | Restore again into the same non-empty folder | Identical files are skipped silently (no prompt); different content prompts Skip / Overwrite / Keep both / Cancel |
| 4.3 | Choose "Keep both" | Original untouched; the restored copy gets a suffixed name |
| 4.4 | Cancel mid restore | Restore stops; already restored files remain; nothing is deleted |
| 4.5 | Disconnect the network mid-restore, reconnect | Download resumes at the last offset (file size keeps growing, never restarts from 0) |
| 4.6 | Restore a file while its Telegram message is deleted | Clear failure message for that file; the rest of the batch continues |
| 4.7 | Restore into a read-only folder / write-protected USB stick | Message names the permission problem; it must NOT talk about the network |
| 4.8 | Restore onto a drive with almost no free space | "Not enough disk space" before downloading; nothing half-written is left behind |
| 4.9 | Restore into a path that is a file, or where a file blocks a sub-folder | Refused with a specific message; the blocking file is untouched |

## 5. Duplicates, search, preview

| # | Action | Expected |
| --- | --- | --- |
| 5.1 | Search a filename fragment and a path fragment | Matching files with their storage/folder and backup time |
| 5.2 | Preview a text, image, PDF, and video file | Text/image/PDF render; video shows a frame or a clear "preview unavailable" note |
| 5.3 | Preview a file with non-ASCII characters in its name | Renders; no mojibake |
| 5.4 | Trigger a restore from a search result | Restore dialog opens with that file preselected |

## 6. Speed limits, tray, packaging

| # | Action | Expected |
| --- | --- | --- |
| 6.1 | Set a speed limit (e.g. 1 MB/s), run a backup | Measured speed stays at or below the limit within ~10%; clearing the limit removes the cap |
| 6.2 | Change the limit mid-transfer | Takes effect on the next chunk smoothly, no stall |
| 6.3 | Close the window while a backup runs | App keeps running in the tray (or prompts); transfers continue |
| 6.4 | Autostart: relaunch with `--minimized` (or via the installer's autostart) | Starts hidden in the tray; no window flash |
| 6.5 | Install `Teloude-Setup-*.exe` on a clean VM, sign in, run one backup | Works from Program Files; no missing DLL; Defender/SmartScreen behavior noted |
| 6.6 | Uninstall | App removed; `%APPDATA%\Teloude` (index/logs) preserved |
| 6.7 | Reinstall and sign in | Previous storages and index are visible again |

## 7. Log hygiene

| # | Action | Expected |
| --- | --- | --- |
| 7.1 | Read `%APPDATA%\Teloude\logs\teloude.log` after the whole session | No login code, no 2FA password, no auth key, no session blob, no message contents |
| 7.2 | Trigger a Telegram rate limit (bulk upload) | Message explains the automatic retry; the app does not crash or spin |

## Reporting a failure

Record: step number, the exact user-facing message, the relevant log lines
(redacted), the Telethon version, the Windows version, and whether the failure
was reproducible. Do not attach session files.
