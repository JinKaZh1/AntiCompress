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
    last_reported = -1
    for block in stream:
        if not block:
            continue
        buf += block
        total += len(block)
        while len(buf) >= CHUNK_SIZE:
            _write_chunk(cctx, index, bytes(buf[:CHUNK_SIZE]), out_dir, chunks)
            del buf[:CHUNK_SIZE]
            index += 1
        if progress and total != last_reported:
            progress(total)
            last_reported = total
    if buf:
        _write_chunk(cctx, index, bytes(buf), out_dir, chunks)
    if progress and total != last_reported:
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
