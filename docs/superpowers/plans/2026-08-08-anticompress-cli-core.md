# AntiCompress CLI Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the AntiCompress CLI core — `repack` (RAR/7z/zip → `.acpkg` chunk folder), `dl` (parallel stream-download + verified extraction, 1x disk), `install` (local chunk-folder → game) — with Steam-style per-chunk integrity on Windows.

**Architecture:** Package = folder of `manifest.json` + independent `chunk-NNNNNN.zst` files (1 MiB decompressed each, zstd frames with checksums). Repack streams the source via `7z x -so` single pass (works for solid archives) with a part-deletion watchdog. Download fetches a bounded window of chunks in parallel, extracts strictly in order, deletes each chunk as consumed. Every byte on disk is either the game or being deleted.

**Tech Stack:** Python 3.11+ (3.14 on this machine), `httpx`, `zstandard`, 7-Zip CLI (`7z.exe`, prerequisite), pytest. No aria2 (rejected: assembles files on disk).

## Global Constraints

- Python ≥ 3.11, Windows-first (paths via `pathlib`, never string concat).
- Runtime deps: `httpx`, `zstandard` only. Dev dep: `pytest`. No others without user approval.
- 7-Zip CLI (`7z.exe`) is a hard prerequisite for `repack` (install via `winget install 7zip.7zip`; tests skip if absent).
- Format version 1, chunk size 1 MiB, chunk filenames `chunk-{index:06d}.zst`, manifest filename `manifest.json`.
- All hashes SHA-256. All manifests serialize with `json.dumps(sort_keys=True, separators=(",", ":"))` and carry a self-hash (`manifest_sha256` = hash of canonical JSON with that field empty).
- Manifest paths use POSIX separators (`/`) always.
- Corrupt/tampered anything → loud `ValueError`/`RuntimeError`, never silent garbage.
- Extraction assembles files as `<name>.acpart` temps, verifies per-file SHA-256, then `os.replace()` into the final name.
- Every task: write failing test → see it fail → implement → see it pass → commit.

---

### Task 1: Project scaffold + format module

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `anticompress/__init__.py`
- Create: `anticompress/format.py`
- Test: `tests/test_format.py`

**Interfaces:**
- Produces: `Manifest` dataclass (`version`, `chunk_size`, `total_size`, `files: list[FileEntry]`, `chunks: list[ChunkInfo]`, `manifest_sha256`), `FileEntry(path, size, offset, sha256)`, `ChunkInfo(index, sha256)`, `VERSION = 1`, `CHUNK_SIZE = 1024*1024`, `chunk_name(index) -> str`, `chunk_expected_size(m, index) -> int`, `serialize(m) -> str`, `deserialize(text) -> Manifest`, `write_manifest(m, path)`, `read_manifest(path) -> Manifest`.

- [ ] **Step 1: Install dependencies**

Run:
```bash
pip install httpx zstandard pytest
```
Expected: all three install cleanly.

- [ ] **Step 2: Write the failing tests**

`tests/test_format.py`:
```python
import json
import pytest

from anticompress.format import (
    VERSION, CHUNK_SIZE, Manifest, FileEntry, ChunkInfo,
    chunk_name, chunk_expected_size, serialize, deserialize, write_manifest, read_manifest,
)

def _sample() -> Manifest:
    return Manifest(
        total_size=2500000,
        files=[FileEntry(path="a/b.bin", size=2500000, offset=0, sha256="ab" * 32)],
        chunks=[ChunkInfo(index=i, sha256="cd" * 32) for i in range(3)],
    )

def test_chunk_name():
    assert chunk_name(0) == "chunk-000000.zst"
    assert chunk_name(42) == "chunk-000042.zst"

def test_chunk_expected_size():
    m = _sample()
    assert chunk_expected_size(m, 0) == CHUNK_SIZE
    assert chunk_expected_size(m, 1) == CHUNK_SIZE
    assert chunk_expected_size(m, 2) == 2500000 - 2 * CHUNK_SIZE

def test_serialize_roundtrip():
    m = _sample()
    text = serialize(m)
    m2 = deserialize(text)
    assert m2.files[0].path == "a/b.bin"
    assert m2.files[0].size == 2500000
    assert m2.chunks[2].sha256 == "cd" * 32
    assert m2.manifest_sha256  # non-empty self-hash

def test_serialize_is_canonical():
    assert serialize(_sample()) == serialize(_sample())

def test_tamper_detected():
    m = _sample()
    text = serialize(m)
    d = json.loads(text)
    d["files"][0]["size"] = 999
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        deserialize(json.dumps(d, sort_keys=True, separators=(",", ":")))

def test_wrong_version_rejected():
    m = _sample()
    text = serialize(m)
    d = json.loads(text)
    d["version"] = 99
    with pytest.raises(ValueError, match="unsupported format version"):
        deserialize(json.dumps(d, sort_keys=True, separators=(",", ":")))

def test_read_write_manifest(tmp_path):
    m = _sample()
    p = tmp_path / "manifest.json"
    write_manifest(m, p)
    assert read_manifest(p).total_size == 2500000
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_format.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.format'`

- [ ] **Step 4: Create the package and format module**

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "anticompress"
version = "0.1.0"
description = "Steam-style streaming download + decompression"
requires-python = ">=3.11"
dependencies = ["httpx", "zstandard"]

[project.scripts]
anticompress = "anticompress.cli:main"

[tool.setuptools]
packages = ["anticompress"]
```

`requirements.txt`:
```
httpx
zstandard
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest
```

`anticompress/__init__.py`:
```python
"""AntiCompress: Steam-style streaming download + decompression."""
__version__ = "0.1.0"
```

`anticompress/format.py`:
```python
"""Manifest model for the .acpkg format (version 1)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

VERSION = 1
CHUNK_SIZE = 1024 * 1024  # 1 MiB decompressed per chunk
CHUNK_NAME = "chunk-{index:06d}.zst"
MANIFEST_NAME = "manifest.json"


@dataclass
class ChunkInfo:
    index: int
    sha256: str  # of the compressed chunk file as stored


@dataclass
class FileEntry:
    path: str  # relative, POSIX separators
    size: int
    offset: int  # offset of this file in the decompressed byte stream
    sha256: str  # of the decompressed file content


@dataclass
class Manifest:
    version: int = VERSION
    chunk_size: int = CHUNK_SIZE
    total_size: int = 0
    files: list[FileEntry] = field(default_factory=list)
    chunks: list[ChunkInfo] = field(default_factory=list)
    manifest_sha256: str = ""


def chunk_name(index: int) -> str:
    return CHUNK_NAME.format(index=index)


def chunk_expected_size(m: Manifest, index: int) -> int:
    """Decompressed size of chunk `index` (the last chunk may be short)."""
    if index < len(m.chunks) - 1:
        return m.chunk_size
    return m.total_size - index * m.chunk_size


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def to_dict(m: Manifest) -> dict:
    return {
        "version": m.version,
        "chunk_size": m.chunk_size,
        "total_size": m.total_size,
        "manifest_sha256": m.manifest_sha256,
        "files": [asdict(f) for f in m.files],
        "chunks": [asdict(c) for c in m.chunks],
    }


def serialize(m: Manifest) -> str:
    """Serialize with the self-hash filled in (hash of canonical JSON with the field empty)."""
    d = to_dict(m)
    d["manifest_sha256"] = ""
    body = _canonical(d)
    d["manifest_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    return _canonical(d)


def deserialize(text: str) -> Manifest:
    d = json.loads(text)
    if d.get("version") != VERSION:
        raise ValueError(f"unsupported format version: {d.get('version')}")
    if d.get("chunk_size") != CHUNK_SIZE:
        raise ValueError(f"unsupported chunk size: {d.get('chunk_size')}")
    expected = d.get("manifest_sha256", "")
    d["manifest_sha256"] = ""
    if hashlib.sha256(_canonical(d).encode()).hexdigest() != expected:
        raise ValueError("manifest hash mismatch (tampered?)")
    return Manifest(
        version=d["version"],
        chunk_size=d["chunk_size"],
        total_size=d["total_size"],
        manifest_sha256=expected,
        files=[FileEntry(**f) for f in d["files"]],
        chunks=[ChunkInfo(**c) for c in d["chunks"]],
    )


def write_manifest(m: Manifest, path: Path) -> None:
    path.write_text(serialize(m), encoding="utf-8")


def read_manifest(path: Path) -> Manifest:
    return deserialize(path.read_text(encoding="utf-8"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_format.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt requirements-dev.txt anticompress/ tests/
git commit -m "feat: scaffold + acpkg manifest format (v1) with self-hash"
```

---

### Task 2: Chunker — byte stream → chunk files

**Files:**
- Create: `anticompress/chunker.py`
- Create: `tests/helpers.py` (shared test utilities)
- Test: `tests/test_chunker.py`

**Interfaces:**
- Consumes: `CHUNK_SIZE`, `ChunkInfo`, `chunk_name` from Task 1.
- Produces: `write_chunks(stream: Iterable[bytes], out_dir: Path, progress=None) -> tuple[list[ChunkInfo], int]` — consumes decompressed bytes, writes chunk files, returns `(chunks, total_size)`. `progress(total_so_far)` called per block.

- [ ] **Step 1: Write the failing tests**

`tests/helpers.py`:
```python
"""Shared test utilities: build packages from in-memory files, compare trees."""
from __future__ import annotations

from pathlib import Path

from anticompress.chunker import write_chunks
from anticompress.format import Manifest, FileEntry, write_manifest


def build_package(files: dict[str, bytes], pkg_dir: Path) -> Manifest:
    """Build a .acpkg chunk folder from {relative_path: content} bytes."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    chunks, total = write_chunks(iter(files.values()), pkg_dir)
    manifest = Manifest(total_size=total)
    offset = 0
    for path, content in files.items():
        manifest.files.append(FileEntry(path=path, size=len(content), offset=offset, sha256=""))
        offset += len(content)
    _fill_file_hashes(manifest, pkg_dir)
    write_manifest(manifest, pkg_dir / "manifest.json")
    return manifest


def _fill_file_hashes(m: Manifest, chunk_dir: Path) -> None:
    import hashlib

    import zstandard

    from anticompress.format import chunk_expected_size, chunk_name

    dctx = zstandard.ZstdDecompressor()
    decompressed = b""
    pos = 0
    ci = 0
    for fe in m.files:
        h = hashlib.sha256()
        need = fe.size
        while need > 0:
            if pos >= len(decompressed):
                if ci >= len(m.chunks):
                    raise RuntimeError("stream ended early")
                raw = (chunk_dir / chunk_name(ci)).read_bytes()
                dobj = dctx.decompressobj()
                decompressed = dobj.decompress(raw) + dobj.flush()
                if len(decompressed) != chunk_expected_size(m, ci):
                    raise RuntimeError("chunk size mismatch")
                ci += 1
                pos = 0
            take = min(need, len(decompressed) - pos)
            h.update(decompressed[pos : pos + take])
            pos += take
            need -= take
        fe.sha256 = h.hexdigest()


def assert_trees_identical(expected: dict[str, bytes], dest: Path) -> None:
    for path, content in expected.items():
        actual = (dest / path).read_bytes()
        assert actual == content, f"content mismatch in {path}"
```

`tests/test_chunker.py`:
```python
import hashlib

import pytest
import zstandard

from anticompress.chunker import write_chunks
from anticompress.format import CHUNK_SIZE, chunk_name, chunk_expected_size

RANDOM_BLOB = bytes((i * 31 + 7) % 256 for i in range(2500000))  # 2.5 MB, incompressible-ish


def test_chunks_written_and_roundtrip(tmp_path):
    chunks, total = write_chunks(iter([RANDOM_BLOB]), tmp_path)
    assert total == len(RANDOM_BLOB)
    assert len(chunks) == 3
    assert chunks[-1].index == 2

    dctx = zstandard.ZstdDecompressor()
    out = b""
    for i, ci in enumerate(chunks):
        raw = (tmp_path / chunk_name(ci.index)).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ci.sha256
        dobj = dctx.decompressobj()
        data = dobj.decompress(raw) + dobj.flush()
        assert len(data) == chunk_expected_size(_fake_manifest(chunks, total), i)
        out += data
    assert out == RANDOM_BLOB


def test_multiple_blocks_concatenate(tmp_path):
    chunks, total = write_chunks(iter([b"hello ", b"world"]), tmp_path)
    assert total == 11
    assert len(chunks) == 1
    dctx = zstandard.ZstdDecompressor()
    dobj = dctx.decompressobj()
    data = dobj.decompress((tmp_path / chunk_name(0)).read_bytes()) + dobj.flush()
    assert data == b"hello world"


def test_empty_stream(tmp_path):
    chunks, total = write_chunks(iter([]), tmp_path)
    assert chunks == []
    assert total == 0


def test_progress_callback(tmp_path):
    seen = []
    write_chunks(iter([RANDOM_BLOB]), tmp_path, progress=lambda n: seen.append(n))
    assert seen == [len(RANDOM_BLOB)]


def _fake_manifest(chunks, total):
    from anticompress.format import Manifest

    m = Manifest(total_size=total, chunks=chunks)
    m.chunk_size = CHUNK_SIZE
    return m
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.chunker'`

- [ ] **Step 3: Write the chunker**

`anticompress/chunker.py`:
```python
"""Split a decompressed byte stream into zstd chunk files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterable

import zstandard

from .format import CHUNK_SIZE, ChunkInfo, chunk_name

Progress = Callable[[int], None] | None


def _compressor() -> zstandard.ZstdCompressor:
    return zstandard.ZstdCompressor(level=19, write_content_size=True, write_checksum=True)


def write_chunks(
    stream: Iterable[bytes], out_dir: Path, progress: Progress = None
) -> tuple[list[ChunkInfo], int]:
    """Consume decompressed bytes, write chunk files into out_dir.

    Returns (chunks, total_size). Each chunk is exactly CHUNK_SIZE decompressed
    bytes except the last, compressed as a zstd frame with content checksum.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[ChunkInfo] = []
    buf = bytearray()
    total = 0
    index = 0
    cctx = _compressor()
    for block in stream:
        if not block:
            continue
        buf += block
        total += len(block)
        while len(buf) >= CHUNK_SIZE:
            _write_chunk(cctx, index, bytes(buf[:CHUNK_SIZE]), out_dir, chunks)
            del buf[:CHUNK_SIZE]
            index += 1
        if progress:
            progress(total)
    if buf:
        _write_chunk(cctx, index, bytes(buf), out_dir, chunks)
    if progress:
        progress(total)
    return chunks, total


def _write_chunk(
    cctx: zstandard.ZstdCompressor,
    index: int,
    data: bytes,
    out_dir: Path,
    chunks: list[ChunkInfo],
) -> None:
    comp = cctx.compress(data)
    sha = hashlib.sha256(comp).hexdigest()
    (out_dir / chunk_name(index)).write_bytes(comp)
    chunks.append(ChunkInfo(index=index, sha256=sha))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chunker.py tests/test_format.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/chunker.py tests/helpers.py tests/test_chunker.py
git commit -m "feat: chunker — zstd chunk files with content checksums"
```

---

### Task 3: Extractor — chunk files → verified game files

**Files:**
- Create: `anticompress/extractor.py`
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `Manifest`, `chunk_name`, `chunk_expected_size` (Task 1); `build_package`, `assert_trees_identical` (Task 2 helpers).
- Produces: `extract_package(chunk_dir: Path, dest_dir: Path, m: Manifest, delete_chunks: bool = True, progress=None) -> None` — reads chunks strictly in order, verifies each chunk's SHA-256, decompresses, assembles files as `.acpart` temps, verifies per-file SHA-256, `os.replace()` into place, deletes consumed chunk files. Skips byte ranges already covered by existing hash-verified files (resume).

- [ ] **Step 1: Write the failing tests**

`tests/test_extractor.py`:
```python
import hashlib

import pytest
import zstandard

from anticompress.extractor import extract_package
from anticompress.format import chunk_name
from tests.helpers import build_package, assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
    "empty.dat": b"",
}


def test_extract_roundtrip(tmp_path):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = build_package(FILES, pkg)
    extract_package(pkg, dest, m)
    assert_trees_identical(FILES, dest)


def test_chunks_deleted_after_extract(tmp_path):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = build_package(FILES, pkg)
    extract_package(pkg, dest, m, delete_chunks=True)
    assert sorted(pkg.iterdir()) == [pkg / "manifest.json"]  # chunks gone


def test_corrupt_chunk_detected(tmp_path):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = build_package(FILES, pkg)
    victim = pkg / chunk_name(m.chunks[len(m.chunks) // 2].index)
    victim.write_bytes(b"garbage")
    with pytest.raises(ValueError, match="hash mismatch"):
        extract_package(pkg, dest, m)


def test_resume_skips_verified_files(tmp_path):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = build_package(FILES, pkg)
    dest.mkdir()
    (dest / "game.exe").write_bytes(FILES["game.exe"])  # already "downloaded"
    extract_package(pkg, dest, m)
    assert_trees_identical(FILES, dest)


def test_manifest_tamper_changes_expected_hash(tmp_path):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = build_package(FILES, pkg)
    m.files[0].sha256 = "ff" * 32
    with pytest.raises(ValueError, match="hash mismatch after assembly"):
        extract_package(pkg, dest, m)


def test_no_acpart_leftovers(tmp_path):
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = build_package(FILES, pkg)
    extract_package(pkg, dest, m)
    assert not list(dest.rglob("*.acpart"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extractor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.extractor'`

- [ ] **Step 3: Write the extractor**

`anticompress/extractor.py`:
```python
"""Assemble game files from chunk files (strict order, atomic, delete-as-you-go)."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable

import zstandard

from .format import Manifest, chunk_expected_size, chunk_name

Progress = Callable[[int], None] | None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _covered_ranges(m: Manifest, dest_dir: Path) -> list[tuple[int, int]]:
    """Decompressed-stream byte ranges already present and hash-verified in dest_dir."""
    ranges: list[tuple[int, int]] = []
    for fe in m.files:
        final = dest_dir / fe.path
        if final.is_file() and final.stat().st_size == fe.size and _sha256_file(final) == fe.sha256:
            ranges.append((fe.offset, fe.offset + fe.size))
    return ranges


def extract_package(
    chunk_dir: Path,
    dest_dir: Path,
    m: Manifest,
    delete_chunks: bool = True,
    progress: Progress = None,
) -> None:
    """Read chunk files in order, decompress, write game files atomically.

    Chunk files are deleted as soon as their bytes are extracted. Byte ranges
    covered by existing hash-verified files are skipped (resume).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    skip = _covered_ranges(m, dest_dir)
    skip.sort()
    dctx = zstandard.ZstdDecompressor()
    fi = 0  # current file index into m.files
    out_fh = None
    tmp_path: Path | None = None
    file_remaining = 0
    stream_pos = 0
    covered_idx = 0
    done = 0

    def close_tmp(keep: bool) -> None:
        nonlocal out_fh, tmp_path
        if out_fh is None:
            return
        out_fh.close()
        out_fh = None
        if keep and tmp_path is not None:
            os.replace(tmp_path, dest_dir / m.files[fi].path)
        elif tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    for ci in m.chunks:
        size = chunk_expected_size(m, ci.index)
        while covered_idx < len(skip) and skip[covered_idx][1] <= stream_pos:
            covered_idx += 1
        if (
            covered_idx < len(skip)
            and skip[covered_idx][0] <= stream_pos
            and skip[covered_idx][1] >= stream_pos + size
        ):
            stream_pos += size
            done += size
            if progress:
                progress(done)
            continue

        path = chunk_dir / chunk_name(ci.index)
        deadline = time.monotonic() + 300
        while not path.is_file():
            if time.monotonic() > deadline:
                raise FileNotFoundError(f"chunk {ci.index} never arrived")
            time.sleep(0.2)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ci.sha256:
            raise ValueError(f"chunk {ci.index} hash mismatch (corrupt or tampered)")

        dobj = dctx.decompressobj()
        out = dobj.decompress(raw) + dobj.flush()
        if len(out) != size:
            raise ValueError(f"chunk {ci.index} decompressed to {len(out)} bytes, expected {size}")

        pos = 0
        while pos < len(out):
            if file_remaining == 0:
                if fi >= len(m.files):
                    raise ValueError("decompressed stream longer than manifest")
                fe = m.files[fi]
                if fe.size == 0:
                    final = dest_dir / fe.path
                    final.parent.mkdir(parents=True, exist_ok=True)
                    final.write_bytes(b"")
                    fi += 1
                    continue
                tmp_path = dest_dir / (fe.path + ".acpart")
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                out_fh = tmp_path.open("wb")
                file_remaining = fe.size
            fe = m.files[fi]
            take = min(file_remaining, len(out) - pos)
            out_fh.write(out[pos : pos + take])
            pos += take
            file_remaining -= take
            if file_remaining == 0:
                out_fh.close()
                out_fh = None
                if _sha256_file(tmp_path) != fe.sha256:
                    tmp_path.unlink(missing_ok=True)
                    raise ValueError(f"file {fe.path} hash mismatch after assembly")
                os.replace(tmp_path, dest_dir / fe.path)
                fi += 1

        done += size
        if progress:
            progress(done)
        if delete_chunks:
            path.unlink(missing_ok=True)
        stream_pos += size

    close_tmp(False)
    if fi != len(m.files):
        raise ValueError("decompressed stream shorter than manifest")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/extractor.py tests/test_extractor.py
git commit -m "feat: extractor — verified atomic assembly, chunk delete-as-you-go, resume skip"
```

---

### Task 4: Repacker — 7z single-pass stream → chunk folder

**Files:**
- Create: `anticompress/repacker.py`
- Test: `tests/test_repacker.py`

**Interfaces:**
- Consumes: `write_chunks` (Task 2), `Manifest`/`FileEntry`/`write_manifest`/`chunk_name`/`chunk_expected_size` (Task 1).
- Produces: `repack(archive: Path, out_dir: Path, seven_zip: str = "7z", progress=None) -> Manifest` — lists archive via `7z l -slt`, streams via `7z x -so` single pass, writes chunk files + manifest with per-file hashes, sample-verifies the listing-order assumption, deletes consumed multi-part volumes. Refuses (deletes out_dir) on any failure.

- [ ] **Step 1: Install 7-Zip (prerequisite)**

Run:
```bash
winget install 7zip.7zip
```
Then verify:
```bash
"C:\Program Files\7-Zip\7z.exe" i
```
Expected: prints 7-Zip version info. If `7z` is not on PATH, tests will use `seven_zip="C:\\Program Files\\7-Zip\\7z.exe"` (the test fixture tries both).

- [ ] **Step 2: Write the failing tests**

`tests/test_repacker.py`:
```python
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from anticompress.extractor import extract_package
from anticompress.format import read_manifest
from anticompress.repacker import repack
from tests.helpers import assert_trees_identical

SEVEN_ZIP = shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
needs_7z = pytest.mark.skipif(not Path(SEVEN_ZIP).exists(), reason="7-Zip not installed")

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}


def _make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)


@needs_7z
def test_repack_zip_roundtrip(tmp_path):
    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = repack(src, pkg, seven_zip=SEVEN_ZIP)
    assert m.total_size == sum(len(v) for v in FILES.values())
    assert {f.path for f in m.files} == set(FILES.keys())
    read_manifest(pkg / "manifest.json")  # parses + self-hash verifies
    extract_package(pkg, dest, m)
    assert_trees_identical(FILES, dest)


@needs_7z
def test_repack_solid_7z_roundtrip(tmp_path):
    """Solid .7z fixture (7-Zip can create it) — exercises the solid single-pass path."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    for name, content in FILES.items():
        p = src_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    src = tmp_path / "game.7z"
    r = subprocess.run(
        [SEVEN_ZIP, "a", "-t7z", "-ms=on", "-mx=9", str(src), "."],
        cwd=src_dir, capture_output=True,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    pkg = tmp_path / "pkg"
    dest = tmp_path / "dest"
    m = repack(src, pkg, seven_zip=SEVEN_ZIP)
    extract_package(pkg, dest, m)
    assert_trees_identical(FILES, dest)


@needs_7z
def test_repack_rejects_corrupt_source(tmp_path):
    src = tmp_path / "game.zip"
    _make_zip(src)
    data = bytearray(src.read_bytes())
    data[len(data) // 2] ^= 0xFF  # corrupt a byte in the middle
    src.write_bytes(data)
    pkg = tmp_path / "pkg"
    with pytest.raises(RuntimeError):
        repack(src, pkg, seven_zip=SEVEN_ZIP)
    assert not pkg.exists()  # no bad package left behind


@needs_7z
def test_repack_volume_deletion(tmp_path):
    """Multi-volume fixture: parts must be deleted as 7z passes them."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    big = bytes((i * 13 + 5) % 256 for i in range(3_000_000))
    (src_dir / "big.bin").write_bytes(big)
    vol = tmp_path / "vol.7z"  # 7z will split
    r = subprocess.run(
        [SEVEN_ZIP, "a", "-t7z", "-v1m", str(vol), "."],
        cwd=src_dir, capture_output=True,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    volumes = sorted(tmp_path.glob("vol.7z.*"))
    assert len(volumes) >= 2, "fixture should span multiple volumes"
    pkg = tmp_path / "pkg"
    m = repack(volumes[0], pkg, seven_zip=SEVEN_ZIP)  # point at first volume
    for v in volumes:
        assert not v.exists(), f"volume {v.name} should have been deleted"
    dest = tmp_path / "dest"
    extract_package(pkg, dest, m)
    assert (dest / "big.bin").read_bytes() == big


@needs_7z
def test_verify_sample_detects_bad_order(tmp_path):
    """Package bytes disagreeing with 7z's own per-file extraction → refused."""
    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "pkg"
    m = repack(src, pkg, seven_zip=SEVEN_ZIP)
    m.files[0].sha256 = "00" * 32  # simulate a listing-order mismatch
    with pytest.raises(RuntimeError, match="order assumption"):
        _verify_sample(m, pkg, src, SEVEN_ZIP)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_repacker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.repacker'`

- [ ] **Step 4: Write the repacker**

`anticompress/repacker.py`:
```python
"""Repack RAR/7z/zip into a .acpkg chunk folder via 7-Zip's single-pass -so stream."""
from __future__ import annotations

import hashlib
import random
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import zstandard

from .chunker import write_chunks
from .format import Manifest, FileEntry, chunk_expected_size, chunk_name, write_manifest

Progress = Callable[[int], None] | None


def _run_7z(args: list[str], seven_zip: str, **kw) -> subprocess.Popen:
    return subprocess.Popen(
        [seven_zip, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw
    )


def _list_slt(archive: Path, seven_zip: str) -> list[tuple[str, int]]:
    """Return [(path, size)] from `7z l -slt` (files only, listing order)."""
    p = _run_7z(["l", "-slt", str(archive)], seven_zip)
    out, err = p.communicate()
    if p.returncode != 0:
        raise RuntimeError(f"7z list failed: {err.decode(errors='replace')[-500:]}")
    entries: list[tuple[str, int]] = []
    cur_path: str | None = None
    cur_size: int | None = None
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("Path = "):
            if cur_path is not None and cur_size is not None and not cur_path.endswith("/"):
                entries.append((cur_path, cur_size))
            cur_path = line[7:]
            cur_size = None
        elif line.startswith("Size = ") and cur_path is not None:
            cur_size = int(line[7:])
    if cur_path is not None and cur_size is not None and not cur_path.endswith("/"):
        entries.append((cur_path, cur_size))
    return entries


def _find_volumes(archive: Path) -> list[Path]:
    """Multi-part volume chains: RAR5 `name.partNN.rar`, RAR4 `name.rNN`. Single file → []."""
    if archive.suffix.lower() != ".rar":
        return []
    stem = archive.name[: -len(archive.suffix)]
    parts = sorted(archive.parent.glob(stem + ".part*.rar"))
    if parts:
        return parts
    olds = sorted(archive.parent.glob(stem + ".r??"))
    if olds:
        return [archive] + [v for v in olds if v != archive]
    return []


def _fill_file_hashes(m: Manifest, chunk_dir: Path) -> None:
    """Compute each file's SHA-256 from the chunk files (stream in order)."""
    dctx = zstandard.ZstdDecompressor()
    decompressed = b""
    pos = 0
    ci = 0
    for fe in m.files:
        h = hashlib.sha256()
        need = fe.size
        while need > 0:
            if pos >= len(decompressed):
                if ci >= len(m.chunks):
                    raise RuntimeError("stream ended early")
                raw = (chunk_dir / chunk_name(ci)).read_bytes()
                dobj = dctx.decompressobj()
                decompressed = dobj.decompress(raw) + dobj.flush()
                if len(decompressed) != chunk_expected_size(m, ci):
                    raise RuntimeError("chunk size mismatch")
                ci += 1
                pos = 0
            take = min(need, len(decompressed) - pos)
            h.update(decompressed[pos : pos + take])
            pos += take
            need -= take
        fe.sha256 = h.hexdigest()


def _verify_sample(
    m: Manifest, chunk_dir: Path, archive: Path, seven_zip: str, max_samples: int = 8
) -> None:
    """Verify the listing-order assumption: 7z's own per-file extraction must
    hash-identical to the package bytes at the computed offsets."""
    top: dict[str, list[FileEntry]] = {}
    for fe in m.files:
        top.setdefault(fe.path.split("/")[0], []).append(fe)
    rng = random.Random(0)
    samples = [rng.choice(v) for v in top.values() if v]
    for fe in samples[:max_samples]:
        p = _run_7z(["x", "-so", str(archive), fe.path.replace("/", "\\")], seven_zip)
        out, err = p.communicate()
        if p.returncode != 0:
            raise RuntimeError(
                f"sample extract failed for {fe.path}: {err.decode(errors='replace')[-200:]}"
            )
        if hashlib.sha256(out).hexdigest() != fe.sha256:
            raise RuntimeError(
                f"archive order assumption broken at {fe.path} — refusing package"
            )


def repack(
    archive: Path, out_dir: Path, seven_zip: str = "7z", progress: Progress = None
) -> Manifest:
    """Convert an archive into a .acpkg chunk folder. Refuses on any failure
    (deletes out_dir). Multi-part volumes are deleted as 7z passes them."""
    entries = _list_slt(archive, seven_zip)
    if not entries:
        raise ValueError("archive contains no files")
    offset = 0
    files: list[FileEntry] = []
    for path, size in entries:
        files.append(FileEntry(path=path.replace("\\", "/"), size=size, offset=offset, sha256=""))
        offset += size
    total = offset

    volumes = _find_volumes(archive)
    cum_sizes: list[int] = []
    acc = 0
    for v in volumes:
        acc += v.stat().st_size
        cum_sizes.append(acc)
    deleted = [False] * len(volumes)

    def delete_consumed(consumed: int) -> None:
        """Delete volumes whose full contents 7z has certainly passed (output
        consumed ≥ cumulative compressed size — always safe, never early)."""
        for k, cum in enumerate(cum_sizes):
            if not deleted[k] and consumed >= cum:
                try:
                    volumes[k].unlink()
                    deleted[k] = True
                except OSError:
                    pass  # still locked; retried on next tick

    out_dir.mkdir(parents=True, exist_ok=True)
    p = _run_7z(["x", "-so", "-y", str(archive)], seven_zip)
    consumed = 0

    def reader():
        try:
            while True:
                block = p.stdout.read(1 << 20)
                if not block:
                    break
                yield block
        finally:
            p.stdout.close()

    def track(block: bytes) -> None:
        nonlocal consumed
        consumed += len(block)
        delete_consumed(consumed)

    try:
        chunks, total_read = write_chunks(_track(reader(), track), out_dir, progress=progress)
    except Exception:
        p.kill()
        raise
    err = p.stderr.read().decode(errors="replace")
    rc = p.wait()
    if rc != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise RuntimeError(f"7z extraction failed (rc={rc}): {err[-500:]}")
    if total_read != total:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise RuntimeError(f"size mismatch: listing says {total}, stream produced {total_read}")

    m = Manifest(total_size=total, files=files, chunks=chunks)
    try:
        _fill_file_hashes(m, out_dir)
        write_manifest(m, out_dir / "manifest.json")
        _verify_sample(m, out_dir, archive, seven_zip)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    return m


def _track(stream, fn: Callable[[bytes], None]):
    for block in stream:
        fn(block)
        yield block
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_repacker.py -v`
Expected: all pass (7-Zip installed in Step 1)

- [ ] **Step 6: Commit**

```bash
git add anticompress/repacker.py tests/test_repacker.py
git commit -m "feat: repacker — 7z -so single-pass, listing-order verification, volume deletion"
```

---

### Task 5: Downloader — parallel chunk fetch + verified extraction

**Files:**
- Create: `anticompress/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `extract_package` (Task 3), `deserialize`/`chunk_name` (Task 1).
- Produces: `download_package(base_url: str, dest_dir: Path, chunk_dir: Path, workers: int = 8, progress=None) -> Manifest` — fetches `{base_url}/manifest.json`, verifies, downloads missing chunks with a thread pool (each chunk verified against its SHA-256 before rename into place), then extracts in order (chunks deleted as consumed).

- [ ] **Step 1: Write the failing tests**

`tests/test_downloader.py`:
```python
import functools
import hashlib
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anticompress.downloader import download_package
from anticompress.format import chunk_name
from tests.helpers import build_package, assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}


class _CountingHandler(SimpleHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests.append(self.path)
        super().do_GET()


@pytest.fixture
def server(tmp_path):
    pkg = tmp_path / "pkg"
    m = build_package(FILES, pkg)
    _CountingHandler.requests = []
    handler = functools.partial(_CountingHandler, directory=str(pkg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, m, pkg
    httpd.shutdown()


def test_download_roundtrip(server, tmp_path):
    httpd, m = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    dest = tmp_path / "dest"
    chunks = tmp_path / "chunks"
    download_package(base, dest, chunks, workers=8)
    assert_trees_identical(FILES, dest)
    assert sorted(chunks.iterdir()) == []  # all chunks consumed


def test_parallel_fetch_all_chunks(server, tmp_path):
    httpd, m = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    download_package(base, tmp_path / "dest", tmp_path / "chunks", workers=16)
    assert len([p for p in _CountingHandler.requests if "chunk-" in p]) == len(m.chunks)


def test_resume_fetches_only_missing(tmp_path):
    """A fully-covered chunk (1 MiB file = exactly chunk 0) must not be re-fetched."""
    pkg = tmp_path / "pkg"
    one_mib = bytes((i * 11) % 256 for i in range(1024 * 1024))
    files = {"a.bin": one_mib, "b.txt": b"tail"}
    m = build_package(files, pkg)
    _CountingHandler.requests = []
    handler = functools.partial(_CountingHandler, directory=str(pkg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.bin").write_bytes(one_mib)  # already present & verified → chunk 0 covered
        download_package(f"http://127.0.0.1:{httpd.server_port}", dest, tmp_path / "chunks")
        assert (dest / "b.txt").read_bytes() == b"tail"
        fetched = [p for p in _CountingHandler.requests if "chunk-" in p]
        assert fetched == ["/chunk-000001.zst"]  # only the missing chunk
    finally:
        httpd.shutdown()


def test_corrupt_chunk_on_server_fails_after_retries(server, tmp_path):
    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    victim = m.chunks[0]
    (pkg / chunk_name(victim.index)).write_bytes(b"bad!")  # corrupt served chunk
    with pytest.raises(Exception):
        download_package(base, tmp_path / "dest", tmp_path / "chunks", workers=1)


def test_manifest_tamper_on_server_rejected(server, tmp_path):
    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    import json

    mp = pkg / "manifest.json"
    d = json.loads(mp.read_text())
    d["total_size"] = 1
    mp.write_text(json.dumps(d, sort_keys=True, separators=(",", ":")))
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        download_package(base, tmp_path / "dest", tmp_path / "chunks")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.downloader'`

- [ ] **Step 3: Write the downloader**

`anticompress/downloader.py`:
```python
"""Fetch .acpkg chunks in parallel, verify, extract in order, delete as consumed."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import os
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx

from .extractor import extract_package
from .format import Manifest, chunk_name, deserialize

Progress = Callable[[int], None] | None


def _fetch_chunk(client: httpx.Client, url: str, tmp_path: Path, final_path: Path, sha256: str, tries: int = 3) -> None:
    for attempt in range(tries):
        try:
            r = client.get(url)
            r.raise_for_status()
            data = r.content
            if hashlib.sha256(data).hexdigest() != sha256:
                raise ValueError("chunk hash mismatch")
            tmp_path.write_bytes(data)
            os.replace(tmp_path, final_path)  # atomic: extractor never sees partial chunks
            return
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1 + attempt)


def _missing_chunks(chunk_dir: Path, m: Manifest) -> list:
    missing = []
    for ci in m.chunks:
        p = chunk_dir / chunk_name(ci.index)
        if not (p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == ci.sha256):
            missing.append(ci)
    return missing


def download_package(
    base_url: str,
    dest_dir: Path,
    chunk_dir: Path,
    workers: int = 8,
    progress: Progress = None,
) -> Manifest:
    """Fetch {base_url}/manifest.json, download missing chunks in parallel
    (verified), then extract strictly in order, deleting chunks as consumed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip("/") + "/"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        r = client.get(urljoin(base, "manifest.json"))
        r.raise_for_status()
        m = deserialize(r.text)

        missing = _missing_chunks(chunk_dir, m)
        if missing:

            def fetch(ci):
                _fetch_chunk(
                    client,
                    urljoin(base, chunk_name(ci.index)),
                    chunk_dir / (chunk_name(ci.index) + ".tmp"),
                    chunk_dir / chunk_name(ci.index),
                    ci.sha256,
                )

            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for _ in ex.map(fetch, missing):
                    pass  # first exception propagates, cancelling the rest

        extract_package(chunk_dir, dest_dir, m, delete_chunks=True, progress=progress)
    return m
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/downloader.py tests/test_downloader.py
git commit -m "feat: downloader — parallel verified chunk fetch + streaming extract"
```

---

### Task 6: Plain zip/tar streaming (`dl` without repack)

**Files:**
- Create: `anticompress/zipstream.py`
- Test: `tests/test_zipstream.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (self-contained).
- Produces: `stream_zip(url: str, dest_dir: Path, progress=None) -> None`, `stream_tar(url: str, dest_dir: Path, progress=None) -> None`, `stream_archive(url: str, dest_dir: Path, progress=None) -> None` (dispatch by extension; `.rar`/`.7z` → `ValueError` with "run repack first").

- [ ] **Step 1: Write the failing tests**

`tests/test_zipstream.py`:
```python
import functools
import io
import tarfile
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from anticompress.zipstream import stream_archive, stream_tar, stream_zip
from tests.helpers import assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def serve(tmp_path):
    def _serve(data: bytes, suffix: str):
        f = tmp_path / f"archive{suffix}"
        f.write_bytes(data)
        handler = functools.partial(_QuietHandler, directory=str(tmp_path))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{httpd.server_port}/archive{suffix}", httpd

    yield _serve


def _zip_bytes(descriptor: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_stream_zip(serve, tmp_path):
    url, httpd = serve(_zip_bytes(), ".zip")
    try:
        dest = tmp_path / "dest"
        stream_zip(url, dest)
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_stream_zip_rejects_tampered_crc(serve, tmp_path):
    data = bytearray(_zip_bytes())
    # corrupt a byte inside the deflate data of the biggest entry
    idx = data.index(b"PK\x03\x04", 100)
    data[idx + 400] ^= 0xFF
    url, httpd = serve(bytes(data), ".zip")
    try:
        with pytest.raises(Exception):
            stream_zip(url, tmp_path / "dest")
    finally:
        httpd.shutdown()


def test_stream_archive_dispatch_zip(serve, tmp_path):
    url, httpd = serve(_zip_bytes(), ".zip")
    try:
        dest = tmp_path / "dest"
        stream_archive(url, dest)
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_stream_tar_gz(serve, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    url, httpd = serve(buf.getvalue(), ".tar.gz")
    try:
        dest = tmp_path / "dest"
        stream_tar(url, dest)
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_rar_and_7z_refused(serve, tmp_path):
    url, httpd = serve(b"not really", ".rar")
    try:
        with pytest.raises(ValueError, match="repack"):
            stream_archive(url, tmp_path / "dest")
    finally:
        httpd.shutdown()


def test_zip_slip_rejected(tmp_path):
    from anticompress.zipstream import _safe_path

    with pytest.raises(ValueError):
        _safe_path(tmp_path / "dest", "../evil.txt")
    with pytest.raises(ValueError):
        _safe_path(tmp_path / "dest", "/abs/evil.txt")
    assert _safe_path(tmp_path / "dest", "a/b.txt").name == "b.txt"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_zipstream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.zipstream'`

- [ ] **Step 3: Write the streaming zip/tar module**

`anticompress/zipstream.py`:
```python
"""Streaming extraction of plain zip/tar archives from a URL (no local copy)."""
from __future__ import annotations

import binascii
import struct
import tarfile
import zlib
from pathlib import Path
from typing import Callable, Iterator

import httpx
import zstandard

Progress = Callable[[int], None] | None

_ZIP_LOCAL = struct.Struct("<HHHHHIIIHH")  # after 4-byte sig: ver, flags, method, mtime, mdate, crc, csize, usize, nlen, elen
_DESC = struct.Struct("<IIII")  # sig, crc, csize, usize
_DESC64 = struct.Struct("<IIQQ")  # sig, crc, csize64, usize64


def _safe_path(dest_dir: Path, name: str) -> Path:
    name = name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"unsafe archive path: {name}")
    p = dest_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class _BufferedBlocks:
    """Reads exact byte counts from an iterator of blocks."""

    def __init__(self, blocks: Iterator[bytes]):
        self._blocks = iter(blocks)
        self._buf = bytearray()

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            block = next(self._blocks, b"")
            if not block:
                raise ValueError("unexpected end of stream")
            self._buf += block
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def peek(self, n: int) -> bytes:
        while len(self._buf) < n:
            block = next(self._blocks, b"")
            if not block:
                raise ValueError("unexpected end of stream")
            self._buf += block
        return bytes(self._buf[:n])


def _zip64_sizes(extra: bytes, usize: int, csize: int) -> tuple[int, int]:
    if usize != 0xFFFFFFFF and csize != 0xFFFFFFFF:
        return usize, csize
    i = 0
    while i + 4 <= len(extra):
        eid, esz = struct.unpack_from("<HH", extra, i)
        if eid == 1:
            off = i + 4
            if usize == 0xFFFFFFFF:
                usize = struct.unpack_from("<Q", extra, off)[0]
                off += 8
            if csize == 0xFFFFFFFF:
                csize = struct.unpack_from("<Q", extra, off)[0]
            return usize, csize
        i += 4 + esz
    raise ValueError("zip64 sizes missing from extra field")


def _stream_zip_blocks(blocks: Iterator[bytes], dest_dir: Path, progress: Progress) -> None:
    b = _BufferedBlocks(blocks)
    total_done = 0
    while True:
        sig = b.peek(4)
        if sig == b"PK\x05\x06" or sig == b"PK\x01\x02" or len(sig) < 4:
            return  # end of central directory / central directory / clean EOF
        if sig != b"PK\x03\x04":
            raise ValueError(f"bad local header signature: {sig[:4]!r}")
        b.read(4)
        (ver, flags, method, _mt, _md, crc, csize, usize, nlen, elen) = _ZIP_LOCAL.unpack(b.read(26))
        name = b.read(nlen).decode("utf-8", errors="replace")
        extra = b.read(elen)
        usize, csize = _zip64_sizes(extra, usize, csize)
        has_desc = bool(flags & 0x08)
        if name.endswith("/"):
            _safe_path(dest_dir, name).mkdir(parents=True, exist_ok=True)
            continue

        out = _safe_path(dest_dir, name)
        crc32 = 0
        size_out = 0
        with out.open("wb") as f:
            if method == 0:
                remaining = usize
                while remaining > 0:
                    chunk = b.read(min(remaining, 1 << 16))
                    f.write(chunk)
                    crc32 = binascii.crc32(chunk, crc32)
                    size_out += len(chunk)
                    remaining -= len(chunk)
            elif method == 8:
                d = zlib.decompressobj(-15)
                if has_desc:
                    while not d.eof:
                        chunk = b.read(1 << 16)
                        data = d.decompress(chunk)
                        if data:
                            f.write(data)
                            crc32 = binascii.crc32(data, crc32)
                            size_out += len(data)
                else:
                    remaining = csize
                    while remaining > 0:
                        chunk = b.read(min(remaining, 1 << 16))
                        data = d.decompress(chunk)
                        if data:
                            f.write(data)
                            crc32 = binascii.crc32(data, crc32)
                            size_out += len(data)
                        remaining -= len(chunk)
                    if not d.eof:
                        raise ValueError(f"truncated deflate stream in {name}")
            else:
                raise ValueError(f"unsupported zip method {method} in {name}")
        if has_desc:
            head = b.read(16)
            if head[:4] == b"PK\x07\x08":
                _sig, crc_d, cs_d, us_d = _DESC.unpack(head)
                if cs_d == 0xFFFFFFFF:  # zip64 descriptor: 8 more bytes
                    _sig, crc_d, cs_d, us_d = _DESC64.unpack(head + b.read(8))
            else:
                crc_d, cs_d, us_d = struct.unpack("<III", head[:12])
            if crc_d != (crc32 & 0xFFFFFFFF):
                raise ValueError(f"CRC mismatch in {name} (data descriptor)")
        else:
            if (crc32 & 0xFFFFFFFF) != crc:
                raise ValueError(f"CRC mismatch in {name}")
            if size_out != usize:
                raise ValueError(f"size mismatch in {name}")
        total_done += size_out
        if progress:
            progress(total_done)


def stream_zip(url: str, dest_dir: Path, progress: Progress = None) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            _stream_zip_blocks(r.iter_bytes(1 << 16), dest_dir, progress)


class _IterStream:
    """File-like adapter over a block iterator (for tarfile)."""

    def __init__(self, blocks: Iterator[bytes]):
        self._blocks = iter(blocks)
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            out, self._buf = self._buf, b""
            return out
        while len(self._buf) < n:
            block = next(self._blocks, b"")
            if not block:
                break
            self._buf += block
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def stream_tar(url: str, dest_dir: Path, progress: Progress = None) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            fobj: object = _IterStream(r.iter_bytes(1 << 16))
            if url.lower().endswith((".tar.zst", ".tzst")):
                dctx = zstandard.ZstdDecompressor()
                fobj = dctx.stream_reader(fobj)  # type: ignore[arg-type]
            with tarfile.open(fileobj=fobj, mode="r|*") as tf:  # type: ignore[arg-type]
                total = 0
                for member in tf:
                    if member.isfile():
                        out = _safe_path(dest_dir, member.name)
                        src = tf.extractfile(member)
                        with src, out.open("wb") as dst:
                            while True:
                                block = src.read(1 << 16)
                                if not block:
                                    break
                                dst.write(block)
                                total += len(block)
                        if progress:
                            progress(total)
                    elif member.isdir():
                        _safe_path(dest_dir, member.name).mkdir(parents=True, exist_ok=True)


def stream_archive(url: str, dest_dir: Path, progress: Progress = None) -> None:
    """Dispatch by extension. rar/7z refuse loudly (solid archives can't stream)."""
    low = url.lower().split("?")[0]
    if low.endswith(".zip"):
        stream_zip(url, dest_dir, progress)
    elif low.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tzst")):
        stream_tar(url, dest_dir, progress)
    elif low.endswith((".rar", ".7z")):
        raise ValueError("rar/7z cannot stream — run `anticompress repack` first")
    else:
        raise ValueError(f"unsupported archive format: {low}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/zipstream.py tests/test_zipstream.py
git commit -m "feat: streaming zip/tar extraction with CRC verification"
```

---

### Task 7: CLI — repack, dl, install + end-to-end round trip

**Files:**
- Create: `anticompress/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `repack` (Task 4), `download_package` (Task 5), `stream_archive` (Task 6), `extract_package` (Task 3), `deserialize` (Task 1).
- Produces: `main(argv=None) -> None` — argparse with `repack ARCHIVE -o OUT [--7z PATH]`, `dl URL -o DEST [--workers N]`, `install PACKAGE_DIR -o DEST` (local extract, chunks deleted as consumed). Space gates before any download/extract.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:
```python
import functools
import shutil
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anticompress.cli import main
from tests.helpers import assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}
SEVEN_ZIP = shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
needs_7z = pytest.mark.skipif(not Path(SEVEN_ZIP).exists(), reason="7-Zip not installed")


def _make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def serve_dir(tmp_path):
    def _serve(directory: Path):
        handler = functools.partial(_QuietHandler, directory=str(directory))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{httpd.server_port}", httpd

    return _serve


@needs_7z
def test_e2e_repack_dl_roundtrip(tmp_path, serve_dir):
    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "game.acpkg"
    main(["repack", str(src), "-o", str(pkg), "--7z", SEVEN_ZIP])
    assert (pkg / "manifest.json").is_file()
    assert list(pkg.glob("chunk-*.zst"))

    url, httpd = serve_dir(pkg)
    try:
        dest = tmp_path / "dest"
        main(["dl", url, "-o", str(dest), "--workers", "4"])
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


@needs_7z
def test_e2e_repack_install_local(tmp_path):
    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "game.acpkg"
    main(["repack", str(src), "-o", str(pkg), "--7z", SEVEN_ZIP])
    dest = tmp_path / "dest"
    main(["install", str(pkg), "-o", str(dest)])
    assert_trees_identical(FILES, dest)
    assert not list(pkg.glob("chunk-*.zst"))  # chunks consumed


def test_dl_plain_zip(tmp_path, serve_dir):
    src = tmp_path / "game.zip"
    _make_zip(src)
    url, httpd = serve_dir(tmp_path)
    try:
        dest = tmp_path / "dest"
        main(["dl", f"{url}/game.zip", "-o", str(dest)])
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_space_gate_blocks(tmp_path):
    with pytest.raises(SystemExit):
        main(["dl", "http://127.0.0.1:1/x.zip", "-o", str(tmp_path / "dest")])


def test_dl_missing_archive_fails(tmp_path):
    with pytest.raises(SystemExit):
        main(["repack", str(tmp_path / "nope.rar"), "-o", str(tmp_path / "pkg")])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.cli'`

- [ ] **Step 3: Write the CLI**

`anticompress/cli.py`:
```python
"""AntiCompress CLI: repack / dl / install."""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import httpx

from .downloader import download_package
from .extractor import extract_package
from .format import read_manifest, deserialize
from .repacker import repack
from .zipstream import stream_archive

BUFFER_BYTES = 64 * 1024 * 1024  # 64 MiB safety buffer


def _free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def _progress(prefix: str, total: int) -> callable:
    start = time.monotonic()
    last = 0

    def cb(done: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last < 0.2 and done < total:
            return
        last = now
        elapsed = max(now - start, 1e-6)
        speed = done / elapsed / 1e6
        if total:
            pct = done / total * 100
            line = f"\r{prefix} {pct:6.1f}%  {done / 1e9:6.2f}/{total / 1e9:6.2f} GB  {speed:6.1f} MB/s"
        else:
            line = f"\r{prefix} {done / 1e9:6.2f} GB  {speed:6.1f} MB/s"
        print(line, end="", flush=True)

    return cb


def _fail(msg: str) -> None:
    print(f"\nerror: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_repack(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    out = Path(args.out)
    if not archive.is_file():
        _fail(f"{archive} not found")
    free = _free_space(out if out.exists() else out.parent)
    src = archive.stat().st_size
    if free < src:
        _fail(f"need ~{src / 1e9:.1f} GB free for repack (have {free / 1e9:.1f} GB); "
              f"put the source archive on a drive with room")
    print(f"repacking {archive} → {out}/ (single pass, this can take a while)")
    t0 = time.monotonic()
    try:
        m = repack(archive, out, seven_zip=args.seven_zip, progress=_progress("repack", src))
    except (RuntimeError, ValueError) as e:
        _fail(str(e))
    print(f"\ndone in {time.monotonic() - t0:.0f}s: {len(m.files)} files, "
          f"{m.total_size / 1e9:.1f} GB → {out}")


def _acpkg_or_stream(url: str, dest: Path, workers: int) -> None:
    """Try .acpkg (manifest present) first; fall back to plain zip/tar streaming."""
    base = url.rstrip("/") + "/"
    is_acpkg = url.rstrip("/").endswith(".acpkg")
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as c:
            r = c.get(base + "manifest.json")
            if r.status_code == 404 and not is_acpkg:
                _stream_plain(url, dest)
                return
            r.raise_for_status()
            m = deserialize(r.text)
    except httpx.HTTPError as e:
        if is_acpkg:
            _fail(f"cannot fetch .acpkg manifest: {e}")
        _stream_plain(url, dest)
        return
    except ValueError as e:
        if is_acpkg:
            _fail(f"invalid .acpkg: {e}")
        _stream_plain(url, dest)
        return
    free = _free_space(dest)
    need = m.total_size + BUFFER_BYTES
    if free < need:
        _fail(f"need {need / 1e9:.1f} GB free (have {free / 1e9:.1f} GB)")
    chunk_dir = dest / ".anticompress-chunks"
    try:
        download_package(base, dest, chunk_dir, workers=workers, progress=_progress("download", m.total_size))
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: {len(m.files)} files extracted to {dest}")


def _stream_plain(url: str, dest: Path) -> None:
    try:
        stream_archive(url, dest, progress=_progress("download", 0))
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: extracted to {dest}")


def cmd_dl(args: argparse.Namespace) -> None:
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    _acpkg_or_stream(args.url, dest, args.workers)


def cmd_install(args: argparse.Namespace) -> None:
    pkg = Path(args.package)
    dest = Path(args.dest)
    manifest_path = pkg / "manifest.json"
    if not manifest_path.is_file():
        _fail(f"{pkg} is not a .acpkg folder (no manifest.json)")
    try:
        m = read_manifest(manifest_path)
    except ValueError as e:
        _fail(str(e))
    free = _free_space(dest)
    need = m.total_size + BUFFER_BYTES
    if free < need:
        _fail(f"need {need / 1e9:.1f} GB free (have {free / 1e9:.1f} GB)")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        extract_package(pkg, dest, m, delete_chunks=True, progress=_progress("install", m.total_size))
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: {len(m.files)} files extracted to {dest}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="anticompress", description="Steam-style streaming download + decompress")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("repack", help="convert RAR/7z/zip into a .acpkg chunk folder")
    p.add_argument("archive")
    p.add_argument("-o", "--out", required=True, help="output .acpkg folder")
    p.add_argument("--7z", dest="seven_zip", default="7z", help="path to 7z.exe")
    p.set_defaults(fn=cmd_repack)

    p = sub.add_parser("dl", help="stream-download + extract (.acpkg folder, zip, tar.gz…); 1x disk space")
    p.add_argument("url")
    p.add_argument("-o", "--dest", required=True)
    p.add_argument("--workers", type=int, default=8, help="parallel chunk connections")
    p.set_defaults(fn=cmd_dl)

    p = sub.add_parser("install", help="extract a local .acpkg folder (chunks deleted as consumed)")
    p.add_argument("package", help="path to the .acpkg folder")
    p.add_argument("-o", "--dest", required=True)
    p.set_defaults(fn=cmd_install)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/cli.py tests/test_cli.py
git commit -m "feat: CLI — repack / dl / install with space gates and progress"
```

---

### Task 8: README + full suite verification

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the complete CLI from Task 7.

- [ ] **Step 1: Write the README**

`README.md`:
```markdown
# AntiCompress

Steam-style streaming download + decompression for Windows. The archive never
exists on disk alongside its content — **you only ever need room for the
final game, never the game + its archive.**

## Install

```
pip install -r requirements-dev.txt
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
  archives), verifies the listing-order assumption by sampling, and deletes
  each multi-part volume the moment 7z passes it.
- `dl` fetches chunks in parallel, verifies each chunk's SHA-256 before
  decompressing, assembles files under `.acpart` temps, verifies per-file
  SHA-256, atomically renames, then deletes each chunk as consumed.
- Resume is free: chunk files that exist and match their hash are skipped.

## Known limitation

Solid multi-part RAR (FitGirl-style) cannot be streamed from a partial
download — no tool can, the format forbids it. Repack them once (works even
on a disk that only fits the final game, thanks to volume deletion), then
they stream forever.
```

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all tests pass (both `7z`-based and pure-Python)

- [ ] **Step 3: Verify the CLI end-to-end by hand**

Run:
```bash
python -m anticompress.cli --help
python -m pip install -e .
anticompress --help
```
Expected: both help screens render; `anticompress` works as a console command.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README with install, usage, and limitations"
```
