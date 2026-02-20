"""
reporter.py — ReportWriter: produce a Markdown run report.

Reports are written to <destination>/reports/YYYY-MM-DD_HH-MM-SS.md.
Old reports beyond the keep_reports limit are pruned automatically.
"""

from __future__ import annotations

import logging
import platform
import socket
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import ReportConfig
from sorter import SortAction, SortResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ReportWriter
# ---------------------------------------------------------------------------


class ReportWriter:
    def __init__(self, destination: Path, config: ReportConfig) -> None:
        self.reports_dir = destination / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.config = config

    def write(
        self,
        results: list[SortResult],
        device: str = "",
        run_start: datetime | None = None,
        run_end: datetime | None = None,
    ) -> Path:
        """Render and save the Markdown report.  Returns the report path."""
        now = run_end or datetime.now()
        ts = now.strftime("%Y-%m-%d_%H-%M-%S")
        report_path = self.reports_dir / f"{ts}.md"

        content = self._render(results, device, run_start, run_end)
        report_path.write_text(content, encoding="utf-8")
        logger.info("Report written: %s", report_path)

        self._prune(self.config.keep_reports)
        return report_path

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(
        self,
        results: list[SortResult],
        device: str,
        run_start: datetime | None,
        run_end: datetime | None,
    ) -> str:
        now = run_end or datetime.now()
        start_str = run_start.strftime("%Y-%m-%d %H:%M:%S") if run_start else "—"
        end_str = now.strftime("%Y-%m-%d %H:%M:%S")
        duration = (
            str(run_end - run_start).split(".")[0] if run_start and run_end else "—"
        )

        # Counts
        total = len(results)
        n_moved = sum(1 for r in results if r.action == SortAction.MOVED)
        n_dry = sum(1 for r in results if r.action == SortAction.DRY_RUN)
        n_dup = sum(1 for r in results if r.action == SortAction.SKIP_DUPLICATE)
        n_det_err = sum(1 for r in results if r.action == SortAction.DETECTION_ERROR)
        n_copy_err = sum(1 for r in results if r.action == SortAction.COPY_ERROR)
        n_detected = sum(
            1 for r in results if r.detection.detected
            and r.action in (SortAction.MOVED, SortAction.SKIP_DUPLICATE, SortAction.DRY_RUN)
        )

        # Top detected labels
        label_counter: Counter[str] = Counter()
        for r in results:
            for lbl in r.detection.labels:
                label_counter[lbl] += 1
        top_labels = (
            ", ".join(f"{lbl} ({cnt})" for lbl, cnt in label_counter.most_common(10))
            or "—"
        )

        lines: list[str] = [
            "# dadcam Run Report",
            "",
            f"| Field | Value |",
            f"|-------|-------|",
            f"| Timestamp | {end_str} |",
            f"| Host | {socket.gethostname()} |",
            f"| Platform | {platform.platform()} |",
            f"| Device | {device or '—'} |",
            f"| Run start | {start_str} |",
            f"| Run end | {end_str} |",
            f"| Duration | {duration} |",
            f"| Dry run | {'yes — no files were moved or removed' if n_dry > 0 else 'no'} |",
            "",
            "## Summary",
            "",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"| Total files processed | {total} |",
            f"| Moved to destination | {n_moved} |",
            f"| Would move (dry run) | {n_dry} |",
            f"| Skipped (duplicate) | {n_dup} |",
            f"| Detection errors | {n_det_err} |",
            f"| Copy errors | {n_copy_err} |",
            f"| Files with detections | {n_detected} |",
            f"| Top detected labels | {top_labels} |",
            "",
            "## Per-File Results",
            "",
            "| File | Type | Detected | Labels | Confidence | Action |",
            "|------|------|----------|--------|------------|--------|",
        ]

        for r in results:
            fname = r.media_file.path.name
            ftype = r.media_file.media_type.name.lower()
            det_icon = "✓" if r.detection.detected else "✗"
            if r.detection.error:
                det_icon = "⚠"
            labels_str = (
                ", ".join(r.detection.labels) if r.detection.labels else "—"
            )
            conf_str = (
                ", ".join(f"{c:.2f}" for c in r.detection.confidences)
                if r.detection.confidences
                else "—"
            )
            action_str = r.action.name
            if r.detection.error and r.action in (
                SortAction.DETECTION_ERROR, SortAction.COPY_ERROR
            ):
                action_str = f"{action_str} ({r.detection.error})"

            lines.append(
                f"| {fname} | {ftype} | {det_icon} | "
                f"{labels_str} | {conf_str} | {action_str} |"
            )

        lines += ["", "---", f"*Generated by dadcam*", ""]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Pruning old reports
    # ------------------------------------------------------------------

    def _prune(self, keep: int) -> None:
        if keep <= 0:
            return
        reports = sorted(self.reports_dir.glob("*.md"))
        excess = len(reports) - keep
        for old in reports[:excess]:
            try:
                old.unlink()
                logger.debug("Pruned old report: %s", old.name)
            except OSError as exc:
                logger.warning("Could not prune report %s: %s", old.name, exc)
