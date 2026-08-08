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


def _list_slt(archive: Path, seven_zip: str) -> list[tuple[str, int, str | None]]:
    """Return [(path, size, crc)] from `7z l -slt` (files only, listing order).

    Directory entries are skipped via their Attributes line (7-Zip lists them
    without a trailing slash); split-archive metadata pseudo-entries have no
    Attributes line and are skipped too. CRC comes from the archive's own
    metadata and is used to verify the listing-order assumption.
    """
    p = _run_7z(["l", "-slt", str(archive)], seven_zip)
    out, err = p.communicate()
    if p.returncode != 0:
        raise RuntimeError(f"7z list failed: {err.decode(errors='replace')[-500:]}")
    entries: list[tuple[str, int, str | None]] = []
    cur_path: str | None = None
    cur_size: int | None = None
    cur_attrs = ""
    cur_attrs_seen = False
    cur_crc: str | None = None
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("Path = "):
            if (
                cur_path is not None
                and cur_size is not None
                and cur_attrs_seen
                and "D" not in cur_attrs
                and not cur_path.endswith("/")
            ):
                entries.append((cur_path, cur_size, cur_crc))
            cur_path = line[7:]
            cur_size = None
            cur_attrs = ""
            cur_attrs_seen = False
            cur_crc = None
        elif line.startswith("Size = ") and cur_path is not None:
            cur_size = int(line[7:])
        elif line.startswith("Attributes = ") and cur_path is not None:
            cur_attrs = line[13:]
            cur_attrs_seen = True
        elif line.startswith("CRC = ") and cur_path is not None:
            cur_crc = line[6:].strip().lower()
    if (
        cur_path is not None
        and cur_size is not None
        and cur_attrs_seen
        and "D" not in cur_attrs
        and not cur_path.endswith("/")
    ):
        entries.append((cur_path, cur_size, cur_crc))
    return entries


def _find_volumes(archive: Path) -> list[Path]:
    """Multi-part volume chains: RAR5 `name.partNN.rar`, RAR4 `name.rNN`,
    7-Zip split `name.7z.NNN`. Single file → [] (no deletion)."""
    stem = archive.name[: -len(archive.suffix)]
    if archive.suffix.lower() == ".rar":
        parts = sorted(archive.parent.glob(stem + ".part*.rar"))
        if parts:
            return parts
        olds = sorted(archive.parent.glob(stem + ".r??"))
        if olds:
            return [archive] + [v for v in olds if v != archive]
    splits = sorted(archive.parent.glob(stem + ".[0-9][0-9][0-9]"))
    if splits:
        return splits
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


def _verify_sample(m: Manifest, chunk_dir: Path, crc_map: dict[str, str], max_samples: int = 8) -> None:
    """Verify the listing-order assumption: sampled files' CRC-32 computed from
    the chunk stream must match the archive's own recorded CRC-32.

    Works after volumes were deleted (no archive re-open needed) and catches
    order swaps between the `7z l` listing and the `-so` stream.
    """
    import binascii

    top: dict[str, list[FileEntry]] = {}
    for fe in m.files:
        if fe.path in crc_map:
            top.setdefault(fe.path.split("/")[0], []).append(fe)
    rng = random.Random(0)
    samples = [rng.choice(v) for v in top.values() if v]
    dctx = zstandard.ZstdDecompressor()
    decompressed = b""
    pos = 0
    ci = 0
    for fe in samples[:max_samples]:
        need = fe.size
        crc = 0
        while need > 0:
            if pos >= len(decompressed):
                if ci >= len(m.chunks):
                    raise RuntimeError("stream ended early during verification")
                raw = (chunk_dir / chunk_name(ci)).read_bytes()
                dobj = dctx.decompressobj()
                decompressed = dobj.decompress(raw) + dobj.flush()
                if len(decompressed) != chunk_expected_size(m, ci):
                    raise RuntimeError("chunk size mismatch during verification")
                ci += 1
                pos = 0
            take = min(need, len(decompressed) - pos)
            crc = binascii.crc32(decompressed[pos : pos + take], crc)
            pos += take
            need -= take
        if f"{crc & 0xFFFFFFFF:08x}" != crc_map[fe.path]:
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
    crc_map: dict[str, str] = {}
    for path, size, crc in entries:
        files.append(FileEntry(path=path.replace("\\", "/"), size=size, offset=offset, sha256=""))
        if crc:
            crc_map[path.replace("\\", "/")] = crc
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
        _verify_sample(m, out_dir, crc_map)
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    # 7z has exited — final sweep for any volumes still on disk (now unlocked)
    for k, v in enumerate(volumes):
        if not deleted[k]:
            try:
                v.unlink()
                deleted[k] = True
            except OSError:
                pass  # best-effort, never fatal
    return m


def _track(stream, fn: Callable[[bytes], None]):
    for block in stream:
        fn(block)
        yield block
