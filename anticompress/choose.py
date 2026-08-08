"""Interactive terminal chooser — spawned by the native host in its own console."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _fmt_size(size: int) -> str:
    if not size:
        return "size unknown"
    return f"{size / 1e9:.2f} GB"


def _write_result(result_path: str, action: str) -> None:
    Path(result_path).write_text(json.dumps({"action": action}), encoding="utf-8")


def _wait_close() -> None:
    input("\nPress Enter to close this window.")


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    msg_path, result_path = argv[0], argv[1]
    msg = json.loads(Path(msg_path).read_text(encoding="utf-8"))
    url = msg.get("url", "")
    filename = msg.get("filename") or "download"
    size = _fmt_size(msg.get("size") or 0)

    print("=== AntiCompress ===")
    print(f"{filename}  ({size})")
    print()
    print("[1] Download with AntiCompress (stream)")
    print("[2] Normal download (Firefox saves it)")
    print()
    choice = input("Choice [1/2]: ").strip()

    if choice != "1":
        _write_result(result_path, "normal")
        return 0

    default_dest = str(Path.home() / "Downloads" / Path(filename).stem)
    dest = input(f"Destination folder [{default_dest}]: ").strip() or default_dest
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    _write_result(result_path, "stream")
    print()
    try:
        from .cli import _acpkg_or_stream

        _acpkg_or_stream(url, dest_path, workers=8)
    except SystemExit:
        pass  # _fail() already printed the error
    except Exception as e:  # noqa: BLE001 — show any failure in the console
        print(f"\nerror: {e}", file=sys.stderr)
    _wait_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
