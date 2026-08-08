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

## 2. Components

Three pieces, Python 3, Windows-first:

### 2.1 `anticompress repack <archive> -o <out.acpkg>`

Converts an existing RAR/7z/zip archive into the `.acpkg` format (chunked zstd
+ manifest). Runs **once**, on the machine that has 2x space (or with the
source archive on a second drive). After this, the package streams anywhere
with 1x space.

Pipeline: `[RAR/7z/zip] → 7-Zip CLI (CRC-verified, streams file-by-file in
order) → chunker (fixed 1 MiB pieces) → zstd per chunk (frame checksum
enabled) → [game.acpkg = manifest.json + chunk stream]`

7-Zip's built-in CRC checks verify the source while reading. A corrupt source
archive **fails the repack loudly** — garbage in never becomes a trusted
package.

### 2.2 `anticompress dl <url> [-o <dest>]`

The Steam moment. Downloads and decompresses in one pass; the archive never
touches disk. Auto-detects the URL's format:

- **`.acpkg`** → full manifest pipeline (below).
- **plain `.zip` / `.tar.gz` / `.tar.zst`** → direct streaming extraction,
  no repack needed, verified with the archive's own CRC checks.
- **`.rar` / `.7z`** → refuses with a clear message: these can't stream
  (solid-archive law); run `repack` first (see 2.4 limitation).

Pipeline: `manifest (downloaded first, verified first) → sequential streaming download (httpx), each chunk's SHA-256 verified as it completes → zstd decompress → assemble files as .acpart temps → per-file SHA-256 → atomic rename → done`

The package is **never written to disk as a whole**. Only a bounded sliding
window of compressed bytes exists at any moment (RAM buffer / small temp),
deleted as it's consumed. A corrupt chunk is re-fetched alone via HTTP Range
(server must support Range — verified at handshake; if not, fail loudly
before downloading anything).

Re-running the same command after an interruption resumes from the state
file (consumed offset) + temp files — never restarts from zero.

**Why not aria2:** evaluated and rejected for the download path — aria2
assembles downloads into a file on disk, which would recreate the 2x-space
problem. Its chunk-checksum concept (from Metalink) is the inspiration for
our per-chunk SHA-256 verification, implemented inside our own stream loop.
aria2 remains a candidate only for v2 parallel prefetch, where consumed
chunk files would be deleted as they're processed.

### 2.4 Known limitation: multi-part solid RAR

The ideal "click link → just works" hits a hard wall with FitGirl-style
multi-part RAR repacks (`part1.rar … partN.rar`): in solid archives, file
data spans volumes, so no tool on earth can stream them — not us, not
anyone. Format law, not a design choice.

Those require: download all parts normally (2x space, once) → `repack` the
parts into one `.acpkg` → from then on it streams with 1x space forever.
The bridge can still help: when it sees `part1.rar`, it offers to capture
all part URLs in one go and queues the repack after the last part lands.

### 2.3 Firefox bridge

Tiny Firefox extension + native messaging host. **Intercepts the download
click BEFORE Firefox writes any bytes** — grabs the URL, cancels the browser's
own download, hands the URL to the CLI via native messaging. The "cancel and
take over" behavior is a hard requirement; if Firefox saved the archive first,
the 2x-space damage is already done and nothing can fix it.

Right-click → "Download with AntiCompress" (and/or auto-capture).

**Terminal chooser (primary flow):** when a download is intercepted, the
bridge spawns a visible console window (`CREATE_NEW_CONSOLE` — native
messaging hosts otherwise run hidden) showing the filename + size with two
choices: **[1] Download with AntiCompress** (streams, progress bar in the
same terminal) or **[2] Normal download** (CLI exits quietly; the extension
restarts the download in Firefox via `browser.downloads.download()`, like
nothing happened). The CLI *is* the dialog — no browser tabs, no extra
clicks.

## 3. `.acpkg` format

- **manifest.json** (first bytes of the package, small): format version,
  chunk size (1 MiB), total sizes, file list (relative path, size, offset in
  decompressed stream), per-file SHA-256, per-chunk entries (compressed
  offset, compressed size, uncompressed size, SHA-256), package SHA-256.
  The manifest itself is verified before being trusted. Compressed offsets
  let the downloader re-fetch a single bad chunk via HTTP Range.
- **chunk stream**: fixed 1 MiB decompressed-size chunks, each zstd-compressed
  with the zstd content-checksum flag enabled (xxHash64 embedded per frame).

No custom hosting, no CDN. A package is a plain file — host it anywhere, keep
it on a drive, share it.

## 4. Integrity — 5 layers (Steam's mechanism, SHA-256 instead of SHA-1)

1. **Manifest hashes** — downloaded first, verified first, source of truth.
2. **Per-chunk verification while downloading** — the stream loop verifies
each chunk's SHA-256 against the manifest the moment its bytes complete,
before decompressing (Steam's per-chunk validation; we implement it
ourselves instead of aria2's Metalink equivalent — see 2.2). Bad chunk →
re-fetch just that chunk via HTTP Range up to 3 tries → then stop with a
loud error naming the chunk and file. Never silently writes garbage.
3. **zstd frame checksums** — decompression-time detection of decompressor
   bugs or disk glitches.
4. **Atomic writes** — files assembled under `.acpart` temp names, verified
   against manifest per-file SHA-256, then `os.replace()` into the final
   name. A crash leaves temp files + clean resume, never a half-written file
   that claims to be finished.
5. **Repack-time source verification** — 7-Zip CRC checks validate the source
   archive during repack; corrupt source refuses to produce a package.

Plus: free-space check upfront (final size + buffer), fail fast before any
download.

If the tool says "done", files are bit-identical to what the repacker read.

## 5. Stack

- **Python 3** — glue only; the C libraries do the real work. User-readable,
  user-hackable.
- **`httpx`** (or `requests`) — sequential streaming download with HTTP Range
  support for resume and single-chunk re-fetch.
- **7-Zip CLI** (`7z.exe`) — reads RAR/7z/zip sources during repack (streams
  per-file in order, CRC verification built in).
- **`zstandard`** Python package (C bindings) — chunk compression with
  content checksums.
- Windows; `7z.exe` is a documented prerequisite (downloadable from
  7-zip.org). No aria2 dependency for v1 (see 2.2).

## 6. Error handling

- Insufficient free space → refuse before download, show "need X, have Y".
- Chunk hash mismatch → retry that chunk via Range (3 tries) → loud failure
  with chunk/file identity if persistent.
- Decompression checksum failure → same loud-failure path.
- Corrupt source at repack → refuse to emit package.
- Resume state lives in a small state file (consumed offset) + `.acpart` temp
  files; a re-run continues, never restarts from zero. If the server lacks
  Range support, fail loudly at handshake (before downloading anything).

## 7. Testing

1. **Round-trip**: repack a mixed folder (random bytes + large binaries) →
   serve package over local HTTP → `dl` it → every file SHA-256-identical.
2. **Corrupt-chunk injection**: flip bytes mid-package → downloader detects,
   re-fetches that chunk, still produces correct files.
3. **Kill-resume**: kill mid-download → resume → identical result.
4. **Space-gate**: run with insufficient free space → fails before download.
5. **Manifest tamper**: alter manifest → refused.
6. **Repack CRC gate**: corrupt source archive → repack refuses.

## 8. Scope notes

- No GUI (terminal UI: progress bar).
- No hosting/CDN.
- No compression-ratio chasing (zstd level ~19 default).
- Compression of *already-compressed* game data is mostly a wash — the value
  is the streaming, not the ratio.
- Bridge phase order: CLI core first (format → repack → dl), Firefox bridge
  last.
