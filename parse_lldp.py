"""
parse_lldp.py

This module parses LLDP-related fields from raw `lldpctl -f keyvalue` output.

Design goals:
- Accept one raw text blob from capture.py
- Extract only LLDP-related data
- Return partial structured data
- Never update application state directly
- Never talk to the display directly

Important:
- This parser should be tolerant of missing fields.
- It should return useful partial results whenever possible.
- Blank values should remain blank instead of causing errors.
"""

from __future__ import annotations

import logging


log = logging.getLogger(__name__)


def parse_keyvalue_output(raw_output: str) -> dict[str, str]:
    """
    Convert raw `lldpctl -f keyvalue` text into a dictionary.

    Example input line:
        lldp.eth0.chassis.name=Switch-01

    Returns:
        A dictionary where the key is the full lldpctl key and the value is the
        text after the first "=" character.

    Notes:
        - Lines without "=" are ignored.
        - Leading/trailing whitespace is stripped.
        - Empty values are allowed.
    """
    parsed: dict[str, str] = {}

    if not raw_output:
        return parsed

    for line in raw_output.splitlines():
        line = line.strip()

        if not line or "=" not in line:
            continue

        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()

    return parsed


def normalize_interface(interface: str) -> str:
    """
    Return a clean interface name.

    Falls back to eth0 if the provided value is blank.
    """
    interface = str(interface).strip()
    return interface or "eth0"


def build_interface_key(interface: str, suffix: str) -> str:
    """
    Build one lldpctl key for the selected interface.

    Example:
        interface="eth0", suffix="chassis.name"
        -> "lldp.eth0.chassis.name"
    """
    interface = normalize_interface(interface)
    return f"lldp.{interface}.{suffix}"


def get_first_value(data: dict[str, str], keys: list[str]) -> str:
    """
    Return the first non-empty value found from the provided keys.
    """
    for key in keys:
        value = data.get(key, "").strip()
        if value:
            return value
    return ""


def strip_domain(hostname: str) -> str:
    """
    Strip the domain portion from a hostname.

    Example:
        switch01.example.local -> switch01
    """
    if not hostname:
        return ""

    return hostname.split(".", 1)[0].strip()


def shorten_interface_name(port_name: str) -> str:
    """
    Shorten long interface names to something that fits better on the display.

    Examples:
        GigabitEthernet1/0/24 -> Gi1/0/24
        TenGigabitEthernet1/1/1 -> Te1/1/1
        FastEthernet0/1 -> Fa0/1

    If no known long prefix is found, the original value is returned.
    """
    if not port_name:
        return ""

    replacements = {
        "TwentyFiveGigE": "Twe",
        "TwentyFiveGigabitEthernet": "Twe",
        "FortyGigabitEthernet": "Fo",
        "HundredGigE": "Hu",
        "HundredGigabitEthernet": "Hu",
        "TenGigabitEthernet": "Te",
        "GigabitEthernet": "Gi",
        "FastEthernet": "Fa",
        "Ethernet": "Eth",
        "Port-Channel": "Po",
    }

    for long_name, short_name in replacements.items():
        if port_name.startswith(long_name):
            return port_name.replace(long_name, short_name, 1)

    return port_name


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

    For this project, we usually prefer the PVID because that most closely
    matches the access VLAN idea.
    """
    return get_first_value(
        data,
        [
            build_interface_key(interface, "vlan.pvid"),
            build_interface_key(interface, "vlan.vlan-id"),
        ],
    )


def extract_voice_vlan(data: dict[str, str], interface: str) -> str:
    """
    Extract the voice VLAN from LLDP-MED style policy fields when available.

    The exact key names can vary depending on vendor and lldpd version, so this
    may need adjustment after testing against real device output.
    """
    return get_first_value(
        data,
        [
            build_interface_key(interface, "port.policy.voice.vid"),
            build_interface_key(interface, "port.policy.vid"),
            build_interface_key(interface, "med.policy.voice.vid"),
            build_interface_key(interface, "med.policy.vid"),
        ],
    )


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
        - It does not decide whether LLDP wins over CDP.
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

    interface_prefix = f"lldp.{interface}."
    has_lldp_keys = any(key.startswith(interface_prefix) for key in data)

    if not has_lldp_keys:
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

    if any(useful_fields):
        result["source"] = "LLDP"
        log.debug("Parsed LLDP data on %s: %s", interface, result)
    else:
        log.debug("LLDP keys existed on %s, but no useful LLDP fields were extracted", interface)

    return result