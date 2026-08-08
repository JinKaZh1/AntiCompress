# AntiCompress Firefox Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Firefox extension + native messaging host that intercepts downloads before Firefox saves them and shows a terminal chooser: **[1] Download with AntiCompress (stream)** or **[2] Normal download**.

**Architecture:** The extension cancels the browser download on `downloads.onCreated` (few KB max; Firefox offers no blocking cancel), sends `{url, filename, size}` to the native host over stdin/stdout JSON framing (4-byte native-endian length prefix). The host spawns an interactive chooser in a **new visible console** (`CREATE_NEW_CONSOLE` — hosts otherwise run hidden). The chooser writes `{"action": "stream"|"normal"}` to a result file; the host relays it to the extension. "Normal" → extension restarts the download via `browser.downloads.download()`, guarded by a pending-restart URL set so the restart never re-triggers the chooser. "Stream" → the chooser process runs the existing `_acpkg_or_stream` pipeline inline (its console becomes the progress bar). `anticompress install-bridge` registers everything (HKCU registry, no admin).

**Tech Stack:** Firefox WebExtensions MV3 (downloads + nativeMessaging permissions), Python stdlib only (struct/json/subprocess), Windows-first (`CREATE_NEW_CONSOLE`; non-Windows falls back to a normal Popen).

## Global Constraints

- Extension ID is exactly `anticompress@anticompress.local` (must match `allowed_extensions` in the native manifest).
- Native host framing: 4-byte little-endian length prefix + UTF-8 JSON on stdin/stdout. `sys.stdout.buffer` only — never `print` to stdout in the host.
- Host path in the native manifest is a single executable — a `.cmd` wrapper invoking `sys.executable -m anticompress.native_host`.
- HKCU registry key `HKCU\Software\Mozilla\NativeMessagingHosts\anticompress` (default value = native manifest path). No admin.
- `blob:`/`data:` URLs pass through to Firefox's normal download (no chooser — they can't be streamed; spec deviation, same outcome as "Normal").
- Interactive output in the chooser must be cp1252-safe (ASCII only, no box-drawing characters).
- Corrupt/unparseable anything → default to `{"action": "normal"}` (never silently drop the download).
- Every task: write failing test → see it fail → implement → see it pass → commit.

---

### Task 1: Native messaging host

**Files:**
- Create: `anticompress/native_host.py`
- Test: `tests/test_native_host.py`

**Interfaces:**
- Produces: `read_message() -> dict | None`, `send_message(msg: dict) -> None`, `spawn_chooser(msg: dict) -> Path` (returns the result-file path), `wait_for_choice(result_path: Path, timeout: float = 3600.0) -> str`, `main() -> None`. `CREATE_NEW_CONSOLE = 0x00000010`.

- [ ] **Step 1: Write the failing tests**

`tests/test_native_host.py`:
```python
import io
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

import anticompress.native_host as nh


def _feed_stdin(monkeypatch, msg: dict) -> None:
    data = json.dumps(msg).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(struct.pack("<I", len(data)) + data)))
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    return out


def _decode_out(out: io.BytesIO) -> dict:
    raw = out.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    return json.loads(raw[4 : 4 + length])


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

    def fake_spawn(msg):
        spawned["msg"] = msg
        return result

    monkeypatch.setattr(nh, "spawn_chooser", fake_spawn)
    out = _feed_stdin(monkeypatch, {"type": "download", "url": "http://x/g.zip", "filename": "g.zip", "size": 5})
    nh.main()
    assert _decode_out(out) == {"action": "stream"}
    assert spawned["msg"]["url"] == "http://x/g.zip"


def test_spawn_chooser_uses_new_console(monkeypatch):
    spawned = {}

    def fake_popen(args, **kw):
        spawned["args"] = args
        spawned["kw"] = kw
        return None

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    result_path = nh.spawn_chooser({"type": "download", "url": "http://x/g.zip"})
    assert "-m" in spawned["args"] and "anticompress.choose" in spawned["args"]
    assert spawned["kw"].get("creationflags") == nh.CREATE_NEW_CONSOLE
    assert result_path.name.endswith(".result")


def test_main_unknown_message_type_defaults_normal(monkeypatch):
    out = _feed_stdin(monkeypatch, {"type": "whatever"})
    nh.main()
    assert _decode_out(out) == {"action": "normal"}
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_native_host.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.native_host'`

- [ ] **Step 3: Write the host**

`anticompress/native_host.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_native_host.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/native_host.py tests/test_native_host.py
git commit -m "feat: native messaging host — framed JSON, chooser spawn, normal-default fallbacks"
```

---

### Task 2: Terminal chooser

**Files:**
- Create: `anticompress/choose.py`
- Test: `tests/test_choose.py`

**Interfaces:**
- Consumes: `_acpkg_or_stream(url, dest, workers)` from `anticompress.cli` (Task 7 of the CLI-core plan).
- Produces: `main() -> int` — run as `python -m anticompress.choose <msgfile> <resultfile>`; writes `{"action": "stream"|"normal"}` to the result file.

- [ ] **Step 1: Write the failing tests**

`tests/test_choose.py`:
```python
import functools
import io
import json
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anticompress.choose import main as choose_main
from tests.helpers import assert_trees_identical

FILES = {"game.exe": b"MZ" + bytes(range(100)), "data.bin": bytes(range(3000))}


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


def test_choice_stream_downloads_to_dest(tmp_path, monkeypatch, serve_zip):
    mp, rp = _write_msg(tmp_path, _msg(serve_zip))
    dest = tmp_path / "out"
    answers = iter(["1", str(dest)])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    # suppress the trailing "press enter" wait
    monkeypatch.setattr("anticompress.choose._wait_close", lambda: None)
    assert choose_main([str(mp), str(rp)]) == 0
    assert json.loads(rp.read_text()) == {"action": "stream"}
    assert_trees_identical(FILES, dest)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_choose.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.choose'`

- [ ] **Step 3: Write the chooser**

`anticompress/choose.py`:
```python
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


def main() -> int:
    msg_path, result_path = sys.argv[1], sys.argv[2]
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_choose.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add anticompress/choose.py tests/test_choose.py
git commit -m "feat: terminal chooser — stream/normal choice, inline download on stream"
```

---

### Task 3: Bridge installer + CLI wiring

**Files:**
- Create: `anticompress/bridge.py`
- Modify: `anticompress/cli.py` (add `install-bridge` subcommand)
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: `install_bridge(app_dir: Path | None = None, reg_add: bool = True) -> dict` — returns `{"wrapper", "manifest", "extension", "registry"}` paths. `EXTENSION_ID = "anticompress@anticompress.local"`, `HOST_NAME = "anticompress"`.
- Consumes: nothing from earlier tasks (static files only).

- [ ] **Step 1: Write the failing tests**

`tests/test_bridge.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anticompress.bridge'`

- [ ] **Step 3: Write the installer and wire the CLI**

`anticompress/bridge.py`:
```python
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
```

Modify `anticompress/cli.py` — add the import and subcommand:

```python
from .bridge import install_bridge
```

```python
    p = sub.add_parser("install-bridge", help="register the Firefox bridge (extension + native host)")
    p.set_defaults(fn=cmd_install_bridge)
```

```python
def cmd_install_bridge(args: argparse.Namespace) -> None:
    try:
        out = install_bridge()
    except Exception as e:
        _fail(str(e))
    print(f"bridge installed:\n  extension: {out['extension']}\n  native host: {out['wrapper']}\n  registry: {out['registry']}")
    print("Next: Firefox -> about:debugging -> This Firefox -> Load Temporary Add-on ->")
    print(f"  {Path(out['extension']) / 'manifest.json'}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: all pass (including the CLI suite)

- [ ] **Step 5: Commit**

```bash
git add anticompress/bridge.py anticompress/cli.py tests/test_bridge.py
git commit -m "feat: bridge installer — native manifest, cmd wrapper, HKCU registry, CLI wiring"
```

---

### Task 4: Firefox extension

**Files:**
- Create: `bridge/extension/manifest.json`
- Create: `bridge/extension/background.js`
- Test: covered by `tests/test_bridge.py::test_extension_manifest_valid_and_permissioned` and `test_background_js_syntax` (Task 3).

**Interfaces:**
- Produces: the static extension loaded by `install_bridge`; communicates with the native host `anticompress` per Task 1 framing.

- [ ] **Step 1: Write the extension files**

`bridge/extension/manifest.json`:
```json
{
  "manifest_version": 3,
  "name": "AntiCompress",
  "version": "0.1.0",
  "description": "Stream-download and auto-decompress with AntiCompress",
  "browser_specific_settings": {
    "gecko": {
      "id": "anticompress@anticompress.local"
    }
  },
  "permissions": ["downloads", "nativeMessaging", "contextMenus"],
  "background": {
    "scripts": ["background.js"]
  }
}
```

`bridge/extension/background.js`:
```javascript
// AntiCompress: intercept downloads before Firefox saves them, ask via the
// native host's terminal chooser: stream (AntiCompress) or normal download.
const RESTART_URLS = new Set();
const HOST = "anticompress";

function ask(url, filename, size) {
  const port = browser.runtime.connectNative(HOST);
  port.onMessage.addListener((msg) => {
    if (msg && msg.action === "normal") {
      RESTART_URLS.add(url);
      browser.downloads.download({ url }).catch(() => {});
    }
    port.disconnect();
  });
  port.onDisconnect.addListener(() => {
    if (port.error) {
      console.error("AntiCompress native host error:", port.error.message);
    }
  });
  port.postMessage({ type: "download", url, filename, size });
}

browser.downloads.onCreated.addListener(async (item) => {
  if (RESTART_URLS.has(item.url)) {
    // Our own "Normal download" restart — let Firefox save it untouched.
    RESTART_URLS.delete(item.url);
    return;
  }
  if (item.url.startsWith("blob:") || item.url.startsWith("data:")) {
    // In-page generated downloads can't be streamed — let Firefox handle them.
    return;
  }
  try {
    await browser.downloads.cancel(item.id);
  } catch (e) {
    // Download already finished (tiny file) — the chooser still gets offered.
  }
  ask(item.url, item.filename || "", item.fileSize || 0);
});

// Secondary flow: right-click a link -> Download with AntiCompress.
browser.contextMenus.create({
  id: "anticompress-dl",
  title: "Download with AntiCompress",
  contexts: ["link"],
});

browser.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === "anticompress-dl" && info.linkUrl) {
    const name = info.linkUrl.split("/").pop() || "download";
    ask(info.linkUrl, decodeURIComponent(name), 0);
  }
});
```

- [ ] **Step 2: Run the bridge tests to verify the extension is valid**

Run: `python -m pytest tests/test_bridge.py -v`
Expected: manifest/permission/ID assertions pass; JS syntax check passes if `node` is installed (skips otherwise)

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add bridge/extension/
git commit -m "feat: Firefox extension — download intercept, restart-loop guard, native messaging"
```

---

### Task 5: README + manual end-to-end verification

**Files:**
- Modify: `README.md` (Firefox bridge section)

**Interfaces:**
- Consumes: the complete bridge from Tasks 1–4.

- [ ] **Step 1: Update the README**

Append to `README.md`:

```markdown
## Firefox bridge (optional)

Click a download link and get a terminal chooser instead of a browser download:
stream it with AntiCompress (1x disk space) or download normally.

```
# one-time install (writes HKCU registry key, no admin needed)
anticompress install-bridge

# then load the extension in Firefox:
#   about:debugging -> This Firefox -> Load Temporary Add-on ->
#   %APPDATA%\anticompress\extension\manifest.json
```

The extension cancels the browser download the instant it starts (a few KB
max) and hands the URL to the CLI. Solid RAR/7z links still work — pick
"Normal download" for those, then `anticompress repack` them (see above).
```

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all pass

- [ ] **Step 3: Install the bridge and verify registration**

Run:
```bash
anticompress install-bridge
reg query HKCU\Software\Mozilla\NativeMessagingHosts\anticompress /ve
```
Expected: `install-bridge` prints the three paths; `reg query` shows the native manifest path.

- [ ] **Step 4: Manual browser verification (requires Firefox, user present)**

1. Load the temporary add-on from `%APPDATA%\anticompress\extension\manifest.json`
2. Serve a zip locally (`python -m http.server`), click its link in Firefox
3. Expected: a console window appears with `=== AntiCompress ===`, filename + size, `[1] stream` / `[2] normal`
4. Pick [1] → the zip streams into the chosen folder, bit-identical
5. Pick [2] on another link → Firefox saves it normally, no chooser loop

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: Firefox bridge install + usage"
```

## Deferred (tracked, not dropped)

- **Multi-part capture** (spec §2.3): when a `.part01.rar`-style URL is
  intercepted, offer to capture the whole part sequence and queue `repack`
  after the last part lands. Requires routing subsequent part downloads
  through the chooser with a "repack queue" mode — a real chunk of work;
  planned as a follow-up plan once the core bridge is verified in Firefox.
