import io
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import anticompress.native_host as nh


def _feed_stdin(monkeypatch, msg: dict) -> io.BytesIO:
    data = json.dumps(msg).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(struct.pack("<I", len(data)) + data)))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    return out


def _decode_out(out: io.BytesIO) -> dict:
    raw = out.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    return json.loads(raw[4 : 4 + length])


def _decode_all(out: io.BytesIO) -> list[dict]:
    raw = out.getvalue()
    msgs = []
    while raw:
        (length,) = struct.unpack("<I", raw[:4])
        msgs.append(json.loads(raw[4 : 4 + length]))
        raw = raw[4 + length :]
    return msgs


class _FakeProc:
    def __init__(self):
        self.waited = False

    def wait(self):
        self.waited = True
        return 0


def test_read_message_framing(monkeypatch):
    data = json.dumps({"type": "download", "url": "http://x/g.zip", "filename": "g.zip", "size": 5}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(struct.pack("<I", len(data)) + data)))
    assert nh.read_message() == {"type": "download", "url": "http://x/g.zip", "filename": "g.zip", "size": 5}


def test_read_message_empty_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    assert nh.read_message() is None


def test_send_message_framing(monkeypatch):
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    nh.send_message({"action": "normal"})
    raw = out.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    assert json.loads(raw[4 : 4 + length]) == {"action": "normal"}


def test_main_stream_choice(monkeypatch, tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"action": "stream"}))
    spawned = {}
    proc = _FakeProc()

    def fake_spawn(msg):
        spawned["msg"] = msg
        return proc, result

    monkeypatch.setattr(nh, "spawn_chooser", fake_spawn)
    out = _feed_stdin(monkeypatch, {"type": "download", "url": "http://x/g.zip", "filename": "g.zip", "size": 5})
    nh.main()
    assert _decode_all(out) == [{"action": "stream"}, {"action": "finished"}]
    assert spawned["msg"]["url"] == "http://x/g.zip"
    assert proc.waited  # host stays alive until the chooser exits


def test_main_waits_for_chooser_before_finished(monkeypatch, tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"action": "stream"}))
    events = []
    proc = _FakeProc()

    def fake_wait():
        events.append("wait")
        return 0

    proc.wait = fake_wait

    def fake_spawn(msg):
        events.append("spawn")
        return proc, result

    monkeypatch.setattr(nh, "spawn_chooser", fake_spawn)
    out = _feed_stdin(monkeypatch, {"type": "download", "url": "http://x/g.zip"})
    nh.main()
    assert events == ["spawn", "wait"]


def test_spawn_chooser_uses_new_console(monkeypatch):
    spawned = {}

    def fake_popen(args, **kw):
        spawned["args"] = args
        spawned["kw"] = kw
        return None

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    proc, result_path = nh.spawn_chooser({"type": "download", "url": "http://x/g.zip"})
    assert "anticompress.choose" in spawned["args"]
    assert spawned["kw"].get("creationflags") == nh.CREATE_NEW_CONSOLE
    assert result_path.name.endswith(".result")


def test_wait_for_choice_timeout_defaults_normal(tmp_path):
    assert nh.wait_for_choice(tmp_path / "nope.json", timeout=0.1) == "normal"


def test_wait_for_choice_reads_action(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"action": "stream"}))
    assert nh.wait_for_choice(p, timeout=1.0) == "stream"


def test_wait_for_choice_garbage_defaults_normal(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("not json")
    assert nh.wait_for_choice(p, timeout=1.0) == "normal"


def test_main_unknown_message_type_defaults_normal(monkeypatch):
    out = _feed_stdin(monkeypatch, {"type": "whatever"})
    nh.main()
    assert _decode_out(out) == {"action": "normal"}
