"""Interactive terminal chooser — spawned by the native host in its own console."""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

_LOG_PATH = Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")) / "anticompress" / "chooser.log"


def _log(line: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass  # logging must never break the chooser


def _fmt_size(size: int) -> str:
    if not size or size < 0:
        return "size unknown"
    return f"{size / 1e9:.2f} GB"


def _write_result(result_path: str, action: str) -> None:
    Path(result_path).write_text(json.dumps({"action": action}), encoding="utf-8")


def _attach_console() -> None:
    """Reattach stdio to the real console.

    When spawned via CREATE_NEW_CONSOLE from a pipe-based parent (Firefox
    native messaging), stdin can remain the dead parent pipe: input() then
    EOFs instantly and the window dies. Opening the console devices fixes it.
    Skipped when stdin is already a tty (normal terminal, test harness).
    """
    if sys.platform != "win32" or sys.stdin.isatty():
        return
    try:
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        return  # no console attached (test harness) — keep inherited stdio
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except OSError:
        pass
    try:
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except OSError:
        pass


def _wait_close() -> None:
    try:
        input("\nPress Enter to close this window.")
    except EOFError:
        pass  # stdin closed (piped) — nothing to wait for


def _looks_like_rar7z(url: str) -> bool:
    low = url.lower().split("?")[0]
    return low.endswith((".rar", ".7z"))


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    msg_path, result_path = argv[0], argv[1]
    _attach_console()
    _log(f"chooser start: {msg_path}")
    try:
        msg = json.loads(Path(msg_path).read_text(encoding="utf-8"))
        url = msg.get("url", "")
        filename = Path(msg.get("filename") or "download").name
        size = _fmt_size(msg.get("size") or 0)
        _log(f"url={url} filename={filename}")

        print("=== AntiCompress ===")
        print(f"{filename}  ({size})")
        print()

        if _looks_like_rar7z(url):
            _log("rar/7z detected -> normal")
            print("This is a RAR/7z archive - AntiCompress cannot stream those.")
            print("Downloading it normally in Firefox; afterwards run:")
            print("  anticompress repack <archive> -o game.acpkg")
            _write_result(result_path, "normal")
            _wait_close()
            return 0

        print("[1] Download with AntiCompress (stream)")
        print("[2] Normal download (Firefox saves it)")
        print()
        choice = input("Choice [1/2]: ").strip()

        if choice != "1":
            _log("choice: normal")
            _write_result(result_path, "normal")
            return 0

        default_dest = str(Path.home() / "Downloads" / Path(filename).stem)
        dest = input(f"Destination folder [{default_dest}]: ").strip() or default_dest
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        _write_result(result_path, "stream")
        _log(f"choice: stream dest={dest_path}")
        print()
        print(f"Starting download to {dest_path} ...")
        print()
        try:
            from .cli import _acpkg_or_stream

            _acpkg_or_stream(url, dest_path, workers=8)
            _log("download finished")
        except SystemExit:
            _log("download failed (SystemExit from _fail)")
            pass  # _fail() already printed the error
        except Exception as e:  # noqa: BLE001 — show any failure in the console
            _log(f"download crashed: {e!r}")
            traceback.print_exc()
            print(f"\nerror: {e}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — never close without a message
        _log("chooser crashed: " + traceback.format_exc().replace(chr(10), " | "))
        traceback.print_exc()
        _write_result(result_path, "normal")
    finally:
        _log("chooser exiting")
        _wait_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
