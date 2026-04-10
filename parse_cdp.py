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


def get_first_value(data: dict[str, str], keys: list[str]) -> str:
    """
    Return the first non-empty value found from the provided keys.
    """
    for key in keys:
        value = data.get(key, "").strip()
        if value:
            return value
    return ""


def build_interface_key(interface: str, suffix: str) -> str:
    """
    Build one lldpctl key for the selected interface.

    Example:
        interface="eth0", suffix="chassis.name"
        -> "lldp.eth0.chassis.name"
    """
    return f"lldp.{interface}.{suffix}"


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

    CDP commonly exposes management/address information through chassis
    mgmt-ip in lldpctl keyvalue output.
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

    VLAN reporting can vary depending on device and lldpd exposure.
    These are reasonable keys to try first.
    """
    return get_first_value(
        data,
        [
            build_interface_key(interface, "vlan.pvid"),
            build_interface_key(interface, "vlan.vlan-id"),
            build_interface_key(interface, "port.vlan"),
        ],
    )


def extract_voice_vlan(data: dict[str, str], interface: str) -> str:
    """
    Extract the voice VLAN from CDP-related data when available.

    CDP voice VLAN exposure may vary, so this may need tuning once you inspect
    real keyvalue output from your environment.
    """
    return get_first_value(
        data,
        [
            build_interface_key(interface, "port.policy.voice.vid"),
            build_interface_key(interface, "port.policy.vid"),
            build_interface_key(interface, "med.policy.voice.vid"),
            build_interface_key(interface, "med.policy.vid"),
            build_interface_key(interface, "cdp.voice-vlan"),
        ],
    )


def looks_like_cdp_neighbor(data: dict[str, str], interface: str) -> bool:
    """
    Make a best-effort guess about whether the captured neighbor data is CDP.

    Important:
    lldpctl may normalize CDP and LLDP into similar key names, so detecting the
    source is not always perfect from keyvalue output alone.

    We therefore look for hints that often appear with Cisco/CDP neighbors.

    This function is intentionally conservative:
    - if we see an explicit CDP-like hint, return True
    - otherwise, return False
    """
    cdp_hint_keys = [
        build_interface_key(interface, "chassis.descr"),
        build_interface_key(interface, "chassis.name"),
        build_interface_key(interface, "port.descr"),
    ]

    hint_text = " ".join(data.get(key, "") for key in cdp_hint_keys).lower()

    cisco_markers = [
        "cisco",
        "ios",
        "nx-os",
        "ios-xe",
        "ios xe",
        "catalyst",
    ]

    return any(marker in hint_text for marker in cisco_markers)


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
        - It does not decide whether CDP wins over LLDP.
        - Because lldpctl often normalizes neighbor output, true source
          identification is best-effort rather than guaranteed.
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

    interface = str(interface).strip() or "eth0"
    data = parse_keyvalue_output(raw_output)

    interface_prefix = f"lldp.{interface}."
    has_neighbor_keys = any(key.startswith(interface_prefix) for key in data)

    if not has_neighbor_keys:
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
        log.debug("Parsed CDP data on %s: %s", interface, result)
    elif any(useful_fields):
        # Leave source blank if we found useful data but cannot confidently say
        # it is CDP. This prevents falsely labeling normalized neighbor data.
        log.debug(
            "Useful neighbor fields found on %s, but source did not clearly look like CDP",
            interface,
        )
    else:
        log.debug("No useful CDP fields were extracted on %s", interface)

    return result