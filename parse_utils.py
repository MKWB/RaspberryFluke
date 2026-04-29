"""
parse_utils.py

Shared helper functions used by the RaspberryFluke parser modules.

What this file does:
- Strip domain suffixes from hostnames
- Shorten long Cisco-style interface names for the display
- Normalize VLAN text to a plain numeric string
- Sanitize strings to printable ASCII before they reach the display

What this file does NOT do:
- Parse lldpctl keyvalue output (removed with lldpd dependency)
- Parse raw frame bytes (that belongs in parse_lldp_raw and parse_cdp_raw)
- Store application state
- Draw anything on the display
"""

from __future__ import annotations

import re


def sanitize_display_string(text: str) -> str:
    """
    Strip non-printable and non-ASCII characters from a string.

    Some switches send TLV values with embedded control characters,
    vendor-specific encoding, or non-ASCII bytes that render as squares
    or other garbage on the e-paper display. This function keeps only
    printable ASCII characters (0x20 through 0x7E).

    Both parse_lldp_raw and parse_cdp_raw should pass all string values
    through this function before returning them to the caller.

    Example:
        "Gi1/0\\x001" -> "Gi1/0"
        "switch\\x01.local" -> "switch.local"
    """
    if not text:
        return ""
    return "".join(c for c in text if 0x20 <= ord(c) <= 0x7E)


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
    Shorten long interface names to something that fits on the display.

    Examples:
        GigabitEthernet1/0/24        -> Gi1/0/24
        TenGigabitEthernet1/1/1      -> Te1/1/1
        TwentyFiveGigabitEthernet1/1 -> Twe1/1
        HundredGigE1/0/1             -> Hu1/0/1
        FastEthernet0/1              -> Fa0/1
        Port-channel10               -> Po10
        Ethernet1/1                  -> Eth1/1

    If no known long prefix is found, the original value is returned unchanged.

    Note:
        Longer prefixes must appear before shorter ones that share the same
        start so the correct replacement is applied.
    """
    if not port_name:
        return ""

    replacements = [
        ("TwentyFiveGigabitEthernet", "Twe"),
        ("TwentyFiveGigE",            "Twe"),
        ("FortyGigabitEthernet",       "Fo"),
        ("HundredGigabitEthernet",     "Hu"),
        ("HundredGigE",                "Hu"),
        ("TenGigabitEthernet",         "Te"),
        ("GigabitEthernet",            "Gi"),
        ("FastEthernet",               "Fa"),
        ("Port-channel",               "Po"),
        ("Port-Channel",               "Po"),
        ("Ethernet",                   "Eth"),
    ]

    for long_name, short_name in replacements:
        if port_name.startswith(long_name):
            return port_name.replace(long_name, short_name, 1)

    return port_name


def normalize_vlan_value(vlan_value: str) -> str:
    """
    Clean up a VLAN string and extract a plain numeric value.

    Handles formats such as:
        "701"               -> "701"
        "VLAN #701"         -> "701"
        "VLAN #701 (voice)" -> "701"
        "701 (Voice VLAN)"  -> "701"

    If no numeric value can be found, returns the stripped raw text
    so that something reaches the display rather than a blank.
    """
    if vlan_value is None:
        return ""

    vlan_text = str(vlan_value).strip()

    if not vlan_text:
        return ""

    if vlan_text.startswith("VLAN #"):
        vlan_text = vlan_text.replace("VLAN #", "", 1).strip()

    match = re.search(r"\b(\d{1,4})\b", vlan_text)
    if match:
        return match.group(1)

    return vlan_text