import functools
import io
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
