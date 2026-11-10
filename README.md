# Dusklight Save Sync

Two-way sync of GameCube GCI saves between devices through a GitHub repository. Made for Dusklight (the
Twilight Princess PC port) on a Windows PC and a Steam Deck, but every game-specific value is in `config.json`,
so any program that keeps a GCI-folder memory card can use it.

Back up your saves independently before you rely on any synchronization.

## What it does

- Checks the local GCI files every 2 s. After a change it waits until the file has had no write for
  `quiet_seconds`, then checks the header, size and stability before it uploads.
- Polls GitHub every `cloud_poll_seconds`. A newer local save is uploaded even while the game runs.
  A newer cloud save is downloaded only after the game is fully closed (the game keeps the card in memory).
- Decides with SHA-256 first, then trusted commit metadata, then the last agreed version, then the GCI header
  date. Each upload commit carries one line:
  `Save-Sync: {"sha256":"<hash>","modified_ns":<original write time, Unix UTC ns>}`.
  The metadata is trusted only when its hash matches the file. A download keeps the original write time.
  Times are compared at NTFS precision (100 ns).
- Never forces a write: the upload sends the current GitHub blob SHA, and a concurrent change is refused.
  Identical content is never re-uploaded. Deletions are never propagated.
- Equal times with different content is a conflict: both versions go to `save-backups/` and nothing is replaced.
  Every replaced save is backed up first.
- Retries after a network failure, after the PC wakes and after a restart. A short glitch shows no popup;
  an outage longer than `alert_after_seconds` shows one, and the recovery shows another.
- One lock (`.save-sync.lock`) for every local synchronizer: the watcher, the launcher and `--once`.

## Files

| File | Role |
|------|------|
| `save_watcher.py` | The watcher: `--once`, `--status`, `--sync-now`, `--test-notification`, `--home <folder>` |
| `save_sync.py` | GitHub access, GCI validation, backups |
| `platform_support.py` | File locks, game detection, native popups (Windows toast, `notify-send` on Linux) |
| `watcher_notifications.py` | Event feed shared with the popups and the Decky plugin |
| `launcher.py` | Optional: rebuild the game image when a translation changed on GitHub, sync, start the game |
| `install-windows.ps1` | Windows: hidden autostart, restart after a crash, popups, Start menu shortcuts |
| `dusklight-save-watcher.service.example` | Steam Deck / Linux: systemd user service |
| `plugin/` | Decky plugin: sync state and events in Game Mode |

## Runtime folder

Config, state, logs and backups live in one runtime folder: `--home`, else `SAVE_SYNC_HOME`, else the code
folder (the Steam Deck layout). On Windows keep the code in a git checkout and the runtime folder outside it.

## Setup on Windows

1. Install Python 3.11+ and the [GitHub CLI](https://cli.github.com/). Log in once: `gh auth login`.
   The token stays in the Windows credential store; the watcher asks `gh auth token` for it and never logs it.
2. Create the runtime folder and copy `config.windows.example.json` there as `config.json`. Set the paths.
3. Run `powershell -ExecutionPolicy Bypass -File install-windows.ps1 -SyncHome <runtime folder>`.
   It registers the scheduled task "Dusklight Save Sync" (hidden, at logon, restarted within a minute),
   the popup name and the Start menu folder "Dusklight Save Sync". `-Uninstall` removes them and keeps the data.
4. Check: Start menu → "Тест сповіщення", then "Стан синхронізації сейвів". If Windows notifications are turned
   off, both say so; the events stay in the status and in `save-watcher.log`.

When you install from a packaged (MSIX) app, such as the Claude desktop app, do not put the runtime folder under
`%LOCALAPPDATA%` or `%APPDATA%`: the app's writes there go to a private copy that the scheduled task does not see.
Run the installer from a normal PowerShell window.

## Setup on the Steam Deck

Copy the files next to `config.json` (for example `~/Downloads/Twilight Princess/`), log in with `gh auth login`
(or keep a private `gh` config in `.github-auth/`), and install the systemd unit from
`dusklight-save-watcher.service.example` (`systemctl --user enable --now dusklight-save-watcher`).
Install the Decky plugin from `plugin/` for Game Mode status. All devices must run the same version of the code.

## Use with another game

Set these keys; nothing else is game-specific:

- `watcher.game_codes`: the 4-letter game codes allowed in the GCI header (default: Twilight Princess GC + Wii).
- `watcher.disc_id`: the disc ID whose saves are synced, for example `GZ2E01`.
- `watcher.process_names`: executable names that mean "the game is running" (default `["dusklight"]`).
- `watcher.quiet_seconds`, `watcher.cloud_poll_seconds`, `watcher.alert_after_seconds`: timing.
- `dusklight_user_dir`: the folder that holds `<Region>/Card A/*.gci`.
- `github_repo`, `branch`, `saves_repo_path`: where the saves live on GitHub.
- `game_title`, `device_name`: used in the commit message only.
- `notifications`, `notification_title`: popups.

## Launcher (optional)

`launcher.py --home <runtime folder>` updates a GameCube image from translation files on GitHub
(`launcher.translation_dirs`), but only when they changed. It rebuilds with a GameCube builder
(`launcher.builder`, for example GCRebuilder `gcr.exe`), verifies the result (same disc ID, translation hashes,
every other file and the DOL unchanged, not a Wii disc) and only then replaces the image. Then it syncs the
saves and starts the game. `--no-launch` updates and syncs only.

## Development checks

```
python -m unittest test_watcher
node test_plugin.mjs
```
