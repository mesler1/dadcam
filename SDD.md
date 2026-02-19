# Software Design Document: dadcam

**Version:** 1.1  
**Date:** 2026-02-18  
**Status:** Draft — decisions locked

---

## 1. Overview

`dadcam` is a Python utility that automatically ingests media (images and video) from a compact flash (CF) drive, runs an animal/creature/person detection model against each file, and sorts results into a structured destination folder. It also includes a privileged setup mode that generates and installs a `udev` rule and `systemd` service so the entire pipeline runs automatically when the CF drive is inserted.

**Target platform:** Valve Steam Deck running SteamOS 3.x (Arch Linux base, AMD APU, CPU-only inference). SteamOS uses a read-only root filesystem; the install script temporarily disables that protection via `steamos-readonly disable`.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- Detect CF drive insertion via `udev` and trigger processing automatically.
- Run a local image/video recognition model to classify each file as containing a detection (animal, mammal, bird, person) or not.
- Sort files into `detections/` and `no_detections/` subdirectories within a configured destination folder.
- Deduplicate: if an identical file already exists at the destination, skip the copy and remove from source.
- Produce a human-readable processing report per run.
- Provide a self-service setup mode (run with `sudo`) for whitelisting a drive and installing the `udev` rule.

### 2.2 Non-Goals

- Cloud-based inference.
- Real-time streaming video analysis.
- GUI interface.

---

## 3. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                       User / udev / systemd                          │
│                                                                      │
│  CF Drive inserted                                                   │
│       │                                                              │
│       ▼                                                              │
│  udev rule (99-dadcam.rules)                                         │
│       │  ENV{SYSTEMD_WANTS}="dadcam@%k.service"                      │
│       ▼                                                              │
│  systemd activates dadcam@<dev>.service  (User=deck)                 │
│       │                                                              │
│       ▼                                                              │
│  dadcam --process --device /dev/<dev>                                │
│                                                                      │
│  Admin: sudo dadcam --setup  ──►  whitelist + udev rule + service    │
└──────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────┐
│              dadcam.py                │
│                                       │
│  ┌──────────┐   ┌──────────────────┐  │
│  │  Setup   │   │  Processor       │  │
│  │  Mode    │   │  Pipeline        │  │
│  └──────────┘   └────────┬─────────┘  │
│                          │            │
│               ┌──────────▼──────────┐ │
│               │   MediaScanner      │ │
│               │  (walk source dir)  │ │
│               └──────────┬──────────┘ │
│                          │            │
│               ┌──────────▼──────────┐ │
│               │  DetectionEngine    │ │
│               │  (YOLO / CLIP / CV2)│ │
│               └──────────┬──────────┘ │
│                          │            │
│               ┌──────────▼──────────┐ │
│               │   FileSorter        │ │
│               │  (copy → dest,      │ │
│               │   remove from src)  │ │
│               └──────────┬──────────┘ │
│                          │            │
│               ┌──────────▼──────────┐ │
│               │   ReportWriter      │ │
│               └─────────────────────┘ │
└───────────────────────────────────────┘
```

---

## 4. Operational Modes

### 4.1 `--setup` (requires `sudo`)

Purpose: Interactively whitelist a CF drive and install the `udev` rule + systemd service.

**Steam Deck note:** SteamOS root is read-only. Setup automatically calls `steamos-readonly disable` before writing to `/etc/` and re-enables it afterwards.

**Steps:**

1. Verify `os.geteuid() == 0`; abort with instructions if not.
2. Call `steamos-readonly disable`.
3. Prompt the user to insert the target CF drive.
4. Watch `udev` events (via `pyudev`) for block device additions.
5. When a device appears, read its attributes:
   - `ID_SERIAL` (or `ID_SERIAL_SHORT`)
   - `ID_VENDOR`, `ID_MODEL`
   - Filesystem UUID (via `blkid`)
6. Display detected attributes and ask the user to confirm whitelisting.
7. Append the drive serial/UUID to `~/.config/dadcam/whitelist.conf`.
8. Write the `udev` rule to `/etc/udev/rules.d/99-dadcam.rules` (the only write that requires the read-only fs to be disabled).
9. Call `steamos-readonly enable` immediately after step 8.
10. Write (or update) the systemd **user** service unit to `~/.config/systemd/user/dadcam@.service` (no root needed).
11. Run `loginctl enable-linger deck` so the user session persists at boot without a desktop login.
12. Reload udev and the user daemon: `udevadm control --reload-rules && systemctl --user daemon-reload`.
13. Print a confirmation summary.

**Generated udev rule (`/etc/udev/rules.d/99-dadcam.rules`):**

```
# dadcam — trigger processing when a whitelisted CF card is inserted
ACTION=="add", SUBSYSTEM=="block", ENV{DEVTYPE}=="partition", \
  ENV{ID_FS_UUID}=="<UUID>", TAG+="systemd", \
  ENV{SYSTEMD_USER_WANTS}="dadcam@%k.service"
```

`SYSTEMD_USER_WANTS` routes activation to the `deck` user's systemd session (UID 1000). Because `loginctl enable-linger deck` is set during setup, that session is always running at boot — no desktop login required.

**Generated systemd user service (`~/.config/systemd/user/dadcam@.service`):**

```ini
[Unit]
Description=dadcam processor for %I
After=dev-%i.device
Requires=dev-%i.device

[Service]
Type=oneshot
ExecStart=/home/deck/dadcam/venv/bin/python /home/deck/dadcam/dadcam.py --process --device /dev/%I
StandardOutput=journal
StandardError=journal
TimeoutStartSec=3600
```

No `User=` directive needed — the unit runs inside the `deck` user session by definition. `Type=oneshot` with a 1-hour timeout avoids the 30-second udev `RUN+=` kill limit. Logs visible via `journalctl --user -u dadcam@*.service`.

### 4.2 `--process` (triggered by systemd service or run manually)

Purpose: Mount the CF drive (if not already mounted), scan and process all media.

When invoked by the user systemd service the process already runs as `deck`. Mounting uses `udisksctl mount -b /dev/<device>`, which is permitted for the local user via PolicyKit on SteamOS — no sudo required.

**Steps:**

1. Verify the device serial/UUID is in the whitelist (`/etc/dadcam/whitelist.conf`). Abort if not found.
2. Mount the partition via `udisksctl mount -b /dev/<device>` (no root needed). Record the mount path from its output.
3. Run the **Processor Pipeline** (§5).
4. Unmount via `udisksctl unmount -b /dev/<device>`.
5. Write the run report to `<destination>/reports/`.

### 4.3 `--report` (optional manual mode)

Purpose: Print or re-generate the last N reports without reprocessing.

---

## 5. Processor Pipeline

### 5.1 MediaScanner

- Recursively walks the source directory (CF mount point or user-specified path).
- Supported extensions:
  - **Images:** `.jpg`, `.jpeg`, `.png`, `.tiff`, `.tif`, `.bmp`
  - **Video:** `.mp4`, `.mov`, `.avi`, `.mts`, `.m4v`, `.mkv`
- RAW formats (CR2, NEF, ARW, DNG, etc.) are **out of scope** and will be skipped.
- Builds a list of `MediaFile` objects (path, type, size, mtime).

### 5.2 DetectionEngine

#### 5.2.1 Model Selection

**Decision: YOLOv8n (Ultralytics) on CPU.** The Steam Deck's AMD RDNA2 iGPU is not targeted (ROCm support for PyTorch is complex and fragile on SteamOS). CPU inference with YOLOv8n is fast enough for typical SD card batch sizes.

| Priority | Model | Notes |
|----------|-------|-------|
| 1 | **YOLOv8n** (Ultralytics) | ~6ms/image on CPU at 640px. Default. |
| 2 | **YOLOv5s** | Fallback if Ultralytics not available. |

Model weights are downloaded on first run to `~/.local/share/dadcam/models/`. The model path/variant is configurable.

#### 5.2.2 Detection Classes of Interest

Drawn from COCO labels:
- `person`
- `bird`
- `cat`, `dog`, `horse`, `sheep`, `cow`, `elephant`, `bear`, `zebra`, `giraffe`
- `deer` (if using extended VOC/OpenImages models)

The full list is configurable.

#### 5.2.3 Image Processing

1. Decode image with `Pillow` (JPEG, PNG, TIFF, BMP).
2. Resize to model input size (640×640 for YOLO) — handled internally by Ultralytics.
3. Run inference.
4. Parse detections: filter by class and confidence threshold (default: 0.35).
5. Return `DetectionResult(detected: bool, labels: list[str], confidences: list[float])`.

#### 5.2.4 Video Processing

1. Open video with `OpenCV` (`cv2.VideoCapture`).
2. Sample frames at a configurable interval (default: every 30 frames, or every 1 second at 30 fps).
3. Run image detection on each sampled frame.
4. Aggregate: `detected = True` if **any** sampled frame returns a detection.
5. Record which frame indices triggered detections.

### 5.3 FileSorter

For each `MediaFile` + `DetectionResult`:

1. Determine destination subfolder:
   - Detection → `<dest>/detections/`
   - No detection → `<dest>/no_detections/`
2. Preserve the original relative path within that subfolder (optional, configurable).
3. **Deduplication check:** Compute SHA-256 of source file. If a file with the same name and hash already exists at destination:
   - Skip the copy.
   - Remove from source.
   - Log as `SKIP_DUPLICATE`.
4. Otherwise, copy the file (`shutil.copy2` to preserve metadata).
5. Verify the copy (compare SHA-256 of source vs destination).
6. If verified, remove from source.
7. If verification fails, leave source intact and log as `COPY_ERROR`.

### 5.4 ReportWriter

Produces a Markdown report at `<dest>/reports/YYYY-MM-DD_HH-MM-SS.md` containing:

- Run timestamp, hostname, device identifier
- Total files found / processed / skipped / errored
- Detection summary (files with detections, top labels)
- Per-file table:

| File | Type | Detection | Labels | Confidence | Action |
|------|------|-----------|--------|------------|--------|
| IMG_001.jpg | image | ✓ | bird, deer | 0.87, 0.62 | MOVED |
| VID_002.mp4 | video | ✗ | — | — | MOVED |
| IMG_003.cr2 | image | ✓ | person | 0.91 | SKIP_DUPLICATE |

---

## 6. Configuration

Config file location: `/etc/dadcam/dadcam.conf` (system) or `~/.config/dadcam/dadcam.conf` (user).  
Format: TOML.

```toml
[paths]
destination = "/home/user/Pictures/dadcam_output"
mount_point = "/mnt/dadcam_cf"

[detection]
model = "yolov8n"            # yolov8n | yolov5s | resnet50
confidence_threshold = 0.35
classes_of_interest = [
  "person", "bird", "cat", "dog", "horse", "sheep",
  "cow", "elephant", "bear", "zebra", "giraffe"
]

[video]
frame_sample_interval = 30  # sample every N frames

[report]
format = "markdown"          # markdown only for now
keep_reports = 50            # max number of reports to retain

[logging]
level = "INFO"               # DEBUG | INFO | WARNING | ERROR
log_file = "/var/log/dadcam/dadcam.log"
```

---

## 7. Whitelist File

Location: `/etc/dadcam/whitelist.conf`  
Format: one entry per line — `UUID=<uuid>` or `SERIAL=<serial>`.

```
UUID=A1B2-C3D4
SERIAL=CF_CARD_LEXAR_12345
```

---

## 8. File & Directory Layout

```
# System path (only this directory requires steamos-readonly disable)
/etc/udev/rules.d/
└── 99-dadcam.rules

# User paths — all writable as deck, no root needed
/home/deck/.config/dadcam/
├── dadcam.conf
└── whitelist.conf

/home/deck/.config/systemd/user/
└── dadcam@.service

/home/deck/.local/share/dadcam/
├── logs/
│   └── dadcam.log
└── models/
    └── yolov8n.pt       # downloaded on first run

# Script installation
/home/deck/dadcam/
├── venv/                # Python virtualenv
├── dadcam.py            # main entry point
├── scanner.py           # MediaScanner
├── detection.py         # DetectionEngine
├── sorter.py            # FileSorter
├── reporter.py          # ReportWriter
├── setup_mode.py        # udev + systemd setup wizard
├── config.py            # config loading
├── whitelist.py         # whitelist management
├── requirements.txt
├── install.sh           # bootstraps venv, disables RO fs, writes system files
└── SDD.md

# Output (user-configured destination)
<destination>/           # e.g. /home/deck/Pictures/dadcam_output
├── detections/
│   └── <original relative paths>
├── no_detections/
│   └── <original relative paths>
└── reports/
    └── 2026-02-18_14-30-00.md
```

---

## 9. Dependencies

| Package | Version hint | Purpose |
|---------|-------------|----------|
| `ultralytics` | ≥8.0 | YOLOv8n inference |
| `opencv-python-headless` | ≥4.8 | Video frame sampling (headless — no GUI needed on Steam Deck) |
| `Pillow` | ≥10.0 | Image loading (JPEG, PNG, TIFF, BMP) |
| `pyudev` | ≥0.24 | udev event monitoring in `--setup` mode |
| `tomli` | ≥2.0 | TOML config parsing (backport; use stdlib `tomllib` on Python 3.11+) |
| `rich` | ≥13.0 | Terminal output and progress bars |
| `torch` (CPU) | ≥2.0 | Pulled in by Ultralytics; install CPU-only wheel to save space |

**RAW support (`rawpy`) is not required.**

**Install note:** All packages live inside `/home/deck/dadcam/venv/`. The Steam Deck's SteamOS Python is not modified.

---

## 10. Security Considerations

- Setup mode (`--setup`) requires `root` for two operations only: writing the udev rule to `/etc/udev/rules.d/` and running `loginctl enable-linger deck`. The script checks `os.geteuid() == 0` and aborts otherwise.
- `steamos-readonly disable/enable` brackets only the udev rule write — the window is as short as possible.
- The systemd **user** service runs entirely as `deck` with no elevated privileges. Mounting uses `udisksctl` via PolicyKit.
- Config, whitelist, service unit, logs, and model weights all live under `/home/deck/` — unaffected by SteamOS OS updates that reset `/etc/` and `/usr/`.
- All file operations validate paths with `Path.resolve()` and prefix checks to prevent traversal.
- The whitelist (UUID/serial check) prevents arbitrary drives from triggering processing.

---

## 11. Error Handling

| Scenario | Behavior |
|----------|----------|
| Device not in whitelist | Log warning, exit cleanly |
| Mount fails | Log error, exit without processing |
| Model not found / inference error | Log error, mark file as `DETECTION_ERROR`, do not move |
| Copy verification failure | Log error, leave file on source, mark `COPY_ERROR` |
| Destination disk full | Abort run, write partial report, log critical error |
| Unsupported file type | Skip silently, log at DEBUG level |

---

## 12. Resolved Decisions

| Decision | Resolution |
|----------|------------|
| RAW support | **Out of scope.** JPEG/PNG/TIFF/BMP + common video only. |
| GPU acceleration | **CPU-only.** AMD RDNA2 ROCm support on SteamOS is fragile; YOLOv8n on CPU is sufficient. |
| udev vs systemd | **Both, user mode.** udev detects device → `ENV{SYSTEMD_USER_WANTS}` activates `dadcam@.service` in the `deck` user session → no 30s kill limit. `loginctl enable-linger deck` ensures the session exists at boot. |
| Target OS | **SteamOS 3.x (Steam Deck).** Install script handles `steamos-readonly disable/enable`. |

## 13. Future Work

- **Dry-run mode:** `--dry-run` flag to preview actions without moving files.
- **Notification:** Post-run desktop notification via `notify-send` (Steam Deck supports this).
- **HEIC support:** Add `pillow-heif` plugin if camera source produces HEIC files.
- **Extended model:** Evaluate iNaturalist-trained model for wildlife-specific species identification.
- **SteamOS update resilience:** SteamOS updates reset `/etc/udev/rules.d/` along with the root filesystem. The udev rule must be re-installed after a major OS update. The install script should detect a missing rule and prompt the user. All other files (service unit, config, whitelist, models) survive updates as they live in `/home/deck/`.
