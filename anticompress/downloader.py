"""Fetch .acpkg chunks in parallel (bounded window), extract in order, delete as consumed."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx

from .extractor import _covered_ranges, extract_package
from .format import Manifest, chunk_expected_size, chunk_name, deserialize

Progress = Callable[[int], None] | None
WINDOW_FACTOR = 4  # chunks kept ahead of extraction: workers * WINDOW_FACTOR


def _fetch_chunk(client: httpx.Client, url: str, tmp_path: Path, final_path: Path, sha256: str, tries: int = 3) -> None:
    for attempt in range(tries):
        try:
            r = client.get(url)
            r.raise_for_status()
            data = r.content
            if hashlib.sha256(data).hexdigest() != sha256:
                raise ValueError("chunk hash mismatch")
            tmp_path.write_bytes(data)
            os.replace(tmp_path, final_path)  # atomic: extractor never sees partial chunks
            return
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1 + attempt)


def _chunks_needed(chunk_dir: Path, dest_dir: Path, m: Manifest) -> list:
    """Chunks to fetch: not already present+verified, and not fully covered
    by already-extracted+verified files (resume)."""
    covered = _covered_ranges(m, dest_dir)
    needed = []
    for ci in m.chunks:
        p = chunk_dir / chunk_name(ci.index)
        if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == ci.sha256:
            continue
        off = ci.index * m.chunk_size
        size = chunk_expected_size(m, ci.index)
        if any(c0 <= off and off + size <= c1 for c0, c1 in covered):
            continue
        needed.append(ci)
    return needed


def download_package(
    base_url: str,
    dest_dir: Path,
    chunk_dir: Path,
    workers: int = 8,
    progress: Progress = None,
) -> Manifest:
    """Fetch {base_url}/manifest.json, then download and extract INTERLEAVED:
    extraction consumes chunks in order as the fetchers fill a bounded window
    ahead — only ~workers*WINDOW_FACTOR chunks ever exist on disk, so peak
    space stays 1x even for 100 GB packages (fetch-all-then-extract would
    double it)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip("/") + "/"
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(connect=30, read=120, write=60, pool=30)) as client:
        r = client.get(urljoin(base, "manifest.json"))
        r.raise_for_status()
        m = deserialize(r.text)

        missing = {ci.index: ci for ci in _chunks_needed(chunk_dir, dest_dir, m)}
        window = max(workers * WINDOW_FACTOR, workers + 2)
        futures: dict[int, cf.Future] = {}
        submitted = 0
        lock = threading.Lock()

        def ensure(up_to: int) -> None:
            """Submit fetches for missing chunks below `up_to` (idempotent, race-safe)."""
            nonlocal submitted
            with lock:
                while submitted < min(up_to, len(m.chunks)):
                    ci = missing.get(submitted)
                    if ci is not None:
                        futures[submitted] = executor.submit(
                            _fetch_chunk,
                            client,
                            urljoin(base, chunk_name(ci.index)),
                            chunk_dir / (chunk_name(ci.index) + ".tmp"),
                            chunk_dir / chunk_name(ci.index),
                            ci.sha256,
                        )
                    submitted += 1

        def on_chunk(index: int) -> None:
            ensure(index + 1 + window)  # keep the window filled as chunks are consumed

        with cf.ThreadPoolExecutor(max_workers=workers) as executor:
            ensure(window)
            extract_package(
                chunk_dir, dest_dir, m,
                delete_chunks=True, progress=progress,
                waiters=futures, on_chunk=on_chunk,
            )
    return m
