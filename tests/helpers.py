"""Shared test utilities: build packages from in-memory files, compare trees."""
from __future__ import annotations

from pathlib import Path

from anticompress.chunker import write_chunks
from anticompress.format import Manifest, FileEntry, write_manifest


def build_package(files: dict[str, bytes], pkg_dir: Path) -> Manifest:
    """Build a .acpkg chunk folder from {relative_path: content} bytes."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    chunks, total = write_chunks(iter(files.values()), pkg_dir)
    manifest = Manifest(total_size=total, chunks=chunks)
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
