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
from pathlib import Path
from typing import Any

import capture
import parse_cdp
import parse_lldp
import rfconfig

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
    """
    interface = str(getattr(rfconfig, "NETWORK_INTERFACE", "eth0")).strip()
    return interface or "eth0"


def get_startup_poll_interval() -> float:
    """
    Return the fast poll interval used during startup before the first
    successful neighbor discovery.
    """
    try:
        value = float(getattr(rfconfig, "STARTUP_POLL_INTERVAL", 1))
    except (TypeError, ValueError):
        value = 1.0

    return max(0.25, value)


def get_steady_poll_interval() -> float:
    """
    Return the normal poll interval used after the first successful
    neighbor discovery.
    """
    try:
        value = float(getattr(rfconfig, "STEADY_POLL_INTERVAL", 10))
    except (TypeError, ValueError):
        value = 10.0

    return max(0.5, value)


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
    Return the normal lldpctl command timeout in seconds.
    """
    try:
        value = float(getattr(rfconfig, "CAPTURE_TIMEOUT", 2))
    except (TypeError, ValueError):
        value = 2.0

    return max(1.0, value)


def should_show_waiting_for_link_screen() -> bool:
    """
    Return True if the app should show a dedicated waiting-for-link screen.
    """
    return bool(getattr(rfconfig, "WAITING_FOR_LINK_SCREEN", True))


def should_show_waiting_for_discovery_screen() -> bool:
    """
    Return True if the app should show a dedicated waiting-for-discovery screen.
    """
    return bool(getattr(rfconfig, "WAITING_FOR_DISCOVERY_SCREEN", True))


def create_display() -> Any:
    """
    Create the selected display backend.
    """
    display_type = get_display_type()

    if display_type == "lcd":
        log.info("Creating LCD display object")
        from display_lcd import LCDDisplay

        return LCDDisplay(
            font_path=getattr(rfconfig, "DISPLAY_FONT_PATH", None),
            rotate_180=getattr(rfconfig, "LCD_ROTATE_180", True),
            clear_on_start=getattr(rfconfig, "LCD_CLEAR_ON_START", True),
            background_color=getattr(rfconfig, "LCD_BACKGROUND_COLOR", (0, 0, 0)),
            text_color=getattr(rfconfig, "LCD_TEXT_COLOR", (255, 255, 255)),
            backlight_brightness=getattr(rfconfig, "LCD_BACKLIGHT_BRIGHTNESS", 100),
        )

    log.info("Creating e-paper display object")
    from display_epaper import EPaperDisplay

    return EPaperDisplay(
        font_path=getattr(rfconfig, "DISPLAY_FONT_PATH", None),
        min_refresh_interval=getattr(rfconfig, "EPAPER_MIN_REFRESH_INTERVAL", 10),
        auto_sleep=getattr(rfconfig, "EPAPER_AUTO_SLEEP", True),
        startup_mode=True,
    )


def initialize_display(display: Any) -> None:
    """
    Initialize the selected display.

    E-paper initialize() now defaults to not clearing on startup, which is what
    we want for faster appliance behavior. LCD initialize() still works with no
    arguments as well.
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


def disable_display_startup_mode(display: Any) -> None:
    """
    Disable startup mode on display backends that support it.

    This is mainly for the e-paper display so it can return to normal
    sleep-friendly behavior after the first real neighbor result is shown.
    """
    if not hasattr(display, "set_startup_mode"):
        return

    try:
        display.set_startup_mode(False)
    except Exception:
        log.exception("Failed to disable display startup mode")


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


def parser_result_score(parsed: dict[str, str]) -> int:
    """
    Return a simple usefulness score for a parsed neighbor record.

    This helps when both parsers found something useful but neither provided
    a high-confidence protocol signal.
    """
    if not parsed:
        return 0

    return sum(
        1
        for key in ("switch_name", "switch_ip", "port", "vlan", "voice_vlan")
        if str(parsed.get(key, "")).strip()
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


def parse_neighbor_data(raw_data: str, interface: str) -> dict[str, str] | None:
    """
    Parse raw lldpctl keyvalue output into one normalized neighbor record.

    Protocol decision rules:
    - Parse both CDP and LLDP from the same raw data.
    - If CDP is high-confidence, use CDP.
    - Else if LLDP is high-confidence, use LLDP.
    - Else if both produced useful but unconfirmed data, choose the richer
      result and label it UNKNOWN.
    - Return None if neither parser found anything useful.

    This avoids falsely labeling generic normalized neighbor data as CDP.
    """
    if not raw_data:
        return None

    cdp_result = parse_cdp.parse_cdp_data(raw_data, interface=interface)
    lldp_result = parse_lldp.parse_lldp_data(raw_data, interface=interface)

    cdp_has_useful = parser_result_has_useful_data(cdp_result)
    lldp_has_useful = parser_result_has_useful_data(lldp_result)

    cdp_is_confident = str(cdp_result.get("source", "")).strip().upper() == "CDP"
    lldp_is_confident = str(lldp_result.get("source", "")).strip().upper() == "LLDP"

    if cdp_is_confident:
        return normalize_neighbor_record(cdp_result, protocol="CDP")

    if lldp_is_confident:
        return normalize_neighbor_record(lldp_result, protocol="LLDP")

    if cdp_has_useful and lldp_has_useful:
        cdp_score = parser_result_score(cdp_result)
        lldp_score = parser_result_score(lldp_result)

        if cdp_score > lldp_score:
            return normalize_neighbor_record(cdp_result, protocol="UNKNOWN")

        return normalize_neighbor_record(lldp_result, protocol="UNKNOWN")

    if lldp_has_useful:
        return normalize_neighbor_record(lldp_result, protocol="UNKNOWN")

    if cdp_has_useful:
        return normalize_neighbor_record(cdp_result, protocol="UNKNOWN")

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
        "Listening for",
        "LLDP/CDP...",
    ]


def build_waiting_for_link_lines(interface: str) -> list[str]:
    """
    Build the screen shown when the Ethernet link is not up yet.
    """
    return [
        "RaspberryFluke",
        "",
        "Waiting for",
        f"link on {interface}",
        "...",
    ]


def build_waiting_for_discovery_lines() -> list[str]:
    """
    Build the screen shown when link is up but no LLDP/CDP data is ready yet.
    """
    return [
        "RaspberryFluke",
        "",
        "Link up",
        "Waiting for",
        "LLDP/CDP...",
    ]


def build_stale_lines() -> list[str]:
    """
    Build the screen shown when discovery data has gone stale.
    """
    return [
        "RaspberryFluke",
        "",
        "No active",
        "neighbor data",
        "",
    ]


def show_lines_if_changed(
    display: Any,
    app_state: RaspberryFlukeState,
    lines: list[str],
    force: bool = False,
) -> bool:
    """
    Show lines only if the resulting text changed.

    Returns:
        True if the display was actually updated.
        False if nothing was drawn.
    """
    display_text = build_display_text(lines)

    if not force and not app_state.display_text_changed(display_text):
        return False

    did_update = display.show_lines(lines, force=force)

    if did_update:
        app_state.set_display_text(display_text)

    return did_update


def show_startup_screen(display: Any, app_state: RaspberryFlukeState) -> None:
    """
    Show a startup message immediately.
    """
    show_lines_if_changed(
        display=display,
        app_state=app_state,
        lines=build_startup_lines(),
        force=True,
    )


def get_link_carrier_state(interface: str) -> bool | None:
    """
    Return Ethernet carrier state for the given interface.

    Returns:
        True  -> link is up
        False -> link is down
        None  -> carrier state could not be determined
    """
    carrier_path = Path("/sys/class/net") / interface / "carrier"

    try:
        raw_value = carrier_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        log.debug("Carrier file not found for interface %s", interface)
        return None
    except Exception as exc:
        log.debug("Could not read carrier state for %s: %s", interface, exc)
        return None

    if raw_value == "1":
        return True

    if raw_value == "0":
        return False

    log.debug("Unexpected carrier value for %s: %r", interface, raw_value)
    return None


def seconds_since_last_success(app_state: RaspberryFlukeState, now_value: float) -> float:
    """
    Return the number of seconds since the last successful discovery.

    If there has never been a success, return infinity.
    """
    if app_state.last_success_time > 0:
        return now_value - app_state.last_success_time

    return float("inf")


def clear_stale_neighbor_if_needed(
    app_state: RaspberryFlukeState,
    display: Any,
    now_value: float,
    discovery_timeout: float,
) -> None:
    """
    Clear active neighbor state and update the screen if the last known
    discovery result is stale.
    """
    if not app_state.has_neighbor():
        return

    age_seconds = seconds_since_last_success(app_state, now_value)

    if age_seconds >= discovery_timeout:
        log.warning(
            "Neighbor data stale for %.2f seconds. Clearing active neighbor state.",
            age_seconds,
        )
        app_state.clear_neighbor()

        show_lines_if_changed(
            display=display,
            app_state=app_state,
            lines=build_stale_lines(),
        )


def run() -> int:
    """
    Start RaspberryFluke and keep it running until shutdown.
    """
    interface = get_network_interface()
    startup_poll_interval = get_startup_poll_interval()
    steady_poll_interval = get_steady_poll_interval()
    discovery_timeout = get_discovery_timeout()
    capture_timeout = get_capture_timeout()

    log.info("Starting RaspberryFluke")
    log.info("DISPLAY_TYPE=%s", get_display_type())
    log.info("NETWORK_INTERFACE=%s", interface)
    log.info("STARTUP_POLL_INTERVAL=%s", startup_poll_interval)
    log.info("STEADY_POLL_INTERVAL=%s", steady_poll_interval)
    log.info("DISCOVERY_TIMEOUT=%s", discovery_timeout)
    log.info("CAPTURE_TIMEOUT=%s", capture_timeout)

    app_state = RaspberryFlukeState()
    display = create_display()

    initialize_display(display)
    show_startup_screen(display, app_state)

    first_success_seen = False

    while not STOP_REQUESTED:
        loop_start = time.monotonic()

        if first_success_seen:
            active_poll_interval = steady_poll_interval
        else:
            active_poll_interval = startup_poll_interval

        try:
            carrier_up = get_link_carrier_state(interface)

            # If link is definitely down, do not waste time calling lldpctl.
            if carrier_up is False:
                log.debug("Link is down on %s", interface)

                if should_show_waiting_for_link_screen():
                    show_lines_if_changed(
                        display=display,
                        app_state=app_state,
                        lines=build_waiting_for_link_lines(interface),
                    )

                clear_stale_neighbor_if_needed(
                    app_state=app_state,
                    display=display,
                    now_value=loop_start,
                    discovery_timeout=discovery_timeout,
                )

            else:
                # During startup mode, do not pass the normal timeout value.
                # capture.py only uses its shorter startup timeout when timeout
                # is omitted.
                if first_success_seen:
                    raw_data = capture.capture_neighbors(
                        interface=interface,
                        timeout=capture_timeout,
                        startup_mode=False,
                    )
                else:
                    raw_data = capture.capture_neighbors(
                        interface=interface,
                        startup_mode=True,
                    )

                parsed_neighbor = parse_neighbor_data(raw_data, interface=interface)

                if parsed_neighbor is not None:
                    neighbor_changed = app_state.neighbor_changed(parsed_neighbor)

                    app_state.update_neighbor(parsed_neighbor)
                    app_state.set_last_success_time(loop_start)

                    display_lines = build_display_lines(parsed_neighbor)

                    if show_lines_if_changed(
                        display=display,
                        app_state=app_state,
                        lines=display_lines,
                    ):
                        log.info(
                            "Display update | protocol=%s | switch=%s | port=%s | vlan=%s | voice=%s | changed=%s",
                            parsed_neighbor["protocol"],
                            parsed_neighbor["switch_name"],
                            parsed_neighbor["port"],
                            parsed_neighbor["vlan"],
                            parsed_neighbor["voice_vlan"],
                            neighbor_changed,
                        )
                    else:
                        log.debug("No display update needed")

                    if not first_success_seen:
                        first_success_seen = True
                        disable_display_startup_mode(display)

                else:
                    age_seconds = seconds_since_last_success(app_state, loop_start)

                    log.debug(
                        "No valid neighbor data this cycle | carrier=%s | seconds since last success: %.2f",
                        carrier_up,
                        age_seconds,
                    )

                    # Before the first success, it helps to tell the user
                    # whether link is up but discovery data is still not ready.
                    if not first_success_seen and carrier_up is True:
                        if should_show_waiting_for_discovery_screen():
                            show_lines_if_changed(
                                display=display,
                                app_state=app_state,
                                lines=build_waiting_for_discovery_lines(),
                            )

                    clear_stale_neighbor_if_needed(
                        app_state=app_state,
                        display=display,
                        now_value=loop_start,
                        discovery_timeout=discovery_timeout,
                    )

        except Exception:
            log.exception("Unhandled error in main loop")

        elapsed = time.monotonic() - loop_start
        sleep_time = max(0.0, active_poll_interval - elapsed)

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