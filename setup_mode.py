"""
setup_mode.py — interactive setup wizard.

Requires root (sudo dadcam --setup).

Steps:
  1. Confirm root
  2. Watch for CF drive insertion via pyudev
  3. Read UUID (blkid) and serial (udev attributes)
  4. User confirms whitelisting
  5. Write to ~/.config/dadcam/whitelist.conf
  6. steamos-readonly disable (Steam Deck only)
  7. Write /etc/udev/rules.d/99-dadcam.rules
  8. steamos-readonly enable
  9. udevadm control --reload-rules && udevadm trigger

Note: the systemd user service unit and loginctl linger are set up by
install.sh (as the deck user, no read-only root changes required).  This
wizard only handles the udev rule, which is the only piece that lives in /etc.

This wizard is idempotent: re-running it adds the new drive to the whitelist
and re-writes the udev rule (which is identical regardless of which drive was
whitelisted, since filtering is done in Python at runtime).
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

try:
    import pyudev  # type: ignore[import-untyped]
except ImportError:
    pyudev = None  # type: ignore[assignment]

try:
    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    from rich.panel import Panel
except ImportError:
    Console = None  # type: ignore[assignment,misc]

import whitelist as wl

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-dadcam.rules")
SCRIPT_PATH = Path(__file__).resolve()
VENV_PYTHON = SCRIPT_PATH.parent / "venv" / "bin" / "python"

# The udev rule content.  Note: filtering by UUID/serial is done at runtime
# by Python (whitelist check), so a single generic rule works for all drives.
UDEV_RULE_TEMPLATE = """\
# dadcam — trigger processing when a CF card is inserted
# Managed by: dadcam --setup
ACTION=="add", SUBSYSTEM=="block", ENV{{DEVTYPE}}=="partition", \\
  TAG+="systemd", \\
  ENV{{SYSTEMD_USER_WANTS}}="dadcam@%k.service"
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _console() -> "Console":
    if Console is not None:
        return Console()
    # Minimal fallback
    class _FallbackConsole:
        def print(self, *args, **kwargs):  # noqa: A003
            print(*args)
        def rule(self, *args, **kwargs):
            print("─" * 60)
    return _FallbackConsole()  # type: ignore[return-value]


con = _console()


def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def _get_uuid(devnode: str) -> str | None:
    """Use blkid to get the filesystem UUID of a partition."""
    try:
        result = _run(
            ["blkid", "-s", "UUID", "-o", "value", devnode],
            capture=True,
            check=False,
        )
        return result.stdout.strip() or None
    except FileNotFoundError:
        return None


def _get_serial(device: "pyudev.Device") -> str | None:  # type: ignore[name-defined]
    for attr in ("ID_SERIAL", "ID_SERIAL_SHORT", "ID_MODEL_ID"):
        val = device.get(attr)
        if val:
            return val.strip()
    return None


def _detect_real_user() -> tuple[str, int]:
    """
    Return (username, uid) of the real user who called sudo.
    Falls back to 'deck' if SUDO_USER is not set.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            info = pwd.getpwnam(sudo_user)
            return info.pw_name, info.pw_uid
        except KeyError:
            pass
    # Default to 'deck' on Steam Deck
    try:
        info = pwd.getpwnam("deck")
        return "deck", info.pw_uid
    except KeyError:
        return "root", 0


def _steamos_readonly(*, enable: bool) -> None:
    action = "enable" if enable else "disable"
    try:
        _run(["steamos-readonly", action], check=False)
    except FileNotFoundError:
        pass  # not a Steam Deck or steamos-readonly not on PATH


# ---------------------------------------------------------------------------
# Prompt helpers (graceful fallback if rich is not installed)
# ---------------------------------------------------------------------------


def _prompt(msg: str, default: str = "") -> str:
    if Console is not None:
        return Prompt.ask(msg, default=default)
    val = input(f"{msg} [{default}]: ").strip()
    return val if val else default


def _confirm(msg: str, default: bool = True) -> bool:
    if Console is not None:
        return Confirm.ask(msg, default=default)
    suffix = "[Y/n]" if default else "[y/N]"
    val = input(f"{msg} {suffix}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


# ---------------------------------------------------------------------------
# Main setup entry point
# ---------------------------------------------------------------------------


def run_setup() -> None:
    """Top-level entry point for --setup mode."""

    # ── 1. Root check ───────────────────────────────────────────────────
    if os.geteuid() != 0:
        con.print("[red]ERROR:[/red] --setup requires root. Run with sudo.")
        sys.exit(1)

    if pyudev is None:
        con.print("[red]ERROR:[/red] pyudev is not installed.  Run: pip install pyudev")
        sys.exit(1)

    real_user, real_uid = _detect_real_user()
    try:
        real_gid = pwd.getpwnam(real_user).pw_gid
    except KeyError:
        real_gid = real_uid

    con.print(Panel(
        "[bold cyan]dadcam setup wizard[/bold cyan]\n"
        "This will whitelist a CF card reader device (any card inserted into it\n"
        "will be processed automatically) and install the udev rule.",
        title="dadcam",
        expand=False,
    ))
    con.print(f"Running as: [bold]{real_user}[/bold] (uid={real_uid})\n")

    # ── 2. Watch for device insertion ───────────────────────────────────
    con.print("Please [bold yellow]insert a CF card[/bold yellow] to detect the reader device …")

    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="block", device_type="partition")

    device_info: dict | None = None
    start_time = time.time()
    timeout = 120  # seconds

    monitor.start()
    for device in iter(monitor.poll, None):
        if device.action != "add":
            continue
        if (time.time() - start_time) > timeout:
            con.print("[red]Timeout waiting for device insertion.[/red]")
            sys.exit(1)

        devnode: str = device.device_node or ""
        uuid = _get_uuid(devnode)
        serial = _get_serial(device)
        vendor = device.get("ID_VENDOR", "")
        model = device.get("ID_MODEL", "")

        con.rule("Device detected")
        con.print(f"  Device : [bold]{devnode}[/bold]")
        con.print(f"  UUID   : {uuid or '(none)'}")
        con.print(f"  Serial : {serial or '(none)'}")
        con.print(f"  Vendor : {vendor or '(none)'}")
        con.print(f"  Model  : {model or '(none)'}")
        con.print()

        if not serial:
            if not uuid:
                con.print("[yellow]Warning:[/yellow] No serial or UUID found for this device.")
                con.print("Cannot whitelist without at least one identifier. Try another device.")
                continue
            con.print(
                "[yellow]Warning:[/yellow] No reader serial found. "
                "Only this specific card (by UUID) can be whitelisted."
            )

        if not _confirm("Whitelist this device?  (Any CF card in this reader will be processed)", default=True):
            con.print("Skipping. Re-insert a different drive or Ctrl-C to abort.")
            continue

        device_info = {
            "devnode": devnode,
            "uuid": uuid,
            "serial": serial,
        }
        break

    if not device_info:
        con.print("[red]No device whitelisted. Aborting.[/red]")
        sys.exit(1)

    # ── 3. Write whitelist ──────────────────────────────────────────────
    # Whitelist lives in the real user's home; write it as that user
    wl_path = Path(f"/home/{real_user}") / ".config" / "dadcam" / "whitelist.conf"
    wl_path.parent.mkdir(parents=True, exist_ok=True)
    if not wl_path.exists():
        wl_path.write_text("# dadcam drive whitelist\n", encoding="utf-8")
    os.chown(wl_path.parent, real_uid, real_gid)

    # Temporarily set HOME so whitelist.py uses the right path
    original_home = os.environ.get("HOME")
    os.environ["HOME"] = str(Path(f"/home/{real_user}"))

    if device_info["serial"]:
        # Device-level: any CF card in this reader will be processed
        wl.add_entry("SERIAL", device_info["serial"])
        con.print(f"[green]✓[/green] Added SERIAL={device_info['serial']} to whitelist (any card in this reader)")
    elif device_info["uuid"]:
        # Fallback: no reader serial available; whitelist this specific card by UUID
        wl.add_entry("UUID", device_info["uuid"])
        con.print(
            f"[green]✓[/green] Added UUID={device_info['uuid']} to whitelist "
            "(this specific card only — reader serial unavailable)"
        )

    if original_home:
        os.environ["HOME"] = original_home

    os.chown(wl_path, real_uid, real_gid)

    # ── 4. Write udev rule (requires writable /etc) ─────────────────────
    con.print("\nWriting udev rule …")
    _steamos_readonly(enable=False)
    try:
        UDEV_RULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UDEV_RULE_PATH.write_text(UDEV_RULE_TEMPLATE, encoding="utf-8")
        con.print(f"[green]✓[/green] Wrote {UDEV_RULE_PATH}")
    finally:
        _steamos_readonly(enable=True)

    # ── 5. Reload udev ───────────────────────────────────────────────────
    try:
        _run(["udevadm", "control", "--reload-rules"])
        _run(["udevadm", "trigger"])
        con.print("[green]✓[/green] udev rules reloaded")
    except subprocess.CalledProcessError as exc:
        con.print(f"[yellow]Warning:[/yellow] udevadm reload failed: {exc}")

    # ── 6. Done ──────────────────────────────────────────────────────────
    con.print()
    con.rule("[green]Setup complete[/green]")
    con.print(
        "Insert [bold]any CF card[/bold] into the whitelisted reader at any time and "
        "dadcam will run automatically.\n"
        f"Logs: [bold]journalctl --user -u 'dadcam@*.service'[/bold]"
    )
    con.print(
        "\n[yellow]Note:[/yellow] SteamOS updates may reset /etc/udev/rules.d/. "
        "Re-run [bold]sudo dadcam --setup[/bold] after a major OS update to restore the udev rule."
    )
