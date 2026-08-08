# AntiCompress — Design Spec

**Date:** 2026-08-08
**Status:** Approved by user (2026-08-08)

## 1. Purpose

Steam-style streaming download + decompression for Windows. When downloading a
compressed archive (zip, rar, 7z), the archive should never exist on disk
alongside its extracted content. **Downloaded = decompressed = done.**

The one-line promise: *you only ever need room for the final game, never the
game + its archive.*

Space math: need = final extracted size + small buffer. Never
download-size + extracted-size. The tool checks free space against the final
size **before downloading a single byte** and fails fast if insufficient.

**The core mechanism: everything on disk is consumable.** The package is a
*folder of independent chunk files*, so any copy of the data (source archive
parts, downloaded chunks) can be deleted the moment its bytes have been
consumed. Peak disk ≈ max(final size, remaining source + chunks written),
never a full duplicate.

## 2. Components

Three pieces, Python 3, Windows-first:

### 2.1 `anticompress repack <archive> -o <out.acpkg>`

Converts an existing RAR/7z/zip archive into a `.acpkg` chunk folder (chunked
zstd + manifest). Runs **once**, and thanks to delete-as-you-go it fits on
the machine that only has room for the final game — e.g. 90GB FitGirl parts
→ 120GB game on 140GB free works (see 2.4).

Pipeline: `[RAR/7z/zip] → 7z x -so (single pass, works for solid archives,
CRC-verified) → chunker (fixed 1 MiB pieces) → zstd per chunk (frame checksum
enabled) → [out.acpkg/ = manifest.json + chunk-NNNNNN.zst files]`

`7z x -so` emits every file's bytes concatenated in archive order in ONE
pass — the only way to repack solid RAR without O(n²) per-file extraction,
and avoids any temp extraction directory (which would need 3x space). File
offsets in the manifest come from `7z l -slt` sizes summed in listing
order; the repacker **sample-verifies** that order assumption (extract 2-3
files per top-level dir via `7z x <file> -so`, compare SHA-256 against the
package bytes at their computed offsets). Any mismatch → refuse the
package loudly. 7-Zip sets binary mode on piped stdout on Windows (covered
by round-trip tests).

**Part-deletion watchdog (multi-part RAR):** a background thread watches the
source parts; each `.partXX.rar` is deleted the moment 7-Zip stops reading it
(try-delete, retry on lock — best-effort, never fatal). Parts shrink as
chunks grow, so peak disk during repack ≈ source size, not 2x. If the
watchdog can't keep up (7-Zip holding parts open), it degrades gracefully to
"archive + package" — correct either way.

### 2.2 `anticompress dl <url> [-o <dest>]`

The Steam moment. Downloads and decompresses in one pass; the archive never
touches disk. Auto-detects the URL's format:

- **`.acpkg/` folder** → full manifest pipeline (below).
- **plain `.zip` / `.tar.gz` / `.tar.zst`** → direct streaming extraction,
  no repack needed. Zip: stream entries via local headers (`stream-unzip`
  handles data-descriptor and zip64 cases), verify each entry's CRC-32
  after decompression, refuse loudly on unsupported features. Tar:
  sequential records via Python tarfile streaming mode. Free-space estimate
  from Content-Length + per-file check before each write.
- **`.rar` / `.7z`** → refuses with a clear message: these can't stream
  (solid-archive law); run `repack` first (see 2.4).

Pipeline (`.acpkg`): `manifest.json (fetched first, verified first) →
parallel fetch of a bounded window of chunk files (N connections, e.g. 8) →
each chunk SHA-256 verified on arrival → extracted strictly in chunk order →
game files assembled as .acpart temps → per-file SHA-256 → atomic rename →
chunk file deleted as soon as its bytes are extracted`

**Parallel, not sequential:** chunk files are independent, so the downloader
fetches a window of N chunks ahead with concurrent connections (saturates
gigabit fiber — the old "single connection" limit is gone) while extraction
consumes them strictly in order. The window bounds disk: only N chunks exist
on disk at any moment, plus the game files written so far.

**Resume is trivial:** a chunk file that exists and matches its manifest hash
is done — a re-run only fetches what's missing. No state file, no Range
dance, no restart-from-zero, ever. A corrupt chunk (hash mismatch) is
re-fetched up to 3 tries, then a loud failure naming the chunk — never
silently written.

**Server needs:** plain static HTTP(S) file serving. No Range support
required for `.acpkg` (each chunk is its own URL). Chunk URLs are relative
to the manifest URL.

**Why not aria2:** evaluated and rejected — aria2 assembles downloads into a
file on disk, which would recreate the 2x-space problem. Its chunk-checksum
concept (from Metalink) is the inspiration for our per-chunk SHA-256
verification, implemented in our own fetch loop.

### 2.3 Firefox bridge

Tiny Firefox extension + native messaging host. **Intercepts the download
click BEFORE Firefox writes any bytes** — grabs the URL, cancels the browser's
own download, hands the URL to the CLI via native messaging. The "cancel and
take over" behavior is a hard requirement; if Firefox saved the archive first,
the 2x-space damage is already done and nothing can fix it.

Right-click → "Download with AntiCompress" (and/or auto-capture).

Mechanics: `downloads.onCreated` → `downloads.cancel(id)` (a few KB max;
Firefox offers no blocking cancel) → native message (4-byte length-prefixed
JSON via registered native messaging host, HKCU registry) → host spawns the
terminal chooser with CREATE_NEW_CONSOLE → user picks.

**Terminal chooser (primary flow):** the bridge spawns a visible console
window (native messaging hosts otherwise run hidden) showing the filename +
size with two choices: **[1] Download with AntiCompress** (streams, progress
bar in the same terminal) or **[2] Normal download** (CLI exits quietly; the
extension restarts the download in Firefox via `browser.downloads.download()`,
like nothing happened). The CLI *is* the dialog — no browser tabs, no extra
clicks.

**Restart-loop guard:** when the user picks "Normal", the extension
restarts the download; the onCreated handler keeps a set of pending-restart
URLs and skips them, so the restart never re-triggers the chooser.

**blob:/data: URLs** (in-page generated downloads) can't be streamed by the
CLI → the chooser offers "Normal download" only.

**Multi-part capture:** when a `.part01.rar`-style URL is intercepted, the
chooser offers to capture the whole part sequence (part URLs are usually
predictable) and queues `repack` after the last part lands.

### 2.4 Known limitation: solid RAR itself

The ideal "click link → stream the FitGirl RAR" is impossible — solid
archives interleave file data across volumes and no tool (present or
otherwise) extracts them from a partial download. Format law.

**But the chunk-folder design turns it into a speed bump, not a wall:**
the 120GB-game / 90GB-archive / 140GB-free case now works, because every
copy on disk is consumable:

1. **Repack:** 7z streams the solid archive once; chunks land; each part is
   deleted as 7z passes it → peak ≈ 90GB.
2. **Extract:** game grows as chunks are consumed; each chunk file deleted
   after extraction → peak ≈ 120GB.
3. **Done:** 120GB game, 20GB free — the Steam math.

The only case that still needs 2x: an archive that is both solid AND
single-file (no parts to delete incrementally) on a disk with no room — then
repack needs the archive on a second drive / external USB, or a machine with
space.

## 3. `.acpkg` format

A folder:

```
out.acpkg/
  manifest.json      ← format version, chunk size (1 MiB), file list
                       (relative path, size, offset in decompressed stream),
                       per-file SHA-256, per-chunk SHA-256, manifest self-hash
  chunk-000001.zst   ← 1 MiB decompressed each, zstd frame with content
  chunk-000002.zst      checksum enabled (xxHash64 embedded per frame)
  ...
```

- Manifest is a plain file — fetched first, verified first, source of truth.
- Chunk URLs in the downloader are `manifest_url + "/" + chunk filename`
  (or relative paths recorded in the manifest — decided at implementation,
  relative-to-manifest is the default).
- No footer, no tail-Range trickery: the manifest-at-the-end problem
  disappears because the manifest is its own file, written last but fetched
  first.
- No custom hosting, no CDN. A package is a folder — host it on any static
  file server, keep it on a drive, share it.

## 4. Integrity — 5 layers (Steam's mechanism, SHA-256 instead of SHA-1)

1. **Manifest hashes** — fetched first, verified first, source of truth.
2. **Per-chunk verification** — every chunk file's SHA-256 is checked
   against the manifest before it is decompressed (Steam's per-chunk
   validation). Bad chunk → re-fetch that chunk file up to 3 tries → then a
   loud error naming the chunk. Never silently writes garbage.
3. **zstd frame checksums** — decompression-time detection of decompressor
   bugs or disk glitches.
4. **Atomic writes** — game files assembled under `.acpart` temp names,
   verified against manifest per-file SHA-256, then `os.replace()` into the
   final name. A crash leaves chunk files + temp files + a clean resume —
   never a half-written file that claims to be finished.
5. **Repack-time source verification** — 7-Zip CRC checks validate the
   source archive during repack; corrupt source refuses to produce a
   package.

Plus: free-space check upfront (final size + buffer), fail fast before any
download.

If the tool says "done", files are bit-identical to what the repacker read.

## 5. Stack

- **Python 3** — glue only; the C libraries do the real work. User-readable,
  user-hackable.
- **`httpx`** (or `requests`) — chunk downloads over plain static HTTP(S);
  a small thread pool provides the parallel fetch window.
- **7-Zip CLI** (`7z.exe`) — reads RAR/7z/zip sources during repack
  (single-pass `-so` stream, CRC verification built in).
- **`zstandard`** Python package (C bindings) — chunk compression with
  content checksums.
- Windows; `7z.exe` is a documented prerequisite (downloadable from
  7-zip.org). No aria2 dependency.

## 6. Error handling

- Insufficient free space → refuse before download, show "need X, have Y".
- Chunk hash mismatch → re-fetch that chunk file (3 tries) → loud failure
  with chunk identity if persistent.
- Decompression checksum failure → same loud-failure path.
- Corrupt source at repack → refuse to emit package.
- Resume: chunk files present + hash-verified = done; missing = fetched.
  Never restarts from zero, no state file.
- Watchdog deletion failures → never fatal, degrade gracefully.

## 7. Testing

1. **Round-trip**: repack a mixed folder (random bytes + large binaries) →
   serve package over local HTTP → `dl` it → every file SHA-256-identical.
2. **Corrupt-chunk injection**: flip bytes in a chunk file → downloader
   detects, re-fetches that chunk, still produces correct files.
3. **Kill-resume**: kill mid-download → resume → identical result, only
   missing chunks fetched.
4. **Space-gate**: run with insufficient free space → fails before download.
5. **Manifest tamper**: alter manifest → refused.
6. **Repack CRC gate**: corrupt source archive → repack refuses.
7. **Order-verification**: repack a fixture whose listing order differs from
   extraction order → repack refuses (no bad package).
8. **Restart guard**: simulate "Normal" restart → onCreated skips it, no
   chooser loop.
9. **Part-deletion**: repack a multi-part fixture → parts deleted as 7z
   passes them; peak disk stays ≈ source size.
10. **Parallel window**: N-connection fetch → chunks extracted in order,
    game files identical to round-trip baseline.

## 8. Scope notes

- No GUI (terminal UI: progress bar).
- No hosting/CDN.
- No compression-ratio chasing (zstd level ~19 default).
- Compression of *already-compressed* game data is mostly a wash — the value
  is the streaming, not the ratio.
- Bridge phase order: CLI core first (format → repack → dl), Firefox bridge
  last.
