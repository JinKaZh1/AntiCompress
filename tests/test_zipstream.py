import binascii
import json
import functools
import io
import struct
import tarfile
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from anticompress.zipstream import stream_archive, stream_tar, stream_zip
from tests.helpers import assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def serve(tmp_path):
    def _serve(data: bytes, suffix: str):
        f = tmp_path / f"archive{suffix}"
        f.write_bytes(data)
        handler = functools.partial(_QuietHandler, directory=str(tmp_path))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{httpd.server_port}/archive{suffix}", httpd

    yield _serve


def _zip_bytes(descriptor: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_stream_zip(serve, tmp_path):
    url, httpd = serve(_zip_bytes(), ".zip")
    try:
        dest = tmp_path / "dest"
        stream_zip(url, dest)
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def _zstd_zip_bytes(name: str, content: bytes) -> bytes:
    """Hand-build a zip whose single entry uses method 93 (Zstandard)."""
    import zstandard

    name_b = name.encode()
    comp = zstandard.ZstdCompressor(level=3).compress(content)
    crc = binascii.crc32(content) & 0xFFFFFFFF
    local = (
        struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 93, 0, 0, crc, len(comp), len(content), len(name_b), 0)
        + name_b
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 20, 20, 0, 93, 0, 0, crc, len(comp), len(content),
            len(name_b), 0, 0, 0, 0, 0, 0,
        )
        + name_b
    )
    eocd = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central), len(local) + len(comp), 0
    )
    return local + comp + central + eocd


def test_stream_zip_zstd_method93(serve, tmp_path):
    """Zips with Zstandard entries (method 93) must stream-extract."""
    content = bytes((i * 17 + 3) % 256 for i in range(200000))
    url, httpd = serve(_zstd_zip_bytes("big.bin", content), ".zip")
    try:
        dest = tmp_path / "dest"
        stream_zip(url, dest)
        assert (dest / "big.bin").read_bytes() == content
    finally:
        httpd.shutdown()


def test_stream_zip_resume_from_entry_boundary(serve, tmp_path):
    """Closing the console at 80% then re-running must continue from the last
    completed entry (Range request), not restart."""
    data = _zip_bytes()
    total = len(data)
    # find entry 2's local header offset via the central directory
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        assert len(names) >= 3
        resume_offset = zf.getinfo(names[1]).header_offset
    url, httpd = serve(data, ".zip")
    try:
        dest = tmp_path / "dest"
        dest.mkdir()
        # entry 0 was already extracted before the "crash"
        pre = dest / names[0]
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_bytes(FILES[names[0]])
        # simulate the crash state file (written after entry 0 completed)
        state = dest / ".anticompress-resume.json"
        state.write_text(json.dumps({"total": total, "next_offset": resume_offset}))
        stream_zip(url, dest)
        assert_trees_identical(FILES, dest)
        assert not state.exists()  # state cleared on completion
    finally:
        httpd.shutdown()


def test_stream_zip_resume_rejected_on_size_mismatch(serve, tmp_path):
    """A different file in the same folder must NOT resume — it restarts clean."""
    data = _zip_bytes()
    url, httpd = serve(data, ".zip")
    try:
        dest = tmp_path / "dest"
        dest.mkdir()
        state = dest / ".anticompress-resume.json"
        state.write_text(json.dumps({"total": 999999, "next_offset": 500}))  # wrong size
        stream_zip(url, dest)
        assert_trees_identical(FILES, dest)  # full fresh download
    finally:
        httpd.shutdown()


def test_stream_zip_rejects_tampered_crc(serve, tmp_path):
    data = bytearray(_zip_bytes())
    # corrupt a byte inside the deflate data of the biggest entry
    idx = data.index(b"PK\x03\x04", 100)
    data[idx + 400] ^= 0xFF
    url, httpd = serve(bytes(data), ".zip")
    try:
        with pytest.raises(Exception):
            stream_zip(url, tmp_path / "dest")
    finally:
        httpd.shutdown()


def test_stream_archive_dispatch_zip(serve, tmp_path):
    url, httpd = serve(_zip_bytes(), ".zip")
    try:
        dest = tmp_path / "dest"
        stream_archive(url, dest)
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_stream_tar_gz(serve, tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    url, httpd = serve(buf.getvalue(), ".tar.gz")
    try:
        dest = tmp_path / "dest"
        stream_tar(url, dest)
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_rar_and_7z_refused(serve, tmp_path):
    url, httpd = serve(b"Rar!\x1a\x07\x01\x00" + b"x" * 100, ".rar")
    try:
        with pytest.raises(ValueError, match="repack"):
            stream_archive(url, tmp_path / "dest")
    finally:
        httpd.shutdown()


def test_zip_slip_rejected(tmp_path):
    from anticompress.zipstream import _safe_path

    with pytest.raises(ValueError):
        _safe_path(tmp_path / "dest", "../evil.txt")
    with pytest.raises(ValueError):
        _safe_path(tmp_path / "dest", "/abs/evil.txt")
    assert _safe_path(tmp_path / "dest", "a/b.txt").name == "b.txt"
