"""
main.py

RaspberryFluke — entry point and main loop.

Monitors Ethernet carrier state, runs the discovery race on link-up,
and updates the e-paper display with switch port information.

Discovery is handled entirely by race.py, which runs SNMP and passive
LLDP/CDP capture in parallel and returns the fastest result.

What this file does:
- Initialize the display
- Monitor link state on the configured interface
- Show appropriate screens (waiting, scanning, result, stale)
- Call race.run() on link-up and feed results to the display
- Handle SIGTERM gracefully for clean shutdown

What this file does NOT do:
- Implement any discovery logic (that belongs in discover_*.py and race.py)
- Parse LLDP or CDP frames directly
- Query SNMP directly
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import rfconfig
import race
import trigger

# Configure logging before importing display or discovery modules
# so their loggers inherit the correct level.
_log_level_str = (getattr(rfconfig, "LOG_LEVEL", "WARNING") or "WARNING").upper()
_log_level     = getattr(logging, _log_level_str, logging.WARNING)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)
log.info("Logging initialized at level %s", _log_level_str)


# ============================================================
# -------------------- DISPLAY FACTORY -----------------------
# ============================================================

def _get_display_type() -> str:
    return str(getattr(rfconfig, "DISPLAY_TYPE", "epaper")).lower().strip()


def _create_display():
    """
    Instantiate the correct display driver based on rfconfig.DISPLAY_TYPE.
    """
    display_type = _get_display_type()
    font_path    = getattr(rfconfig, "DISPLAY_FONT_PATH", None)

    if display_type == "epaper":
        from display_epaper import EPaperDisplay
        return EPaperDisplay(
            font_path=font_path,
            min_refresh_interval=int(getattr(rfconfig, "EPAPER_MIN_REFRESH_INTERVAL", 10)),
            auto_sleep=bool(getattr(rfconfig, "EPAPER_AUTO_SLEEP", True)),
            startup_mode=True,
            partial_refresh_limit=int(getattr(rfconfig, "EPAPER_PARTIAL_REFRESH_LIMIT", 8)),
        )

    if display_type == "lcd":
        from display_lcd import LCDDisplay
        return LCDDisplay(
            font_path=font_path,
            rotate_180=bool(getattr(rfconfig, "LCD_ROTATE_180", True)),
            clear_on_start=bool(getattr(rfconfig, "LCD_CLEAR_ON_START", True)),
            background_color=getattr(rfconfig, "LCD_BACKGROUND_COLOR", (0, 0, 0)),
            text_color=getattr(rfconfig, "LCD_TEXT_COLOR", (255, 255, 255)),
            backlight_brightness=int(getattr(rfconfig, "LCD_BACKLIGHT_BRIGHTNESS", 100)),
        )

    log.warning(
        "Unknown DISPLAY_TYPE '%s' — defaulting to epaper.",
        display_type,
    )
    from display_epaper import EPaperDisplay
    return EPaperDisplay(font_path=font_path)


# ============================================================
# -------------------- DISPLAY HELPERS -----------------------
# ============================================================

def _truncate(text: str, max_len: int) -> str:
    text = str(text).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def build_display_lines(neighbor: dict) -> list[str]:
    """Build the 5 body lines for a valid neighbor result."""
    return [
        _truncate(neighbor.get("switch_name", "Unknown"), 22),
        _truncate(neighbor.get("switch_ip",   "Unknown"), 22),
        _truncate(neighbor.get("port",         "Unknown"), 22),
        f"VLAN: {neighbor.get('vlan', 'Unknown')}",
        f"VOICE: {neighbor.get('voice_vlan', 'None')}",
    ]


def build_scanning_lines() -> list[str]:
    """Shown immediately after link-up while discovery is running."""
    return ["", "", "Scanning...", "", ""]


def build_waiting_for_link_lines() -> list[str]:
    """Shown when no Ethernet carrier is detected."""
    return ["", "", "Waiting for", "link...", ""]


def build_stale_lines() -> list[str]:
    """Shown when a previously seen neighbor has not re-advertised."""
    return ["", "No active", "neighbor data.", "", ""]


def _show(display, lines: list[str], force: bool = False, protocol: str = "") -> bool:
    """
    Show lines on whichever display is connected.

    Wraps the display's show_lines method and passes the optional
    protocol string for the top-corner indicator. Falls back gracefully
    if the display driver does not support the protocol parameter.
    """
    try:
        return display.show_lines(lines, force=force, protocol=protocol)
    except TypeError:
        return display.show_lines(lines, force=force)
    except Exception as exc:
        log.error("Display update failed: %s", exc)
        return False


# ============================================================
# -------------------- CARRIER DETECTION ---------------------
# ============================================================

def _read_carrier(interface: str) -> bool:
    """
    Read the Ethernet carrier state from the kernel sysfs.

    Returns True if link is up, False if down or the file cannot be read.
    """
    carrier_path = Path(f"/sys/class/net/{interface}/carrier")
    try:
        return carrier_path.read_text(encoding="ascii").strip() == "1"
    except Exception:
        return False


def _interface_exists(interface: str) -> bool:
    return Path(f"/sys/class/net/{interface}").exists()


# ============================================================
# -------------------- MAIN LOOP -----------------------------
# ============================================================

def run() -> None:
    """
    Main loop. Never returns unless a fatal error occurs or SIGTERM fires.
    """
    # --- Shutdown event (set by SIGTERM/SIGINT handler) ---
    shutdown_event = threading.Event()

    def _sigterm_handler(signum, frame):
        log.info("Signal %d received — initiating graceful shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT,  _sigterm_handler)

    # --- Configuration ---
    interface      = str(getattr(rfconfig, "NETWORK_INTERFACE",   "eth0"))
    disc_timeout   = float(getattr(rfconfig, "DISCOVERY_TIMEOUT", 180.0))
    reveal_delay   = float(getattr(rfconfig, "RESULT_REVEAL_DELAY", 1.5))

    log.info("Starting RaspberryFluke")
    log.info("DISPLAY_TYPE=%s",       _get_display_type())
    log.info("NETWORK_INTERFACE=%s",  interface)
    log.info("DISCOVERY_TIMEOUT=%s",  disc_timeout)
    log.info("RESULT_REVEAL_DELAY=%s", reveal_delay)

    # --- Verify interface exists ---
    if not _interface_exists(interface):
        log.error(
            "Network interface '%s' not found. Check NETWORK_INTERFACE in rfconfig.py.",
            interface,
        )
        sys.exit(1)

    log.info("Network interface '%s' found.", interface)

    # --- Get local MAC for self-frame filtering ---
    local_mac = trigger.get_interface_mac(interface)
    if local_mac is None:
        log.warning("Could not read MAC address for %s — self-frame filtering disabled", interface)

    # --- Create display ---
    log.info("Creating %s display object", _get_display_type())
    display = _create_display()
    display.initialize()

    # --- Initial screen ---
    _show(display, build_waiting_for_link_lines(), force=True)

    # --- Main event loop ---
    while not shutdown_event.is_set():

        carrier = _read_carrier(interface)

        if not carrier:
            _show(display, build_waiting_for_link_lines())
            _wait_or_shutdown(shutdown_event, 0.5)
            continue

        # ---- Link is up — start a discovery session ----
        log.info("Link up on %s — starting discovery", interface)

        # Show "Scanning..." and note when it finishes drawing.
        # The reveal delay timer starts AFTER the display finishes drawing
        # so the user always sees the screen for at least reveal_delay seconds.
        _show(display, build_scanning_lines(), force=True)
        scan_drawn_at = time.monotonic()

        log.debug(
            "Scanning screen shown. Reveal delay: %.2fs after screen draw.",
            reveal_delay,
        )

        # cancel_event is set when link goes down or shutdown fires.
        cancel_event = threading.Event()

        # Run the discovery race in a background thread so we can
        # simultaneously monitor carrier state.
        result_holder: list[Optional[dict]] = [None]

        def _race_thread():
            result_holder[0] = race.run(
                interface=interface,
                local_mac=local_mac,
                cancel_event=cancel_event,
                timeout=disc_timeout,
            )

        race_thread = threading.Thread(
            target=_race_thread,
            name="rf-race",
            daemon=True,
        )
        race_thread.start()

        # Monitor carrier while the race runs.
        while not shutdown_event.is_set():
            if not _read_carrier(interface):
                log.info("Link lost on %s — cancelling discovery", interface)
                cancel_event.set()
                break

            if not race_thread.is_alive():
                break  # Race finished

            _wait_or_shutdown(shutdown_event, 0.5)

        # Wait for the race thread to finish (it will exit quickly once
        # cancel_event is set or when it completes naturally).
        race_thread.join(timeout=5.0)
        cancel_event.set()  # Ensure all threads stop

        if shutdown_event.is_set():
            break

        result = result_holder[0]

        if not _read_carrier(interface):
            # Link went down while we were waiting — go back to top of loop.
            _show(display, build_waiting_for_link_lines(), force=True)
            continue

        if result is None:
            # Discovery timed out with link still up.
            log.info("Discovery timed out on %s", interface)
            _show(display, build_stale_lines(), force=True)
            # Stay on stale screen until link drops.
            while _read_carrier(interface) and not shutdown_event.is_set():
                _wait_or_shutdown(shutdown_event, 1.0)
            continue

        # ---- We have a result ----
        # Enforce the reveal delay: if "Scanning..." has not been
        # visible for at least reveal_delay seconds, wait out the remainder.
        elapsed = time.monotonic() - scan_drawn_at
        if elapsed < reveal_delay:
            remainder = reveal_delay - elapsed
            log.debug("Reveal delay: waiting %.2fs before showing result", remainder)
            _wait_or_shutdown(shutdown_event, remainder)

        if shutdown_event.is_set():
            break

        protocol = result.get("protocol", "")
        display.set_startup_mode(False)
        _show(display, build_display_lines(result), force=True, protocol=protocol)

        log.info(
            "Display updated | protocol=%s switch=%s ip=%s port=%s vlan=%s voice=%s",
            protocol,
            result.get("switch_name"),
            result.get("switch_ip"),
            result.get("port"),
            result.get("vlan"),
            result.get("voice_vlan"),
        )

        # ---- Monitor link while showing result ----
        stale_shown = False
        last_success = time.monotonic()

        while not shutdown_event.is_set():
            if not _read_carrier(interface):
                log.info("Link lost on %s", interface)
                display.set_startup_mode(True)
                _show(display, build_waiting_for_link_lines(), force=True)
                break

            # Show stale warning if we have not received refreshed data
            # within the discovery timeout window.
            stale_elapsed = time.monotonic() - last_success
            if not stale_shown and stale_elapsed > disc_timeout:
                log.info(
                    "Neighbor data stale on %s (%.0fs since last success)",
                    interface,
                    stale_elapsed,
                )
                _show(display, build_stale_lines(), force=True)
                stale_shown = True

            _wait_or_shutdown(shutdown_event, 1.0)

    # ---- Graceful shutdown ----
    log.info("Shutting down RaspberryFluke")
    try:
        display.shutdown()
    except Exception as exc:
        log.debug("Display shutdown error: %s", exc)


def _wait_or_shutdown(shutdown_event: threading.Event, seconds: float) -> None:
    """Sleep for up to `seconds` but wake immediately if shutdown fires."""
    shutdown_event.wait(timeout=seconds)


# ============================================================
# -------------------- ENTRY POINT ---------------------------
# ============================================================

if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Fatal error in main loop")
        sys.exit(1)