"""
whitelist.py — manage the dadcam drive whitelist.

Whitelist file location: ~/.config/dadcam/whitelist.conf
Format: one entry per line, either:
    UUID=A1B2-C3D4
    SERIAL=LEXAR_CF_12345
Blank lines and lines starting with # are ignored.
"""

from __future__ import annotations

from pathlib import Path

WHITELIST_PATH = Path.home() / ".config" / "dadcam" / "whitelist.conf"


def _ensure_file() -> None:
    WHITELIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not WHITELIST_PATH.exists():
        WHITELIST_PATH.write_text("# dadcam drive whitelist\n", encoding="utf-8")


def load_entries() -> list[dict[str, str]]:
    """Return a list of dicts with keys 'type' ('UUID'|'SERIAL') and 'value'."""
    _ensure_file()
    entries: list[dict[str, str]] = []
    for line in WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().upper()
            value = value.strip()
            if key in ("UUID", "SERIAL") and value:
                entries.append({"type": key, "value": value})
    return entries


def is_whitelisted(uuid: str | None, serial: str | None) -> bool:
    """Return True if either the UUID or serial matches a whitelist entry."""
    for entry in load_entries():
        if entry["type"] == "UUID" and uuid and entry["value"] == uuid:
            return True
        if entry["type"] == "SERIAL" and serial and entry["value"] == serial:
            return True
    return False


def add_entry(entry_type: str, value: str) -> None:
    """Append a new UUID or SERIAL entry if not already present."""
    _ensure_file()
    entry_type = entry_type.upper()
    if entry_type not in ("UUID", "SERIAL"):
        raise ValueError(f"entry_type must be UUID or SERIAL, got: {entry_type!r}")
    # Check for duplicates
    for existing in load_entries():
        if existing["type"] == entry_type and existing["value"] == value:
            return  # already whitelisted
    with open(WHITELIST_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"{entry_type}={value}\n")


def list_whitelist() -> list[str]:
    """Return formatted strings of all whitelist entries."""
    return [f"{e['type']}={e['value']}" for e in load_entries()]


def remove_entry(entry_type: str, value: str) -> bool:
    """Remove a specific entry. Returns True if an entry was removed."""
    _ensure_file()
    entry_type = entry_type.upper()
    original = WHITELIST_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    target = f"{entry_type}={value}\n"
    filtered = [line for line in original if line != target]
    if len(filtered) == len(original):
        return False
    WHITELIST_PATH.write_text("".join(filtered), encoding="utf-8")
    return True
