# Victoria II Modern Launcher (Web Prototype)

A lightweight dark-mode launcher UI prototype that preserves core Victoria II launcher tasks while modernizing the experience.

## Included Features

- Auto-detect `.mod` descriptors from `Victoria 2/mod`.
- Enable/disable mods, search/filter list, and drag/drop load order.
- Basic conflict hints:
  - Missing dependencies (`dependencies={}`)
  - Shared `replace_path` collisions
- Save/load/delete named mod presets.
- Optional auto-load last preset.
- Session memory for selected mods and launch options.
- Launch payload event (`window` event name: `v2-launch`) for native app bridge integration.

## Run

Because this uses browser module scripts and the File System Access API, run it under a Chromium-compatible host:

```bash
python3 -m http.server 4173
# open http://localhost:4173
```

> In a packaged app (Tauri/Electron/.NET WebView), connect the `v2-launch` event to your native process launcher for `v2game.exe`.

## Notes

- `version.txt` is used to infer game version/checksum when available.
- For true executable launching and deeper checksum logic, wire this UI to your host environment APIs.
