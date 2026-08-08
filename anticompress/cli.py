"""AntiCompress CLI: repack / dl / install."""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import httpx

from .bridge import install_bridge
from .downloader import download_package
from .extractor import extract_package
from .format import read_manifest, deserialize
from .repacker import repack
from .zipstream import stream_archive

BUFFER_BYTES = 64 * 1024 * 1024  # 64 MiB safety buffer


def _free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


class _Progress:
    """Renders a live progress line: bar, percent, GB, speed.

    `set_total()` lets a downloader fill in the real size once headers
    arrive (Content-Length), upgrading the line from GB-only to a % bar.
    """

    def __init__(self, prefix: str, total: int = 0):
        self.prefix = prefix
        self.total = total
        self.start = None
        self.last = 0
        self.samples: list[tuple[float, int]] = []  # (time, bytes) for windowed speed

    def set_total(self, total: int) -> None:
        self.total = total

    def __call__(self, done: int) -> None:
        now = time.monotonic()
        if self.start is None:
            self.start = now  # measure from the first byte, not process start
        self.samples.append((now, done))
        cutoff = now - 3.0
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)
        if now - self.last < 0.2 and done < self.total:
            return
        self.last = now
        if len(self.samples) >= 2:
            t0, d0 = self.samples[0]
            speed = (done - d0) / max(now - t0, 1e-6) / 1e6  # last 3s, not lifetime avg
        else:
            speed = done / max(now - self.start, 1e-6) / 1e6
        done_gb = done / 1e9
        if self.total:
            pct = done / self.total * 100
            filled = int(pct / 100 * 20)
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            line = (
                f"\r{self.prefix} [{bar}] {pct:5.1f}%  "
                f"{done_gb:5.2f}/{self.total / 1e9:5.2f} GB  {speed:6.1f} MB/s"
            )
        else:
            line = f"\r{self.prefix} {done_gb:5.2f} GB  {speed:6.1f} MB/s"
        print(line, end="", flush=True)


def _progress(prefix: str, total: int) -> _Progress:
    return _Progress(prefix, total)


def _ensure_utf8() -> None:
    """UTF-8 stdout/stderr with replacement errors — box/block chars degrade
    to '?' on legacy code pages instead of crashing the whole tool."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _fail(msg: str) -> None:
    print(f"\nerror: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_7z(explicit: str) -> str:
    """Resolve 7z.exe: explicit path wins; else PATH; else standard install location."""
    if explicit != "7z":
        return explicit
    found = shutil.which("7z")
    if found:
        return found
    alt = Path(r"C:\Program Files\7-Zip\7z.exe")
    return str(alt) if alt.is_file() else explicit


def cmd_install_bridge(args: argparse.Namespace) -> None:
    try:
        out = install_bridge()
    except Exception as e:
        _fail(str(e))
    print(f"bridge installed:\n  extension: {out['extension']}\n  native host: {out['wrapper']}\n  registry: {out['registry']}")
    print("Next: Firefox -> about:debugging -> This Firefox -> Load Temporary Add-on ->")
    print(f"  {Path(out['extension']) / 'manifest.json'}")


def cmd_repack(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    out = Path(args.out)
    seven_zip = _resolve_7z(args.seven_zip)
    if not archive.is_file():
        _fail(f"{archive} not found")
    if not Path(seven_zip).is_file() and shutil.which(seven_zip) is None:
        _fail(f"7z not found (install from 7-zip.org or pass --7z PATH)")
    free = _free_space(out if out.exists() else out.parent)
    src = archive.stat().st_size
    if free < src:
        _fail(f"need ~{src / 1e9:.1f} GB free for repack (have {free / 1e9:.1f} GB); "
              f"put the source archive on a drive with room")
    print(f"repacking {archive} -> {out}/ (single pass, this can take a while)")
    t0 = time.monotonic()
    try:
        m = repack(archive, out, seven_zip=seven_zip, progress=_progress("repack", 0))
    except (RuntimeError, ValueError) as e:
        _fail(str(e))
    print(f"\ndone in {time.monotonic() - t0:.0f}s: {len(m.files)} files, "
          f"{m.total_size / 1e9:.1f} GB -> {out}")


def _stream_plain(url: str, dest: Path) -> None:
    try:
        stream_archive(url, dest, progress=_progress("download", 0))
    except KeyboardInterrupt:
        print("\npaused — click the download link again (same folder) to resume")
        return
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: extracted to {dest}")


def _probe_manifest(client: httpx.Client, base: str):
    """Peek at {base}manifest.json; return the parsed manifest if it looks
    like one, else None. Never buffers large non-JSON responses — some
    servers answer every path with the actual file (dlproxy-style)."""
    try:
        with client.stream("GET", base + "manifest.json") as r:
            if r.status_code == 404:
                return None
            r.raise_for_status()
            head = b""
            for block in r.iter_bytes(8192):
                head += block
                if len(head) >= 4096:
                    break
        if not head.lstrip().startswith(b"{"):
            return None  # binary file / HTML — not a manifest
        r2 = client.get(base + "manifest.json")
        r2.raise_for_status()
        return deserialize(r2.text)
    except (httpx.HTTPError, ValueError):
        return None


def _acpkg_or_stream(url: str, dest: Path, workers: int) -> None:
    """Try .acpkg (manifest present) first; fall back to plain zip/tar streaming."""
    base = url.rstrip("/") + "/"
    is_acpkg = url.rstrip("/").endswith(".acpkg")
    with httpx.Client(
        follow_redirects=True, timeout=httpx.Timeout(connect=30, read=30, write=30, pool=30)
    ) as client:
        m = _probe_manifest(client, base)
        if m is None:
            if is_acpkg:
                _fail("not a valid .acpkg (no manifest.json found at the URL)")
            _stream_plain(url, dest)
            return
        free = _free_space(dest)
        need = m.total_size + BUFFER_BYTES
        if free < need:
            _fail(f"need {need / 1e9:.1f} GB free (have {free / 1e9:.1f} GB)")
        chunk_dir = dest / ".anticompress-chunks"
        try:
            download_package(
                base, dest, chunk_dir, workers=workers, progress=_progress("download", m.total_size),
                resume_notice=lambda done, total: print(f"\nResuming: {done}/{total} chunks already on disk"),
            )
        except KeyboardInterrupt:
            print("\npaused — click the download link again (same folder) to resume")
            return
        except Exception as e:
            _fail(str(e))
        print(f"\ndone: {len(m.files)} files extracted to {dest}")


def cmd_dl(args: argparse.Namespace) -> None:
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    _acpkg_or_stream(args.url, dest, args.workers)


def cmd_install(args: argparse.Namespace) -> None:
    pkg = Path(args.package)
    dest = Path(args.dest)
    manifest_path = pkg / "manifest.json"
    if not manifest_path.is_file():
        _fail(f"{pkg} is not a .acpkg folder (no manifest.json)")
    try:
        m = read_manifest(manifest_path)
    except ValueError as e:
        _fail(str(e))
    dest.mkdir(parents=True, exist_ok=True)
    free = _free_space(dest)
    need = m.total_size + BUFFER_BYTES
    if free < need:
        _fail(f"need {need / 1e9:.1f} GB free (have {free / 1e9:.1f} GB)")
    try:
        extract_package(pkg, dest, m, delete_chunks=True, progress=_progress("install", m.total_size))
    except KeyboardInterrupt:
        print("\npaused — re-run install with the same folder to resume")
        return
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: {len(m.files)} files extracted to {dest}")


def main(argv: list[str] | None = None) -> None:
    _ensure_utf8()
    ap = argparse.ArgumentParser(prog="anticompress", description="Steam-style streaming download + decompress")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("repack", help="convert RAR/7z/zip into a .acpkg chunk folder")
    p.add_argument("archive")
    p.add_argument("-o", "--out", required=True, help="output .acpkg folder")
    p.add_argument("--7z", dest="seven_zip", default="7z", help="path to 7z.exe")
    p.set_defaults(fn=cmd_repack)

    p = sub.add_parser("dl", help="stream-download + extract (.acpkg folder, zip, tar.gz…); 1x disk space")
    p.add_argument("url")
    p.add_argument("-o", "--dest", required=True)
    p.add_argument("--workers", type=int, default=8, help="parallel chunk connections")
    p.set_defaults(fn=cmd_dl)

    p = sub.add_parser("install", help="extract a local .acpkg folder (chunks deleted as consumed)")
    p.add_argument("package", help="path to the .acpkg folder")
    p.add_argument("-o", "--dest", required=True)
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("install-bridge", help="register the Firefox bridge (extension + native host)")
    p.set_defaults(fn=cmd_install_bridge)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
