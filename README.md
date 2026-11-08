# Dusklight Save Sync

A Decky plugin and companion watcher for syncing Twilight Princess save data with a GitHub repository and showing save sync status in Game Mode.

## Status

Version 1.0.2. This project is intended for a local Steam Deck setup and remains experimental. Back up save data independently before relying on synchronization.

## Contents

- `plugin/`: Decky plugin (`main.py`, `dist/index.js`, and `plugin.json`).
- `save_watcher.py`, `save_sync.py`, `watcher_notifications.py`: companion watcher and sync logic.
- `config.example.json`: sample configuration. Set the repository to your own private save repository and configure credentials locally; never commit credentials or save files.
- `dusklight-save-watcher.service.example`: optional user service template.

The plugin reads local watcher status and events. Install/configure the watcher separately for your SteamOS environment. Review the scripts and adapt paths and service settings before use.

## Development checks

Run `python3 -m unittest test_watcher.py` and `node test_plugin.mjs` from this directory. Python sources can be syntax checked with `python3 -m py_compile save_sync.py save_watcher.py watcher_notifications.py plugin/main.py`.
