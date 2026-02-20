#!/usr/bin/env python3
"""
dadcam.py — main entry point.

Usage:
    sudo python dadcam.py --setup
        Interactive wizard: whitelist a CF drive, install udev rule & systemd service.

    python dadcam.py --process --device /dev/sda1
        Process the media on the given block device.
        Usually invoked automatically by the systemd user service on CF insertion.

    python dadcam.py --process --device /dev/sda1 --dry-run
        Same as above but only logs what would happen; no files are copied or removed.

    python dadcam.py --report [--last N]
        Print the paths of the last N reports (default 5).

    python dadcam.py --list-whitelist
        Show all whitelisted drives.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: ensure the script's own directory is on sys.path so sibling
# modules (config, scanner, …) are importable regardless of cwd.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DadcamConfig, ensure_user_config_exists, load_config
from detection import DetectionEngine
from reporter import ReportWriter
from scanner import MediaScanner
from sorter import FileSorter, SortAction
import whitelist as wl

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    _RICH = True
except ImportError:
    _RICH = False

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(cfg: DadcamConfig) -> None:
    log_path = Path(cfg.logging.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.logging.level, logging.INFO)
    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        print(f"Warning: cannot open log file {log_path}: {exc}", file=sys.stderr)

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


logger = logging.getLogger("dadcam")

# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------


def _blkid_value(device: str, tag: str) -> str | None:
    try:
        result = subprocess.run(
            ["blkid", "-s", tag, "-o", "value", device],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None
    except FileNotFoundError:
        return None


def _get_device_info(device: str) -> tuple[str | None, str | None]:
    """Return (uuid, serial) for the block device."""
    uuid = _blkid_value(device, "UUID")

    # Try pyudev for serial
    serial: str | None = None
    try:
        import pyudev  # type: ignore[import-untyped]
        context = pyudev.Context()
        udev_device = pyudev.Devices.from_device_file(context, device)
        for attr in ("ID_SERIAL", "ID_SERIAL_SHORT", "ID_MODEL_ID"):
            val = udev_device.get(attr)
            if val:
                serial = val.strip()
                break
    except Exception:
        pass

    return uuid, serial


def _mount_device(device: str) -> str | None:
    """Mount device using udisksctl. Returns the mount path or None on failure."""
    try:
        result = subprocess.run(
            ["udisksctl", "mount", "-b", device, "--no-user-interaction"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            # udisksctl output: "Mounted /dev/sda1 at /run/media/deck/LABEL."
            for part in result.stdout.split():
                if part.startswith("/") and part != device.rstrip("."):
                    return part.rstrip(".")
            logger.warning("udisksctl mounted but could not parse mount path: %s", result.stdout)
        else:
            logger.error("udisksctl mount failed: %s", result.stderr.strip())
    except FileNotFoundError:
        logger.error("udisksctl not found — cannot mount device automatically")
    return None


def _unmount_device(device: str) -> None:
    try:
        subprocess.run(
            ["udisksctl", "unmount", "-b", device, "--no-user-interaction"],
            check=False,
            capture_output=True,
        )
        logger.info("Unmounted %s", device)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Process pipeline
# ---------------------------------------------------------------------------


def run_process(device: str, cfg: DadcamConfig, dry_run: bool = False) -> int:
    """Full processing pipeline. Returns exit code (0 = success)."""
    logger.info("=== dadcam process started — device: %s ===", device)
    if dry_run:
        logger.info("DRY-RUN mode: no files will be copied or removed")
    run_start = datetime.now()

    # ── 1. Whitelist check ───────────────────────────────────────────────
    uuid, serial = _get_device_info(device)
    logger.info("Device UUID=%s SERIAL=%s", uuid, serial)

    if not wl.is_whitelisted(uuid, serial):
        logger.warning(
            "Device UUID=%s SERIAL=%s is not whitelisted. Aborting.",
            uuid, serial
        )
        return 1

    # ── 2. Mount ─────────────────────────────────────────────────────────
    mount_path_str = _mount_device(device)
    if not mount_path_str:
        logger.error("Could not mount %s", device)
        return 1
    mount_path = Path(mount_path_str)
    logger.info("Mounted at: %s", mount_path)

    try:
        return _process_mounted(device, mount_path, cfg, run_start, dry_run=dry_run)
    finally:
        _unmount_device(device)


def _process_mounted(
    device: str,
    mount_path: Path,
    cfg: DadcamConfig,
    run_start: datetime,
    dry_run: bool = False,
) -> int:
    dest_path = Path(cfg.paths.destination)

    # ── 3. Scan ───────────────────────────────────────────────────────────
    logger.info("Scanning %s …", mount_path)
    scanner = MediaScanner(mount_path)
    try:
        media_files = scanner.scan()
    except (FileNotFoundError, NotADirectoryError) as exc:
        logger.error("Scan failed: %s", exc)
        return 1

    if not media_files:
        logger.info("No media files found on device. Done.")
        return 0

    logger.info("Found %d media files", len(media_files))

    # ── 4 + 5. Detect + Sort ──────────────────────────────────────────────
    engine = DetectionEngine(cfg.detection)
    sorter = FileSorter(cfg.paths, dry_run=dry_run)
    sort_results = []

    if _RICH:
        console = Console()
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        task = progress.add_task("Processing", total=len(media_files))

        with progress:
            for mf in media_files:
                progress.update(task, description=f"[cyan]{mf.path.name[:40]}[/cyan]")
                det = engine.process(mf)
                result = sorter.sort(mf, det)
                sort_results.append(result)
                progress.advance(task)
    else:
        for i, mf in enumerate(media_files, 1):
            print(f"[{i}/{len(media_files)}] {mf.path.name}", file=sys.stderr)
            det = engine.process(mf)
            result = sorter.sort(mf, det)
            sort_results.append(result)

    # ── 6. Report ─────────────────────────────────────────────────────────
    run_end = datetime.now()
    reporter = ReportWriter(dest_path, cfg.report)
    report_path = reporter.write(
        results=sort_results,
        device=device,
        run_start=run_start,
        run_end=run_end,
    )

    # Print summary
    moved = sum(1 for r in sort_results if r.action == SortAction.MOVED)
    dry = sum(1 for r in sort_results if r.action == SortAction.DRY_RUN)
    dupes = sum(1 for r in sort_results if r.action == SortAction.SKIP_DUPLICATE)
    errors = sum(1 for r in sort_results if r.action in (SortAction.COPY_ERROR, SortAction.DETECTION_ERROR))
    detected = sum(1 for r in sort_results if r.detection.detected)

    if dry_run:
        logger.info(
            "Dry-run complete — total=%d would-move=%d errors=%d detections=%d",
            len(sort_results), dry, errors, detected,
        )
    else:
        logger.info(
            "Run complete — total=%d moved=%d duplicates=%d errors=%d detections=%d",
            len(sort_results), moved, dupes, errors, detected,
        )
    logger.info("Report: %s", report_path)

    if _RICH:
        if dry_run:
            Console().print(
                f"\n[bold yellow]Dry run complete.[/bold yellow] "
                f"{dry} would be moved, {detected} with detections. "
                f"No files were copied or removed.\n"
                f"Report: [underline]{report_path}[/underline]"
            )
        else:
            Console().print(
                f"\n[bold green]Done.[/bold green] "
                f"{moved} moved, {dupes} duplicates, {errors} errors, "
                f"{detected} with detections.\n"
                f"Report: [underline]{report_path}[/underline]"
            )

    return 0 if errors == 0 else 2


# ---------------------------------------------------------------------------
# Report mode
# ---------------------------------------------------------------------------


def run_report(destination: str, last_n: int) -> None:
    reports_dir = Path(destination) / "reports"
    if not reports_dir.exists():
        print(f"No reports directory found at {reports_dir}", file=sys.stderr)
        return
    reports = sorted(reports_dir.glob("*.md"))
    if not reports:
        print("No reports found.", file=sys.stderr)
        return
    for rp in reports[-last_n:]:
        print(rp)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dadcam",
        description="Camera trap media processor with animal detection.",
    )
    sub = parser.add_subparsers(dest="mode")

    # --setup
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Interactive setup wizard (requires sudo).",
    )

    # --process
    parser.add_argument(
        "--process",
        action="store_true",
        help="Process media from a CF device.",
    )
    parser.add_argument(
        "--device",
        metavar="DEV",
        help="Block device path to process, e.g. /dev/sda1 (used with --process).",
    )
    parser.add_argument(
        "--source",
        metavar="DIR",
        help="Process a directory directly (no mount/unmount). Overrides --device.",
    )

    # --report
    parser.add_argument(
        "--report",
        action="store_true",
        help="List recent report paths.",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=5,
        metavar="N",
        help="Number of recent reports to show (default: 5).",
    )

    # --dry-run
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help=(
            "Log what would happen without copying or removing any files. "
            "A report is still written. Implies --process."
        ),
    )

    # --list-whitelist
    parser.add_argument(
        "--list-whitelist",
        action="store_true",
        help="Print all whitelisted drives.",
    )

    # --config
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Path to an additional config file.",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ── Setup mode ────────────────────────────────────────────────────────
    if args.setup:
        from setup_mode import run_setup
        run_setup()
        return

    # ── Config + logging ─────────────────────────────────────────────────
    ensure_user_config_exists()
    cfg = load_config(Path(args.config) if args.config else None)
    setup_logging(cfg)

    # ── List whitelist ────────────────────────────────────────────────────
    if args.list_whitelist:
        entries = wl.list_whitelist()
        if entries:
            for e in entries:
                print(e)
        else:
            print("Whitelist is empty.")
        return

    # ── Report mode ───────────────────────────────────────────────────────
    if args.report:
        run_report(cfg.paths.destination, args.last)
        return

    # ── Process mode ─────────────────────────────────────────────────────
    if args.process or args.device or args.source or args.dry_run:
        dry_run: bool = args.dry_run
        if args.source:
            # Directory mode — skip mount/unmount/whitelist
            source_path = Path(args.source)
            if not source_path.exists():
                logger.error("Source directory not found: %s", source_path)
                sys.exit(1)
            run_start = datetime.now()
            dest_path = Path(cfg.paths.destination)
            sys.exit(
                _process_mounted(args.source, source_path, cfg, run_start, dry_run=dry_run)
            )
        elif args.device:
            sys.exit(run_process(args.device, cfg, dry_run=dry_run))
        else:
            parser.error("--process requires --device <dev> or --source <dir>")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
