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
