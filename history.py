"""
history.py

Port discovery history logging for RaspberryFluke.

Three modes controlled by PORT_HISTORY_MODE in rfconfig.py:

  Mode 0 — Off (default)
    No history is recorded. Zero disk activity. Fully compatible with
    read-only filesystem with no special considerations.

  Mode 1 — Port History
    Records the last PORT_HISTORY_LIMIT port discovery results as JSON
    lines in PORT_HISTORY_PATH/history.jsonl. Each entry contains a
    timestamp plus all discovered switch data. Oldest entries are dropped
    when the limit is reached. Uses atomic writes (temp file + rename) to
    protect against data corruption on hard power cuts.

  Mode 2 — Debug Log
    Records verbose log entries to PORT_HISTORY_PATH/debug.log using a
    rotating file handler (max 5MB per file, 3 rotations kept). This is
    additive — it runs alongside the systemd journal, not instead of it.
    Useful for field troubleshooting without a live SSH session.

What this file does:
  - Read PORT_HISTORY_MODE, PORT_HISTORY_LIMIT, PORT_HISTORY_PATH from rfconfig
  - Provide a single record(result) function that main.py calls
  - Handle all file I/O, rotation, and atomic writes internally
  - Fail silently so a history write error never affects discovery or display

What this file does NOT do:
  - Talk to the display
  - Affect discovery logic
  - Raise exceptions to the caller
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import rfconfig


log = logging.getLogger(__name__)


# ============================================================
# Configuration helpers
# ============================================================

def _get_mode() -> int:
    try:
        return max(0, min(2, int(getattr(rfconfig, "PORT_HISTORY_MODE", 0))))
    except (TypeError, ValueError):
        return 0


def _get_limit() -> int:
    try:
        return max(1, int(getattr(rfconfig, "PORT_HISTORY_LIMIT", 50)))
    except (TypeError, ValueError):
        return 50


def _get_path() -> Path:
    raw = str(getattr(rfconfig, "PORT_HISTORY_PATH", "/data/raspberryfluke")).strip()
    return Path(raw)


# ============================================================
# Path helpers
# ============================================================

def _ensure_dir(path: Path) -> bool:
    """
    Ensure the history directory exists.

    Returns True if the directory is ready to use, False if it could
    not be created (e.g. read-only filesystem without a writable partition).
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        log.warning(
            "History: could not create directory %s: %s. "
            "Is the writable partition mounted?",
            path,
            exc,
        )
        return False


# ============================================================
# Entry builder
# ============================================================

def _build_entry(result: dict) -> dict:
    """
    Build a history entry dict from a discovery result.

    Includes a human-readable timestamp using the system clock.
    The Pi Zero 2W has no RTC — the clock syncs via NTP after boot.
    On networks without internet access the timestamp may be approximate.
    """
    return {
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "protocol":    result.get("protocol",    ""),
        "switch_name": result.get("switch_name", ""),
        "switch_ip":   result.get("switch_ip",   ""),
        "port":        result.get("port",        ""),
        "vlan":        result.get("vlan",        ""),
        "voice_vlan":  result.get("voice_vlan",  ""),
    }


# ============================================================
# Mode 1 — Port History
# ============================================================

def _record_port_history(result: dict, history_dir: Path, limit: int) -> None:
    """
    Append a JSON entry to history.jsonl, enforcing the entry limit.

    Uses an atomic write pattern:
      1. Read existing entries
      2. Append new entry
      3. Enforce limit (drop oldest if needed)
      4. Write to a temp file
      5. Atomically rename temp file over the real file

    If power is cut between steps 4 and 5, the rename never completes
    and the existing file is untouched. If power is cut during step 4,
    only the temp file is affected — the real file is intact.
    """
    history_file = history_dir / "history.jsonl"
    tmp_file     = history_dir / "history.jsonl.tmp"

    # Read existing entries.
    entries: list[dict] = []
    if history_file.exists():
        try:
            for line in history_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("History: could not read %s: %s — starting fresh", history_file, exc)
            entries = []

    # Append new entry.
    entries.append(_build_entry(result))

    # Enforce limit — keep the most recent entries.
    if len(entries) > limit:
        entries = entries[-limit:]

    # Write to temp file then atomically rename.
    try:
        content = "\n".join(json.dumps(e) for e in entries) + "\n"
        tmp_file.write_text(content, encoding="utf-8")
        tmp_file.rename(history_file)
        log.debug(
            "History: recorded entry (%d/%d) | switch=%s port=%s",
            len(entries),
            limit,
            result.get("switch_name"),
            result.get("port"),
        )
    except OSError as exc:
        log.warning("History: could not write %s: %s", history_file, exc)
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass


# ============================================================
# Mode 2 — Debug Log
# ============================================================

# Module-level rotating file handler — created once on first use.
_debug_handler: Optional[logging.handlers.RotatingFileHandler] = None
_debug_logger:  Optional[logging.Logger]                        = None


def _get_debug_logger(history_dir: Path) -> Optional[logging.Logger]:
    """
    Return a logger that writes to PORT_HISTORY_PATH/debug.log.

    The logger is created once and reused. Uses a RotatingFileHandler
    with a 5MB limit and 3 backup files kept.

    Returns None if the log file cannot be created.
    """
    global _debug_handler, _debug_logger

    if _debug_logger is not None:
        return _debug_logger

    debug_log = history_dir / "debug.log"

    try:
        handler = logging.handlers.RotatingFileHandler(
            filename=str(debug_log),
            maxBytes=5 * 1024 * 1024,   # 5MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

        # Create a dedicated logger that does not propagate to the root logger
        # so debug entries go to file only, not to the systemd journal.
        debug_log_logger = logging.getLogger("raspberryfluke.debug_file")
        debug_log_logger.propagate = False
        debug_log_logger.setLevel(logging.DEBUG)
        debug_log_logger.addHandler(handler)

        _debug_handler = handler
        _debug_logger  = debug_log_logger

        log.debug("History: debug log handler initialized at %s", debug_log)
        return _debug_logger

    except OSError as exc:
        log.warning("History: could not create debug log at %s: %s", debug_log, exc)
        return None


def _record_debug_log(result: dict, history_dir: Path) -> None:
    """
    Write a debug log entry for a discovery result.

    Entries are written to PORT_HISTORY_PATH/debug.log via a rotating
    file handler. The file rotates at 5MB and 3 backups are kept.
    """
    logger = _get_debug_logger(history_dir)
    if logger is None:
        return

    entry = _build_entry(result)
    logger.info(
        "Discovery result | protocol=%s switch=%s ip=%s port=%s vlan=%s voice=%s",
        entry["protocol"],
        entry["switch_name"],
        entry["switch_ip"],
        entry["port"],
        entry["vlan"],
        entry["voice_vlan"],
    )


# ============================================================
# Public entry point
# ============================================================

def record(result: dict) -> None:
    """
    Record a discovery result according to PORT_HISTORY_MODE.

    This is the only function main.py needs to call. All mode logic,
    file I/O, and error handling is handled internally. Failures are
    logged as warnings but never propagate to the caller — a history
    write error must never affect discovery or display behavior.

    Parameters:
        result : the neighbor dict returned by race.run(), containing
                 protocol, switch_name, switch_ip, port, vlan, voice_vlan

    Modes:
        0 — Off:          returns immediately, no disk activity
        1 — Port History: appends JSON entry to history.jsonl
        2 — Debug Log:    writes to rotating debug.log file
    """
    mode = _get_mode()

    if mode == 0:
        return

    history_dir = _get_path()

    if not _ensure_dir(history_dir):
        return

    try:
        if mode == 1:
            _record_port_history(result, history_dir, _get_limit())
        elif mode == 2:
            _record_debug_log(result, history_dir)
    except Exception as exc:
        # Belt-and-suspenders catch — specific errors are handled above.
        log.warning("History: unexpected error in record(): %s", exc)
