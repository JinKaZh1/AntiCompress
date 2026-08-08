"""Install the Firefox bridge: native manifest, cmd wrapper, registry key, extension copy."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

EXTENSION_ID = "anticompress@anticompress.local"
HOST_NAME = "anticompress"
REGISTRY_KEY = r"HKCU\Software\Mozilla\NativeMessagingHosts\anticompress"


def _app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "anticompress"


def install_bridge(app_dir: Path | None = None, reg_add: bool = True) -> dict:
    app = app_dir or _app_dir()
    app.mkdir(parents=True, exist_ok=True)

    # 1. cmd wrapper — the native manifest's "path" must be a single executable
    wrapper = app / "anticompress-bridge.cmd"
    wrapper.write_text(f'@"{sys.executable}" -m anticompress.native_host %*\r\n', encoding="utf-8")

    # 2. native manifest
    manifest = {
        "name": HOST_NAME,
        "description": "AntiCompress native messaging host",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }
    manifest_path = app / "native-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # 3. copy the extension
    ext_src = Path(__file__).resolve().parent.parent / "bridge" / "extension"
    ext_dst = app / "extension"
    if ext_dst.exists():
        shutil.rmtree(ext_dst)
    shutil.copytree(ext_src, ext_dst)

    # 4. HKCU registry (no admin needed)
    if reg_add:
        subprocess.run(
            ["reg", "add", REGISTRY_KEY, "/ve", "/d", str(manifest_path), "/f"],
            check=True, capture_output=True,
        )

    return {
        "wrapper": str(wrapper),
        "manifest": str(manifest_path),
        "extension": str(ext_dst),
        "registry": REGISTRY_KEY,
    }
