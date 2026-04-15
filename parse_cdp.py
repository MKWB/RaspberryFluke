"""
parse_cdp.py

This module parses CDP-related fields from raw `lldpctl -f keyvalue` output.

Design goals:
- Accept one raw text blob from capture.py
- Extract only CDP-related data
- Return partial structured data
- Never update application state directly
- Never talk to the display directly

Important:
- This parser should be tolerant of missing fields.
- It should return useful partial results whenever possible.
- Blank values should remain blank instead of causing errors.

Important design note:
- lldpctl often normalizes neighbor data into a shared key space.
- Because of that, useful fields can exist even when the raw output does not
  prove the source was truly CDP.
- This parser is therefore intentionally conservative about labeling a result
  as source='CDP'.
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
    Extract the CDP neighbor device name.

    In lldpctl keyvalue output, CDP device information is often exposed through
    the same lldp.<interface>.chassis.* style keys, so we read those fields
    rather than looking for a totally separate key namespace.
    """
    hostname = get_first_value(
        data,
        [
            build_interface_key(interface, "chassis.name"),
            build_interface_key(interface, "chassis.descr"),
        ],
    )

    return strip_domain(hostname)


def extract_switch_ip(data: dict[str, str], interface: str) -> str:
    """
    Extract the CDP neighbor management IP address.
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
    Extract the remote switch port/interface.

    For CDP-originated neighbor data, the remote interface frequently appears in
    port.descr or port.ifname. We prefer the human-friendly description first.
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
    Extract the primary/data VLAN from CDP-related data.

    Prefer actual VLAN ID fields over looser text fields.
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
    Extract the voice VLAN from CDP-related data when available.

    CDP voice VLAN exposure may vary. We check a few likely candidates and keep
    this tolerant rather than strict.
    """
    voice_vlan = get_first_value(
        data,
        [
            build_interface_key(interface, "cdp.voice-vlan"),
            build_interface_key(interface, "port.policy.voice.vid"),
            build_interface_key(interface, "port.policy.vid"),
            build_interface_key(interface, "med.policy.voice.vid"),
            build_interface_key(interface, "med.policy.vid"),
        ],
    )

    return normalize_vlan_value(voice_vlan)


def has_explicit_cdp_keys(data: dict[str, str], interface: str) -> bool:
    """
    Return True if the parsed key set contains an explicitly CDP-named key.

    This is a stronger signal than simply seeing useful chassis/port values.
    """
    interface = normalize_interface(interface)
    explicit_prefix = f"lldp.{interface}.cdp."

    return any(key.startswith(explicit_prefix) for key in data)


def looks_like_cdp_neighbor(data: dict[str, str], interface: str) -> bool:
    """
    Make a conservative decision about whether the captured neighbor data is CDP.

    High-confidence CDP signals:
    - an explicit protocol/source hint contains "cdp"
    - an explicit CDP-specific key exists for the interface

    We intentionally do NOT treat generic Cisco-looking text alone as proof of
    CDP because the same device may also advertise via LLDP.
    """
    protocol_hint = get_protocol_hint_value(data, interface).lower()

    if "cdp" in protocol_hint:
        return True

    if has_explicit_cdp_keys(data, interface):
        return True

    return False


def parse_cdp_data(raw_output: str, interface: str = "eth0") -> dict[str, str]:
    """
    Parse CDP-related fields from raw `lldpctl -f keyvalue` output.

    Parameters:
        raw_output:
            Raw lldpctl keyvalue output.
        interface:
            Interface name to read from, such as "eth0".

    Returns a dictionary with the shared neighbor schema used by the rest of
    the RaspberryFluke project.

    Returned keys:
        source
        switch_name
        switch_ip
        port
        vlan
        voice_vlan

    Notes:
        - Missing fields are returned as empty strings.
        - This parser only extracts CDP-related information as best as possible
          from lldpctl keyvalue output.
        - Because lldpctl often normalizes neighbor output, true source
          identification is intentionally conservative.
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
        log.debug("parse_cdp_data called with empty raw output")
        return result

    interface = normalize_interface(interface)
    data = parse_keyvalue_output(raw_output)

    if not has_interface_keys(data, interface):
        log.debug("No neighbor keys detected in raw keyvalue output for interface %s", interface)
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

    if any(useful_fields) and looks_like_cdp_neighbor(data, interface):
        result["source"] = "CDP"
        log.debug("Parsed high-confidence CDP data on %s: %s", interface, result)
    elif any(useful_fields):
        log.debug(
            "Useful neighbor fields found on %s, but the data did not provide a high-confidence CDP signal",
            interface,
        )
    else:
        log.debug("No useful CDP fields were extracted on %s", interface)

    return result