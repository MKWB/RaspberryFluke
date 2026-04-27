"""
trigger.py

Sends protocol trigger frames the moment link comes up to prompt an
immediate neighbor advertisement from the connected switch.

LLDP fast-start:
    IEEE 802.1AB-compliant switches respond to an incoming LLDP frame
    almost immediately with their own LLDP advertisement. This collapses
    the wait from up to 30 seconds down to 1-5 seconds.

CDP trigger:
    Cisco IOS switches respond to an incoming CDP frame on a port by
    immediately sending their own CDP advertisement. This is not formally
    specified in the CDP standard but is consistently implemented across
    IOS versions. This collapses the wait from up to 60 seconds down to
    roughly 5-15 seconds on most Cisco switches.

What this file does:
- Read the Pi's MAC address from the kernel
- Build a minimal valid LLDP Ethernet frame
- Build a minimal valid CDP 802.3/LLC/SNAP frame
- Send both once on link-up via raw AF_PACKET sockets

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
import threading
import time
from pathlib import Path


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLDP constants
# ---------------------------------------------------------------------------

# LLDP uses a reserved multicast destination MAC defined by IEEE 802.1AB.
_LLDP_MULTICAST_MAC = b"\x01\x80\xc2\x00\x00\x0e"

# LLDP Ethertype as a 2-byte big-endian value.
_LLDP_ETHERTYPE = b"\x88\xcc"

# TTL for the LLDP trigger frame in seconds.
# Short TTL keeps the Pi out of the switch neighbor table for long.
_LLDP_TRIGGER_TTL = 30


# ---------------------------------------------------------------------------
# CDP constants
# ---------------------------------------------------------------------------

# CDP uses a Cisco-reserved multicast destination MAC.
_CDP_MULTICAST_MAC = b"\x01\x00\x0c\xcc\xcc\xcc"

# 802.3/LLC/SNAP framing constants used by CDP.
_CDP_LLC      = b"\xaa\xaa\x03"   # DSAP=0xAA, SSAP=0xAA, Control=0x03 (UI)
_CDP_SNAP_OUI = b"\x00\x00\x0c"   # Cisco OUI
_CDP_SNAP_PID = b"\x20\x00"       # CDP protocol identifier

# CDP version and TTL for the trigger frame.
_CDP_VERSION     = 2
_CDP_TRIGGER_TTL = 30              # seconds

# CDP TLV type codes used in the trigger frame.
_CDP_TLV_DEVICE_ID    = 0x0001
_CDP_TLV_ADDRESSES    = 0x0002
_CDP_TLV_PORT_ID      = 0x0003
_CDP_TLV_CAPABILITIES = 0x0004
_CDP_TLV_SW_VERSION   = 0x0005
_CDP_TLV_PLATFORM     = 0x0006

# CDP capability bitmask — report as a Host device.
_CDP_CAPABILITY_HOST  = 0x00000010


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


def _send_raw_frame(interface: str, frame: bytes, label: str) -> bool:
    """
    Open a raw AF_PACKET socket, send one frame, and close it.

    Parameters:
        interface : interface name such as "eth0"
        frame     : complete Ethernet frame bytes to send
        label     : short string used in log messages ("LLDP" or "CDP")

    Returns True on success, False on failure.
    """
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((interface, 0))
        sock.send(frame)
        sock.close()

        log.debug(
            "%s trigger frame sent on %s (%d bytes)",
            label,
            interface,
            len(frame),
        )
        return True

    except PermissionError:
        log.error(
            "Permission denied sending %s trigger on %s. "
            "The service must run as root.",
            label,
            interface,
        )
        return False

    except OSError as exc:
        log.error(
            "Could not send %s trigger on %s: %s",
            label,
            interface,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# LLDP trigger
# ---------------------------------------------------------------------------

def _build_lldp_tlv(tlv_type: int, value: bytes) -> bytes:
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
    sufficient to prompt a switch to respond with its own advertisement.

    TLV details:
        Chassis ID (type 1): subtype 4 (MAC address) + 6-byte MAC
        Port ID    (type 2): subtype 7 (locally assigned) + interface name
        TTL        (type 3): 2-byte unsigned integer
        End        (type 0): zero-length, signals end of LLDPDU
    """
    chassis_id_tlv = _build_lldp_tlv(1, b"\x04" + src_mac)
    port_id_tlv    = _build_lldp_tlv(2, b"\x07" + interface.encode("ascii"))
    ttl_tlv        = _build_lldp_tlv(3, struct.pack("!H", _LLDP_TRIGGER_TTL))
    end_tlv        = _build_lldp_tlv(0, b"")

    lldp_pdu   = chassis_id_tlv + port_id_tlv + ttl_tlv + end_tlv
    eth_header = _LLDP_MULTICAST_MAC + src_mac + _LLDP_ETHERTYPE

    return eth_header + lldp_pdu


def send_lldp_trigger(interface: str) -> bool:
    """
    Send one LLDP fast-start frame on the given interface.

    LLDP-capable switches respond to this frame almost immediately with
    their own LLDP advertisement, collapsing discovery time from up to
    30 seconds down to 1-5 seconds.

    Parameters:
        interface : interface name such as "eth0"

    Returns True if sent, False on failure.
    """
    src_mac = get_interface_mac(interface)

    if src_mac is None:
        log.warning(
            "LLDP trigger skipped: could not read MAC address for %s",
            interface,
        )
        return False

    frame = _build_lldp_frame(src_mac, interface)
    return _send_raw_frame(interface, frame, "LLDP")


# ---------------------------------------------------------------------------
# CDP trigger
# ---------------------------------------------------------------------------

def _cdp_checksum(data: bytes) -> int:
    """
    Compute the standard one's complement checksum used by CDP.

    This is the same algorithm used for IP header checksums.
    The checksum field in the CDP header must be zeroed before calling this.
    """
    # Pad to even length.
    if len(data) % 2:
        data += b"\x00"

    total = 0
    for i in range(0, len(data), 2):
        word   = (data[i] << 8) + data[i + 1]
        total += word

    # Fold 32-bit sum into 16 bits.
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)

    return (~total) & 0xFFFF


def _build_cdp_tlv(tlv_type: int, value: bytes) -> bytes:
    """
    Build one CDP TLV.

    CDP TLV layout:
        Type   : 2 bytes, big-endian
        Length : 2 bytes, big-endian — includes the 4-byte header itself
        Value  : (length - 4) bytes
    """
    length = 4 + len(value)
    return struct.pack("!HH", tlv_type, length) + value


def _build_cdp_addresses_tlv() -> bytes:
    """
    Build a CDP Addresses TLV (type 0x0002) advertising 0.0.0.0.

    Format per address entry:
        Protocol type   : 1 byte  (1 = NLPID)
        Protocol length : 1 byte
        Protocol        : 1 byte  (0xCC = IP)
        Address length  : 2 bytes
        Address         : 4 bytes (IPv4)
    """
    num_addresses = struct.pack("!I", 1)
    ip_entry = (
        b"\x01"             # protocol type: NLPID
        b"\x01"             # protocol length: 1
        b"\xcc"             # NLPID for IP
        + struct.pack("!H", 4)   # address length: 4 bytes
        + b"\x00\x00\x00\x00"   # IP address: 0.0.0.0
    )
    return _build_cdp_tlv(_CDP_TLV_ADDRESSES, num_addresses + ip_entry)


def _build_cdp_frame(src_mac: bytes, interface: str) -> bytes:
    """
    Build a CDP frame using 802.3/LLC/SNAP framing.

    Frame layout:
        Ethernet 802.3 header : dst (6) + src (6) + length (2)
        LLC                   : DSAP (1) + SSAP (1) + Control (1)
        SNAP                  : OUI (3) + PID (2)
        CDP header            : version (1) + TTL (1) + checksum (2)
        CDP TLVs              : Device ID, Addresses, Port ID,
                                Capabilities, Software Version, Platform

    Sending a fuller CDP advertisement (rather than the bare minimum of
    Device ID and Port ID) causes Cisco IOS to treat RaspberryFluke as a
    proper CDP neighbor, which improves the likelihood of an immediate
    response on platforms like the Catalyst 6500.

    The 802.3 length field covers everything from the LLC header onward.
    The CDP checksum covers the CDP header and TLVs only.
    """
    device_id    = b"RaspberryFluke"
    port_id      = interface.encode("ascii")
    sw_version   = b"RaspberryFluke Network Discovery Tool"
    platform     = b"Raspberry Pi Zero 2W"
    capabilities = struct.pack("!I", _CDP_CAPABILITY_HOST)

    device_id_tlv    = _build_cdp_tlv(_CDP_TLV_DEVICE_ID,    device_id)
    addresses_tlv    = _build_cdp_addresses_tlv()
    port_id_tlv      = _build_cdp_tlv(_CDP_TLV_PORT_ID,      port_id)
    capabilities_tlv = _build_cdp_tlv(_CDP_TLV_CAPABILITIES, capabilities)
    sw_version_tlv   = _build_cdp_tlv(_CDP_TLV_SW_VERSION,   sw_version)
    platform_tlv     = _build_cdp_tlv(_CDP_TLV_PLATFORM,     platform)

    tlvs = (
        device_id_tlv
        + addresses_tlv
        + port_id_tlv
        + capabilities_tlv
        + sw_version_tlv
        + platform_tlv
    )

    # Build CDP PDU with checksum zeroed, compute checksum, then rebuild.
    cdp_header_no_checksum = struct.pack("!BBH", _CDP_VERSION, _CDP_TRIGGER_TTL, 0)
    checksum               = _cdp_checksum(cdp_header_no_checksum + tlvs)
    cdp_pdu                = struct.pack("!BBH", _CDP_VERSION, _CDP_TRIGGER_TTL, checksum) + tlvs

    # 802.3 payload = LLC + SNAP + CDP PDU.
    snap_header  = _CDP_LLC + _CDP_SNAP_OUI + _CDP_SNAP_PID
    payload      = snap_header + cdp_pdu
    length_field = struct.pack("!H", len(payload))
    eth_header   = _CDP_MULTICAST_MAC + src_mac + length_field

    return eth_header + payload


def send_cdp_trigger(interface: str) -> bool:
    """
    Send a single CDP trigger frame on the given interface.

    Used for the initial frame sent before the persistent burst loop starts.
    Returns True if sent, False if MAC lookup failed.
    """
    src_mac = get_interface_mac(interface)
    if src_mac is None:
        log.warning("CDP trigger skipped: could not read MAC for %s", interface)
        return False
    frame = _build_cdp_frame(src_mac, interface)
    return _send_raw_frame(interface, frame, "CDP")


def start_persistent_cdp_burst(
    interface:    str,
    cancel_event: threading.Event,
    interval:     float = 0.1,
) -> threading.Thread:
    """
    Start a background thread that sends CDP frames continuously at
    interval seconds apart until cancel_event is set.

    Sending CDP frames persistently throughout the entire discovery window
    rather than just once at the start dramatically increases the chance
    of catching the switch's CDP polling cycle on platforms that do not
    support immediate CDP response (such as the Catalyst 6500 Sup2T).

    Parameters:
        interface    : Ethernet interface name, e.g. "eth0"
        cancel_event : set this to stop the burst thread
        interval     : seconds between frames (default 100ms)

    Returns the running Thread so the caller can join it on cleanup.
    """
    src_mac = get_interface_mac(interface)

    if src_mac is None:
        log.warning("Persistent CDP burst: could not read MAC for %s", interface)
        # Return an already-finished dummy thread.
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        return t

    frame   = _build_cdp_frame(src_mac, interface)
    counter = [0]

    def _burst_loop() -> None:
        while not cancel_event.is_set():
            counter[0] += 1
            _send_raw_frame(interface, frame, f"CDP persistent #{counter[0]}")
            # Use event wait so the thread wakes immediately on cancel.
            cancel_event.wait(timeout=interval)

    t = threading.Thread(target=_burst_loop, name="rf-cdp-burst", daemon=True)
    t.start()
    log.debug(
        "Persistent CDP burst started on %s (%.0fms interval)",
        interface,
        interval * 1000,
    )
    return t


# ---------------------------------------------------------------------------
# Combined trigger — call this from race.py
# ---------------------------------------------------------------------------

def send_all_triggers(interface: str) -> None:
    """
    Send one LLDP frame and one initial CDP frame on the given interface.

    Called after the raw capture socket is confirmed open so no frames
    are missed. The persistent CDP burst is started separately via
    start_persistent_cdp_burst() so it continues throughout the window.

    Failures are logged but do not raise exceptions.
    """
    send_lldp_trigger(interface)
    send_cdp_trigger(interface)
