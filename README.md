# AntiCompress

Steam-style streaming download + decompression for Windows. The archive never
exists on disk alongside its content **you only ever need room for the
final game, never the game + its archive.**

## TL;DR for dummies (like me)

**The problem:** you download a 60 GB game in a 50 GB zip, and your PC says
"you need 110 GB to install this" — because it wants to keep the zip AND the
game. Dumb.

**The fix:** AntiCompress unpacks the zip *while* it downloads, so you only
ever need room for the game itself (60 GB, not 110 GB).

**How to use it (the lazy way):**

1. Download `anticompress.exe` from the [releases page](https://github.com/JinKaZh1/AntiCompress/releases)
2. Run it once and type: `anticompress install-bridge` (one-time setup)
3. In Firefox: `about:debugging` -> **This Firefox** -> **Load Temporary
   Add-on** -> pick the `manifest.json` it tells you about
4. Click any `.zip` download link -> a window pops up -> press **Enter** ->
   done. Game lands in your Downloads folder, already unpacked.

That's it. That's the whole thing.

**Also works without Firefox:** `anticompress dl https://link-to-file.zip -o "D:\Games\MyGame"`

**Not sure about something?** Press `Ctrl+C` to pause anything. Close the
window to cancel. Re-click the same link later and it continues where it
stopped. You can't break it.

---

## Install


```
pip install -r requirements-dev.txt
pip install -e .
```

Prerequisite for `repack`: [7-Zip](https://www.7-zip.org/) (`winget install 7zip.7zip`).

## Usage

```
# Convert a RAR/7z/zip into a .acpkg chunk folder (single pass, solid-safe;
# multi-part volumes are deleted as 7z passes them)
anticompress repack game.rar -o game.acpkg

# Stream-download + extract with parallel verified chunks (1x disk space)
anticompress dl https://host/game.acpkg -o "D:\Games\Game"

# Plain zip/tar.gz URLs work too — no repack needed
anticompress dl https://host/game.zip -o "D:\Games\Game"

# Extract a local .acpkg folder (chunks deleted as consumed)
anticompress install game.acpkg -o "D:\Games\Game"
```

## How it works

- `.acpkg` = folder of `manifest.json` (file list, per-file SHA-256,
  per-chunk SHA-256, self-hash) + `chunk-000000.zst` … (1 MiB decompressed
  each, zstd frames with content checksums).
- `repack` streams the source via `7z x -so` in one pass (works for solid
  archives), verifies the listing-order assumption against the archive's own
  CRCs, and deletes each multi-part volume the moment 7z passes it.
- `dl` fetches chunks in parallel, verifies each chunk's SHA-256 before
  decompressing, assembles files under `.acpart` temps, verifies per-file
  SHA-256, atomically renames, then deletes each chunk as consumed.
- Resume is free: files/chunks that exist and match their hash are skipped.

## Pause & resume

- **Ctrl+C** pauses any download cleanly (chunks and partial files stay on
  disk).
- **Closed the console by accident?** Just click the download link again and
  pick the **same destination folder** — downloads continue from where they
  stopped:
  - `.acpkg`: verified chunks are skipped automatically.
  - plain `.zip`: resumes at the last completed file via HTTP Range
    (state file `.anticompress-resume.json` in the destination; cleared on
    completion, ignored if the file's size doesn't match).
  - `.tar.gz` and friends: no resume (gzip streams have no sync points) —
    they restart.

## Known limitation

Solid multi-part RAR (FitGirl-style) cannot be streamed from a partial
download — no tool can, the format forbids it. Repack them once (works even
on a disk that only fits the final game, thanks to volume deletion), then
they stream forever.

## Firefox bridge (optional)

Click a download link and get a terminal chooser instead of a browser download:
stream it with AntiCompress (1x disk space) or download normally.

```
# one-time install (writes HKCU registry key, no admin needed)
anticompress install-bridge

# then load the extension in Firefox:
#   about:debugging -> This Firefox -> Load Temporary Add-on ->
#   %APPDATA%\anticompress\extension\manifest.json
```

The extension cancels the browser download the instant it starts (a few KB
max) and hands the URL to the CLI. Solid RAR/7z links still work — pick
"Normal download" for those, then `anticompress repack` them (see above).
Right-click a link -> "Download with AntiCompress" also works.
