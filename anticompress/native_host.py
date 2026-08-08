"""Native messaging host: receives download info from the extension, spawns the chooser."""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CREATE_NEW_CONSOLE = 0x00000010
RESPONSE_TIMEOUT = 3600.0  # 1 hour to answer the prompt

_LOG_PATH = Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")) / "anticompress" / "host.log"


def _log(line: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass


def read_message() -> dict | None:
    raw = sys.stdin.buffer.read(4)
    if not raw:
        return None
    (length,) = struct.unpack("<I", raw)
    return json.loads(sys.stdin.buffer.read(length))


def send_message(msg: dict) -> None:
    data = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


def spawn_chooser(msg: dict) -> tuple[subprocess.Popen, Path]:
    fd, path = tempfile.mkstemp(prefix="anticompress-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(msg, f)
    result_path = Path(path + ".result")
    args = [sys.executable, "-m", "anticompress.choose", path, str(result_path)]
    if sys.platform == "win32":
        proc = subprocess.Popen(args, creationflags=CREATE_NEW_CONSOLE)
    else:
        proc = subprocess.Popen(args)
    return proc, result_path


def wait_for_choice(result_path: Path, timeout: float = RESPONSE_TIMEOUT) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                return json.loads(result_path.read_text(encoding="utf-8")).get("action", "normal")
            except Exception:
                return "normal"
        time.sleep(0.2)
    return "normal"


def _safe_send(msg: dict) -> None:
    try:
        send_message(msg)
        _log(f"sent: {msg}")
    except Exception as e:
        _log(f"send failed ({msg}): {e!r}")


def main() -> None:
    _log("host start")
    msg = read_message()
    if not msg or msg.get("type") != "download":
        _log(f"bad message: {msg!r}")
        _safe_send({"action": "normal"})
        return
    _log(f"received: {msg.get('url', '')[:80]}... filename={msg.get('filename')}")
    proc, result_path = spawn_chooser(msg)
    _log(f"chooser spawned, result: {result_path}")
    action = wait_for_choice(result_path)
    _log(f"choice: {action}")
    _safe_send({"action": action})
    # Stay alive until the chooser finishes. Firefox tears down the native
    # host's process tree when the host exits — which would kill a still-
    # running download. The "finished" message lets the extension close
    # the port cleanly afterwards.
    try:
        proc.wait()
        _log("chooser exited")
    except Exception as e:
        _log(f"chooser wait error: {e!r}")
    _safe_send({"action": "finished"})
    _log("host exit")


if __name__ == "__main__":
    main()
