import shutil
import os
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
    big = os.urandom(3_000_000)  # incompressible → guaranteed multiple volumes
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
    """Chunk-stream CRC disagreeing with the archive's recorded CRC → refused."""
    from anticompress.repacker import _verify_sample

    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "pkg"
    m = repack(src, pkg, seven_zip=SEVEN_ZIP)
    crc_map = {f.path: "00000000" for f in m.files}  # wrong CRCs → order check must fail
    with pytest.raises(RuntimeError, match="order assumption"):
        _verify_sample(m, pkg, crc_map)
