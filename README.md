# dadcam

Automatic camera trap media processor for the **Steam Deck**.

Insert a compact flash card from a trail/wildlife camera and dadcam will:

1. Detect the card via udev
2. Run YOLOv8 animal/person detection on every image and video clip
3. Sort files into `detections/` or `no_detections/` on your destination drive
4. Remove originals from the CF card after verified copy
5. Write a Markdown report summarising the run

Everything runs locally on the Deck's CPU — no cloud, no internet required after setup.

---

## Requirements

- Steam Deck (SteamOS 3.x) in Desktop Mode
- The CF card reader you use (USB or dock)
- ~2 GB free space for the Python virtualenv + YOLOv8 model weights

---

## Installation

### 1. Get the code onto the Deck

```bash
# Option A — git
git clone https://github.com/you/dadcam.git ~/dadcam

# Option B — copy from another machine
scp -r /path/to/dadcam deck@steamdeck.local:~/dadcam
```

### 2. Run the installer

Run as the `deck` user (**not** root):

```bash
cd ~/dadcam
bash install.sh
```

This will:
- Create `~/dadcam/venv/` with an isolated Python environment
- Install CPU-only PyTorch (~200 MB) and all other dependencies
- Write a default config to `~/.config/dadcam/dadcam.conf`
- Install `~/.config/systemd/user/dadcam@.service` (no root required)
- Enable systemd linger via `sudo loginctl enable-linger deck` so the service
  activates at boot without a desktop login (does **not** disable the read-only root fs)

### 3. Edit your config

```bash
nano ~/.config/dadcam/dadcam.conf
```

At minimum, set the destination — typically your SD card:

```toml
[paths]
destination = "/run/media/deck/MY_SDCARD/dadcam_output"
```

### 4. Whitelist your CF card and install the udev rule

Insert the CF card, then run the setup wizard **once**:

```bash
sudo ~/dadcam/venv/bin/python ~/dadcam/dadcam.py --setup
```

The wizard will:
- Detect the inserted card and display its UUID and serial
- Ask you to confirm whitelisting
- Write `/etc/udev/rules.d/99-dadcam.rules` (briefly disables SteamOS read-only fs)

From this point, **inserting the CF card triggers processing automatically**.

---

## Usage

### Automatic (normal use)

Insert the whitelisted CF card. Processing starts within a few seconds.
Watch progress in real time:

```bash
journalctl --user -u 'dadcam@*.service' -f
```

### Manual — test on a folder

```bash
~/dadcam/venv/bin/python ~/dadcam/dadcam.py --source /path/to/photos
```

### Manual — process a specific device

```bash
~/dadcam/venv/bin/python ~/dadcam/dadcam.py --process --device /dev/sda1
```

### View recent reports

```bash
~/dadcam/venv/bin/python ~/dadcam/dadcam.py --report --last 5
```

### List whitelisted drives

```bash
~/dadcam/venv/bin/python ~/dadcam/dadcam.py --list-whitelist
```

### Add another CF card

Re-run setup with the new card inserted:

```bash
sudo ~/dadcam/venv/bin/python ~/dadcam/dadcam.py --setup
```

---

## Output structure

```
<destination>/
├── detections/          # files where an animal or person was detected
│   └── IMG_0042.jpg
├── no_detections/       # files with nothing of interest
│   └── IMG_0001.jpg
└── reports/
    └── 2026-02-18_14-30-00.md
```

Original files are removed from the CF card only after the copy is SHA-256 verified.
Files already present at the destination with matching content are skipped (deduplication).

---

## Detection

Uses **YOLOv8n** (Ultralytics) running on the Deck's CPU.  
Model weights (~6 MB) are downloaded automatically on the first run to `~/.local/share/dadcam/models/`.

**Default classes detected:** person, bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe.

Customise in `~/.config/dadcam/dadcam.conf`:

```toml
[detection]
model = "yolov8n"
confidence_threshold = 0.35
classes_of_interest = ["person", "bird", "deer", "bear"]
```

For video files, frames are sampled every 30 frames (configurable via `frame_sample_interval`).
A video is marked as a detection if **any** sampled frame contains a match.

---

## Configuration reference

`~/.config/dadcam/dadcam.conf` (TOML format):

```toml
[paths]
destination = "/home/deck/Pictures/dadcam_output"

[detection]
model = "yolov8n"               # yolov8n | yolov5s
confidence_threshold = 0.35
classes_of_interest = [
  "person", "bird", "cat", "dog", "horse", "sheep",
  "cow", "elephant", "bear", "zebra", "giraffe"
]

[video]
frame_sample_interval = 30      # check every N frames

[report]
keep_reports = 50               # oldest reports pruned automatically

[logging]
level = "INFO"                  # DEBUG | INFO | WARNING | ERROR
log_file = "/home/deck/.local/share/dadcam/logs/dadcam.log"
```

---

## File layout

| Path | Purpose |
|------|---------|
| `~/dadcam/` | Scripts and virtualenv |
| `~/.config/dadcam/dadcam.conf` | User configuration |
| `~/.config/dadcam/whitelist.conf` | Whitelisted CF card UUIDs / serials |
| `~/.config/systemd/user/dadcam@.service` | systemd user service unit |
| `~/.local/share/dadcam/models/yolov8n.pt` | Cached model weights |
| `~/.local/share/dadcam/logs/dadcam.log` | Persistent log file |
| `/etc/udev/rules.d/99-dadcam.rules` | udev device trigger ⚠️ see note below |

> **SteamOS update note:** Major SteamOS updates reset `/etc/udev/rules.d/`.
> After an update, re-run `sudo ~/dadcam/venv/bin/python ~/dadcam/dadcam.py --setup`
> to restore the udev rule. Everything else (config, whitelist, service, models) lives
> in your home directory and is unaffected.

---

## Troubleshooting

**Card inserted but nothing happens**

```bash
# Check the udev rule is installed
cat /etc/udev/rules.d/99-dadcam.rules

# Check the systemd service exists
ls ~/.config/systemd/user/dadcam@.service

# Check linger is enabled
loginctl show-user deck | grep Linger

# Re-run setup if any of the above are missing
sudo ~/dadcam/venv/bin/python ~/dadcam/dadcam.py --setup
```

**"Device not whitelisted" in logs**

The card's UUID or serial doesn't match the whitelist.  Run setup again with the card inserted.

```bash
~/dadcam/venv/bin/python ~/dadcam/dadcam.py --list-whitelist
```

**Model download fails**

Ensure the Deck has internet access on first run, or manually download `yolov8n.pt` from
[ultralytics releases](https://github.com/ultralytics/assets/releases) and place it in
`~/.local/share/dadcam/models/yolov8n.pt`.

**Check recent run logs**

```bash
journalctl --user -u 'dadcam@*.service' --since "1 hour ago"
```

---

## Project layout

```
dadcam/
├── dadcam.py        # CLI entry point
├── scanner.py       # walk source directory, enumerate media files
├── detection.py     # YOLOv8 inference on images and video
├── sorter.py        # copy + SHA-256 verify + remove source
├── reporter.py      # write Markdown run report
├── setup_mode.py    # interactive udev + systemd setup wizard
├── config.py        # TOML config loader
├── whitelist.py     # whitelist read/write
├── requirements.txt
├── install.sh       # bootstrap virtualenv and dependencies
├── README.md
└── SDD.md           # software design document
```

---

## License

MIT
