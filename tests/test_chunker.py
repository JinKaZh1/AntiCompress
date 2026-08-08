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
