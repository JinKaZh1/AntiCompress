import functools
import shutil
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anticompress.cli import main
from tests.helpers import assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}
SEVEN_ZIP = shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe"
needs_7z = pytest.mark.skipif(not Path(SEVEN_ZIP).exists(), reason="7-Zip not installed")


def _make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def serve_dir(tmp_path):
    def _serve(directory: Path):
        handler = functools.partial(_QuietHandler, directory=str(directory))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{httpd.server_port}", httpd

    return _serve


@needs_7z
def test_e2e_repack_dl_roundtrip(tmp_path, serve_dir):
    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "game.acpkg"
    main(["repack", str(src), "-o", str(pkg), "--7z", SEVEN_ZIP])
    assert (pkg / "manifest.json").is_file()
    assert list(pkg.glob("chunk-*.zst"))

    url, httpd = serve_dir(pkg)
    try:
        dest = tmp_path / "dest"
        main(["dl", url, "-o", str(dest), "--workers", "4"])
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


@needs_7z
def test_e2e_repack_install_local(tmp_path):
    src = tmp_path / "game.zip"
    _make_zip(src)
    pkg = tmp_path / "game.acpkg"
    main(["repack", str(src), "-o", str(pkg), "--7z", SEVEN_ZIP])
    dest = tmp_path / "dest"
    main(["install", str(pkg), "-o", str(dest)])
    assert_trees_identical(FILES, dest)
    assert not list(pkg.glob("chunk-*.zst"))  # chunks consumed


def test_dl_plain_zip(tmp_path, serve_dir):
    src = tmp_path / "game.zip"
    _make_zip(src)
    url, httpd = serve_dir(tmp_path)
    try:
        dest = tmp_path / "dest"
        main(["dl", f"{url}/game.zip", "-o", str(dest)])
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_dlproxy_style_server_streams_zip(tmp_path):
    """A server that answers EVERY path with the actual file (dlproxy-style)
    must not be mistaken for an .acpkg and must stream-extract correctly —
    the manifest probe may only peek, never buffer the file."""
    import io
    import shutil

    src = tmp_path / "game.zip"
    _make_zip(src)

    class _AnyPathHandler(_QuietHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.end_headers()
            with open(Path(self.directory) / "game.zip", "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    handler = functools.partial(_AnyPathHandler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/game.zip"
        dest = tmp_path / "dest"
        main(["dl", url, "-o", str(dest)])
        assert_trees_identical(FILES, dest)
    finally:
        httpd.shutdown()


def test_space_gate_blocks(tmp_path):
    with pytest.raises(SystemExit):
        main(["dl", "http://127.0.0.1:1/x.zip", "-o", str(tmp_path / "dest")])


def test_dl_missing_archive_fails(tmp_path):
    with pytest.raises(SystemExit):
        main(["repack", str(tmp_path / "nope.rar"), "-o", str(tmp_path / "pkg")])
