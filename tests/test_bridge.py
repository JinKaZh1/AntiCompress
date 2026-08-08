import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from anticompress.bridge import install_bridge, EXTENSION_ID, HOST_NAME


def test_install_bridge_writes_files(tmp_path):
    out = install_bridge(app_dir=tmp_path / "app", reg_add=False)
    wrapper = Path(out["wrapper"])
    manifest_path = Path(out["manifest"])
    ext_dst = Path(out["extension"])
    assert wrapper.is_file()
    assert sys.executable in wrapper.read_text(encoding="utf-8")
    assert manifest_path.is_file()
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert m["name"] == HOST_NAME
    assert m["type"] == "stdio"
    assert m["allowed_extensions"] == [EXTENSION_ID]
    assert m["path"] == str(wrapper)
    assert (ext_dst / "manifest.json").is_file()
    assert (ext_dst / "background.js").is_file()


def test_extension_manifest_valid_and_permissioned(tmp_path):
    ext = Path(__file__).resolve().parent.parent / "bridge" / "extension"
    m = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3
    assert "downloads" in m["permissions"]
    assert "nativeMessaging" in m["permissions"]
    assert "contextMenus" in m["permissions"]
    assert m["browser_specific_settings"]["gecko"]["id"] == EXTENSION_ID
    assert "background" in m and "scripts" in m["background"]


def test_background_js_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for JS syntax check")
    ext = Path(__file__).resolve().parent.parent / "bridge" / "extension"
    r = subprocess.run([node, "--check", str(ext / "background.js")], capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_registry_key_written_and_queryable(tmp_path):
    out = install_bridge(app_dir=tmp_path / "app", reg_add=True)
    r = subprocess.run(
        ["reg", "query", out["registry"], "/ve"],
        capture_output=True,
    )
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    assert str(Path(out["manifest"])) in r.stdout.decode(errors="replace")
