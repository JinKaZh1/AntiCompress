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


def spawn_chooser(msg: dict) -> Path:
    fd, path = tempfile.mkstemp(prefix="anticompress-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(msg, f)
    result_path = Path(path + ".result")
    args = [sys.executable, "-m", "anticompress.choose", path, str(result_path)]
    if sys.platform == "win32":
        subprocess.Popen(args, creationflags=CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(args)
    return result_path


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


def main() -> None:
    msg = read_message()
    if not msg or msg.get("type") != "download":
        send_message({"action": "normal"})
        return
    result_path = spawn_chooser(msg)
    send_message({"action": wait_for_choice(result_path)})


if __name__ == "__main__":
    main()
