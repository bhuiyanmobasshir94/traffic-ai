#!/usr/bin/env python3
"""Fetch the demo toll-plaza footage into data/videos/.

Stdlib-only (urllib + hashlib) so it can run before `poetry install` — the
first thing a fresh checkout needs before `make up` builds anything. See
data/videos/ATTRIBUTION.md for source and licence provenance.

Usage:
    python scripts/fetch_demo_videos.py [--force] [--dest DIR]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = REPO_ROOT / "data" / "videos"

# Filenames must match src/traffic_ai/cameras.py CameraConfig.video_filename
# for toll-plaza-a and toll-plaza-b — that is what the worker looks for.


@dataclass(frozen=True)
class DemoVideo:
    filename: str
    url: str
    md5: str
    size: int


DEMO_VIDEOS: tuple[DemoVideo, ...] = (
    DemoVideo(
        filename="toll-plaza-a.mp4",
        url="https://media.roboflow.com/supervision/video-examples/vehicles.mp4",
        md5="8155ff4e4de08cfa25f39de96483f918",
        size=35_345_757,
    ),
    DemoVideo(
        filename="toll-plaza-b.mp4",
        url="https://media.roboflow.com/supervision/video-examples/vehicles-2.mp4",
        md5="830af6fba21ffbf14867a7fea595937b",
        size=29_782_444,
    ),
)

_CHUNK_SIZE = 1024 * 1024


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _md5_of(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 — integrity check against a known-good
    # value from a trusted source, not a security boundary.
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_valid(path: Path, video: DemoVideo) -> bool:
    if not path.is_file():
        return False
    if path.stat().st_size != video.size:
        return False
    return _md5_of(path) == video.md5


def _download(video: DemoVideo, dest: Path) -> None:
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    _log(f"[fetch] {video.filename}: downloading from {video.url}")
    try:
        with urllib.request.urlopen(video.url, timeout=30) as resp:  # noqa: S310 — fixed https URL, not user input
            total = int(resp.headers.get("Content-Length", video.size))
            written = 0
            with tmp_path.open("wb") as out:
                while chunk := resp.read(_CHUNK_SIZE):
                    out.write(chunk)
                    written += len(chunk)
                    pct = written / total * 100 if total else 0
                    _log(f"[fetch] {video.filename}: {written:,}/{total:,} bytes ({pct:.1f}%)")
    except (urllib.error.URLError, OSError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"{video.filename}: download failed: {exc}") from exc

    if not _is_valid(tmp_path, video):
        actual = _md5_of(tmp_path) if tmp_path.is_file() else "<missing>"
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"{video.filename}: checksum mismatch after download "
            f"(expected md5 {video.md5}, got {actual}) — file deleted"
        )

    tmp_path.replace(dest)
    retrieved_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    _log(f"[fetch] {video.filename}: OK, verified md5 {video.md5} (retrieved {retrieved_at})")


def fetch_all(dest_dir: Path, *, force: bool) -> int:
    """Fetch every demo video into dest_dir. Returns the process exit code."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for video in DEMO_VIDEOS:
        dest = dest_dir / video.filename
        if not force and _is_valid(dest, video):
            _log(f"[fetch] {video.filename}: already present and verified, skipping")
            continue
        try:
            _download(video, dest)
        except RuntimeError as exc:
            _log(f"[fetch] ERROR: {exc}")
            failures.append(video.filename)

    if failures:
        _log(f"[fetch] failed: {', '.join(failures)}")
        return 1
    _log("[fetch] all demo videos present and verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"destination directory (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if a valid file already exists",
    )
    args = parser.parse_args(argv)
    return fetch_all(args.dest, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
