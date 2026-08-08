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
