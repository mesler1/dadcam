"""
config.py — load and validate dadcam configuration.

Search order (later entries override earlier):
  1. Built-in defaults
  2. /etc/dadcam/dadcam.conf   (system-wide, if present)
  3. ~/.config/dadcam/dadcam.conf  (user, primary location on Steam Deck)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        raise ImportError(
            "tomli is required for Python < 3.11.  "
            "Install it with: pip install tomli"
        )

# ---------------------------------------------------------------------------
# Default configuration values
# ---------------------------------------------------------------------------

SYSTEM_CONF = Path("/etc/dadcam/dadcam.conf")
USER_CONF = Path.home() / ".config" / "dadcam" / "dadcam.conf"

DEFAULT_DESTINATION = str(Path.home() / "Pictures" / "dadcam_output")
DEFAULT_LOG_FILE = str(
    Path.home() / ".local" / "share" / "dadcam" / "logs" / "dadcam.log"
)
DEFAULT_MODEL_DIR = str(
    Path.home() / ".local" / "share" / "dadcam" / "models"
)

CLASSES_OF_INTEREST: list[str] = [
    "person",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
]


# ---------------------------------------------------------------------------
# Dataclass representing the full config
# ---------------------------------------------------------------------------


@dataclass
class PathsConfig:
    destination: str = DEFAULT_DESTINATION
    mount_point: str = "/mnt/dadcam_cf"


@dataclass
class DetectionConfig:
    model: str = "yolov8n"          # yolov8n | yolov5s
    confidence_threshold: float = 0.35
    classes_of_interest: list[str] = field(
        default_factory=lambda: list(CLASSES_OF_INTEREST)
    )
    model_dir: str = DEFAULT_MODEL_DIR


@dataclass
class VideoConfig:
    frame_sample_interval: int = 30  # process every N frames


@dataclass
class ReportConfig:
    format: str = "markdown"
    keep_reports: int = 50


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = DEFAULT_LOG_FILE


@dataclass
class DadcamConfig:
    paths: PathsConfig = field(default_factory=PathsConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise ValueError(f"Failed to parse config file {path}: {exc}") from exc


def load_config(extra_path: Path | None = None) -> DadcamConfig:
    """Load and merge config from all known locations."""
    raw: dict = {}
    for conf_path in [SYSTEM_CONF, USER_CONF]:
        raw = _merge(raw, _load_toml(conf_path))
    if extra_path:
        raw = _merge(raw, _load_toml(extra_path))

    cfg = DadcamConfig()

    p = raw.get("paths", {})
    cfg.paths.destination = p.get("destination", cfg.paths.destination)
    cfg.paths.mount_point = p.get("mount_point", cfg.paths.mount_point)

    d = raw.get("detection", {})
    cfg.detection.model = d.get("model", cfg.detection.model)
    cfg.detection.confidence_threshold = float(
        d.get("confidence_threshold", cfg.detection.confidence_threshold)
    )
    cfg.detection.classes_of_interest = d.get(
        "classes_of_interest", cfg.detection.classes_of_interest
    )
    cfg.detection.model_dir = d.get("model_dir", cfg.detection.model_dir)

    v = raw.get("video", {})
    cfg.video.frame_sample_interval = int(
        v.get("frame_sample_interval", cfg.video.frame_sample_interval)
    )

    r = raw.get("report", {})
    cfg.report.format = r.get("format", cfg.report.format)
    cfg.report.keep_reports = int(
        r.get("keep_reports", cfg.report.keep_reports)
    )

    lo = raw.get("logging", {})
    cfg.logging.level = lo.get("level", cfg.logging.level).upper()
    cfg.logging.log_file = lo.get("log_file", cfg.logging.log_file)

    return cfg


def ensure_user_config_exists() -> None:
    """Write a default config file to the user location if none exists."""
    if USER_CONF.exists():
        return
    USER_CONF.parent.mkdir(parents=True, exist_ok=True)
    USER_CONF.write_text(
        f"""\
[paths]
destination = "{DEFAULT_DESTINATION}"

[detection]
model = "yolov8n"
confidence_threshold = 0.35
classes_of_interest = [
  "person", "bird", "cat", "dog", "horse", "sheep",
  "cow", "elephant", "bear", "zebra", "giraffe"
]

[video]
frame_sample_interval = 30

[report]
keep_reports = 50

[logging]
level = "INFO"
log_file = "{DEFAULT_LOG_FILE}"
""",
        encoding="utf-8",
    )
