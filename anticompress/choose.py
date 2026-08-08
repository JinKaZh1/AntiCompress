"""Interactive terminal chooser — spawned by the native host in its own console."""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

import httpx

_LOG_PATH = Path(os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")) / "anticompress" / "chooser.log"


def _log(line: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass  # logging must never break the chooser


def _fetch_size(url: str) -> str:
    """Best-effort HEAD to learn the real size (Firefox often reports -1)."""
    try:
        with httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(connect=4, read=4, write=4, pool=4)
        ) as client:
            r = client.head(url)
            if r.status_code == 200:
                total = int(r.headers.get("content-length") or 0)
                if total > 0:
                    return f"{total / 1e9:.2f} GB"
    except Exception:
        pass
    return ""


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


def _box(lines: list[str]) -> str:
    """Render lines inside a double-line box (UTF-8; degrades safely)."""
    width = max(len(l) for l in lines) + 4
    out = ["\u2554" + "\u2550" * (width - 2) + "\u2557"]
    for l in lines:
        out.append("\u2551 " + l.ljust(width - 4) + " \u2551")
    out.append("\u255a" + "\u2550" * (width - 2) + "\u255d")
    return "\n".join(out)


def _redraw(lines: list[str]) -> None:
    """Replace the box already on screen with a new one (ANSI up + clear)."""
    try:
        box = _box(lines)
        printed = box.count("\n") + 2  # box lines + the trailing blank
        sys.stdout.write(f"\x1b[{printed}A\x1b[J")
        print(box)
        print()
        sys.stdout.flush()
    except Exception:
        pass  # no VT: old box stays, acceptable


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
        box_lines = [
            "AntiCompress",
            "",
            f"{filename}  ({size})",
            "",
            "[1] Download with AntiCompress (stream)",
            "[2] Normal download (Firefox saves it)",
        ]
        if "unknown" in size:
            # show the box instantly, then fill the size in when HEAD answers
            print(_box(box_lines))
            print()
            fetched = _fetch_size(url)
            if fetched:
                size = fetched
                box_lines[2] = f"{filename}  ({size})"
                _redraw(box_lines)
        else:
            print(_box(box_lines))
            print()
        _log(f"url={url} filename={filename} size={size}")

        if _looks_like_rar7z(url):
            _log("rar/7z detected -> normal")
            print("This is a RAR/7z archive - AntiCompress cannot stream those.")
            print("Downloading it normally in Firefox; afterwards run:")
            print("  anticompress repack <archive> -o game.acpkg")
            _write_result(result_path, "normal")
            _wait_close()
            return 0

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
