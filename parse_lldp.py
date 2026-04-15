"""
parse_lldp.py

This module parses LLDP-related fields from raw `lldpctl -f keyvalue` output.

Design goals:
- Accept one raw text blob from capture.py
- Extract only LLDP-related fields
- Return partial structured data
- Never update application state directly
- Never talk to the display directly

Important:
- This parser should be tolerant of missing fields.
- It should return useful partial results whenever possible.
- Blank values should remain blank instead of causing errors.

Important design note:
- lldpctl often normalizes neighbor output into a shared key space.
- Because of that, useful fields can exist even when the raw output does not
  explicitly prove the source was LLDP.
- This parser stays tolerant for field extraction, while source labeling is
  kept conservative.
"""

from __future__ import annotations

import logging

from parse_utils import (
    build_interface_key,
    get_first_value,
    get_protocol_hint_value,
    has_interface_keys,
    normalize_interface,
    normalize_vlan_value,
    parse_keyvalue_output,
    shorten_interface_name,
    strip_domain,
)


log = logging.getLogger(__name__)


def extract_switch_name(data: dict[str, str], interface: str) -> str:
    """
    Extract the LLDP neighbor hostname.
    """
    hostname = get_first_value(
        data,
        [
            build_interface_key(interface, "chassis.name"),
        ],
    )

    return strip_domain(hostname)


def extract_switch_ip(data: dict[str, str], interface: str) -> str:
    """
    Extract the LLDP neighbor management IP address.

    We return it as switch_ip because that matches the shared schema used by
    main.py and state.py.
    """
    return get_first_value(
        data,
        [
            build_interface_key(interface, "chassis.mgmt-ip"),
            build_interface_key(interface, "chassis.ip"),
        ],
    )


def extract_port(data: dict[str, str], interface: str) -> str:
    """
    Extract the LLDP port/interface identifier.

    We prefer description first because it is often friendlier on switch gear,
    then fall back to interface name or raw port ID.
    """
    port = get_first_value(
        data,
        [
            build_interface_key(interface, "port.descr"),
            build_interface_key(interface, "port.ifname"),
            build_interface_key(interface, "port.id"),
        ],
    )

    return shorten_interface_name(port)


def extract_vlan(data: dict[str, str], interface: str) -> str:
    """
    Extract the primary/data VLAN from LLDP information.

    Prefer actual VLAN ID fields, but allow a port.vlan fallback so we do not
    throw away usable normalized data.
    """
    vlan_value = get_first_value(
        data,
        [
            build_interface_key(interface, "vlan.vlan-id"),
            build_interface_key(interface, "port.vlan"),
            build_interface_key(interface, "vlan"),
        ],
    )

    return normalize_vlan_value(vlan_value)


def extract_voice_vlan(data: dict[str, str], interface: str) -> str:
    """
    Extract the voice VLAN from LLDP-MED style policy fields when available.

    The exact key names can vary depending on vendor and lldpd version, so this
    remains intentionally tolerant.
    """
    voice_vlan = get_first_value(
        data,
        [
            build_interface_key(interface, "port.policy.voice.vid"),
            build_interface_key(interface, "port.policy.vid"),
            build_interface_key(interface, "med.policy.voice.vid"),
            build_interface_key(interface, "med.policy.vid"),
        ],
    )

    return normalize_vlan_value(voice_vlan)


def looks_like_lldp_neighbor(data: dict[str, str], interface: str) -> bool:
    """
    Make a conservative decision about whether the captured neighbor data is LLDP.

    High-confidence LLDP signal:
    - an explicit protocol/source hint contains "lldp"

    If that signal is missing, we still extract fields, but we do not
    automatically stamp the result as source='LLDP' here.
    """
    protocol_hint = get_protocol_hint_value(data, interface).lower()
    return "lldp" in protocol_hint


def parse_lldp_data(raw_output: str, interface: str = "eth0") -> dict[str, str]:
    """
    Parse LLDP-related fields from raw `lldpctl -f keyvalue` output.

    Parameters:
        raw_output:
            Raw lldpctl keyvalue output.
        interface:
            Interface name to read from, such as "eth0".

    Returns a dictionary with the shared neighbor schema used by the rest of the
    RaspberryFluke project.

    Returned keys:
        source
        switch_name
        switch_ip
        port
        vlan
        voice_vlan

    Notes:
        - Missing fields are returned as empty strings.
        - This parser only extracts LLDP-related information.
        - Because lldpctl often normalizes output, source labeling is kept
          conservative while field extraction remains tolerant.
    """
    result = {
        "source": "",
        "switch_name": "",
        "switch_ip": "",
        "port": "",
        "vlan": "",
        "voice_vlan": "",
    }

    if not raw_output:
        log.debug("parse_lldp_data called with empty raw output")
        return result

    interface = normalize_interface(interface)
    data = parse_keyvalue_output(raw_output)

    if not has_interface_keys(data, interface):
        log.debug("No LLDP keys detected in raw keyvalue output for interface %s", interface)
        return result

    result["switch_name"] = extract_switch_name(data, interface)
    result["switch_ip"] = extract_switch_ip(data, interface)
    result["port"] = extract_port(data, interface)
    result["vlan"] = extract_vlan(data, interface)
    result["voice_vlan"] = extract_voice_vlan(data, interface)

    useful_fields = [
        result["switch_name"],
        result["switch_ip"],
        result["port"],
        result["vlan"],
        result["voice_vlan"],
    ]

    if any(useful_fields) and looks_like_lldp_neighbor(data, interface):
        result["source"] = "LLDP"
        log.debug("Parsed high-confidence LLDP data on %s: %s", interface, result)
    elif any(useful_fields):
        log.debug(
            "Useful neighbor fields found on %s, but the data did not provide a high-confidence LLDP signal",
            interface,
        )
    else:
        log.debug("LLDP keys existed on %s, but no useful LLDP fields were extracted", interface)

    return result