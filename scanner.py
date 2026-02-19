"""
scanner.py — MediaScanner: walk a source directory and enumerate media files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported file types
# ---------------------------------------------------------------------------


class MediaType(Enum):
    IMAGE = auto()
    VIDEO = auto()


IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".avi", ".mts", ".m4v", ".mkv"}
)


def media_type_for(path: Path) -> MediaType | None:
    """Return the MediaType for *path*, or None if unsupported."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return MediaType.IMAGE
    if ext in VIDEO_EXTENSIONS:
        return MediaType.VIDEO
    return None


# ---------------------------------------------------------------------------
# MediaFile dataclass
# ---------------------------------------------------------------------------


@dataclass
class MediaFile:
    path: Path              # absolute path on the source filesystem
    media_type: MediaType
    size_bytes: int
    relative_path: Path     # path relative to the scan root


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class MediaScanner:
    """Recursively walks *root* and returns a list of MediaFile objects."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def scan(self) -> list[MediaFile]:
        if not self.root.exists():
            raise FileNotFoundError(f"Source directory not found: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Source path is not a directory: {self.root}")

        results: list[MediaFile] = []
        skipped = 0

        for item in sorted(self.root.rglob("*")):
            if not item.is_file():
                continue

            mtype = media_type_for(item)
            if mtype is None:
                logger.debug("Skipping unsupported file: %s", item.name)
                skipped += 1
                continue

            try:
                size = item.stat().st_size
            except OSError as exc:
                logger.warning("Cannot stat %s: %s", item, exc)
                continue

            results.append(
                MediaFile(
                    path=item,
                    media_type=mtype,
                    size_bytes=size,
                    relative_path=item.relative_to(self.root),
                )
            )
            logger.debug("Found %s: %s (%d bytes)", mtype.name, item.name, size)

        logger.info(
            "Scan complete: %d media files found, %d files skipped",
            len(results),
            skipped,
        )
        return results
