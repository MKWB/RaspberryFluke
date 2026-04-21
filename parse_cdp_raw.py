"""
parse_cdp_raw.py

Parses raw CDP Ethernet frame bytes into structured neighbor data.

CDP (Cisco Discovery Protocol) uses 802.3 Ethernet frames with
LLC/SNAP encapsulation rather than Ethernet II. The CDP payload
contains TLVs with a 2-byte type and 2-byte length (the length
includes the 4-byte type+length header itself).

What this file does:
- Locate the CDP payload inside the 802.3/LLC/SNAP frame
- Parse CDP TLVs
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

from parse_utils import shorten_interface_name, strip_domain, normalize_vlan_value


log = logging.getLogger(__name__)

# CDP frames use 802.3 framing with LLC/SNAP.
# The CDP payload begins after:
#   6 bytes  destination MAC
#   6 bytes  source MAC
#   2 bytes  802.3 length field
#   3 bytes  LLC  (AA AA 03)
#   3 bytes  SNAP OUI (00 00 0C = Cisco)
#   2 bytes  SNAP PID (20 00 = CDP)
# = 22 bytes total before the CDP version byte.
_CDP_PAYLOAD_OFFSET = 22

# CDP header after the payload offset:
#   1 byte  version
#   1 byte  TTL
#   2 bytes checksum
# = 4 bytes, so TLVs begin at _CDP_PAYLOAD_OFFSET + 4
_CDP_HEADER_LEN = 4
_CDP_TLV_OFFSET = _CDP_PAYLOAD_OFFSET + _CDP_HEADER_LEN

# --- CDP TLV type codes ---
_CDP_TYPE_DEVICE_ID   = 0x0001   # Switch hostname
_CDP_TYPE_ADDRESSES   = 0x0002   # Management IP addresses
_CDP_TYPE_PORT_ID     = 0x0003   # Remote switch port
_CDP_TYPE_NATIVE_VLAN = 0x000A   # Native/access VLAN
_CDP_TYPE_VOICE_VLAN  = 0x000E   # VoIP VLAN

# CDP address protocol type for IP (NLPID encapsulation).
_CDP_PROTO_TYPE_NLPID = 0x01
_CDP_PROTO_IP         = b"\xcc"


def _parse_cdp_tlvs(payload: bytes) -> dict[int, list[bytes]]:
    """
    Walk the CDP TLV sequence and collect all TLVs.

    CDP TLV header (4 bytes, big-endian):
        bytes 0-1: Type
        bytes 2-3: Length (includes the 4-byte header itself)

    Returns a dict mapping TLV type -> list of value byte strings.
    """
    tlvs: dict[int, list[bytes]] = {}
    offset = 0

    while offset + 4 <= len(payload):
        tlv_type   = struct.unpack("!H", payload[offset:     offset + 2])[0]
        tlv_length = struct.unpack("!H", payload[offset + 2: offset + 4])[0]

        if tlv_length < 4:
            # Malformed TLV — length must include the 4-byte header.
            log.debug("CDP TLV at offset %d has invalid length %d", offset, tlv_length)
            break

        value_len = tlv_length - 4
        value_end = offset + 4 + value_len

        if value_end > len(payload):
            log.debug("CDP TLV at offset %d overruns payload", offset)
            break

        value   = payload[offset + 4: value_end]
        offset  = value_end

        tlvs.setdefault(tlv_type, []).append(value)

    return tlvs


def _decode_string(value: bytes) -> str:
    """
    Decode a TLV value as a UTF-8 string, falling back to latin-1.
    """
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError:
        return value.decode("latin-1").strip()


def _extract_device_id(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the Device ID TLV — the switch hostname.

    CDP Device ID (type 0x0001) is a plain ASCII string containing
    the switch's configured hostname. Domain suffix is stripped.
    """
    values = tlvs.get(_CDP_TYPE_DEVICE_ID, [])
    if not values:
        return ""

    return strip_domain(_decode_string(values[0]))


def _extract_addresses(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the first IPv4 management address from the Addresses TLV.

    CDP Addresses TLV (type 0x0002) value layout:
        bytes 0-3: number of addresses (4-byte big-endian unsigned int)
        For each address:
            byte 0:   protocol type (1=NLPID, 2=802.2)
            byte 1:   protocol length (P)
            bytes 2..2+P-1: protocol bytes
            bytes 2+P .. 2+P+1: address length (2-byte big-endian)
            bytes 2+P+2 .. end: address bytes

    For IPv4: protocol type=1, protocol=0xCC, address length=4.
    """
    values = tlvs.get(_CDP_TYPE_ADDRESSES, [])
    if not values:
        return ""

    value = values[0]
    if len(value) < 4:
        return ""

    num_addresses = struct.unpack("!I", value[0:4])[0]
    offset        = 4

    for _ in range(num_addresses):
        if offset + 2 > len(value):
            break

        proto_type = value[offset]
        proto_len  = value[offset + 1]
        offset    += 2

        if offset + proto_len > len(value):
            break

        protocol = value[offset: offset + proto_len]
        offset  += proto_len

        if offset + 2 > len(value):
            break

        addr_len = struct.unpack("!H", value[offset: offset + 2])[0]
        offset  += 2

        if offset + addr_len > len(value):
            break

        addr_bytes = value[offset: offset + addr_len]
        offset    += addr_len

        # IPv4: NLPID encapsulation, protocol = 0xCC, 4-byte address.
        if proto_type == _CDP_PROTO_TYPE_NLPID and protocol == _CDP_PROTO_IP and addr_len == 4:
            return socket.inet_ntoa(addr_bytes)

    return ""


def _extract_port_id(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the Port ID TLV — the remote switch port.

    CDP Port ID (type 0x0003) is a plain ASCII string containing the
    port the switch is sending CDP from, which is the port the Pi is
    connected to.
    """
    values = tlvs.get(_CDP_TYPE_PORT_ID, [])
    if not values:
        return ""

    port = _decode_string(values[0])
    return shorten_interface_name(port)


def _extract_native_vlan(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the Native VLAN TLV — the access/data VLAN.

    CDP Native VLAN (type 0x000A) is a 2-byte big-endian unsigned integer
    containing the untagged VLAN ID configured on the port.
    """
    values = tlvs.get(_CDP_TYPE_NATIVE_VLAN, [])
    if not values:
        return ""

    value = values[0]
    if len(value) < 2:
        return ""

    vlan_id = struct.unpack("!H", value[0:2])[0]
    return normalize_vlan_value(str(vlan_id)) if vlan_id > 0 else ""


def _extract_voice_vlan(tlvs: dict[int, list[bytes]]) -> str:
    """
    Extract the VoIP VLAN TLV — the voice VLAN.

    CDP VoIP VLAN (type 0x000E) is commonly a 3-byte value:
        byte 0:   flags/data (we skip this)
        bytes 1-2: VLAN ID (2-byte big-endian unsigned int)

    Some implementations send only 2 bytes (no flag byte). We try
    the 3-byte format first and fall back to the 2-byte format.
    """
    values = tlvs.get(_CDP_TYPE_VOICE_VLAN, [])
    if not values:
        return ""

    value = values[0]

    # 3-byte format: 1 flag byte + 2 byte VLAN ID
    if len(value) >= 3:
        vlan_id = struct.unpack("!H", value[1:3])[0]
        if vlan_id > 0:
            return normalize_vlan_value(str(vlan_id))

    # 2-byte format: 2 byte VLAN ID only
    if len(value) >= 2:
        vlan_id = struct.unpack("!H", value[0:2])[0]
        if vlan_id > 0:
            return normalize_vlan_value(str(vlan_id))

    return ""


def parse_cdp_frame(frame: bytes) -> dict[str, str]:
    """
    Parse a raw CDP Ethernet frame into a structured neighbor record.

    Parameters:
        frame:
            Raw frame bytes as returned by capture_raw.RawCapture.receive_frame().
            The frame must start at the Ethernet header (destination MAC).

    Returns a dict with the shared neighbor schema:
        source      : "CDP"
        switch_name : switch hostname (domain stripped)
        switch_ip   : management IP address
        port        : remote switch port name (shortened)
        vlan        : native/access VLAN ID as a string
        voice_vlan  : voice VLAN ID as a string

    Missing fields are returned as empty strings.
    """
    result = {
        "source":      "CDP",
        "switch_name": "",
        "switch_ip":   "",
        "port":        "",
        "vlan":        "",
        "voice_vlan":  "",
    }

    if len(frame) <= _CDP_TLV_OFFSET:
        log.debug("CDP frame too short to parse (%d bytes)", len(frame))
        return result

    cdp_payload = frame[_CDP_TLV_OFFSET:]
    tlvs        = _parse_cdp_tlvs(cdp_payload)

    if not tlvs:
        log.debug("No TLVs found in CDP frame")
        return result

    result["switch_name"] = _extract_device_id(tlvs)
    result["switch_ip"]   = _extract_addresses(tlvs)
    result["port"]        = _extract_port_id(tlvs)
    result["vlan"]        = _extract_native_vlan(tlvs)
    result["voice_vlan"]  = _extract_voice_vlan(tlvs)

    log.debug("Parsed CDP frame: %s", result)
    return result
