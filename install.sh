#!/usr/bin/env bash
# install.sh — bootstrap dadcam on a Steam Deck
#
# Run as the deck user (NOT as root):
#   bash install.sh
#
# What this script does:
#   1. Checks prerequisites (Python 3.10+, pip)
#   2. Creates a Python virtualenv at ~/dadcam/venv
#   3. Installs CPU-only PyTorch, then all pip requirements
#   4. Creates the user config file if it does not exist
#   5. Prints next steps (sudo dadcam --setup)
#
# SteamOS note:
#   The virtualenv lives entirely in /home/deck — no system Python modification.
#   The udev rule (written to /etc) is handled by --setup, not this script.
#   SteamOS updates may wipe /etc/udev/rules.d/. Re-run --setup after OS updates.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON="${PYTHON:-python3}"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()  { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error() { echo -e "${RED}[error]${RESET} $*" >&2; }

# ── Abort if running as root ──────────────────────────────────────────────────
if [[ $EUID -eq 0 ]]; then
    error "Do not run install.sh as root."
    error "Run it as the deck user: bash install.sh"
    error "The script will prompt for sudo only when needed."
    exit 1
fi

echo -e "${BOLD}dadcam installer${RESET}"
echo "─────────────────────────────────────────────"

# ── 1. Python version check ───────────────────────────────────────────────────
info "Checking Python version …"
PY_VERSION=$("$PYTHON" -c "import sys; print(sys.version_info[:2])" 2>/dev/null || true)
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")

if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ) ]]; then
    error "Python 3.10 or newer is required. Found: $("$PYTHON" --version 2>&1)"
    error "On Steam Deck: python3 is typically available via the OS or flatpak."
    exit 1
fi
ok "Python $PY_MAJOR.$PY_MINOR found at $("$PYTHON" -c 'import sys; print(sys.executable)')"

# ── 2. Create virtualenv ──────────────────────────────────────────────────────
if [[ -d "$VENV_DIR" ]]; then
    warn "Virtualenv already exists at $VENV_DIR — skipping creation."
    warn "Delete it first if you want a clean install: rm -rf $VENV_DIR"
else
    info "Creating virtualenv at $VENV_DIR …"
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtualenv created"
fi

PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"

# ── 3. Upgrade pip + wheel ────────────────────────────────────────────────────
info "Upgrading pip and wheel …"
"$PIP" install --quiet --upgrade pip wheel

# ── 4. Install CPU-only PyTorch ───────────────────────────────────────────────
# Using the PyTorch CPU wheel index keeps the download to ~200 MB instead of
# the full CUDA build (~2 GB), which matters on the Steam Deck's limited SSD.
info "Installing CPU-only PyTorch (this may take a few minutes) …"
"$PIP" install --quiet \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu
ok "PyTorch (CPU) installed"

# ── 5. Install remaining requirements ────────────────────────────────────────
info "Installing remaining requirements from requirements.txt …"
"$PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
ok "All Python dependencies installed"

# ── 6. Create default user config if missing ─────────────────────────────────
CONFIG_DIR="$HOME/.config/dadcam"
CONFIG_FILE="$CONFIG_DIR/dadcam.conf"
if [[ -f "$CONFIG_FILE" ]]; then
    warn "Config already exists at $CONFIG_FILE — leaving it unchanged."
else
    info "Creating default config at $CONFIG_FILE …"
    mkdir -p "$CONFIG_DIR"
    "$VENV_PYTHON" - <<'PYEOF'
import sys
sys.path.insert(0, ".")
from config import ensure_user_config_exists
ensure_user_config_exists()
PYEOF
    ok "Config written: $CONFIG_FILE"
fi

# ── 7. Create log / model directories ─────────────────────────────────────────
mkdir -p "$HOME/.local/share/dadcam/logs"
mkdir -p "$HOME/.local/share/dadcam/models"
ok "Data directories ready"

# ── 8. Make dadcam.py executable ─────────────────────────────────────────────
chmod +x "$SCRIPT_DIR/dadcam.py"

# ── 9. Check for udev rule (installed by --setup) ─────────────────────────────
UDEV_RULE="/etc/udev/rules.d/99-dadcam.rules"
echo ""
echo "─────────────────────────────────────────────"
echo -e "${BOLD}${GREEN}Installation complete!${RESET}"
echo ""
echo "Next step: whitelist your CF drive and install the udev rule."
echo "Insert the CF card now, then run:"
echo ""
echo -e "  ${BOLD}sudo $VENV_PYTHON $SCRIPT_DIR/dadcam.py --setup${RESET}"
echo ""
if [[ -f "$UDEV_RULE" ]]; then
    ok "udev rule already present at $UDEV_RULE"
else
    warn "udev rule not yet installed (run --setup to fix this)"
fi
echo ""
echo "Manual test (without udev, source directory):"
echo -e "  ${BOLD}$VENV_PYTHON $SCRIPT_DIR/dadcam.py --source /path/to/media${RESET}"
echo ""
echo "View logs:"
echo -e "  ${BOLD}journalctl --user -u 'dadcam@*.service'${RESET}"
echo ""
echo -e "${YELLOW}Note:${RESET} SteamOS OS updates reset /etc/udev/rules.d/."
echo "Re-run --setup after a major system update to restore the udev rule."
