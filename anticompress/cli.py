"""AntiCompress CLI: repack / dl / install."""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import httpx

from .downloader import download_package
from .extractor import extract_package
from .format import read_manifest, deserialize
from .repacker import repack
from .zipstream import stream_archive

BUFFER_BYTES = 64 * 1024 * 1024  # 64 MiB safety buffer


def _free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def _progress(prefix: str, total: int) -> callable:
    start = time.monotonic()
    last = 0

    def cb(done: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last < 0.2 and done < total:
            return
        last = now
        elapsed = max(now - start, 1e-6)
        speed = done / elapsed / 1e6
        if total:
            pct = done / total * 100
            line = f"\r{prefix} {pct:6.1f}%  {done / 1e9:6.2f}/{total / 1e9:6.2f} GB  {speed:6.1f} MB/s"
        else:
            line = f"\r{prefix} {done / 1e9:6.2f} GB  {speed:6.1f} MB/s"
        print(line, end="", flush=True)

    return cb


def _fail(msg: str) -> None:
    print(f"\nerror: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_repack(args: argparse.Namespace) -> None:
    archive = Path(args.archive)
    out = Path(args.out)
    if not archive.is_file():
        _fail(f"{archive} not found")
    free = _free_space(out if out.exists() else out.parent)
    src = archive.stat().st_size
    if free < src:
        _fail(f"need ~{src / 1e9:.1f} GB free for repack (have {free / 1e9:.1f} GB); "
              f"put the source archive on a drive with room")
    print(f"repacking {archive} → {out}/ (single pass, this can take a while)")
    t0 = time.monotonic()
    try:
        m = repack(archive, out, seven_zip=args.seven_zip, progress=_progress("repack", 0))
    except (RuntimeError, ValueError) as e:
        _fail(str(e))
    print(f"\ndone in {time.monotonic() - t0:.0f}s: {len(m.files)} files, "
          f"{m.total_size / 1e9:.1f} GB → {out}")


def _stream_plain(url: str, dest: Path) -> None:
    try:
        stream_archive(url, dest, progress=_progress("download", 0))
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: extracted to {dest}")


def _acpkg_or_stream(url: str, dest: Path, workers: int) -> None:
    """Try .acpkg (manifest present) first; fall back to plain zip/tar streaming."""
    base = url.rstrip("/") + "/"
    is_acpkg = url.rstrip("/").endswith(".acpkg")
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as c:
            r = c.get(base + "manifest.json")
            if r.status_code == 404 and not is_acpkg:
                _stream_plain(url, dest)
                return
            r.raise_for_status()
            m = deserialize(r.text)
    except httpx.HTTPError as e:
        if is_acpkg:
            _fail(f"cannot fetch .acpkg manifest: {e}")
        _stream_plain(url, dest)
        return
    except ValueError as e:
        if is_acpkg:
            _fail(f"invalid .acpkg: {e}")
        _stream_plain(url, dest)
        return
    free = _free_space(dest)
    need = m.total_size + BUFFER_BYTES
    if free < need:
        _fail(f"need {need / 1e9:.1f} GB free (have {free / 1e9:.1f} GB)")
    chunk_dir = dest / ".anticompress-chunks"
    try:
        download_package(base, dest, chunk_dir, workers=workers, progress=_progress("download", m.total_size))
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
    except Exception as e:
        _fail(str(e))
    print(f"\ndone: {len(m.files)} files extracted to {dest}")


def main(argv: list[str] | None = None) -> None:
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

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
