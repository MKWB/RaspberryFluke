#!/usr/bin/env python3
"""
main.py

Main entry point for RaspberryFluke.

What this file does:
- Load configuration values from rfconfig.py
- Set up logging
- Create the selected display backend
- Create the in-memory runtime state
- Capture raw neighbor data from lldpctl
- Parse that raw data into one normalized neighbor record
- Update the display only when visible text changes
- Keep runtime state in memory only
- Handle graceful shutdown

What this file does NOT do:
- Implement e-paper refresh timing policy
- Implement LCD-specific drawing rules
- Parse raw keyvalue fields itself
- Write runtime state to disk
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

import capture
import parse_cdp
import parse_lldp
import rfconfig
from display_epaper import EPaperDisplay
from display_lcd import LCDDisplay
from state import RaspberryFlukeState


STOP_REQUESTED = False
log = logging.getLogger(__name__)


def setup_logging() -> None:
    """
    Configure logging from rfconfig.py.

    Intended behavior:
    - appliance mode defaults to WARNING
    - dev mode defaults to INFO
    - LOG_LEVEL can override, but invalid values fall back safely
    """
    app_mode = str(getattr(rfconfig, "APP_MODE", "appliance")).strip().lower()
    configured_level_name = str(getattr(rfconfig, "LOG_LEVEL", "")).strip().upper()

    if app_mode == "dev":
        default_level = logging.INFO
    else:
        default_level = logging.WARNING

    if configured_level_name and hasattr(logging, configured_level_name):
        selected_level = getattr(logging, configured_level_name)
    else:
        selected_level = default_level

    logging.basicConfig(
        level=selected_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    log.info("Logging initialized")
    log.info("APP_MODE=%s", app_mode)
    log.info("LOG_LEVEL=%s", logging.getLevelName(selected_level))


def handle_shutdown_signal(signum: int, _frame: Any) -> None:
    """
    Mark the application for clean shutdown.
    """
    global STOP_REQUESTED

    try:
        signal_name = signal.Signals(signum).name
    except Exception:
        signal_name = str(signum)

    log.info("Shutdown signal received: %s", signal_name)
    STOP_REQUESTED = True


def get_display_type() -> str:
    """
    Return the configured display type, or 'epaper' if invalid.
    """
    display_type = str(getattr(rfconfig, "DISPLAY_TYPE", "epaper")).strip().lower()

    if display_type not in ("epaper", "lcd"):
        log.warning("Invalid DISPLAY_TYPE '%s'. Falling back to 'epaper'.", display_type)
        return "epaper"

    return display_type


def get_network_interface() -> str:
    """
    Return the configured network interface.

    For the current RaspberryFluke hardware design, this is expected
    to be a single wired interface such as eth0.
    """
    interface = str(getattr(rfconfig, "NETWORK_INTERFACE", "eth0")).strip()

    if not interface:
        return "eth0"

    return interface


def get_poll_interval() -> float:
    """
    Return main loop poll interval in seconds.
    """
    try:
        value = float(getattr(rfconfig, "POLL_INTERVAL", 10))
    except (TypeError, ValueError):
        value = 10.0

    return max(1.0, value)


def get_discovery_timeout() -> float:
    """
    Return how long active neighbor state can remain valid without a
    successful parse before it is considered stale.
    """
    try:
        value = float(getattr(rfconfig, "DISCOVERY_TIMEOUT", 180))
    except (TypeError, ValueError):
        value = 180.0

    return max(1.0, value)


def get_capture_timeout() -> float:
    """
    Return lldpctl command timeout in seconds.

    Keep this shorter than or equal to the polling interval.
    """
    try:
        value = float(getattr(rfconfig, "CAPTURE_TIMEOUT", 5))
    except (TypeError, ValueError):
        value = 5.0

    return max(1.0, value)


def create_display() -> Any:
    """
    Create the selected display backend.
    """
    display_type = get_display_type()

    if display_type == "lcd":
        log.info("Creating LCD display object")
        return LCDDisplay(
            font_path=getattr(rfconfig, "DISPLAY_FONT_PATH", None),
            rotate_180=getattr(rfconfig, "LCD_ROTATE_180", True),
            clear_on_start=getattr(rfconfig, "LCD_CLEAR_ON_START", True),
            background_color=getattr(rfconfig, "LCD_BACKGROUND_COLOR", (0, 0, 0)),
            text_color=getattr(rfconfig, "LCD_TEXT_COLOR", (255, 255, 255)),
            backlight_brightness=getattr(rfconfig, "LCD_BACKLIGHT_BRIGHTNESS", 100),
        )

    log.info("Creating e-paper display object")
    return EPaperDisplay(
        font_path=getattr(rfconfig, "DISPLAY_FONT_PATH", None),
        min_refresh_interval=getattr(rfconfig, "EPAPER_MIN_REFRESH_INTERVAL", 10),
        auto_sleep=getattr(rfconfig, "EPAPER_AUTO_SLEEP", True),
    )


def initialize_display(display: Any) -> None:
    """
    Initialize the selected display.
    """
    display.initialize()


def shutdown_display(display: Any) -> None:
    """
    Shut the selected display down cleanly.
    """
    try:
        display.shutdown()
    except Exception:
        log.exception("Display shutdown failed")


def first_non_empty(*values: Any, default: str = "") -> str:
    """
    Return the first non-empty value as a stripped string.
    """
    for value in values:
        if value is None:
            continue

        text = str(value).strip()
        if text:
            return text

    return default


def parser_result_has_useful_data(parsed: dict[str, str]) -> bool:
    """
    Return True if the parser result contains at least one useful field.
    """
    if not parsed:
        return False

    return any(
        str(parsed.get(key, "")).strip()
        for key in ("switch_name", "switch_ip", "port", "vlan", "voice_vlan")
    )


def normalize_neighbor_record(parsed: dict[str, str], protocol: str) -> dict[str, str]:
    """
    Convert parser output into one consistent schema.
    """
    return {
        "switch_name": first_non_empty(parsed.get("switch_name"), default="Unknown"),
        "switch_ip": first_non_empty(parsed.get("switch_ip"), default="Unknown"),
        "port": first_non_empty(parsed.get("port"), default="Unknown"),
        "vlan": first_non_empty(parsed.get("vlan"), default="Unknown"),
        "voice_vlan": first_non_empty(parsed.get("voice_vlan"), default="None"),
        "protocol": protocol,
    }


def parse_neighbor_data(raw_data: str) -> dict[str, str] | None:
    """
    Parse raw lldpctl keyvalue output into one normalized neighbor record.

    Policy:
    - Try CDP first because this project is Cisco-heavy.
    - Use CDP only if it returned useful fields.
    - Otherwise try LLDP.
    - Return None if neither parser found anything useful.
    """
    if not raw_data:
        return None

    cdp_result = parse_cdp.parse_cdp_data(raw_data)
    if parser_result_has_useful_data(cdp_result):
        return normalize_neighbor_record(cdp_result, protocol="CDP")

    lldp_result = parse_lldp.parse_lldp_data(raw_data)
    if parser_result_has_useful_data(lldp_result):
        return normalize_neighbor_record(lldp_result, protocol="LLDP")

    return None


def build_display_lines(neighbor: dict[str, str]) -> list[str]:
    """
    Build the exact 5 lines shown on screen.
    """
    return [
        f"SW: {neighbor['switch_name']}",
        f"IP: {neighbor['switch_ip']}",
        f"PORT: {neighbor['port']}",
        f"VLAN: {neighbor['vlan']}",
        f"VOICE: {neighbor['voice_vlan']}",
    ]


def build_display_text(lines: list[str]) -> str:
    """
    Join the display lines into one text block for state comparison.
    """
    return "\n".join(lines)


def build_startup_lines() -> list[str]:
    """
    Build the startup screen.
    """
    return [
        "RaspberryFluke",
        "",
        "Booting...",
        "Waiting for",
        "link...",
    ]


def show_startup_screen(display: Any, app_state: RaspberryFlukeState) -> None:
    """
    Show a startup message immediately.
    """
    startup_lines = build_startup_lines()
    startup_text = build_display_text(startup_lines)

    display.show_lines(startup_lines, force=True)
    app_state.set_display_text(startup_text)


def run() -> int:
    """
    Start RaspberryFluke and keep it running until shutdown.
    """
    interface = get_network_interface()
    poll_interval = get_poll_interval()
    discovery_timeout = get_discovery_timeout()
    capture_timeout = get_capture_timeout()

    log.info("Starting RaspberryFluke")
    log.info("DISPLAY_TYPE=%s", get_display_type())
    log.info("NETWORK_INTERFACE=%s", interface)
    log.info("POLL_INTERVAL=%s", poll_interval)
    log.info("DISCOVERY_TIMEOUT=%s", discovery_timeout)
    log.info("CAPTURE_TIMEOUT=%s", capture_timeout)

    app_state = RaspberryFlukeState()
    display = create_display()

    initialize_display(display)
    show_startup_screen(display, app_state)

    while not STOP_REQUESTED:
        loop_start = time.monotonic()

        try:
            raw_data = capture.capture_neighbors(
                interface=interface,
                timeout=int(capture_timeout),
            )

            parsed_neighbor = parse_neighbor_data(raw_data)

            if parsed_neighbor is not None:
                neighbor_changed = app_state.neighbor_changed(parsed_neighbor)

                app_state.update_neighbor(parsed_neighbor)
                app_state.set_last_success_time(loop_start)

                display_lines = build_display_lines(parsed_neighbor)
                display_text = build_display_text(display_lines)

                if app_state.display_text_changed(display_text):
                    log.info(
                        "Display update | protocol=%s | switch=%s | port=%s | vlan=%s | voice=%s | changed=%s",
                        parsed_neighbor["protocol"],
                        parsed_neighbor["switch_name"],
                        parsed_neighbor["port"],
                        parsed_neighbor["vlan"],
                        parsed_neighbor["voice_vlan"],
                        neighbor_changed,
                    )

                    display.show_lines(display_lines, force=False)
                    app_state.set_display_text(display_text)
                else:
                    log.debug("No display update needed")

            else:
                last_success_time = app_state.last_success_time

                if last_success_time > 0:
                    seconds_since_success = loop_start - last_success_time
                else:
                    seconds_since_success = float("inf")

                log.debug(
                    "No valid neighbor data this cycle | seconds since last success: %.2f",
                    seconds_since_success,
                )

                if seconds_since_success >= discovery_timeout:
                    if app_state.has_neighbor():
                        log.warning(
                            "Neighbor data stale for %.2f seconds. Clearing active neighbor state.",
                            seconds_since_success,
                        )
                        app_state.clear_neighbor()

        except Exception:
            log.exception("Unhandled error in main loop")

        elapsed = time.monotonic() - loop_start
        sleep_time = max(0.0, poll_interval - elapsed)

        if sleep_time > 0:
            time.sleep(sleep_time)

    log.info("Main loop exited. Shutting down display.")
    shutdown_display(display)
    log.info("RaspberryFluke stopped cleanly")
    return 0


def main() -> int:
    """
    Application entry point.
    """
    setup_logging()

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        return run()
    except Exception:
        log.exception("Fatal application error")
        return 1


if __name__ == "__main__":
    sys.exit(main())