"""
parse_utils.py

Shared helper functions used by the RaspberryFluke parser modules.

What this file does:
- Parse lldpctl keyvalue output into a dictionary
- Normalize interface names
- Build interface-specific lldpctl keys
- Return the first non-empty value from a list of keys
- Strip domains from hostnames
- Shorten long interface names for display
- Normalize VLAN text
- Check whether raw parsed data includes keys for one interface

What this file does NOT do:
- Decide whether a neighbor is really CDP or really LLDP
- Store application state
- Draw anything on the display
"""

from __future__ import annotations


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


def get_protocol_hint_value(data: dict[str, str], interface: str) -> str:
    """
    Return the first protocol/source hint value we can find for the interface.

    Important:
    The exact key names exposed by lldpctl can vary a bit by version and
    environment, so this is intentionally best-effort.

    This helper does NOT decide which protocol is correct.
    It only returns the first protocol-ish hint string if one exists.
    """
    return get_first_value(
        data,
        [
            build_interface_key(interface, "via"),
            build_interface_key(interface, "protocol"),
            build_interface_key(interface, "neighbor.via"),
            build_interface_key(interface, "neighbor.protocol"),
        ],
    )


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

    replacements = [
        ("TwentyFiveGigabitEthernet", "Twe"),
        ("TwentyFiveGigE", "Twe"),
        ("FortyGigabitEthernet", "Fo"),
        ("HundredGigabitEthernet", "Hu"),
        ("HundredGigE", "Hu"),
        ("TenGigabitEthernet", "Te"),
        ("GigabitEthernet", "Gi"),
        ("FastEthernet", "Fa"),
        ("Port-channel", "Po"),
        ("Port-Channel", "Po"),
        ("Ethernet", "Eth"),
    ]

    for long_name, short_name in replacements:
        if port_name.startswith(long_name):
            return port_name.replace(long_name, short_name, 1)

    return port_name


def normalize_vlan_value(vlan_value: str) -> str:
    """
    Clean up VLAN text returned by lldpctl.

    Example:
        VLAN #701 -> 701
    """
    if vlan_value is None:
        return ""

    vlan_text = str(vlan_value).strip()

    if not vlan_text:
        return ""

    if vlan_text.startswith("VLAN #"):
        vlan_text = vlan_text.replace("VLAN #", "", 1).strip()

    return vlan_text


def has_interface_keys(data: dict[str, str], interface: str) -> bool:
    """
    Return True if the parsed keyvalue output contains any keys for the
    requested interface.
    """
    interface = normalize_interface(interface)
    interface_prefix = f"lldp.{interface}."
    return any(key.startswith(interface_prefix) for key in data)