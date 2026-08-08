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

_TIMEOUT = httpx.Timeout(connect=30, read=120, write=60, pool=30)


def _safe_path(dest_dir: Path, name: str) -> Path:
    name = name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"unsafe archive path: {name}")
    p = dest_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _sniff(head: bytes) -> str | None:
    """Identify the archive format from magic bytes (URL extensions lie)."""
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head[257:262] == b"ustar":
        return "tar"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"BZh"):
        return "bz2"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    return None


def _prepend(first: bytes, it: Iterator[bytes]) -> Iterator[bytes]:
    yield first
    yield from it


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
            elif method == 93:
                # Zstandard-compressed entry (APPNOTE method 93; modern archivers)
                d = zstandard.ZstdDecompressor().decompressobj()
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
                        raise ValueError(f"truncated zstd stream in {name}")
                tail = d.flush()  # completes the frame, verifies its checksum
                if tail:
                    f.write(tail)
                    crc32 = binascii.crc32(tail, crc32)
                    size_out += len(tail)
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
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
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


def _stream_tar_blocks(
    blocks: Iterator[bytes], dest_dir: Path, progress: Progress, is_zstd: bool = False
) -> None:
    fobj: object = _IterStream(blocks)
    if is_zstd:
        dctx = zstandard.ZstdDecompressor()
        fobj = dctx.stream_reader(fobj)  # type: ignore[arg-type]
    mode = "r|" if is_zstd else "r|*"
    with tarfile.open(fileobj=fobj, mode=mode) as tf:  # type: ignore[arg-type]
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


def stream_tar(url: str, dest_dir: Path, progress: Progress = None) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            _stream_tar_blocks(r.iter_bytes(1 << 16), dest_dir, progress)


def stream_archive(url: str, dest_dir: Path, progress: Progress = None) -> None:
    """Stream-extract whatever the URL actually IS (magic bytes, not the URL
    extension — dlproxy-style links carry no extension). rar/7z refuse loudly."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            it = r.iter_bytes(1 << 16)
            first = next(it, b"")
            kind = _sniff(first)
            blocks = _prepend(first, it)
            if kind == "zip":
                _stream_zip_blocks(blocks, dest_dir, progress)
            elif kind in ("tar", "gzip", "xz", "bz2"):
                _stream_tar_blocks(blocks, dest_dir, progress)
            elif kind == "zstd":
                _stream_tar_blocks(blocks, dest_dir, progress, is_zstd=True)
            elif kind in ("rar", "7z"):
                raise ValueError("rar/7z cannot stream — run `anticompress repack` first")
            else:
                raise ValueError(f"unsupported archive format: {url}")
