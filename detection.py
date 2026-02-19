"""
detection.py — DetectionEngine: run animal/person detection on images and video.

Model priority:
  1. YOLOv8n via ultralytics  (default)
  2. YOLOv5s via ultralytics  (fallback if yolov8n unavailable)

Model weights are cached in ~/.local/share/dadcam/models/.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from config import DetectionConfig
from scanner import MediaFile, MediaType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class DetectionResult:
    detected: bool
    labels: list[str] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    # For video: frame indices where detections occurred
    detection_frames: list[int] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        if not self.detected:
            return "no detection"
        pairs = ", ".join(
            f"{l} ({c:.2f})" for l, c in zip(self.labels, self.confidences)
        )
        return pairs


DETECTION_ERROR = DetectionResult(detected=False, error="detection_error")


# ---------------------------------------------------------------------------
# DetectionEngine
# ---------------------------------------------------------------------------


class DetectionEngine:
    """
    Loads the configured YOLO model and runs inference on images and video.
    CPU-only; no GPU required.
    """

    def __init__(self, config: DetectionConfig) -> None:
        self.config = config
        self.model = self._load_model()
        # Map model class names to lowercased strings once
        self._class_names: list[str] = [
            n.lower() for n in self.model.names.values()
        ]
        self._interest_set: set[str] = set(
            c.lower() for c in config.classes_of_interest
        )
        logger.info(
            "DetectionEngine ready — model: %s, classes: %s",
            config.model,
            sorted(self._interest_set),
        )

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model(self):
        try:
            from ultralytics import YOLO  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "ultralytics is not installed.  "
                "Run: pip install ultralytics"
            )

        model_dir = Path(self.config.model_dir)
        model_dir.mkdir(parents=True, exist_ok=True)

        # Tell ultralytics to use our model directory
        os.environ.setdefault("YOLO_CONFIG_DIR", str(model_dir))

        model_name = self.config.model if self.config.model.endswith(".pt") \
            else f"{self.config.model}.pt"

        local_path = model_dir / model_name
        if local_path.exists():
            logger.info("Loading model from cache: %s", local_path)
            model = YOLO(str(local_path))
        else:
            logger.info(
                "Model not found locally, downloading %s to %s",
                model_name,
                model_dir,
            )
            model = YOLO(model_name)
            # Save to cache for future runs
            try:
                import shutil
                downloaded = Path(model_name)
                if downloaded.exists():
                    shutil.move(str(downloaded), str(local_path))
            except Exception as exc:
                logger.debug("Could not cache model file: %s", exc)

        # Force CPU
        model.to("cpu")
        return model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, media_file: MediaFile) -> DetectionResult:
        """Run detection on a MediaFile. Returns a DetectionResult."""
        try:
            if media_file.media_type == MediaType.IMAGE:
                return self._process_image(media_file.path)
            else:
                return self._process_video(media_file.path)
        except Exception as exc:
            logger.error("Detection failed for %s: %s", media_file.path, exc)
            return DetectionResult(detected=False, error=str(exc))

    # ------------------------------------------------------------------
    # Image
    # ------------------------------------------------------------------

    def _process_image(self, path: Path) -> DetectionResult:
        try:
            img = Image.open(path).convert("RGB")
        except Exception as exc:
            logger.warning("Cannot open image %s: %s", path, exc)
            return DetectionResult(detected=False, error=f"open_error: {exc}")

        return self._run_inference_pil(img)

    def _run_inference_pil(self, img: Image.Image) -> DetectionResult:
        results = self.model(
            img,
            verbose=False,
            conf=self.config.confidence_threshold,
            device="cpu",
        )
        return self._parse_results(results)

    def _run_inference_array(self, frame: np.ndarray) -> DetectionResult:
        # Convert BGR (OpenCV) → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.model(
            rgb,
            verbose=False,
            conf=self.config.confidence_threshold,
            device="cpu",
        )
        return self._parse_results(results)

    def _parse_results(self, results) -> DetectionResult:
        labels: list[str] = []
        confidences: list[float] = []

        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                cls_idx = int(box.cls[0].item())
                conf = float(box.conf[0].item())
                label = self._class_names[cls_idx] if cls_idx < len(self._class_names) else str(cls_idx)
                if label not in self._interest_set:
                    continue
                labels.append(label)
                confidences.append(round(conf, 4))

        detected = len(labels) > 0
        return DetectionResult(detected=detected, labels=labels, confidences=confidences)

    # ------------------------------------------------------------------
    # Video
    # ------------------------------------------------------------------

    def _process_video(self, path: Path) -> DetectionResult:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            logger.warning("Cannot open video: %s", path)
            return DetectionResult(detected=False, error="video_open_error")

        interval = self.config.frame_sample_interval
        all_labels: list[str] = []
        all_confidences: list[float] = []
        detection_frames: list[int] = []
        frame_idx = 0

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                if frame_idx % interval == 0:
                    frame_result = self._run_inference_array(frame)
                    if frame_result.detected:
                        detection_frames.append(frame_idx)
                        all_labels.extend(frame_result.labels)
                        all_confidences.extend(frame_result.confidences)
                        logger.debug(
                            "Video %s frame %d: %s",
                            path.name,
                            frame_idx,
                            frame_result.summary(),
                        )

                frame_idx += 1
        finally:
            cap.release()

        # Deduplicate labels (keep highest confidence per class)
        best: dict[str, float] = {}
        for lbl, conf in zip(all_labels, all_confidences):
            if lbl not in best or conf > best[lbl]:
                best[lbl] = conf

        detected = len(best) > 0
        return DetectionResult(
            detected=detected,
            labels=list(best.keys()),
            confidences=[best[l] for l in best],
            detection_frames=detection_frames,
        )
