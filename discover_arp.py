"""
discover_arp.py

Gateway IP discovery via ARP packet observation.

Used as a fallback by discover_snmp.py when DHCP is unavailable (e.g. the
port is statically assigned at the switch level). Even without a DHCP lease,
the wire is not silent — other devices on the VLAN are sending ARP traffic,
and the switch itself may send ARP probes.

By listening for a short time we can extract source IPs from ARP packets
and use them as SNMP query candidates.

What this file does:
- Open a raw AF_PACKET socket
- Listen for ARP packets (EtherType 0x0806)
- Extract sender IP addresses from ARP sender protocol address field
- Filter out link-local (169.254.x.x) and obviously non-gateway addresses
- Return the most frequently seen IP as the most likely gateway

What this file does NOT do:
- Perform any SNMP queries
- Parse LLDP or CDP frames
- Manage application state
- Talk to the display
"""

from __future__ import annotations

import logging
import select
import socket
import struct
import threading
import time
from collections import Counter
from typing import Optional


log = logging.getLogger(__name__)

# EtherType for ARP is 0x0806.
_ARP_ETHERTYPE = 0x0806

# Minimum valid Ethernet frame size (header only).
_MIN_FRAME_SIZE = 14

# ARP packet offsets (relative to start of Ethernet payload, i.e. byte 14).
# ARP header: HType(2) + PType(2) + HLen(1) + PLen(1) + Oper(2) = 8 bytes
# Sender MAC: 6 bytes
# Sender IP:  4 bytes  ← this is what we want
# Target MAC: 6 bytes
# Target IP:  4 bytes
_ARP_SENDER_IP_OFFSET = 14 + 8 + 6   # = 28


def _is_arp_frame(frame: bytes) -> bool:
    """Return True if this Ethernet frame carries an ARP packet."""
    if len(frame) < _MIN_FRAME_SIZE:
        return False
    ethertype = struct.unpack("!H", frame[12:14])[0]
    return ethertype == _ARP_ETHERTYPE


def _extract_sender_ip(frame: bytes) -> Optional[str]:
    """
    Extract the ARP sender protocol address (sender IP) from the frame.

    Returns an IPv4 address string or None if the frame is too short
    or the address is obviously useless (all-zeros, link-local, etc.).
    """
    if len(frame) < _ARP_SENDER_IP_OFFSET + 4:
        return None

    ip_bytes = frame[_ARP_SENDER_IP_OFFSET: _ARP_SENDER_IP_OFFSET + 4]
    ip_str   = socket.inet_ntoa(ip_bytes)
    first    = ip_bytes[0]

    # Filter unusable addresses.
    if ip_str == "0.0.0.0":
        return None
    if ip_str.startswith("169.254."):
        return None   # link-local — not a real gateway
    if ip_str.startswith("127."):
        return None   # loopback
    if first == 255 or first == 0:
        return None   # broadcast or reserved

    return ip_str


def get_gateway_candidate(
    interface:    str,
    cancel_event: threading.Event,
    timeout:      float = 3.0,
) -> Optional[str]:
    """
    Listen for ARP traffic on the interface and return the most likely
    gateway IP address.

    Opens a raw socket, collects ARP sender IPs for up to timeout seconds,
    then returns the IP seen most often. The most frequently seen IP on a
    segment is usually the default gateway because it answers ARP probes
    from every device on the VLAN.

    Parameters:
        interface    : Ethernet interface name, e.g. "eth0"
        cancel_event : set externally to abort early
        timeout      : maximum seconds to observe traffic

    Returns:
        IPv4 address string of the best gateway candidate, or None if
        no suitable traffic was seen within the timeout.
    """
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((interface, 0))
    except (PermissionError, OSError) as exc:
        log.error("ARP observation: could not open socket on %s: %s", interface, exc)
        return None

    ip_counts: Counter = Counter()
    deadline = time.monotonic() + timeout

    log.debug("ARP observation started on %s (%.1fs window)", interface, timeout)

    try:
        while not cancel_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                ready, _, _ = select.select([sock], [], [], min(remaining, 0.5))
            except Exception:
                break

            if not ready:
                continue

            try:
                frame = sock.recv(65535)
            except Exception:
                break

            if not _is_arp_frame(frame):
                continue

            sender_ip = _extract_sender_ip(frame)
            if sender_ip:
                ip_counts[sender_ip] += 1
                log.debug("ARP: saw sender IP %s (count=%d)", sender_ip, ip_counts[sender_ip])

    finally:
        try:
            sock.close()
        except Exception:
            pass

    if not ip_counts:
        log.debug("ARP observation: no suitable IPs observed on %s", interface)
        return None

    best_ip, count = ip_counts.most_common(1)[0]
    log.debug(
        "ARP observation: best gateway candidate on %s is %s (seen %d times)",
        interface,
        best_ip,
        count,
    )
    return best_ip
