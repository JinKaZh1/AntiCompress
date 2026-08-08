import functools
import json
import sys
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import anticompress.choose as choose
from anticompress.choose import main as choose_main
from tests.helpers import assert_trees_identical

FILES = {"game.exe": b"MZ" + bytes(range(100)), "data.bin": bytes(i % 256 for i in range(3000))}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def serve_zip(tmp_path):
    f = tmp_path / "game.zip"
    with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in FILES.items():
            zf.writestr(name, content)
    handler = functools.partial(_QuietHandler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/game.zip"
    httpd.shutdown()


def _msg(url: str, filename: str = "game.zip", size: int = 1000) -> dict:
    return {"type": "download", "url": url, "filename": filename, "size": size}


def _write_msg(tmp_path, msg: dict) -> tuple[Path, Path]:
    mp = tmp_path / "msg.json"
    mp.write_text(json.dumps(msg))
    return mp, tmp_path / "result.json"


def test_choice_normal_writes_result(tmp_path, monkeypatch):
    mp, rp = _write_msg(tmp_path, _msg("http://x/g.zip"))
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert choose_main([str(mp), str(rp)]) == 0
    assert json.loads(rp.read_text()) == {"action": "normal"}


def test_attach_console_skips_when_tty(monkeypatch):
    class FakeTty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", FakeTty())

    def boom(*a, **k):
        raise AssertionError("must not open console when stdin is a tty")

    monkeypatch.setattr("builtins.open", boom)
    choose._attach_console()  # no exception


def test_attach_console_fallback_on_error(monkeypatch):
    class FakePipe:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", FakePipe())

    def boom(*a, **k):
        raise OSError

    monkeypatch.setattr("builtins.open", boom)
    choose._attach_console()  # no exception, stdio untouched
    assert type(choose.sys.stdin) is FakePipe


def test_fetch_size_heads_content_length(serve_zip):
    assert choose._fetch_size(serve_zip) != ""


def test_choice_rar_url_auto_normal(tmp_path, monkeypatch):
    """RAR/7z links can't stream — chooser writes "normal" without prompting."""
    mp, rp = _write_msg(tmp_path, _msg("http://x/game.part01.rar", filename="game.part01.rar"))
    monkeypatch.setattr("builtins.input", lambda *a: "1")  # must NOT be asked
    monkeypatch.setattr("anticompress.choose._wait_close", lambda: None)
    assert choose_main([str(mp), str(rp)]) == 0
    assert json.loads(rp.read_text()) == {"action": "normal"}


def test_choice_stream_downloads_to_dest(tmp_path, monkeypatch, serve_zip):
    mp, rp = _write_msg(tmp_path, _msg(serve_zip))
    dest = tmp_path / "out"
    answers = iter(["1", str(dest)])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("anticompress.choose._wait_close", lambda: None)
    assert choose_main([str(mp), str(rp)]) == 0
    assert json.loads(rp.read_text()) == {"action": "stream"}
    assert_trees_identical(FILES, dest)
