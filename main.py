"""
main.py

Main entry point for RaspberryFluke.

What this file does:
- Load configuration from rfconfig.py
- Set up logging
- Validate startup configuration
- Create the selected display backend
- Create the in-memory runtime state
- Send an LLDP trigger frame on link-up to prompt an immediate switch response
- Capture raw LLDP and CDP frames directly from the wire
- Parse frames immediately as they arrive
- Hold the first valid result behind a short loading screen per link session
- Update the display when content changes
- Handle graceful shutdown

What this file does NOT do:
- Implement e-paper refresh timing policy
- Implement LCD-specific drawing rules
- Parse raw frame bytes itself
- Write runtime state to disk
- Call lldpctl or depend on lldpd
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

import capture_raw
import parse_cdp_raw
import parse_lldp_raw
import rfconfig
import trigger

from state import RaspberryFlukeState


log = logging.getLogger(__name__)

# Shutdown event. Set by the signal handler to stop the main loop cleanly.
_stop_event = threading.Event()

# Error backoff settings for the main loop.
_MAX_CONSECUTIVE_ERRORS = 5
_BASE_BACKOFF_SECONDS   = 5.0
_MAX_BACKOFF_SECONDS    = 60.0


# ============================================================
# -------------------- SETUP ---------------------------------
# ============================================================

def setup_logging() -> None:
    """
    Configure logging from rfconfig.py.

    LOG_LEVEL controls verbosity.
    "WARNING" minimizes SD card writes during normal appliance operation.
    "INFO" or "DEBUG" are useful when troubleshooting.
    """
    configured_level_name = str(
        getattr(rfconfig, "LOG_LEVEL", "WARNING")
    ).strip().upper()

    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    if configured_level_name not in valid_levels:
        configured_level_name = "WARNING"

    selected_level = getattr(logging, configured_level_name, logging.WARNING)

    logging.basicConfig(
        level=selected_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    log.info("Logging initialized at level %s", configured_level_name)


def handle_shutdown_signal(signum: int, _frame: object) -> None:
    """
    Set the shutdown event when SIGTERM or SIGINT is received.

    Setting _stop_event wakes any select() or event.wait() call in the
    main loop so shutdown happens within seconds rather than waiting
    for the next receive timeout.
    """
    try:
        signal_name = signal.Signals(signum).name
    except Exception:
        signal_name = str(signum)

    log.info("Shutdown signal received: %s", signal_name)
    _stop_event.set()


# ============================================================
# -------------------- CONFIG HELPERS ------------------------
# ============================================================

def get_display_type() -> str:
    display_type = str(getattr(rfconfig, "DISPLAY_TYPE", "epaper")).strip().lower()

    if display_type not in ("epaper", "lcd"):
        log.warning(
            "Invalid DISPLAY_TYPE '%s' in rfconfig.py. Falling back to 'epaper'.",
            display_type,
        )
        return "epaper"

    return display_type


def get_network_interface() -> str:
    interface = str(getattr(rfconfig, "NETWORK_INTERFACE", "eth0")).strip()
    return interface or "eth0"


def get_raw_receive_timeout() -> float:
    try:
        value = float(getattr(rfconfig, "RAW_RECEIVE_TIMEOUT", 2.0))
    except (TypeError, ValueError):
        value = 2.0
    return max(0.5, value)


def get_discovery_timeout() -> float:
    try:
        value = float(getattr(rfconfig, "DISCOVERY_TIMEOUT", 180))
    except (TypeError, ValueError):
        value = 180.0
    return max(1.0, value)





def get_result_reveal_delay() -> float:
    """
    Return how long to hold the loading screen before showing the
    first valid neighbor result for a new link session.

    Defaults to 2.0 seconds if the setting does not exist in rfconfig.py.
    """
    try:
        value = float(getattr(rfconfig, "RESULT_REVEAL_DELAY", 1.5))
    except (TypeError, ValueError):
        value = 1.5

    return max(0.0, value)


# ============================================================
# -------------------- STARTUP VALIDATION --------------------
# ============================================================

def validate_startup_config(interface: str) -> None:
    """
    Check that the configured network interface exists on this system.

    Logs a warning if not found so the operator knows immediately
    rather than seeing mysterious empty output during operation.
    The main loop is not aborted — carrier detection handles
    missing interfaces gracefully.
    """
    net_path = Path("/sys/class/net") / interface

    if not net_path.exists():
        log.warning(
            "Network interface '%s' was not found in /sys/class/net. "
            "Check NETWORK_INTERFACE in rfconfig.py.",
            interface,
        )
    else:
        log.info("Network interface '%s' found.", interface)


# ============================================================
# -------------------- DISPLAY CREATION ----------------------
# ============================================================

def create_display() -> object:
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
        partial_refresh_limit=getattr(rfconfig, "EPAPER_PARTIAL_REFRESH_LIMIT", 8),
        startup_mode=True,
    )


def initialize_display(display: object) -> None:
    display.initialize()


def shutdown_display(display: object) -> None:
    try:
        display.shutdown()
    except Exception:
        log.exception("Display shutdown failed")


def disable_display_startup_mode(display: object) -> None:
    """
    Disable startup mode after the first real neighbor is shown.

    The e-paper display can then sleep between updates as normal
    instead of staying awake for fast early-boot screen changes.
    """
    if not hasattr(display, "set_startup_mode"):
        return
    try:
        display.set_startup_mode(False)
    except Exception:
        log.exception("Failed to disable display startup mode")


def enable_display_startup_mode(display: object) -> None:
    """
    Re-enable startup mode when a neighbor goes stale.

    Returns the display to fast-update behavior while waiting
    for the next port connection.
    """
    if not hasattr(display, "set_startup_mode"):
        return
    try:
        display.set_startup_mode(True)
    except Exception:
        log.exception("Failed to re-enable display startup mode")


# ============================================================
# -------------------- SELF-FRAME FILTERING ------------------
# ============================================================

def is_self_generated_lldp_frame(frame: bytes, local_mac: bytes | None) -> bool:
    """
    Return True if the LLDP frame was sent by RaspberryFluke itself.

    The app sends a one-shot LLDP trigger on link-up. Because raw AF_PACKET
    sockets can see locally transmitted frames, the capture socket may read
    that outgoing trigger back before the switch's real response arrives.

    The trigger frame uses the interface MAC as the Ethernet source MAC, so
    comparing bytes 6:12 of the Ethernet header against the local interface
    MAC is enough to identify and discard it.
    """
    if local_mac is None:
        return False

    if len(frame) < 12:
        return False

    return frame[6:12] == local_mac


# ============================================================
# -------------------- PARSING -------------------------------
# ============================================================

def first_non_empty(*values: object, default: str = "") -> str:
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

    Used as a sanity check to avoid displaying a completely empty record
    in the rare case where a frame was captured but parsing yielded nothing.
    """
    if not parsed:
        return False

    return any(
        str(parsed.get(key, "")).strip()
        for key in ("switch_name", "switch_ip", "port", "vlan", "voice_vlan")
    )


def normalize_neighbor_record(parsed: dict[str, str], protocol: str) -> dict[str, str]:
    """
    Convert parser output into one consistent schema for main.py and state.py.
    """
    return {
        "switch_name": first_non_empty(parsed.get("switch_name"), default="Unknown"),
        "switch_ip":   first_non_empty(parsed.get("switch_ip"),   default="Unknown"),
        "port":        first_non_empty(parsed.get("port"),        default="Unknown"),
        "vlan":        first_non_empty(parsed.get("vlan"),        default="Unknown"),
        "voice_vlan":  first_non_empty(parsed.get("voice_vlan"),  default="None"),
        "protocol":    protocol,
    }


def parse_neighbor_data(
    protocol: str,
    frame: bytes,
) -> dict[str, str] | None:
    """
    Parse a raw frame into a normalized neighbor record.

    Because capture_raw.py identifies the frame type before returning it,
    we know exactly which parser to call. There is no dual-parse or
    protocol confidence scoring — the raw socket tells us the answer.

    Parameters:
        protocol : "lldp" or "cdp" as returned by RawCapture.receive_frame()
        frame    : raw Ethernet frame bytes

    Returns a normalized neighbor dict, or None if parsing yielded no
    useful data (malformed frame, unexpected format, etc.)
    """
    if protocol == "lldp":
        parsed = parse_lldp_raw.parse_lldp_frame(frame)
        label  = "LLDP"
    elif protocol == "cdp":
        parsed = parse_cdp_raw.parse_cdp_frame(frame)
        label  = "CDP"
    else:
        log.debug("Unknown protocol '%s' — skipping frame", protocol)
        return None

    if not parser_result_has_useful_data(parsed):
        log.debug("Parser returned no useful data for %s frame", protocol.upper())
        return None

    return normalize_neighbor_record(parsed, protocol=label)


# ============================================================
# -------------------- DISPLAY CONTENT -----------------------
# ============================================================

def build_display_lines(neighbor: dict[str, str]) -> list[str]:
    """
    Build the 5 body lines shown below the RaspberryFluke header.
    """
    return [
        f"SW: {neighbor.get('switch_name', 'Unknown')}",
        f"IP: {neighbor.get('switch_ip',   'Unknown')}",
        f"PORT: {neighbor.get('port',      'Unknown')}",
        f"VLAN: {neighbor.get('vlan',      'Unknown')}",
        f"VOICE: {neighbor.get('voice_vlan', 'None')}",
    ]


def build_loading_lines() -> list[str]:
    return ["", "", "Waiting for LLDP/CDP...", "", ""]


def build_waiting_for_link_lines(interface: str) -> list[str]:
    return ["", "Waiting for", f"link on {interface}", "...", ""]


def build_stale_lines() -> list[str]:
    return ["", "No active", "neighbor data", "", ""]


def show_lines_if_changed(
    display: object,
    lines: list[str],
    force: bool = False,
) -> bool:
    """
    Show lines on the display.

    The display module tracks its own last-shown content and skips the
    hardware update if nothing changed, so this simply delegates.
    """
    return display.show_lines(lines, force=force)


def reveal_pending_neighbor_if_ready(
    *,
    display: object,
    pending_neighbor: dict[str, str] | None,
    reveal_deadline: float,
    first_success_seen: bool,
) -> tuple[bool, bool, dict[str, str] | None]:
    """
    Show the pending neighbor once the reveal deadline has passed.

    Returns:
        revealed_now:
            True if the pending neighbor was just shown on screen.
        updated_first_success_seen:
            Updated value for first_success_seen.
        updated_pending_neighbor:
            None after reveal, otherwise the original pending neighbor.
    """
    if pending_neighbor is None:
        return False, first_success_seen, pending_neighbor

    if time.monotonic() < reveal_deadline:
        return False, first_success_seen, pending_neighbor

    display_lines = build_display_lines(pending_neighbor)

    if show_lines_if_changed(display=display, lines=display_lines):
        log.info(
            "Display updated after loading delay | protocol=%s | switch=%s | "
            "port=%s | vlan=%s | voice=%s",
            pending_neighbor["protocol"],
            pending_neighbor["switch_name"],
            pending_neighbor["port"],
            pending_neighbor["vlan"],
            pending_neighbor["voice_vlan"],
        )
    else:
        log.debug("Loading delay expired but display content was unchanged")

    if not first_success_seen:
        first_success_seen = True
        disable_display_startup_mode(display)

    return True, first_success_seen, None


# ============================================================
# -------------------- LINK / TIMING HELPERS -----------------
# ============================================================

def get_link_carrier_state(interface: str) -> bool | None:
    """
    Return Ethernet carrier state for the given interface.

    Reads /sys/class/net/<interface>/carrier which is updated by the
    kernel driver in real time. This is faster and more reliable than
    any subprocess call.

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


def seconds_since_last_success(
    app_state: RaspberryFlukeState,
    now_value: float,
) -> float:
    """
    Return seconds since the last successful frame parse.

    Returns infinity if there has never been a success so that timeout
    comparisons work correctly before any data is seen.
    """
    if app_state.last_success_time > 0:
        return now_value - app_state.last_success_time
    return float("inf")


def clear_stale_neighbor_if_needed(
    app_state: RaspberryFlukeState,
    display: object,
    now_value: float,
    discovery_timeout: float,
) -> bool:
    """
    Clear active neighbor state if it has gone stale and update the screen.

    Returns True if the neighbor was cleared, False if nothing changed.
    """
    if not app_state.has_neighbor():
        return False

    age = seconds_since_last_success(app_state, now_value)

    if age >= discovery_timeout:
        log.warning(
            "Neighbor data stale for %.1f seconds. Clearing state.",
            age,
        )
        app_state.clear_neighbor()
        show_lines_if_changed(display=display, lines=build_stale_lines())
        return True

    return False


# ============================================================
# -------------------- MAIN SERVICE LOOP ---------------------
# ============================================================

def run() -> int:
    """
    Start RaspberryFluke and keep it running until shutdown.

    Loop model:
    - When link is down: poll carrier every second, show waiting screen.
    - When link comes up:
        1. Open raw capture socket.
        2. Show a loading screen for the configured reveal delay.
        3. Send LLDP trigger frame to prompt an immediate switch response.
        4. Capture and parse frames immediately in the background.
        5. Hold the first valid result until the loading delay expires.
        6. Show the newest valid result once the delay completes.

    This keeps real discovery speed fast while making the user-facing
    transition more obvious.
    """
    interface               = get_network_interface()
    receive_timeout         = get_raw_receive_timeout()
    discovery_timeout       = get_discovery_timeout()
    reveal_delay            = get_result_reveal_delay()
    local_mac               = trigger.get_interface_mac(interface)

    log.info("Starting RaspberryFluke")
    log.info("DISPLAY_TYPE=%s",        get_display_type())
    log.info("NETWORK_INTERFACE=%s",   interface)
    log.info("RAW_RECEIVE_TIMEOUT=%s", receive_timeout)
    log.info("DISCOVERY_TIMEOUT=%s",   discovery_timeout)
    log.info("RESULT_REVEAL_DELAY=%s", reveal_delay)

    validate_startup_config(interface)

    app_state = RaspberryFlukeState()
    display   = create_display()

    initialize_display(display)

    # --- Session state ---
    # raw_cap:                open RawCapture instance while link is up, None otherwise
    # trigger_sent:           True after the LLDP trigger has been sent for this link session
    # first_success_seen:     True after the first neighbor has been displayed
    # session_loading_active: True while the loading screen delay window is active
    # session_reveal_deadline: monotonic time when pending data may be shown
    # pending_neighbor:       newest valid neighbor captured during the loading delay
    raw_cap                 = None
    trigger_sent            = False
    first_success_seen      = False
    session_loading_active  = False
    session_reveal_deadline = 0.0
    pending_neighbor        = None
    consecutive_errors      = 0

    while not _stop_event.is_set():
        loop_start = time.monotonic()

        try:
            carrier_up = get_link_carrier_state(interface)

            # --------------------------------------------------------
            # Link is definitely down
            # --------------------------------------------------------
            if carrier_up is False:
                log.debug("Link is down on %s", interface)

                # Close the raw socket if it was open.
                if raw_cap is not None:
                    raw_cap.close()
                    raw_cap = None

                trigger_sent            = False
                session_loading_active  = False
                session_reveal_deadline = 0.0
                pending_neighbor        = None

                # Return to startup mode and fast polling if we had data.
                if first_success_seen:
                    first_success_seen = False
                    enable_display_startup_mode(display)

                show_lines_if_changed(
                        display=display,
                        lines=build_waiting_for_link_lines(interface),
                    )

                clear_stale_neighbor_if_needed(
                    app_state=app_state,
                    display=display,
                    now_value=loop_start,
                    discovery_timeout=discovery_timeout,
                )

                # Wait before checking carrier again. Using _stop_event.wait()
                # means a shutdown signal wakes us immediately.
                _stop_event.wait(timeout=1.0)
                continue

            # --------------------------------------------------------
            # Link is up (or indeterminate — proceed and let the socket handle it)
            # --------------------------------------------------------

            # Open the raw capture socket once per link session.
            if raw_cap is None:
                raw_cap = capture_raw.RawCapture(interface)

                if not raw_cap.open():
                    log.error(
                        "Could not open raw capture socket on %s. "
                        "Retrying in 5 seconds.",
                        interface,
                    )
                    raw_cap = None
                    _stop_event.wait(timeout=5.0)
                    continue

                show_lines_if_changed(
                        display=display,
                        lines=build_loading_lines(),
                        force=True,
                    )
                    # Set the deadline AFTER show_lines_if_changed returns.
                    # The e-paper refresh takes ~3 seconds, so the timer only
                    # starts once "Waiting for LLDP/CDP..." is physically on screen.
                    # This guarantees the user sees it for at least
                    # reveal_delay additional seconds before data replaces it.
                    session_loading_active  = True
                    session_reveal_deadline = time.monotonic() + reveal_delay
                    pending_neighbor        = None
                    log.debug(
                        "Waiting screen shown for new session on %s. "
                        "Result reveal deadline in %.2f seconds.",
                        interface,
                        reveal_delay,
                    )

            # Send the LLDP trigger once per link session.
            # This prompts the switch to advertise immediately rather than
            # waiting for its natural advertisement interval.
            if not trigger_sent:
                trigger.send_all_triggers(interface)
                trigger_sent = True
                log.debug("LLDP + CDP triggers sent on %s", interface)

            # If a pending result exists during the loading window, reduce the
            # next receive timeout so the screen flips close to the reveal deadline.
            effective_receive_timeout = receive_timeout

            if session_loading_active and pending_neighbor is not None:
                remaining = session_reveal_deadline - time.monotonic()
                effective_receive_timeout = max(0.05, min(receive_timeout, remaining))

            # Block and wait for an LLDP or CDP frame.
            protocol, frame = raw_cap.receive_frame(timeout=effective_receive_timeout)

            if protocol is not None and frame is not None:
                if protocol == "lldp" and is_self_generated_lldp_frame(frame, local_mac):
                    log.debug("Ignoring self-generated LLDP trigger frame on %s", interface)
                    continue

                parsed = parse_neighbor_data(protocol, frame)

                if parsed is not None:
                    neighbor_changed = app_state.neighbor_changed(parsed)
                    app_state.update_neighbor(parsed)
                    app_state.set_last_success_time(loop_start)

                    if session_loading_active:
                        pending_neighbor = parsed
                        log.debug(
                            "Valid neighbor captured during loading delay | "
                            "protocol=%s | switch=%s | port=%s | vlan=%s | voice=%s",
                            parsed["protocol"],
                            parsed["switch_name"],
                            parsed["port"],
                            parsed["vlan"],
                            parsed["voice_vlan"],
                        )

                        revealed_now, first_success_seen, pending_neighbor = (
                            reveal_pending_neighbor_if_ready(
                                display=display,
                                pending_neighbor=pending_neighbor,
                                reveal_deadline=session_reveal_deadline,
                                first_success_seen=first_success_seen,
                            )
                        )

                        if revealed_now:
                            session_loading_active  = False
                            session_reveal_deadline = 0.0
                    else:
                        display_lines = build_display_lines(parsed)

                        if show_lines_if_changed(display=display, lines=display_lines):
                            log.info(
                                "Display updated | protocol=%s | switch=%s | "
                                "port=%s | vlan=%s | voice=%s | changed=%s",
                                parsed["protocol"],
                                parsed["switch_name"],
                                parsed["port"],
                                parsed["vlan"],
                                parsed["voice_vlan"],
                                neighbor_changed,
                            )
                        else:
                            log.debug("Frame received but display content unchanged")

                        if not first_success_seen:
                            first_success_seen = True
                            disable_display_startup_mode(display)

            else:
                # No frame arrived within receive_timeout.
                if session_loading_active:
                    revealed_now, first_success_seen, pending_neighbor = (
                        reveal_pending_neighbor_if_ready(
                            display=display,
                            pending_neighbor=pending_neighbor,
                            reveal_deadline=session_reveal_deadline,
                            first_success_seen=first_success_seen,
                        )
                    )

                    if revealed_now:
                        session_loading_active  = False
                        session_reveal_deadline = 0.0
                        consecutive_errors = 0
                        continue

                age = seconds_since_last_success(app_state, loop_start)
                log.debug(
                    "No frame received on %s | seconds since last success: %.1f",
                    interface,
                    age,
                )

                was_cleared = clear_stale_neighbor_if_needed(
                    app_state=app_state,
                    display=display,
                    now_value=loop_start,
                    discovery_timeout=discovery_timeout,
                )

                if was_cleared and first_success_seen:
                    first_success_seen = False
                    enable_display_startup_mode(display)

            # Clean cycle — reset error counter.
            consecutive_errors = 0

        except Exception:
            consecutive_errors += 1

            if consecutive_errors <= _MAX_CONSECUTIVE_ERRORS:
                log.exception(
                    "Unhandled error in main loop (consecutive=%d)",
                    consecutive_errors,
                )
            elif consecutive_errors == _MAX_CONSECUTIVE_ERRORS + 1:
                log.error(
                    "Error rate too high after %d consecutive failures. "
                    "Further errors suppressed. Check hardware and logs.",
                    consecutive_errors,
                )

            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                backoff = min(
                    _BASE_BACKOFF_SECONDS * consecutive_errors,
                    _MAX_BACKOFF_SECONDS,
                )
                log.debug("Backing off %.1f seconds after repeated errors", backoff)
                _stop_event.wait(timeout=backoff)

    # ----------------------------------------------------------------
    # Shutdown
    # ----------------------------------------------------------------
    if raw_cap is not None:
        raw_cap.close()

    log.info("Main loop exited. Shutting down display.")
    shutdown_display(display)
    log.info("RaspberryFluke stopped cleanly.")
    return 0


def main() -> int:
    """
    Application entry point.
    """
    setup_logging()

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT,  handle_shutdown_signal)

    try:
        return run()
    except Exception:
        log.exception("Fatal application error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
