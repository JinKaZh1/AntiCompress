import functools
import hashlib
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anticompress.downloader import download_package
from anticompress.format import chunk_name
from tests.helpers import build_package, assert_trees_identical

FILES = {
    "game.exe": b"MZ" + bytes(range(200)),
    "data/pack.bin": bytes((i * 7 + 3) % 256 for i in range(2_500_000)),
    "readme.txt": b"hello anticompress\n",
}


class _CountingHandler(SimpleHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests.append(self.path)
        super().do_GET()


@pytest.fixture
def server(tmp_path):
    pkg = tmp_path / "pkg"
    m = build_package(FILES, pkg)
    _CountingHandler.requests = []
    handler = functools.partial(_CountingHandler, directory=str(pkg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, m, pkg
    httpd.shutdown()


def test_download_roundtrip(server, tmp_path):
    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    dest = tmp_path / "dest"
    chunks = tmp_path / "chunks"
    download_package(base, dest, chunks, workers=8)
    assert_trees_identical(FILES, dest)
    assert sorted(chunks.iterdir()) == []  # all chunks consumed


def test_parallel_fetch_all_chunks(server, tmp_path):
    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    download_package(base, tmp_path / "dest", tmp_path / "chunks", workers=16)
    assert len([p for p in _CountingHandler.requests if "chunk-" in p]) == len(m.chunks)


def test_resume_fetches_only_missing(tmp_path):
    """A fully-covered chunk (1 MiB file = exactly chunk 0) must not be re-fetched."""
    pkg = tmp_path / "pkg"
    one_mib = bytes((i * 11) % 256 for i in range(1024 * 1024))
    files = {"a.bin": one_mib, "b.txt": b"tail"}
    m = build_package(files, pkg)
    _CountingHandler.requests = []
    handler = functools.partial(_CountingHandler, directory=str(pkg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.bin").write_bytes(one_mib)  # already present & verified → chunk 0 covered
        download_package(f"http://127.0.0.1:{httpd.server_port}", dest, tmp_path / "chunks")
        assert (dest / "b.txt").read_bytes() == b"tail"
        fetched = [p for p in _CountingHandler.requests if "chunk-" in p]
        assert fetched == ["/chunk-000001.zst"]  # only the missing chunk
    finally:
        httpd.shutdown()


def test_window_stays_bounded_during_download(server, tmp_path):
    """The core scale guarantee: chunks on disk must never exceed the fetch
    window — fetch-all-then-extract would double disk usage at 100 GB."""
    import anticompress.downloader as dl

    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    chunks = tmp_path / "chunks"
    dest = tmp_path / "dest"
    peak = [0]
    real_fetch = dl._fetch_chunk

    def tracking_fetch(client, url, tmp_path_, final_path, sha256, tries=3):
        real_fetch(client, url, tmp_path_, final_path, sha256, tries)
        n = len([p for p in chunks.iterdir() if p.suffix == ".zst"])
        peak[0] = max(peak[0], n)

    dl._fetch_chunk = tracking_fetch
    try:
        download_package(base, dest, chunks, workers=2)
    finally:
        dl._fetch_chunk = real_fetch
    assert_trees_identical(FILES, dest)
    window = max(2 * dl.WINDOW_FACTOR, 2 + 2)
    assert peak[0] <= window + 4, f"chunk window exceeded: {peak[0]} on disk"


def test_corrupt_chunk_on_server_fails_after_retries(server, tmp_path):
    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    victim = m.chunks[0]
    (pkg / chunk_name(victim.index)).write_bytes(b"bad!")  # corrupt served chunk
    with pytest.raises(Exception):
        download_package(base, tmp_path / "dest", tmp_path / "chunks", workers=1)


def test_manifest_tamper_on_server_rejected(server, tmp_path):
    httpd, m, pkg = server
    base = f"http://127.0.0.1:{httpd.server_port}"
    import json

    mp = pkg / "manifest.json"
    d = json.loads(mp.read_text())
    d["total_size"] = 1
    mp.write_text(json.dumps(d, sort_keys=True, separators=(",", ":")))
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        download_package(base, tmp_path / "dest", tmp_path / "chunks")
