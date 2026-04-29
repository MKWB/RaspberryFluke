"""
parse_lldp_raw.py

Parses raw LLDP Ethernet frame bytes into structured neighbor data.

LLDP (Link Layer Discovery Protocol, IEEE 802.1AB) frames are structured
as a sequence of TLV (Type-Length-Value) blocks following a standard
Ethernet header. This module reads those TLVs directly from the raw bytes
captured by capture_raw.py.

What this file does:
- Parse the LLDP TLV sequence from raw frame bytes
- Extract switch hostname, management IP, port, VLAN, and voice VLAN
- Return a dict using the shared neighbor schema

What this file does NOT do:
- Capture frames (that is capture_raw.py's job)
- Update application state
- Talk to the display
- Fall back to lldpctl
"""

from __future__ import annotations

import logging
import socket
import struct

from parse_utils import shorten_interface_name, strip_domain, normalize_vlan_value, sanitize_display_string


log = logging.getLogger(__name__)

# Ethernet header is 14 bytes. LLDP PDU starts immediately after.
_ETH_HEADER_LEN = 14

# --- LLDP TLV type codes ---
_TLV_END         = 0
_TLV_CHASSIS_ID  = 1
_TLV_PORT_ID     = 2
_TLV_TTL         = 3
_TLV_PORT_DESCR  = 4
_TLV_SYS_NAME    = 5
_TLV_MGMT_ADDR   = 8
_TLV_ORG_SPEC    = 127

# --- Chassis ID subtypes ---
_CHASSIS_SUBTYPE_MAC     = 4
_CHASSIS_SUBTYPE_NET     = 5

# --- Port ID subtypes ---
_PORT_SUBTYPE_IFALIAS    = 1
_PORT_SUBTYPE_PORT_COMP  = 2
_PORT_SUBTYPE_MAC        = 3
_PORT_SUBTYPE_IFNAME     = 5
_PORT_SUBTYPE_LOCAL      = 7

# --- Management Address subtypes ---
_MGMT_ADDR_IPV4 = 1
_MGMT_ADDR_IPV6 = 2

# --- Organizationally Specific OUIs ---
_OUI_IEEE_8021  = b"\x00\x80\xc2"   # IEEE 802.1
_OUI_TIA_MED    = b"\x00\x12\xbb"   # TIA-1057 (LLDP-MED)

# --- IEEE 802.1 subtypes ---
_8021_SUBTYPE_PVID       = 0x01      # Port VLAN ID

# --- LLDP-MED subtypes ---
_MED_SUBTYPE_NET_POLICY  = 0x02      # Network Policy

# --- LLDP-MED Application Types ---
_MED_APP_VOICE           = 1         # Voice


def _parse_tlvs(lldp_pdu: bytes) -> dict[int, list[bytes]]:
    """
    Walk the LLDP PDU byte sequence and collect all TLVs.

    Returns a dict mapping TLV type -> list of value byte strings.
    Multiple TLVs of the same type are collected in order.

    LLDP TLV header (2 bytes, big-endian):
        Bits 15-9: Type   (7 bits)
        Bits  8-0: Length (9 bits)
    """
    tlvs: dict[int, list[bytes]] = {}
    offset = 0

    while offset + 2 <= len(lldp_pdu):
        header    = struct.unpack("!H", lldp_pdu[offset: offset + 2])[0]
        tlv_type  = (header >> 9) & 0x7F
        tlv_len   = header & 0x1FF
        offset   += 2

        if tlv_type == _TLV_END:
            break

        if offset + tlv_len > len(lldp_pdu):
            log.debug("LLDP TLV length overruns frame at offset %d", offset)
            break

        value   = lldp_pdu[offset: offset + tlv_len]
        offset += tlv_len

        tlvs.setdefault(tlv_type, []).append(value)

    return tlvs


def _decode_string(value: bytes) -> str:
    """
    Decode a TLV value as a UTF-8 string, falling back to latin-1.

    Non-printable and non-ASCII characters are stripped via
    parse_utils.sanitize_display_string after decoding.
    """
    try:
        text = value.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = value.decode("latin-1").strip()

    return sanitize_display_string(text)


def _extract_chassis_id(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract a human-readable chassis identifier.

    Subtypes:
        4 (MAC address)    -> formatted as XX:XX:XX:XX:XX:XX
        5 (network address)-> formatted as dotted-decimal IPv4 if 4 bytes
        1,2,3,6,7          -> decoded as string
    """
    values = tlvs.get(_TLV_CHASSIS_ID, [])
    if not values:
        return ""

    value = values[0]
    if len(value) < 2:
        return ""

    subtype = value[0]
    data    = value[1:]

    if subtype == _CHASSIS_SUBTYPE_MAC and len(data) == 6:
        return ":".join(f"{b:02x}" for b in data)

    if subtype == _CHASSIS_SUBTYPE_NET and len(data) >= 2:
        addr_family = data[0]
        addr_bytes  = data[1:]
        if addr_family == _MGMT_ADDR_IPV4 and len(addr_bytes) == 4:
            return socket.inet_ntoa(addr_bytes)

    return _decode_string(data)


def _extract_system_name(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the System Name TLV (type 5).

    System Name is the cleanest hostname source in LLDP. It is the
    configured hostname of the switch with no subtype prefix to strip.
    """
    values = tlvs.get(_TLV_SYS_NAME, [])
    if not values:
        return ""

    return _decode_string(values[0])


def _extract_switch_name(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the switch hostname from the System Name TLV (type 5).

    System Name is the most reliable and human-readable hostname source
    in LLDP. If it is absent, we return an empty string rather than
    falling back to Chassis ID — a MAC address on the display is
    meaningless to a technician and worse than showing "Unknown".
    """
    name = _extract_system_name(tlvs)
    return strip_domain(name)


def _extract_port(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the remote switch port name.

    Priority:
        1. Port Description (type 4) — often contains the full interface
           name such as "GigabitEthernet1/0/24" on Cisco switches.
        2. Port ID (type 2) subtype 5 (interface name) or 7 (locally
           assigned) — the standardized interface identifier.
        3. Port ID any other string subtype — last resort.

    MAC-address Port IDs are skipped because a MAC is not a useful
    port label for a field technician.
    """
    # Port Description (type 4)
    descr_values = tlvs.get(_TLV_PORT_DESCR, [])
    if descr_values:
        descr = _decode_string(descr_values[0])
        if descr:
            return shorten_interface_name(descr)

    # Port ID (type 2)
    port_values = tlvs.get(_TLV_PORT_ID, [])
    if not port_values:
        return ""

    value   = port_values[0]
    if len(value) < 2:
        return ""

    subtype = value[0]
    data    = value[1:]

    # Skip MAC-based Port IDs — not useful on screen.
    if subtype == _PORT_SUBTYPE_MAC:
        return ""

    port = _decode_string(data)
    return shorten_interface_name(port)


def _extract_management_ip(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the switch management IP address from Management Address TLVs.

    Management Address TLV value layout:
        byte 0:          address string length M (includes the subtype byte)
        byte 1:          address subtype (1=IPv4, 2=IPv6)
        bytes 2..M:      address bytes (M-1 bytes)
        byte M+1:        interface numbering subtype
        bytes M+2..M+5:  interface number (4 bytes)
        byte M+6:        OID string length
        ...              OID

    We prefer IPv4 and return the first IPv4 address found.
    """
    values = tlvs.get(_TLV_MGMT_ADDR, [])

    for value in values:
        if len(value) < 3:
            continue

        addr_string_len = value[0]   # includes the subtype byte
        addr_subtype    = value[1]
        addr_data       = value[2: 1 + addr_string_len]

        if addr_subtype == _MGMT_ADDR_IPV4 and len(addr_data) == 4:
            return socket.inet_ntoa(addr_data)

    # IPv6 fallback — return first address as a hex string if no IPv4 found.
    for value in values:
        if len(value) < 3:
            continue

        addr_string_len = value[0]
        addr_subtype    = value[1]
        addr_data       = value[2: 1 + addr_string_len]

        if addr_subtype == _MGMT_ADDR_IPV6 and len(addr_data) == 16:
            return socket.inet_ntop(socket.AF_INET6, addr_data)

    return ""


def _extract_vlan_and_voice(tlvs: dict[int, list[bytes]]) -> tuple[str, str]:
    """
    Extract the access VLAN and voice VLAN from Organizationally Specific TLVs.

    Access VLAN source:
        IEEE 802.1 (OUI 00:80:C2), subtype 0x01 — Port VLAN ID (PVID)
        This is the untagged VLAN on the port (the data VLAN).

    Voice VLAN source:
        TIA LLDP-MED (OUI 00:12:BB), subtype 0x02 — Network Policy
        Application Type 1 = Voice.

    LLDP-MED Network Policy TLV content (after the 4-byte OUI+subtype prefix):
        byte 4:    Application Type (1 = Voice)
        bytes 5-7: 3-byte big-endian policy field
            bit 23:     Unknown Policy flag
            bit 22:     Tagged flag
            bits 21-20: Reserved
            bits 19-8:  VLAN ID (12 bits)   <- NOTE: shifted 9 from bit 0
            bits 7-5:   L2 Priority
            bits 4-0:   DSCP value (5 bits... sometimes 6)

    Wait, let me recalculate. The 3-byte policy field is 24 bits.
    According to TIA-1057 section 10.2.3:
        Bits [23]:   U (Unknown Policy)
        Bits [22]:   T (Tagged)
        Bits [21]:   X (Reserved)
        Bits [20:9]: VLAN ID (12 bits)
        Bits [8:6]:  L2 Priority (3 bits)
        Bits [5:0]:  DSCP value (6 bits)

    So VLAN ID = (policy_int >> 9) & 0xFFF
    """
    vlan       = ""
    voice_vlan = ""

    org_values = tlvs.get(_TLV_ORG_SPEC, [])

    for value in org_values:
        # Every Org-Specific TLV has at least a 3-byte OUI and 1-byte subtype.
        if len(value) < 4:
            continue

        oui     = value[0:3]
        subtype = value[3]

        # ---- IEEE 802.1 Port VLAN ID ----
        if oui == _OUI_IEEE_8021 and subtype == _8021_SUBTYPE_PVID:
            if len(value) >= 6:
                pvid = struct.unpack("!H", value[4:6])[0]
                if pvid > 0:
                    vlan = normalize_vlan_value(str(pvid))

        # ---- LLDP-MED Network Policy ----
        elif oui == _OUI_TIA_MED and subtype == _MED_SUBTYPE_NET_POLICY:
            if len(value) >= 8:
                app_type   = value[4]
                policy_int = int.from_bytes(value[5:8], "big")
                vlan_id    = (policy_int >> 9) & 0xFFF

                if app_type == _MED_APP_VOICE and vlan_id > 0:
                    voice_vlan = normalize_vlan_value(str(vlan_id))

    return vlan, voice_vlan


def parse_lldp_frame(frame: bytes) -> dict[str, str]:
    """
    Parse a raw LLDP Ethernet frame into a structured neighbor record.

    Parameters:
        frame:
            Raw frame bytes as returned by capture_raw.RawCapture.receive_frame().
            The frame must start at the Ethernet header (destination MAC).

    Returns a dict with the shared neighbor schema:
        source      : "LLDP"
        switch_name : switch hostname (domain stripped)
        switch_ip   : management IP address
        port        : remote switch port name (shortened)
        vlan        : access/data VLAN ID as a string
        voice_vlan  : voice VLAN ID as a string

    Missing fields are returned as empty strings.
    """
    result = {
        "source":      "LLDP",
        "switch_name": "",
        "switch_ip":   "",
        "port":        "",
        "vlan":        "",
        "voice_vlan":  "",
    }

    if len(frame) <= _ETH_HEADER_LEN:
        log.debug("LLDP frame too short to parse (%d bytes)", len(frame))
        return result

    lldp_pdu = frame[_ETH_HEADER_LEN:]
    tlvs     = _parse_tlvs(lldp_pdu)

    if not tlvs:
        log.debug("No TLVs found in LLDP frame")
        return result

    result["switch_name"] = _extract_switch_name(tlvs)
    result["switch_ip"]   = _extract_management_ip(tlvs)
    result["port"]        = _extract_port(tlvs)

    vlan, voice_vlan      = _extract_vlan_and_voice(tlvs)
    result["vlan"]        = vlan
    result["voice_vlan"]  = voice_vlan

    log.debug("Parsed LLDP frame: %s", result)
    return result