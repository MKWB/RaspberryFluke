"""
discover_passive.py

Passive LLDP and CDP neighbor discovery via raw Ethernet frame capture.

Listens on a raw AF_PACKET socket for LLDP and CDP frames broadcast by the
connected switch. Returns the first valid result as a normalized neighbor dict.

This is the fallback discovery path used when SNMP is unavailable or slow.
On LLDP-capable switches it typically returns results in 3-8 seconds.
On CDP-only Cisco switches it may take 20-60 seconds depending on where
the switch is in its advertisement cycle.

What this file does:
- Open a raw AF_PACKET socket via capture_raw.RawCapture
- Listen for incoming LLDP and CDP frames
- Filter out self-generated trigger frames by source MAC
- Parse frames using parse_lldp_raw and parse_cdp_raw
- Return the first valid result as a normalized dict

What this file does NOT do:
- Send trigger frames (race.py calls trigger.send_all_triggers before starting threads)
- Talk to the display
- Manage session state
- Call SNMP or perform any active network queries
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import capture_raw
import parse_cdp_raw
import parse_lldp_raw
import parse_utils
import rfconfig


log = logging.getLogger(__name__)

# Fields that must be non-empty for a parsed result to be considered useful.
_REQUIRED_FIELDS = ("switch_name",)


def _get_receive_timeout() -> float:
    try:
        return max(0.5, float(getattr(rfconfig, "RAW_RECEIVE_TIMEOUT", 2.0)))
    except (TypeError, ValueError):
        return 2.0


def _is_self_generated(frame: bytes, local_mac: Optional[bytes]) -> bool:
    """
    Return True if the frame was sent by this device.

    Both the LLDP and CDP trigger frames use the interface MAC as the
    Ethernet source address (bytes 6-12 of the frame header). Filtering
    by source MAC is sufficient to discard our own outgoing frames that
    loop back through the raw socket.
    """
    if local_mac is None or len(frame) < 12:
        return False
    return frame[6:12] == local_mac


def _parse_frame(protocol: str, frame: bytes) -> Optional[dict]:
    """
    Parse a raw LLDP or CDP frame using the appropriate parser.

    Returns a raw parsed dict or None if parsing failed or returned
    no useful data.
    """
    try:
        if protocol == "lldp":
            return parse_lldp_raw.parse_lldp_frame(frame)
        if protocol == "cdp":
            return parse_cdp_raw.parse_cdp_frame(frame)
    except Exception as exc:
        log.debug("Frame parse error (%s): %s", protocol.upper(), exc)
    return None


def _has_useful_data(parsed: Optional[dict]) -> bool:
    """Return True if the parsed result contains at least a switch name."""
    if not parsed:
        return False
    for field in _REQUIRED_FIELDS:
        value = parsed.get(field, "")
        if not value or str(value).strip() in ("", "Unknown", "None"):
            return False
    return True


def _normalize(parsed: dict, protocol_label: str) -> dict:
    """
    Convert a raw parser dict into the standard neighbor result format.

    All fields are strings. Missing or empty values become "".
    The interface name shortener is applied to the port field.
    """
    return {
        "protocol":    protocol_label,
        "switch_name": parse_utils.strip_domain(
                           str(parsed.get("switch_name", "")).strip()
                       ),
        "switch_ip":   str(parsed.get("switch_ip",   "")).strip(),
        "port":        parse_utils.shorten_interface_name(
                           str(parsed.get("port", "")).strip()
                       ),
        "vlan":        parse_utils.normalize_vlan_value(
                           str(parsed.get("vlan", ""))
                       ),
        "voice_vlan":  parse_utils.normalize_vlan_value(
                           str(parsed.get("voice_vlan", ""))
                       ),
    }


def discover(
    interface:   str,
    local_mac:   Optional[bytes],
    cancel_event: threading.Event,
    timeout:     float = 180.0,
) -> Optional[dict]:
    """
    Listen for LLDP or CDP frames and return the first valid result.

    This is a blocking call intended to run inside a thread managed by
    race.py. It returns as soon as a valid frame is received, or None
    if the cancel_event is set or the timeout expires.

    Parameters:
        interface    : Ethernet interface name, e.g. "eth0"
        local_mac    : 6-byte MAC address used to filter self-generated frames
        cancel_event : set by race.py when another discovery method wins
        timeout      : maximum seconds to wait before returning None

    Returns:
        Normalized neighbor dict or None.
    """
    receive_timeout = _get_receive_timeout()
    deadline        = time.monotonic() + timeout

    raw_cap = capture_raw.RawCapture(interface)
    if not raw_cap.open():
        log.error("Passive discovery: could not open raw socket on %s", interface)
        return None

    try:
        log.debug("Passive discovery started on %s", interface)

        while not cancel_event.is_set() and time.monotonic() < deadline:
            # Block for up to receive_timeout seconds waiting for a frame.
            # Short timeout keeps the cancel_event check responsive.
            protocol, frame = raw_cap.receive_frame(timeout=receive_timeout)

            if protocol is None or frame is None:
                # Timeout elapsed with no matching frame — check cancel and retry.
                continue

            if _is_self_generated(frame, local_mac):
                log.debug(
                    "Passive: ignoring self-generated %s frame on %s",
                    protocol.upper(),
                    interface,
                )
                continue

            parsed = _parse_frame(protocol, frame)

            if not _has_useful_data(parsed):
                log.debug(
                    "Passive: %s frame on %s yielded no useful data",
                    protocol.upper(),
                    interface,
                )
                continue

            label  = "LLDP" if protocol == "lldp" else "CDP"
            result = _normalize(parsed, label)

            log.info(
                "Passive discovery success | protocol=%s switch=%s port=%s "
                "vlan=%s voice=%s",
                result["protocol"],
                result["switch_name"],
                result["port"],
                result["vlan"],
                result["voice_vlan"],
            )
            return result

        log.debug(
            "Passive discovery ended on %s (%s)",
            interface,
            "cancelled" if cancel_event.is_set() else "timeout",
        )
        return None

    finally:
        raw_cap.close()
