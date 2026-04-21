"""
trigger.py

Sends an LLDP fast-start frame the moment link comes up.

Modern managed switches respond to an incoming LLDP frame almost
immediately with their own LLDP advertisement. This collapses the
wait time from up to 30 seconds (LLDP) or 60 seconds (CDP) down to
roughly 1-5 seconds in most environments.

What this file does:
- Read the Pi's MAC address from the kernel
- Build a minimal valid LLDP Ethernet frame
- Send it once on link-up via a raw AF_PACKET socket

What this file does NOT do:
- Listen for any frames (that is capture_raw.py's job)
- Parse received data
- Update application state
- Talk to the display
"""

from __future__ import annotations

import logging
import socket
import struct
from pathlib import Path


log = logging.getLogger(__name__)

# LLDP uses a reserved multicast destination MAC defined by IEEE 802.1AB.
_LLDP_MULTICAST_MAC = b"\x01\x80\xc2\x00\x00\x0e"

# LLDP Ethertype as a 2-byte big-endian value.
_LLDP_ETHERTYPE = b"\x88\xcc"

# TTL for the trigger frame in seconds.
# We are not a real LLDP neighbor so we use a short TTL. The switch will
# expire this entry quickly and our Pi will not clutter its neighbor table.
_TRIGGER_TTL_SECONDS = 30


def get_interface_mac(interface: str) -> bytes | None:
    """
    Read the MAC address of the interface from the kernel sysfs.

    Returns the MAC as 6 raw bytes, or None if it cannot be read.

    Reading from /sys/class/net/<iface>/address is the most reliable
    approach and requires no external tools or extra privileges.
    """
    mac_path = Path("/sys/class/net") / interface / "address"

    try:
        mac_str = mac_path.read_text(encoding="utf-8").strip()
        return bytes(int(octet, 16) for octet in mac_str.split(":"))
    except Exception as exc:
        log.error(
            "Could not read MAC address for interface %s: %s",
            interface,
            exc,
        )
        return None


def _build_tlv(tlv_type: int, value: bytes) -> bytes:
    """
    Build one LLDP TLV (Type-Length-Value block).

    LLDP TLV header layout (2 bytes, big-endian):
        Bits 15-9: Type   (7 bits)
        Bits  8-0: Length (9 bits)

    The value follows immediately after the 2-byte header.
    """
    header = ((tlv_type & 0x7F) << 9) | (len(value) & 0x1FF)
    return struct.pack("!H", header) + value


def _build_lldp_frame(src_mac: bytes, interface: str) -> bytes:
    """
    Build a minimal valid LLDP frame.

    IEEE 802.1AB requires exactly three TLVs: Chassis ID, Port ID, and TTL,
    followed by the End TLV. This is the smallest legal LLDP frame and is
    sufficient to prompt a switch to respond.

    TLV details:
        Chassis ID (type 1): subtype 4 (MAC address) + 6-byte MAC
        Port ID    (type 2): subtype 7 (locally assigned) + interface name
        TTL        (type 3): 2-byte unsigned integer
        End        (type 0): zero-length, signals end of LLDPDU
    """
    chassis_id_tlv = _build_tlv(1, b"\x04" + src_mac)
    port_id_tlv    = _build_tlv(2, b"\x07" + interface.encode("ascii"))
    ttl_tlv        = _build_tlv(3, struct.pack("!H", _TRIGGER_TTL_SECONDS))
    end_tlv        = _build_tlv(0, b"")

    lldp_pdu   = chassis_id_tlv + port_id_tlv + ttl_tlv + end_tlv
    eth_header = _LLDP_MULTICAST_MAC + src_mac + _LLDP_ETHERTYPE

    return eth_header + lldp_pdu


def send_lldp_trigger(interface: str) -> bool:
    """
    Send one LLDP fast-start frame on the given interface.

    The switch receives this frame and immediately sends back its own
    LLDP advertisement. Combined with raw socket capture in capture_raw.py,
    this typically produces switch data on screen within 1-5 seconds of
    link-up instead of waiting for the switch's natural advertisement
    interval (up to 30 seconds for LLDP, 60 seconds for CDP).

    Parameters:
        interface:
            Interface name to send on, such as "eth0".

    Returns:
        True if the frame was sent successfully.
        False if sending failed (logged as an error).
    """
    src_mac = get_interface_mac(interface)

    if src_mac is None:
        log.warning(
            "LLDP trigger skipped: could not read MAC address for %s",
            interface,
        )
        return False

    frame = _build_lldp_frame(src_mac, interface)

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((interface, 0))
        sock.send(frame)
        sock.close()

        log.debug(
            "LLDP trigger frame sent on %s (%d bytes)",
            interface,
            len(frame),
        )
        return True

    except PermissionError:
        log.error(
            "Permission denied sending LLDP trigger on %s. "
            "The service must run as root.",
            interface,
        )
        return False

    except OSError as exc:
        log.error(
            "Could not send LLDP trigger on %s: %s",
            interface,
            exc,
        )
        return False
