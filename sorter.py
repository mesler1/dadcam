"""
sorter.py — FileSorter: copy verified media to destination and remove from source.

Destination layout:
    <dest>/detections/     — files where a detection was found
    <dest>/no_detections/  — files with no detection

Deduplication:
    SHA-256 of source is compared against any existing file at the destination
    with the same name.  If they match the file is already present; skip copy
    and remove from source.

Copy safety:
    1. shutil.copy2  (preserves mtime / metadata)
    2. SHA-256 of destination compared against source
    3. Only remove source after verification passes
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from config import PathsConfig
from detection import DetectionResult
from scanner import MediaFile

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB


# ---------------------------------------------------------------------------
# Action enum  (recorded per file for the report)
# ---------------------------------------------------------------------------


class SortAction(Enum):
    MOVED = auto()            # copied + verified + source removed
    SKIP_DUPLICATE = auto()   # already at destination, source removed
    COPY_ERROR = auto()       # copy or verify failed, source kept
    DETECTION_ERROR = auto()  # inference failed, source kept


@dataclass
class SortResult:
    media_file: MediaFile
    detection: DetectionResult
    action: SortAction
    dest_path: Path | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def _safe_dest_path(dest_dir: Path, relative: Path) -> Path:
    """
    Resolve the destination path and ensure it stays inside dest_dir.
    Raises ValueError on path-traversal attempts.
    """
    candidate = (dest_dir / relative).resolve()
    if not str(candidate).startswith(str(dest_dir.resolve())):
        raise ValueError(
            f"Path traversal detected: {relative!r} escapes {dest_dir}"
        )
    return candidate


# ---------------------------------------------------------------------------
# FileSorter
# ---------------------------------------------------------------------------


class FileSorter:
    def __init__(self, config: PathsConfig) -> None:
        self.dest_root = Path(config.destination).resolve()
        self.detections_dir = self.dest_root / "detections"
        self.no_detections_dir = self.dest_root / "no_detections"

        # Create output directories up front
        for d in (self.detections_dir, self.no_detections_dir):
            d.mkdir(parents=True, exist_ok=True)

    def sort(self, media_file: MediaFile, detection: DetectionResult) -> SortResult:
        """Process one file: copy to the appropriate folder then remove source."""

        # Inference errors — leave file on source
        if detection.error and detection.error == "detection_error":
            logger.warning("Skipping %s due to detection error", media_file.path.name)
            return SortResult(
                media_file=media_file,
                detection=detection,
                action=SortAction.DETECTION_ERROR,
            )

        # Choose subfolder
        subfolder = self.detections_dir if detection.detected else self.no_detections_dir

        try:
            dest_path = _safe_dest_path(subfolder, media_file.relative_path)
        except ValueError as exc:
            logger.error("Path traversal check failed: %s", exc)
            return SortResult(
                media_file=media_file,
                detection=detection,
                action=SortAction.COPY_ERROR,
                error=str(exc),
            )

        # ---- Deduplication check ----------------------------------------
        src_hash = _sha256(media_file.path)
        if dest_path.exists():
            dest_hash = _sha256(dest_path)
            if src_hash == dest_hash:
                logger.info(
                    "DUPLICATE — already at destination, removing source: %s",
                    media_file.path.name,
                )
                try:
                    media_file.path.unlink()
                except OSError as exc:
                    logger.error("Could not remove source %s: %s", media_file.path, exc)
                return SortResult(
                    media_file=media_file,
                    detection=detection,
                    action=SortAction.SKIP_DUPLICATE,
                    dest_path=dest_path,
                )
            else:
                # Same name, different content — rename destination to avoid collision
                dest_path = _unique_path(dest_path)
                logger.debug(
                    "Name collision with different content; using: %s",
                    dest_path.name,
                )

        # ---- Copy -------------------------------------------------------
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(media_file.path), str(dest_path))
        except OSError as exc:
            logger.error("Copy failed for %s: %s", media_file.path.name, exc)
            return SortResult(
                media_file=media_file,
                detection=detection,
                action=SortAction.COPY_ERROR,
                dest_path=dest_path,
                error=str(exc),
            )

        # ---- Verify copy -----------------------------------------------
        dest_hash = _sha256(dest_path)
        if dest_hash != src_hash:
            logger.error(
                "Copy verification FAILED for %s (src=%s, dst=%s)",
                media_file.path.name,
                src_hash[:12],
                dest_hash[:12],
            )
            try:
                dest_path.unlink(missing_ok=True)
            except OSError:
                pass
            return SortResult(
                media_file=media_file,
                detection=detection,
                action=SortAction.COPY_ERROR,
                dest_path=dest_path,
                error="hash_mismatch_after_copy",
            )

        # ---- Remove source ----------------------------------------------
        try:
            media_file.path.unlink()
        except OSError as exc:
            logger.error(
                "Copy verified but could not remove source %s: %s",
                media_file.path,
                exc,
            )
            # Still report as MOVED since the file is safely at the destination
        logger.info(
            "MOVED %s → %s",
            media_file.path.name,
            dest_path.relative_to(self.dest_root),
        )
        return SortResult(
            media_file=media_file,
            detection=detection,
            action=SortAction.MOVED,
            dest_path=dest_path,
        )


def _unique_path(path: Path) -> Path:
    """Return a path that does not yet exist by appending _1, _2, …"""
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
