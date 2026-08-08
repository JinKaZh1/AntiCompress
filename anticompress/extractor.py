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
            # advance past files fully consumed by the skipped region
            # (covered files are already verified on disk; empty files get created)
            while fi < len(m.files) and m.files[fi].offset + m.files[fi].size <= stream_pos:
                if m.files[fi].size == 0:
                    final = dest_dir / m.files[fi].path
                    final.parent.mkdir(parents=True, exist_ok=True)
                    final.write_bytes(b"")
                fi += 1
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

    # drain any trailing zero-byte files (the pos-loop above can't reach them)
    while fi < len(m.files) and m.files[fi].size == 0:
        final = dest_dir / m.files[fi].path
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"")
        fi += 1

    close_tmp(False)
    if fi != len(m.files):
        raise ValueError("decompressed stream shorter than manifest")
