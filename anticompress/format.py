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
